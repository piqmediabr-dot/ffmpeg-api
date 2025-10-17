from flask import Flask, request, jsonify
import os, json, tempfile, subprocess, shutil, time
import requests

# Google Drive
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload
import google.auth
from google.oauth2 import service_account

app = Flask(__name__)

SCOPES = ["https://www.googleapis.com/auth/drive.file"]

def get_gcp_creds():
    """
    1) Se GOOGLE_APPLICATION_CREDENTIALS estiver apontando para um arquivo válido (ADC), usa.
    2) Senão, se SERVICE_ACCOUNT_JSON existir (conteúdo JSON), usa ele.
    """
    gac = os.environ.get("GOOGLE_APPLICATION_CREDENTIALS", "")
    if gac and os.path.exists(gac):
        creds, _ = google.auth.default(scopes=SCOPES)
        return creds

    sa_json = os.environ.get("SERVICE_ACCOUNT_JSON")
    if sa_json:
        info = json.loads(sa_json)
        return service_account.Credentials.from_service_account_info(info, scopes=SCOPES)

    raise RuntimeError("Credenciais GCP não encontradas. Configure GOOGLE_APPLICATION_CREDENTIALS ou SERVICE_ACCOUNT_JSON.")

def download_file(url: str, dest_path: str, timeout_sec: int = 120):
    with requests.get(url, stream=True, timeout=timeout_sec) as r:
        r.raise_for_status()
        with open(dest_path, "wb") as f:
            for chunk in r.iter_content(chunk_size=1024 * 1024):
                if chunk:
                    f.write(chunk)

def concat_videos(input_paths, output_path):
    """
    Usa o demuxer concat. Para máxima compatibilidade, reencode (x264 + aac).
    """
    list_file = output_path + ".inputs.txt"
    with open(list_file, "w", encoding="utf-8") as f:
        for p in input_paths:
            # caminhos seguros para ffmpeg
            f.write(f"file '{p}'\n")

    # Reencode para evitar falhas de 'different codecs/timebase'
    cmd = [
        "ffmpeg",
        "-y",
        "-f", "concat",
        "-safe", "0",
        "-i", list_file,
        "-c:v", "libx264",
        "-preset", "veryfast",
        "-crf", "23",
        "-c:a", "aac",
        "-b:a", "192k",
        "-movflags", "+faststart",
        output_path,
    ]
    subprocess.run(cmd, check=True)

def drive_upload(creds, file_path, folder_id, output_name, make_public=False):
    service = build("drive", "v3", credentials=creds)
    file_metadata = {
        "name": output_name,
        "parents": [folder_id]
    }
    media = MediaFileUpload(file_path, mimetype="video/mp4", resumable=True)
    file = service.files().create(body=file_metadata, media_body=media, fields="id, webViewLink, webContentLink, name, size").execute()

    if make_public:
        service.permissions().create(
            fileId=file["id"],
            body={"role": "reader", "type": "anyone"},
        ).execute()
        # Recarrega links após permissão (opcional)
        file = service.files().get(fileId=file["id"], fields="id, webViewLink, webContentLink, name, size").execute()

    return file

@app.route("/", methods=["GET"])
def index():
    return jsonify({"status": "ok", "message": "FFmpeg API online"}), 200

@app.route("/health", methods=["GET"])
def health():
    return jsonify({"ok": True}), 200

@app.route("/process", methods=["POST"])
def process():
    """
    Espera um JSON:
    {
      "inputs": ["https://...mp4", "https://...mp4"],
      "output_basename": "Story_DEMO_final",
      "drive_folder_id": "xxxxxxxxxxxxxxxxxxxx",
      "public": true
    }
    """
    payload = request.get_json(force=True, silent=True) or {}
    inputs = payload.get("inputs") or []
    folder_id = payload.get("drive_folder_id")
    out_base = payload.get("output_basename", f"concat_{int(time.time())}")
    make_public = bool(payload.get("public", False))

    if not inputs or not isinstance(inputs, list) or not folder_id:
        return jsonify({
            "status": "error",
            "message": "JSON inválido. Necessário 'inputs' (lista de URLs) e 'drive_folder_id'."
        }), 400

    tmpdir = tempfile.mkdtemp(prefix="ffconcat_")
    try:
        # 1) Download
        local_inputs = []
        for i, url in enumerate(inputs):
            ext = ".mp4"
            local_path = os.path.join(tmpdir, f"in_{i}{ext}")
            download_file(url, local_path)
            local_inputs.append(local_path)

        # 2) Concat
        output_name = f"{out_base}.mp4"
        out_path = os.path.join(tmpdir, output_name)
        concat_videos(local_inputs, out_path)

        # 3) Upload Drive
        creds = get_gcp_creds()
        file = drive_upload(creds, out_path, folder_id, output_name, make_public=make_public)

        return jsonify({
            "status": "done",
            "file_id": file.get("id"),
            "webViewLink": file.get("webViewLink"),
            "webContentLink": file.get("webContentLink"),
            "output_name": file.get("name"),
            "size": file.get("size")
        }), 200

    except subprocess.CalledProcessError as e:
        return jsonify({"status": "ffmpeg_error", "returncode": e.returncode}), 500
    except requests.HTTPError as e:
        return jsonify({"status": "download_error", "message": str(e)}), 502
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500
    finally:
        # Limpa tmp
        try:
            shutil.rmtree(tmpdir, ignore_errors=True)
        except Exception:
            pass

if __name__ == "__main__":
    port = int(os.environ.get("PORT", "8080"))
    app.run(host="0.0.0.0", port=port)

