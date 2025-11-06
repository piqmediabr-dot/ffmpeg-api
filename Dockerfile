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
COPY app.py .

# Porta padrão do Cloud Run
ENV PORT=8080

# Usa shell pra interpolar ${PORT}
CMD ["bash","-lc","exec gunicorn -w 1 -k gthread --threads 8 --timeout 0 -b 0.0.0.0:${PORT} app:app"]


