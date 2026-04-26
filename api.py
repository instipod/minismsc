#!/usr/bin/env python3
"""
REST API for Mini SMSC
Provides HTTP endpoints to send SMS via Open5GS
"""

import os
import logging
import threading
from typing import Optional, List
from fastapi import FastAPI, HTTPException, status
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field, field_validator
from smsc import SMSCService

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s'
)
logger = logging.getLogger(__name__)

app = FastAPI(
    title="Mini SMSC API",
    description="REST API for sending SMS via Open5GS",
    version="1.0.0"
)
smsc_service: SMSCService = None


# Pydantic models
class SMSRequest(BaseModel):
    imsi: str = Field(..., description="Subscriber IMSI (14-15 digits)")
    msisdn: Optional[str] = Field(None, description="Subscriber phone number (defaults to IMSI)")
    sender: str = Field("SMSC", description="Sender address/short code")
    text: str = Field(..., description="SMS text content (max 160 characters)")
    request_delivery_report: bool = Field(False, description="Request delivery confirmation")

    @field_validator('imsi')
    @classmethod
    def validate_imsi(cls, v: str) -> str:
        if not v.isdigit() or len(v) < 14 or len(v) > 15:
            raise ValueError('IMSI must be 14-15 digits')
        return v

    @field_validator('text')
    @classmethod
    def validate_text(cls, v: str) -> str:
        if len(v) > 160:
            raise ValueError('Text must not exceed 160 characters')
        return v


class BulkSMSRequest(BaseModel):
    messages: List[SMSRequest] = Field(..., description="List of SMS messages to send")

    @field_validator('messages')
    @classmethod
    def validate_messages_length(cls, v: List[SMSRequest]) -> List[SMSRequest]:
        if len(v) > 100:
            raise ValueError('Maximum 100 messages per bulk request')
        return v


class HealthResponse(BaseModel):
    status: str
    service: str
    connected: bool


class LAIInfo(BaseModel):
    mcc: str
    mnc: str
    lac: int


class StatusResponse(BaseModel):
    connected: bool
    listen_address: str
    listen_port: int
    vlr_name: str
    smsc_address: str
    lai: LAIInfo
    queue_length: int


class SMSDetails(BaseModel):
    imsi: str
    msisdn: str
    sender: str
    text_length: int
    request_delivery_report: bool


class SMSResponse(BaseModel):
    status: str
    message: str
    details: SMSDetails


class BulkError(BaseModel):
    index: int
    imsi: Optional[str] = None
    error: str


class BulkSMSResponse(BaseModel):
    queued: int
    failed: int
    errors: List[BulkError]


# Endpoints
@app.get('/health', response_model=HealthResponse)
async def health():
    """Health check endpoint"""
    return HealthResponse(
        status='healthy',
        service='mini-smsc',
        connected=smsc_service.connected if smsc_service else False
    )


@app.get('/api/status', response_model=StatusResponse)
async def get_status():
    """Get SMSC service status"""
    if not smsc_service:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail='SMSC service not initialized'
        )

    return StatusResponse(
        connected=smsc_service.connected,
        listen_address=smsc_service.listen_address,
        listen_port=smsc_service.listen_port,
        vlr_name=smsc_service.vlr_name,
        smsc_address=smsc_service.smsc_address,
        lai=LAIInfo(
            mcc=smsc_service.lai_mcc,
            mnc=smsc_service.lai_mnc,
            lac=smsc_service.lai_lac
        ),
        queue_length=len(smsc_service.message_queue)
    )


@app.post('/api/sms/send', response_model=SMSResponse, status_code=status.HTTP_202_ACCEPTED)
async def send_sms(sms: SMSRequest):
    """Send SMS to a subscriber"""
    if not smsc_service:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail='SMSC service not initialized'
        )

    if not smsc_service.connected:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail='SMSC not connected to MME'
        )

    msisdn = sms.msisdn or sms.imsi

    try:
        smsc_service.send_sms(
            imsi=sms.imsi,
            msisdn=msisdn,
            sender=sms.sender,
            text=sms.text,
            request_delivery_report=sms.request_delivery_report
        )

        logger.info(f"API: SMS queued for {sms.imsi}")

        return SMSResponse(
            status='queued',
            message='SMS queued for delivery',
            details=SMSDetails(
                imsi=sms.imsi,
                msisdn=msisdn,
                sender=sms.sender,
                text_length=len(sms.text),
                request_delivery_report=sms.request_delivery_report
            )
        )

    except Exception as e:
        logger.error(f"API: Error queuing SMS: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=str(e)
        )


@app.post('/api/sms/send/bulk', response_model=BulkSMSResponse, status_code=status.HTTP_202_ACCEPTED)
async def send_bulk_sms(request: BulkSMSRequest):
    """Send SMS to multiple subscribers"""
    if not smsc_service:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail='SMSC service not initialized'
        )

    if not smsc_service.connected:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail='SMSC not connected to MME'
        )

    results = BulkSMSResponse(queued=0, failed=0, errors=[])

    for idx, msg in enumerate(request.messages):
        msisdn = msg.msisdn or msg.imsi

        try:
            smsc_service.send_sms(
                msg.imsi,
                msisdn,
                msg.sender,
                msg.text,
                msg.request_delivery_report
            )
            results.queued += 1
        except Exception as e:
            results.failed += 1
            results.errors.append(BulkError(
                index=idx,
                imsi=msg.imsi,
                error=str(e)
            ))

    return results


def run_smsc_background(smsc: SMSCService):
    """Run SMSC service in background thread"""
    try:
        smsc.run()
    except Exception as e:
        logger.error(f"SMSC service error: {e}")


def main():
    """Main entry point"""
    import argparse
    import uvicorn

    parser = argparse.ArgumentParser(description='Mini SMSC/VLR REST API')
    parser.add_argument('--listen-address',
                       default=os.getenv('LISTEN_ADDRESS', '0.0.0.0'),
                       help='Address to bind SGsAP server (env: LISTEN_ADDRESS, default: 0.0.0.0)')
    parser.add_argument('--listen-port', type=int,
                       default=int(os.getenv('LISTEN_PORT', '29118')),
                       help='SGsAP server port (env: LISTEN_PORT, default: 29118)')
    parser.add_argument('--vlr-name',
                       default=os.getenv('VLR_NAME', 'vlr.open5gs.org'),
                       help='VLR/MSC FQDN (env: VLR_NAME, default: vlr.open5gs.org)')
    parser.add_argument('--lai-mcc',
                       default=os.getenv('LAI_MCC', '001'),
                       help='Location Area MCC (env: LAI_MCC, default: 001)')
    parser.add_argument('--lai-mnc',
                       default=os.getenv('LAI_MNC', '01'),
                       help='Location Area MNC (env: LAI_MNC, default: 01)')
    parser.add_argument('--lai-lac', type=int,
                       default=int(os.getenv('LAI_LAC', '1')),
                       help='Location Area Code (env: LAI_LAC, default: 1)')
    parser.add_argument('--smsc-address',
                       default=os.getenv('SMSC_ADDRESS', '+0000'),
                       help='SMSC service center number (env: SMSC_ADDRESS, default: +0000)')
    parser.add_argument('--api-host',
                       default=os.getenv('API_HOST', '0.0.0.0'),
                       help='API host (env: API_HOST, default: 0.0.0.0)')
    parser.add_argument('--api-port', type=int,
                       default=int(os.getenv('API_PORT', '8080')),
                       help='API port (env: API_PORT, default: 8080)')

    args = parser.parse_args()

    # Create SMSC service
    global smsc_service
    smsc_service = SMSCService(
        args.listen_address,
        args.listen_port,
        args.vlr_name,
        args.lai_mcc,
        args.lai_mnc,
        args.lai_lac,
        args.smsc_address
    )

    try:
        # Start listening for MME
        logger.info("Starting SGsAP server...")
        smsc_service.listen()

        # Start SMSC service in background thread
        smsc_thread = threading.Thread(
            target=run_smsc_background,
            args=(smsc_service,),
            daemon=True
        )
        smsc_thread.start()

        # Start FastAPI with uvicorn
        logger.info(f"Starting REST API on {args.api_host}:{args.api_port}")
        logger.info("\nAPI Endpoints:")
        logger.info(f"  GET  http://{args.api_host}:{args.api_port}/health")
        logger.info(f"  GET  http://{args.api_host}:{args.api_port}/api/status")
        logger.info(f"  POST http://{args.api_host}:{args.api_port}/api/sms/send")
        logger.info(f"  POST http://{args.api_host}:{args.api_port}/api/sms/send/bulk")
        logger.info(f"  Docs: http://{args.api_host}:{args.api_port}/docs")

        uvicorn.run(app, host=args.api_host, port=args.api_port, log_level="info")

    except KeyboardInterrupt:
        logger.info("\nShutting down...")
    except Exception as e:
        logger.error(f"Service error: {e}")
        return 1
    finally:
        if smsc_service:
            smsc_service.running = False
            smsc_service.disconnect()

    return 0


if __name__ == '__main__':
    exit(main())
