import uuid, threading

def _run_concat_and_upload(data):
    # --- ESTA É A SUA LÓGICA PESADA ATUAL, SÓ QUE SEM request.get_json() ---
    # Reaproveita suas funções: ffmpeg_exists, run, download, upload_to_drive
    import tempfile, os, shutil

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
    audio_url     = data.get("audio_url")
    upload_flag   = bool(data.get("upload", UPLOAD_TO_DRIVE))
    drive_folder  = data.get("drive_folder_id", DRIVE_FOLDER_ID)
    output_name   = data.get("output_name", f"out-{uuid.uuid4().hex[:8]}.mp4")

    tmpdir = tempfile.mkdtemp(prefix="ffx_")
    try:
        # 1) baixar vídeos
        local_videos = []
        for i, clip in enumerate(data["clips"]):
            url = clip.get("url")
            if not url:
                print(f"[worker] clip[{i}] sem 'url'", flush=True)
                shutil.rmtree(tmpdir, ignore_errors=True)
                return
            local_path = os.path.join(tmpdir, f"clip_{i}.mp4")
            download(url, local_path)
            local_videos.append({"path": local_path, "ss": clip.get("ss"), "to": clip.get("to")})

        # 2) baixar áudio (opcional)
        local_audio = None
        if audio_url:
            local_audio = os.path.join(tmpdir, "audio_input")
            download(audio_url, local_audio)

        # 3) normalizar
        norm_paths = []
        for i, c in enumerate(local_videos):
            norm = os.path.join(tmpdir, f"norm_{i}.mp4")
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

        # 4) concat
        list_file = os.path.join(tmpdir, "inputs.txt")
        with open(list_file, "w", encoding="utf-8") as f:
            for p in norm_paths:
                f.write(f"file '{p}'\n")
        concat_out = os.path.join(tmpdir, "concat.mp4")
        run(["ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", list_file, "-c", "copy", concat_out])

        # 5) mix/replace áudio se houver
        final_out = os.path.join(tmpdir, output_name)
        if local_audio:
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
            shutil.copyfile(concat_out, final_out)

        # 6) upload (se habilitado)
        if upload_flag:
            if not drive_folder:
                print("[worker] upload=true mas sem drive_folder_id", flush=True)
                return
            info = upload_to_drive(final_out, output_name, drive_folder)
            print("[worker] upload ok:",
                  {"id": info.get("id"), "webViewLink": info.get("webViewLink"), "webContentLink": info.get("webContentLink")},
                  flush=True)
        else:
            print("[worker] arquivo final pronto (sem upload):", final_out, flush=True)

    except Exception as e:
        print(f"[worker] ERRO: {e}", flush=True)
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


@app.route("/concat_and_upload", methods=["POST"])
def concat_and_upload():
    # ACK imediato (evita timeout no n8n). O trabalho roda em background.
    data = request.get_json(force=True, silent=False)
    clips = (data or {}).get("clips") or []
    if not clips:
        return jsonify({"ok": False, "error": "clips vazio"}), 400

    job_id = uuid.uuid4().hex[:12]
    threading.Thread(target=_run_concat_and_upload, args=(data,), daemon=True).start()
    return jsonify({"status": "accepted", "job_id": job_id}), 202
