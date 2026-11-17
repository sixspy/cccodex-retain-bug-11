"""分区实物映像判定：单态 / 双影。

回灌（把已归档分区切回在线）最容易留下的脏态是「既像在线又像已归档」：
- partitions.storage_state = 在线，但 archive_uri 仍在；
- meta 里仍残留 `shadow:<partition_id>` 归档影子标记。

本模块只做只读判定，写入侧的拦截与清理在 reinject.py，
对账侧的独立计数在 reconcile.py。判定口径必须唯一，避免页面、扫描、对账各说各话。
"""

SHADOW_KEY_PREFIX = "shadow:"


def shadow_key(partition_id: int) -> str:
    return f"{SHADOW_KEY_PREFIX}{partition_id}"


def shadow_partition_ids(conn) -> set[int]:
    """一次取出所有残留影子标记的分区 id，避免逐行查询。"""
    rows = conn.execute(
        "SELECT key FROM meta WHERE key LIKE ?", (SHADOW_KEY_PREFIX + "%",)
    ).fetchall()
    ids: set[int] = set()
    for row in rows:
        raw = str(row["key"])[len(SHADOW_KEY_PREFIX) :]
        try:
            ids.add(int(raw))
        except ValueError:
            continue
    return ids


def shadow_marker(conn, partition_id: int) -> str | None:
    row = conn.execute(
        "SELECT value FROM meta WHERE key = ?", (shadow_key(partition_id),)
    ).fetchone()
    return str(row["value"]) if row else None


def image_state(part: dict, has_shadow: bool = False) -> dict:
    """返回分区实物映像判定。

    part: 至少含 id / storage_state / archive_uri。
    has_shadow: meta 中是否存在该分区的归档影子标记。

    双影（dual）只认「在线 / 裁撤中 / 已销毁」等非归档态却仍带归档痕迹，
    或影子标记与当前存储态不匹配。已归档且带 archive_uri 是正常单态。
    """
    state = str(part.get("storage_state") or "")
    archive_uri = part.get("archive_uri")
    reasons: list[str] = []

    if state == "在线" and archive_uri:
        reasons.append("在线分区仍带归档地址")
    if state == "在线" and has_shadow:
        reasons.append("在线分区仍带归档影子标记")
    if state == "裁撤中" and archive_uri:
        reasons.append("裁撤中分区仍带归档地址")
    if state == "已销毁" and (archive_uri or has_shadow):
        reasons.append("已销毁分区仍带归档痕迹")

    dual = bool(reasons)

    # 已归档 + 归档地址是正常单态；残留影子标记只是上一轮没有收尾，不构成双影。
    stale_shadow = state == "已归档" and has_shadow and not dual
    broken = state == "已归档" and not archive_uri
    if broken:
        reasons.append("已归档分区缺少归档地址")

    return {
        "state": "双影" if dual else "单态",
        "dual": dual,
        "stale_shadow": stale_shadow,
        "broken_archive": broken,
        "reasons": reasons,
        "online_image": state == "在线" and not dual,
        "archived_image": state == "已归档" and not broken,
    }
