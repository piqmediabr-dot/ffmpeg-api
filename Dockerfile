# Base
FROM python:3.11-slim

# Evita ruído na instalação
ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

# ffmpeg + certificados
RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg ca-certificates curl && \
    rm -rf /var/lib/apt/lists/*

# App
WORKDIR /app

# venv dedicado
RUN python -m venv /opt/venv
ENV PATH="/opt/venv/bin:${PATH}"

# Dependências
COPY requirements.txt ./
RUN pip install --upgrade pip setuptools wheel && \
    pip install --no-cache-dir -r requirements.txt && \
    pip install --no-cache-dir gunicorn

# Código
COPY . .

# Porta padrão do Cloud Run (será sobrescrita pela plataforma)
ENV PORT=8080

# Servidor web (gunicorn) — 1 worker gthread, 8 threads, sem timeout hard
# Usamos shell para interpolar ${PORT}
CMD ["bash","-lc","exec gunicorn -w 1 -k gthread --threads 8 --timeout 0 -b 0.0.0.0:${PORT} app:app"]

