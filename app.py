# app.py — FFmpeg API (Cloud Run) - concat demuxer + mix opcional de áudio
# ------------------------------------------------------------
# Endpoints:
#   GET  /             -> {"message":"FFmpeg API online","status":"ok"}
#   GET  /health       -> {"status":"ok"}
#   GET  /healthz      -> {"status":"ok"}
#   POST /concat_and_upload   (assíncrono: dispara thread e responde 202)
#   POST /concat_sync         (síncrono: executa no request e devolve 200/500)
#
# Payload (ambos):
# {
#   "clips": [
#     { "source_url": "https://...", "ss": "0", "to": "5" },
#     { "url": "https://..." }
#   ],
#   "resolution": "1080x1920" ou "1080:1920",
#   "fps": 30,
#   "video_bitrate": "4M",
#   "audio_bitrate": "192k",
#   "audio_url": "https://.../bgm.mp3",
#   "audio_gain": 0.2,
#   "output_name": "final.mp4",
#   "upload": false,
#   "drive_folder_id": "..."
# }
# ------------------------------------------------------------

import os
import uuid
import shutil
import threading
import tempfile
import subprocess
from datetime import datetime

from flask import Flask, request, jsonify
import requests

app = Flask(__name__)

# =========================
# Configs (ENV)
# =========================
DEFAULT_RESOLUTION = os.getenv("DEFAULT_RESOLUTION", "1080x1920")
DEFAULT_FPS        = int(os.getenv("DEFAULT_FPS", "30"))
DEFAULT_VIDEO_BR   = os.getenv("DEFAULT_VIDEO_BR", "4M")
DEFAULT_AUDIO_BR   = os.getenv("DEFAULT_AUDIO_BR", "192k")
UPLOAD_TO_DRIVE    = os.getenv("UPLOAD_TO_DRIVE", "false").lower() == "true"
DRIVE_FOLDER_ID    = os.getenv("DRIVE_FOLDER_ID", "")

def _now():
    return datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")

def _log(msg):
    # sempre com prefixo [worker] para facilitar filtro no Cloud Logging
    print(f"[worker] {_now()} {msg}", flush=True)

# =========================
# Helpers
# =========================
def ffmpeg_exists() -> bool:
    from shutil import which
    return which("ffmpeg") is not None

def run(cmd: list[str]) -> None:
    _log(f"RUN: {' '.join(cmd)}")
    p = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    if p.stdout:
        _log(f"STDOUT:\n{p.stdout}")
    if p.stderr:
        _log(f"STDERR:\n{p.stderr}")
    if p.returncode != 0:
        raise RuntimeError(f"FFmpeg/Proc error ({p.returncode})")

def download(url: str, to_path: str) -> None:
    _log(f"Baixando: {url} -> {to_path}")
    with requests.get(url, stream=True, timeout=120) as r:
        r.raise_for_status()
        with open(to_path, "wb") as f:
            for chunk in r.iter_content(chunk_size=1024 * 1024):
                if chunk:
                    f.write(chunk)

def upload_to_drive(local_path: str, name: str, folder_id: str) -> dict:
    # Stub seguro (trocar pela integração real depois)
    return {
        "id": "fake-id",
        "name": name,
        "webViewLink": "https://drive.google.com/",
        "webContentLink": "https://drive.google.com/",
    }

def _parse_res(res: str) -> tuple[int, int]:
    """
    Aceita "1080x1920" ou "1080:1920" e retorna (w, h) inteiros.
    """
    s = str(res).lower().strip()
    if "x" in s:
        w, h = s.split("x", 1)
    elif ":" in s:
        w, h = s.split(":", 1)
    else:
        raise ValueError(f"resolution inválida: {res}")
    return int(w), int(h)

# =========================
# Núcleo de vídeo/áudio
# =========================
def _baixar_videos_normalizar_sem_audio(
    clips: list[dict], tmpdir: str, resolution: str, fps: int, vbr: str, abr: str
) -> list[str]:
    local_videos = []
    for i, clip in enumerate(clips):
        url = clip.get("source_url") or clip.get("url")
        if not url:
            raise ValueError(f"clip[{i}] sem 'source_url'/'url' no payload")
        local_path = os.path.join(tmpdir, f"clip_{i}.mp4")
        download(url, local_path)
        local_videos.append({"path": local_path, "ss": clip.get("ss"), "to": clip.get("to")})

    w, h = _parse_res(resolution)
    vf = (
        f"scale={w}:{h}:force_original_aspect_ratio=decrease,"
        f"pad={w}:{h}:(ow-iw)/2:(oh-ih)/2:color=black,"
        f"setsar=1"
    )
    _log(f"VF montado: {vf} | fps={fps} vbr={vbr}")

    norm_paths = []
    for i, c in enumerate(local_videos):
        norm = os.path.join(tmpdir, f"norm_{i}.mp4")
        cmd = ["ffmpeg", "-y"]
        if c.get("ss"):
            cmd += ["-ss", str(c["ss"])]
        cmd += ["-i", c["path"]]
        if c.get("to"):
            cmd += ["-to", str(c["to"])]

        cmd += [
            "-vf", vf,
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

def _concat_video_apenas_por_demuxer(norm_paths: list[str], tmpdir: str, output_name: str) -> str:
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

    final_out = os.path.join(
        tmpdir, output_name if output_name.endswith(".mp4") else output_name + ".mp4"
    )
    shutil.copyfile(concat_out, final_out)
    return final_out

def _mix_audio_se_houver(final_video_path: str, audio_url: str | None, tmpdir: str, abr: str, audio_gain: float) -> str:
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
        "-filter:a", f"volume={audio_gain}",
        "-shortest",
        mixed_out
    ])
    return mixed_out

def _pipeline(data: dict) -> dict:
    if not ffmpeg_exists():
        raise RuntimeError("ffmpeg não encontrado no container")

    if not data or "clips" not in data or not data["clips"]:
        raise ValueError("payload sem 'clips'")

    resolution   = data.get("resolution", DEFAULT_RESOLUTION)
    fps          = int(data.get("fps", DEFAULT_FPS))
    vbr          = data.get("video_bitrate", DEFAULT_VIDEO_BR)
    abr          = data.get("audio_bitrate", DEFAULT_AUDIO_BR)
    audio_url    = data.get("audio_url")
    audio_gain   = float(data.get("audio_gain", 0.2))
    audio_gain   = max(0.0, min(audio_gain, 5.0))
    upload_flag  = bool(data.get("upload", UPLOAD_TO_DRIVE))
    drive_folder = data.get("drive_folder_id", DRIVE_FOLDER_ID)
    output_name  = data.get("output_name", f"out-{uuid.uuid4().hex[:8]}.mp4")

    _log(f"REQ: resolution={resolution} fps={fps} vbr={vbr} abr={abr} "
         f"audio_url={bool(audio_url)} audio_gain={audio_gain} "
         f"upload={upload_flag} drive_folder={(drive_folder[:6]+'...' if drive_folder else '')} "
         f"output_name={output_name}")

    tmpdir = tempfile.mkdtemp(prefix="ffx_")
    _log(f"TMP criado: {tmpdir}")
    try:
        norm_paths = _baixar_videos_normalizar_sem_audio(
            clips=data["clips"],
            tmpdir=tmpdir,
            resolution=resolution,
            fps=fps,
            vbr=vbr,
            abr=abr,
        )
        final_out = _concat_video_apenas_por_demuxer(norm_paths, tmpdir, output_name)
        final_with_audio = _mix_audio_se_houver(final_out, audio_url, tmpdir, abr, audio_gain)

        resp = {"ok": True, "path": final_with_audio}
        if upload_flag:
            if not drive_folder:
                raise ValueError("upload=true mas sem drive_folder_id")
            info = upload_to_drive(final_with_audio, os.path.basename(final_with_audio), drive_folder)
            resp["upload"] = {
                "id": info.get("id"),
                "webViewLink": info.get("webViewLink"),
                "webContentLink": info.get("webContentLink"),
            }
            _log(f"Upload ok: {resp['upload']}")
        else:
            _log(f"Arquivo pronto (sem upload): {final_with_audio}")
        return resp
    finally:
        try:
            shutil.rmtree(tmpdir, ignore_errors=True)
            _log(f"TMP removido: {tmpdir}")
        except Exception as e:
            _log(f"Falha ao remover TMP: {e}")

def _run_concat_and_upload(data: dict) -> None:
    try:
        _ = _pipeline(data)
    except Exception as e:
        _log(f"ERRO: {e}")

# =========================
# Rotas HTTP
# =========================
@app.get("/")
def root():
    return jsonify({"message": "FFmpeg API online", "status": "ok"}), 200

@app.get("/health")
def health():
    return jsonify({"status": "ok"}), 200

@app.get("/healthz")
def healthz():
    return jsonify({"status": "ok"}), 200

@app.post("/concat_and_upload")
def concat_and_upload():
    # assíncrono (thread)
    data = request.get_json(force=True, silent=False)
    clips = (data or {}).get("clips") or []
    if not isinstance(clips, list) or not clips:
        return jsonify({"ok": False, "error": "clips vazio ou inválido"}), 400
    job_id = uuid.uuid4().hex[:12]
    threading.Thread(target=_run_concat_and_upload, args=(data,), daemon=True).start()
    return jsonify({"status": "accepted", "job_id": job_id}), 202

@app.post("/concat_sync")
def concat_sync():
    # síncrono (depuração)
    try:
        data = request.get_json(force=True, silent=False)
        result = _pipeline(data)
        return jsonify(result), 200
    except Exception as e:
        _log(f"ERRO sync: {e}")
        return jsonify({"ok": False, "error": str(e)}), 500

if __name__ == "__main__":
    port = int(os.environ.get("PORT", "8080"))
    app.run(host="0.0.0.0", port=port)



