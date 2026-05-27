import os
import json
from flask import Blueprint, jsonify, request, send_file
from flask_cors import CORS
from celery.result import AsyncResult

from app.tasks import (
    process_videos_task,
    process_clip_ai_task,
    process_centered_mobile_task,
    process_single_clip_task,
    process_sitcom_edit_task,
    celery,
)
from app.state import mark_aborted
from app.state import r as redis_client
from app.scripts.video_import import (
    SOURCE_VIDEO_FILENAME,
    activate_source_video,
    check_ytdlp_connection,
    download_source_from_url,
    get_newest_video_metadata,
    get_active_source_filename,
    get_source_video_path,
    get_video_storage_dir,
    list_source_videos,
    save_uploaded_source,
)
from app.scripts.single_clip_pipeline import (
    CENTERED_MOBILE_RENDER_DIRNAME,
    CENTERED_MOBILE_SOURCE_DIRNAME,
    SINGLE_CLIP_RENDER_DIRNAME,
    SINGLE_CLIP_SOURCE_DIRNAME,
    list_centered_mobile_renders,
    list_single_clip_renders,
    save_uploaded_centered_mobile_source,
    save_uploaded_single_clip_source,
)
from app.scripts.sitcom_pipeline import (
    SITCOM_EDIT_DIRNAME,
    SITCOM_SOURCE_DIRNAME,
    activate_sitcom_source,
    get_active_sitcom_source_filename,
    list_sitcom_edits,
    list_sitcom_sources,
    resolve_sitcom_artifact_path,
    save_uploaded_sitcom_source,
)
from app.scripts.synapse_watcher import (
    clear_auto_synapse_check_lock,
    get_auto_synapse_status,
    queue_auto_synapse_check,
    request_auto_synapse_stop,
    stop_auto_synapse_task,
)


views = Blueprint("views", __name__)
CORS(views)
RESERVED_TOP_LEVEL_DIRS = {
    "sources",
    CENTERED_MOBILE_SOURCE_DIRNAME,
    CENTERED_MOBILE_RENDER_DIRNAME,
    SINGLE_CLIP_SOURCE_DIRNAME,
    SINGLE_CLIP_RENDER_DIRNAME,
    SITCOM_SOURCE_DIRNAME,
    SITCOM_EDIT_DIRNAME,
}


def _video_dir():
    path = get_video_storage_dir()
    os.makedirs(path, exist_ok=True)
    return path


def _truthy(value, default=False):
    if value is None:
        return bool(default)
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value != 0
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


def _top_level_dir(path, base_dir):
    rel = os.path.relpath(path, base_dir)
    if rel in {".", ""}:
        return ""
    return rel.split(os.sep, 1)[0]


def _is_reserved_media_subtree(path, base_dir):
    return _top_level_dir(path, base_dir) in RESERVED_TOP_LEVEL_DIRS


def _safe_video_subdir(task_id):
    raw = (task_id or "").strip().replace("\\", "/").strip("/")
    if raw in {"", ".", "root"}:
        return ""
    parts = [piece for piece in raw.split("/") if piece]
    if ".." in parts:
        raise ValueError("Invalid video directory.")
    return "/".join(parts)


def _json_from_task_exception(info):
    if isinstance(info, dict):
        return info
    if hasattr(info, "args") and info.args:
        first_arg = info.args[0]
        if isinstance(first_arg, dict):
            return first_arg
        if isinstance(first_arg, str):
            try:
                decoded = json.loads(first_arg)
                if isinstance(decoded, dict):
                    return decoded
            except (TypeError, ValueError):
                return None
    return None


def _json_safe_task_info(info):
    decoded = _json_from_task_exception(info)
    if decoded is not None:
        return decoded
    if info is None or isinstance(info, (str, int, float, bool)):
        return info
    if isinstance(info, dict):
        return {str(key): _json_safe_task_info(value) for key, value in info.items()}
    if isinstance(info, (list, tuple)):
        return [_json_safe_task_info(value) for value in info]
    return str(info)


def _task_progress_payload(info):
    safe_info = _json_safe_task_info(info)
    if isinstance(safe_info, dict):
        return safe_info
    if safe_info in (None, "", [], {}):
        return {}
    return {"message": str(safe_info)}


def _task_status_payload(task):
    state = str(task.state or "")
    info = task.info

    if state == "PENDING":
        return {"state": "PENDING", "status": "Waiting to start..."}
    if state == "PROGRESS":
        return {"state": "PROGRESS", "progress": _task_progress_payload(info)}
    if state == "RENDERING":
        return {"state": "RENDERING", "progress": _task_progress_payload(info)}
    if state == "SUCCESS":
        return {"state": "SUCCESS", "result": _json_safe_task_info(info)}
    if state == "FAILURE":
        decoded = _json_from_task_exception(info)
        if decoded is not None:
            return {"state": "FAILURE", "error": decoded}
        return {"state": "FAILURE", "status": str(info)}
    if state == "REVOKED":
        payload = {"state": "REVOKED", "status": "Task was stopped."}
        safe_info = _json_safe_task_info(info)
        if safe_info not in (None, "", [], {}):
            if isinstance(safe_info, dict):
                payload["progress"] = safe_info
            else:
                payload["details"] = str(safe_info)
        return payload

    safe_info = _json_safe_task_info(info)
    if safe_info not in (None, "", [], {}):
        return {"state": state, "progress": _task_progress_payload(safe_info)}
    return {"state": state}


def _task_status_response(task):
    return jsonify(_task_status_payload(task))


def _load_existing_clip_ai_result(clip_dir):
    analysis_path = os.path.join(clip_dir, "ai_analysis.json")
    if not os.path.exists(analysis_path):
        return None
    try:
        with open(analysis_path, "r", encoding="utf-8") as handle:
            parsed = json.load(handle)
    except (OSError, ValueError, json.JSONDecodeError):
        return None

    if not isinstance(parsed, dict):
        return None

    artifacts = parsed.get("artifacts") if isinstance(parsed.get("artifacts"), dict) else {}
    artifacts = dict(artifacts)
    edited_path = os.path.join(clip_dir, "ai_edited.mp4")
    if not os.path.exists(edited_path):
        artifacts["edited_video"] = ""
    warnings = parsed.get("warnings") if isinstance(parsed.get("warnings"), list) else []
    return {
        "status": parsed.get("status") or "",
        "title": parsed.get("title") or "",
        "description": parsed.get("description") or "",
        "overlay_title": parsed.get("overlay_title") or "",
        "warnings": warnings,
        "artifacts": artifacts,
        "processed_at_utc": parsed.get("processed_at_utc"),
    }


def _clip_download_url(subdir, filename):
    safe_subdir = (subdir or "").replace("\\", "/").strip("/") or "root"
    return f"/videos/{safe_subdir}/{os.path.basename(filename)}"


def _auto_synapse_status_payload():
    status = dict(get_auto_synapse_status() or {})
    active_task_id = str(status.get("active_task_id") or "").strip()
    if not active_task_id:
        return status

    task_payload = _task_status_payload(AsyncResult(active_task_id, app=celery))
    status["active_task"] = {"id": active_task_id, **task_payload}
    if task_payload.get("state"):
        status["active_task_state"] = task_payload["state"]
    return status


@views.route("/healthz", methods=["GET"])
def healthz():
    return jsonify({"status": "ok"})


@views.route("/start-process", methods=["POST"])
def start_process():
    payload = request.get_json(silent=True) or {}
    source_url = (payload.get("source_url") or "").strip()
    source_filename = (payload.get("source_filename") or "").strip()
    auto_subtitles = _truthy(payload.get("auto_subtitles"), default=False)
    task = process_videos_task.apply_async(
        kwargs={
            "source_url": source_url,
            "source_filename": source_filename,
            "auto_subtitles": auto_subtitles,
        }
    )
    return jsonify({"task_id": task.id}), 202


@views.route("/latest-synapse-video", methods=["GET"])
def latest_synapse_video():
    try:
        return jsonify(get_newest_video_metadata())
    except Exception as exc:
        return jsonify({"error": str(exc)}), 502


@views.route("/yt-dlp/check", methods=["GET", "POST"])
def yt_dlp_check():
    if request.method == "POST":
        payload = request.get_json(silent=True) or {}
        source_url = str(payload.get("url") or payload.get("source_url") or "").strip()
    else:
        source_url = str(request.args.get("url") or "").strip()
    if not source_url:
        source_url = str(request.args.get("source_url") or "").strip()
    if not source_url:
        return jsonify({"ok": False, "error": "Missing source URL."}), 400

    result = check_ytdlp_connection(source_url, download=False)
    return jsonify(result), 200 if result.get("ok") else 502


@views.route("/auto-synapse/status", methods=["GET"])
def auto_synapse_status():
    return jsonify(_auto_synapse_status_payload())


@views.route("/auto-synapse/diagnostics", methods=["GET"])
def auto_synapse_diagnostics():
    status = _auto_synapse_status_payload()

    redis_ok = False
    redis_error = ""
    try:
        redis_ok = bool(redis_client.ping())
    except Exception as exc:
        redis_error = str(exc)

    celery_workers = []
    celery_error = ""
    try:
        celery_workers = celery.control.ping(timeout=1.0) or []
    except Exception as exc:
        celery_error = str(exc)

    source_url = str(request.args.get("url") or status.get("latest_url") or "").strip()
    ytdlp = {"checked": False}
    if _truthy(request.args.get("check_ytdlp"), default=False):
        ytdlp = check_ytdlp_connection(source_url, download=False)
        ytdlp["checked"] = True

    return jsonify(
        {
            "watcher": status,
            "active_task": status.get("active_task") or {},
            "redis": {
                "ok": redis_ok,
                "error": redis_error,
            },
            "celery": {
                "ok": bool(celery_workers),
                "workers": celery_workers,
                "error": celery_error,
            },
            "yt_dlp": ytdlp,
        }
    )


@views.route("/auto-synapse/check", methods=["POST"])
def auto_synapse_check():
    queued = queue_auto_synapse_check(trigger="manual_api")
    payload = {
        "status": "queued" if queued else "disabled",
        "watcher": _auto_synapse_status_payload(),
    }
    return jsonify(payload), 202 if queued else 200


@views.route("/auto-synapse/stop", methods=["POST"])
def auto_synapse_stop():
    status = get_auto_synapse_status()
    active_task_id = str(status.get("active_task_id") or "").strip()
    current_state = str(status.get("state") or "").strip().lower()
    request_auto_synapse_stop(reason="Auto Synapse stop requested by user.")
    if not active_task_id:
        if current_state in {"error", "download_failed", "idle", "disabled"}:
            clear_auto_synapse_check_lock(reason="Auto Synapse reset requested without an active Celery task.")
            watcher = stop_auto_synapse_task(reason="Auto Synapse watcher reset by user.")
            return jsonify({"status": "reset", "watcher": watcher}), 200
        stop_status = "stopping" if current_state in {"downloading", "processing", "stopping"} else "idle"
        return jsonify({"status": stop_status, "watcher": _auto_synapse_status_payload()}), 200

    abort_mark = "requested"
    abort_details = ""
    try:
        mark_aborted(active_task_id)
    except Exception as exc:
        abort_mark = "failed"
        abort_details = str(exc)

    task = AsyncResult(active_task_id, app=celery)
    task_snapshot = _task_status_payload(task)
    task_state = str(task_snapshot.get("state") or "")
    if task_state in {"SUCCESS", "FAILURE", "REVOKED"}:
        watcher = stop_auto_synapse_task(
            active_task_id,
            reason=task_snapshot.get("status") or task_snapshot.get("details") or "Task was already finished.",
        )
        payload = {
            "status": "stopped",
            "task_id": active_task_id,
            "abort_mark": abort_mark,
            "revoke": "not_needed",
            "watcher": watcher,
        }
        if abort_details:
            payload["abort_mark_details"] = abort_details
        return jsonify(payload), 200

    try:
        task.revoke(terminate=True)
    except Exception as exc:
        payload = {
            "status": "stop_failed",
            "task_id": active_task_id,
            "abort_mark": abort_mark,
            "revoke": "failed",
            "details": str(exc),
            "watcher": _auto_synapse_status_payload(),
        }
        if abort_details:
            payload["abort_mark_details"] = abort_details
        return jsonify(payload), 202

    watcher = stop_auto_synapse_task(active_task_id, reason="Task was stopped by request.")
    payload = {
        "status": "stopped",
        "task_id": active_task_id,
        "abort_mark": abort_mark,
        "revoke": "requested",
        "watcher": watcher,
    }
    if abort_details:
        payload["abort_mark_details"] = abort_details
    return jsonify(payload), 200


@views.route("/upload-source-video", methods=["POST"])
def upload_source_video():
    if "source_video" not in request.files:
        return jsonify({"error": "Missing form field: source_video"}), 400

    source_video = request.files["source_video"]
    if not source_video or source_video.filename == "":
        return jsonify({"error": "No file selected."}), 400

    try:
        saved_name, source_path = save_uploaded_source(source_video)
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400

    return jsonify(
        {
            "status": "ok",
            "filename": saved_name,
            "source_path": source_path,
            "size_bytes": os.path.getsize(source_path),
        }
    )


@views.route("/source-videos/download", methods=["POST"])
def download_source_video():
    payload = request.get_json(silent=True) or {}
    source_url = str(payload.get("url") or payload.get("source_url") or "").strip()
    if not source_url:
        return jsonify({"error": "Missing source URL."}), 400

    try:
        saved_name, source_path, clean_url = download_source_from_url(source_url)
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
    except Exception as exc:
        return jsonify({"error": str(exc)}), 502

    return jsonify(
        {
            "status": "ok",
            "filename": saved_name,
            "source_path": source_path,
            "size_bytes": os.path.getsize(source_path),
            "source_url": clean_url,
        }
    )


@views.route("/source-videos", methods=["GET"])
def source_videos():
    return jsonify(
        {
            "active": get_active_source_filename(),
            "videos": list_source_videos(),
        }
    )


@views.route("/source-videos/select", methods=["POST"])
def select_source_video():
    payload = request.get_json(silent=True) or {}
    file_name = payload.get("filename", "")
    try:
        active_name, active_path = activate_source_video(file_name)
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
    except FileNotFoundError:
        return jsonify({"error": "Source video not found."}), 404

    return jsonify(
        {
            "status": "ok",
            "active": active_name,
            "source_path": active_path,
        }
    )


@views.route("/status/<task_id>")
def task_status(task_id):
    return _task_status_response(AsyncResult(task_id, app=celery))


@views.route("/results/<task_id>")
def list_results(task_id):
    video_dir = os.path.join(_video_dir(), task_id)
    if not os.path.exists(video_dir):
        return jsonify([])

    files = [f for f in os.listdir(video_dir) if f.endswith(".mp4")]
    return jsonify(files)


@views.route("/videos/<path:task_id>/<filename>")
def download_clip(task_id, filename):
    safe_filename = os.path.basename(filename)
    base_dir = os.path.realpath(_video_dir())
    try:
        safe_subdir = _safe_video_subdir(task_id)
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400

    target_dir = os.path.realpath(os.path.join(base_dir, safe_subdir))
    try:
        if os.path.commonpath([base_dir, target_dir]) != base_dir:
            return jsonify({"error": "Invalid video directory."}), 400
    except ValueError:
        return jsonify({"error": "Invalid video directory."}), 400

    full_path = os.path.realpath(os.path.join(target_dir, safe_filename))
    try:
        if os.path.commonpath([target_dir, full_path]) != target_dir:
            return jsonify({"error": "Invalid file path."}), 400
    except ValueError:
        return jsonify({"error": "Invalid file path."}), 400

    if not os.path.exists(full_path):
        return jsonify({"error": "File not found", "path": full_path}), 404

    try:
        return send_file(full_path, as_attachment=False)
    except Exception as exc:
        return jsonify({"error": "Send failed", "details": str(exc)}), 500


@views.route("/abort/<task_id>", methods=["POST"])
def abort_task(task_id):
    abort_mark = "requested"
    abort_details = None
    try:
        mark_aborted(task_id)
    except Exception as exc:
        abort_mark = "failed"
        abort_details = str(exc)
    task = AsyncResult(task_id, app=celery)
    try:
        task.revoke(terminate=True)
    except Exception as exc:
        response = {
            "status": "abort_marked" if abort_mark == "requested" else "abort_request_failed",
            "abort_mark": abort_mark,
            "revoke": "failed",
            "details": str(exc),
        }
        if abort_details:
            response["abort_mark_details"] = abort_details
        return jsonify(response), 202
    response = {
        "status": "aborting",
        "abort_mark": abort_mark,
        "revoke": "requested",
    }
    if abort_details:
        response["abort_mark_details"] = abort_details
    return jsonify(response)


@views.route("/existing-clips", methods=["GET"])
def existing_clips():
    base_dir = _video_dir()
    clips = []
    for root, _, files in os.walk(base_dir):
        if _is_reserved_media_subtree(root, base_dir):
            continue
        for file_name in files:
            if not file_name.endswith(".mp4"):
                continue
            if file_name == SOURCE_VIDEO_FILENAME:
                continue
            if file_name.startswith("ai_"):
                continue
            rel_path = os.path.relpath(os.path.join(root, file_name), base_dir)
            subdir = os.path.dirname(rel_path)
            ai_result = _load_existing_clip_ai_result(root)
            artifacts = ai_result.get("artifacts") if isinstance(ai_result, dict) else {}
            final_video_url = ""
            if isinstance(artifacts, dict):
                final_video_url = artifacts.get("edited_video") or ""
            clips.append(
                {
                    "subdir": subdir,
                    "filename": file_name,
                    "original_video_url": _clip_download_url(subdir, file_name),
                    "final_video_url": final_video_url,
                    "ai_result": ai_result,
                }
            )

    return jsonify(clips)


@views.route("/existing-clips", methods=["DELETE"])
def delete_existing_clips():
    base_dir = _video_dir()
    deleted_files = 0
    deleted_dirs = 0

    for root, _, files in os.walk(base_dir):
        if _is_reserved_media_subtree(root, base_dir):
            continue
        for file_name in files:
            full_path = os.path.join(root, file_name)
            try:
                os.remove(full_path)
                deleted_files += 1
            except FileNotFoundError:
                continue
            except Exception as exc:
                return (
                    jsonify(
                        {
                            "error": f"Could not delete file: {full_path}",
                            "details": str(exc),
                            "deleted_files": deleted_files,
                            "deleted_dirs": deleted_dirs,
                        }
                    ),
                    500,
                )

    for root, dirs, _ in os.walk(base_dir, topdown=False):
        if _is_reserved_media_subtree(root, base_dir):
            continue
        for dir_name in dirs:
            dir_path = os.path.join(root, dir_name)
            if _is_reserved_media_subtree(dir_path, base_dir):
                continue
            try:
                if not os.listdir(dir_path):
                    os.rmdir(dir_path)
                    deleted_dirs += 1
            except Exception:
                continue

    return jsonify(
        {
            "status": "ok",
            "deleted_files": deleted_files,
            "deleted_dirs": deleted_dirs,
        }
    )


@views.route("/existing-clips/single", methods=["DELETE"])
def delete_single_clip():
    payload = request.get_json(silent=True) or {}
    subdir = (payload.get("subdir") or "").strip().replace("\\", "/").strip("/")
    filename = os.path.basename((payload.get("filename") or "").strip())
    if not filename:
        return jsonify({"error": "Missing filename."}), 400
    if not filename.lower().endswith(".mp4"):
        return jsonify({"error": "Invalid clip filename."}), 400
    if ".." in [piece for piece in subdir.split("/") if piece]:
        return jsonify({"error": "Invalid clip path."}), 400

    base_dir = os.path.realpath(_video_dir())
    clip_dir = os.path.realpath(os.path.join(base_dir, subdir))
    if not clip_dir.startswith(base_dir):
        return jsonify({"error": "Invalid clip directory."}), 400
    if _is_reserved_media_subtree(clip_dir, base_dir):
        return jsonify({"error": "Cannot delete from protected media directory."}), 400

    clip_path = os.path.realpath(os.path.join(clip_dir, filename))
    if not clip_path.startswith(clip_dir):
        return jsonify({"error": "Invalid clip file path."}), 400
    if not os.path.exists(clip_path):
        return jsonify({"error": "Clip not found."}), 404

    try:
        os.remove(clip_path)
    except Exception as exc:
        return jsonify({"error": "Could not delete clip.", "details": str(exc)}), 500

    deleted_related = []
    try:
        for file_name in os.listdir(clip_dir):
            if not file_name.startswith("ai_"):
                continue
            full_path = os.path.join(clip_dir, file_name)
            if not os.path.isfile(full_path):
                continue
            os.remove(full_path)
            deleted_related.append(file_name)
    except Exception as exc:
        return jsonify(
            {
                "status": "partial",
                "deleted_clip": filename,
                "error": "Deleted clip but failed while deleting AI artifacts.",
                "details": str(exc),
                "deleted_ai_files": deleted_related,
            }
        ), 500

    removed_dir = False
    try:
        if clip_dir != base_dir and not os.listdir(clip_dir):
            os.rmdir(clip_dir)
            removed_dir = True
    except Exception:
        removed_dir = False

    return jsonify(
        {
            "status": "ok",
            "deleted_clip": filename,
            "deleted_ai_files": deleted_related,
            "removed_dir": removed_dir,
        }
    )


@views.route("/clip-ai/start", methods=["POST"])
def start_clip_ai():
    payload = request.get_json(silent=True) or {}
    subdir = (payload.get("subdir") or "").strip()
    filename = (payload.get("filename") or "").strip()
    show_items = bool(payload.get("show_items", False))
    vine_boom = bool(payload.get("vine_boom", False))
    overlay_title = str(payload.get("overlay_title") or "").strip()
    if not filename:
        return jsonify({"error": "Missing filename."}), 400
    task = process_clip_ai_task.apply_async(
        kwargs={
            "subdir": subdir,
            "filename": filename,
            "show_items": show_items,
            "vine_boom": vine_boom,
            "overlay_title": overlay_title,
        }
    )
    return jsonify({"task_id": task.id}), 202


@views.route("/clip-ai/status/<task_id>")
def clip_ai_status(task_id):
    return _task_status_response(AsyncResult(task_id, app=celery))


@views.route("/single-clip/render", methods=["POST"])
def single_clip_render():
    source_video = request.files.get("source_video") or request.files.get("video")
    overlay_title = str(request.form.get("overlay_title") or "").strip()
    source_url = str(request.form.get("source_url") or "").strip()

    if source_video and source_video.filename == "":
        source_video = None
    if not source_video and not source_url:
        return jsonify({"error": "Provide either a source_video file or a source_url."}), 400

    saved_name = ""
    source_path = ""
    size_bytes = 0
    if source_video:
        try:
            saved_name, source_path = save_uploaded_single_clip_source(source_video)
        except ValueError as exc:
            return jsonify({"error": str(exc)}), 400
        size_bytes = os.path.getsize(source_path)

    task = process_single_clip_task.apply_async(
        kwargs={
            "source_filename": saved_name,
            "source_url": source_url,
            "overlay_title": overlay_title,
        }
    )
    return (
        jsonify(
            {
                "task_id": task.id,
                "filename": saved_name,
                "source_path": source_path,
                "size_bytes": size_bytes,
                "source_url": source_url,
            }
        ),
        202,
    )


@views.route("/single-clip/status/<task_id>", methods=["GET"])
def single_clip_status(task_id):
    return _task_status_response(AsyncResult(task_id, app=celery))


@views.route("/single-clip/renders", methods=["GET"])
def single_clip_renders():
    return jsonify(list_single_clip_renders())


@views.route("/centered-mobile/render", methods=["POST"])
def centered_mobile_render():
    source_video = request.files.get("source_video") or request.files.get("video")
    source_url = str(request.form.get("source_url") or "").strip()
    if source_video and source_video.filename == "":
        source_video = None
    if not source_video and not source_url:
        return jsonify({"error": "Provide either a source_video file or a source_url."}), 400
    tier_list_enabled = _truthy(request.form.get("tier_list_enabled"), default=False)

    saved_name = ""
    source_path = ""
    size_bytes = 0
    if source_video:
        try:
            saved_name, source_path = save_uploaded_centered_mobile_source(source_video)
        except ValueError as exc:
            return jsonify({"error": str(exc)}), 400
        size_bytes = os.path.getsize(source_path)

    task = process_centered_mobile_task.apply_async(
        kwargs={
            "source_filename": saved_name,
            "source_url": source_url,
            "tier_list_enabled": tier_list_enabled,
        }
    )
    return (
        jsonify(
            {
                "task_id": task.id,
                "filename": saved_name,
                "source_path": source_path,
                "size_bytes": size_bytes,
                "source_url": source_url,
                "tier_list_enabled": tier_list_enabled,
            }
        ),
        202,
    )


@views.route("/centered-mobile/status/<task_id>", methods=["GET"])
def centered_mobile_status(task_id):
    return _task_status_response(AsyncResult(task_id, app=celery))


@views.route("/centered-mobile/renders", methods=["GET"])
def centered_mobile_renders():
    return jsonify(list_centered_mobile_renders())


@views.route("/sitcom/upload-source", methods=["POST"])
def upload_sitcom_source():
    source_video = request.files.get("source_video") or request.files.get("video")
    if not source_video:
        return jsonify({"error": "Missing form field: source_video"}), 400
    if source_video.filename == "":
        return jsonify({"error": "No file selected."}), 400

    try:
        saved_name, source_path = save_uploaded_sitcom_source(source_video)
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400

    return jsonify(
        {
            "status": "ok",
            "filename": saved_name,
            "source_path": source_path,
            "size_bytes": os.path.getsize(source_path),
        }
    )


@views.route("/sitcom/sources", methods=["GET"])
def sitcom_sources():
    return jsonify(
        {
            "active": get_active_sitcom_source_filename(),
            "videos": list_sitcom_sources(),
        }
    )


@views.route("/sitcom/sources/select", methods=["POST"])
def select_sitcom_source():
    payload = request.get_json(silent=True) or {}
    file_name = payload.get("filename", "")
    try:
        active_name, active_path = activate_sitcom_source(file_name)
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
    except FileNotFoundError:
        return jsonify({"error": "Source video not found."}), 404

    return jsonify(
        {
            "status": "ok",
            "active": active_name,
            "source_path": active_path,
        }
    )


@views.route("/sitcom/edit/start", methods=["POST"])
def start_sitcom_edit():
    payload = request.get_json(silent=True) or {}
    source_filename = str(payload.get("source_filename") or "").strip()
    ai_remove_query = str(payload.get("ai_remove_query") or "").strip()
    ai_auto_edit = _truthy(payload.get("ai_auto_edit"), default=False)
    mobile_mode = str(payload.get("mobile_mode") or "blur").strip().lower()
    subtitle_language = str(payload.get("subtitle_language") or "").strip()
    generate_subtitles = _truthy(payload.get("generate_subtitles"), default=True)
    burn_subtitles = _truthy(payload.get("burn_subtitles"), default=True)
    split_into_clips = _truthy(payload.get("split_into_clips"), default=True)
    if not generate_subtitles:
        burn_subtitles = False

    try:
        target_keep_ratio = float(payload.get("target_keep_ratio", 0.72))
    except (TypeError, ValueError):
        return jsonify({"error": "target_keep_ratio must be a number between 0.35 and 0.95."}), 400
    target_keep_ratio = max(0.35, min(0.95, target_keep_ratio))

    remove_ranges = payload.get("remove_ranges")
    if isinstance(remove_ranges, str):
        remove_ranges = [line.strip() for line in remove_ranges.splitlines() if line.strip()]
    if remove_ranges is None:
        remove_ranges = []
    if not isinstance(remove_ranges, list):
        return jsonify({"error": "remove_ranges must be a list."}), 400

    if mobile_mode not in {"blur", "crop", "pad"}:
        return jsonify({"error": "mobile_mode must be one of: blur, crop, pad."}), 400

    try:
        clip_max_seconds = float(payload.get("clip_max_seconds", 60.0))
        clip_min_seconds = float(payload.get("clip_min_seconds", 12.0))
    except (TypeError, ValueError):
        return jsonify({"error": "clip_max_seconds/clip_min_seconds must be numeric."}), 400
    clip_max_seconds = max(10.0, min(600.0, clip_max_seconds))
    clip_min_seconds = max(3.0, min(240.0, clip_min_seconds))

    task = process_sitcom_edit_task.apply_async(
        kwargs={
            "source_filename": source_filename,
            "remove_ranges": remove_ranges,
            "ai_remove_query": ai_remove_query,
            "ai_auto_edit": ai_auto_edit,
            "target_keep_ratio": target_keep_ratio,
            "mobile_mode": mobile_mode,
            "split_into_clips": split_into_clips,
            "clip_max_seconds": clip_max_seconds,
            "clip_min_seconds": clip_min_seconds,
            "generate_subtitles": generate_subtitles,
            "burn_subtitles": burn_subtitles,
            "subtitle_language": subtitle_language,
        }
    )
    return jsonify({"task_id": task.id}), 202


@views.route("/sitcom/edit/status/<task_id>", methods=["GET"])
def sitcom_edit_status(task_id):
    return _task_status_response(AsyncResult(task_id, app=celery))


@views.route("/sitcom/edits", methods=["GET"])
def sitcom_edits():
    return jsonify(list_sitcom_edits())


@views.route("/sitcom/files/<task_id>/<filename>", methods=["GET"])
def sitcom_artifact(task_id, filename):
    try:
        full_path = resolve_sitcom_artifact_path(task_id, filename)
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
    except FileNotFoundError:
        return jsonify({"error": "Artifact not found."}), 404

    try:
        return send_file(full_path, as_attachment=False)
    except Exception as exc:
        return jsonify({"error": "Send failed", "details": str(exc)}), 500
