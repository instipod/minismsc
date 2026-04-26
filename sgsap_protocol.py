"""
SGsAP Protocol Implementation
Implements SGs Application Part protocol for MME-SMSC communication
Based on 3GPP TS 29.118
"""

from enum import IntEnum
from typing import Optional, Dict, Any
import struct


class SGsAPMessageType(IntEnum):
    """SGsAP Message Types"""
    # MME to VLR/MSC
    PAGING_REQUEST = 0x01
    UE_UNREACHABLE = 0x1F

    # VLR/MSC to MME
    PAGING_REJECT = 0x02
    SERVICE_REQUEST = 0x06
    DOWNLINK_UNITDATA = 0x07
    UPLINK_UNITDATA = 0x08
    LOCATION_UPDATE_REQUEST = 0x09
    LOCATION_UPDATE_ACCEPT = 0x0A
    LOCATION_UPDATE_REJECT = 0x0B
    TMSI_REALLOCATION_COMPLETE = 0x0C
    ALERT_REQUEST = 0x0D
    ALERT_ACK = 0x0E
    ALERT_REJECT = 0x0F
    UE_ACTIVITY_INDICATION = 0x10
    EPS_DETACH_INDICATION = 0x11
    EPS_DETACH_ACK = 0x12
    IMSI_DETACH_INDICATION = 0x13
    IMSI_DETACH_ACK = 0x14
    RESET_INDICATION = 0x15
    RESET_ACK = 0x16
    VLR_STATUS_REQUEST = 0x17
    VLR_STATUS_ACK = 0x18


class SGsAPIEI(IntEnum):
    """SGsAP Information Element Identifiers"""
    IMSI = 0x01
    VLR_NAME = 0x02
    TMSI = 0x03
    LOCATION_AREA_IDENTIFIER = 0x04
    CHANNEL_NEEDED = 0x05
    EMLPP_PRIORITY = 0x06
    TMSI_STATUS = 0x07
    SGS_CAUSE = 0x08
    MME_NAME = 0x09
    EPS_LOCATION_UPDATE_TYPE = 0x0A
    GLOBAL_CN_ID = 0x0B
    MOBILE_IDENTITY = 0x0E
    REJECT_CAUSE = 0x0F
    IMSI_DETACH_FROM_EPS_SERVICE_TYPE = 0x10
    IMSI_DETACH_FROM_NON_EPS_SERVICE_TYPE = 0x11
    NAS_MESSAGE_CONTAINER = 0x16
    MM_INFORMATION = 0x17
    ERRONEOUS_MESSAGE = 0x1B
    CLI = 0x1C
    LCS_CLIENT_IDENTITY = 0x1D
    LCS_INDICATOR = 0x1E
    SS_CODE = 0x1F
    SERVICE_INDICATOR = 0x20
    UE_TIME_ZONE = 0x21
    MOBILE_STATION_CLASSMARK_2 = 0x22
    TRACKING_AREA_IDENTITY = 0x23
    E_UTRAN_CELL_GLOBAL_IDENTITY = 0x24
    UE_EMM_MODE = 0x25


class SGsAPMessage:
    """Base class for SGsAP messages"""

    def __init__(self, msg_type: SGsAPMessageType):
        self.msg_type = msg_type
        self.ies: Dict[SGsAPIEI, bytes] = {}

    def add_ie(self, iei: SGsAPIEI, value: bytes):
        """Add an Information Element"""
        self.ies[iei] = value

    def encode(self) -> bytes:
        """Encode message to bytes"""
        msg = struct.pack('B', self.msg_type)

        for iei, value in self.ies.items():
            # TLV format: Type (1 byte) + Length (1 byte) + Value
            msg += struct.pack('BB', iei, len(value))
            msg += value

        return msg

    @staticmethod
    def decode(data: bytes) -> 'SGsAPMessage':
        """Decode message from bytes"""
        if len(data) < 1:
            raise ValueError("Message too short")

        msg_type = SGsAPMessageType(data[0])
        msg = SGsAPMessage(msg_type)

        offset = 1
        while offset < len(data):
            if offset + 2 > len(data):
                break

            iei = SGsAPIEI(data[offset])
            length = data[offset + 1]
            offset += 2

            if offset + length > len(data):
                raise ValueError("Invalid IE length")

            value = data[offset:offset + length]
            msg.add_ie(iei, value)
            offset += length

        return msg


def encode_imsi(imsi: str) -> bytes:
    """Encode IMSI in TBCD format"""
    # IMSI is encoded in TBCD (Telephony Binary Coded Decimal)
    # Pad with 'F' if odd length
    if len(imsi) % 2 == 1:
        imsi += 'F'

    result = bytearray()
    result.append(len(imsi) - 1 if imsi.endswith('F') else len(imsi))

    for i in range(0, len(imsi), 2):
        d1 = int(imsi[i])
        d2 = int(imsi[i + 1], 16)  # Use 16 to handle 'F'
        result.append((d2 << 4) | d1)

    return bytes(result)


def decode_imsi(data: bytes) -> str:
    """Decode IMSI from TBCD format"""
    if len(data) < 1:
        return ""

    length = data[0]
    imsi = ""

    for i in range(1, len(data)):
        d1 = data[i] & 0x0F
        d2 = (data[i] >> 4) & 0x0F

        imsi += str(d1)
        if d2 != 0x0F:
            imsi += str(d2)

    return imsi[:length]


def encode_mme_name(name: str) -> bytes:
    """Encode MME/VLR name as FQDN"""
    parts = name.split('.')
    result = bytearray()

    for part in parts:
        result.append(len(part))
        result.extend(part.encode('ascii'))

    return bytes(result)


def create_downlink_unitdata(imsi: str, nas_message: bytes) -> SGsAPMessage:
    """Create a Downlink Unitdata message for SMS delivery"""
    msg = SGsAPMessage(SGsAPMessageType.DOWNLINK_UNITDATA)
    msg.add_ie(SGsAPIEI.IMSI, encode_imsi(imsi))
    msg.add_ie(SGsAPIEI.NAS_MESSAGE_CONTAINER, nas_message)
    return msg


def create_reset_indication(vlr_name: str) -> SGsAPMessage:
    """Create a Reset Indication message"""
    msg = SGsAPMessage(SGsAPMessageType.RESET_INDICATION)
    msg.add_ie(SGsAPIEI.VLR_NAME, encode_mme_name(vlr_name))
    return msg


def create_reset_ack(vlr_name: str) -> SGsAPMessage:
    """Create a Reset Ack message"""
    msg = SGsAPMessage(SGsAPMessageType.RESET_ACK)
    msg.add_ie(SGsAPIEI.VLR_NAME, encode_mme_name(vlr_name))
    return msg
