# Dockerfile — FFmpeg API
FROM python:3.11-slim

# Sistema + ffmpeg
RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg ca-certificates curl \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Virtualenv (opcional) e deps Python
RUN python -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

COPY requirements.txt .
RUN pip install --upgrade pip setuptools wheel \
 && pip install --no-cache-dir -r requirements.txt

# App
COPY app.py .

# Porta do Cloud Run
ENV PORT=8080
EXPOSE 8080

# Gunicorn; pode ser sobrescrito por GUNICORN_CMD_ARGS
CMD ["bash", "-lc", "exec gunicorn ${GUNICORN_CMD_ARGS:-'-w 1 --threads 1 --timeout 1200 --bind 0.0.0.0:8080'} app:app"]


