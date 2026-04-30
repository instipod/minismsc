"""
Minimal MT SMS NAS PDU encoder for SGsAP DOWNLINK-UNITDATA.

Stack (bottom-up):
  SMS-DELIVER TPDU  (3GPP TS 23.040)
  RP-DATA           (3GPP TS 24.011 section 7.3.1.1, MT direction)
  CP-DATA           (3GPP TS 24.011 section 7.2)
  = NAS Message Container IE value in SGsAP

No multi-part SMS. Single-part only (max 160 GSM-7 chars or 70 UCS-2 chars).
"""

from datetime import datetime, timezone

from config import settings

# GSM 03.38 basic character set (index = septet value)
_GSM7 = (
    "@£$¥èéùìòÇ\nØø\rÅåΔ_ΦΓΛΩΠΨΣΘΞ\x1bÆæßÉ !\"#¤%&'()*+,-./"
    "0123456789:;<=>?¡ABCDEFGHIJKLMNOPQRSTUVWXYZ"
    "ÄÖÑÜ§¿abcdefghijklmnopqrstuvwxyz"
    "äöñüà"
)
_GSM7_SET = set(_GSM7)


def _is_gsm7(text: str) -> bool:
    return all(c in _GSM7_SET for c in text)


def _decode_gsm7_packed(data: bytes, n_septets: int) -> str:
    """Unpack GSM-7 octets back to a string (inverse of _encode_gsm7_packed)."""
    bits = 0
    bit_count = 0
    septets = []
    for byte in data:
        bits |= byte << bit_count
        bit_count += 8
        while bit_count >= 7 and len(septets) < n_septets:
            septets.append(bits & 0x7F)
            bits >>= 7
            bit_count -= 7
    return "".join(_GSM7[s] for s in septets if s < len(_GSM7))


def _unpack_semi_octets(packed: bytes, n_digits: int) -> str:
    """Unpack BCD semi-octet bytes to a digit string."""
    digits = []
    for byte in packed:
        digits.append(str(byte & 0x0F))
        hi = (byte >> 4) & 0x0F
        if hi != 0xF:
            digits.append(str(hi))
    return "".join(digits[:n_digits])


def _decode_tp_address(data: bytes, offset: int) -> tuple[str, int]:
    """
    Decode a TP-layer address (TS 23.040 §9.1.2.5).
    Length byte is the number of useful semi-octets (digit count).
    Returns (number_string, total_bytes_consumed).
    """
    n_digits = data[offset]
    ton_npi = data[offset + 1]
    n_bytes = (n_digits + 1) // 2
    packed = data[offset + 2: offset + 2 + n_bytes]
    digits = _unpack_semi_octets(packed, n_digits)
    prefix = "+" if (ton_npi & 0x70) == 0x10 else ""
    return prefix + digits, 2 + n_bytes


def _decode_sms_submit(tpdu: bytes) -> dict:
    """Decode an SMS-SUBMIT TPDU (TS 23.040 §9.2.2.2)."""
    if len(tpdu) < 4:
        raise ValueError(f"TPDU too short: {len(tpdu)} bytes")
    first = tpdu[0]
    tp_mti = first & 0x03
    if tp_mti != 0x01:
        raise ValueError(f"Not SMS-SUBMIT: TP-MTI={tp_mti}")
    tp_vpf = (first >> 3) & 0x03
    offset = 2  # skip first byte + TP-MR
    destination, da_size = _decode_tp_address(tpdu, offset)
    offset += da_size
    _tp_pid = tpdu[offset]; offset += 1
    tp_dcs = tpdu[offset]; offset += 1
    vp_sizes = {0: 0, 1: 7, 2: 1, 3: 7}
    offset += vp_sizes.get(tp_vpf, 0)
    tp_udl = tpdu[offset]; offset += 1
    tp_ud = tpdu[offset:]
    coding = (tp_dcs >> 2) & 0x03 if (tp_dcs & 0xF0) == 0 else (0 if tp_dcs == 0 else 2 if tp_dcs == 0x08 else 1)
    if coding == 0:
        text = _decode_gsm7_packed(tp_ud, tp_udl)
    elif coding == 2:
        text = tp_ud[:tp_udl].decode("utf-16-be", errors="replace")
    else:
        text = f"<8-bit data: {tp_ud.hex()}>"
    return {"destination": destination, "text": text}


def _decode_rp_data_mo(rp: bytes) -> dict:
    """Decode RP-DATA from MS (MO direction, TS 24.011 §7.3.1.2)."""
    rp_mr = rp[1]
    offset = 2
    # RP-OA (for MO, this is the UE's own MSISDN or empty — skip it)
    oa_len = rp[offset]; offset += 1 + oa_len
    # RP-DA (SMSC address — skip it)
    da_len = rp[offset]; offset += 1 + da_len
    # RP-UD (TPDU)
    ud_len = rp[offset]; offset += 1
    tpdu = rp[offset: offset + ud_len]
    result: dict = {"type": "MO-SMS", "mr": rp_mr}
    try:
        result.update(_decode_sms_submit(tpdu))
    except Exception as exc:
        result["decode_error"] = str(exc)
        result["tpdu_hex"] = tpdu.hex()
    return result


def decode_uplink_nas_pdu(data: bytes) -> dict:
    """
    Decode a NAS PDU received in SGsAP-UPLINK-UNITDATA.
    Handles CP-DATA (RP-DATA-MO, RP-ACK, RP-ERROR, RP-SMMA) and CP-ACK/CP-ERROR.
    Returns a dict with at least 'type' and 'pd_ti' keys.
    pd_ti is the first byte of the NAS PDU (PD + TI flag + TI value).
    """
    if len(data) < 2:
        raise ValueError(f"NAS PDU too short: {len(data)} bytes")
    pd_ti = data[0]
    cp_mti = data[1]
    if cp_mti == 0x04:
        return {"type": "CP-ACK", "pd_ti": pd_ti}
    if cp_mti == 0x10:
        return {"type": "CP-ERROR", "cause": data[2] if len(data) > 2 else 0, "pd_ti": pd_ti}
    if cp_mti != 0x01:
        return {"type": f"CP-UNKNOWN(0x{cp_mti:02X})", "pd_ti": pd_ti}
    if len(data) < 3:
        raise ValueError("CP-DATA missing length byte")
    cp_ud_len = data[2]
    if len(data) < 3 + cp_ud_len:
        raise ValueError(f"CP-User-Data truncated (want {cp_ud_len}, have {len(data)-3})")
    rp = data[3: 3 + cp_ud_len]
    if len(rp) < 2:
        raise ValueError("RP payload too short")
    rp_mti = rp[0] & 0x07
    rp_mr = rp[1]
    if rp_mti in (0, 1):   # RP-DATA (some UEs send 0 instead of 1 in uplink)
        result = _decode_rp_data_mo(rp)
        result["pd_ti"] = pd_ti
        return result
    if rp_mti in (2, 3):   # RP-ACK from MS (MT delivery confirmed); 3=standard, 2=direction-bit-inverted
        return {"type": "RP-ACK", "mr": rp_mr, "pd_ti": pd_ti}
    if rp_mti in (4, 5):   # RP-ERROR from MS; 5=standard, 4=direction-bit-inverted
        cause = rp[4] if len(rp) > 4 else 0
        return {"type": "RP-ERROR", "mr": rp_mr, "cause": cause, "pd_ti": pd_ti}
    if rp_mti in (6, 7):   # RP-SMMA (MS memory available); 6=standard, 7=direction-bit-inverted
        return {"type": "RP-SMMA", "mr": rp_mr, "pd_ti": pd_ti}
    return {"type": f"RP-UNKNOWN(mti={rp_mti})", "mr": rp_mr, "pd_ti": pd_ti}


def build_cp_ack(pd_ti: int) -> bytes:
    """
    CP-ACK NAS PDU (TS 24.011 §7.2) with TI flag flipped for the responding side.
    pd_ti: first byte from the received CP-DATA.
    """
    return bytes([pd_ti ^ 0x80, 0x04])


def build_rp_ack_nas_pdu(pd_ti: int, rp_mr: int) -> bytes:
    """
    CP-DATA wrapping a minimal RP-ACK (network→MS, TS 24.011 §7.3.2.1).
    Sent after successfully receiving an MO SMS RP-DATA.

    Standard RP-ACK N→MS = 0x02 (bit0=0).  Some UEs invert the direction bit,
    sending RP-DATA MO as 0x00 (should be 0x01) and expecting RP-ACK as 0x03
    (should be 0x02).  Use 0x03 to satisfy both inverted and spec-compliant UEs
    (a spec-compliant UE will still accept 0x03 as RP-ACK MS→N in the context
    of closing a MO transaction it initiated).
    """
    rp_ack = bytes([0x03, rp_mr & 0xFF])  # RP-MTI=3 for direction-bit-inverted UEs
    return bytes([pd_ti ^ 0x80, 0x01, len(rp_ack)]) + rp_ack


def _encode_gsm7_packed(text: str) -> bytes:
    """Pack septets into octets (8 septets → 7 bytes)."""
    septets = [_GSM7.index(c) for c in text]
    result = bytearray()
    bit_buf = 0
    bit_count = 0
    for septet in septets:
        bit_buf |= septet << bit_count
        bit_count += 7
        while bit_count >= 8:
            result.append(bit_buf & 0xFF)
            bit_buf >>= 8
            bit_count -= 8
    if bit_count > 0:
        result.append(bit_buf & 0xFF)
    return bytes(result)


def _pack_digits(digits: str) -> bytes:
    """Pack digit string into semi-octet bytes, padding the last nibble with 0xF."""
    packed = bytearray()
    for i in range(0, len(digits), 2):
        lo = int(digits[i])
        hi = int(digits[i + 1]) if i + 1 < len(digits) else 0xF
        packed.append((hi << 4) | lo)
    return bytes(packed)


def _encode_tp_address(number: str) -> bytes:
    """
    Encode a TP-layer address (TP-OA in SMS-DELIVER, TS 23.040 §9.1.2.5).
    Length byte = number of useful semi-octets (digit count), NOT byte count.
    """
    if number.startswith("+"):
        ton_npi = 0x91
        digits = number[1:]
    else:
        ton_npi = 0x81
        digits = number
    return bytes([len(digits), ton_npi]) + _pack_digits(digits)


def _encode_rp_address(number: str) -> bytes:
    """
    Encode an RP-layer address (RP-OA / RP-DA in RP-DATA, TS 24.011 §8.2.5.1).
    Length byte = number of bytes that follow it (1 type byte + packed digit bytes).
    This differs from TP-OA where the length counts semi-octets.
    """
    if number.startswith("+"):
        ton_npi = 0x91
        digits = number[1:]
    else:
        ton_npi = 0x81
        digits = number
    packed = _pack_digits(digits)
    return bytes([1 + len(packed), ton_npi]) + packed


def _encode_scts(dt: datetime) -> bytes:
    """Encode Service Centre Time Stamp (7 bytes, semi-octet BCD)."""

    def bcd(v: int) -> int:
        return ((v % 10) << 4) | (v // 10)

    year = dt.year % 100
    tz_offset = 0  # UTC, positive
    return bytes([
        bcd(year),
        bcd(dt.month),
        bcd(dt.day),
        bcd(dt.hour),
        bcd(dt.minute),
        bcd(dt.second),
        tz_offset,
    ])


def _build_tpdu(sender: str, message: str) -> bytes:
    """
    Build an SMS-DELIVER TPDU (3GPP TS 23.040 section 9.2.2.1).
    """
    # TP-MTI=00 (DELIVER), TP-MMS=1 (no more messages), TP-SRI=0, TP-UDHI=0, TP-RP=0
    first_byte = 0x04  # 0b00000100: MTI=00, MMS=1

    oa = _encode_tp_address(sender)   # TP-Originating-Address
    pid = 0x00                      # TP-PID: normal SMS
    now = datetime.now(tz=timezone.utc)
    scts = _encode_scts(now)

    if _is_gsm7(message):
        dcs = 0x00
        ud = _encode_gsm7_packed(message)
        udl = len(message)  # number of septets
    else:
        dcs = 0x08  # UCS-2
        ud = message.encode("utf-16-be")
        udl = len(ud)  # number of bytes

    return bytes([first_byte]) + oa + bytes([pid, dcs]) + scts + bytes([udl]) + ud


def _build_rp_data(tpdu: bytes, smsc_address: str, mr: int = 0) -> bytes:
    """
    Build RP-DATA for MT SMS (network→MS), TS 24.011 section 7.3.1.1.

    RP-OA carries the MSC/SMSC address — mandatory for MT; an empty RP-OA
    causes UEs to return RP-ERROR cause 96 (invalid mandatory information).

    RP-User-Data is LV (length + TPDU), not TLV — real implementations and
    Wireshark omit the 0x41 IEI even though Table 7.7 lists the format as TLV.
    """
    rp_mti = 0x01  # RP-DATA (network → MS)
    rp_mr = mr & 0xFF
    rp_oa = _encode_rp_address(smsc_address)  # MSC address as originator
    rp_da = bytes([0x00])                      # empty destination address for MT
    rp_ud = bytes([len(tpdu)]) + tpdu         # LV — no IEI prefix

    return bytes([rp_mti, rp_mr]) + rp_oa + rp_da + rp_ud


def _build_cp_data(rp_data: bytes) -> bytes:
    """
    Build CP-DATA (3GPP TS 24.011 section 7.2).
    Protocol discriminator + TI = 0x09 (SMS, TI flag=0, TI value=0).
    Message type = 0x01 (CP-DATA).
    """
    pd_ti = 0x09
    cp_mti = 0x01
    if len(rp_data) > 255:
        raise ValueError(f"RP-DATA too long for CP-DATA: {len(rp_data)} bytes")
    return bytes([pd_ti, cp_mti, len(rp_data)]) + rp_data


def build_mt_sms_nas_pdu(sender: str, message: str, mr: int = 0) -> bytes:
    """
    Build the complete NAS PDU for an MT SMS delivery.
    This is the value of the NAS Message Container IE in SGsAP DOWNLINK-UNITDATA.
    mr: RP message reference (0-255); used to correlate RP-ACK/RP-ERROR from the UE.
    """
    tpdu = _build_tpdu(sender, message)
    rp_data = _build_rp_data(tpdu, settings.vlr_msisdn, mr=mr)
    cp_data = _build_cp_data(rp_data)
    return cp_data
