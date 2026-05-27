import json
import os
import re
import subprocess
from datetime import datetime

from moviepy.editor import VideoFileClip
try:
    from yt_dlp import YoutubeDL
except ImportError:
    YoutubeDL = None

from .ai_pipeline import AI_ARTIFACT_FILENAMES, run_ai_pipeline_for_clip
from .video_editor import render_highlight_clip, seconds_to_timestamp
from .video_import import get_video_storage_dir


SINGLE_CLIP_SOURCE_DIRNAME = "single_clip_sources"
SINGLE_CLIP_RENDER_DIRNAME = "single_clip_renders"
SINGLE_CLIP_RESULT_FILENAME = "single_clip_result.json"

CENTERED_MOBILE_SOURCE_DIRNAME = "centered_mobile_sources"
CENTERED_MOBILE_RENDER_DIRNAME = "centered_mobile_renders"
CENTERED_MOBILE_RESULT_FILENAME = "centered_mobile_result.json"

SUPPORTED_VIDEO_EXTENSIONS = {".mp4", ".mov", ".mkv", ".webm", ".m4v"}
DEFAULT_AUDIO_BITRATE = "160k"
DEFAULT_OUTPUT_CRF = "18"


def get_single_clip_source_dir():
    return os.path.join(get_video_storage_dir(), SINGLE_CLIP_SOURCE_DIRNAME)


def get_single_clip_render_dir():
    return os.path.join(get_video_storage_dir(), SINGLE_CLIP_RENDER_DIRNAME)


def get_centered_mobile_source_dir():
    return os.path.join(get_video_storage_dir(), CENTERED_MOBILE_SOURCE_DIRNAME)


def get_centered_mobile_render_dir():
    return os.path.join(get_video_storage_dir(), CENTERED_MOBILE_RENDER_DIRNAME)


def _read_json_file_dict(path):
    if not os.path.exists(path):
        return {}
    try:
        with open(path, "r", encoding="utf-8") as handle:
            loaded = json.load(handle)
            return loaded if isinstance(loaded, dict) else {}
    except (OSError, ValueError, json.JSONDecodeError):
        return {}


def _clean_filename(value):
    base = os.path.basename(value or "")
    return re.sub(r"[^A-Za-z0-9._-]", "_", base)


def _ensure_unique_filename(directory, file_name):
    candidate = file_name
    stem, ext = os.path.splitext(file_name)
    suffix = 1
    while os.path.exists(os.path.join(directory, candidate)):
        candidate = f"{stem}_{suffix}{ext}"
        suffix += 1
    return candidate


def _safe_task_id(task_id):
    safe_task_id = os.path.basename(str(task_id or "").strip())
    if not safe_task_id:
        raise ValueError("Missing task id.")
    return safe_task_id


def _task_subdir(render_dirname, task_id):
    return f"{render_dirname}/{_safe_task_id(task_id)}"


def _task_dir_for(render_root, task_id):
    return os.path.join(render_root, _safe_task_id(task_id))


def _task_file_path(render_root, task_id, file_name):
    return os.path.join(_task_dir_for(render_root, task_id), file_name)


def _task_file_url(render_dirname, task_id, file_name):
    return f"/videos/{_task_subdir(render_dirname, task_id)}/{file_name}"


def _optional_task_file_url(render_root, render_dirname, task_id, file_name):
    if not os.path.exists(_task_file_path(render_root, task_id, file_name)):
        return None
    return _task_file_url(render_dirname, task_id, file_name)


def _probe_video_duration_seconds(video_path):
    with VideoFileClip(video_path) as clip:
        return float(clip.duration or 0.0)


def _probe_video_metadata(video_path):
    with VideoFileClip(video_path) as clip:
        return {
            "duration_seconds": float(clip.duration or 0.0),
            "width": int(clip.w or 0),
            "height": int(clip.h or 0),
        }


def _persist_result(render_root, task_id, result, result_filename):
    task_dir = _task_dir_for(render_root, task_id)
    os.makedirs(task_dir, exist_ok=True)

    result_path = os.path.join(task_dir, result_filename)
    analysis_path = os.path.join(task_dir, AI_ARTIFACT_FILENAMES["analysis_json"])
    for path in (result_path, analysis_path):
        with open(path, "w", encoding="utf-8") as handle:
            json.dump(result, handle, ensure_ascii=False, indent=2)


def _save_uploaded_clip_source(file_storage, source_dir):
    os.makedirs(source_dir, exist_ok=True)

    original = _clean_filename(file_storage.filename)
    if not original:
        raise ValueError("Invalid file name.")
    ext = os.path.splitext(original)[1].lower()
    if ext not in SUPPORTED_VIDEO_EXTENSIONS:
        raise ValueError(f"Unsupported file type: {ext}")

    unique_name = _ensure_unique_filename(source_dir, original)
    final_path = os.path.join(source_dir, unique_name)
    temp_path = final_path + ".upload"
    file_storage.save(temp_path)
    os.replace(temp_path, final_path)
    return unique_name, final_path


def _youtube_source_stem(title, video_id):
    base_title = _clean_filename(title or "youtube_clip")
    stem = os.path.splitext(base_title)[0]
    if video_id:
        stem = f"{stem}_{_clean_filename(video_id)}"
    stem = stem.strip("._-")
    return stem[:180] or "youtube_clip"


def _download_youtube_source(source_url, source_dir, progress_callback=None):
    if YoutubeDL is None:
        raise RuntimeError("yt-dlp is not installed in the backend image.")

    clean_url = str(source_url or "").strip()
    if not clean_url:
        raise ValueError("Missing source URL.")

    os.makedirs(source_dir, exist_ok=True)

    _emit(progress_callback, "SINGLE_DOWNLOADING", "Fetching YouTube video metadata.")
    metadata_opts = {
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
        "extract_flat": False,
    }
    with YoutubeDL(metadata_opts) as metadata_ydl:
        info = metadata_ydl.extract_info(clean_url, download=False)

    if not isinstance(info, dict):
        raise RuntimeError("Could not read YouTube metadata.")

    video_id = str(info.get("id") or "").strip()
    title = str(info.get("title") or video_id or "youtube_clip").strip()
    stem = _youtube_source_stem(title, video_id)
    unique_stem = _ensure_unique_filename(source_dir, f"{stem}.mp4")
    unique_stem = os.path.splitext(unique_stem)[0]
    output_template = os.path.join(source_dir, f"{unique_stem}.%(ext)s")

    def _progress_hook(data):
        status = data.get("status")
        if status == "downloading":
            downloaded = float(data.get("downloaded_bytes") or 0.0)
            total = float(
                data.get("total_bytes")
                or data.get("total_bytes_estimate")
                or 0.0
            )
            if total > 0:
                percent = min(100, int((downloaded / total) * 100))
                message = f"Downloading YouTube clip ({percent}%)."
            else:
                message = "Downloading YouTube clip..."
            _emit(
                progress_callback,
                "SINGLE_DOWNLOADING",
                message,
                {
                    "downloaded_bytes": int(downloaded),
                    "total_bytes": int(total),
                },
            )
        elif status == "finished":
            _emit(progress_callback, "SINGLE_DOWNLOADING", "Download finished. Preparing render.")

    download_opts = {
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
        "outtmpl": output_template,
        "restrictfilenames": True,
        "merge_output_format": "mp4",
        "format": "bv*+ba/b",
        "progress_hooks": [_progress_hook],
    }

    with YoutubeDL(download_opts) as ydl:
        downloaded_info = ydl.extract_info(clean_url, download=True)

    final_path = None
    requested_downloads = downloaded_info.get("requested_downloads") if isinstance(downloaded_info, dict) else None
    if isinstance(requested_downloads, list):
        for item in requested_downloads:
            candidate = item.get("filepath") if isinstance(item, dict) else None
            if candidate and os.path.exists(candidate):
                final_path = candidate
                break

    if not final_path:
        for ext in (".mp4", ".mkv", ".webm", ".mov", ".m4v"):
            candidate = os.path.join(source_dir, f"{unique_stem}{ext}")
            if os.path.exists(candidate):
                final_path = candidate
                break

    if not final_path:
        raise RuntimeError("yt-dlp finished but no downloaded file was found.")

    return os.path.basename(final_path), final_path, clean_url


def _resolve_uploaded_clip_source_path(file_name, source_dir):
    clean_name = _clean_filename(file_name)
    if not clean_name:
        raise ValueError("Missing filename.")
    source_path = os.path.join(source_dir, clean_name)
    if not os.path.exists(source_path):
        raise FileNotFoundError(clean_name)
    return source_path


def _list_render_results(render_root, render_dirname, result_filename):
    os.makedirs(render_root, exist_ok=True)

    items = []
    for entry in os.listdir(render_root):
        task_dir = os.path.join(render_root, entry)
        if not os.path.isdir(task_dir):
            continue

        result = _read_json_file_dict(os.path.join(task_dir, result_filename))
        if not result:
            continue

        artifacts = result.get("artifacts") if isinstance(result.get("artifacts"), dict) else {}
        rendered_video = artifacts.get("rendered_video") or _optional_task_file_url(
            render_root,
            render_dirname,
            entry,
            "clip_0.mp4",
        )
        edited_video = artifacts.get("edited_video") or _optional_task_file_url(
            render_root,
            render_dirname,
            entry,
            AI_ARTIFACT_FILENAMES["edited_video"],
        )
        preview_video = edited_video or rendered_video
        if not preview_video:
            continue

        items.append(
            {
                "task_id": entry,
                "source_filename": result.get("source_filename")
                or (result.get("clip_context") or {}).get("source_video", ""),
                "source_url": result.get("source_url", ""),
                "title": result.get("title", ""),
                "description": result.get("description", ""),
                "duration_seconds": result.get("duration_seconds"),
                "processed_at_utc": result.get("processed_at_utc")
                or datetime.utcfromtimestamp(os.path.getmtime(task_dir)).isoformat() + "Z",
                "video": preview_video,
                "rendered_video": rendered_video,
                "edited_video": edited_video,
                "subtitles_srt": artifacts.get("subtitles_srt")
                or _optional_task_file_url(
                    render_root,
                    render_dirname,
                    entry,
                    AI_ARTIFACT_FILENAMES["subtitles_srt"],
                ),
                "subtitles_vtt": artifacts.get("subtitles_vtt")
                or _optional_task_file_url(
                    render_root,
                    render_dirname,
                    entry,
                    AI_ARTIFACT_FILENAMES["subtitles_vtt"],
                ),
                "analysis_json": artifacts.get("analysis_json")
                or _optional_task_file_url(
                    render_root,
                    render_dirname,
                    entry,
                    AI_ARTIFACT_FILENAMES["analysis_json"],
                ),
                "tier_list_enabled": bool(result.get("tier_list_enabled", False)),
                "tier_list_entries": result.get("tier_list_entries", []),
                "warnings": result.get("warnings", []),
            }
        )

    items.sort(key=lambda item: item.get("processed_at_utc", ""), reverse=True)
    return items


def _output_crf_value():
    return os.getenv("LEAGUECLIPS_OUTPUT_CRF", DEFAULT_OUTPUT_CRF)


def _h264_aac_encode_args(audio_bitrate=DEFAULT_AUDIO_BITRATE):
    return [
        "-c:v",
        "libx264",
        "-preset",
        "veryfast",
        "-crf",
        _output_crf_value(),
        "-pix_fmt",
        "yuv420p",
        "-c:a",
        "aac",
        "-b:a",
        audio_bitrate,
    ]


def _run_cmd(args):
    completed = subprocess.run(args, capture_output=True, text=True)
    if completed.returncode != 0:
        stderr = (completed.stderr or "").strip()
        stdout = (completed.stdout or "").strip()
        raise RuntimeError(stderr or stdout or f"Command failed: {' '.join(args)}")
    return completed


def _emit(progress_callback, state, message, extra=None):
    if not progress_callback:
        return
    payload = {"message": message}
    if extra:
        payload.update(extra)
    progress_callback(state, payload)


def _write_clip_context(task_dir, source_filename, clip_label, duration_seconds, extra=None):
    payload = {
        "clip_index": 0,
        "label": clip_label,
        "start_timestamp": "00:00",
        "end_timestamp": seconds_to_timestamp(duration_seconds),
        "source_video": os.path.basename(source_filename),
    }
    if extra:
        payload.update(extra)

    with open(os.path.join(task_dir, "clip_context.json"), "w", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=2)


def _centered_mobile_layout(source_width, source_height):
    source_width = max(1, int(source_width or 0))
    source_height = max(1, int(source_height or 0))
    scale = min(1080.0 / float(source_width), 1920.0 / float(source_height))
    rendered_width = max(2, int(round(source_width * scale)))
    rendered_height = max(2, int(round(source_height * scale)))
    if rendered_width % 2 != 0:
        rendered_width = max(2, rendered_width - 1)
    if rendered_height % 2 != 0:
        rendered_height = max(2, rendered_height - 1)
    pad_left = max(0, (1080 - rendered_width) // 2)
    pad_top = max(0, (1920 - rendered_height) // 2)
    return {
        "rendered_width": rendered_width,
        "rendered_height": rendered_height,
        "pad_left": pad_left,
        "pad_right": max(0, 1080 - rendered_width - pad_left),
        "pad_top": pad_top,
        "pad_bottom": max(0, 1920 - rendered_height - pad_top),
    }


def _render_centered_mobile_clip(source_video_path, output_path):
    _run_cmd(
        [
            "ffmpeg",
            "-y",
            "-i",
            source_video_path,
            "-vf",
            (
                "scale=1080:1920:force_original_aspect_ratio=decrease,"
                "pad=1080:1920:(ow-iw)/2:(oh-ih)/2:black,"
                "setsar=1"
            ),
            "-map",
            "0:v:0",
            "-map",
            "0:a?",
            *_h264_aac_encode_args(),
            output_path,
        ]
    )


def save_uploaded_single_clip_source(file_storage):
    return _save_uploaded_clip_source(file_storage, get_single_clip_source_dir())


def resolve_single_clip_source_path(file_name):
    return _resolve_uploaded_clip_source_path(file_name, get_single_clip_source_dir())


def list_single_clip_renders():
    return _list_render_results(
        get_single_clip_render_dir(),
        SINGLE_CLIP_RENDER_DIRNAME,
        SINGLE_CLIP_RESULT_FILENAME,
    )


def save_uploaded_centered_mobile_source(file_storage):
    return _save_uploaded_clip_source(file_storage, get_centered_mobile_source_dir())


def resolve_centered_mobile_source_path(file_name):
    return _resolve_uploaded_clip_source_path(file_name, get_centered_mobile_source_dir())


def list_centered_mobile_renders():
    return _list_render_results(
        get_centered_mobile_render_dir(),
        CENTERED_MOBILE_RENDER_DIRNAME,
        CENTERED_MOBILE_RESULT_FILENAME,
    )


def run_single_clip_pipeline(
    task_id,
    source_filename="",
    source_url="",
    progress_callback=None,
    overlay_title="",
):
    clean_url = str(source_url or "").strip()
    if clean_url:
        clean_name, source_path, clean_url = _download_youtube_source(
            clean_url,
            get_single_clip_source_dir(),
            progress_callback=progress_callback,
        )
    else:
        clean_name = _clean_filename(source_filename)
        if not clean_name:
            raise ValueError("Missing source filename.")
        source_path = resolve_single_clip_source_path(clean_name)

    duration_seconds = _probe_video_duration_seconds(source_path)
    if duration_seconds <= 1.0:
        raise ValueError("Uploaded clip is too short to process.")

    render_root = get_single_clip_render_dir()
    task_dir = _task_dir_for(render_root, task_id)
    os.makedirs(task_dir, exist_ok=True)

    _emit(
        progress_callback,
        "SINGLE_PREP",
        "Preparing single clip render.",
        {"task_id": task_id, "source_filename": clean_name, "source_url": clean_url},
    )

    clip_label = os.path.splitext(clean_name)[0] or "Single Clip"
    render_highlight_clip(
        source_video_path=source_path,
        clip_dir=task_dir,
        clip_index=0,
        clip_label=clip_label,
        start_seconds=0.0,
        end_seconds=duration_seconds,
        progress_callback=progress_callback,
        start_timestamp="00:00",
        end_timestamp=seconds_to_timestamp(duration_seconds),
        temp_audio_token=task_id,
    )

    result = run_ai_pipeline_for_clip(
        subdir=_task_subdir(SINGLE_CLIP_RENDER_DIRNAME, task_id),
        filename="clip_0.mp4",
        progress_callback=progress_callback,
        show_items=False,
        vine_boom=False,
        overlay_title=overlay_title,
    )

    result["task_id"] = _safe_task_id(task_id)
    result["source_filename"] = clean_name
    result["source_url"] = clean_url
    result["pipeline_kind"] = "single_clip_render"
    artifacts = result.get("artifacts") if isinstance(result.get("artifacts"), dict) else {}
    artifacts["rendered_video"] = _task_file_url(SINGLE_CLIP_RENDER_DIRNAME, task_id, "clip_0.mp4")
    result["artifacts"] = artifacts
    _persist_result(render_root, task_id, result, SINGLE_CLIP_RESULT_FILENAME)
    return result


def run_centered_mobile_pipeline(
    task_id,
    source_filename="",
    source_url="",
    progress_callback=None,
    tier_list_enabled=False,
):
    clean_url = str(source_url or "").strip()
    if clean_url:
        clean_name, source_path, clean_url = _download_youtube_source(
            clean_url,
            get_centered_mobile_source_dir(),
            progress_callback=progress_callback,
        )
    else:
        clean_name = _clean_filename(source_filename)
        if not clean_name:
            raise ValueError("Missing source filename.")
        source_path = resolve_centered_mobile_source_path(clean_name)

    video_meta = _probe_video_metadata(source_path)
    duration_seconds = float(video_meta["duration_seconds"])
    if duration_seconds <= 1.0:
        raise ValueError("Uploaded clip is too short to process.")

    render_root = get_centered_mobile_render_dir()
    task_dir = _task_dir_for(render_root, task_id)
    os.makedirs(task_dir, exist_ok=True)

    clip_label = os.path.splitext(clean_name)[0] or "Centered Mobile Clip"
    _emit(
        progress_callback,
        "CENTERED_MOBILE_PREP",
        "Preparing centered mobile render.",
        {"task_id": task_id, "source_filename": clean_name, "source_url": clean_url},
    )

    output_path = os.path.join(task_dir, "clip_0.mp4")
    _emit(
        progress_callback,
        "CENTERED_MOBILE_RENDERING",
        "Rendering full clip into a centered 9:16 layout.",
        {"task_id": task_id, "source_filename": clean_name},
    )
    _render_centered_mobile_clip(source_path, output_path)
    _write_clip_context(
        task_dir,
        clean_name,
        clip_label,
        duration_seconds,
        extra={
            "layout_mode": "centered_mobile_pad",
            "render_style": "subtitles_only",
            "source_width": int(video_meta["width"]),
            "source_height": int(video_meta["height"]),
            **_centered_mobile_layout(video_meta["width"], video_meta["height"]),
        },
    )

    result = run_ai_pipeline_for_clip(
        subdir=_task_subdir(CENTERED_MOBILE_RENDER_DIRNAME, task_id),
        filename="clip_0.mp4",
        progress_callback=progress_callback,
        show_items=False,
        vine_boom=False,
        overlay_title="",
        tier_list_enabled=bool(tier_list_enabled),
    )

    result["task_id"] = _safe_task_id(task_id)
    result["source_filename"] = clean_name
    result["source_url"] = clean_url
    result["pipeline_kind"] = "centered_mobile_subtitles_only"
    artifacts = result.get("artifacts") if isinstance(result.get("artifacts"), dict) else {}
    artifacts["rendered_video"] = _task_file_url(CENTERED_MOBILE_RENDER_DIRNAME, task_id, "clip_0.mp4")
    result["artifacts"] = artifacts
    _persist_result(render_root, task_id, result, CENTERED_MOBILE_RESULT_FILENAME)
    return result
