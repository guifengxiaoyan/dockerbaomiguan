FROM python:3.11-slim

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
    ca-certificates \
    openssl \
    && rm -rf /var/lib/apt/lists/* \
    && update-ca-certificates \
    && mkdir -p /app/data

COPY requirements.txt .

RUN pip install --no-cache-dir -r requirements.txt \
    && pip install --no-cache-dir --upgrade certifi urllib3

COPY *.py .

VOLUME ["/app/data"]

ENV CREDENTIALS_FILE=/app/data/credentials.json
ENV REQUESTS_CA_BUNDLE=/etc/ssl/certs/ca-certificates.crt
ENV SSL_CERT_FILE=/etc/ssl/certs/ca-certificates.crt

# Web 模式 (默认)
EXPOSE 8765
ENTRYPOINT ["python", "app.py"]
