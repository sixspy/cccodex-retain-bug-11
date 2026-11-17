import sqlite3

from app.services.images import image_state, shadow_partition_ids


def scan_images(conn: sqlite3.Connection) -> list[dict]:
    """全量分区映像判定：页面、对账、详情共用同一口径。"""
    shadows = shadow_partition_ids(conn)
    rows = conn.execute(
        "SELECT id, domain_code, biz_date, row_count, storage_state, archive_uri "
        "FROM partitions ORDER BY domain_code, biz_date"
    ).fetchall()
    items: list[dict] = []
    for row in rows:
        part = dict(row)
        image = image_state(part, has_shadow=part["id"] in shadows)
        part["image"] = image
        part["shadow_state"] = "archived" if part["id"] in shadows else None
        # 双影分区的存储态在页面/详情按「双影」呈现，库内原值留在 raw_state。
        part["raw_state"] = part["storage_state"]
        part["display_state"] = "双影" if image["dual"] else part["storage_state"]
        items.append(part)
    return items


def build_summary(
    online: int,
    archived: int,
    destroyed: int,
    pending: int,
    mismatch_count: int,
    dual_count: int,
    superseded: int,
) -> str:
    return (
        f"在线{online} / 已归档{archived} / 已销毁{destroyed} / "
        f"待执行裁决{pending} / 错位{mismatch_count} / 双影{dual_count}"
        f"（回灌作废{superseded}）"
    )


def reconcile_queue(conn: sqlite3.Connection) -> dict:
    """对账队列的即时视图（不落报告）：错位、双影、过期归档痕迹分开列。"""
    items = scan_images(conn)
    dual_items = [
        {
            "partition_id": item["id"],
            "domain_code": item["domain_code"],
            "biz_date": item["biz_date"],
            "storage_state": item["storage_state"],
            "archive_uri": item["archive_uri"],
            "shadow_state": item["shadow_state"],
            "reasons": item["image"]["reasons"],
        }
        for item in items
        if item["image"]["dual"]
    ]
    stale_items = [
        {
            "partition_id": item["id"],
            "domain_code": item["domain_code"],
            "biz_date": item["biz_date"],
            "storage_state": item["storage_state"],
            "archive_uri": item["archive_uri"],
            "shadow_state": item["shadow_state"],
            "reasons": item["image"]["reasons"],
        }
        for item in items
        if item["image"]["stale_shadow"] or item["image"]["broken_archive"]
    ]
    mismatches = conn.execute(
        """
        SELECT j.id AS judgment_id, j.decided_action, j.status AS judgment_status,
               p.id AS partition_id, p.domain_code, p.biz_date,
               p.storage_state, p.archive_uri
        FROM judgments j
        JOIN partitions p ON p.id = j.partition_id
        WHERE j.status = '已完成'
          AND (
            (j.decided_action = '归档' AND (p.storage_state != '已归档' OR p.archive_uri IS NULL))
            OR (j.decided_action = '销毁' AND p.storage_state != '已销毁')
          )
        ORDER BY j.id
        """
    ).fetchall()
    pending = conn.execute(
        "SELECT COUNT(*) AS c FROM judgments WHERE status IN ('待执行','执行中')"
    ).fetchone()["c"]
    # 双影分区已单列，不再重复算进错位。
    dual_ids = {d["partition_id"] for d in dual_items}
    mismatch_items = [
        dict(m) for m in mismatches if int(m["partition_id"]) not in dual_ids
    ]
    return {
        "mismatches": mismatch_items,
        "duals": dual_items,
        "stale_archive_traces": stale_items,
        "pending_judgment_count": int(pending),
        "counts": {
            "mismatch": len(mismatch_items),
            "dual": len(dual_items),
            "stale": len(stale_items),
        },
    }


def run_reconcile(conn: sqlite3.Connection, as_of_date: str) -> dict:
    """策略视角与实物分区对账。

    错位只算「完成裁决与当前单态不符」，回灌作废的归档裁决不再算错位；
    双影单独计数（dual_count），既不在线计数里混，也不当普通错位稀释。
    """
    items = scan_images(conn)
    dual_ids = {item["id"] for item in items if item["image"]["dual"]}
    # 单态计数 + 双影单列 = 分区总数，避免同一条分区在两种口径里各报一次。
    counts: dict[str, int] = {}
    for item in items:
        if item["id"] in dual_ids:
            continue
        counts[item["storage_state"]] = counts.get(item["storage_state"], 0) + 1
    online = int(counts.get("在线", 0))
    archived = int(counts.get("已归档", 0))
    destroyed = int(counts.get("已销毁", 0))
    pending = conn.execute(
        "SELECT COUNT(*) AS c FROM judgments WHERE status IN ('待执行','执行中')"
    ).fetchone()["c"]

    dual_count = len(dual_ids)

    # 历史完成裁决与实物单态不符即为错位；双影分区单独归入双影，
    # 由 dual_count 呈现，避免同一条分区在「错位」与「双影」两个口径里各报一次。
    mismatches = conn.execute(
        """
        SELECT j.id AS judgment_id, j.decided_action, j.status AS judgment_status,
               p.id AS partition_id, p.storage_state, p.archive_uri
        FROM judgments j
        JOIN partitions p ON p.id = j.partition_id
        WHERE j.status = '已完成'
          AND (
            (j.decided_action = '归档' AND (p.storage_state != '已归档' OR p.archive_uri IS NULL))
            OR (j.decided_action = '销毁' AND p.storage_state != '已销毁')
          )
        ORDER BY j.id
        """
    ).fetchall()
    mismatch_items = [
        dict(m) for m in mismatches if int(m["partition_id"]) not in dual_ids
    ]
    superseded = conn.execute(
        "SELECT COUNT(*) AS c FROM judgments WHERE status = '已作废'"
    ).fetchone()["c"]
    mismatch_count = len(mismatch_items)

    dual_items = [
        {
            "partition_id": item["id"],
            "domain_code": item["domain_code"],
            "biz_date": item["biz_date"],
            "storage_state": item["storage_state"],
            "archive_uri": item["archive_uri"],
            "shadow_state": item["shadow_state"],
            "reasons": item["image"]["reasons"],
        }
        for item in items
        if item["image"]["dual"]
    ]

    stale_items = [
        {
            "partition_id": item["id"],
            "domain_code": item["domain_code"],
            "biz_date": item["biz_date"],
            "storage_state": item["storage_state"],
            "archive_uri": item["archive_uri"],
        }
        for item in items
        if item["image"]["stale_shadow"] or item["image"]["broken_archive"]
    ]

    summary = build_summary(
        online, archived, destroyed, int(pending), mismatch_count, dual_count,
        int(superseded),
    )
    cur = conn.execute(
        "INSERT INTO reconcile_reports "
        "(as_of_date, online_count, archived_count, destroyed_count, "
        "pending_judgment_count, mismatch_count, dual_count, summary) "
        "VALUES (?,?,?,?,?,?,?,?)",
        (
            as_of_date,
            online,
            archived,
            destroyed,
            int(pending),
            mismatch_count,
            dual_count,
            summary,
        ),
    )
    report_id = int(cur.lastrowid)
    row = conn.execute(
        "SELECT id, as_of_date, online_count, archived_count, destroyed_count, "
        "pending_judgment_count, mismatch_count, dual_count, summary, created_at "
        "FROM reconcile_reports WHERE id = ?",
        (report_id,),
    ).fetchone()
    report = dict(row)
    report["mismatches"] = mismatch_items
    report["duals"] = dual_items
    report["stale_archive_traces"] = stale_items
    return report


def list_reports(conn: sqlite3.Connection) -> list[dict]:
    rows = conn.execute(
        "SELECT id, as_of_date, online_count, archived_count, destroyed_count, "
        "pending_judgment_count, mismatch_count, dual_count, summary, created_at "
        "FROM reconcile_reports ORDER BY id DESC"
    ).fetchall()
    return [dict(r) for r in rows]
