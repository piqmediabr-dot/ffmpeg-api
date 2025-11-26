# ===== base =====
FROM python:3.11-slim

# Evita prompts no apt-get
ENV DEBIAN_FRONTEND=noninteractive

# Instala ffmpeg e utilitários básicos
RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg ca-certificates curl \
 && rm -rf /var/lib/apt/lists/*

# ===== app =====
WORKDIR /app

# Virtualenv (opcional, bom para isolar deps)
RUN python -m venv /opt/venv
ENV PATH="/opt/venv/bin:${PATH}"

# Copia requirements e instala
COPY requirements.txt .
RUN pip install --upgrade pip setuptools wheel \
 && pip install --no-cache-dir -r requirements.txt

# Copia o app
COPY app.py .

# Porta padrão do Cloud Run
ENV PORT=8080

# Timeout do gunicorn (pode ser sobrescrito por env no deploy)
ENV GUNICORN_CMD_ARGS="--timeout 180 --workers 1 --threads 1 --bind 0.0.0.0:8080"

# Healthcheck opcional (não bloqueia o deploy, é só para referência local)
# HEALTHCHECK --interval=30s --timeout=5s --retries=3 CMD curl -fsS http://localhost:8080/healthz || exit 1

# Comando de entrada: gunicorn servindo o Flask "app:app"
CMD ["sh", "-c", "exec gunicorn app:app"]



