import sqlite3

from app.services.images import image_state, shadow_partition_ids


def list_partitions(
    conn: sqlite3.Connection,
    domain_code: str | None = None,
    storage_state: str | None = None,
) -> list[dict]:
    clauses: list[str] = []
    params: list[object] = []
    if domain_code:
        clauses.append("domain_code = ?")
        params.append(domain_code)
    where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
    rows = conn.execute(
        "SELECT id, domain_code, biz_date, row_count, storage_state, "
        "archive_uri, created_at FROM partitions"
        + where
        + " ORDER BY domain_code, biz_date",
        params,
    ).fetchall()
    shadows = shadow_partition_ids(conn)
    items: list[dict] = []
    for row in rows:
        part = dict(row)
        image = image_state(part, has_shadow=part["id"] in shadows)
        part["image"] = image
        part["raw_state"] = part["storage_state"]
        part["display_state"] = "双影" if image["dual"] else part["storage_state"]
        part["shadow_state"] = "archived" if part["id"] in shadows else None
        items.append(part)
    if storage_state == "双影":
        # 双影是映像判定而非库内存储态：库层按判定过滤，页面/接口口径才一致。
        items = [p for p in items if p["image"]["dual"]]
    elif storage_state:
        items = [p for p in items if p["storage_state"] == storage_state]
    return items


def get_partition(conn: sqlite3.Connection, partition_id: int) -> dict | None:
    row = conn.execute(
        "SELECT id, domain_code, biz_date, row_count, storage_state, "
        "archive_uri, created_at FROM partitions WHERE id = ?",
        (partition_id,),
    ).fetchone()
    return dict(row) if row else None
