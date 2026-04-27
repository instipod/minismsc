FROM python:3.14-alpine

RUN apk update && apk add build-base lksctp-tools-dev curl

COPY requirements.txt /tmp/requirements.txt
RUN pip3 install -r /tmp/requirements.txt && rm /tmp/requirements.txt

COPY *.py /usr/local/bin/

HEALTHCHECK --interval=30s --timeout=5s --start-period=5s --retries=3 CMD curl -f http://localhost:8080/health | grep -q "\"status\":\"healthy\"" || exit 1
CMD ["python3", "/usr/local/bin/api.py"]