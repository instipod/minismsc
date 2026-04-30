from datetime import datetime, timezone

import aiosqlite

_SCHEMA = """
CREATE TABLE IF NOT EXISTS sms_queue (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    imsi          TEXT    NOT NULL,
    sender        TEXT    NOT NULL,
    message       TEXT    NOT NULL,
    status        TEXT    NOT NULL DEFAULT 'pending',
    retry_count   INTEGER NOT NULL DEFAULT 0,
    created_at    TEXT    NOT NULL,
    last_tried_at TEXT,
    error_detail  TEXT
);
CREATE INDEX IF NOT EXISTS idx_sms_queue_status ON sms_queue(status);
CREATE INDEX IF NOT EXISTS idx_sms_queue_imsi   ON sms_queue(imsi);

CREATE TABLE IF NOT EXISTS imsi_map (
    imsi       TEXT PRIMARY KEY,
    mme_ip     TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
"""


def _now_iso() -> str:
    return datetime.now(tz=timezone.utc).isoformat()


async def init_db(db_path: str) -> None:
    async with aiosqlite.connect(db_path) as db:
        await db.executescript(_SCHEMA)
        await db.commit()


async def enqueue_sms(db_path: str, imsi: str, sender: str, message: str) -> int:
    async with aiosqlite.connect(db_path) as db:
        cursor = await db.execute(
            "INSERT INTO sms_queue (imsi, sender, message, created_at) VALUES (?, ?, ?, ?)",
            (imsi, sender, message, _now_iso()),
        )
        await db.commit()
        return cursor.lastrowid


async def get_pending_sms(db_path: str) -> list[dict]:
    async with aiosqlite.connect(db_path) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            "SELECT * FROM sms_queue WHERE status = 'pending' ORDER BY created_at ASC"
        )
        rows = await cursor.fetchall()
        return [dict(row) for row in rows]


async def mark_sent(db_path: str, sms_id: int) -> None:
    async with aiosqlite.connect(db_path) as db:
        await db.execute(
            "UPDATE sms_queue SET status = 'sent', last_tried_at = ? WHERE id = ?",
            (_now_iso(), sms_id),
        )
        await db.commit()


async def increment_retry(
    db_path: str, sms_id: int, error: str, max_retries: int
) -> None:
    now = _now_iso()
    async with aiosqlite.connect(db_path) as db:
        cursor = await db.execute(
            "SELECT retry_count FROM sms_queue WHERE id = ?", (sms_id,)
        )
        row = await cursor.fetchone()
        if row is None:
            return
        new_count = row[0] + 1
        new_status = "failed" if new_count >= max_retries else "pending"
        await db.execute(
            """UPDATE sms_queue
               SET retry_count = ?, status = ?, last_tried_at = ?, error_detail = ?
               WHERE id = ?""",
            (new_count, new_status, now, error, sms_id),
        )
        await db.commit()


async def upsert_imsi_mapping(db_path: str, imsi: str, mme_ip: str) -> None:
    async with aiosqlite.connect(db_path) as db:
        await db.execute(
            "INSERT OR REPLACE INTO imsi_map (imsi, mme_ip, updated_at) VALUES (?, ?, ?)",
            (imsi, mme_ip, _now_iso()),
        )
        await db.commit()


async def delete_imsi_mapping(db_path: str, imsi: str) -> None:
    async with aiosqlite.connect(db_path) as db:
        await db.execute("DELETE FROM imsi_map WHERE imsi = ?", (imsi,))
        await db.commit()


async def clear_imsi_mappings(db_path: str) -> None:
    async with aiosqlite.connect(db_path) as db:
        await db.execute("DELETE FROM imsi_map")
        await db.commit()


async def load_imsi_mappings(db_path: str) -> dict:
    async with aiosqlite.connect(db_path) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute("SELECT imsi, mme_ip FROM imsi_map")
        rows = await cursor.fetchall()
        return {row["imsi"]: row["mme_ip"] for row in rows}
