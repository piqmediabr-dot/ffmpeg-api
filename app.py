# app.py — FFmpeg API (Cloud Run) - concat demuxer + mix opcional de áudio
# ------------------------------------------------------------
# Endpoints:
#   GET  /            -> {"message":"FFmpeg API online","status":"ok"}
#   GET  /healthz     -> {"status":"ok"}
#   POST /concat_and_upload
#       Payload JSON:
#       {
#         "clips": [
#           { "source_url": "https://...", "ss": "0", "to": "5" },
#           { "url": "https://..." },            # "url" também é aceito
#           ...
#         ],
#         "resolution": "1080x1920",            # opcional (env DEFAULT_RESOLUTION)
#         "fps": 30,                             # opcional (env DEFAULT_FPS)
#         "video_bitrate": "4M",                 # opcional (env DEFAULT_VIDEO_BR)
#         "audio_bitrate": "192k",               # opcional (env DEFAULT_AUDIO_BR)
#         "audio_url": "https://.../bgm.mp3",    # opcional (BGM/narração)
#         "output_name": "final.mp4",            # opcional
#         "upload": false,                       # opcional (env UPLOAD_TO_DRIVE)
#         "drive_folder_id": "..."               # obrigatório se upload=true
#       }
#
# Observações:
# - Os clipes são normalizados SEM áudio (-an), evitando erro quando o input é mudo.
# - Concatenação via demuxer (arquivo inputs.txt) copiando streams de vídeo.
# - Se "audio_url" vier, faz replace/mix do áudio no final.
# - Upload ao Drive é opcional; esta função está como stub seguro por padrão.
# ------------------------------------------------------------

import os
import io
import uuid
import json
import shutil
import threading
import tempfile
import subprocess

from flask import Flask, request, jsonify
import requests

app = Flask(__name__)

# =========================
# Configurações (ENV)
# =========================
DEFAULT_RESOLUTION = os.getenv("DEFAULT_RESOLUTION", "1080x1920")
DEFAULT_FPS        = int(os.getenv("DEFAULT_FPS", "30"))
DEFAULT_VIDEO_BR   = os.getenv("DEFAULT_VIDEO_BR", "4M")
DEFAULT_AUDIO_BR   = os.getenv("DEFAULT_AUDIO_BR", "192k")
UPLOAD_TO_DRIVE    = os.getenv("UPLOAD_TO_DRIVE", "false").lower() == "true"
DRIVE_FOLDER_ID    = os.getenv("DRIVE_FOLDER_ID", "")  # usado somente se upload=true

# =========================
# Helpers
# =========================
def ffmpeg_exists() -> bool:
    """Verifica se o ffmpeg está no PATH do container."""
    from shutil import which
    return which("ffmpeg") is not None

def run(cmd: list[str]) -> None:
    """Executa comando e lança exceção com stderr amigável em caso de erro."""
    proc = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    if proc.returncode != 0:
        raise RuntimeError(f"FFmpeg/Proc error ({proc.returncode}):\n{proc.stderr}")

def download(url: str, to_path: str) -> None:
    """Baixa um arquivo por streaming (robusto para vídeos/áudio grandes)."""
    with requests.get(url, stream=True, timeout=60) as r:
        r.raise_for_status()
        with open(to_path, "wb") as f:
            for chunk in r.iter_content(chunk_size=1024 * 1024):
                if chunk:
                    f.write(chunk)

def upload_to_drive(local_path: str, name: str, folder_id: str) -> dict:
    """
    Stub seguro para upload ao Google Drive.
    Substitua por sua implementação real (google-api-python-client) quando quiser usar upload=true.
    Retorna um dicionário com chaves semelhantes às do Drive.
    """
    # Evita falhas enquanto você usa UPLOAD_TO_DRIVE=false
    return {
        "id": "fake-id",
        "name": name,
        "webViewLink": "https://drive.google.com/",
        "webContentLink": "https://drive.google.com/",
    }

# =========================
# Núcleo de vídeo/áudio
# =========================
def _baixar_videos_normalizar_sem_audio(clips: list[dict], tmpdir: str,
                                       resolution: str, fps: int,
                                       vbr: str, abr: str) -> list[str]:
    """
    Baixa e normaliza cada clipe APENAS VÍDEO (-an).
    Aceita "source_url" ou "url" em cada item.
    Retorna caminhos dos arquivos normalizados (mp4).
    """
    local_videos = []
    for i, clip in enumerate(clips):
        url = clip.get("source_url") or clip.get("url")
        if not url:
            raise ValueError(f"clip[{i}] sem 'source_url'/'url' no payload")

        local_path = os.path.join(tmpdir, f"clip_{i}.mp4")
        download(url, local_path)
        local_videos.append({"path": local_path, "ss": clip.get("ss"), "to": clip.get("to")})

    norm_paths = []
    for i, c in enumerate(local_videos):
        norm = os.path.join(tmpdir, f"norm_{i}.mp4")
        cmd = ["ffmpeg", "-y"]
        if c.get("ss"):
            cmd += ["-ss", str(c["ss"])]
        cmd += ["-i", c["path"]]
        if c.get("to"):
            cmd += ["-to", str(c["to"])]

        # Normalização VÍDEO-ONLY (sem áudio): scale+pad, fps, codec, bitrate, yuv420p
        cmd += [
            "-vf", f"scale={resolution}:force_original_aspect_ratio=decrease,"
                   f"pad={resolution}:(ow-iw)/2:(oh-ih)/2,setsar=1",
            "-r", str(fps),
            "-c:v", "libx264",
            "-b:v", vbr,
            "-pix_fmt", "yuv420p",
            "-an",  # <- remove qualquer áudio do clipe
            norm
        ]
        run(cmd)
        norm_paths.append(norm)

    return norm_paths

def _concat_video_apenas_por_demuxer(norm_paths: list[str], tmpdir: str, output_name: str) -> str:
    """
    Concatena os `norm_paths` via concat demuxer (copia stream de vídeo).
    Retorna caminho final (no tmpdir) com nome `output_name`.
    """
    list_file = os.path.join(tmpdir, "inputs.txt")
    with open(list_file, "w", encoding="utf-8") as f:
        for p in norm_paths:
            f.write(f"file '{p}'\n")

    concat_out = os.path.join(tmpdir, "concat.mp4")
    run([
        "ffmpeg", "-y",
        "-f", "concat", "-safe", "0",
        "-i", list_file,
        "-c", "copy",
        concat_out
    ])

    final_out = os.path.join(tmpdir, output_name if output_name.endswith(".mp4") else output_name + ".mp4")
    shutil.copyfile(concat_out, final_out)
    return final_out

def _mix_audio_se_houver(final_video_path: str, audio_url: str | None, tmpdir: str, abr: str) -> str:
    """
    Se houver audio_url, baixa e faz replace/mix: mantém vídeo do arquivo final e
    usa o áudio externo (BGM/narração). Retorna o caminho com áudio.
    """
    if not audio_url:
        return final_video_path

    local_audio = os.path.join(tmpdir, "audio_input")
    download(audio_url, local_audio)

    mixed_out = os.path.join(tmpdir, "mixed.mp4")
    run([
        "ffmpeg", "-y",
        "-i", final_video_path, "-i", local_audio,
        "-map", "0:v:0", "-map", "1:a:0",
        "-c:v", "copy",
        "-c:a", "aac", "-b:a", abr,
        "-shortest",
        mixed_out
    ])
    return mixed_out

def _run_concat_and_upload(data: dict) -> None:
    """
    Trabalho pesado em thread:
      1) normaliza clipes sem áudio
      2) concatena vídeo
      3) mixa áudio externo, se houver
      4) (opcional) faz upload ao Drive
    """
    tmpdir = None
    try:
        if not ffmpeg_exists():
            print("[worker] ffmpeg não encontrado no container", flush=True)
            return

        if not data or "clips" not in data or not data["clips"]:
            print("[worker] payload sem 'clips'", flush=True)
            return

        resolution   = data.get("resolution", DEFAULT_RESOLUTION)
        fps          = int(data.get("fps", DEFAULT_FPS))
        vbr          = data.get("video_bitrate", DEFAULT_VIDEO_BR)
        abr          = data.get("audio_bitrate", DEFAULT_AUDIO_BR)
        audio_url    = data.get("audio_url")
        upload_flag  = bool(data.get("upload", UPLOAD_TO_DRIVE))
        drive_folder = data.get("drive_folder_id", DRIVE_FOLDER_ID)
        output_name  = data.get("output_name", f"out-{uuid.uuid4().hex[:8]}.mp4")

        tmpdir = tempfile.mkdtemp(prefix="ffx_")

        # 1) Normalizar (vídeo-only)
        norm_paths = _baixar_videos_normalizar_sem_audio(
            clips=data["clips"],
            tmpdir=tmpdir,
            resolution=resolution,
            fps=fps,
            vbr=vbr,
            abr=abr,
        )

        # 2) Concat vídeo
        final_out = _concat_video_apenas_por_demuxer(norm_paths, tmpdir, output_name)

        # 3) Mix/replace áudio (opcional)
        final_with_audio = _mix_audio_se_houver(final_out, audio_url, tmpdir, abr)

        # 4) Upload (opcional)
        if upload_flag:
            if not drive_folder:
                print("[worker] upload=true mas sem drive_folder_id", flush=True)
                return
            info = upload_to_drive(final_with_audio, os.path.basename(final_with_audio), drive_folder)
            print("[worker] upload ok:", {
                "id": info.get("id"),
                "webViewLink": info.get("webViewLink"),
                "webContentLink": info.get("webContentLink"),
            }, flush=True)
        else:
            print("[worker] arquivo final pronto (sem upload):", final_with_audio, flush=True)

    except Exception as e:
        print(f"[worker] ERRO: {e}", flush=True)
    finally:
        if tmpdir:
            try:
                shutil.rmtree(tmpdir, ignore_errors=True)
            except Exception:
                pass

# =========================
# Rotas HTTP
# =========================
@app.get("/")
def root():
    return jsonify({"message": "FFmpeg API online", "status": "ok"}), 200

@app.get("/healthz")
def healthz():
    return jsonify({"status": "ok"}), 200

@app.post("/concat_and_upload")
def concat_and_upload():
    # ACK rápido (evita timeout do n8n); processamento em background
    data = request.get_json(force=True, silent=False)
    clips = (data or {}).get("clips") or []
    if not isinstance(clips, list) or not clips:
        return jsonify({"ok": False, "error": "clips vazio ou inválido"}), 400

    # Compat: aceita "source_url" OU "url" no backend
    # (a normalização já trata isso)
    job_id = uuid.uuid4().hex[:12]
    threading.Thread(target=_run_concat_and_upload, args=(data,), daemon=True).start()
    return jsonify({"status": "accepted", "job_id": job_id}), 202

# =========================
# Main (porta do Cloud Run)
# =========================
if __name__ == "__main__":
    port = int(os.environ.get("PORT", "8080"))
    # host 0.0.0.0 é obrigatório para Cloud Run
    app.run(host="0.0.0.0", port=port)

