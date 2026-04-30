import asyncio
import logging

from config import settings

from .builder import build_downlink_unitdata, build_location_update_accept, build_reset_ack, encode_lai, generate_tmsi
from .imsi import decode_imsi
from .parser import SgsapMessage
from .protocol import IEType, MsgType

_lai_bytes = encode_lai(settings.vlr_mcc, settings.vlr_mnc, settings.vlr_lac)

log = logging.getLogger(__name__)


async def handle_message(
    msg: SgsapMessage,
    mme_ip: str,
    transport: asyncio.Transport,
    app_state,
) -> None:
    match msg.msg_type:
        case MsgType.LOCATION_UPDATE_REQUEST:
            await _handle_location_update(msg, mme_ip, transport, app_state)
        case MsgType.SERVICE_REQUEST:
            await _handle_service_request(msg, mme_ip, transport, app_state)
        case MsgType.UPLINK_UNITDATA:
            await _handle_uplink_unitdata(msg, mme_ip, transport, app_state)
        case MsgType.PAGING_REJECT:
            await _handle_paging_reject(msg, mme_ip, app_state)
        case MsgType.IMSI_DETACH_INDICATION:
            await _handle_imsi_detach(msg, mme_ip, app_state)
        case MsgType.TMSI_REALLOCATION_COMPLETE:
            _handle_tmsi_reallocation_complete(msg, mme_ip)
        case MsgType.RESET_INDICATION:
            _handle_reset(mme_ip, transport)
        case _:
            log.warning(
                "Unhandled SGsAP message type 0x%02X from %s", msg.msg_type, mme_ip
            )


async def _handle_location_update(
    msg: SgsapMessage,
    mme_ip: str,
    transport: asyncio.Transport,
    app_state,
) -> None:
    imsi_raw = msg.ies.get(IEType.IMSI)
    if not imsi_raw:
        log.error("LOCATION_UPDATE_REQUEST missing IMSI IE from %s", mme_ip)
        return
    try:
        imsi = decode_imsi(imsi_raw)
    except ValueError as exc:
        log.error("Failed to decode IMSI from %s: %s", mme_ip, exc)
        return

    mme_name_raw = msg.ies.get(IEType.MME_NAME)
    mme_name = mme_name_raw.decode("ascii", errors="replace") if mme_name_raw else mme_ip

    await app_state.update_imsi(imsi, mme_ip)
    from db.sms_queue import upsert_imsi_mapping
    await upsert_imsi_mapping(settings.db_path, imsi, mme_ip)
    log.info("Location update: IMSI %s registered to MME %s (%s)", imsi, mme_ip, mme_name)

    tmsi = generate_tmsi()
    response = build_location_update_accept(imsi_raw, _lai_bytes, tmsi)
    transport.write(response)
    log.debug("Assigned TMSI 0x%08X to IMSI %s", tmsi, imsi)


async def _delivery_timeout(app_state, imsi: str, mr: int, sms_id: int, db_path: str) -> None:
    try:
        await asyncio.sleep(5.0)
        info = await app_state.pop_delivery(imsi, mr)
        if info is not None:
            from db.sms_queue import increment_retry
            await increment_retry(db_path, sms_id, "RP-DATA delivery timeout", settings.sms_max_retries)
            log.warning("RP-DATA timeout IMSI=%s MR=%d SMS id=%d", imsi, mr, sms_id)
    except asyncio.CancelledError:
        pass


async def _handle_service_request(
    msg: SgsapMessage,
    mme_ip: str,
    transport: asyncio.Transport,
    app_state,
) -> None:
    from db.sms_queue import increment_retry, upsert_imsi_mapping
    from sms.pdu import build_mt_sms_nas_pdu

    imsi_raw = msg.ies.get(IEType.IMSI)
    if not imsi_raw:
        log.error("SERVICE_REQUEST missing IMSI IE from %s", mme_ip)
        return
    try:
        imsi = decode_imsi(imsi_raw)
    except ValueError as exc:
        log.error("Failed to decode IMSI from SERVICE_REQUEST from %s: %s", mme_ip, exc)
        return

    log.info("SERVICE_REQUEST: IMSI %s now ECM-CONNECTED via %s", imsi, mme_ip)
    await app_state.update_imsi(imsi, mme_ip)
    await upsert_imsi_mapping(settings.db_path, imsi, mme_ip)
    await app_state.cancel_paging_timer(imsi)

    items = await app_state.dequeue_paging(imsi)
    if not items:
        log.debug("SERVICE_REQUEST for IMSI %s: no pending paging items", imsi)
        return

    for item in items:
        sms_id = item.get("sms_id")
        try:
            mr = await app_state.next_mr(imsi)
            nas_pdu = build_mt_sms_nas_pdu(item["sender"], item["message"], mr=mr)
            sgsap_pdu = build_downlink_unitdata(imsi_raw, nas_pdu)
            transport.write(sgsap_pdu)
            if sms_id is not None:
                timeout_task = asyncio.create_task(
                    _delivery_timeout(app_state, imsi, mr, sms_id, settings.db_path)
                )
                await app_state.register_delivery(imsi, mr, sms_id, timeout_task)
            log.info("DOWNLINK_UNITDATA sent to IMSI %s (SMS id=%s MR=%d)", imsi, sms_id, mr)
        except Exception as exc:
            log.error("Failed to deliver SMS id=%s to IMSI %s: %s", sms_id, imsi, exc)
            if sms_id is not None:
                await increment_retry(settings.db_path, sms_id, str(exc), settings.sms_max_retries)


async def _handle_uplink_unitdata(
    msg: SgsapMessage,
    mme_ip: str,
    transport: asyncio.Transport,
    app_state,
) -> None:
    from db.sms_queue import increment_retry, mark_sent
    from sms.pdu import build_cp_ack, build_rp_ack_nas_pdu, decode_uplink_nas_pdu

    imsi_raw = msg.ies.get(IEType.IMSI)
    nas_pdu = msg.ies.get(IEType.NAS_MESSAGE_CONTAINER)
    imsi = "UNKNOWN"
    if imsi_raw:
        try:
            imsi = decode_imsi(imsi_raw)
        except ValueError:
            pass

    if not nas_pdu:
        log.warning("[UL] IMSI=%s: no NAS PDU in UPLINK-UNITDATA", imsi)
        return

    try:
        decoded = decode_uplink_nas_pdu(nas_pdu)
    except Exception as exc:
        log.warning("[UL] IMSI=%s decode error: %s  hex=%s", imsi, exc, nas_pdu.hex())
        return

    ul_type = decoded.get("type", "UNKNOWN")
    pd_ti = decoded.get("pd_ti", 0x09)
    rp_mr = decoded.get("mr", 0)

    if ul_type == "MO-SMS":
        if imsi_raw:
            # CP-ACK stops TC1 on the UE (CP retransmission timer)
            transport.write(build_downlink_unitdata(imsi_raw, build_cp_ack(pd_ti)))
            # CP-DATA(RP-ACK) stops TRP1 on the UE (RP retransmission timer)
            transport.write(build_downlink_unitdata(imsi_raw, build_rp_ack_nas_pdu(pd_ti, rp_mr)))
        dest = decoded.get("destination", "?")
        text = decoded.get("text")
        if text is not None:
            log.info("[MO-SMS] IMSI=%s → %s: %r", imsi, dest, text)
        else:
            log.info("[MO-SMS] IMSI=%s → %s  tpdu_error=%s  hex=%s",
                     imsi, dest, decoded.get("decode_error"), decoded.get("tpdu_hex"))
        await _mo_sms_callback(imsi, dest, text)
    elif ul_type == "RP-ACK":
        if imsi_raw:
            transport.write(build_downlink_unitdata(imsi_raw, build_cp_ack(pd_ti)))
        delivery = await app_state.pop_delivery(imsi, rp_mr)
        if delivery is not None:
            timeout_task = delivery.get("timeout_task")
            if timeout_task and not timeout_task.done():
                timeout_task.cancel()
            sms_id = delivery["sms_id"]
            await mark_sent(settings.db_path, sms_id)
            log.info("[MT-DELIVERED] IMSI=%s MR=%d SMS id=%d", imsi, rp_mr, sms_id)
        else:
            log.info("[MT-DELIVERED] IMSI=%s MR=%d (no tracked delivery)", imsi, rp_mr)
    elif ul_type == "RP-ERROR":
        if imsi_raw:
            transport.write(build_downlink_unitdata(imsi_raw, build_cp_ack(pd_ti)))
        delivery = await app_state.pop_delivery(imsi, rp_mr)
        if delivery is not None:
            timeout_task = delivery.get("timeout_task")
            if timeout_task and not timeout_task.done():
                timeout_task.cancel()
            sms_id = delivery["sms_id"]
            cause = decoded.get("cause", 0)
            await increment_retry(settings.db_path, sms_id,
                                  f"RP-ERROR cause={cause}", settings.sms_max_retries)
            log.warning("[MT-REJECTED] IMSI=%s MR=%d RP-cause=%d SMS id=%d",
                        imsi, rp_mr, cause, sms_id)
        else:
            log.warning("[MT-REJECTED] IMSI=%s MR=%d RP-cause=%d (no tracked delivery)",
                        imsi, rp_mr, decoded.get("cause", 0))
    elif ul_type == "CP-ACK":
        log.debug("[CP-ACK] IMSI=%s", imsi)
    else:
        log.info("[UL] IMSI=%s type=%s", imsi, ul_type)


async def _mo_sms_callback(imsi: str, destination: str, text: str | None) -> None:
    """
    Callback for incoming MO SMS from a UE.
    Replace this function body to forward to an upstream SMS system.
    """


def _handle_tmsi_reallocation_complete(msg: SgsapMessage, mme_ip: str) -> None:
    imsi_raw = msg.ies.get(IEType.IMSI)
    if imsi_raw:
        try:
            imsi = decode_imsi(imsi_raw)
            log.debug("TMSI-REALLOCATION-COMPLETE: IMSI %s confirmed TMSI from %s", imsi, mme_ip)
        except ValueError:
            pass


def _handle_reset(mme_ip: str, transport: asyncio.Transport) -> None:
    log.info("RESET_INDICATION from %s — sending RESET_ACK", mme_ip)
    transport.write(build_reset_ack())


async def _handle_imsi_detach(msg: SgsapMessage, mme_ip: str, app_state) -> None:
    from db.sms_queue import delete_imsi_mapping

    imsi_raw = msg.ies.get(IEType.IMSI)
    if not imsi_raw:
        log.error("IMSI_DETACH_INDICATION missing IMSI IE from %s", mme_ip)
        return
    try:
        imsi = decode_imsi(imsi_raw)
    except ValueError as exc:
        log.error("Failed to decode IMSI from IMSI_DETACH_INDICATION from %s: %s", mme_ip, exc)
        return

    await app_state.delete_imsi(imsi)
    await delete_imsi_mapping(settings.db_path, imsi)
    log.info("IMSI-DETACH: removed mapping for IMSI %s (from MME %s)", imsi, mme_ip)


async def _handle_paging_reject(msg: SgsapMessage, mme_ip: str, app_state) -> None:
    from db.sms_queue import increment_retry

    imsi_raw = msg.ies.get(IEType.IMSI)
    if not imsi_raw:
        log.error("PAGING_REJECT missing IMSI IE from %s", mme_ip)
        return
    try:
        imsi = decode_imsi(imsi_raw)
    except ValueError as exc:
        log.error("Failed to decode IMSI from PAGING_REJECT from %s: %s", mme_ip, exc)
        return

    cause_raw = msg.ies.get(IEType.SGSAP_CAUSE)
    cause = cause_raw[0] if cause_raw else 0

    await app_state.cancel_paging_timer(imsi)
    items = await app_state.dequeue_paging(imsi)
    for item in items:
        sms_id = item.get("sms_id")
        if sms_id is not None:
            await increment_retry(settings.db_path, sms_id,
                                  f"paging rejected cause={cause}", settings.sms_max_retries)
    log.warning("PAGING-REJECT for IMSI %s cause=%d: %d SMS requeued", imsi, cause, len(items))
