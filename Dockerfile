FROM python:3.11-slim

# Evita prompts do apt
ENV DEBIAN_FRONTEND=noninteractive

# Atualiza e instala ffmpeg + dependências mínimas
RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg \
    ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# App
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app.py .

# Healthcheck simples (opcional)
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
  CMD python -c "import requests; import os; import sys; \
  import urllib.request as u; \
  import json; \
  u.urlopen('http://localhost:8080/health', timeout=2).read(); print('ok')" || exit 1

ENV PYTHONUNBUFFERED=1
ENV PORT=8080

CMD ["python", "app.py"]
