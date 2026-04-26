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
    decode_imsi
)
from sms_encoder import create_sms_deliver_pdu, create_rp_data_dl, create_cp_data


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


class SMSCService:
    """SMSC Service - handles SMS delivery via SGs interface"""

    def __init__(self, mme_address: str, mme_port: int, vlr_name: str):
        """
        Initialize SMSC service

        Args:
            mme_address: MME IP address
            mme_port: MME SGs interface port (typically 29118)
            vlr_name: VLR/MSC name (FQDN)
        """
        self.mme_address = mme_address
        self.mme_port = mme_port
        self.vlr_name = vlr_name

        self.sock: Optional[socket.socket] = None
        self.connected = False
        self.running = False
        self.rp_reference = 0

        # Message queue
        self.message_queue: list = []
        self.queue_lock = threading.Lock()

        logger.info(f"SMSC initialized - VLR: {vlr_name}")

    def connect(self):
        """Connect to MME via SCTP"""
        try:
            # Create SCTP socket
            # Note: Python's socket module needs sctp support
            # You may need to install pysctp: pip install pysctp
            try:
                import sctp
                self.sock = sctp.sctpsocket_tcp(socket.AF_INET)
            except ImportError:
                logger.warning("SCTP module not available, using TCP fallback")
                # Fallback to TCP for testing
                self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)

            logger.info(f"Connecting to MME at {self.mme_address}:{self.mme_port}")
            self.sock.connect((self.mme_address, self.mme_port))
            self.connected = True
            logger.info("Connected to MME")

            # Send Reset Indication
            self._send_reset_indication()

        except Exception as e:
            logger.error(f"Failed to connect to MME: {e}")
            raise

    def disconnect(self):
        """Disconnect from MME"""
        if self.sock:
            self.sock.close()
            self.sock = None
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
        logger.debug(f"Sending SGsAP message type {msg.msg_type.name}: {data.hex()}")
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
            logger.debug(f"Received SGsAP message type {msg.msg_type.name}")
            return msg

        except socket.timeout:
            return None
        except Exception as e:
            logger.error(f"Error receiving message: {e}")
            return None

    def send_sms(self, imsi: str, msisdn: str, sender: str, text: str):
        """
        Queue an SMS for delivery

        Args:
            imsi: Destination IMSI (e.g., "001010000000001")
            msisdn: Destination phone number (e.g., "+1234567890")
            sender: Sender phone number
            text: SMS text content
        """
        sms = SMSMessage(
            destination_imsi=imsi,
            destination_msisdn=msisdn,
            sender=sender,
            text=text,
            timestamp=datetime.now()
        )

        with self.queue_lock:
            self.message_queue.append(sms)

        logger.info(f"SMS queued for {imsi}: {text[:50]}")

    def _process_sms(self, sms: SMSMessage):
        """Process and send an SMS message"""
        try:
            # Create SMS TPDU (TP-DELIVER)
            tpdu = create_sms_deliver_pdu(sms.sender, sms.text)

            # Wrap in RP-DATA
            self.rp_reference = (self.rp_reference + 1) % 256
            rp_data = create_rp_data_dl(
                sms.destination_msisdn,
                tpdu,
                self.rp_reference
            )

            # Wrap in CP-DATA (NAS message)
            nas_message = create_cp_data(rp_data)

            # Create SGsAP Downlink Unitdata message
            sgsap_msg = create_downlink_unitdata(sms.destination_imsi, nas_message)

            # Send to MME
            self._send_message(sgsap_msg)

            logger.info(f"SMS sent to {sms.destination_imsi} from {sms.sender}")

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

        elif msg.msg_type == SGsAPMessageType.PAGING_REQUEST:
            logger.info("Received Paging Request from MME")
            # Could implement paging handling here

        elif msg.msg_type == SGsAPMessageType.UPLINK_UNITDATA:
            logger.info("Received Uplink Unitdata (incoming SMS)")
            # SMS reception - not implemented yet per requirements
            if SGsAPIEI.IMSI in msg.ies:
                imsi = decode_imsi(msg.ies[SGsAPIEI.IMSI])
                logger.info(f"Uplink SMS from IMSI: {imsi}")

        else:
            logger.debug(f"Received {msg.msg_type.name} message")

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

    parser = argparse.ArgumentParser(description='Mini SMSC Service')
    parser.add_argument('--mme-address', default='127.0.0.1',
                       help='MME IP address (default: 127.0.0.1)')
    parser.add_argument('--mme-port', type=int, default=29118,
                       help='MME SGs port (default: 29118)')
    parser.add_argument('--vlr-name', default='vlr.open5gs.org',
                       help='VLR/MSC FQDN (default: vlr.open5gs.org)')

    args = parser.parse_args()

    # Create and start SMSC service
    smsc = SMSCService(args.mme_address, args.mme_port, args.vlr_name)

    try:
        smsc.connect()

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
