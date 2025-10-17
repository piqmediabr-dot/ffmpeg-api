from flask import Flask, request, jsonify
import os, tempfile, subprocess, uuid, json, shutil
import requests

# ==== Config ====
DEFAULT_RESOLUTION = os.getenv("DEFAULT_RESOLUTION", "1080x1920")  # 9:16
DEFAULT_FPS        = int(os.getenv("DEFAULT_FPS", "30"))
DEFAULT_VIDEO_BR   = os.getenv("DEFAULT_VIDEO_BR", "4M")
DEFAULT_AUDIO_BR   = os.getenv("DEFAULT_AUDIO_BR", "192k")
UPLOAD_TO_DRIVE    = os.getenv("UPLOAD_TO_DRIVE", "true").lower() == "true"
DRIVE_FOLDER_ID    = os.getenv("DRIVE_FOLDER_ID")  # pode vir via env ou payload

# ==== Flask ====
app = Flask(__name__)

@app.route("/", methods=["GET"])
def index():
    return jsonify({"ok": True, "service": "ffmpeg-api", "message": "online"}), 200

@app.route("/health", methods=["GET"])
def health():
    return jsonify({"ok": True}), 200

# ==== Util ====
def run(cmd):
    proc = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    if proc.returncode != 0:
        raise RuntimeError(proc.stderr[-4000:])
    return proc

def download(url, dest_path):
    r = requests.get(url, stream=True, timeout=120)
    r.raise_for_status()
    with open(dest_path, "wb") as f:
        for chunk in r.iter_content(chunk_size=1024 * 1024):
            if chunk:
                f.write(chunk)
    return dest_path

def ffmpeg_exists():
    try:
        subprocess.run(["ffmpeg", "-version"], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        return True
    except Exception:
        return False

# ==== Google Drive upload (usa credencial padrão do Cloud Run SA) ====
def upload_to_drive(filepath, filename, folder_id):
    # Lazy import para não pesar o cold start quando não usar upload
    from googleapiclient.discovery import build
    from googleapiclient.http import MediaFileUpload
    import google.auth

    creds, _ = google.auth.default(scopes=["https://www.googleapis.com/auth/drive.file"])
    service = build("drive", "v3", credentials=creds)

    file_metadata = {"name": filename}
    if folder_id:
        file_metadata["parents"] = [folder_id]

    media = MediaFileUpload(filepath, mimetype="video/mp4", resumable=True)
    created = service.files().create(body=file_metadata, media_body=media, fields="id, webViewLink, webContentLink").execute()
    return created

# ==== Core: concat + (opcional) mix de áudio ====
@app.route("/concat_and_upload", methods=["POST"])
def concat_and_upload():
    """
    Payload esperado (exemplo):
    {
      "clips": [
        {"url": "https://.../cena1.mp4", "ss": "00:00:02", "to": "00:00:07"},
        {"url": "https://.../cena2.mp4"}  // ss/to opcionais
      ],
      "audio_url": "https://.../narracao.mp3",   // opcional
      "resolution": "1080x1920",                 // opcional
      "fps": 30,                                 // opcional
      "video_bitrate": "4M",                     // opcional
      "audio_bitrate": "192k",                   // opcional
      "output_name": "tiktok_60s.mp4",           // opcional
      "upload": true,                            // opcional (default vem do env)
      "drive_folder_id": "xxxxx"                 // opcional (se não vier, usa env)
    }
    """
    if not ffmpeg_exists():
        return jsonify({"ok": False, "error": "ffmpeg not found in container"}), 500

    data = request.get_json(force=True, silent=False)
    if not data or "clips" not in data or not data["clips"]:
        return jsonify({"ok": False, "error": "payload precisa de 'clips'"}), 400

    resolution    = data.get("resolution", DEFAULT_RESOLUTION)
    fps           = int(data.get("fps", DEFAULT_FPS))
    vbr           = data.get("video_bitrate", DEFAULT_VIDEO_BR)
    abr           = data.get("audio_bitrate", DEFAULT_AUDIO_BR)
    audio_url     = data.get("audio_url")
    upload_flag   = bool(data.get("upload", UPLOAD_TO_DRIVE))
    drive_folder  = data.get("drive_folder_id", DRIVE_FOLDER_ID)
    output_name   = data.get("output_name", f"out-{uuid.uuid4().hex[:8]}.mp4")

    tmpdir = tempfile.mkdtemp(prefix="ffx_")
    try:
        # 1) Baixar todos os vídeos
        local_videos = []
        for i, clip in enumerate(data["clips"]):
            url = clip.get("url")
            if not url:
                shutil.rmtree(tmpdir, ignore_errors=True)
                return jsonify({"ok": False, "error": f"clip[{i}] sem 'url'"}), 400
            local_path = os.path.join(tmpdir, f"clip_{i}.mp4")
            download(url, local_path)
            local_videos.append({"path": local_path, "ss": clip.get("ss"), "to": clip.get("to")})

        # 2) (Opcional) baixar áudio
        local_audio = None
        if audio_url:
            local_audio = os.path.join(tmpdir, "audio_input")
            # deixe a extensão livre; o ffmpeg detecta pelo header
            download(audio_url, local_audio)

        # 3) Normalizar cada vídeo para mesmo formato/tamanho/fps (intermediários)
        norm_paths = []
        for i, c in enumerate(local_videos):
            norm = os.path.join(tmpdir, f"norm_{i}.mp4")
            # build filtros (ss/to aplicados aqui, se presentes)
            cmd = ["ffmpeg", "-y"]
            if c.get("ss"): cmd += ["-ss", c["ss"]]
            cmd += ["-i", c["path"]]
            if c.get("to"): cmd += ["-to", c["to"]]
            cmd += [
                "-vf", f"scale={resolution}:force_original_aspect_ratio=decrease,pad={resolution}:(ow-iw)/2:(oh-ih)/2,setsar=1",
                "-r", str(fps),
                "-c:v", "libx264", "-b:v", vbr, "-pix_fmt", "yuv420p",
                "-c:a", "aac", "-b:a", abr,
                norm
            ]
            run(cmd)
            norm_paths.append(norm)

        # 4) Concat dos normalizados
        # Usamos concat demuxer (lista textfile)
        list_file = os.path.join(tmpdir, "inputs.txt")
        with open(list_file, "w", encoding="utf-8") as f:
            for p in norm_paths:
                f.write(f"file '{p}'\n")
        concat_out = os.path.join(tmpdir, "concat.mp4")
        run(["ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", list_file,
             "-c", "copy", concat_out])

        # 5) Se houver audio_url, fazer mix/replace (substitui o áudio)
        final_out = os.path.join(tmpdir, output_name)
        if local_audio:
            # Ajusta duração para o vídeo (copia vídeo e re-encode áudio p/ mix limpo)
            run([
                "ffmpeg", "-y",
                "-i", concat_out, "-i", local_audio,
                "-map", "0:v:0", "-map", "1:a:0",
                "-c:v", "copy",
                "-c:a", "aac", "-b:a", abr,
                "-shortest",
                final_out
            ])
        else:
            # Apenas copia o resultado do concat
            shutil.copyfile(concat_out, final_out)

        result = {"ok": True, "output_path": final_out}

        # 6) Upload para Drive (se habilitado)
        if upload_flag:
            if not drive_folder:
                return jsonify({"ok": False, "error": "upload=true, mas sem drive_folder_id (env ou payload)"}), 400
            info = upload_to_drive(final_out, output_name, drive_folder)
            result.update({
                "uploaded": True,
                "drive_file_id": info.get("id"),
                "webViewLink": info.get("webViewLink"),
                "webContentLink": info.get("webContentLink")
            })
        else:
            result["uploaded"] = False

        return jsonify(result), 200

    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)

if __name__ == "__main__":
    port = int(os.getenv("PORT", "8080"))
    app.run(host="0.0.0.0", port=port)
