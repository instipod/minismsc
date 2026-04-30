import asyncio
import logging

from db.sms_queue import get_pending_sms, increment_retry
from sgsap.builder import build_paging_request
from sgsap.imsi import encode_imsi

log = logging.getLogger(__name__)


async def _paging_timeout(app_state, imsi: str, db_path: str) -> None:
    try:
        await asyncio.sleep(5.0)
        async with app_state._lock:
            app_state.paging_timers.pop(imsi, None)
        items = await app_state.dequeue_paging(imsi)
        for item in items:
            sms_id = item.get("sms_id")
            if sms_id is not None:
                from config import settings
                await increment_retry(db_path, sms_id, "paging timeout", settings.sms_max_retries)
        if items:
            log.warning("Paging timeout IMSI=%s: %d SMS requeued", imsi, len(items))
    except asyncio.CancelledError:
        pass


async def start_retry_task(app_state, db_path: str, interval: int) -> None:
    """
    Background task: retry pending queued SMS every `interval` seconds.
    Sends SGsAP-PAGING-REQUEST to wake the UE; the SERVICE_REQUEST handler
    delivers the actual DOWNLINK-UNITDATA and marks SMS as sent.
    Runs until cancelled (clean shutdown via CancelledError re-raise).
    """
    log.info("SMS retry task started (interval=%ds)", interval)
    while True:
        try:
            await asyncio.sleep(interval)
            await _retry_pending(app_state, db_path)
        except asyncio.CancelledError:
            log.info("SMS retry task stopped")
            raise
        except Exception:
            log.exception("Unexpected error in SMS retry task")


async def _retry_pending(app_state, db_path: str) -> None:
    from config import settings

    pending = await get_pending_sms(db_path)
    if not pending:
        return
    log.debug("Retry pass: %d pending SMS", len(pending))

    for sms in pending:
        transport = await app_state.get_transport_for_imsi(sms["imsi"])
        if transport is None or transport.is_closing():
            continue

        try:
            item = {"sms_id": sms["id"], "sender": sms["sender"], "message": sms["message"]}
            should_page = await app_state.enqueue_paging(sms["imsi"], item)
            if should_page:
                imsi_bytes = encode_imsi(sms["imsi"])
                paging_pdu = build_paging_request(imsi_bytes, settings.vlr_name)
                transport.write(paging_pdu)
                log.info(
                    "Retry: PAGING_REQUEST sent for IMSI %s (SMS id=%d)",
                    sms["imsi"],
                    sms["id"],
                )
                task = asyncio.create_task(_paging_timeout(app_state, sms["imsi"], db_path))
                old = await app_state.set_paging_timer(sms["imsi"], task)
                if old and not old.done():
                    old.cancel()
            # else: already paging this IMSI — SERVICE_REQUEST will deliver it
        except Exception as exc:
            await increment_retry(db_path, sms["id"], str(exc), settings.sms_max_retries)
            log.warning("Retry paging failed: SMS id=%d error=%s", sms["id"], exc)
