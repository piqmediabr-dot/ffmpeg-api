FROM python:3.11-slim

# Instala ffmpeg e curl do Debian (sem conflitos com o pip do Python oficial)
RUN apt-get update \
 && apt-get install -y --no-install-recommends ffmpeg curl \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Copia requirements e instala no ambiente Python da imagem (aqui não tem PEP 668)
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copia o app
COPY app.py .

EXPOSE 8080
CMD ["gunicorn", "-b", ":$PORT", "app:app"]
