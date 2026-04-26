#!/usr/bin/env python3
"""
Mini SMSC - Basic SMS Center for Open5GS
Communicates with MME over SGs interface using SCTP
"""

import socket
import logging
import threading
import time
from typing import Optional, Dict
from dataclasses import dataclass
from datetime import datetime

from sgsap_protocol import (
    SGsAPMessage, SGsAPMessageType, SGsAPIEI,
    create_downlink_unitdata, create_reset_indication, create_reset_ack,
    create_location_update_accept, create_location_update_reject,
    create_imsi_detach_ack, create_eps_detach_ack,
    decode_imsi, decode_location_area_id
)
from sms_encoder import create_sms_deliver_pdu, create_rp_data_dl, create_cp_data, create_cp_ack


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


class SMSCService:
    """SMSC Service - handles SMS delivery via SGs interface"""

    def __init__(self, listen_address: str, listen_port: int, vlr_name: str,
                 lai_mcc: str = "001", lai_mnc: str = "01", lai_lac: int = 1,
                 smsc_address: str = "+0000"):
        """
        Initialize SMSC service

        Args:
            listen_address: IP address to bind to (e.g., '0.0.0.0' for all interfaces)
            listen_port: SGsAP server port (typically 29118)
            vlr_name: VLR/MSC name (FQDN)
            lai_mcc: Location Area MCC (default: "001")
            lai_mnc: Location Area MNC (default: "01")
            lai_lac: Location Area Code (default: 1)
            smsc_address: SMSC service center number (default: "+0000")
        """
        self.listen_address = listen_address
        self.listen_port = listen_port
        self.vlr_name = vlr_name
        self.lai_mcc = lai_mcc
        self.lai_mnc = lai_mnc
        self.lai_lac = lai_lac
        self.smsc_address = smsc_address

        self.server_sock: Optional[socket.socket] = None
        self.sock: Optional[socket.socket] = None
        self.connected = False
        self.running = False
        self.rp_reference = 0
        self.ti_counter = 0

        # Message queue
        self.message_queue: list = []
        self.queue_lock = threading.Lock()

        # Pending SMS tracking for retries
        self.pending_sms: Dict[int, PendingSMS] = {}  # Key: TI value
        self.pending_lock = threading.Lock()
        self.retry_timeout = 3.0  # seconds

        logger.info(f"SMSC/VLR initialized - VLR: {vlr_name}, LAI: {lai_mcc}-{lai_mnc}-{lai_lac}")

    def listen(self):
        """Listen for MME connection via SCTP (SGsAP server)"""
        try:
            # Create SCTP socket
            # Note: Python's socket module needs sctp support
            # You may need to install pysctp: pip install pysctp
            try:
                import sctp
                self.server_sock = sctp.sctpsocket_tcp(socket.AF_INET)
            except ImportError:
                logger.warning("SCTP module not available, using TCP fallback")
                # Fallback to TCP for testing
                self.server_sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)

            # Allow address reuse
            self.server_sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)

            # Bind and listen
            logger.info(f"Binding to {self.listen_address}:{self.listen_port}")
            self.server_sock.bind((self.listen_address, self.listen_port))
            self.server_sock.listen(1)
            logger.info(f"Listening for MME connection on port {self.listen_port}")

            # Accept connection from MME
            logger.info("Waiting for MME to connect...")
            self.sock, mme_addr = self.server_sock.accept()
            self.connected = True
            logger.info(f"MME connected from {mme_addr}")

            # Send Reset Indication
            self._send_reset_indication()

        except Exception as e:
            logger.error(f"Failed to start SGsAP server: {e}")
            raise

    def disconnect(self):
        """Disconnect from MME"""
        if self.sock:
            self.sock.close()
            self.sock = None
        if self.server_sock:
            self.server_sock.close()
            self.server_sock = None
        self.connected = False
        logger.info("Disconnected from MME")

    def _send_reset_indication(self):
        """Send Reset Indication to MME"""
        msg = create_reset_indication(self.vlr_name)
        self._send_message(msg)
        logger.info("Sent Reset Indication")

    def _send_message(self, msg: SGsAPMessage):
        """Send SGsAP message to MME"""
        if not self.connected or not self.sock:
            raise RuntimeError("Not connected to MME")

        data = msg.encode()
        logger.info(f"Sending SGsAP message type {msg.msg_type.name} ({len(data)} bytes)")
        self.sock.sendall(data)

    def _receive_message(self, timeout: float = 1.0) -> Optional[SGsAPMessage]:
        """Receive SGsAP message from MME"""
        if not self.connected or not self.sock:
            return None

        try:
            self.sock.settimeout(timeout)
            data = self.sock.recv(4096)

            if not data:
                logger.warning("Connection closed by MME")
                self.connected = False
                return None

            msg = SGsAPMessage.decode(data)
            logger.info(f"Received SGsAP message type {msg.msg_type.name} ({len(data)} bytes)")
            if msg.msg_type == SGsAPMessageType.LOCATION_UPDATE_REQUEST and SGsAPIEI.IMSI in msg.ies:
                logger.info(f"  IMSI raw bytes: {msg.ies[SGsAPIEI.IMSI].hex()}")
            return msg

        except socket.timeout:
            return None
        except Exception as e:
            logger.error(f"Error receiving message: {e}")
            return None

    def send_sms(self, imsi: str, msisdn: str, sender: str, text: str,
                 request_delivery_report: bool = False):
        """
        Queue an SMS for delivery

        Args:
            imsi: Destination IMSI (e.g., "001010000000001")
            msisdn: Destination phone number (e.g., "+1234567890")
            sender: Sender phone number
            text: SMS text content
            request_delivery_report: Request delivery report from UE
        """
        sms = SMSMessage(
            destination_imsi=imsi,
            destination_msisdn=msisdn,
            sender=sender,
            text=text,
            timestamp=datetime.now(),
            request_delivery_report=request_delivery_report
        )

        with self.queue_lock:
            self.message_queue.append(sms)

        logger.info(f"SMS queued for {imsi}: {text[:50]} (delivery_report={request_delivery_report})")

    def _get_next_ti(self) -> int:
        """Get next Transaction Identifier (0-6, wrapping)"""
        ti = self.ti_counter
        self.ti_counter = (self.ti_counter + 1) % 7
        return ti

    def _process_sms(self, sms: SMSMessage):
        """Process and send an SMS message"""
        try:
            # Allocate TI for this transaction
            ti = self._get_next_ti()

            # Create SMS TPDU (TP-DELIVER)
            tpdu = create_sms_deliver_pdu(sms.sender, sms.text,
                                         request_status_report=sms.request_delivery_report)

            # Wrap in RP-DATA
            self.rp_reference = (self.rp_reference + 1) % 256
            rp_data = create_rp_data_dl(
                sms.destination_msisdn,
                tpdu,
                self.rp_reference,
                self.smsc_address,
                include_destination=False  # Omit RP-DA for MT-SMS
            )
            logger.info(f"  TPDU ({len(tpdu)} bytes): {tpdu.hex()}")
            logger.info(f"  RP-DATA ({len(rp_data)} bytes): {rp_data.hex()}")

            # Wrap in CP-DATA (NAS message) with allocated TI
            nas_message = create_cp_data(rp_data, ti)
            logger.info(f"  CP-DATA/NAS ({len(nas_message)} bytes): {nas_message.hex()}")

            # Track as pending
            with self.pending_lock:
                self.pending_sms[ti] = PendingSMS(
                    imsi=sms.destination_imsi,
                    nas_message=nas_message,
                    ti=ti,
                    sent_time=time.time(),
                    retry_count=0
                )

            # Create SGsAP Downlink Unitdata message
            sgsap_msg = create_downlink_unitdata(sms.destination_imsi, nas_message)

            # Send to MME
            self._send_message(sgsap_msg)

            logger.info(f"SMS sent to {sms.destination_imsi} from {sms.sender} (TI={ti})")

        except Exception as e:
            logger.error(f"Failed to send SMS: {e}")

    def _handle_incoming_message(self, msg: SGsAPMessage):
        """Handle incoming SGsAP message from MME"""

        if msg.msg_type == SGsAPMessageType.RESET_INDICATION:
            logger.info("Received Reset Indication from MME")
            # Send Reset Ack
            ack = create_reset_ack(self.vlr_name)
            self._send_message(ack)

        elif msg.msg_type == SGsAPMessageType.RESET_ACK:
            logger.info("Received Reset Ack from MME")

        elif msg.msg_type == SGsAPMessageType.LOCATION_UPDATE_REQUEST:
            # VLR Location Update - critical for UE attach
            if SGsAPIEI.IMSI in msg.ies:
                imsi = decode_imsi(msg.ies[SGsAPIEI.IMSI])
                logger.info(f"Received Location Update Request from IMSI: {imsi}")

                # Extract LAI if present
                if SGsAPIEI.LOCATION_AREA_IDENTIFIER in msg.ies:
                    lai = decode_location_area_id(msg.ies[SGsAPIEI.LOCATION_AREA_IDENTIFIER])
                    logger.info(f"  LAI: MCC={lai.get('mcc')}, MNC={lai.get('mnc')}, LAC={lai.get('lac')}")

                # Send Location Update Accept
                accept_msg = create_location_update_accept(
                    imsi,
                    self.lai_mcc,
                    self.lai_mnc,
                    self.lai_lac
                )
                self._send_message(accept_msg)
                logger.info(f"Sent Location Update Accept for IMSI: {imsi}")

        elif msg.msg_type == SGsAPMessageType.IMSI_DETACH_INDICATION:
            # UE is detaching
            if SGsAPIEI.IMSI in msg.ies:
                imsi = decode_imsi(msg.ies[SGsAPIEI.IMSI])
                logger.info(f"Received IMSI Detach Indication from IMSI: {imsi}")

                # Send IMSI Detach Ack
                ack_msg = create_imsi_detach_ack(imsi)
                self._send_message(ack_msg)
                logger.info(f"Sent IMSI Detach Ack for IMSI: {imsi}")

        elif msg.msg_type == SGsAPMessageType.EPS_DETACH_INDICATION:
            # UE is detaching from EPS
            if SGsAPIEI.IMSI in msg.ies:
                imsi = decode_imsi(msg.ies[SGsAPIEI.IMSI])
                logger.info(f"Received EPS Detach Indication from IMSI: {imsi}")

                # Send EPS Detach Ack
                ack_msg = create_eps_detach_ack(imsi)
                self._send_message(ack_msg)
                logger.info(f"Sent EPS Detach Ack for IMSI: {imsi}")

        elif msg.msg_type == SGsAPMessageType.PAGING_REQUEST:
            logger.info("Received Paging Request from MME")
            # Could implement paging handling here

        elif msg.msg_type == SGsAPMessageType.UPLINK_UNITDATA:
            if SGsAPIEI.IMSI in msg.ies and SGsAPIEI.NAS_MESSAGE_CONTAINER in msg.ies:
                imsi = decode_imsi(msg.ies[SGsAPIEI.IMSI])
                nas_msg = msg.ies[SGsAPIEI.NAS_MESSAGE_CONTAINER]
                logger.info(f"📨 Uplink NAS message from {imsi}: {nas_msg.hex()}")

                # Parse CP-layer message
                if len(nas_msg) >= 2:
                    pd_ti = nas_msg[0]
                    cp_msg_type = nas_msg[1]

                    # Extract TI value (bits 4-6) and TI flag (bit 3)
                    ti_value = (pd_ti >> 4) & 0x07
                    ti_flag = (pd_ti >> 3) & 0x01

                    if cp_msg_type == 0x04:  # CP-ACK
                        logger.info(f"Received CP-ACK from IMSI: {imsi}, TI={ti_value}, TI-flag={ti_flag}")

                        # Mark message as acknowledged and remove from pending
                        with self.pending_lock:
                            if ti_value in self.pending_sms:
                                pending = self.pending_sms[ti_value]
                                duration = time.time() - pending.sent_time
                                logger.info(f"✓ SMS acknowledged (TI={ti_value}, {duration:.2f}s, {pending.retry_count} retries)")
                                del self.pending_sms[ti_value]
                            else:
                                logger.debug(f"CP-ACK for unknown TI={ti_value}")

                    elif cp_msg_type == 0x01:  # CP-DATA
                        # CP-DATA contains RP message
                        if len(nas_msg) >= 4:
                            cp_ud_len = nas_msg[2]
                            rp_msg = nas_msg[3:3+cp_ud_len]

                            if len(rp_msg) >= 1:
                                rp_msg_type = rp_msg[0]
                                logger.info(f"  RP message type: 0x{rp_msg_type:02x}, length: {len(rp_msg)} bytes")
                                logger.info(f"  RP message hex: {rp_msg.hex()}")

                                if rp_msg_type == 0x02:  # RP-ACK (MS to Network)
                                    logger.info(f"Received RP-ACK from IMSI: {imsi} - SMS delivered successfully")

                                    # Send CP-ACK to complete the transaction
                                    cp_ack = create_cp_ack(ti_value)
                                    sgsap_msg = create_downlink_unitdata(imsi, cp_ack)
                                    self._send_message(sgsap_msg)
                                    logger.info(f"Sent CP-ACK to IMSI: {imsi}")

                                elif rp_msg_type == 0x04:  # RP-ERROR (MS to Network)
                                    if len(rp_msg) >= 3:
                                        rp_cause = rp_msg[2] if len(rp_msg) > 2 else 0
                                        logger.warning(f"Received RP-ERROR from IMSI: {imsi}, cause: {rp_cause}")

                                        # Still send CP-ACK to complete the transaction
                                        cp_ack = create_cp_ack(ti_value)
                                        sgsap_msg = create_downlink_unitdata(imsi, cp_ack)
                                        self._send_message(sgsap_msg)
                                        logger.info(f"Sent CP-ACK to IMSI: {imsi}")

                                elif rp_msg_type == 0x00:  # RP-DATA (MS to Network) - contains STATUS-REPORT or MO-SMS
                                    logger.info(f"Received RP-DATA (MO) from IMSI: {imsi}")

                                    # Extract TPDU from RP-DATA
                                    # RP-DATA (MS→Network) format: [MTI=0x00][Reference][OA-Len][OA...][DA-Len][DA...][UDL][UD...]
                                    try:
                                        offset = 2  # Skip MTI and Reference
                                        rp_ref = rp_msg[1] if len(rp_msg) > 1 else 0

                                        # Skip RP-Originator Address
                                        if offset < len(rp_msg):
                                            oa_len = rp_msg[offset]
                                            logger.info(f"  RP-OA length: {oa_len}")
                                            offset += 1 + oa_len

                                        # Skip RP-Destination Address
                                        if offset < len(rp_msg):
                                            da_len = rp_msg[offset]
                                            logger.info(f"  RP-DA length: {da_len}")
                                            offset += 1 + da_len

                                        # Extract RP-User-Data (TPDU)
                                        if offset < len(rp_msg):
                                            tpdu_len = rp_msg[offset]
                                            offset += 1
                                            tpdu = rp_msg[offset:offset+tpdu_len]
                                            logger.info(f"  TPDU length: {tpdu_len}, hex: {tpdu.hex()}")

                                            # Check message type
                                            if len(tpdu) > 0:
                                                tp_mti = tpdu[0] & 0x03
                                                logger.info(f"  TP-MTI: 0x{tp_mti:02x}")

                                                if tp_mti == 0x02:  # SMS-STATUS-REPORT
                                                    logger.info(f"  → This is a STATUS-REPORT")
                                                    self._handle_status_report(imsi, tpdu)
                                                elif tp_mti == 0x01:  # SMS-SUBMIT (MO-SMS)
                                                    logger.info(f"  → This is MO-SMS (SMS-SUBMIT)")
                                                else:
                                                    logger.info(f"  → Unknown TPDU type MTI=0x{tp_mti:02x}")

                                    except Exception as e:
                                        logger.error(f"Error parsing RP-DATA: {e}")
                                        import traceback
                                        traceback.print_exc()

                                    # Send RP-ACK back
                                    # Create RP-ACK: [MTI=0x03][Reference]
                                    rp_ref = rp_msg[1] if len(rp_msg) > 1 else 0
                                    rp_ack = bytes([0x03, rp_ref])

                                    # Wrap in CP-DATA
                                    cp_data_response = create_cp_data(rp_ack, ti_value)
                                    sgsap_msg = create_downlink_unitdata(imsi, cp_data_response)
                                    self._send_message(sgsap_msg)
                                    logger.info(f"Sent RP-ACK to IMSI: {imsi}")

                                else:
                                    logger.info(f"Received RP message type 0x{rp_msg_type:02x} from IMSI: {imsi}")
                    else:
                        logger.info(f"Received CP message type 0x{cp_msg_type:02x} from IMSI: {imsi}")

        else:
            logger.info(f"Received {msg.msg_type.name} message (not handled)")

    def _handle_status_report(self, imsi: str, tpdu: bytes):
        """
        Handle SMS-STATUS-REPORT from UE

        Args:
            imsi: IMSI of the UE
            tpdu: SMS-STATUS-REPORT TPDU
        """
        try:
            # SMS-STATUS-REPORT format (3GPP TS 23.040):
            # Byte 0: TP-MTI, TP-MMS, TP-SRQ, TP-UDHI
            # Byte 1: TP-MR (Message Reference)
            # Bytes 2+: TP-RA (Recipient Address)
            # Next: TP-SCTS (Service Centre Time Stamp) - 7 bytes
            # Next: TP-DT (Discharge Time) - 7 bytes
            # Next: TP-ST (Status) - 1 byte

            if len(tpdu) < 3:
                logger.warning(f"STATUS-REPORT too short: {len(tpdu)} bytes")
                return

            first_octet = tpdu[0]
            msg_ref = tpdu[1]

            # Parse Recipient Address
            offset = 2
            ra_len = tpdu[offset]
            offset += 1
            ra_type = tpdu[offset] if offset < len(tpdu) else 0
            offset += 1

            # Calculate address octets
            ra_octets = (ra_len + 1) // 2
            ra_data = tpdu[offset:offset+ra_octets] if offset+ra_octets <= len(tpdu) else bytes()
            offset += ra_octets

            # Skip SCTS (7 bytes) and DT (7 bytes)
            offset += 14

            # Extract Status
            status = tpdu[offset] if offset < len(tpdu) else 0xFF

            # Status interpretation (3GPP TS 23.040 section 9.2.3.15):
            # 0x00-0x1F: Short message received by SME
            # 0x20-0x3F: Temporary error
            # 0x40-0x5F: Permanent error
            # 0x60-0x7F: Temporary error (reserved)

            status_desc = "unknown"
            if status <= 0x1F:
                status_desc = "delivered successfully"
            elif status <= 0x3F:
                status_desc = "temporary error"
            elif status <= 0x5F:
                status_desc = "permanent error"

            logger.info(f"📱 DELIVERY REPORT from IMSI {imsi}: "
                       f"MsgRef={msg_ref}, Status=0x{status:02x} ({status_desc})")

        except Exception as e:
            logger.error(f"Error parsing STATUS-REPORT: {e}")

    def _check_pending_timeouts(self):
        """Check for pending SMS that need retry"""
        current_time = time.time()

        with self.pending_lock:
            to_retry = []
            to_remove = []

            for ti, pending in self.pending_sms.items():
                elapsed = current_time - pending.sent_time

                if elapsed >= self.retry_timeout:
                    if pending.retry_count >= pending.max_retries:
                        logger.error(f"✗ SMS delivery failed after {pending.max_retries} retries (TI={ti}, IMSI={pending.imsi})")
                        to_remove.append(ti)
                    else:
                        to_retry.append((ti, pending))

            # Remove failed messages
            for ti in to_remove:
                del self.pending_sms[ti]

            # Retry messages
            for ti, pending in to_retry:
                pending.retry_count += 1
                pending.sent_time = current_time

                try:
                    sgsap_msg = create_downlink_unitdata(pending.imsi, pending.nas_message)
                    self._send_message(sgsap_msg)
                    logger.warning(f"🔄 Retrying SMS (TI={ti}, attempt {pending.retry_count}/{pending.max_retries})")
                except Exception as e:
                    logger.error(f"Failed to retry SMS (TI={ti}): {e}")
                    del self.pending_sms[ti]

    def run(self):
        """Main service loop"""
        self.running = True

        # Start receiver thread
        receiver_thread = threading.Thread(target=self._receiver_loop, daemon=True)
        receiver_thread.start()

        logger.info("SMSC service running")

        try:
            while self.running:
                # Process queued messages
                with self.queue_lock:
                    if self.message_queue:
                        sms = self.message_queue.pop(0)
                        self._process_sms(sms)

                # Check for messages needing retry
                self._check_pending_timeouts()

                time.sleep(0.1)

        except KeyboardInterrupt:
            logger.info("Shutting down...")
        finally:
            self.running = False
            self.disconnect()

    def _receiver_loop(self):
        """Background thread for receiving messages"""
        while self.running and self.connected:
            msg = self._receive_message(timeout=1.0)
            if msg:
                self._handle_incoming_message(msg)


def main():
    """Main entry point"""
    import argparse

    parser = argparse.ArgumentParser(description='Mini SMSC/VLR Service')
    parser.add_argument('--listen-address', default='0.0.0.0',
                       help='Address to bind to (default: 0.0.0.0)')
    parser.add_argument('--listen-port', type=int, default=29118,
                       help='SGsAP server port (default: 29118)')
    parser.add_argument('--vlr-name', default='vlr.open5gs.org',
                       help='VLR/MSC FQDN (default: vlr.open5gs.org)')
    parser.add_argument('--lai-mcc', default='001',
                       help='Location Area MCC (default: 001)')
    parser.add_argument('--lai-mnc', default='01',
                       help='Location Area MNC (default: 01)')
    parser.add_argument('--lai-lac', type=int, default=1,
                       help='Location Area Code (default: 1)')
    parser.add_argument('--smsc-address', default='+0000',
                       help='SMSC service center number (default: +0000)')

    args = parser.parse_args()

    # Create and start SMSC service
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

        # Example: Send a test SMS after connection
        # In production, you'd expose an API (HTTP/REST) to accept SMS requests
        logger.info("\n=== Ready to send SMS ===")
        logger.info("Call smsc.send_sms(imsi, msisdn, sender, text) to send")
        logger.info("Example: smsc.send_sms('001010000000001', '+1234567890', '+0987654321', 'Hello LTE!')")

        # Start service loop
        smsc.run()

    except Exception as e:
        logger.error(f"Service error: {e}")
        return 1

    return 0


if __name__ == '__main__':
    exit(main())
