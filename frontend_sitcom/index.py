import requests
from flask import Flask, Response, jsonify, render_template, request, stream_with_context


app = Flask(__name__)
BACKEND_BASE = "http://backend:5000"


def _proxy_json(method, path, **kwargs):
    try:
        response = requests.request(method=method, url=f"{BACKEND_BASE}{path}", timeout=120, **kwargs)
        return response.content, response.status_code, {"Content-Type": "application/json"}
    except requests.exceptions.RequestException as exc:
        return jsonify({"error": "Backend request failed", "details": str(exc)}), 502


@app.route("/")
def home():
    return render_template("index.html")


@app.route("/sitcom/upload-source", methods=["POST"])
def upload_source():
    source_video = request.files.get("source_video") or request.files.get("video")
    if not source_video:
        return jsonify({"error": "Missing form field: source_video"}), 400
    files = {
        "source_video": (
            source_video.filename,
            source_video.stream,
            source_video.mimetype or "application/octet-stream",
        )
    }
    return _proxy_json("POST", "/sitcom/upload-source", files=files)


@app.route("/sitcom/sources", methods=["GET"])
def sources():
    return _proxy_json("GET", "/sitcom/sources")


@app.route("/sitcom/sources/select", methods=["POST"])
def select_source():
    payload = request.get_json(silent=True) or {}
    return _proxy_json("POST", "/sitcom/sources/select", json=payload)


@app.route("/sitcom/edit/start", methods=["POST"])
def start_edit():
    payload = request.get_json(silent=True) or {}
    return _proxy_json("POST", "/sitcom/edit/start", json=payload)


@app.route("/sitcom/edit/status/<task_id>", methods=["GET"])
def edit_status(task_id):
    return _proxy_json("GET", f"/sitcom/edit/status/{task_id}")


@app.route("/sitcom/edits", methods=["GET"])
def edits():
    return _proxy_json("GET", "/sitcom/edits")


@app.route("/sitcom/files/<task_id>/<filename>", methods=["GET"])
def artifact(task_id, filename):
    backend_url = f"{BACKEND_BASE}/sitcom/files/{task_id}/{filename}"
    try:
        response = requests.get(backend_url, stream=True, timeout=120)
    except requests.exceptions.RequestException as exc:
        return jsonify({"error": "Could not fetch artifact file", "details": str(exc)}), 502

    if response.status_code >= 400:
        return Response(
            response.content,
            status=response.status_code,
            content_type=response.headers.get("Content-Type", "application/json"),
        )

    return Response(
        stream_with_context(response.iter_content(chunk_size=8192)),
        content_type=response.headers.get("Content-Type", "application/octet-stream"),
        headers={
            "Content-Disposition": response.headers.get(
                "Content-Disposition", f'inline; filename="{filename}"'
            ),
            "Content-Length": response.headers.get("Content-Length", "0"),
        },
        status=response.status_code,
    )


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=3001, debug=True)
