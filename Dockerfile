# 1) Substituir o Dockerfile por este conteúdo (Python 3.11 + ffmpeg)
cat > Dockerfile <<'DOCKER'
FROM python:3.11-slim

ENV DEBIAN_FRONTEND=noninteractive \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

# FFmpeg e deps mínimas
RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg \
    ca-certificates \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Instalar deps primeiro (cache de layer)
COPY requirements.txt .
RUN python -m pip install --no-cache-dir -r requirements.txt

# Copiar a app
COPY app.py .

EXPOSE 8080
CMD ["python", "app.py"]
DOCKER

# 2) Build (usa bucket fixo e a sua SA do serviço)
gcloud builds submit \
  --region us-central1 \
  --gcs-source-staging-dir="gs://cb-src-piqmediaautomation-us-central1-fixed/source" \
  --gcs-log-dir="gs://cb-src-piqmediaautomation-us-central1-fixed/logs" \
  --service-account="projects/piqmediaautomation/serviceAccounts/ffmpeg-api-sa@piqmediaautomation.iam.gserviceaccount.com" \
  --tag us-central1-docker.pkg.dev/piqmediaautomation/cloud-run-source-deploy/ffmpeg-api:latest .

# 3) Deploy (mesma imagem)
gcloud run deploy ffmpeg-api \
  --image us-central1-docker.pkg.dev/piqmediaautomation/cloud-run-source-deploy/ffmpeg-api:latest \
  --region us-central1 \
  --allow-unauthenticated \
  --service-account "ffmpeg-api-sa@piqmediaautomation.iam.gserviceaccount.com" \
  --set-env-vars DEFAULT_RESOLUTION=1080x1920,DEFAULT_FPS=30,DEFAULT_VIDEO_BR=4M,DEFAULT_AUDIO_BR=192k,UPLOAD_TO_DRIVE=false \
  --max-instances=3 \
  --cpu=2 --memory=2Gi

# 4) Teste rápido do endpoint novo
SERVICE_URL="$(gcloud run services describe ffmpeg-api --region us-central1 --format='value(status.url)')"
echo "$SERVICE_URL"
/usr/bin/curl -s -X POST "${SERVICE_URL}/concat_and_upload" \
  -H "Content-Type: application/json" \
  -d '{
    "clips": [
      {"url": "https://filesamples.com/samples/video/mp4/sample_640x360.mp4"},
      {"url": "https://filesamples.com/samples/video/mp4/sample_640x360.mp4"}
    ],
    "output_name": "teste_concat.mp4",
    "upload": true,
    "drive_folder_id": "1Y7PsDfg3UnbhffiaS3hNOSGtQD_il6Xg"
  }'
