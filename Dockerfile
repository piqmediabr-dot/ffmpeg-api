# Substituir o Dockerfile por um que usa venv (resolve PEP 668)
cat > Dockerfile <<'DOCKER'
FROM python:3.11-slim

ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

# Instalar ffmpeg e deps
RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg ca-certificates && \
    rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Criar venv e ativar no PATH
RUN python -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

# Instalar dependências no venv (sem tocar no sistema)
COPY requirements.txt .
RUN pip install --upgrade pip && \
    pip install --no-cache-dir -r requirements.txt

# Copiar a aplicação
COPY app.py .

EXPOSE 8080
CMD ["python", "app.py"]
DOCKER

# Build da imagem (usa bucket fixo + sua SA)
gcloud builds submit \
  --region us-central1 \
  --gcs-source-staging-dir="gs://cb-src-piqmediaautomation-us-central1-fixed/source" \
  --gcs-log-dir="gs://cb-src-piqmediaautomation-us-central1-fixed/logs" \
  --service-account="projects/piqmediaautomation/serviceAccounts/ffmpeg-api-sa@piqmediaautomation.iam.gserviceaccount.com" \
  --tag us-central1-docker.pkg.dev/piqmediaautomation/cloud-run-source-deploy/ffmpeg-api:latest .

# Deploy
gcloud run deploy ffmpeg-api \
  --image us-central1-docker.pkg.dev/piqmediaautomation/cloud-run-source-deploy/ffmpeg-api:latest \
  --region us-central1 \
  --allow-unauthenticated \
  --service-account "ffmpeg-api-sa@piqmediaautomation.iam.gserviceaccount.com" \
  --set-env-vars DEFAULT_RESOLUTION=1080x1920,DEFAULT_FPS=30,DEFAULT_VIDEO_BR=4M,DEFAULT_AUDIO_BR=192k,UPLOAD_TO_DRIVE=false \
  --max-instances=3 \
  --cpu=2 --memory=2Gi
