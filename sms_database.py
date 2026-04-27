"""
SQLite database for SMS message persistence and store-and-forward capability
"""
import sqlite3
import uuid
import time
import threading
from typing import Optional, List, Dict, Any
from contextlib import contextmanager
import logging

logger = logging.getLogger(__name__)


class SMSDatabase:
    """Thread-safe SQLite database for SMS message storage"""

    def __init__(self, db_path: str = "sms.db"):
        self.db_path = db_path
        self.lock = threading.Lock()
        self._init_db()
        logger.info(f"SMS Database initialized at {db_path}")

    def _init_db(self):
        """Initialize database schema"""
        with self._get_conn() as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS messages (
                    guid TEXT PRIMARY KEY,
                    imsi TEXT NOT NULL,
                    msisdn TEXT NOT NULL,
                    sender TEXT NOT NULL,
                    message_text TEXT NOT NULL,
                    status TEXT NOT NULL,
                    ti INTEGER,
                    created_at REAL NOT NULL,
                    last_attempt_at REAL,
                    retry_count INTEGER DEFAULT 0,
                    do_not_deliver_after REAL,
                    store_until REAL NOT NULL,
                    error_reason TEXT,
                    request_delivery_report INTEGER DEFAULT 0
                )
            """)

            conn.execute("CREATE INDEX IF NOT EXISTS idx_status ON messages(status)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_ti ON messages(ti)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_store_until ON messages(store_until)")

            logger.debug("Database schema initialized")

    @contextmanager
    def _get_conn(self):
        """Thread-safe database connection context manager"""
        with self.lock:
            conn = sqlite3.connect(self.db_path)
            conn.row_factory = sqlite3.Row
            try:
                yield conn
                conn.commit()
            except Exception:
                conn.rollback()
                raise
            finally:
                conn.close()

    def insert_message(self, imsi: str, msisdn: str, sender: str,
                      text: str, request_delivery_report: bool,
                      do_not_deliver_after: Optional[float] = None,
                      store_until: Optional[float] = None) -> str:
        """
        Insert new message, return GUID

        Args:
            imsi: Subscriber IMSI
            msisdn: Subscriber phone number
            sender: Sender phone number
            text: Message text
            request_delivery_report: Whether to request delivery report
            do_not_deliver_after: Unix timestamp, retry until this time (default: 24 hours)
            store_until: Unix timestamp, delete after this (default: 7 days)

        Returns:
            GUID string for tracking
        """
        guid = str(uuid.uuid4())
        created_at = time.time()

        # Default: store_until = 7 days, do_not_deliver_after = 24 hours
        if store_until is None:
            store_until = created_at + (7 * 24 * 3600)
        if do_not_deliver_after is None:
            do_not_deliver_after = created_at + (24 * 3600)

        with self._get_conn() as conn:
            conn.execute("""
                INSERT INTO messages (guid, imsi, msisdn, sender, message_text,
                                     status, created_at, do_not_deliver_after,
                                     store_until, request_delivery_report)
                VALUES (?, ?, ?, ?, ?, 'queued', ?, ?, ?, ?)
            """, (guid, imsi, msisdn, sender, text, created_at,
                  do_not_deliver_after, store_until,
                  1 if request_delivery_report else 0))

        logger.debug(f"Inserted message {guid} for IMSI {imsi}")
        return guid

    def update_status(self, guid: str, status: str,
                     error_reason: Optional[str] = None):
        """Update message status"""
        with self._get_conn() as conn:
            conn.execute("""
                UPDATE messages
                SET status = ?, error_reason = ?
                WHERE guid = ?
            """, (status, error_reason, guid))

        logger.debug(f"Updated message {guid} status to {status}")

    def mark_sent(self, guid: str, ti: int):
        """Mark message as sent (assign TI and increment retry count)"""
        with self._get_conn() as conn:
            conn.execute("""
                UPDATE messages
                SET ti = ?, last_attempt_at = ?, retry_count = retry_count + 1
                WHERE guid = ?
            """, (ti, time.time(), guid))

        logger.debug(f"Marked message {guid} as sent with TI={ti}")

    def reset_ti(self, guid: str):
        """Reset TI to NULL (for retry)"""
        with self._get_conn() as conn:
            conn.execute("UPDATE messages SET ti = NULL WHERE guid = ?", (guid,))

    def get_by_guid(self, guid: str) -> Optional[Dict[str, Any]]:
        """Get message by GUID"""
        with self._get_conn() as conn:
            row = conn.execute(
                "SELECT * FROM messages WHERE guid = ?", (guid,)
            ).fetchone()
            return dict(row) if row else None

    def get_by_ti(self, ti: int) -> Optional[Dict[str, Any]]:
        """Get message by TI (not in delivered/failed state)"""
        with self._get_conn() as conn:
            row = conn.execute("""
                SELECT * FROM messages
                WHERE ti = ? AND status NOT IN ('delivered', 'failed')
                ORDER BY last_attempt_at DESC
                LIMIT 1
            """, (ti,)).fetchone()
            return dict(row) if row else None

    def get_by_imsi_acknowledged(self, imsi: str) -> Optional[Dict[str, Any]]:
        """Get most recent acknowledged message for IMSI"""
        with self._get_conn() as conn:
            row = conn.execute("""
                SELECT * FROM messages
                WHERE imsi = ? AND status = 'acknowledged'
                ORDER BY last_attempt_at DESC
                LIMIT 1
            """, (imsi,)).fetchone()
            return dict(row) if row else None

    def get_queued(self, limit: int = 100) -> List[Dict[str, Any]]:
        """Get queued messages ready to send"""
        with self._get_conn() as conn:
            rows = conn.execute("""
                SELECT * FROM messages
                WHERE status = 'queued'
                ORDER BY created_at ASC
                LIMIT ?
            """, (limit,)).fetchall()
            return [dict(row) for row in rows]

    def get_pending_for_retry(self) -> List[Dict[str, Any]]:
        """Get messages that might need retry (acknowledged or retrying state)"""
        with self._get_conn() as conn:
            rows = conn.execute("""
                SELECT * FROM messages
                WHERE status IN ('retrying', 'acknowledged')
                  AND last_attempt_at IS NOT NULL
                  AND ti IS NOT NULL
                ORDER BY last_attempt_at ASC
            """).fetchall()
            return [dict(row) for row in rows]

    def get_all_pending(self) -> List[Dict[str, Any]]:
        """Get all pending messages (for recovery)"""
        with self._get_conn() as conn:
            rows = conn.execute("""
                SELECT * FROM messages
                WHERE status IN ('acknowledged', 'retrying')
            """).fetchall()
            return [dict(row) for row in rows]

    def cleanup_expired(self) -> int:
        """Delete messages past store_until, return count"""
        current_time = time.time()
        with self._get_conn() as conn:
            cursor = conn.execute("""
                DELETE FROM messages
                WHERE store_until < ?
            """, (current_time,))
            count = cursor.rowcount

        if count > 0:
            logger.info(f"Cleaned up {count} expired messages")

        return count
