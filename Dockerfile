FROM python:3.14-alpine

RUN apk update && apk add build-base lksctp-tools-dev

COPY requirements.txt /tmp/requirements.txt
RUN pip3 install -r /tmp/requirements.txt && rm /tmp/requirements.txt

COPY *.py /usr/local/bin/
CMD ["python3", "/usr/local/bin/api.py"]