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
    pip install --no-cache-dir -r requirements.txt

# Código
COPY . .

# Porta
ENV PORT=8080

# Servidor web (gunicorn) chamando app:app
# 2 workers, 4 threads, sem timeout hard (deixa a thread background trabalhar)
CMD ["gunicorn", "-w", "1", "-b", ":8080", "app:app"]

