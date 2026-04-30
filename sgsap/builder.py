import os
import struct

from .protocol import IEType, MsgType


def generate_tmsi() -> int:
    """Generate a random 32-bit TMSI; 0xFFFFFFFF is the unallocated sentinel."""
    while True:
        value = struct.unpack(">I", os.urandom(4))[0]
        if value != 0xFFFFFFFF:
            return value


def encode_tmsi_identity(tmsi: int) -> bytes:
    """
    Encode a TMSI as a Mobile Identity value (TS 24.008 §10.5.1.4).
    Byte 0: 0xF4 — upper nibble 1111, odd/even bit 0, type-of-identity 100 (TMSI).
    Bytes 1-4: TMSI in big-endian binary (not BCD).
    """
    return bytes([0xF4]) + struct.pack(">I", tmsi)


def encode_lai(mcc: str, mnc: str, lac: int) -> bytes:
    """
    Encode a Location Area Identifier per 3GPP TS 24.008 section 10.5.1.3.
    mcc: exactly 3 digit string, e.g. "001"
    mnc: 2 or 3 digit string, e.g. "01" or "001"
    lac: 16-bit Location Area Code

    Byte layout:
      0: MCC digit 2 (hi) | MCC digit 1 (lo)
      1: MNC digit 3 (hi) | MCC digit 3 (lo)   [MNC digit 3 = 0xF for 2-digit MNC]
      2: MNC digit 2 (hi) | MNC digit 1 (lo)
      3: LAC high byte
      4: LAC low byte
    """
    if len(mcc) != 3 or not mcc.isdigit():
        raise ValueError(f"MCC must be exactly 3 digits: {mcc!r}")
    if len(mnc) not in (2, 3) or not mnc.isdigit():
        raise ValueError(f"MNC must be 2 or 3 digits: {mnc!r}")
    if not (0 <= lac <= 0xFFFF):
        raise ValueError(f"LAC must be 0–65535: {lac}")

    m = [int(d) for d in mcc]
    n = [int(d) for d in mnc]
    n3 = n[2] if len(n) == 3 else 0xF

    return bytes([
        (m[1] << 4) | m[0],
        (n3   << 4) | m[2],
        (n[1] << 4) | n[0],
        (lac >> 8) & 0xFF,
        lac & 0xFF,
    ])


def build_message(msg_type: int, ies: list[tuple[int, bytes]]) -> bytes:
    """
    Encode a SGsAP message.
    ies: ordered list of (ie_type, ie_value) tuples.
    """
    parts: list[bytes] = [bytes([msg_type])]
    for ie_type, ie_value in ies:
        if len(ie_value) > 255:
            raise ValueError(
                f"IE 0x{ie_type:02X} value length {len(ie_value)} exceeds 255"
            )
        parts.append(bytes([ie_type, len(ie_value)]))
        parts.append(ie_value)
    return b"".join(parts)


def build_location_update_accept(imsi_bytes: bytes, lai_bytes: bytes, tmsi: int | None = None) -> bytes:
    ies: list[tuple[int, bytes]] = [
        (IEType.IMSI, imsi_bytes),
        (IEType.EPS_LOCATION_AREA_ID, lai_bytes),
    ]
    if tmsi is not None:
        ies.append((IEType.MOBILE_IDENTITY, encode_tmsi_identity(tmsi)))
    return build_message(MsgType.LOCATION_UPDATE_ACCEPT, ies)


def build_location_update_reject(imsi_bytes: bytes, cause: int) -> bytes:
    return build_message(
        MsgType.LOCATION_UPDATE_REJECT,
        [
            (IEType.IMSI, imsi_bytes),
            (0x08, bytes([cause])),  # Reject Cause IE (0x08)
        ],
    )


def _encode_fqdn(name: str) -> bytes:
    """
    Encode a domain name in DNS wire format (RFC 1035 §3.1).
    Each label is prefixed with its length byte; the name ends with 0x00.
    e.g. "vlr.minismsc.local" → b'\x03vlr\x08minismsc\x05local\x00'
    """
    parts = bytearray()
    for label in name.rstrip(".").split("."):
        encoded = label.encode("ascii")
        parts.append(len(encoded))
        parts.extend(encoded)
    parts.append(0x00)
    return bytes(parts)


def build_paging_request(imsi_bytes: bytes, vlr_name: str, service_indicator: int = 0x02) -> bytes:
    """
    SGsAP-PAGING-REQUEST (0x01), TS 29.118 section 8.14.
    Mandatory IEs: IMSI, VLR Name (DNS wire-format FQDN), Service Indicator (0x02 = SMS).
    """
    return build_message(
        MsgType.PAGING_REQUEST,
        [
            (IEType.IMSI, imsi_bytes),
            (IEType.VLR_NAME, _encode_fqdn(vlr_name)),
            (IEType.SERVICE_INDICATOR, bytes([service_indicator])),
        ],
    )


def build_reset_ack() -> bytes:
    return build_message(MsgType.RESET_ACK, [])


def build_downlink_unitdata(imsi_bytes: bytes, nas_pdu: bytes) -> bytes:
    return build_message(
        MsgType.DOWNLINK_UNITDATA,
        [
            (IEType.IMSI, imsi_bytes),
            (IEType.NAS_MESSAGE_CONTAINER, nas_pdu),
        ],
    )
