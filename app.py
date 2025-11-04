# app.py — Concat de clipes mudos (demuxer) + mix opcional de áudio + saúde p/ Cloud Run
import os
import uuid
import shutil
import threading
import tempfile
from flask import Flask, request, jsonify

# ==========================
# Defaults via ENV VARS
# ==========================
DEFAULT_RESOLUTION = os.getenv("DEFAULT_RESOLUTION", "1080x1920")  # ex: 1080x1920
DEFAULT_FPS        = int(os.getenv("DEFAULT_FPS", "30"))
DEFAULT_VIDEO_BR   = os.getenv("DEFAULT_VIDEO_BR", "4M")
DEFAULT_AUDIO_BR   = os.getenv("DEFAULT_AUDIO_BR", "192k")
UPLOAD_TO_DRIVE    = os.getenv("UPLOAD_TO_DRIVE", "false").lower() == "true"
DRIVE_FOLDER_ID    = os.getenv("DRIVE_FOLDER_ID", "")

app = Flask(__name__)

# ==========================
# Import helpers do projeto
# ==========================
# Esperado: helpers com ffmpeg_exists(), run(cmd), download(url, path), upload_to_drive(file_path, name, folder_id)
try:
    from helpers import ffmpeg_exists, run, download, upload_to_drive  # type: ignore
except Exception:
    # --------- FALLBACKS MÍNIMOS (apenas p/ testes locais) ---------
    import subprocess, requests
    def ffmpeg_exists() -> bool:
        try:
            subprocess.run(["ffmpeg", "-version"], capture_output=True, check=False)
            return True
        except Exception:
            return False

    def run(cmd_list):
        proc = subprocess.run(cmd_list, capture_output=True, text=True)
        if proc.returncode != 0:
            raise RuntimeError(f"ffmpeg failed: {proc.stderr[:2000]}")

    def download(url: str, dest_path: str):
        r = requests.get(url, stream=True, timeout=90)
        r.raise_for_status()
        with open(dest_path, "wb") as f:
            for chunk in r.iter_content(8192):
                if chunk:
                    f.write(chunk)

    def upload_to_drive(file_path: str, name: str, folder_id: str):
        # Dummy: apenas retorna info simulada (substitua pela sua integração real)
        return {"id": "dummy", "webViewLink": "", "webContentLink": ""}

# ==========================
# Health check (Cloud Run)
# ==========================
@app.route("/healthz")
def healthz():
    return "ok", 200

# ==========================
# Núcleo de processamento
# ==========================
def _baixar_videos_normalizar_sem_audio(clips, tmpdir, resolution, fps, vbr, abr):
    """
    Baixa cada clipe e normaliza o VÍDEO SEM ÁUDIO (-an).
    Retorna a lista de caminhos normalizados (mp4).
    """
    local_videos = []

    # 1) baixar vídeos (aceita source_url ou url)
    for i, clip in enumerate(clips):
        url = clip.get("source_url") or clip.get("url")
        if not url:
            raise ValueError(f"clip[{i}] sem 'source_url'/'url' no payload")
        local_path = os.path.join(tmpdir, f"clip_{i}.mp4")
        download(url, local_path)
        local_videos.append({"path": local_path, "ss": clip.get("ss"), "to": clip.get("to")})

    # 2) normalizar SEM áudio (evita erro em clipes mudos)
    norm_paths = []
    for i, c in enumerate(local_videos):
        norm = os.path.join(tmpdir, f"norm_{i}.mp4")
        cmd = ["ffmpeg", "-y"]
        if c.get("ss"): cmd += ["-ss", str(c["ss"])]
        cmd += ["-i", c["path"]]
        if c.get("to"): cmd += ["-to", str(c["to"])]

        # Filtro de vídeo: scale + pad + sar, FPS, codec, bitrate, pixel fmt, sem áudio
        cmd += [
            "-vf", f"scale={resolution}:force_original_aspect_ratio=decrease,"
                   f"pad={resolution}:(ow-iw)/2:(oh-ih)/2,setsar=1",
            "-r", str(fps),
            "-c:v", "libx264",
            "-b:v", vbr,
            "-pix_fmt", "yuv420p",
            "-an",  # <— SEM ÁUDIO
            norm
        ]
        run(cmd)
        norm_paths.append(norm)

    return norm_paths


def _concat_video_apenas_por_demuxer(norm_paths, tmpdir, output_name):
    """
    Concatena os norm_paths usando concat demuxer (vídeo-apenas).
    Retorna o caminho do arquivo final (com nome desejado).
    """
    list_file = os.path.join(tmpdir, "inputs.txt")
    with open(list_file, "w", encoding="utf-8") as f:
        for p in norm_paths:
            f.write(f"file '{p}'\n")

    concat_out = os.path.join(tmpdir, "concat.mp4")
    # Vídeo-APENAS: copia stream (já normalizada) sem tocar em áudio
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


def _mix_audio_se_houver(final_video_path, audio_url, tmpdir, abr):
    """
    Se houver audio_url, baixa e usa como trilha final (mantém vídeo).
    Retorna caminho do arquivo com áudio mixado.
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


def _run_concat_and_upload(data):
    # --- EXECUÇÃO PESADA EM THREAD ---
    tmpdir = tempfile.mkdtemp(prefix="ffx_")
    try:
        if not ffmpeg_exists():
            print("[worker] ffmpeg não encontrado no container", flush=True)
            return

        if not data or "clips" not in data or not data["clips"]:
            print("[worker] payload sem 'clips'", flush=True)
            return

        # Parametrização
        resolution    = data.get("resolution", DEFAULT_RESOLUTION)
        fps           = int(data.get("fps", DEFAULT_FPS))
        vbr           = data.get("video_bitrate", DEFAULT_VIDEO_BR)
        abr           = data.get("audio_bitrate", DEFAULT_AUDIO_BR)
        audio_url     = data.get("audio_url")               # opcional
        upload_flag   = bool(data.get("upload", UPLOAD_TO_DRIVE))
        drive_folder  = data.get("drive_folder_id", DRIVE_FOLDER_ID)
        output_name   = data.get("output_name", f"out-{uuid.uuid4().hex[:8]}.mp4")

        # 1) Normaliza todos os clipes SEM áudio (robusto p/ inputs mudos)
        norm_paths = _baixar_videos_normalizar_sem_audio(data["clips"], tmpdir, resolution, fps, vbr, abr)

        # 2) Concatena via demuxer (vídeo-apenas)
        final_out = _concat_video_apenas_por_demuxer(norm_paths, tmpdir, output_name)

        # 3) Se houver áudio externo (BGM/narração), troca/mixa
        final_with_audio = _mix_audio_se_houver(final_out, audio_url, tmpdir, abr)

        # 4) Upload (se habilitado)
        if upload_flag:
            if not drive_folder:
                print("[worker] upload=true mas sem drive_folder_id", flush=True)
                return
            info = upload_to_drive(final_with_audio, os.path.basename(final_with_audio), drive_folder)
            print("[worker] upload ok:",
                  {"id": info.get("id"),
                   "webViewLink": info.get("webViewLink"),
                   "webContentLink": info.get("webContentLink")},
                  flush=True)
        else:
            print("[worker] arquivo final pronto (sem upload):", final_with_audio, flush=True)

    except Exception as e:
        print(f"[worker] ERRO: {e}", flush=True)
    finally:
        try:
            shutil.rmtree(tmpdir, ignore_errors=True)
        except Exception:
            pass

# ==========================
# Endpoint público
# ==========================
@app.route("/concat_and_upload", methods=["POST"])
def concat_and_upload():
    # ACK imediato (evita timeout no n8n). O trabalho roda em background.
    data = request.get_json(force=True, silent=False)

    clips = (data or {}).get("clips") or []
    if not clips:
        return jsonify({"ok": False, "error": "clips vazio"}), 400

    # Compat c/ payloads antigos/atuais:
    # n8n envia: [{ "source_url": "..." }, ...]
    # versão antiga aceitava: [{ "url": "..." }, ...]
    # A normalização acima trata ambos.

    job_id = uuid.uuid4().hex[:12]
    threading.Thread(target=_run_concat_and_upload, args=(data,), daemon=True).start()
    return jsonify({"status": "accepted", "job_id": job_id}), 202

# ==========================
# Fallback local
# ==========================
if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", "8080")))
