# app.py — FFmpeg API (Cloud Run) - concat vídeo-only + mix opcional de áudio
# -----------------------------------------------------------------------------
# Endpoints:
#   GET  /            -> {"message":"FFmpeg API online","status":"ok"}
#   GET  /healthz     -> {"status":"ok"}
#   POST /concat_and_upload[?sync=1]
#       Payload JSON:
#       {
#         "clips": [
#           { "source_url": "https://...", "ss": "0", "to": "5" },
#           { "url": "https://..." }  # aceita "url" também
#         ],
#         "resolution": "1080x1920",  # opcional (env DEFAULT_RESOLUTION)
#         "fps": 30,                  # opcional (env DEFAULT_FPS)
#         "video_bitrate": "4M",      # opcional (env DEFAULT_VIDEO_BR)
#         "audio_bitrate": "192k",    # opcional (env DEFAULT_AUDIO_BR)
#         "audio_url": "https://.../bgm_or_voice.mp3",  # opcional
#         "audio_gain": 0.2,          # opcional (0.0–1.0 típico; até 5.0 permitido)
#         "output_name": "final.mp4", # opcional
#         "upload": false,            # opcional (env UPLOAD_TO_DRIVE)
#         "drive_folder_id": "..."    # obrigatório se upload=true
#       }
#
# Observações:
# - Clipes são re-encodados padronizando resolução/FPS/codec e **sem áudio** (-an).
# - Concatenação usa demuxer (inputs.txt) com "-c copy".
# - Se "audio_url" vier, faz replace/mix do áudio no final com ganho.
# - Upload ao Drive é **stub** (retorna links fake) — substitua quando quiser usar.
# - Para teste rápido, chame POST /concat_and_upload?sync=1 (bloqueante).
# -----------------------------------------------------------------------------

import os
import uuid
import json
import shutil
import threading
import tempfile
import subprocess
from typing import List, Dict, Optional

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
# Utilitários
# =========================
def log(msg: str, **extra):
    """Log simples em stdout (Cloud Run mostra em Logs)."""
    if extra:
        try:
            msg = f"{msg} | {json.dumps(extra, ensure_ascii=False)}"
        except Exception:
            pass
    print(msg, flush=True)

def ffmpeg_exists() -> bool:
    """Verifica se o ffmpeg está no PATH do container."""
    from shutil import which
    return which("ffmpeg") is not None

def run(cmd: List[str]) -> None:
    """Executa comando e lança exceção com stderr amigável em caso de erro."""
    log("RUN", cmd=" ".join(cmd))
    proc = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    if proc.returncode != 0:
        raise RuntimeError(f"FFmpeg/Proc error ({proc.returncode}):\n{proc.stderr}")

def download(url: str, to_path: str) -> None:
    """Baixa um arquivo por streaming (robusto para vídeos/áudio grandes)."""
    log("DOWNLOAD", url=url, to=to_path)
    with requests.get(url, stream=True, timeout=60) as r:
        r.raise_for_status()
        with open(to_path, "wb") as f:
            for chunk in r.iterate_content(1024 * 1024):
                if chunk:
                    f.write(chunk)

def upload_to_drive(local_path: str, name: str, folder_id: str) -> dict:
    """
    Stub seguro para upload ao Google Drive.
    Substitua por sua implementação real quando quiser usar upload=true.
    """
    log("UPLOAD_STUB", file=name, folder_id=folder_id, path=local_path)
    return {
        "id": "fake-id",
        "name": name,
        "webViewLink": "https://drive.google.com/",
        "webContentLink": "https://drive.google.com/",
    }

# =========================
# Núcleo de vídeo/áudio
# =========================
def _baixar_videos_normalizar_sem_audio(
    clips: List[Dict], tmpdir: str, resolution: str, fps: int, vbr: str
) -> List[str]:
    """
    Baixa e normaliza cada clipe APENAS VÍDEO (-an).
    Aceita "source_url" ou "url" em cada item.
    Retorna caminhos dos arquivos normalizados (mp4).
    """
    if not clips:
        raise ValueError("payload sem 'clips'")

    local_videos = []
    for i, clip in enumerate(clips):
        url = clip.get("source_url") or clip.get("url")
        if not url:
            raise ValueError(f"clip[{i}] sem 'source_url'/'url' no payload")

        src_path = os.path.join(tmpdir, f"in_{i}.mp4")
        download(url, src_path)
        local_videos.append({"path": src_path, "ss": clip.get("ss"), "to": clip.get("to")})

    norm_paths = []
    for i, c in enumerate(local_videos):
        norm = os.path.join(tmpdir, f"norm_{i}.mp4")
        cmd = ["ffmpeg", "-y"]
        if c.get("ss") not in (None, ""):
            cmd += ["-ss", str(c["ss"])]
        cmd += ["-i", c["path"]]
        if c.get("to") not in (None, ""):
            cmd += ["-to", str(c["to"])]

        # Normalização VÍDEO-ONLY (sem áudio)
        # scale+pad para 9:16 com letterbox quando necessário
        cmd += [
            "-vf", f"scale={resolution}:force_original_aspect_ratio=decrease,"
                   f"pad={resolution}:(ow-iw)/2:(oh-ih)/2,setsar=1",
            "-r", str(fps),
            "-c:v", "libx264",
            "-b:v", vbr,
            "-pix_fmt", "yuv420p",
            "-an",
            norm
        ]
        run(cmd)
        norm_paths.append(norm)

    return norm_paths

def _concat_por_demuxer(norm_paths: List[str], tmpdir: str, output_name: str) -> str:
    """
    Concatena os `norm_paths` via concat demuxer (copia stream de vídeo).
    Retorna caminho final (no tmpdir) com nome `output_name`.
    """
    if not norm_paths:
        raise ValueError("nenhum arquivo normalizado para concatenar")

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

def _mix_audio_se_houver(
    final_video_path: str,
    audio_url: Optional[str],
    tmpdir: str,
    abr: str,
    audio_gain: float
) -> str:
    """
    Se houver audio_url, baixa e faz replace: mantém vídeo e usa o áudio externo
    (BGM/narração) com ganho (0.0–1.0 comum; até 5.0 permitido). Retorna o caminho com áudio.
    """
    if not audio_url:
        return final_video_path

    local_audio = os.path.join(tmpdir, "audio_input")
    download(audio_url, local_audio)

    mixed_out = os.path.join(tmpdir, "mixed.mp4")
    # volume=audio_gain  -> 0.0 (mudo) a 1.0 (volume original). Ex.: 0.2 = 20%
    run([
        "ffmpeg", "-y",
        "-i", final_video_path, "-i", local_audio,
        "-map", "0:v:0", "-map", "1:a:0",
        "-c:v", "copy",
        "-c:a", "aac", "-b:a", abr,
        "-filter:a", f"volume={audio_gain}",
        "-shortest",
        mixed_out
    ])
    return mixed_out

def _processar_job(data: dict) -> dict:
    """
    Pipeline:
      1) normaliza clipes sem áudio
      2) concatena vídeo
      3) mixa áudio externo, se houver (com audio_gain)
      4) (opcional) upload ao Drive (stub)
    Retorna metadados básicos.
    """
    tmpdir = tempfile.mkdtemp(prefix="ffx_")
    log("TMPDIR_OPEN", tmpdir=tmpdir)
    try:
        if not ffmpeg_exists():
            raise RuntimeError("ffmpeg não encontrado no container")

        if not data or "clips" not in data or not data["clips"]:
            raise ValueError("payload sem 'clips'")

        resolution   = str(data.get("resolution", DEFAULT_RESOLUTION))
        fps          = int(data.get("fps", DEFAULT_FPS))
        vbr          = str(data.get("video_bitrate", DEFAULT_VIDEO_BR))
        abr          = str(data.get("audio_bitrate", DEFAULT_AUDIO_BR))
        audio_url    = data.get("audio_url")
        output_name  = str(data.get("output_name", f"out-{uuid.uuid4().hex[:8]}.mp4"))

        try:
            audio_gain = float(data.get("audio_gain", 0.2))
        except Exception:
            audio_gain = 0.2
        # clamp básico (permite até 5x se realmente quiser)
        audio_gain = max(0.0, min(audio_gain, 5.0))

        upload_flag  = bool(data.get("upload", UPLOAD_TO_DRIVE))
        drive_folder = str(data.get("drive_folder_id", DRIVE_FOLDER_ID))

        # 1) Normalizar (vídeo-only)
        norm_paths = _baixar_videos_normalizar_sem_audio(
            clips=data["clips"],
            tmpdir=tmpdir,
            resolution=resolution,
            fps=fps,
            vbr=vbr,
        )
        log("NORMALIZE_OK", count=len(norm_paths))

        # 2) Concat vídeo
        final_out = _concat_por_demuxer(norm_paths, tmpdir, output_name)
        log("CONCAT_OK", path=final_out)

        # 3) Mix/replace áudio (opcional, com ganho)
        final_with_audio = _mix_audio_se_houver(final_out, audio_url, tmpdir, abr, audio_gain)
        log("AUDIO_OK" if audio_url else "AUDIO_SKIPPED", path=final_with_audio)

        # 4) Upload (opcional)
        upload_info = None
        if upload_flag:
            if not drive_folder:
                raise ValueError("upload=true mas 'drive_folder_id' não foi fornecido")
            upload_info = upload_to_drive(final_with_audio, os.path.basename(final_with_audio), drive_folder)
            log("UPLOAD_DONE", info=upload_info)

        return {
            "ok": True,
            "output_path": final_with_audio,
            "uploaded": bool(upload_info),
            "upload_info": upload_info,
        }

    finally:
        try:
            shutil.rmtree(tmpdir, ignore_errors=True)
            log("TMPDIR_CLOSED")
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
    # aceita sync=1 para processamento síncrono (debug/teste)
    sync = request.args.get("sync") in ("1", "true", "yes")

    try:
        data = request.get_json(force=True, silent=False)
    except Exception as e:
        return jsonify({"ok": False, "error": f"payload inválido: {e}"}), 400

    if not isinstance(data, dict):
        return jsonify({"ok": False, "error": "JSON inválido"}), 400

    if sync:
        # execução síncrona (útil para teste rápido e logs)
        try:
            result = _processar_job(data)
            return jsonify(result), 200
        except Exception as e:
            log("SYNC_ERROR", error=str(e))
            return jsonify({"ok": False, "error": str(e)}), 500

    # execução assíncrona para n8n (evita timeout do Http Request)
    job_id = uuid.uuid4().hex[:12]
    def _worker():
        log("JOB_START", job_id=job_id)
        try:
            _processar_job(data)
            log("JOB_DONE", job_id=job_id)
        except Exception as e:
            log("JOB_FAIL", job_id=job_id, error=str(e))

    threading.Thread(target=_worker, daemon=True).start()
    return jsonify({"status": "accepted", "job_id": job_id}), 202

# =========================
# Main (porta do Cloud Run)
# =========================
if __name__ == "__main__":
    # Executa com Flask interno (para rodar local ou via `python app.py` no container)
    port = int(os.environ.get("PORT", "8080"))
    log("BOOT", port=port, resolution=DEFAULT_RESOLUTION, fps=DEFAULT_FPS)
    app.run(host="0.0.0.0", port=port)
