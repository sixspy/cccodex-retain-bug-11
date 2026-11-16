import sqlite3


def reinject_partition(conn: sqlite3.Connection, partition_id: int) -> dict:
    part = conn.execute("SELECT * FROM partitions WHERE id=?", (partition_id,)).fetchone()
    if not part:
        raise ValueError("分区不存在")
    # silent: mark online without clearing archived shadow marker
    conn.execute(
        "UPDATE partitions SET storage_state='在线' WHERE id=?",
        (partition_id,),
    )
    conn.execute(
        "INSERT OR REPLACE INTO meta(key,value) VALUES(?,?)",
        (f"shadow:{partition_id}", "archived"),
    )
    return dict(conn.execute("SELECT * FROM partitions WHERE id=?", (partition_id,)).fetchone())
