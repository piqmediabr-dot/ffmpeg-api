# ==============================
# BASE IMAGE
# ==============================
FROM python:3.11-slim

WORKDIR /app

# ==============================
# ENVIRONMENT CONFIG
# ==============================
ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PORT=8080

# ==============================
# DEPENDÊNCIAS DO SISTEMA
# ==============================
# ffmpeg -> concatenação de vídeos
# ffprobe -> cálculo de duração
RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg \
    && rm -rf /var/lib/apt/lists/*

# ==============================
# DEPENDÊNCIAS PYTHON
# ==============================
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# ==============================
# CÓDIGO DA APLICAÇÃO
# ==============================
COPY app.py .

# ==============================
# EXECUÇÃO
# ==============================
# Gunicorn é usado para produção no Cloud Run
CMD exec gunicorn --bind :$PORT --workers 1 --threads 8 --timeout 0 app:app
