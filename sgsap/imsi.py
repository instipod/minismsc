"""
IMSI BCD encoding/decoding per 3GPP TS 24.008 section 10.5.1.4
(Mobile Identity IE value, without the outer length octet used in TS 24.008 —
SGsAP carries the identity value directly inside the SGsAP IE TLV wrapper).

Byte 0 layout: digit1[7:4] | odd_flag[3] | type[2:0]
  type = 001 (IMSI)
  odd_flag = 1 if total digit count is odd, else 0
Subsequent bytes: low nibble = earlier digit, high nibble = later digit.
For even total digit count, the high nibble of the last byte is 0xF padding.
"""


def encode_imsi(imsi_str: str) -> bytes:
    if not imsi_str.isdigit():
        raise ValueError(f"IMSI must be digits only: {imsi_str!r}")
    if not (6 <= len(imsi_str) <= 15):
        raise ValueError(f"IMSI length must be 6–15 digits, got {len(imsi_str)}")
    digits = imsi_str
    odd = len(digits) % 2 == 1
    byte0 = (int(digits[0]) << 4) | (0x08 if odd else 0x00) | 0x01
    result = bytearray([byte0])
    remaining = digits[1:]
    for i in range(0, len(remaining), 2):
        lo = int(remaining[i])
        hi = int(remaining[i + 1]) if i + 1 < len(remaining) else 0xF
        result.append((hi << 4) | lo)
    return bytes(result)


def decode_imsi(data: bytes) -> str:
    if not data:
        raise ValueError("Empty IMSI IE value")
    odd = bool(data[0] & 0x08)
    # First digit from high nibble of byte 0
    digits = [str(data[0] >> 4)]
    # Each remaining byte contributes low nibble then high nibble
    for byte in data[1:]:
        digits.append(str(byte & 0x0F))
        digits.append(str(byte >> 4))
    # For even digit count, last appended value is 0xF padding
    if not odd and digits and digits[-1].upper() == 'F':
        digits.pop()
    return "".join(digits)
