"""归档回灌闭环：单态归一、双影专拦、对账口径、写锁与整笔退回。"""

import threading
import time

import pytest

from app.db import connect, get_db
from app.services import (
    execute_judgment,
    list_judgments,
    list_partitions,
    reinject_partition,
    run_expire_scan,
    run_reconcile,
)
from app.services.images import shadow_key
from app.services.reinject import ReinjectError, get_partition_detail
import app.services.reinject as reinject_module


def _archive_one(conn) -> tuple[int, int]:
    """跑到期扫描并归档一条裁决，返回 (partition_id, judgment_id)。"""
    run_expire_scan(conn, "2026-11-16")
    pending = [
        j for j in list_judgments(conn, status="待执行") if j["decided_action"] == "归档"
    ]
    assert pending, "应有一条归档裁决"
    judgment = pending[0]
    execute_judgment(conn, judgment["id"])
    return judgment["partition_id"], judgment["id"]


def _plant_dual(conn, partition_id: int) -> None:
    """复刻被修掉的旧回灌：置在线但不清归档地址，另留影子标记。"""
    conn.execute(
        "UPDATE partitions SET storage_state = '在线' WHERE id = ?", (partition_id,)
    )
    conn.execute(
        "INSERT OR REPLACE INTO meta (key, value) VALUES (?, 'archived')",
        (shadow_key(partition_id),),
    )


def test_reinject_archived_partition_returns_online_single_state(db_path):
    with get_db(db_path) as conn:
        partition_id, _ = _archive_one(conn)

    with get_db(db_path) as conn:
        item = reinject_partition(conn, partition_id)
        assert item["storage_state"] == "在线"
        assert item["archive_uri"] is None
        assert item["image"]["state"] == "单态"
        assert item["shadow_state"] is None
        # 快照语义：回灌前的实物与路径都留在审计里
        assert item["reinject_runs"][0]["mode"] == "对照临时副本"
        assert item["reinject_runs"][0]["snapshot_state"] == "已归档"
        assert item["reinject_runs"][0]["snapshot_archive_uri"]
        assert item["reinject_runs"][0]["ok"] == 1

    with get_db(db_path) as conn:
        shadows = conn.execute(
            "SELECT COUNT(*) AS c FROM meta WHERE key LIKE 'shadow:%'"
        ).fetchone()["c"]
        assert shadows == 0
        # 原归档裁决作废：不再被对账当成现行错位
        judgments = [j for j in list_judgments(conn) if j["partition_id"] == partition_id]
        assert judgments[0]["status"] == "已作废"


def test_reconcile_reports_zero_mismatch_and_zero_dual_after_reinject(db_path):
    with get_db(db_path) as conn:
        partition_id, _ = _archive_one(conn)

    with get_db(db_path) as conn:
        before = run_reconcile(conn, "2026-11-16")
        assert before["dual_count"] == 0
        assert before["mismatch_count"] == 0

        reinject_partition(conn, partition_id)
        after = run_reconcile(conn, "2026-11-16")
        assert after["dual_count"] == 0
        assert after["mismatch_count"] == 0
        assert after["duals"] == []
        # 在线数包含刚回灌的分区，且没有被重复计数
        assert after["online_count"] == before["online_count"] + 1


def test_scan_does_not_reopen_judgment_for_reinjected_partition(db_path):
    with get_db(db_path) as conn:
        partition_id, _ = _archive_one(conn)
        reinject_partition(conn, partition_id)

    with get_db(db_path) as conn:
        first = [j for j in list_judgments(conn) if j["partition_id"] == partition_id]
        run_expire_scan(conn, "2026-11-16")
        second = [j for j in list_judgments(conn) if j["partition_id"] == partition_id]
        # 回灌出在线单态后重新可裁撤，但同一轮扫描不重复叠单
        assert len(second) == len(first) + 1
        assert [j["status"] for j in second].count("待执行") == 1


def test_dual_image_is_intercepted_by_scan_reconcile_and_reinject(db_path):
    with get_db(db_path) as conn:
        partition_id, _ = _archive_one(conn)
        _plant_dual(conn, partition_id)

    with get_db(db_path) as conn:
        # 页面与库内同判：显示态为双影，原因可读
        listed = [p for p in list_partitions(conn) if p["id"] == partition_id][0]
        assert listed["display_state"] == "双影"
        assert listed["image"]["dual"] is True
        assert listed["image"]["reasons"]

        # 双影专拦：对账单列，不混进错位
        report = run_reconcile(conn, "2026-11-16")
        assert report["dual_count"] == 1
        assert report["mismatch_count"] == 0

        # 双影专拦：扫描不为它新开裁决
        count_before = len(
            [j for j in list_judgments(conn) if j["partition_id"] == partition_id]
        )
        run_expire_scan(conn, "2026-11-16")
        count_after = len(
            [j for j in list_judgments(conn) if j["partition_id"] == partition_id]
        )
        assert count_after == count_before

        # 双影专拦：未显式归一不允许回灌，且不落任何写入
        with pytest.raises(ReinjectError) as exc:
            reinject_partition(conn, partition_id)
        assert exc.value.code == "DUAL_IMAGE"
        still = get_partition_detail(conn, partition_id)
        assert still["image"]["dual"] is True

        # 显式归一路径才允许落库
        repaired = reinject_partition(conn, partition_id, allow_dual_repair=True)
        assert repaired["storage_state"] == "在线"
        assert repaired["archive_uri"] is None
        assert repaired["image"]["state"] == "单态"
        assert repaired["shadow_state"] is None


def test_reinject_rejects_second_path(db_path):
    with get_db(db_path) as conn:
        partition_id, _ = _archive_one(conn)

    with get_db(db_path) as conn:
        with pytest.raises(ReinjectError) as exc:
            reinject_partition(conn, partition_id, mode="直接升主轨")
        assert exc.value.code == "MODE_NOT_SUPPORTED"


def test_reinject_failure_rolls_back_whole_transaction(db_path):
    with get_db(db_path) as conn:
        partition_id, _ = _archive_one(conn)

    calls = {"n": 0}
    original = reinject_module.image_state

    def failing(part, has_shadow=False):
        calls["n"] += 1
        if calls["n"] >= 3:  # 升主轨后的落库核对失败
            raise RuntimeError("simulated promote verification failure")
        return original(part, has_shadow)

    with get_db(db_path) as conn:
        reinject_module.image_state = failing
        try:
            with pytest.raises(RuntimeError):
                reinject_partition(conn, partition_id)
        finally:
            reinject_module.image_state = original

    with get_db(db_path) as conn:
        # 整笔退回：分区仍是回灌前的已归档单态，影子标记没有留下
        item = get_partition_detail(conn, partition_id)
        assert item["storage_state"] == "已归档"
        assert item["archive_uri"]
        assert item["image"]["state"] == "单态"
        assert item["shadow_state"] is None
        # 失败审计单独提交，可追溯
        last = item["reinject_runs"][0]
        assert last["ok"] == 0
        assert "simulated" in last["detail"]
        # 锁已释放
        assert conn.execute("SELECT COUNT(*) AS c FROM reinject_locks").fetchone()["c"] == 0


def test_two_backfills_write_one_result(db_path):
    with get_db(db_path) as conn:
        partition_id, _ = _archive_one(conn)

    original = reinject_module.image_state

    def slow(part, has_shadow=False):
        time.sleep(0.25)
        return original(part, has_shadow)

    results: list[tuple[str, str]] = []
    reinject_module.image_state = slow

    def runner(name: str) -> None:
        try:
            reinject_partition(connect(db_path), partition_id, owner=name)
            results.append((name, "ok"))
        except ReinjectError as exc:
            results.append((name, exc.code))

    try:
        threads = [threading.Thread(target=runner, args=(f"job{i}",)) for i in range(2)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
    finally:
        reinject_module.image_state = original

    assert sorted(code for _, code in results) == ["ALREADY_ONLINE", "ok"]
    with get_db(db_path) as conn:
        item = get_partition_detail(conn, partition_id)
        assert item["storage_state"] == "在线"
        assert item["archive_uri"] is None
        # 只落一笔成功回灌，账实一致
        ok_runs = [r for r in item["reinject_runs"] if r["ok"] == 1]
        assert len(ok_runs) == 1
        assert conn.execute("SELECT COUNT(*) AS c FROM reinject_locks").fetchone()["c"] == 0


def test_reinject_api_returns_code_and_detail(authed):
    scan = authed.post("/api/scan-jobs", json={"as_of_date": "2026-11-16"})
    assert scan.status_code == 200
    pending = authed.get(
        "/api/judgments", params={"status": "待执行"}
    ).json()["items"]
    archive = [j for j in pending if j["decided_action"] == "归档"][0]
    exe = authed.post(f"/api/judgments/{archive['id']}/execute")
    assert exe.status_code == 200
    partition_id = archive["partition_id"]

    detail = authed.get(f"/api/partitions/{partition_id}")
    assert detail.status_code == 200
    assert detail.json()["item"]["storage_state"] == "已归档"

    reinjected = authed.post(
        f"/api/partitions/{partition_id}/reinject",
        json={"mode": "对照临时副本"},
    )
    assert reinjected.status_code == 200
    item = reinjected.json()["item"]
    assert item["image"]["state"] == "单态"
    assert item["archive_uri"] is None

    queue = authed.get("/api/reconcile-queue").json()
    assert queue["counts"]["dual"] == 0
    assert queue["counts"]["mismatch"] == 0
