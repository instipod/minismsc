import asyncio
import logging

from fastapi import APIRouter, Request
from pydantic import BaseModel

from config import settings
from db.sms_queue import enqueue_sms
from sgsap.builder import build_paging_request
from sgsap.imsi import encode_imsi
from sms.retry import _paging_timeout

router = APIRouter(tags=["sms"])
log = logging.getLogger(__name__)


class SendSmsRequest(BaseModel):
    imsi: str
    sender: str
    message: str


class SendSmsResponse(BaseModel):
    status: str       # "paging" | "queued"
    sms_id: int | None = None


@router.post("/sms", response_model=SendSmsResponse)
async def send_sms(body: SendSmsRequest, request: Request) -> SendSmsResponse:
    state = request.app.state.app_state

    sms_id = await enqueue_sms(settings.db_path, body.imsi, body.sender, body.message)
    log.info("SMS queued: id=%d IMSI=%s sender=%s", sms_id, body.imsi, body.sender)

    transport = await state.get_transport_for_imsi(body.imsi)
    if transport is not None and not transport.is_closing():
        try:
            item = {"sms_id": sms_id, "sender": body.sender, "message": body.message}
            should_page = await state.enqueue_paging(body.imsi, item)
            if should_page:
                imsi_bytes = encode_imsi(body.imsi)
                paging_pdu = build_paging_request(imsi_bytes, settings.vlr_name)
                transport.write(paging_pdu)
                log.info("PAGING_REQUEST sent for IMSI=%s", body.imsi)
                task = asyncio.create_task(_paging_timeout(state, body.imsi, settings.db_path))
                old = await state.set_paging_timer(body.imsi, task)
                if old and not old.done():
                    old.cancel()
            return SendSmsResponse(status="paging", sms_id=sms_id)
        except Exception as exc:
            log.warning("Paging failed for IMSI %s: %s — SMS remains queued", body.imsi, exc)

    return SendSmsResponse(status="queued", sms_id=sms_id)
