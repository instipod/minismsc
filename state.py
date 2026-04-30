import asyncio
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any


@dataclass
class MmeConnection:
    ip: str
    port: int
    connected_at: datetime
    transport: Any  # _MmeTransport; Any avoids a circular import


@dataclass
class AppState:
    mmes: dict[str, MmeConnection] = field(default_factory=dict)
    imsi_map: dict[str, str] = field(default_factory=dict)
    # imsi → pending SMS items waiting to be delivered after UE responds to paging
    paging_queue: dict[str, list[dict]] = field(default_factory=dict)
    # (imsi, mr) → {"sms_id": int, "timeout_task": asyncio.Task}
    pending_delivery: dict[tuple, dict] = field(default_factory=dict)
    # imsi → active paging-timeout asyncio.Task
    paging_timers: dict[str, Any] = field(default_factory=dict)
    # imsi → next RP message reference (0-255 wraparound)
    _mr_counters: dict[str, int] = field(default_factory=dict)
    _lock: asyncio.Lock = field(default_factory=asyncio.Lock)

    async def register_mme(self, ip: str, port: int, transport: Any) -> None:
        async with self._lock:
            self.mmes[ip] = MmeConnection(
                ip=ip,
                port=port,
                connected_at=datetime.utcnow(),
                transport=transport,
            )

    async def deregister_mme(self, ip: str) -> None:
        async with self._lock:
            self.mmes.pop(ip, None)

    async def update_imsi(self, imsi: str, mme_ip: str) -> None:
        async with self._lock:
            self.imsi_map[imsi] = mme_ip

    async def get_transport_for_imsi(self, imsi: str) -> Any | None:
        async with self._lock:
            mme_ip = self.imsi_map.get(imsi)
            if mme_ip is None:
                return None
            conn = self.mmes.get(mme_ip)
            if conn is None:
                return None
            return conn.transport

    async def clear_imsi_map(self) -> None:
        async with self._lock:
            self.imsi_map.clear()

    async def delete_imsi(self, imsi: str) -> bool:
        async with self._lock:
            if imsi not in self.imsi_map:
                return False
            del self.imsi_map[imsi]
            return True

    async def enqueue_paging(self, imsi: str, item: dict) -> bool:
        """
        Add an SMS item to the in-memory paging queue for imsi.
        Returns True if this is the first item (caller should send PAGING-REQUEST).
        De-duplicates by sms_id so the retry task can't double-queue the same SMS.
        """
        async with self._lock:
            queue = self.paging_queue.setdefault(imsi, [])
            sms_id = item.get("sms_id")
            if sms_id is not None and any(e.get("sms_id") == sms_id for e in queue):
                return False
            was_empty = len(queue) == 0
            queue.append(item)
            return was_empty

    async def dequeue_paging(self, imsi: str) -> list[dict]:
        """Remove and return all queued paging items for imsi."""
        async with self._lock:
            return self.paging_queue.pop(imsi, [])

    async def next_mr(self, imsi: str) -> int:
        """Return the next RP message reference for imsi and advance the counter."""
        async with self._lock:
            val = self._mr_counters.get(imsi, 0)
            self._mr_counters[imsi] = (val + 1) % 256
            return val

    async def register_delivery(self, imsi: str, mr: int, sms_id: int, timeout_task: Any) -> None:
        """Track an in-flight MT SMS delivery keyed by (imsi, mr)."""
        async with self._lock:
            self.pending_delivery[(imsi, mr)] = {"sms_id": sms_id, "timeout_task": timeout_task}

    async def pop_delivery(self, imsi: str, mr: int) -> dict | None:
        """Remove and return the delivery record for (imsi, mr), or None if not found."""
        async with self._lock:
            return self.pending_delivery.pop((imsi, mr), None)

    async def set_paging_timer(self, imsi: str, task: Any) -> Any | None:
        """Store a new paging timer task, returning the previous one (caller must cancel it)."""
        async with self._lock:
            old = self.paging_timers.get(imsi)
            self.paging_timers[imsi] = task
            return old

    async def cancel_paging_timer(self, imsi: str) -> None:
        """Cancel and remove any active paging timer for imsi."""
        async with self._lock:
            task = self.paging_timers.pop(imsi, None)
        if task is not None and not task.done():
            task.cancel()
