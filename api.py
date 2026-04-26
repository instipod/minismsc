#!/usr/bin/env python3
"""
REST API for Mini SMSC
Provides HTTP endpoints to send SMS via Open5GS
"""

import os
import logging
import threading
from flask import Flask, request, jsonify
from smsc import SMSCService

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s'
)
logger = logging.getLogger(__name__)

app = Flask(__name__)
smsc_service: SMSCService = None


@app.route('/health', methods=['GET'])
def health():
    """Health check endpoint"""
    return jsonify({
        'status': 'healthy',
        'service': 'mini-smsc',
        'connected': smsc_service.connected if smsc_service else False
    }), 200


@app.route('/api/status', methods=['GET'])
def status():
    """Get SMSC service status"""
    if not smsc_service:
        return jsonify({'error': 'SMSC service not initialized'}), 503

    return jsonify({
        'connected': smsc_service.connected,
        'mme_address': smsc_service.mme_address,
        'mme_port': smsc_service.mme_port,
        'vlr_name': smsc_service.vlr_name,
        'queue_length': len(smsc_service.message_queue)
    }), 200


@app.route('/api/sms/send', methods=['POST'])
def send_sms():
    """
    Send SMS to a subscriber

    Request body:
    {
        "imsi": "001010000000001",
        "msisdn": "+1234567890",
        "sender": "+9999",
        "text": "Hello from SMSC!"
    }

    Optional fields:
    - msisdn: defaults to imsi if not provided
    - sender: defaults to "SMSC" if not provided
    """
    if not smsc_service:
        return jsonify({'error': 'SMSC service not initialized'}), 503

    if not smsc_service.connected:
        return jsonify({'error': 'SMSC not connected to MME'}), 503

    # Parse request
    data = request.get_json()
    if not data:
        return jsonify({'error': 'Invalid JSON'}), 400

    # Validate required fields
    imsi = data.get('imsi')
    text = data.get('text')

    if not imsi:
        return jsonify({'error': 'Missing required field: imsi'}), 400
    if not text:
        return jsonify({'error': 'Missing required field: text'}), 400

    # Optional fields
    msisdn = data.get('msisdn', imsi)  # Default to IMSI if not provided
    sender = data.get('sender', 'SMSC')  # Default sender

    # Validate IMSI format (should be digits, 14-15 length)
    if not imsi.isdigit() or len(imsi) < 14 or len(imsi) > 15:
        return jsonify({'error': 'Invalid IMSI format'}), 400

    # Validate text length (160 chars for GSM7, 70 for UCS2)
    if len(text) > 160:
        return jsonify({'error': 'Text too long (max 160 characters)'}), 400

    try:
        # Queue SMS for sending
        smsc_service.send_sms(
            imsi=imsi,
            msisdn=msisdn,
            sender=sender,
            text=text
        )

        logger.info(f"API: SMS queued for {imsi}")

        return jsonify({
            'status': 'queued',
            'message': 'SMS queued for delivery',
            'details': {
                'imsi': imsi,
                'msisdn': msisdn,
                'sender': sender,
                'text_length': len(text)
            }
        }), 202  # 202 Accepted

    except Exception as e:
        logger.error(f"API: Error queuing SMS: {e}")
        return jsonify({'error': str(e)}), 500


@app.route('/api/sms/send/bulk', methods=['POST'])
def send_bulk_sms():
    """
    Send SMS to multiple subscribers

    Request body:
    {
        "messages": [
            {
                "imsi": "001010000000001",
                "msisdn": "+1234567890",
                "sender": "+9999",
                "text": "Hello!"
            },
            ...
        ]
    }
    """
    if not smsc_service:
        return jsonify({'error': 'SMSC service not initialized'}), 503

    if not smsc_service.connected:
        return jsonify({'error': 'SMSC not connected to MME'}), 503

    data = request.get_json()
    if not data or 'messages' not in data:
        return jsonify({'error': 'Missing messages array'}), 400

    messages = data['messages']
    if not isinstance(messages, list):
        return jsonify({'error': 'messages must be an array'}), 400

    if len(messages) > 100:
        return jsonify({'error': 'Maximum 100 messages per bulk request'}), 400

    results = {
        'queued': 0,
        'failed': 0,
        'errors': []
    }

    for idx, msg in enumerate(messages):
        imsi = msg.get('imsi')
        text = msg.get('text')

        if not imsi or not text:
            results['failed'] += 1
            results['errors'].append({
                'index': idx,
                'error': 'Missing imsi or text'
            })
            continue

        msisdn = msg.get('msisdn', imsi)
        sender = msg.get('sender', 'SMSC')

        try:
            smsc_service.send_sms(imsi, msisdn, sender, text)
            results['queued'] += 1
        except Exception as e:
            results['failed'] += 1
            results['errors'].append({
                'index': idx,
                'imsi': imsi,
                'error': str(e)
            })

    return jsonify(results), 202


def run_smsc_background(smsc: SMSCService):
    """Run SMSC service in background thread"""
    try:
        smsc.run()
    except Exception as e:
        logger.error(f"SMSC service error: {e}")


def main():
    """Main entry point"""
    import argparse

    parser = argparse.ArgumentParser(description='Mini SMSC REST API')
    parser.add_argument('--mme-address',
                       default=os.getenv('MME_ADDRESS', '127.0.0.1'),
                       help='MME IP address (env: MME_ADDRESS, default: 127.0.0.1)')
    parser.add_argument('--mme-port', type=int,
                       default=int(os.getenv('MME_PORT', '29118')),
                       help='MME SGs port (env: MME_PORT, default: 29118)')
    parser.add_argument('--vlr-name',
                       default=os.getenv('VLR_NAME', 'vlr.open5gs.org'),
                       help='VLR/MSC FQDN (env: VLR_NAME, default: vlr.open5gs.org)')
    parser.add_argument('--api-host',
                       default=os.getenv('API_HOST', '0.0.0.0'),
                       help='API host (env: API_HOST, default: 0.0.0.0)')
    parser.add_argument('--api-port', type=int,
                       default=int(os.getenv('API_PORT', '8080')),
                       help='API port (env: API_PORT, default: 8080)')

    args = parser.parse_args()

    # Create SMSC service
    global smsc_service
    smsc_service = SMSCService(args.mme_address, args.mme_port, args.vlr_name)

    try:
        # Connect to MME
        logger.info("Connecting to MME...")
        smsc_service.connect()

        # Start SMSC service in background thread
        smsc_thread = threading.Thread(
            target=run_smsc_background,
            args=(smsc_service,),
            daemon=True
        )
        smsc_thread.start()

        # Start Flask API
        logger.info(f"Starting REST API on {args.api_host}:{args.api_port}")
        logger.info("\nAPI Endpoints:")
        logger.info(f"  GET  http://{args.api_host}:{args.api_port}/health")
        logger.info(f"  GET  http://{args.api_host}:{args.api_port}/api/status")
        logger.info(f"  POST http://{args.api_host}:{args.api_port}/api/sms/send")
        logger.info(f"  POST http://{args.api_host}:{args.api_port}/api/sms/send/bulk")

        app.run(host=args.api_host, port=args.api_port, debug=False)

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
