# app.py
from flask import Flask, request, jsonify
import os
import uuid
import shutil
import tempfile
import subprocess
from datetime import timedelta
from typing import List, Optional

import requests
from google.cloud import storage
from google.api_core import exceptions as gcloud_exceptions

app = Flask(__name__)
# Aceitar rotas com e sem barra final
app.url_map.strict_slashes = False

# CORS básico
@app.after_request
def add_cors(resp):
    resp.headers["Access-Control-Allow-Origin"] = "*"
    resp.headers["Access-Control-Allow-Headers"] = "Content-Type, Authorization"
    resp.headers["Access-Control-Allow-Methods"] = "POST, OPTIONS, GET"
    return resp

# ---------- Utilidades ----------
def _json_error(code: int, msg: str):
    return jsonify({"status": "error", "detail": msg}), code

def _ensure_ffmpeg():
    try:
        subprocess.run(["ffmpeg", "-version"], stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=True)
    except Exception:
        raise RuntimeError("FFmpeg não encontrado no container. Instale-o no Dockerfile (apt-get install -y ffmpeg).")

def _download_to_tmp(url: str, tmpdir: str) -> str:
    local = os.path.join(tmpdir, f"in_{uuid.uuid4().hex}.mp4")
    try:
        with requests.get(url, stream=True, timeout=120) as r:
            r.raise_for_status()
            with open(local, "wb") as f:
                for chunk in r.iter_content(chunk_size=1024 * 1024):
                    if chunk:
                        f.write(chunk)
        return local
    except Exception as e:
        raise RuntimeError(f"Falha ao baixar {url}: {e}")

def _make_list_file(paths: List[str], tmpdir: str) -> str:
    lst = os.path.join(tmpdir, "concat.txt")
    with open(lst, "w", encoding="utf-8") as f:
        for p in paths:
            f.write(f"file '{p}'\n")
    return lst

def _run_ffmpeg(cmd: List[str]):
    proc = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    if proc.returncode != 0:
        err = (proc.stderr or b"").decode(errors="ignore")
        raise RuntimeError(f"FFmpeg falhou: {err[:1000]}")

def _upload_to_gcs(local_file: str, bucket_name: str, object_name: str) -> str:
    client = storage.Client()
    bucket = client.bucket(bucket_name)
    blob = bucket.blob(object_name)
    blob.upload_from_filename(local_file, content_type="video/mp4")
    return f"gs://{bucket_name}/{object_name}"

def _signed_url(bucket_name: str, object_name: str, minutes: int) -> Optional[str]:
    try:
        client = storage.Client()
        blob = client.bucket(bucket_name).blob(object_name)
        return blob.generate_signed_url(
            version="v4",
            expiration=timedelta(minutes=max(1, minutes)),
            method="GET",
            response_disposition=f'attachment; filename="{os.path.basename(object_name)}"'
        )
    except gcloud_exceptions.GoogleAPICallError:
        return None
    except Exception:
        return None

# ---------- Rotas básicas ----------
@app.route("/", methods=["GET"])
def index():
    return jsonify({
        "status": "ok",
        "message": "FFmpeg API online e respondendo!",
        "endpoints": ["/health", "/concat_and_upload"]
    }), 200

@app.route("/health", methods=["GET"])
def health():
    return jsonify({"ok": True}), 200

# Mantido para compatibilidade (placeholder)
@app.route("/process", methods=["POST"])
def process():
    data = request.get_json(force=True, silent=True) or {}
    return jsonify({"received": data, "status": "processing_soon"}), 200

# ---------- Endpoint principal ----------
@app.route("/concat_and_upload", methods=["POST", "OPTIONS"])
def concat_and_upload():
    # Preflight CORS
    if request.method == "OPTIONS":
        return ("", 204)

    """
    Body JSON:
    {
      "input_videos": ["https://...mp4", "https://...mp4"],
      "bucket_name": "meu-bucket",
      "output_filename": "final.mp4",           # opcional
      "concat_mode": "reencode" | "copy",       # opcional (default reencode)
      "signed_url_expiration_minutes": 60       # opcional
    }
    """
    body = request.get_json(force=True, silent=True)
    if not body:
        return _json_error(400, "JSON inválido ou ausente.")

    input_videos = body.get("input_videos") or []
    bucket_name = body.get("bucket_name")
    output_filename = (body.get("output_filename") or "").strip()
    concat_mode = (body.get("concat_mode") or "reencode").lower()
    exp_minutes = int(body.get("signed_url_expiration_minutes") or 60)

    if not isinstance(input_videos, list) or len(input_videos) < 2:
        return _json_error(400, "Envie pelo menos 2 URLs em 'input_videos'.")
    if not bucket_name:
        return _json_error(400, "Campo 'bucket_name' é obrigatório.")

    if not output_filename:
        output_filename = f"concat_{uuid.uuid4().hex}.mp4"
    if not output_filename.lower().endswith(".mp4"):
        output_filename += ".mp4"

    try:
        _ensure_ffmpeg()
    except RuntimeError as e:
        return _json_error(500, str(e))

    tmpdir = tempfile.mkdtemp(prefix="ffmpeg_concat_")
    local_inputs = []
    output_local = os.path.join(tmpdir, f"out_{uuid.uuid4().hex}.mp4")

    try:
        # 1) Download
        for url in input_videos:
            local_inputs.append(_download_to_tmp(str(url), tmpdir))

        # 2) Lista para concat
        list_file = _make_list_file(local_inputs, tmpdir)

        # 3) Comando FFmpeg
        if concat_mode == "copy":
            cmd = [
                "ffmpeg", "-y",
                "-f", "concat", "-safe", "0",
                "-i", list_file,
                "-c", "copy",
                output_local
            ]
        else:
            cmd = [
                "ffmpeg", "-y",
                "-f", "concat", "-safe", "0",
                "-i", list_file,
                "-c:v", "libx264", "-preset", "veryfast", "-crf", "23",
                "-c:a", "aac", "-b:a", "128k",
                "-movflags", "+faststart",
                output_local
            ]

        # 4) Executar
        _run_ffmpeg(cmd)

        # 5) Upload GCS
        gs_uri = _upload_to_gcs(output_local, bucket_name, output_filename)

        # 6) URL assinada
        signed = _signed_url(bucket_name, output_filename, exp_minutes)

        return jsonify({
            "status": "ok",
            "output_gs_uri": gs_uri,
            "output_signed_url": signed,
            "details": {
                "mode": concat_mode,
                "inputs": len(local_inputs)
            }
        }), 200

    except Exception as e:
        return _json_error(500, f"Falha no processamento: {e}")

    finally:
        try:
            shutil.rmtree(tmpdir, ignore_errors=True)
        except Exception:
            pass

# ---------- Execução ----------
if __name__ == "__main__":
    port = int(os.environ.get("PORT", "8080"))
    app.run(host="0.0.0.0", port=port)
