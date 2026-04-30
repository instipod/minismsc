from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    sgsap_host: str = "0.0.0.0"
    sgsap_port: int = 29118
    sms_retry_interval: int = 10
    sms_max_retries: int = 10
    db_path: str = "sms_queue.sqlite"
    log_level: str = "INFO"

    # VLR Location Area Identity — included in SGsAP-LOCATION-UPDATE-ACCEPT
    # Override via MINISMSC_VLR_MCC / MINISMSC_VLR_MNC / MINISMSC_VLR_LAC
    vlr_mcc: str = "315"
    vlr_mnc: str = "010"
    vlr_lac: int = 1

    # VLR MSISDN — used as the RP-Originator-Address in MT SMS RP-DATA.
    # TS 24.011 requires the SMSC/MSC address here; UEs reject an empty RP-OA.
    # Override via MINISMSC_VLR_MSISDN
    vlr_msisdn: str = "+13155550001"

    # VLR Name — FQDN sent in the VLR Name IE of SGsAP-PAGING-REQUEST.
    # Override via MINISMSC_VLR_NAME
    vlr_name: str = "vlr.minismsc.local"

    model_config = {"env_prefix": "MINISMSC_"}


settings = Settings()
