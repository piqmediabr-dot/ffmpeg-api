from flask import Flask, request, jsonify
import os

app = Flask(__name__)

# === Rota principal (teste) ===
@app.route("/", methods=["GET"])
def index():
    return jsonify({
        "status": "ok",
        "message": "FFmpeg API online e respondendo!"
    }), 200

# === Rota de health check (usada pelo Cloud Run) ===
@app.route("/health", methods=["GET"])
def health():
    return jsonify({"ok": True}), 200

# === Exemplo de endpoint futuro (para integrar com n8n, etc.) ===
@app.route("/process", methods=["POST"])
def process():
    data = request.get_json(force=True)
    # Aqui entra a lógica futura com FFmpeg, Google Drive, etc.
    return jsonify({
        "received": data,
        "status": "processing_soon"
    }), 200

# === Execução local ou Cloud Run ===
if __name__ == "__main__":
    port = int(os.environ.get("PORT", "8080"))
    # Cloud Run exige que o app escute em 0.0.0.0 na porta $PORT
    app.run(host="0.0.0.0", port=port)
