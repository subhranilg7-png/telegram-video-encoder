FROM python:3.9-slim

RUN apt-get update && apt-get install -y \
    ffmpeg \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY telegram_encoder_bot.py .

RUN mkdir -p /tmp/encodes

CMD ["python", "telegram_encoder_bot.py"]