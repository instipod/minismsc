from enum import IntEnum


class MsgType(IntEnum):
    PAGING_REQUEST = 0x01
    PAGING_REJECT = 0x02          # TS 29.118 §9.3.16 — MME rejects paging
    SERVICE_REQUEST = 0x06
    DOWNLINK_UNITDATA = 0x07
    UPLINK_UNITDATA = 0x08
    LOCATION_UPDATE_REQUEST = 0x09
    LOCATION_UPDATE_ACCEPT = 0x0A
    LOCATION_UPDATE_REJECT = 0x0B
    TMSI_REALLOCATION_COMPLETE = 0x0C
    IMSI_DETACH_INDICATION = 0x14  # TS 29.118 §9.3.7 — UE detached from non-EPS services
    RESET_INDICATION = 0x15
    RESET_ACK = 0x16
    STATUS = 0x1D


class IEType(IntEnum):
    IMSI = 0x01
    VLR_NAME = 0x02               # TS 29.118 Table 9.2
    EPS_LOCATION_AREA_ID = 0x04  # Location Area Identifier (TS 29.118 Table 9.2)
    SGSAP_CAUSE = 0x08            # TS 29.118 Table 9.2 — rejection/error cause
    MME_NAME = 0x09               # TS 29.118 / Open5GS SGSAP_IE_MME_NAME_TYPE
    MOBILE_IDENTITY = 0x0E        # TS 29.118 / Open5GS SGSAP_IE_MOBILE_IDENTITY_TYPE — carries assigned TMSI/P-TMSI
    NAS_MESSAGE_CONTAINER = 0x16  # TS 29.118 Table 9.2
    SERVICE_INDICATOR = 0x20      # TS 29.118 Table 9.2 — value 0x02 = SMS
