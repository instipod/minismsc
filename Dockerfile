# Build stage: compile C-extension packages (uvloop, httptools)
FROM python:3.14-alpine AS builder

RUN apk add --no-cache \
    gcc \
    musl-dev \
    libffi-dev

WORKDIR /build
COPY requirements.txt .
RUN python -m venv /venv && \
    /venv/bin/pip install --no-cache-dir -r requirements.txt


# Runtime stage
FROM python:3.14-alpine

# No Alpine SCTP packages are needed — raw kernel SCTP sockets are used via
# Python's socket module directly (AF_INET, SOCK_STREAM, IPPROTO_SCTP=132).
# The host kernel must have the SCTP module loaded: modprobe sctp

COPY --from=builder /venv /venv

RUN addgroup -S minismsc && \
    adduser -S minismsc -G minismsc && \
    mkdir /data && \
    chown minismsc:minismsc /data

WORKDIR /app
COPY --chown=minismsc:minismsc . .

USER minismsc

ENV PATH="/venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    MINISMSC_DB_PATH=/data/sms_queue.sqlite

# /data holds the persistent SQLite SMS queue; mount a volume here in production
VOLUME /data

# 8000: FastAPI REST API
# 29118: SGsAP SCTP (MME connections)
EXPOSE 8000
EXPOSE 29118

CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]
