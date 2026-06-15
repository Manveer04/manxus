FROM python:3.11-slim

WORKDIR /app

RUN apt-get update && apt-get install -y \
    wget curl gnupg ca-certificates \
    fonts-liberation libatk-bridge2.0-0 libatk1.0-0 \
    libcairo2 libcups2 libdbus-1-3 libdrm2 libgbm1 \
    libglib2.0-0 libgtk-3-0 libnspr4 libnss3 \
    libpango-1.0-0 libx11-6 libxcb1 libxcomposite1 \
    libxdamage1 libxext6 libxfixes3 libxrandr2 \
    libxshmfence1 xdg-utils libasound2 \
    xvfb x11vnc novnc \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt websockify

RUN playwright install chromium

COPY app/ ./app/
COPY shopee_tracker.py ./
COPY urls.txt ./
COPY shopee_cookies.json ./
COPY api.py ./
COPY start.sh .
RUN chmod +x start.sh

RUN mkdir -p /app/sessions /app/data /app/db

EXPOSE 8000 6080
CMD ["./start.sh"]
