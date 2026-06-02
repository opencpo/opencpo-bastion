FROM python:3.12-slim AS builder

WORKDIR /build
COPY requirements.txt .
RUN pip install --no-cache-dir --prefix=/install -r requirements.txt || \
    pip install --no-cache-dir --prefix=/install \
        websockets httpx uvicorn fastapi pyyaml cryptography prometheus_client


FROM python:3.12-slim

WORKDIR /app

# Raspberry Pi GPIO support (conditional — no-op on non-Pi)
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    && rm -rf /var/lib/apt/lists/*

COPY --from=builder /install /usr/local
COPY . .

EXPOSE 7443 8080

ENV HOST=0.0.0.0

# Default: start the tap proxy (override per deployment)
CMD ["python", "-u", "-m", "gateway.tap"]
