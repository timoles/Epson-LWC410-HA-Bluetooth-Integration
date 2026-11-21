# Self-contained: ignores the Supervisor's BUILD_FROM so the base stays pinned.
FROM python:3.12-alpine@sha256:c5ba3deef00c7758561a0f03c26f44f10589263d40e732b8748e0d1f38f6dd89

# bluez: bluetoothctl (adapter power on, scan); font-dejavu: label font
RUN apk add --no-cache bluez font-dejavu

COPY requirements.txt /tmp/requirements.txt
RUN pip install --no-cache-dir --require-hashes --only-binary=:all: -r /tmp/requirements.txt \
    && rm /tmp/requirements.txt

COPY app/ /app/
WORKDIR /app
CMD ["python3", "-u", "/app/server.py"]
