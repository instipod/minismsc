#!/usr/bin/env python3
"""
Mini SMSC - Basic SMS Center for Open5GS
Communicates with MME over SGs interface using SCTP
"""

import socket
import logging
import threading
import time
from typing import Optional, Dict, List
from dataclasses import dataclass
from datetime import datetime, timezone

from sgsap_protocol import (
    SGsAPMessage, SGsAPMessageType, SGsAPIEI,
    create_downlink_unitdata, create_reset_indication, create_reset_ack,
    create_location_update_accept, create_location_update_reject,
    create_imsi_detach_ack, create_eps_detach_ack,
    decode_imsi, decode_location_area_id
)
from sms_encoder import create_sms_deliver_pdu, create_rp_data_dl, create_cp_data, create_cp_ack
from sms_database import SMSDatabase


logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s'
)
logger = logging.getLogger(__name__)


@dataclass
class SMSMessage:
    """SMS message to be delivered"""
    destination_imsi: str
    destination_msisdn: str
    sender: str
    text: str
    timestamp: datetime
    request_delivery_report: bool = False


@dataclass
class PendingSMS:
    """Tracks a pending SMS awaiting acknowledgment"""
    imsi: str
    nas_message: bytes
    ti: int
    sent_time: float
    retry_count: int = 0
    max_retries: int = 5


class MMEConnection:
    """Represents a single MME connection with its own state"""

    def __init__(self, sock: socket.socket, address: tuple, vlr_name: str):
        self.sock = sock
        self.host: str = address[0]
        self.port: int = address[1]
        self.key: str = f"{address[0]}:{address[1]}"
        self.vlr_name = vlr_name
        self.connected = True
        self.connected_at: datetime = datetime.now(timezone.utc)
        self.mme_name: Optional[str] = None

        # Per-connection TI and RP reference counters
        self.ti_counter = 0
        self.rp_reference = 0

        # Pending SMS tracking (TI → GUID) for this connection
        self.pending_sms: Dict[int, str] = {}
        self.pending_lock = threading.Lock()

    def get_available_ti(self) -> Optional[int]:
        """Return next free TI slot (0-6), or None if all are in use"""
        with self.pending_lock:
            for ti in range(7):
                if ti not in self.pending_sms:
                    return ti
        return None

    def to_dict(self) -> dict:
        return {
            'address': self.host,
            'port': self.port,
            'mme_name': self.mme_name,
            'connected_at': self.connected_at.isoformat() + 'Z',
            'pending_sms_count': len(self.pending_sms),
        }


class SMSCService:
    """SMSC Service - handles SMS delivery via SGs interface"""

    def __init__(self, listen_address: str, listen_port: int, vlr_name: str,
                 lai_mcc: str = "001", lai_mnc: str = "01", lai_lac: int = 1,
                 smsc_address: str = "+0000", db_path: str = "sms.db"):
        self.listen_address = listen_address
        self.listen_port = listen_port
        self.vlr_name = vlr_name
        self.lai_mcc = lai_mcc
        self.lai_mnc = lai_mnc
        self.lai_lac = lai_lac
        self.smsc_address = smsc_address

        self.server_sock: Optional[socket.socket] = None
        self.running = False

        # Multiple MME connections: key = "host:port"
        self.mme_connections: Dict[str, MMEConnection] = {}
        self.connections_lock = threading.RLock()

        # Database for message persistence
        self.db = SMSDatabase(db_path)

        # IMSI → MME address mapping (loaded from DB, updated on Location Update / Detach)
        self.imsi_to_mme: Dict[str, str] = self.db.load_imsi_mme_mappings()

        self.retry_timeout = 3.0
        self.max_retries = 5

        self._recover_pending_messages()

        logger.info(f"SMSC/VLR initialized - VLR: {vlr_name}, LAI: {lai_mcc}-{lai_mnc}-{lai_lac}")

    # ------------------------------------------------------------------
    # Properties
    # ------------------------------------------------------------------

    @property
    def connected(self) -> bool:
        """True if at least one MME is currently connected"""
        with self.connections_lock:
            return any(c.connected for c in self.mme_connections.values())

    def get_connected_mmes(self) -> List[dict]:
        """Return info dicts for all currently connected MMEs"""
        with self.connections_lock:
            return [c.to_dict() for c in self.mme_connections.values() if c.connected]

    # ------------------------------------------------------------------
    # Startup / teardown
    # ------------------------------------------------------------------

    def _recover_pending_messages(self):
        """Recover pending messages from database on startup"""
        pending = self.db.get_all_pending()
        logger.info(f"Recovering {len(pending)} pending messages from database")
        for msg_row in pending:
            self.db.reset_ti(msg_row['guid'])
            self.db.update_status(msg_row['guid'], 'queued')
        logger.info("Message recovery complete")

    def listen(self):
        """Bind the SGsAP server socket and start accepting connections (non-blocking)."""
        try:
            try:
                import sctp  # type: ignore[import-untyped]
                self.server_sock = sctp.sctpsocket_tcp(socket.AF_INET)
            except ImportError:
                logger.warning("SCTP module not available, using TCP fallback")
                self.server_sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)

            self.server_sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            self.server_sock.bind((self.listen_address, self.listen_port))
            self.server_sock.listen(5)
            logger.info(f"Listening for MME connections on {self.listen_address}:{self.listen_port}")

            accept_thread = threading.Thread(target=self._accept_loop, daemon=True)
            accept_thread.start()

        except Exception as e:
            logger.error(f"Failed to start SGsAP server: {e}")
            raise

    def _accept_loop(self):
        """Background thread: accept new MME connections indefinitely."""
        while self.server_sock is not None:
            try:
                self.server_sock.settimeout(1.0)
                sock, addr = self.server_sock.accept()
                logger.info(f"New MME connection from {addr}")

                mme = MMEConnection(sock, addr, self.vlr_name)

                with self.connections_lock:
                    self.mme_connections[mme.key] = mme

                self._send_reset_indication(mme)

                recv_thread = threading.Thread(
                    target=self._mme_receiver_loop,
                    args=(mme,),
                    daemon=True
                )
                recv_thread.start()

            except socket.timeout:
                continue
            except OSError:
                if self.server_sock is not None:
                    logger.error("Accept loop encountered an error")
                break

    def disconnect(self):
        """Disconnect all MMEs and close the server socket."""
        with self.connections_lock:
            for mme in list(self.mme_connections.values()):
                mme.connected = False
                try:
                    mme.sock.close()
                except Exception:
                    pass
            self.mme_connections.clear()
            self.imsi_to_mme.clear()

        if self.server_sock:
            try:
                self.server_sock.close()
            except Exception:
                pass
            self.server_sock = None

        logger.info("Disconnected from all MMEs")

    # ------------------------------------------------------------------
    # Per-MME send / receive
    # ------------------------------------------------------------------

    def _send_to_mme(self, mme: MMEConnection, msg: SGsAPMessage):
        """Send an SGsAP message to a specific MME."""
        if not mme.connected:
            raise RuntimeError(f"MME {mme.key} is not connected")

        data = msg.encode()
        logger.info(f"[{mme.key}] Sending {msg.msg_type.name} ({len(data)} bytes)")
        mme.sock.sendall(data)

    def _receive_from_mme(self, mme: MMEConnection, timeout: float = 1.0) -> Optional[SGsAPMessage]:
        """Receive one SGsAP message from a specific MME."""
        if not mme.connected:
            return None

        try:
            mme.sock.settimeout(timeout)
            data = mme.sock.recv(4096)

            if not data:
                logger.warning(f"[{mme.key}] Connection closed by MME")
                mme.connected = False
                return None

            msg = SGsAPMessage.decode(data)
            logger.info(f"[{mme.key}] Received {msg.msg_type.name} ({len(data)} bytes)")
            if msg.msg_type == SGsAPMessageType.LOCATION_UPDATE_REQUEST and SGsAPIEI.IMSI in msg.ies:
                logger.info(f"  IMSI raw bytes: {msg.ies[SGsAPIEI.IMSI].hex()}")
            return msg

        except socket.timeout:
            return None
        except Exception as e:
            logger.error(f"[{mme.key}] Error receiving message: {e}")
            mme.connected = False
            return None

    def _send_reset_indication(self, mme: MMEConnection):
        """Send Reset Indication to a specific MME."""
        msg = create_reset_indication(self.vlr_name)
        self._send_to_mme(mme, msg)
        logger.info(f"[{mme.key}] Sent Reset Indication")

    # ------------------------------------------------------------------
    # Per-MME receiver thread
    # ------------------------------------------------------------------

    def _mme_receiver_loop(self, mme: MMEConnection):
        """Background thread: receive and dispatch messages from one MME."""
        while mme.connected:
            msg = self._receive_from_mme(mme, timeout=1.0)
            if msg:
                self._handle_incoming_message(msg, mme)

        # Cleanup on disconnect
        logger.info(f"[{mme.key}] Receiver loop exiting, cleaning up")
        with self.connections_lock:
            for imsi, mme_addr in list(self.imsi_to_mme.items()):
                if mme_addr == mme.host:
                    del self.imsi_to_mme[imsi]
            self.mme_connections.pop(mme.key, None)

    # ------------------------------------------------------------------
    # SMS queueing
    # ------------------------------------------------------------------

    def send_sms(self, imsi: str, msisdn: str, sender: str, text: str,
                 request_delivery_report: bool = False,
                 do_not_deliver_after: Optional[float] = None,
                 store_until: Optional[float] = None) -> str:
        """Queue an SMS for delivery. Returns a GUID for tracking."""
        guid = self.db.insert_message(
            imsi=imsi,
            msisdn=msisdn,
            sender=sender,
            text=text,
            request_delivery_report=request_delivery_report,
            do_not_deliver_after=do_not_deliver_after,
            store_until=store_until
        )
        logger.info(f"SMS queued for {imsi} GUID={guid}: {text[:50]!r} (delivery_report={request_delivery_report})")
        return guid

    # ------------------------------------------------------------------
    # SMS processing
    # ------------------------------------------------------------------

    def _process_sms(self, guid: str):
        """Attempt to send a queued SMS to the appropriate MME."""
        try:
            msg_row = self.db.get_by_guid(guid)
            if not msg_row:
                logger.error(f"GUID {guid} not found in database")
                return

            if msg_row['do_not_deliver_after']:
                if time.time() > msg_row['do_not_deliver_after']:
                    logger.warning(f"GUID {guid} expired (do_not_deliver_after)")
                    self.db.update_status(guid, 'failed', 'Expired: do_not_deliver_after')
                    return

            # Route to the MME that has this IMSI registered
            with self.connections_lock:
                mme_address = self.imsi_to_mme.get(msg_row['imsi'])
                target_mmes = []

                if mme_address:
                    # IMSI is known - find the MME connection by address
                    for mme in self.mme_connections.values():
                        if mme.host == mme_address and mme.connected:
                            target_mmes = [mme]
                            break
                else:
                    # IMSI is unknown - broadcast to all connected MMEs
                    logger.info(f"IMSI {msg_row['imsi']} not mapped to any MME, broadcasting to all connected MMEs")
                    target_mmes = [mme for mme in self.mme_connections.values() if mme.connected]

            if not target_mmes:
                logger.debug(f"No connected MME for IMSI {msg_row['imsi']}, deferring GUID {guid}")
                return

            # Send to all target MMEs
            for mme in target_mmes:
                ti = mme.get_available_ti()
                if ti is None:
                    logger.debug(f"No available TI slots on {mme.key} for GUID {guid}")
                    continue

                tpdu = create_sms_deliver_pdu(
                    msg_row['sender'],
                    msg_row['message_text'],
                    request_status_report=bool(msg_row['request_delivery_report'])
                )

                mme.rp_reference = (mme.rp_reference + 1) % 256
                rp_data = create_rp_data_dl(
                    msg_row['msisdn'],
                    tpdu,
                    mme.rp_reference,
                    self.smsc_address,
                    include_destination=False
                )
                logger.info(f"  TPDU ({len(tpdu)} bytes): {tpdu.hex()}")
                logger.info(f"  RP-DATA ({len(rp_data)} bytes): {rp_data.hex()}")

                nas_message = create_cp_data(rp_data, ti)
                logger.info(f"  CP-DATA/NAS ({len(nas_message)} bytes): {nas_message.hex()}")

                self.db.mark_sent(guid, ti)

                with mme.pending_lock:
                    mme.pending_sms[ti] = guid

                sgsap_msg = create_downlink_unitdata(msg_row['imsi'], nas_message)
                self._send_to_mme(mme, sgsap_msg)

                logger.info(f"SMS sent GUID={guid}, TI={ti}, IMSI={msg_row['imsi']}, MME={mme.key}")

        except Exception as e:
            logger.error(f"Failed to send GUID {guid}: {e}")
            self.db.update_status(guid, 'failed', str(e))

    # ------------------------------------------------------------------
    # Incoming message handling
    # ------------------------------------------------------------------

    def _handle_incoming_message(self, msg: SGsAPMessage, mme: MMEConnection):
        """Dispatch an incoming SGsAP message from a specific MME."""

        if msg.msg_type == SGsAPMessageType.RESET_INDICATION:
            logger.info(f"[{mme.key}] Received Reset Indication")
            ack = create_reset_ack(self.vlr_name)
            self._send_to_mme(mme, ack)

        elif msg.msg_type == SGsAPMessageType.RESET_ACK:
            logger.info(f"[{mme.key}] Received Reset Ack")

        elif msg.msg_type == SGsAPMessageType.LOCATION_UPDATE_REQUEST:
            if SGsAPIEI.IMSI in msg.ies:
                imsi = decode_imsi(msg.ies[SGsAPIEI.IMSI])
                logger.info(f"[{mme.key}] Location Update Request from IMSI: {imsi}")

                if SGsAPIEI.LOCATION_AREA_IDENTIFIER in msg.ies:
                    lai = decode_location_area_id(msg.ies[SGsAPIEI.LOCATION_AREA_IDENTIFIER])
                    logger.info(f"  LAI: MCC={lai.get('mcc')}, MNC={lai.get('mnc')}, LAC={lai.get('lac')}")

                # Record which MME this subscriber is on
                with self.connections_lock:
                    self.imsi_to_mme[imsi] = mme.host
                self.db.set_imsi_mme_mapping(imsi, mme.host)

                accept_msg = create_location_update_accept(
                    imsi, self.lai_mcc, self.lai_mnc, self.lai_lac
                )
                self._send_to_mme(mme, accept_msg)
                logger.info(f"[{mme.key}] Sent Location Update Accept for IMSI: {imsi}")

        elif msg.msg_type == SGsAPMessageType.IMSI_DETACH_INDICATION:
            if SGsAPIEI.IMSI in msg.ies:
                imsi = decode_imsi(msg.ies[SGsAPIEI.IMSI])
                logger.info(f"[{mme.key}] IMSI Detach Indication from IMSI: {imsi}")

                with self.connections_lock:
                    self.imsi_to_mme.pop(imsi, None)
                self.db.remove_imsi_mme_mapping(imsi)

                ack_msg = create_imsi_detach_ack(imsi)
                self._send_to_mme(mme, ack_msg)

        elif msg.msg_type == SGsAPMessageType.EPS_DETACH_INDICATION:
            if SGsAPIEI.IMSI in msg.ies:
                imsi = decode_imsi(msg.ies[SGsAPIEI.IMSI])
                logger.info(f"[{mme.key}] EPS Detach Indication from IMSI: {imsi}")

                with self.connections_lock:
                    self.imsi_to_mme.pop(imsi, None)
                self.db.remove_imsi_mme_mapping(imsi)

                ack_msg = create_eps_detach_ack(imsi)
                self._send_to_mme(mme, ack_msg)

        elif msg.msg_type == SGsAPMessageType.PAGING_REQUEST:
            logger.info(f"[{mme.key}] Received Paging Request")

        elif msg.msg_type == SGsAPMessageType.UPLINK_UNITDATA:
            if SGsAPIEI.IMSI in msg.ies and SGsAPIEI.NAS_MESSAGE_CONTAINER in msg.ies:
                imsi = decode_imsi(msg.ies[SGsAPIEI.IMSI])
                nas_msg = msg.ies[SGsAPIEI.NAS_MESSAGE_CONTAINER]
                logger.info(f"[{mme.key}] Uplink NAS from {imsi}: {nas_msg.hex()}")

                if len(nas_msg) >= 2:
                    pd_ti = nas_msg[0]
                    cp_msg_type = nas_msg[1]
                    ti_value = (pd_ti >> 4) & 0x07
                    ti_flag = (pd_ti >> 3) & 0x01

                    if cp_msg_type == 0x04:  # CP-ACK
                        logger.info(f"Received CP-ACK from IMSI: {imsi}, TI={ti_value}, TI-flag={ti_flag}")

                        with mme.pending_lock:
                            guid = mme.pending_sms.get(ti_value)
                            if guid:
                                msg_row = self.db.get_by_guid(guid)
                                if msg_row:
                                    duration = time.time() - msg_row['last_attempt_at']
                                    logger.info(f"✓ CP-ACK GUID={guid}, TI={ti_value}, {duration:.2f}s, retry={msg_row['retry_count']}")
                                    self.db.update_status(guid, 'acknowledged')
                                    del mme.pending_sms[ti_value]
                                else:
                                    logger.warning(f"CP-ACK for GUID {guid} not found in DB")
                            else:
                                logger.debug(f"CP-ACK for unknown TI={ti_value}")

                    elif cp_msg_type == 0x01:  # CP-DATA
                        if len(nas_msg) >= 4:
                            cp_ud_len = nas_msg[2]
                            rp_msg = nas_msg[3:3+cp_ud_len]

                            if len(rp_msg) >= 1:
                                rp_msg_type = rp_msg[0]
                                logger.info(f"  RP type: 0x{rp_msg_type:02x}, {len(rp_msg)} bytes: {rp_msg.hex()}")

                                if rp_msg_type == 0x02:  # RP-ACK (MS→Network)
                                    logger.info(f"Received RP-ACK from IMSI: {imsi} - SMS delivered successfully")

                                    msg_row = self.db.get_by_ti(ti_value)
                                    if not msg_row:
                                        msg_row = self.db.get_by_imsi_acknowledged(imsi)

                                    if msg_row:
                                        logger.info(f"✓ RP-ACK marks GUID={msg_row['guid']} as DELIVERED")
                                        self.db.update_status(msg_row['guid'], 'delivered')
                                    else:
                                        logger.warning(f"RP-ACK from {imsi} but no acknowledged message found")

                                    cp_ack = create_cp_ack(ti_value)
                                    sgsap_msg = create_downlink_unitdata(imsi, cp_ack)
                                    self._send_to_mme(mme, sgsap_msg)
                                    logger.info(f"Sent CP-ACK to IMSI: {imsi}")

                                elif rp_msg_type == 0x04:  # RP-ERROR (MS→Network)
                                    if len(rp_msg) >= 3:
                                        rp_cause = rp_msg[2]
                                        logger.warning(f"RP-ERROR from IMSI: {imsi}, cause: {rp_cause}")

                                    cp_ack = create_cp_ack(ti_value)
                                    sgsap_msg = create_downlink_unitdata(imsi, cp_ack)
                                    self._send_to_mme(mme, sgsap_msg)

                                elif rp_msg_type == 0x00:  # RP-DATA (MS→Network, MO-SMS)
                                    logger.info(f"Received RP-DATA (MO) from IMSI: {imsi}")

                                    try:
                                        offset = 2
                                        rp_ref = rp_msg[1] if len(rp_msg) > 1 else 0

                                        if offset < len(rp_msg):
                                            oa_len = rp_msg[offset]
                                            logger.info(f"  RP-OA length: {oa_len}")
                                            offset += 1 + oa_len

                                        if offset < len(rp_msg):
                                            da_len = rp_msg[offset]
                                            logger.info(f"  RP-DA length: {da_len}")
                                            offset += 1 + da_len

                                        if offset < len(rp_msg):
                                            tpdu_len = rp_msg[offset]
                                            offset += 1
                                            tpdu = rp_msg[offset:offset+tpdu_len]
                                            logger.info(f"  TPDU length: {tpdu_len}, hex: {tpdu.hex()}")

                                            if len(tpdu) > 0:
                                                tp_mti = tpdu[0] & 0x03
                                                if tp_mti == 0x02:
                                                    self._handle_status_report(imsi, tpdu)
                                                elif tp_mti == 0x01:
                                                    logger.info(f"  → MO-SMS (SMS-SUBMIT) from {imsi}")
                                                else:
                                                    logger.info(f"  → Unknown TPDU MTI=0x{tp_mti:02x}")

                                    except Exception as e:
                                        logger.error(f"Error parsing RP-DATA: {e}")
                                        import traceback
                                        traceback.print_exc()

                                    rp_ref = rp_msg[1] if len(rp_msg) > 1 else 0
                                    rp_ack = bytes([0x03, rp_ref])
                                    cp_data_response = create_cp_data(rp_ack, ti_value)
                                    sgsap_msg = create_downlink_unitdata(imsi, cp_data_response)
                                    self._send_to_mme(mme, sgsap_msg)
                                    logger.info(f"Sent RP-ACK to IMSI: {imsi}")

                                else:
                                    logger.info(f"Received RP type 0x{rp_msg_type:02x} from IMSI: {imsi}")
                    else:
                        logger.info(f"Received CP type 0x{cp_msg_type:02x} from IMSI: {imsi}")

        else:
            logger.info(f"[{mme.key}] Received {msg.msg_type.name} (unhandled)")

    def _handle_status_report(self, imsi: str, tpdu: bytes):
        """Handle SMS-STATUS-REPORT from UE."""
        try:
            if len(tpdu) < 3:
                logger.warning(f"STATUS-REPORT too short: {len(tpdu)} bytes")
                return

            msg_ref = tpdu[1]
            offset = 2
            ra_len = tpdu[offset]
            offset += 1
            ra_type = tpdu[offset] if offset < len(tpdu) else 0
            offset += 1
            ra_octets = (ra_len + 1) // 2
            offset += ra_octets
            offset += 14  # skip SCTS + DT
            status = tpdu[offset] if offset < len(tpdu) else 0xFF

            if status <= 0x1F:
                status_desc = "delivered successfully"
            elif status <= 0x3F:
                status_desc = "temporary error"
            elif status <= 0x5F:
                status_desc = "permanent error"
            else:
                status_desc = "unknown"

            logger.info(f"DELIVERY REPORT from {imsi}: MsgRef={msg_ref}, "
                        f"Status=0x{status:02x} ({status_desc})")

        except Exception as e:
            logger.error(f"Error parsing STATUS-REPORT: {e}")

    # ------------------------------------------------------------------
    # Retry / cleanup
    # ------------------------------------------------------------------

    def _check_pending_timeouts(self):
        """Check for pending messages that need retry."""
        current_time = time.time()
        pending_messages = self.db.get_pending_for_retry()

        for msg_row in pending_messages:
            guid = msg_row['guid']
            last_attempt = msg_row.get('last_attempt_at')
            if not last_attempt:
                continue

            if current_time - last_attempt < self.retry_timeout:
                continue

            def _free_ti(imsi, ti):
                with self.connections_lock:
                    mme_key = self.imsi_to_mme.get(imsi)
                    mme = self.mme_connections.get(mme_key) if mme_key else None
                if mme and ti is not None:
                    with mme.pending_lock:
                        mme.pending_sms.pop(ti, None)

            if msg_row['do_not_deliver_after'] and current_time > msg_row['do_not_deliver_after']:
                logger.error(f"✗ GUID {guid} failed: do_not_deliver_after exceeded")
                self.db.update_status(guid, 'failed', 'Expired: do_not_deliver_after')
                _free_ti(msg_row['imsi'], msg_row.get('ti'))
                continue

            if msg_row['retry_count'] >= self.max_retries:
                logger.error(f"✗ GUID {guid} failed after {self.max_retries} retries")
                self.db.update_status(guid, 'failed', f'Max retries ({self.max_retries}) exceeded')
                _free_ti(msg_row['imsi'], msg_row.get('ti'))
                continue

            logger.warning(f"🔄 Retrying GUID {guid} (attempt {msg_row['retry_count'] + 1}/{self.max_retries})")
            self.db.update_status(guid, 'retrying')
            _free_ti(msg_row['imsi'], msg_row.get('ti'))
            self.db.reset_ti(guid)

    # ------------------------------------------------------------------
    # Main service loop
    # ------------------------------------------------------------------

    def run(self):
        """Main service loop: drain the SMS queue and handle retries."""
        self.running = True

        cleanup_thread = threading.Thread(target=self._cleanup_loop, daemon=True)
        cleanup_thread.start()

        logger.info("SMSC service running")

        try:
            while self.running:
                queued = self.db.get_queued(limit=10)
                for msg_row in queued:
                    self._process_sms(msg_row['guid'])

                self._check_pending_timeouts()
                time.sleep(0.1)

        except KeyboardInterrupt:
            logger.info("Shutting down...")
        finally:
            self.running = False
            self.disconnect()

    def _cleanup_loop(self):
        """Background thread: delete messages past their store_until timestamp."""
        while self.running:
            try:
                deleted = self.db.cleanup_expired()
                if deleted > 0:
                    logger.info(f"Cleaned up {deleted} expired messages")
            except Exception as e:
                logger.error(f"Cleanup error: {e}")
            time.sleep(3600)


def main():
    """Main entry point"""
    import argparse

    parser = argparse.ArgumentParser(description='Mini SMSC/VLR Service')
    parser.add_argument('--listen-address', default='0.0.0.0')
    parser.add_argument('--listen-port', type=int, default=29118)
    parser.add_argument('--vlr-name', default='vlr.open5gs.org')
    parser.add_argument('--lai-mcc', default='001')
    parser.add_argument('--lai-mnc', default='01')
    parser.add_argument('--lai-lac', type=int, default=1)
    parser.add_argument('--smsc-address', default='+0000')

    args = parser.parse_args()

    smsc = SMSCService(
        args.listen_address,
        args.listen_port,
        args.vlr_name,
        args.lai_mcc,
        args.lai_mnc,
        args.lai_lac,
        args.smsc_address
    )

    try:
        smsc.listen()
        logger.info("\n=== SMSC ready — waiting for MME connections ===")
        smsc.run()
    except Exception as e:
        logger.error(f"Service error: {e}")
        return 1

    return 0


if __name__ == '__main__':
    exit(main())
