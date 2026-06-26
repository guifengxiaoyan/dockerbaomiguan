FROM python:3.11-slim

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
    ca-certificates \
    openssl \
    && rm -rf /var/lib/apt/lists/* \
    && update-ca-certificates

COPY requirements.txt .

RUN pip install --no-cache-dir -r requirements.txt \
    && pip install --no-cache-dir --upgrade certifi

COPY *.py .

VOLUME ["/app/data"]

ENV CREDENTIALS_FILE=/app/data/credentials.json

# Web 模式 (默认)
EXPOSE 8765
ENTRYPOINT ["python", "app.py"]

# CLI 交互模式: 覆盖 entrypoint
# docker run -it --rm -v ./data:/app/data --entrypoint python autobaomiguan main.py
