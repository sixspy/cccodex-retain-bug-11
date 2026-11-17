"""归档回灌：把已归档分区一次性切回在线。

口径选择（只此一条，不做第二条隐式路径）
--------------------------------------
本实现采用「对照临时副本 → 升主轨」这一条路径：

1. 在同一个库的同一个写事务里先对 partitions 行做快照；
2. 把候选映像写进 TEMP 表 reinject_stage（对照临时副本），
   在副本上核对「结果映像」与「库内分区态」是否都是单态；
3. 核对通过后用一条带快照条件的 UPDATE（compare-and-swap）升主轨，
   同时清掉归档地址与 meta 里的 shadow:<id> 残留；
4. 任一环节失败（含核对不通过、行被并发改动、DB 写锁拿不到）
   整笔 ROLLBACK，partitions / meta / judgments 一起退回，不存在
   「在线又已归档」的中间态。

「直接升主轨」的旁路不实现：两条路径会让失败语义与对账口径分叉。

写锁与双写
----------
两名补跑同时回灌同一分区时，先抢 reinject_locks 行（短事务提交，带过期时间），
抢不到直接 409，不进入主事务；主事务本身再用 BEGIN IMMEDIATE 拿库级写锁。
因此不会出现两路都写、账实各记一笔的情况。

快照字段（reinject_runs）
------------------------
previous_state / previous_archive_uri / snapshot_row_count 等记录回灌前实物，
snapshot_state 为回灌前存储态，mode 固定为「对照临时副本」，
detail 写明结果。失败也落一条 ok=0 的审计，但业务表已整笔退回。
"""

import json
import sqlite3
import uuid
from contextlib import contextmanager

from app.services.images import image_state, shadow_key, shadow_marker

REINJECT_MODE = "对照临时副本"
TARGET_STATE = "在线"
LOCK_SECONDS = 60

STAGE_DDL = (
    "CREATE TEMP TABLE IF NOT EXISTS reinject_stage ("
    "partition_id INTEGER PRIMARY KEY,"
    "domain_code TEXT NOT NULL,"
    "biz_date TEXT NOT NULL,"
    "row_count INTEGER NOT NULL,"
    "storage_state TEXT NOT NULL,"
    "archive_uri TEXT)"
)


class ReinjectError(ValueError):
    """带稳定错误码的回灌拒绝/失败，接口层据此返回 409。"""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


def _db_path(conn: sqlite3.Connection) -> str | None:
    for row in conn.execute("PRAGMA database_list").fetchall():
        if row["name"] == "main" and row["file"]:
            return str(row["file"])
    return None


@contextmanager
def _lock_conn(conn: sqlite3.Connection):
    """锁/审计用的独立连接：让另一路补跑在对方提交前就能看见「已占用」。"""
    path = _db_path(conn)
    if path is None or conn.in_transaction:
        # 内存库或调用方已在事务里：只能退回到同一连接内判定。
        yield None
        return
    other = sqlite3.connect(path, timeout=5.0)
    other.row_factory = sqlite3.Row
    other.execute("PRAGMA busy_timeout = 5000")
    try:
        yield other
    finally:
        other.close()


def _acquire_lock(conn: sqlite3.Connection, holder: sqlite3.Connection | None,
                  partition_id: int, owner: str) -> None:
    target = holder or conn
    # holder 是独立连接：自己起事务、自己提交，锁行对另一路立即可见。
    # 没有独立连接时（内存库/调用方已在事务内）用 SAVEPOINT，绝不 rollback 外层。
    savepoint = None
    try:
        if holder is not None:
            holder.execute("BEGIN IMMEDIATE")
        elif target.in_transaction:
            savepoint = f"lock_{uuid.uuid4().hex[:8]}"
            target.execute(f"SAVEPOINT {savepoint}")
        target.execute(
            "DELETE FROM reinject_locks WHERE expires_at <= datetime('now')"
        )
        row = target.execute(
            "SELECT owner FROM reinject_locks WHERE partition_id = ?",
            (partition_id,),
        ).fetchone()
        if row:
            raise ReinjectError(
                "LOCKED",
                f"分区 {partition_id} 正由另一路补跑回灌（{row['owner']}），"
                "本次不写库，避免回灌双写",
            )
        target.execute(
            "INSERT INTO reinject_locks (partition_id, owner, expires_at) "
            "VALUES (?,?,datetime('now', ?))",
            (partition_id, owner, f"+{LOCK_SECONDS} seconds"),
        )
        if holder is not None:
            holder.commit()
        elif savepoint:
            target.execute(f"RELEASE SAVEPOINT {savepoint}")
    except sqlite3.OperationalError as exc:
        if holder is not None:
            holder.rollback()
        elif savepoint:
            target.execute(f"ROLLBACK TO SAVEPOINT {savepoint}")
            target.execute(f"RELEASE SAVEPOINT {savepoint}")
        raise ReinjectError(
            "LOCKED", f"回灌写锁被占用：{exc}；本次不写库"
        ) from exc
    except Exception:
        if holder is not None:
            holder.rollback()
        elif savepoint:
            target.execute(f"ROLLBACK TO SAVEPOINT {savepoint}")
            target.execute(f"RELEASE SAVEPOINT {savepoint}")
        raise


def _release_lock(conn: sqlite3.Connection, holder: sqlite3.Connection | None,
                  partition_id: int, owner: str) -> None:
    target = holder or conn
    try:
        target.execute(
            "DELETE FROM reinject_locks WHERE partition_id = ? AND owner = ?",
            (partition_id, owner),
        )
        if holder is not None:
            target.commit()
    except sqlite3.Error:
        if holder is not None:
            target.rollback()


class _Tx:
    """回灌业务事务：调用方已开事务则用 SAVEPOINT，否则 BEGIN IMMEDIATE。

    BEGIN IMMEDIATE 是真正的库级写锁：两路补跑同时回灌时，后到者排队，
    拿到锁后重读分区态，已在线单态就拒绝，不会叠出第二笔写入。
    """

    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn
        self.nested = conn.in_transaction
        self.name = f"reinject_{uuid.uuid4().hex[:8]}"

    def __enter__(self):
        if self.nested:
            self.conn.execute(f"SAVEPOINT {self.name}")
        else:
            try:
                self.conn.execute("BEGIN IMMEDIATE")
            except sqlite3.OperationalError as exc:
                # 库级写锁在超时内没拿到：不写任何业务行，直接按重入对待。
                raise ReinjectError(
                    "LOCKED", f"回灌写锁被占用：{exc}；本次不写库"
                ) from exc
        return self.conn

    def __exit__(self, exc_type, exc, tb):
        if exc_type is not None:
            self.rollback()
        return False

    def commit(self):
        if self.nested:
            self.conn.execute(f"RELEASE SAVEPOINT {self.name}")
        else:
            self.conn.commit()

    def rollback(self):
        if self.nested:
            self.conn.execute(f"ROLLBACK TO SAVEPOINT {self.name}")
            self.conn.execute(f"RELEASE SAVEPOINT {self.name}")
        else:
            self.conn.rollback()


def _fail_audit(
    conn: sqlite3.Connection,
    partition_id: int,
    snapshot: dict | None,
    previous: dict | None,
    detail: str,
) -> None:
    """业务事务已回滚后的失败审计：单独提交，保证失败可追溯。"""
    snapshot = snapshot or {}
    previous = previous or {}
    sql = (
        "INSERT INTO reinject_runs "
        "(partition_id, mode, target_state, snapshot_state, "
        "snapshot_archive_uri, snapshot_row_count, previous_state, "
        "previous_archive_uri, precheck, ok, detail) "
        "VALUES (?,?,?,?,?,?,?,?,?,0,?)"
    )
    params = (
        partition_id,
        REINJECT_MODE,
        TARGET_STATE,
        str(snapshot.get("storage_state") or previous.get("storage_state") or ""),
        snapshot.get("archive_uri"),
        int(snapshot.get("row_count") or 0),
        str(previous.get("storage_state") or ""),
        previous.get("archive_uri"),
        str(snapshot.get("precheck") or ""),
        detail,
    )
    try:
        if conn.in_transaction:
            # 同一连接上还挂着调用方事务：审计单独用 SAVEPOINT，避免连带回滚。
            name = f"audit_{uuid.uuid4().hex[:8]}"
            conn.execute(f"SAVEPOINT {name}")
            conn.execute(sql, params)
            conn.execute(f"RELEASE SAVEPOINT {name}")
        else:
            conn.execute(sql, params)
            conn.commit()
    except sqlite3.Error:
        if conn.in_transaction:
            conn.rollback()


def reinject_partition(
    conn: sqlite3.Connection,
    partition_id: int,
    *,
    mode: str = REINJECT_MODE,
    allow_dual_repair: bool = False,
    owner: str | None = None,
) -> dict:
    """把分区回灌为在线单态；失败整笔退回并记失败审计。"""
    if mode != REINJECT_MODE:
        raise ReinjectError(
            "MODE_NOT_SUPPORTED",
            f"回灌只走「{REINJECT_MODE}」一条路径，不接受：{mode}",
        )

    owner = owner or f"reinject:{partition_id}:{uuid.uuid4().hex[:8]}"
    snapshot: dict | None = None
    previous: dict | None = None
    with _lock_conn(conn) as holder:
        _acquire_lock(conn, holder, partition_id, owner)
        try:
            with _Tx(conn) as tx:
                row = tx.execute(
                    "SELECT * FROM partitions WHERE id = ?", (partition_id,)
                ).fetchone()
                if not row:
                    raise ReinjectError("NOT_FOUND", "分区不存在")

                part = dict(row)
                previous = {
                    "storage_state": part["storage_state"],
                    "archive_uri": part["archive_uri"],
                    "row_count": int(part["row_count"]),
                }
                shadow = shadow_marker(tx, partition_id)
                current_image = image_state(part, has_shadow=shadow is not None)

                if part["storage_state"] == "已销毁":
                    raise ReinjectError("DESTROYED", "已销毁分区不可回灌")
                if part["storage_state"] == "裁撤中":
                    raise ReinjectError("IN_FLIGHT", "分区正在裁撤中，不可回灌")

                # 双影专拦：当前库内态与回灌结果必须同判。
                has_trace = (
                    current_image["dual"]
                    or current_image["stale_shadow"]
                    or current_image["broken_archive"]
                )
                if has_trace and not allow_dual_repair:
                    raise ReinjectError(
                        "DUAL_IMAGE",
                        "双影专拦：当前分区"
                        + "、".join(current_image["reasons"] or ["归档痕迹未收尾"])
                        + "；需显式确认归一后才可回灌",
                    )
                if part["storage_state"] == "在线" and not has_trace:
                    raise ReinjectError("ALREADY_ONLINE", "分区已是在线单态，无需回灌")

                precheck = json.dumps(
                    {
                        "current_state": part["storage_state"],
                        "current_archive_uri": part["archive_uri"],
                        "current_shadow": shadow,
                        "dual": current_image["dual"],
                        "reasons": current_image["reasons"],
                        "mode": REINJECT_MODE,
                        "target": TARGET_STATE,
                    },
                    ensure_ascii=False,
                    sort_keys=True,
                )
                snapshot = {
                    "storage_state": part["storage_state"],
                    "archive_uri": part["archive_uri"],
                    "row_count": int(part["row_count"]),
                    "precheck": precheck,
                }

                # 对照临时副本：候选映像先落在同库 TEMP 表，核对通过后才升主轨。
                tx.execute(STAGE_DDL)
                tx.execute(
                    "DELETE FROM reinject_stage WHERE partition_id = ?", (partition_id,)
                )
                tx.execute(
                    "INSERT INTO reinject_stage "
                    "(partition_id, domain_code, biz_date, row_count, "
                    "storage_state, archive_uri) "
                    "VALUES (?,?,?,?,?,NULL)",
                    (
                        partition_id,
                        part["domain_code"],
                        part["biz_date"],
                        int(part["row_count"]),
                        TARGET_STATE,
                    ),
                )
                staged = dict(
                    tx.execute(
                        "SELECT * FROM reinject_stage WHERE partition_id = ?",
                        (partition_id,),
                    ).fetchone()
                )
                result_image = image_state(staged, has_shadow=False)
                if result_image["dual"] or not result_image["online_image"]:
                    raise ReinjectError(
                        "DUAL_IMAGE",
                        "双影专拦：对照副本核出的回灌结果不是在线单态（"
                        + "、".join(result_image["reasons"] or ["结果映像非法"])
                        + "），整笔退回",
                    )

                # 升主轨：带快照条件的 compare-and-swap，期间行被改动即放弃。
                cur = tx.execute(
                    "UPDATE partitions SET storage_state = ?, archive_uri = NULL "
                    "WHERE id = ? AND storage_state = ? AND archive_uri IS ?",
                    (
                        TARGET_STATE,
                        partition_id,
                        snapshot["storage_state"],
                        snapshot["archive_uri"],
                    ),
                )
                if cur.rowcount != 1:
                    raise ReinjectError(
                        "CAS_CONFLICT", "分区在回灌期间被改动，整笔退回"
                    )
                tx.execute(
                    "DELETE FROM meta WHERE key = ?", (shadow_key(partition_id),)
                )

                # 归档裁决被回灌取代：标作废，对账不把历史归档当现行错位。
                tx.execute(
                    "UPDATE judgments SET status = '已作废', fail_reason = ? "
                    "WHERE partition_id = ? AND status = '已完成' "
                    "AND decided_action = '归档'",
                    ("归档回灌：分区已切回在线，原归档裁决作废", partition_id),
                )

                promoted = dict(
                    tx.execute(
                        "SELECT * FROM partitions WHERE id = ?", (partition_id,)
                    ).fetchone()
                )
                promoted_image = image_state(
                    promoted, has_shadow=shadow_marker(tx, partition_id) is not None
                )
                if promoted_image["dual"] or not promoted_image["online_image"]:
                    raise ReinjectError(
                        "DUAL_IMAGE",
                        "双影专拦：落库后的分区映像不是在线单态，整笔退回",
                    )

                tx.execute(
                    "INSERT INTO reinject_runs "
                    "(partition_id, mode, target_state, snapshot_state, "
                    "snapshot_archive_uri, snapshot_row_count, previous_state, "
                    "previous_archive_uri, precheck, ok, detail) "
                    "VALUES (?,?,?,?,?,?,?,?,?,1,?)",
                    (
                        partition_id,
                        REINJECT_MODE,
                        TARGET_STATE,
                        snapshot["storage_state"],
                        snapshot["archive_uri"],
                        snapshot["row_count"],
                        snapshot["storage_state"],
                        snapshot["archive_uri"],
                        precheck,
                        "回灌完成：对照副本核对通过，升主轨为在线单态，"
                        "归档地址与影子标记已清理",
                    ),
                )
                tx.execute(
                    "DELETE FROM reinject_stage WHERE partition_id = ?", (partition_id,)
                )
                tx.commit()
        except Exception as exc:
            # 业务事务整笔退回；失败审计走锁连接单独提交，保证可追溯。
            _fail_audit(holder or conn, partition_id, snapshot, previous, str(exc))
            raise
        finally:
            _release_lock(conn, holder, partition_id, owner)

    return get_partition_detail(conn, partition_id)


def get_partition_detail(conn: sqlite3.Connection, partition_id: int) -> dict | None:
    row = conn.execute(
        "SELECT id, domain_code, biz_date, row_count, storage_state, archive_uri, created_at "
        "FROM partitions WHERE id = ?",
        (partition_id,),
    ).fetchone()
    if not row:
        return None
    part = dict(row)
    shadow = shadow_marker(conn, partition_id)
    image = image_state(part, has_shadow=shadow is not None)
    latest = conn.execute(
        "SELECT id, status, fail_reason, decided_action, created_at FROM judgments "
        "WHERE partition_id = ? ORDER BY id DESC LIMIT 1",
        (partition_id,),
    ).fetchone()
    runs = conn.execute(
        "SELECT id, mode, target_state, snapshot_state, snapshot_archive_uri, "
        "snapshot_row_count, previous_state, previous_archive_uri, precheck, ok, "
        "detail, created_at FROM reinject_runs WHERE partition_id = ? "
        "ORDER BY id DESC LIMIT 10",
        (partition_id,),
    ).fetchall()
    part["shadow_state"] = shadow
    part["image"] = image
    part["latest_judgment"] = dict(latest) if latest else None
    part["reinject_runs"] = [dict(r) for r in runs]
    return part


def list_reinject_runs(conn: sqlite3.Connection, limit: int = 100) -> list[dict]:
    rows = conn.execute(
        "SELECT id, partition_id, mode, target_state, snapshot_state, "
        "snapshot_archive_uri, snapshot_row_count, previous_state, "
        "previous_archive_uri, precheck, ok, detail, created_at "
        "FROM reinject_runs ORDER BY id DESC LIMIT ?",
        (limit,),
    ).fetchall()
    return [dict(r) for r in rows]
