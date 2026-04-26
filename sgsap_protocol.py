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
    """
    Encode IMSI in Mobile Identity format (3GPP TS 24.008)
    Byte 0: bits 0-2 = identity type (1), bit 3 = odd/even, bits 4-7 = first digit
    Remaining bytes: TBCD encoded digits
    """
    if len(imsi) < 1:
        return bytes()

    result = bytearray()

    # First byte: identity type (1 = IMSI), odd/even indicator, first digit
    identity_type = 0x01  # IMSI
    odd_even = 1 if len(imsi) % 2 == 1 else 0
    first_digit = int(imsi[0])

    first_byte = identity_type | (odd_even << 3) | (first_digit << 4)
    result.append(first_byte)

    # Encode remaining digits in TBCD format
    remaining = imsi[1:]
    if len(remaining) % 2 == 1:
        remaining += 'F'  # Pad with filler

    for i in range(0, len(remaining), 2):
        d1 = int(remaining[i])
        d2 = int(remaining[i + 1], 16)  # Use 16 to handle 'F'
        result.append((d2 << 4) | d1)

    return bytes(result)


def decode_imsi(data: bytes) -> str:
    """
    Decode IMSI from Mobile Identity format (3GPP TS 24.008)
    Byte 0: bits 0-2 = identity type, bit 3 = odd/even, bits 4-7 = first digit
    Remaining bytes: TBCD encoded digits
    """
    if len(data) < 1:
        return ""

    # First byte contains identity type and first digit
    first_byte = data[0]
    identity_type = first_byte & 0x07
    odd_even = (first_byte >> 3) & 0x01  # 1 = odd, 0 = even
    first_digit = (first_byte >> 4) & 0x0F

    imsi = str(first_digit)

    # Decode remaining bytes in TBCD format
    for i in range(1, len(data)):
        d1 = data[i] & 0x0F
        d2 = (data[i] >> 4) & 0x0F

        imsi += str(d1)
        if d2 != 0x0F:  # 0xF is filler for odd-length
            imsi += str(d2)

    return imsi


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


def encode_location_area_id(mcc: str, mnc: str, lac: int) -> bytes:
    """
    Encode Location Area Identifier (LAI)
    MCC: 3 digits, MNC: 2-3 digits, LAC: 2 bytes
    """
    # Pad MCC/MNC to ensure correct format
    mcc = mcc.zfill(3)
    mnc = mnc.ljust(3, 'F')  # Pad MNC with F if 2 digits

    result = bytearray()

    # Encode MCC and MNC in TBCD format
    # Byte 1: MCC digit 2, MCC digit 1
    result.append((int(mcc[1]) << 4) | int(mcc[0]))
    # Byte 2: MNC digit 3, MCC digit 3
    result.append((int(mnc[2], 16) << 4) | int(mcc[2]))
    # Byte 3: MNC digit 2, MNC digit 1
    result.append((int(mnc[1]) << 4) | int(mnc[0]))
    # Bytes 4-5: LAC (2 bytes, big endian)
    result.extend(struct.pack('>H', lac))

    return bytes(result)


def decode_location_area_id(data: bytes) -> dict:
    """Decode Location Area Identifier (LAI)"""
    if len(data) < 5:
        return {}

    mcc = f"{data[0] & 0x0F}{(data[0] >> 4) & 0x0F}{data[1] & 0x0F}"
    mnc_digit3 = (data[1] >> 4) & 0x0F
    mnc = f"{data[2] & 0x0F}{(data[2] >> 4) & 0x0F}"
    if mnc_digit3 != 0x0F:
        mnc += f"{mnc_digit3}"

    lac = struct.unpack('>H', data[3:5])[0]

    return {
        'mcc': mcc,
        'mnc': mnc,
        'lac': lac
    }


def create_location_update_accept(imsi: str, lai_mcc: str, lai_mnc: str, lai_lac: int,
                                   mobile_identity: Optional[bytes] = None) -> SGsAPMessage:
    """
    Create a Location Update Accept message

    Args:
        imsi: Subscriber IMSI
        lai_mcc: Location Area MCC (e.g., "315")
        lai_mnc: Location Area MNC (e.g., "010")
        lai_lac: Location Area Code (e.g., 1)
        mobile_identity: Optional new TMSI (if None, UE keeps IMSI)
    """
    msg = SGsAPMessage(SGsAPMessageType.LOCATION_UPDATE_ACCEPT)
    msg.add_ie(SGsAPIEI.IMSI, encode_imsi(imsi))
    msg.add_ie(SGsAPIEI.LOCATION_AREA_IDENTIFIER,
               encode_location_area_id(lai_mcc, lai_mnc, lai_lac))

    # Optionally include new TMSI
    if mobile_identity:
        msg.add_ie(SGsAPIEI.MOBILE_IDENTITY, mobile_identity)

    return msg


def create_location_update_reject(imsi: str, reject_cause: int) -> SGsAPMessage:
    """
    Create a Location Update Reject message

    Args:
        imsi: Subscriber IMSI
        reject_cause: Reject cause code (3GPP TS 24.008)
    """
    msg = SGsAPMessage(SGsAPMessageType.LOCATION_UPDATE_REJECT)
    msg.add_ie(SGsAPIEI.IMSI, encode_imsi(imsi))
    msg.add_ie(SGsAPIEI.REJECT_CAUSE, struct.pack('B', reject_cause))
    return msg


def create_imsi_detach_ack(imsi: str) -> SGsAPMessage:
    """Create IMSI Detach Ack message"""
    msg = SGsAPMessage(SGsAPMessageType.IMSI_DETACH_ACK)
    msg.add_ie(SGsAPIEI.IMSI, encode_imsi(imsi))
    return msg


def create_eps_detach_ack(imsi: str) -> SGsAPMessage:
    """Create EPS Detach Ack message"""
    msg = SGsAPMessage(SGsAPMessageType.EPS_DETACH_ACK)
    msg.add_ie(SGsAPIEI.IMSI, encode_imsi(imsi))
    return msg


def create_tmsi_reallocation_complete() -> SGsAPMessage:
    """Create TMSI Reallocation Complete message"""
    msg = SGsAPMessage(SGsAPMessageType.TMSI_REALLOCATION_COMPLETE)
    return msg
