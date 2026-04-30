from dataclasses import dataclass

from .protocol import IEType, MsgType


@dataclass
class SgsapMessage:
    msg_type: MsgType
    ies: dict  # IEType (or raw int for unknown types) → bytes
    raw: bytes


def parse_message(data: bytes) -> SgsapMessage:
    """
    Parse a raw SGsAP PDU into a SgsapMessage.

    Format: MsgType (1 byte) followed by zero or more TLV IEs:
      IE Type (1 byte) | IE Length (1 byte) | IE Value (IE Length bytes)

    Raises ValueError on empty input, unknown message type, or truncated IEs.
    Unknown IE types are stored with their raw int key rather than dropped.
    """
    if len(data) < 1:
        raise ValueError("Empty SGsAP message")
    try:
        msg_type = MsgType(data[0])
    except ValueError:
        raise ValueError(f"Unknown SGsAP message type: 0x{data[0]:02X}")

    ies: dict = {}
    offset = 1
    while offset < len(data):
        if offset + 2 > len(data):
            raise ValueError(f"Truncated IE header at offset {offset}")
        ie_type_raw = data[offset]
        ie_len = data[offset + 1]
        offset += 2
        if offset + ie_len > len(data):
            raise ValueError(
                f"IE type 0x{ie_type_raw:02X} length {ie_len} exceeds buffer at offset {offset}"
            )
        ie_value = data[offset : offset + ie_len]
        try:
            ie_key: IEType | int = IEType(ie_type_raw)
        except ValueError:
            ie_key = ie_type_raw
        ies[ie_key] = ie_value
        offset += ie_len

    return SgsapMessage(msg_type=msg_type, ies=ies, raw=bytes(data))
