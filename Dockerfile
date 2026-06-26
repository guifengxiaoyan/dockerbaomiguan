FROM python:3.11-slim

WORKDIR /app

COPY requirements.txt .

RUN pip install --no-cache-dir -r requirements.txt

COPY *.py .

VOLUME ["/app/data"]

ENV CREDENTIALS_FILE=/app/data/credentials.json

# Web 模式 (默认)
EXPOSE 8765
ENTRYPOINT ["python", "app.py"]

# CLI 交互模式: 覆盖 entrypoint
# docker run -it --rm -v ./data:/app/data --entrypoint python autobaomiguan main.py
