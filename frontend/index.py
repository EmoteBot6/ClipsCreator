import os
import requests
from flask import (
    Flask,
    Response,
    jsonify,
    render_template,
    request,
    send_from_directory,
    stream_with_context,
)


app = Flask(__name__)
BACKEND_BASE = "http://backend:5000"
REQUEST_TIMEOUT = 120


def _proxy_json(method, path, **kwargs):
    try:
        response = requests.request(
            method=method,
            url=f"{BACKEND_BASE}{path}",
            timeout=REQUEST_TIMEOUT,
            **kwargs,
        )
        content_type = response.headers.get("Content-Type", "")
        if "json" in content_type.lower():
            return response.content, response.status_code, {"Content-Type": content_type}

        try:
            parsed = response.json()
        except ValueError:
            details = (response.text or "").strip()
            if len(details) > 1200:
                details = details[:1200]
            status_code = response.status_code if response.status_code >= 400 else 502
            return (
                jsonify(
                    {
                        "error": "Backend returned a non-JSON response",
                        "status_code": response.status_code,
                        "details": details,
                    }
                ),
                status_code,
            )
        return jsonify(parsed), response.status_code
    except requests.exceptions.RequestException as exc:
        return jsonify({"error": "Backend request failed", "details": str(exc)}), 502


@app.route("/")
@app.route("/multi-clip")
def home():
    return render_template("multi_clip.html", active_page="multi_clip")


@app.route("/single-clip")
def single_clip():
    return render_template("single_clip.html", active_page="single_clip")


@app.route("/centered-mobile")
def centered_mobile():
    return render_template("centered_mobile.html", active_page="centered_mobile")


@app.route("/favicon.ico")
def favicon():
    return send_from_directory(
        os.path.join(app.root_path, "static"),
        "favicon.ico",
        mimetype="image/vnd.microsoft.icon",
    )


@app.route("/latest-synapse-video", methods=["GET"])
def latest_synapse_video():
    return _proxy_json("GET", "/latest-synapse-video")


@app.route("/auto-synapse/status", methods=["GET"])
def auto_synapse_status():
    return _proxy_json("GET", "/auto-synapse/status")


@app.route("/auto-synapse/diagnostics", methods=["GET"])
def auto_synapse_diagnostics():
    query = request.query_string.decode("utf-8")
    path = "/auto-synapse/diagnostics"
    if query:
        path = f"{path}?{query}"
    return _proxy_json("GET", path)


@app.route("/auto-synapse/check", methods=["POST"])
def auto_synapse_check():
    return _proxy_json("POST", "/auto-synapse/check")


@app.route("/auto-synapse/stop", methods=["POST"])
def auto_synapse_stop():
    return _proxy_json("POST", "/auto-synapse/stop")


@app.route("/upload-source-video", methods=["POST"])
def upload_source_video():
    if "source_video" not in request.files:
        return jsonify({"error": "Missing form field: source_video"}), 400

    source_video = request.files["source_video"]
    files = {
        "source_video": (
            source_video.filename,
            source_video.stream,
            source_video.mimetype or "application/octet-stream",
        )
    }
    return _proxy_json("POST", "/upload-source-video", files=files)


@app.route("/source-videos/download", methods=["POST"])
def download_source_video():
    payload = request.get_json(silent=True) or {}
    return _proxy_json("POST", "/source-videos/download", json=payload)


@app.route("/source-videos", methods=["GET"])
def source_videos():
    return _proxy_json("GET", "/source-videos")


@app.route("/source-videos/select", methods=["POST"])
def select_source_video():
    payload = request.get_json(silent=True) or {}
    return _proxy_json("POST", "/source-videos/select", json=payload)


@app.route("/existing-clips", methods=["GET"])
def get_existing_clips():
    return _proxy_json("GET", "/existing-clips")


@app.route("/existing-clips", methods=["DELETE"])
def delete_existing_clips():
    return _proxy_json("DELETE", "/existing-clips")


@app.route("/existing-clips/single", methods=["DELETE"])
def delete_single_clip():
    payload = request.get_json(silent=True) or {}
    return _proxy_json("DELETE", "/existing-clips/single", json=payload)


@app.route("/start-process", methods=["POST"])
def proxy_start_process():
    payload = request.get_json(silent=True) or {}
    return _proxy_json("POST", "/start-process", json=payload)


@app.route("/status/<task_id>")
def proxy_task_status(task_id):
    return _proxy_json("GET", f"/status/{task_id}")


@app.route("/clip-ai/start", methods=["POST"])
def proxy_clip_ai_start():
    payload = request.get_json(silent=True) or {}
    return _proxy_json("POST", "/clip-ai/start", json=payload)


@app.route("/clip-ai/status/<task_id>")
def proxy_clip_ai_status(task_id):
    return _proxy_json("GET", f"/clip-ai/status/{task_id}")


@app.route("/single-clip/render", methods=["POST"])
def proxy_single_clip_render():
    source_video = request.files.get("source_video") or request.files.get("video")
    source_url = str(request.form.get("source_url") or "").strip()
    if not source_video and not source_url:
        return jsonify({"error": "Provide either a source_video file or a source_url."}), 400

    files = None
    if source_video:
        files = {
            "source_video": (
                source_video.filename,
                source_video.stream,
                source_video.mimetype or "application/octet-stream",
            )
        }
    data = {
        "overlay_title": str(request.form.get("overlay_title") or "").strip(),
        "source_url": source_url,
    }
    kwargs = {"data": data}
    if files is not None:
        kwargs["files"] = files
    return _proxy_json("POST", "/single-clip/render", **kwargs)


@app.route("/single-clip/status/<task_id>", methods=["GET"])
def proxy_single_clip_status(task_id):
    return _proxy_json("GET", f"/single-clip/status/{task_id}")


@app.route("/single-clip/renders", methods=["GET"])
def proxy_single_clip_renders():
    return _proxy_json("GET", "/single-clip/renders")


@app.route("/centered-mobile/render", methods=["POST"])
def proxy_centered_mobile_render():
    source_video = request.files.get("source_video") or request.files.get("video")
    source_url = str(request.form.get("source_url") or "").strip()
    if not source_video and not source_url:
        return jsonify({"error": "Provide either a source_video file or a source_url."}), 400

    files = None
    if source_video:
        files = {
            "source_video": (
                source_video.filename,
                source_video.stream,
                source_video.mimetype or "application/octet-stream",
            )
        }
    data = {
        "source_url": source_url,
        "tier_list_enabled": str(request.form.get("tier_list_enabled") or "").strip(),
    }
    kwargs = {"data": data}
    if files is not None:
        kwargs["files"] = files
    return _proxy_json("POST", "/centered-mobile/render", **kwargs)


@app.route("/centered-mobile/status/<task_id>", methods=["GET"])
def proxy_centered_mobile_status(task_id):
    return _proxy_json("GET", f"/centered-mobile/status/{task_id}")


@app.route("/centered-mobile/renders", methods=["GET"])
def proxy_centered_mobile_renders():
    return _proxy_json("GET", "/centered-mobile/renders")


@app.route("/videos/<path:task_id>/<filename>")
def proxy_download(task_id, filename):
    backend_url = f"{BACKEND_BASE}/videos/{task_id}/{filename}"
    try:
        response = requests.get(backend_url, stream=True, timeout=REQUEST_TIMEOUT)
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
    except requests.exceptions.RequestException as exc:
        return jsonify({"error": "Could not fetch video file", "details": str(exc)}), 502


@app.route("/abort/<task_id>", methods=["POST", "GET"])
def abort(task_id):
    return _proxy_json("POST", f"/abort/{task_id}")


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=3000, debug=True)
