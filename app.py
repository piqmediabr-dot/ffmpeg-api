cat > app.py << 'EOF'
import os, subprocess, tempfile
from flask import Flask, request, jsonify
import google.auth
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload

app = Flask(__name__)

def run(cmd):
    p = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    return p.returncode, p.stdout, p.stderr

def drive_service():
    creds, _ = google.auth.default(scopes=["https://www.googleapis.com/auth/drive"])
    return build("drive", "v3", credentials=creds, cache_discovery=False)

def upload_to_drive(local_path, filename, folder_id, make_public=False):
    svc = drive_service()
    media = MediaFileUpload(local_path, mimetype="video/mp4", resumable=True)
    meta = {"name": filename}
    if folder_id:
        meta["parents"] = [folder_id]
    file = svc.files().create(body=meta, media_body=media, fields="id,name,webViewLink,webContentLink").execute()
    file_id = file["id"]
    if make_public:
        svc.permissions().create(fileId=file_id, body={"role": "reader", "type": "anyone"}).execute()
    file = svc.files().get(fileId=file_id, fields="id,name,webViewLink,webContentLink").execute()
    return file

@app.get("/health")
def health():
    return {"ok": True}

@app.post("/concat")
def concat():
    d = request.get_json()
    inputs = d["inputs"]
    outname = d["output_basename"] + ".mp4"
    tmpdir = tempfile.mkdtemp()
    local_inputs = []
    for i, url in enumerate(inputs):
        local = os.path.join(tmpdir, f"in_{i}.mp4")
        rc,_,err = run(["bash","-lc", f"curl -fL '{url}' -o '{local}'"])
        if rc != 0:
            return jsonify({"ok": False, "step":"download", "url": url, "stderr": err}), 400
        local_inputs.append(local)
    listfile = os.path.join(tmpdir, "list.txt")
    with open(listfile,"w") as f:
        for p in local_inputs: f.write(f"file '{p}'\n")
    outfile = os.path.join(tmpdir, outname)
    rc, out, err = run(["ffmpeg","-y","-f","concat","-safe","0","-i",listfile,"-c","copy", outfile])
    if rc != 0:
        rc2, out2, err2 = run([
            "ffmpeg","-y","-f","concat","-safe","0","-i",listfile,
            "-c:v","libx264","-preset","veryfast","-crf","20",
            "-c:a","aac","-b:a","192k", outfile
        ])
        if rc2 != 0:
            return jsonify({"ok": False, "step":"ffmpeg", "stderr": err + "\n" + err2}), 400
    return jsonify({"ok": True, "local_output": outfile}), 200

@app.post("/concat_and_upload")
def concat_and_upload():
    d = request.get_json()
    d.setdefault("public", False)
    # 1) concat
    with app.test_request_context():
        from flask import json
        resp = app.test_client().post("/concat", json={"inputs": d["inputs"], "output_basename": d["output_basename"]})
        data = resp.get_json()
    if not data.get("ok"):
        return jsonify(data), 400
    local_out = data["local_output"]
    filename = os.path.basename(local_out)
    # 2) upload
    try:
        meta = upload_to_drive(local_out, filename, d.get("drive_folder_id"), d["public"])
        return jsonify({"ok": True, "drive_file": meta}), 200
    except Exception as e:
        return jsonify({"ok": False, "step":"drive_upload", "error": str(e)}), 500
EOF
if __name__ == "__main__":
    import os
    port = int(os.environ.get("PORT", "8080"))
    # O Cloud Run exige bind em 0.0.0.0 e na porta $PORT
    app.run(host="0.0.0.0", port=port)
