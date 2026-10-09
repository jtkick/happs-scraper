# Crawler pool (tools/worker.py): `docker compose up -d --build`, see compose.yaml.
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PLAYWRIGHT_ENABLED=true

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt scrapy-playwright \
 && playwright install --with-deps chromium \
 && rm -rf /var/lib/apt/lists/*

COPY scrapy.cfg ./
COPY scraper ./scraper
COPY tools ./tools

CMD ["python", "tools/worker.py"]
