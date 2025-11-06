# app.py — FFmpeg API (Cloud Run) - concat vídeo-only + BGM opcional
# ------------------------------------------------------------
# Endpoints:
#   GET  /            -> {"message":"FFmpeg API online","status":"ok"}
#   GET  /healthz     -> {"status":"ok"}
#   POST /concat_and_upload
#       Payload JSON (campos relevantes):
#       {
#         "clips": [
#           { "source_url": "https://...", "ss": "0", "to": "5" },
#           { "url": "https://..." }
#         ],
#         "resolution": "1080x1920",            # opcional (env DEFAULT_RESOLUTION)
#         "fps": 30,                            # opcional (env DEFAULT_FPS)
#         "video_bitrate": "4M",                # opcional (env DEFAULT_VIDEO_BR)
#         "audio_bitrate": "192k",              # opcional (env DEFAULT_AUDIO_BR)
#
#         # NOVO (preferencial):
#         "bgm_url": "https://.../bgm.mp3",     # opcional (trilha/locução)
#         "bgm_gain_db": -15,                   # opcional (ganho em dB; padrão -15)
#
#         # Compatibilidade antiga (ainda funciona):
#         "audio_url": "https://.../bgm.mp3",   # será tratado como bgm_url
#         "audio_gain": 0.2,                    # linear (0.0–1.0); converte p/ dB se bgm_gain_db ausente
#
#         "output_name": "final.mp4",           # opcional
#         "upload": false,                      # opcional (env UPLOAD_TO_DRIVE)
#         "drive_folder_id": "..."              # obrigatório se upload=true
#       }
#
# Observações:
# - Clipes são normalizados SEM áudio (-an), evitando erro em inputs mudos.
# - Concat via demuxer com cópia do stream de vídeo (tudo já reencodado de forma homogênea).
# - Se houver trilha (bgm_url ou audio_url), ela é aplicada com volume e LOOP (-stream_loop -1).
# - Sem trilha -> vídeo sai mudo (sem erro).
# - Upload ao Drive segue como stub (safe).
# ------------------------------------------------------------

import os
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
    from shutil import which
    return which("ffmpeg") is not None

def run(cmd: list[str]) -> None:
    proc = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    if proc.returncode != 0:
        raise RuntimeError(f"FFmpeg/Proc error ({proc.returncode}):\n{proc.stderr}")

def download(url: str, to_path: str) -> None:
    with requests.get(url, stream=True, timeout=60) as r:
        r.raise_for_status()
        with open(to_path, "wb") as f:
            for chunk in r.iter_content(chunk_size=1024 * 1024):
                if chunk:
                    f.write(chunk)

def upload_to_drive(local_path: str, name: str, folder_id: str) -> dict:
    # Stub seguro (substitua pela sua implementação real)
    return {
        "id": "fake-id",
        "name": name,
        "webViewLink": "https://drive.google.com/",
        "webContentLink": "https://drive.google.com/",
    }

def parse_resolution(res_str: str) -> tuple[int, int]:
    # Aceita "1080x1920" e retorna (1080, 1920)
    try:
        w, h = str(res_str).lower().split("x")
        return int(w), int(h)
    except Exception:
        return (1080, 1920)

# =========================
# Núcleo de vídeo/áudio
# =========================
def _baixar_videos_normalizar_sem_audio(
    clips: list[dict], tmpdir: str, resolution: str, fps: int, vbr: str
) -> list[str]:
    """
    Baixa e normaliza cada clipe APENAS VÍDEO (-an).
    Aceita "source_url" ou "url" em cada item. Retorna caminhos normalizados (mp4).
    """
    local_videos = []
    for i, clip in enumerate(clips):
        url = clip.get("source_url") or clip.get("url")
        if not url:
            raise ValueError(f"clip[{i}] sem 'source_url'/'url' no payload")
        local_path = os.path.join(tmpdir, f"clip_{i}.mp4")
        download(url, local_path)
        local_videos.append({"path": local_path, "ss": clip.get("ss"), "to": clip.get("to")})

    w, h = parse_resolution(resolution)
    norm_paths = []
    for i, c in enumerate(local_videos):
        norm = os.path.join(tmpdir, f"norm_{i}.mp4")
        cmd = ["ffmpeg", "-y"]
        if c.get("ss"):
            cmd += ["-ss", str(c["ss"])]
        cmd += ["-i", c["path"]]
        if c.get("to"):
            cmd += ["-to", str(c["to"])]

        # Correto: scale/pad com w:h (não usar "1080x1920" dentro do filtro)
        vf = (
            f"scale={w}:{h}:force_original_aspect_ratio=decrease,"
            f"pad={w}:{h}:(ow-iw)/2:(oh-ih)/2,setsar=1"
        )

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
    """
    Concatena os `norm_paths` via concat demuxer (cópia do stream de vídeo).
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

def _mix_audio_se_houver(final_video_path: str, bgm_url: str | None, tmpdir: str,
                         abr: str, bgm_gain_db: float | None, audio_gain_linear: float | None) -> str:
    """
    Se houver bgm_url, aplica a trilha em loop contínuo até o fim do vídeo:
      - Vídeo: copia do arquivo final concatenado
      - Áudio: BGM em loop (-stream_loop -1)
      - Volume: usa bgm_gain_db (dB). Se ausente, usa audio_gain_linear (fator). Padrão = -15 dB.
    Retorna o caminho com áudio; se não houver BGM, retorna o original.
    """
    if not bgm_url:
        return final_video_path

    local_audio = os.path.join(tmpdir, "bgm_input")
    download(bgm_url, local_audio)

    volume_filter = None
    if isinstance(bgm_gain_db, (int, float)):
        volume_filter = f"volume={bgm_gain_db}dB"
    elif isinstance(audio_gain_linear, (int, float)) and audio_gain_linear > 0:
        # usa fator linear (compatibilidade)
        volume_filter = f"volume={audio_gain_linear}"
    else:
        # padrão suave contínuo
        volume_filter = "volume=-15dB"

    mixed_out = os.path.join(tmpdir, "mixed.mp4")
    # -stream_loop -1: repete a BGM indefinidamente; -shortest encerra junto com o vídeo
    run([
        "ffmpeg", "-y",
        "-i", final_video_path,
        "-stream_loop", "-1", "-i", local_audio,
        "-map", "0:v:0", "-map", "1:a:0",
        "-c:v", "copy",
        "-c:a", "aac", "-b:a", abr,
        "-filter:a", volume_filter,
        "-shortest",
        mixed_out
    ])
    return mixed_out

def _run_concat_and_upload(data: dict) -> None:
    """
    Pipeline:
      1) normaliza clipes sem áudio
      2) concatena vídeo
      3) aplica BGM/locução (opcional) com volume e loop
      4) (opcional) upload ao Drive (stub)
    """
    tmpdir = None
    try:
        if not ffmpeg_exists():
            print("[worker] ffmpeg não encontrado no container", flush=True)
            return
        if not data or "clips" not in data or not data["clips"]:
            print("[worker] payload sem 'clips'", flush=True)
            return

        resolution    = data.get("resolution", DEFAULT_RESOLUTION)
        fps           = int(data.get("fps", DEFAULT_FPS))
        vbr           = data.get("video_bitrate", DEFAULT_VIDEO_BR)
        abr           = data.get("audio_bitrate", DEFAULT_AUDIO_BR)

        # Preferir bgm_url/bgm_gain_db; manter compat com audio_url/audio_gain
        bgm_url       = data.get("bgm_url") or data.get("audio_url")
        bgm_gain_db   = data.get("bgm_gain_db")
        try:
            if bgm_gain_db is not None:
                bgm_gain_db = float(bgm_gain_db)
        except Exception:
            bgm_gain_db = None

        audio_gain_linear = None
        try:
            if data.get("audio_gain") is not None:
                audio_gain_linear = float(data.get("audio_gain"))
        except Exception:
            audio_gain_linear = None

        upload_flag   = bool(data.get("upload", UPLOAD_TO_DRIVE))
        drive_folder  = data.get("drive_folder_id", DRIVE_FOLDER_ID)
        output_name   = data.get("output_name", f"out-{uuid.uuid4().hex[:8]}.mp4")

        tmpdir = tempfile.mkdtemp(prefix="ffx_")

        # 1) Normalizar (vídeo-only)
        norm_paths = _baixar_videos_normalizar_sem_audio(
            clips=data["clips"],
            tmpdir=tmpdir,
            resolution=resolution,
            fps=fps,
            vbr=vbr,
        )

        # 2) Concat vídeo
        final_out = _concat_video_apenas_por_demuxer(norm_paths, tmpdir, output_name)

        # 3) BGM opcional
        final_with_audio = _mix_audio_se_houver(
            final_video_path=final_out,
            bgm_url=bgm_url,
            tmpdir=tmpdir,
            abr=abr,
            bgm_gain_db=bgm_gain_db,
            audio_gain_linear=audio_gain_linear
        )

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

    job_id = uuid.uuid4().hex[:12]
    threading.Thread(target=_run_concat_and_upload, args=(data,), daemon=True).start()
    return jsonify({"status": "accepted", "job_id": job_id}), 202

# =========================
# Main (porta do Cloud Run)
# =========================
if __name__ == "__main__":
    port = int(os.environ.get("PORT", "8080"))
    app.run(host="0.0.0.0", port=port)



