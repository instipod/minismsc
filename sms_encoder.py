"""
SMS TP-LAYER (Transfer Protocol Layer) Encoder
Implements SMS-DELIVER and SMS-SUBMIT PDU encoding
Based on 3GPP TS 23.040
"""

from datetime import datetime
from typing import Optional
import struct


class SMSEncoding:
    """SMS encoding schemes"""
    GSM7 = 0x00
    DATA_8BIT = 0x04
    UCS2 = 0x08


def encode_address(number: str, type_of_number: int = 0x91) -> bytes:
    """
    Encode phone number in semi-octet format
    type_of_number: 0x91 = International, 0x81 = Unknown/National
    """
    # Remove + if present
    if number.startswith('+'):
        number = number[1:]
        type_of_number = 0x91

    # Length is number of useful semi-octets
    length = len(number)

    # Pad with F if odd length
    if length % 2 == 1:
        number += 'F'

    result = bytearray()
    result.append(length)
    result.append(type_of_number)

    # Swap each pair of digits
    for i in range(0, len(number), 2):
        d1 = int(number[i], 16)
        d2 = int(number[i + 1], 16)
        result.append((d2 << 4) | d1)

    return bytes(result)


def encode_timestamp(dt: Optional[datetime] = None) -> bytes:
    """Encode timestamp in semi-octet format"""
    if dt is None:
        dt = datetime.now()

    def swap_digits(value: int) -> int:
        """Swap decimal digits to semi-octet format"""
        d1 = value % 10
        d2 = value // 10
        return (d1 << 4) | d2

    result = bytearray()
    result.append(swap_digits(dt.year % 100))
    result.append(swap_digits(dt.month))
    result.append(swap_digits(dt.day))
    result.append(swap_digits(dt.hour))
    result.append(swap_digits(dt.minute))
    result.append(swap_digits(dt.second))

    # Timezone (quarters of an hour)
    # For simplicity, using 0 (GMT)
    result.append(0x00)

    return bytes(result)


def encode_gsm7(text: str) -> bytes:
    """
    Encode text in GSM 7-bit default alphabet
    Note: This is a simplified version, doesn't handle extended chars
    """
    gsm7_basic = (
        "@£$¥èéùìòÇ\nØø\rÅåΔ_ΦΓΛΩΠΨΣΘΞ\x1bÆæßÉ !\"#¤%&'()*+,-./0123456789:;<=>?"
        "¡ABCDEFGHIJKLMNOPQRSTUVWXYZÄÖÑÜ§¿abcdefghijklmnopqrstuvwxyzäöñüà"
    )

    result = []
    for char in text:
        if char in gsm7_basic:
            result.append(gsm7_basic.index(char))
        else:
            result.append(0x20)  # Space as fallback

    # Pack 7-bit characters into bytes
    packed = bytearray()
    bits = 0
    bit_count = 0

    for value in result:
        bits |= (value << bit_count)
        bit_count += 7

        while bit_count >= 8:
            packed.append(bits & 0xFF)
            bits >>= 8
            bit_count -= 8

    if bit_count > 0:
        packed.append(bits & 0xFF)

    return bytes(packed)


def create_sms_deliver_pdu(sender: str, text: str, encoding: int = SMSEncoding.GSM7,
                           request_status_report: bool = False) -> bytes:
    """
    Create an SMS-DELIVER PDU (for mobile-terminated SMS)

    Args:
        sender: Sender phone number (e.g., "+1234567890")
        text: SMS text content
        encoding: Text encoding (GSM7, 8BIT, or UCS2)
        request_status_report: If True, request delivery report from UE (TP-SRI=1)

    Returns:
        Complete SMS-DELIVER TPDU
    """
    pdu = bytearray()

    # TP-MTI (Message Type Indicator): SMS-DELIVER = 0x00
    # TP-MMS (More Messages to Send): 0 (no more messages)
    # TP-SRI (Status Report Indication): bit 5
    # TP-UDHI (User Data Header Indicator): 0
    # TP-RP (Reply Path): 0
    first_byte = 0x00
    if request_status_report:
        first_byte |= 0x20  # Set bit 5 (TP-SRI)
    pdu.append(first_byte)

    # TP-OA (Originating Address)
    pdu.extend(encode_address(sender))

    # TP-PID (Protocol Identifier): 0x00 (default)
    pdu.append(0x00)

    # TP-DCS (Data Coding Scheme)
    pdu.append(encoding)

    # TP-SCTS (Service Centre Time Stamp)
    pdu.extend(encode_timestamp())

    # TP-UDL (User Data Length) and TP-UD (User Data)
    if encoding == SMSEncoding.GSM7:
        user_data = encode_gsm7(text)
        pdu.append(len(text))  # Length in septets for GSM7
        pdu.extend(user_data)
    elif encoding == SMSEncoding.UCS2:
        user_data = text.encode('utf-16-be')
        pdu.append(len(user_data))
        pdu.extend(user_data)
    else:  # 8-bit
        user_data = text.encode('latin-1')
        pdu.append(len(user_data))
        pdu.extend(user_data)

    return bytes(pdu)


def create_rp_data_dl(destination: str, tpdu: bytes, reference: int = 0, smsc_address: str = "+0000",
                      include_destination: bool = True) -> bytes:
    """
    Create RP-DATA message (downlink) - wraps SMS TPDU for NAS transport

    Args:
        destination: Destination MSISDN
        tpdu: SMS TPDU (from create_sms_deliver_pdu)
        reference: RP message reference (0-255)
        smsc_address: SMSC service center address (default: +0000)
        include_destination: Include RP-Destination Address (default: True)

    Returns:
        Complete RP-DATA message for NAS container
    """
    rp_data = bytearray()

    # RP-MTI (Message Type Indicator): RP-DATA (network to MS) = 0x01
    rp_data.append(0x01)

    # RP-Message Reference
    rp_data.append(reference & 0xFF)

    # RP-Originator Address (SMSC address) - MANDATORY for MT-SMS
    # encode_address returns [digit_count, type, bcd_digits...]
    # But RP address needs [byte_count, type, bcd_digits...]
    # So we skip the first byte and use length of remainder
    smsc_addr_full = encode_address(smsc_address)
    smsc_addr_value = smsc_addr_full[1:]  # Skip digit count, keep [type, bcd_digits...]
    rp_data.append(len(smsc_addr_value))  # Length in bytes
    rp_data.extend(smsc_addr_value)

    # RP-Destination Address (destination MSISDN)
    # For MT-SMS, some implementations expect this to be absent (length 0)
    # since routing is done via IMSI in SGsAP layer
    if include_destination and destination:
        dest_addr_full = encode_address(destination)
        dest_addr_value = dest_addr_full[1:]  # Skip digit count, keep [type, bcd_digits...]
        rp_data.append(len(dest_addr_value))  # Length in bytes
        rp_data.extend(dest_addr_value)
    else:
        # RP-Destination Address absent (length 0)
        rp_data.append(0x00)

    # RP-User Data (contains the TPDU)
    rp_data.append(len(tpdu))
    rp_data.extend(tpdu)

    return bytes(rp_data)


def create_cp_data(rp_message: bytes, ti: int = 0) -> bytes:
    """
    Create CP-DATA message - wraps RP-DATA for NAS transport

    Args:
        rp_message: RP-DATA message
        ti: Transaction Identifier (0-6)

    Returns:
        Complete CP-DATA message (NAS message)
    """
    cp_data = bytearray()

    # Protocol Discriminator: SMS (0x09) + Transaction ID
    pd_ti = 0x09 | ((ti & 0x07) << 4)
    cp_data.append(pd_ti)

    # Message Type: CP-DATA = 0x01
    cp_data.append(0x01)

    # CP-User Data (contains the RP message)
    cp_data.append(len(rp_message))
    cp_data.extend(rp_message)

    return bytes(cp_data)


def create_cp_ack(ti: int = 0) -> bytes:
    """
    Create CP-ACK message - acknowledges CP-DATA receipt

    Args:
        ti: Transaction Identifier (must match the CP-DATA being acknowledged)
            TI flag should be set to 1 (responding to peer-allocated TI)

    Returns:
        Complete CP-ACK message (NAS message)
    """
    cp_ack = bytearray()

    # Protocol Discriminator: SMS (0x09) + Transaction ID with TI flag = 1
    # TI flag (bit 4) = 1 means responding to peer-allocated TI
    pd_ti = 0x09 | ((ti & 0x07) << 4) | 0x08
    cp_ack.append(pd_ti)

    # Message Type: CP-ACK = 0x04
    cp_ack.append(0x04)

    return bytes(cp_ack)
