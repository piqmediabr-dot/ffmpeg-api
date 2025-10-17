from flask import Flask, request, jsonify
import os, json, tempfile, subprocess, shutil, time
import requests

# Google Drive
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload
import google.auth
from google.oauth2 import service_account

from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

app = Flask(__name__)

SCOPES = ["https://www.googleapis.com/auth/drive.file"]

# ========================
# FUNÇÕES AUXILIARES
# ========================

def get_gcp_creds():
    """
    Retorna as credenciais GCP válidas.
    Pode usar GOOGLE_APPLICATION_CREDENTIALS (arquivo)
    ou SERVICE_ACCOUNT_JSON (conteúdo em variável de ambiente).
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


def make_http_session():
    s = requests.Session()
    retries = Retry(total=3, backoff_factor=0.5, status_forcelist=[429, 500, 502, 503, 504])
    s.mount("http://", HTTPAdapter(max_retries=retries))
    s.mount("https://", HTTPAdapter(max_retries=retries))
    return s


def head_check(url: str, timeout_sec: int = 10):
    s = make_http_session()
    r = s.head(url, allow_redirects=True, timeout=timeout_sec)
    r.raise_for_status()
    ctype = r.headers.get("Content-Type", "")
    clen = int(r.headers.get("Content-Length", "0") or 0)
    return ctype, clen


def ffprobe_duration(path: str) -> float:
    """Retorna a duração do vídeo em segundos."""
    cmd = [
        "ffprobe","-v","error","-show_entries","format=duration",
        "-of","default=noprint_wrappers=1:nokey=1", path
    ]
    out = subprocess.check_output(cmd, text=True).strip()
    try:
        return float(out)
    except:
        return 0.0


def download_file(url: str, dest_path: str, timeout_sec: int = 120):
    s = make_http_session()
    with s.get(url, stream=True, timeout=timeout_sec) as r:
        r.raise_for_status()
        with open(dest_path, "wb") as f:
            for chunk in r.iter_content(chunk_size=1024 * 1024):
                if chunk:
                    f.write(chunk)


def concat_videos(input_paths, output_path):
    """Concatena vídeos usando ffmpeg com reencode (compatibilidade total)."""
    list_file = output_path + ".inputs.txt"
    with open(list_file, "w", encoding="utf-8") as f:
        for p in input_paths:
            f.write(f"file '{p}'\n")

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
        file = service.files().get(fileId=file["id"], fields="id, webViewLink, webContentLink, name, size").execute()

    return file


# ========================
# ROTAS PRINCIPAIS
# ========================

@app.route("/", methods=["GET"])
def index():
    return jsonify({"status": "ok", "message": "FFmpeg API online"}), 200


@app.route("/health", methods=["GET"])
def health():
    return jsonify({"ok": True}), 200


@app.route("/process", methods=["POST"])
def process():
    """
    JSON esperado:
    {
      "inputs": ["https://...mp4", "https://...mp4"],
      "output_basename": "Story_DEMO_final",
      "drive_folder_id": "xxxxxxxxxxxxxxxxxxxx",
      "public": true,
      "callback_url": "https://n8n.meuwebhook",
      "max_total_size_mb": 800,
      "enforce_mp4": true
    }
    """
    payload = request.get_json(force=True, silent=True) or {}
    inputs = payload.get("inputs") or []
    folder_id = payload.get("drive_folder_id")
    out_base = payload.get("output_basename", f"concat_{int(time.time())}")
    make_public = bool(payload.get("public", False))

    callback_url = payload.get("callback_url")
    max_total_size_mb = int(payload.get("max_total_size_mb", 800))
    enforce_mp4 = bool(payload.get("enforce_mp4", True))

    # Validação básica
    if not inputs or not isinstance(inputs, list) or not folder_id:
        return jsonify({
            "status": "error",
            "message": "JSON inválido. Necessário 'inputs' (lista de URLs) e 'drive_folder_id'."
        }), 400

    # Verificação de cabeçalhos e tamanho total
    total_size = 0
    for u in inputs:
        try:
            ctype, clen = head_check(u)
            total_size += clen
            if enforce_mp4 and "video/mp4" not in ctype.lower():
                return jsonify({"status": "error", "message": f"Input não-MP4 detectado: {u} ({ctype})"}), 415
        except Exception as e:
            return jsonify({"status": "error", "message": f"HEAD falhou em {u}: {e}"}), 400

    if (total_size / (1024 * 1024)) > max_total_size_mb:
        return jsonify({"status": "error", "message": f"Tamanho total > {max_total_size_mb}MB"}), 413

    tmpdir = tempfile.mkdtemp(prefix="ffconcat_")

    try:
        # 1) Download
        local_inputs = []
        for i, url in enumerate(inputs):
            local_path = os.path.join(tmpdir, f"in_{i}.mp4")
            download_file(url, local_path)
            local_inputs.append(local_path)

        # 2) Concatenação
        output_name = f"{out_base}.mp4"
        out_path = os.path.join(tmpdir, output_name)
        concat_videos(local_inputs, out_path)

        # 3) Metadados
        duration_sec = ffprobe_duration(out_path)

        # 4) Upload no Drive
        creds = get_gcp_creds()
        file = drive_upload(creds, out_path, folder_id, output_name, make_public=make_public)

        result = {
            "status": "done",
            "file_id": file.get("id"),
            "webViewLink": file.get("webViewLink"),
            "webContentLink": file.get("webContentLink"),
            "output_name": file.get("name"),
            "size": file.get("size"),
            "duration_sec": duration_sec,
            "inputs_count": len(inputs)
        }

        # Callback opcional (para o n8n)
        if callback_url:
            try:
                s = make_http_session()
                s.post(callback_url, json=result, timeout=10)
            except Exception:
                pass

        return jsonify(result), 200

    except subprocess.CalledProcessError as e:
        err = {"status": "ffmpeg_error", "returncode": e.returncode}
        if payload.get("callback_url"):
            try:
                s = make_http_session()
                s.post(payload["callback_url"], json=err, timeout=10)
            except Exception:
                pass
        return jsonify(err), 500

    except requests.HTTPError as e:
        err = {"status": "download_error", "message": str(e)}
        if payload.get("callback_url"):
            try:
                s = make_http_session()
                s.post(payload["callback_url"], json=err, timeout=10)
            except Exception:
                pass
        return jsonify(err), 502

    except Exception as e:
        err = {"status": "error", "message": str(e)}
        if payload.get("callback_url"):
            try:
                s = make_http_session()
                s.post(payload["callback_url"], json=err, timeout=10)
            except Exception:
                pass
        return jsonify(err), 500

    finally:
        try:
            shutil.rmtree(tmpdir, ignore_errors=True)
        except Exception:
            pass


if __name__ == "__main__":
    port = int(os.environ.get("PORT", "8080"))
    app.run(host="0.0.0.0", port=port)
