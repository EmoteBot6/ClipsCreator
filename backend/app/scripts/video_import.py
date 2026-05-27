from __future__ import unicode_literals

import json
import logging
import os
import re
import time
import xml.etree.ElementTree as ET
from datetime import datetime
from urllib.parse import parse_qs, urlparse

import requests
from moviepy.editor import VideoFileClip
try:
    from yt_dlp import YoutubeDL
except ImportError:
    YoutubeDL = None


logger = logging.getLogger(__name__)

SYNAPSE_HANDLE_URL = "https://www.youtube.com/@Synapse1"
SOURCE_VIDEO_FILENAME = "source_video.mp4"
SOURCE_LIBRARY_DIRNAME = "sources"
ACTIVE_SOURCE_MARKER = ".active_source"
SUPPORTED_SOURCE_VIDEO_EXTENSIONS = {".mp4", ".mov", ".mkv", ".webm", ".m4v"}


def get_video_storage_dir():
    configured = os.getenv("LEAGUECLIPS_VIDEO_DIR")
    if configured:
        return configured
    if os.path.isdir("/videos"):
        return "/videos"
    return os.path.abspath("videos")


def get_source_video_path():
    active = get_active_source_filename()
    if active:
        active_path = os.path.join(get_source_library_dir(), active)
        if os.path.exists(active_path):
            return active_path
        set_active_source_filename(None)
    return os.path.join(get_video_storage_dir(), SOURCE_VIDEO_FILENAME)


def get_source_library_dir():
    return os.path.join(get_video_storage_dir(), SOURCE_LIBRARY_DIRNAME)


def resolve_source_video_path(file_name=""):
    clean_name = _clean_filename(file_name)
    if clean_name:
        source_path = os.path.join(get_source_library_dir(), clean_name)
        if not os.path.exists(source_path):
            raise FileNotFoundError(clean_name)
        return clean_name, source_path

    active = get_active_source_filename()
    if active:
        active_path = os.path.join(get_source_library_dir(), active)
        if os.path.exists(active_path):
            return active, active_path
        set_active_source_filename(None)

    fallback_path = os.path.join(get_video_storage_dir(), SOURCE_VIDEO_FILENAME)
    if os.path.exists(fallback_path):
        return SOURCE_VIDEO_FILENAME, fallback_path
    raise FileNotFoundError(fallback_path)


def _active_source_marker_path():
    return os.path.join(get_video_storage_dir(), ACTIVE_SOURCE_MARKER)


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


def _safe_float_env(name, default, min_value=None, max_value=None):
    try:
        value = float(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        value = float(default)
    if min_value is not None:
        value = max(float(min_value), value)
    if max_value is not None:
        value = min(float(max_value), value)
    return value


def _safe_int_env(name, default, min_value=None, max_value=None):
    try:
        value = int(float(os.getenv(name, str(default))))
    except (TypeError, ValueError):
        value = int(default)
    if min_value is not None:
        value = max(int(min_value), value)
    if max_value is not None:
        value = min(int(max_value), value)
    return value


def _safe_bool_env(name, default=False):
    value = os.getenv(name)
    if value is None:
        return bool(default)
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


def _safe_bytes_from_mb_env(name, default_mb, min_mb=0, max_mb=512):
    mb_value = _safe_float_env(name, default_mb, min_value=min_mb, max_value=max_mb)
    if mb_value <= 0:
        return None
    return int(mb_value * 1024 * 1024)


def _is_stop_requested_error(exc):
    return "stop requested" in str(exc or "").lower()


def _is_transient_download_error(exc):
    message = str(exc or "").lower()
    transient_tokens = (
        "read timed out",
        "timed out",
        "connection reset",
        "connection aborted",
        "connection broken",
        "remote end closed",
        "temporarily unavailable",
        "temporary failure",
        "incomplete read",
        "ssl",
        "http error 500",
        "http error 502",
        "http error 503",
        "http error 504",
        "503 service unavailable",
        "504 gateway",
    )
    return any(token in message for token in transient_tokens)


def _youtube_source_stem(title, video_id):
    base_title = _clean_filename(title or "youtube_source")
    stem = os.path.splitext(base_title)[0]
    if video_id:
        stem = f"{stem}_{_clean_filename(video_id)}"
    stem = stem.strip("._-")
    return stem[:180] or "youtube_source"


def set_active_source_filename(file_name):
    marker = _active_source_marker_path()
    if not file_name:
        if os.path.exists(marker):
            os.remove(marker)
        return
    with open(marker, "w", encoding="utf-8") as handle:
        handle.write(file_name)


def get_active_source_filename():
    marker = _active_source_marker_path()
    if not os.path.exists(marker):
        return None
    try:
        with open(marker, "r", encoding="utf-8") as handle:
            value = handle.read().strip()
    except OSError:
        return None
    if not value:
        return None
    active_path = os.path.join(get_source_library_dir(), value)
    if not os.path.exists(active_path):
        set_active_source_filename(None)
        return None
    return value


def list_source_videos():
    source_dir = get_source_library_dir()
    os.makedirs(source_dir, exist_ok=True)
    active = get_active_source_filename()
    videos = []
    for file_name in os.listdir(source_dir):
        full_path = os.path.join(source_dir, file_name)
        if not os.path.isfile(full_path):
            continue
        ext = os.path.splitext(file_name)[1].lower()
        if ext not in SUPPORTED_SOURCE_VIDEO_EXTENSIONS:
            continue
        videos.append(
            {
                "filename": file_name,
                "size_bytes": os.path.getsize(full_path),
                "modified_utc": datetime.utcfromtimestamp(
                    os.path.getmtime(full_path)
                ).isoformat() + "Z",
                "is_active": file_name == active,
            }
        )
    videos.sort(key=lambda item: item["modified_utc"], reverse=True)
    return videos


def save_uploaded_source(file_storage):
    source_dir = get_source_library_dir()
    os.makedirs(source_dir, exist_ok=True)

    original = _clean_filename(file_storage.filename)
    if not original:
        raise ValueError("Invalid file name.")
    ext = os.path.splitext(original)[1].lower()
    if ext not in SUPPORTED_SOURCE_VIDEO_EXTENSIONS:
        raise ValueError(f"Unsupported file type: {ext}")

    unique_name = _ensure_unique_filename(source_dir, original)
    final_path = os.path.join(source_dir, unique_name)
    temp_path = final_path + ".upload"
    file_storage.save(temp_path)
    os.replace(temp_path, final_path)
    set_active_source_filename(unique_name)
    return unique_name, final_path


def _summarize_ytdlp_progress(payload):
    if not isinstance(payload, dict):
        return {}
    total = payload.get("total_bytes") or payload.get("total_bytes_estimate")
    downloaded = payload.get("downloaded_bytes")
    percent = None
    if total and downloaded:
        try:
            percent = round((float(downloaded) / float(total)) * 100, 1)
        except (TypeError, ValueError, ZeroDivisionError):
            percent = None
    return {
        "status": payload.get("status"),
        "filename": os.path.basename(str(payload.get("filename") or "")),
        "downloaded_bytes": downloaded,
        "total_bytes": total,
        "percent": percent,
        "speed": payload.get("speed"),
        "eta": payload.get("eta"),
    }


def _emit_ytdlp_progress(progress_callback, **updates):
    if not progress_callback:
        return
    try:
        progress_callback({key: value for key, value in updates.items() if value is not None})
    except Exception:
        logger.exception("yt-dlp progress callback failed")


def check_ytdlp_connection(source_url, download=False):
    if YoutubeDL is None:
        return {
            "ok": False,
            "error": "yt-dlp is not installed in the backend image.",
        }

    clean_url = str(source_url or "").strip()
    if not clean_url:
        return {
            "ok": False,
            "error": "Missing source URL.",
        }

    opts = {
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
        "extract_flat": False,
        "skip_download": not bool(download),
    }
    logger.info("Checking yt-dlp connection for %s", clean_url)
    try:
        with YoutubeDL(opts) as ydl:
            info = ydl.extract_info(clean_url, download=bool(download))
    except Exception as exc:
        logger.exception("yt-dlp connection check failed for %s", clean_url)
        return {
            "ok": False,
            "error": str(exc),
        }

    if not isinstance(info, dict):
        return {
            "ok": False,
            "error": "yt-dlp returned no metadata.",
        }

    formats = info.get("formats")
    return {
        "ok": True,
        "id": info.get("id"),
        "title": info.get("title"),
        "duration": info.get("duration"),
        "extractor": info.get("extractor"),
        "webpage_url": info.get("webpage_url") or clean_url,
        "format_count": len(formats) if isinstance(formats, list) else None,
    }


def download_source_from_url(source_url, progress_callback=None):
    if YoutubeDL is None:
        raise RuntimeError("yt-dlp is not installed in the backend image.")

    clean_url = str(source_url or "").strip()
    if not clean_url:
        raise ValueError("Missing source URL.")

    source_dir = get_source_library_dir()
    os.makedirs(source_dir, exist_ok=True)

    logger.info("Starting source download from %s into %s", clean_url, source_dir)
    _emit_ytdlp_progress(progress_callback, step="metadata", message="Reading YouTube metadata.")
    socket_timeout = _safe_float_env("LEAGUECLIPS_YTDLP_SOCKET_TIMEOUT_SECONDS", 90.0, 5.0, 300.0)
    retries = _safe_int_env("LEAGUECLIPS_YTDLP_RETRIES", 15, 0, 100)
    fragment_retries = _safe_int_env("LEAGUECLIPS_YTDLP_FRAGMENT_RETRIES", 20, 0, 100)
    file_access_retries = _safe_int_env("LEAGUECLIPS_YTDLP_FILE_ACCESS_RETRIES", 5, 0, 50)
    download_attempts = _safe_int_env("LEAGUECLIPS_YTDLP_DOWNLOAD_ATTEMPTS", 3, 1, 10)
    retry_sleep_seconds = _safe_float_env("LEAGUECLIPS_YTDLP_RETRY_SLEEP_SECONDS", 8.0, 0.0, 120.0)
    http_chunk_size = _safe_bytes_from_mb_env("LEAGUECLIPS_YTDLP_HTTP_CHUNK_SIZE_MB", 16.0, 0, 256)
    metadata_opts = {
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
        "extract_flat": False,
        "socket_timeout": socket_timeout,
        "retries": retries,
    }
    with YoutubeDL(metadata_opts) as metadata_ydl:
        info = metadata_ydl.extract_info(clean_url, download=False)

    if not isinstance(info, dict):
        raise RuntimeError("Could not read YouTube metadata.")

    video_id = str(info.get("id") or "").strip()
    title = str(info.get("title") or video_id or "youtube_source").strip()
    logger.info(
        "Resolved YouTube source metadata: id=%s title=%r duration=%s",
        video_id,
        title,
        info.get("duration"),
    )
    stem = _youtube_source_stem(title, video_id)
    unique_name = _ensure_unique_filename(source_dir, f"{stem}.mp4")
    unique_stem = os.path.splitext(unique_name)[0]
    output_template = os.path.join(source_dir, f"{unique_stem}.%(ext)s")

    progress_log_interval = _safe_float_env("LEAGUECLIPS_YTDLP_PROGRESS_LOG_SECONDS", 10.0, 1.0, 120.0)
    progress_emit_interval = _safe_float_env("LEAGUECLIPS_YTDLP_PROGRESS_STATUS_SECONDS", 1.5, 0.2, 30.0)
    progress_state = {
        "last_log_at": 0.0,
        "last_emit_at": 0.0,
        "last_log_percent": None,
        "last_emit_percent": None,
        "last_status": "",
    }

    def _progress_moved(current, previous, min_step):
        if current is None:
            return False
        if previous is None:
            return True
        try:
            return abs(float(current) - float(previous)) >= float(min_step)
        except (TypeError, ValueError):
            return False

    def _should_report_progress(summary, report_type):
        now = time.monotonic()
        percent = summary.get("percent")
        status = summary.get("status") or ""
        if report_type == "log":
            last_at_key = "last_log_at"
            last_percent_key = "last_log_percent"
            interval = progress_log_interval
            percent_step = 5.0
        else:
            last_at_key = "last_emit_at"
            last_percent_key = "last_emit_percent"
            interval = progress_emit_interval
            percent_step = 1.0

        status_changed = status != progress_state.get("last_status")
        interval_elapsed = now - float(progress_state.get(last_at_key) or 0.0) >= interval
        percent_moved = _progress_moved(percent, progress_state.get(last_percent_key), percent_step)
        is_finished = status == "finished" or percent == 100
        if status_changed or interval_elapsed or percent_moved or is_finished:
            progress_state[last_at_key] = now
            progress_state[last_percent_key] = percent
            if report_type == "emit":
                progress_state["last_status"] = status
            return True
        return False

    def _progress_hook(payload):
        summary = _summarize_ytdlp_progress(payload)
        status = summary.get("status")
        if status == "downloading":
            if _should_report_progress(summary, "log"):
                logger.info(
                    "yt-dlp downloading %s: %s%% (%s/%s bytes), eta=%s",
                    summary.get("filename") or unique_name,
                    summary.get("percent"),
                    summary.get("downloaded_bytes"),
                    summary.get("total_bytes"),
                    summary.get("eta"),
                )
            if _should_report_progress(summary, "emit"):
                _emit_ytdlp_progress(
                    progress_callback,
                    step="downloading",
                    message="Downloading YouTube source.",
                    **summary,
                )
        elif status == "finished":
            logger.info("yt-dlp finished download for %s", summary.get("filename") or unique_name)
            _emit_ytdlp_progress(
                progress_callback,
                step="postprocess",
                message="Download finished, post-processing source.",
                **summary,
            )

    _emit_ytdlp_progress(progress_callback, step="downloading", message="Starting YouTube download.")
    download_opts = {
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
        "outtmpl": output_template,
        "restrictfilenames": True,
        "merge_output_format": "mp4",
        "format": "bv*+ba/b",
        "progress_hooks": [_progress_hook],
        "socket_timeout": socket_timeout,
        "retries": retries,
        "fragment_retries": fragment_retries,
        "file_access_retries": file_access_retries,
        "continuedl": True,
        "nopart": False,
        "overwrites": False,
        "concurrent_fragment_downloads": 1,
    }
    if http_chunk_size:
        download_opts["http_chunk_size"] = http_chunk_size
    if _safe_bool_env("LEAGUECLIPS_YTDLP_FORCE_IPV4", default=False):
        download_opts["source_address"] = "0.0.0.0"

    downloaded_info = None
    last_error = None
    for attempt in range(1, download_attempts + 1):
        _emit_ytdlp_progress(
            progress_callback,
            step="downloading",
            message=f"Starting YouTube download attempt {attempt}/{download_attempts}.",
            attempt=attempt,
            attempts=download_attempts,
        )
        try:
            logger.info(
                "yt-dlp download attempt %s/%s for %s (timeout=%ss retries=%s fragment_retries=%s chunk=%s)",
                attempt,
                download_attempts,
                clean_url,
                socket_timeout,
                retries,
                fragment_retries,
                http_chunk_size or "default",
            )
            with YoutubeDL(download_opts) as ydl:
                downloaded_info = ydl.extract_info(clean_url, download=True)
            last_error = None
            break
        except Exception as exc:
            last_error = exc
            if _is_stop_requested_error(exc):
                raise
            can_retry = attempt < download_attempts and _is_transient_download_error(exc)
            if not can_retry:
                raise
            logger.warning(
                "yt-dlp transient download error on attempt %s/%s for %s: %s",
                attempt,
                download_attempts,
                clean_url,
                exc,
            )
            _emit_ytdlp_progress(
                progress_callback,
                step="retrying",
                message=f"Network timeout during download; retrying attempt {attempt + 1}/{download_attempts}.",
                attempt=attempt,
                attempts=download_attempts,
                error=str(exc),
            )
            if retry_sleep_seconds > 0:
                time.sleep(retry_sleep_seconds)

    if last_error is not None:
        raise last_error

    final_path = None
    requested_downloads = downloaded_info.get("requested_downloads") if isinstance(downloaded_info, dict) else None
    if isinstance(requested_downloads, list):
        for item in requested_downloads:
            candidate = item.get("filepath") if isinstance(item, dict) else None
            if candidate and os.path.exists(candidate):
                final_path = candidate
                break

    if not final_path:
        for ext in SUPPORTED_SOURCE_VIDEO_EXTENSIONS:
            candidate = os.path.join(source_dir, f"{unique_stem}{ext}")
            if os.path.exists(candidate):
                final_path = candidate
                break

    if not final_path:
        raise RuntimeError("yt-dlp finished but no downloaded file was found.")

    saved_name = os.path.basename(final_path)
    set_active_source_filename(saved_name)
    logger.info(
        "Source download saved: filename=%s path=%s size_bytes=%s",
        saved_name,
        final_path,
        os.path.getsize(final_path),
    )
    _emit_ytdlp_progress(
        progress_callback,
        step="done",
        message="YouTube source downloaded.",
        filename=saved_name,
        path=final_path,
        size_bytes=os.path.getsize(final_path),
    )
    return saved_name, final_path, clean_url


def activate_source_video(file_name):
    clean_name = _clean_filename(file_name)
    if not clean_name:
        raise ValueError("Missing filename.")
    source_dir = get_source_library_dir()
    full_path = os.path.join(source_dir, clean_name)
    if not os.path.exists(full_path):
        raise FileNotFoundError(clean_name)
    set_active_source_filename(clean_name)
    return clean_name, full_path


def _seconds_to_timestamp(seconds):
    seconds = int(seconds)
    hours = seconds // 3600
    minutes = (seconds % 3600) // 60
    secs = seconds % 60
    if hours > 0:
        return f"{hours:02d}:{minutes:02d}:{secs:02d}"
    return f"{minutes:02d}:{secs:02d}"


def _timestamp_to_seconds(timestamp):
    parts = [int(part) for part in timestamp.split(":")]
    if len(parts) == 2:
        minutes, seconds = parts
        return minutes * 60 + seconds
    if len(parts) == 3:
        hours, minutes, seconds = parts
        return hours * 3600 + minutes * 60 + seconds
    raise ValueError(f"Unsupported timestamp: {timestamp}")


def _safe_clip_seconds():
    try:
        value = int(os.getenv("LEAGUECLIPS_CLIP_SECONDS", "60"))
    except ValueError:
        value = 60
    return max(10, value)


def _safe_clip_trim_seconds():
    try:
        value = float(os.getenv("LEAGUECLIPS_CLIP_TRIM_SECONDS", "0.5"))
    except ValueError:
        value = 0.5
    return max(0.0, min(5.0, value))


def _apply_clip_trim(start_seconds, end_seconds, total_seconds):
    # Add an extra second on top of configured trim to prevent clip boundary overlap bleed.
    trim = _safe_clip_trim_seconds() + 1.0
    start = max(0.0, float(start_seconds))
    end = min(float(total_seconds), float(end_seconds))

    if end <= start:
        return None

    trimmed_start = min(end, start + trim)
    trimmed_end = max(trimmed_start, end - trim)

    # Keep clips long enough for usable renders.
    if trimmed_end - trimmed_start < 2.0:
        return None
    return int(trimmed_start), int(trimmed_end)


def _extract_video_id_from_source_name(file_name):
    if not file_name:
        return None

    source_value = str(file_name).strip()

    # Direct URL support.
    if "youtube.com" in source_value or "youtu.be/" in source_value:
        parsed = urlparse(source_value)
        if "youtu.be" in (parsed.netloc or ""):
            candidate = parsed.path.strip("/")
            if candidate and re.fullmatch(r"[A-Za-z0-9_-]{11}", candidate):
                return candidate
        query = parse_qs(parsed.query)
        candidate = (query.get("v") or [None])[0]
        if candidate and re.fullmatch(r"[A-Za-z0-9_-]{11}", candidate):
            return candidate

    name = os.path.basename(source_value)

    # Support cases where an URL-like fragment is embedded in the file name.
    if "v=" in name:
        parsed = urlparse(name)
        query = parse_qs(parsed.query)
        candidate = (query.get("v") or [None])[0]
        if candidate and re.fullmatch(r"[A-Za-z0-9_-]{11}", candidate):
            return candidate

    # Common upload pattern: tokens separated by underscores/dots/spaces, e.g. "..._KD2oHiVz4XQ_..."
    token_candidates = re.split(r"[_\s.]+", name)
    for token in token_candidates:
        if re.fullmatch(r"[A-Za-z0-9_-]{11}", token):
            return token

    # Fallback: detect 11-char IDs bounded by non-alphanumeric chars.
    candidates = re.findall(r"(?<![A-Za-z0-9])([A-Za-z0-9_-]{11})(?![A-Za-z0-9])", name)
    if not candidates:
        return None
    return candidates[-1]


def _decode_escaped_text(value):
    if value is None:
        return None
    try:
        return bytes(value, "utf-8").decode("unicode_escape")
    except UnicodeDecodeError:
        return value.replace("\\n", "\n").replace('\\"', '"')


def _fetch_youtube_description(video_id):
    if not video_id:
        return None

    url = f"https://www.youtube.com/watch?v={video_id}"
    response = requests.get(
        url,
        timeout=12,
        headers={
            "User-Agent": (
                "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
            )
        },
    )
    response.raise_for_status()
    html = response.text

    # Primary extraction from ytInitialPlayerResponse JSON.
    player_match = re.search(r"ytInitialPlayerResponse\s*=\s*(\{.*?\});", html, re.DOTALL)
    if player_match:
        try:
            payload = json.loads(player_match.group(1))
            details = payload.get("videoDetails", {})
            short_description = details.get("shortDescription")
            decoded = _decode_escaped_text(short_description)
            if decoded:
                return decoded
        except (ValueError, TypeError, json.JSONDecodeError):
            pass

    # Fallback regex extraction for pages where player payload shape changes.
    for pattern in (
        r'"shortDescription":"(.*?)","isCrawlable":',
        r'"description":{"simpleText":"(.*?)"}',
    ):
        match = re.search(pattern, html, re.DOTALL)
        if match:
            decoded = _decode_escaped_text(match.group(1))
            if decoded:
                return decoded
    return None


def _parse_description_markers(description_text, total_seconds):
    if not description_text:
        return []

    marker_pattern = re.compile(
        r"^\s*(?:[-*]\s*)?(\d{1,2}:\d{2}(?::\d{2})?)\s*(?:[-:|\u2013\u2014]\s*)?(.+?)\s*$"
    )
    markers = []

    for line in description_text.splitlines():
        match = marker_pattern.match(line)
        if not match:
            continue
        ts = match.group(1)
        label = re.sub(r"^[\s\-:|]+", "", match.group(2)).strip()
        try:
            start = _timestamp_to_seconds(ts)
        except ValueError:
            continue
        if start >= total_seconds:
            continue
        markers.append((start, label))

    return _clip_definitions_from_markers(markers, total_seconds)


def _clip_definitions_from_markers(markers, total_seconds):
    if not markers:
        return []

    # Keep first occurrence per timestamp while preserving order.
    seen = set()
    deduped = []
    for start, label in markers:
        if start in seen:
            continue
        seen.add(start)
        deduped.append((start, label))

    clip_definitions = []
    for index, (start, label) in enumerate(deduped):
        end = total_seconds if index + 1 >= len(deduped) else deduped[index + 1][0]
        trimmed = _apply_clip_trim(start, end, total_seconds)
        if not trimmed:
            continue
        trim_start, trim_end = trimmed
        clip_definitions.append(
            [
                _seconds_to_timestamp(trim_start),
                label or f"Clip_{index + 1:03d}",
                len(clip_definitions),
                _seconds_to_timestamp(trim_end),
            ]
        )
    return clip_definitions


def _truthy_env(name, default=False):
    value = os.getenv(name)
    if value is None:
        return bool(default)
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


def _normalize_ollama_host(raw_host):
    host = (raw_host or "http://127.0.0.1:11434").strip().rstrip("/")
    if not host:
        host = "http://127.0.0.1:11434"
    for suffix in ("/api/generate", "/api", "/v1/chat/completions", "/v1/completions"):
        if host.endswith(suffix):
            host = host[: -len(suffix)].rstrip("/")
    return host


def _ollama_request_timeout():
    connect_timeout = _safe_float_env(
        "LEAGUECLIPS_OLLAMA_CONNECT_TIMEOUT_SECONDS",
        10.0,
        min_value=1.0,
        max_value=120.0,
    )
    read_timeout = _safe_float_env(
        "LEAGUECLIPS_OLLAMA_TIMEOUT_SECONDS",
        90.0,
        min_value=5.0,
        max_value=900.0,
    )
    return (connect_timeout, read_timeout)


def _parse_json_object(text):
    raw = str(text or "").strip()
    if not raw:
        return None
    try:
        return json.loads(raw)
    except (TypeError, ValueError, json.JSONDecodeError):
        pass

    start = raw.find("{")
    end = raw.rfind("}")
    if start == -1 or end == -1 or end <= start:
        return None
    try:
        return json.loads(raw[start : end + 1])
    except (TypeError, ValueError, json.JSONDecodeError):
        return None


def _chat_completion_text(payload):
    choices = payload.get("choices")
    if not isinstance(choices, list) or not choices:
        return ""
    first = choices[0] if isinstance(choices[0], dict) else {}
    message = first.get("message")
    if isinstance(message, dict):
        content = message.get("content")
        if isinstance(content, str):
            return content
    text = first.get("text")
    return text if isinstance(text, str) else ""


def _ollama_json_response(prompt):
    model = (os.getenv("LEAGUECLIPS_OLLAMA_MODEL") or "").strip()
    if not model:
        return None

    host = _normalize_ollama_host(os.getenv("LEAGUECLIPS_OLLAMA_HOST", "http://127.0.0.1:11434"))
    timeout = _ollama_request_timeout()
    native = requests.post(
        f"{host}/api/generate",
        json={"model": model, "prompt": prompt, "stream": False, "format": "json"},
        timeout=timeout,
    )
    if native.status_code != 404:
        native.raise_for_status()
        return _parse_json_object((native.json() or {}).get("response", ""))

    chat = requests.post(
        f"{host}/v1/chat/completions",
        json={
            "model": model,
            "messages": [{"role": "user", "content": prompt}],
            "temperature": 0,
        },
        timeout=timeout,
    )
    chat.raise_for_status()
    return _parse_json_object(_chat_completion_text(chat.json() or {}))


def _marker_candidates_from_ollama_payload(payload, total_seconds):
    if isinstance(payload, dict):
        items = payload.get("markers") or payload.get("clips") or payload.get("timestamps")
    elif isinstance(payload, list):
        items = payload
    else:
        items = []
    if not isinstance(items, list):
        return []

    markers = []
    for item in items:
        if not isinstance(item, dict):
            continue
        timestamp = item.get("timestamp") or item.get("time") or item.get("start") or item.get("start_time")
        label = str(item.get("label") or item.get("title") or item.get("description") or "Clip").strip()
        try:
            start = _timestamp_to_seconds(str(timestamp).strip())
        except (TypeError, ValueError):
            continue
        if start >= total_seconds:
            continue
        markers.append((start, label or "Clip"))
    markers.sort(key=lambda marker: marker[0])
    return markers


def _parse_description_markers_with_ollama(description_text, total_seconds):
    if not description_text:
        return []
    if not _truthy_env("LEAGUECLIPS_OLLAMA_SPLIT_MARKERS_ENABLED", default=True):
        return []
    if not (os.getenv("LEAGUECLIPS_OLLAMA_MODEL") or "").strip():
        return []

    prompt = (
        "You extract explicit timestamp markers from a YouTube description for clip splitting.\n"
        "Return STRICT JSON only with key markers.\n"
        "markers must be an array of objects: {\"timestamp\":\"MM:SS or HH:MM:SS\", \"label\":\"short label\"}.\n"
        "Only include timestamps that appear explicitly in the description. Do not invent timestamps.\n"
        "If there are no explicit clip markers, return {\"markers\":[]}.\n"
        f"Video duration seconds: {int(total_seconds)}\n"
        f"Description:\n{description_text[:6000]}"
    )
    try:
        payload = _ollama_json_response(prompt)
    except Exception as exc:
        logger.warning("Ollama clip marker extraction skipped: %s", exc)
        return []

    markers = _marker_candidates_from_ollama_payload(payload, total_seconds)
    clip_definitions = _clip_definitions_from_markers(markers, total_seconds)
    if clip_definitions:
        logger.info("Using %s Ollama-normalized YouTube markers.", len(clip_definitions))
    return clip_definitions


def _parse_rss_entry(entry, namespace):
    title = entry.findtext("atom:title", default="Latest Synapse video", namespaces=namespace)
    video_id = entry.findtext("yt:videoId", default="", namespaces=namespace)

    link = entry.find("atom:link", namespace)
    video_url = link.attrib.get("href") if link is not None else ""
    if not video_url and video_id:
        video_url = f"https://www.youtube.com/watch?v={video_id}"
    if not video_url:
        raise ValueError("Could not resolve latest video URL.")

    thumbnail_url = None
    thumbnail = entry.find("media:group/media:thumbnail", namespace)
    if thumbnail is not None:
        thumbnail_url = thumbnail.attrib.get("url")
    if not thumbnail_url and video_id:
        thumbnail_url = f"https://i.ytimg.com/vi/{video_id}/hqdefault.jpg"

    published_utc = (
        entry.findtext("atom:published", default="", namespaces=namespace)
        or entry.findtext("atom:updated", default="", namespaces=namespace)
        or None
    )

    return {
        "title": title,
        "url": video_url,
        "thumbnail": thumbnail_url,
        "channel": "Synapse",
        "video_id": video_id or None,
        "published_utc": published_utc,
    }


def _parse_rss_feed_entries(feed_xml, limit=None):
    root = ET.fromstring(feed_xml)
    namespace = {
        "atom": "http://www.w3.org/2005/Atom",
        "yt": "http://www.youtube.com/xml/schemas/2015",
        "media": "http://search.yahoo.com/mrss/",
    }

    entries = root.findall("atom:entry", namespace)
    if not entries:
        raise ValueError("No video entries in channel RSS feed.")

    items = []
    for entry in entries[:limit]:
        items.append(_parse_rss_entry(entry, namespace))
    return items


def _parse_rss_feed(feed_xml):
    items = _parse_rss_feed_entries(feed_xml, limit=1)
    if not items:
        raise ValueError("No video entries in channel RSS feed.")
    return items[0]


def _fetch_latest_feed():
    channel_id = os.getenv("LEAGUECLIPS_SYNAPSE_CHANNEL_ID", "").strip()
    if not channel_id:
        channel_id = _resolve_channel_id_from_handle()
    rss_url = f"https://www.youtube.com/feeds/videos.xml?channel_id={channel_id}"
    rss_response = requests.get(rss_url, timeout=10)
    rss_response.raise_for_status()
    return rss_response.text


def _resolve_channel_id_from_handle():
    handle_response = requests.get(SYNAPSE_HANDLE_URL, timeout=10)
    handle_response.raise_for_status()
    html = handle_response.text

    patterns = [
        r'itemprop="channelId"\s+content="(UC[^"]+)"',
        r'"externalId":"(UC[^"]+)"',
        r'"channelId":"(UC[^"]+)"',
        r'href="https://www\.youtube\.com/channel/(UC[^"]+)"',
    ]
    for pattern in patterns:
        match = re.search(pattern, html)
        if match:
            return match.group(1)

    if "Synapse" not in html:
        raise ValueError("Could not resolve Synapse channel ID.")
    raise ValueError("Could not resolve channel ID from @Synapse1 page.")


def get_newest_video_metadata():
    feed_xml = _fetch_latest_feed()
    return _parse_rss_feed(feed_xml)


def get_recent_synapse_video_metadata(limit=10):
    feed_xml = _fetch_latest_feed()
    return _parse_rss_feed_entries(feed_xml, limit=limit)


def get_newest_video():
    return get_newest_video_metadata()["url"]


def get_list(url="", source_filename=""):
    source_name, source_video_path = resolve_source_video_path(source_filename)

    clip_seconds = _safe_clip_seconds()
    with VideoFileClip(source_video_path) as source:
        total_seconds = int(source.duration)

    if total_seconds <= 5:
        raise ValueError("Uploaded source video is too short to split.")

    video_id = _extract_video_id_from_source_name(url) if url else None
    if not video_id:
        video_id = _extract_video_id_from_source_name(source_name or source_video_path)
    if video_id:
        try:
            description = _fetch_youtube_description(video_id)
            marker_clips = _parse_description_markers(description, total_seconds)
            if marker_clips:
                logger.info("Using %s YouTube markers from video %s", len(marker_clips), video_id)
                return marker_clips
            ollama_marker_clips = _parse_description_markers_with_ollama(description, total_seconds)
            if ollama_marker_clips:
                logger.info("Using %s Ollama-normalized markers from video %s", len(ollama_marker_clips), video_id)
                return ollama_marker_clips
        except Exception as exc:
            logger.warning("Could not parse YouTube markers for %s: %s", video_id, exc)

    clip_definitions = []
    clip_index = 0
    current_start = 0

    while current_start < total_seconds:
        next_start = min(current_start + clip_seconds, total_seconds)
        trimmed = _apply_clip_trim(current_start, next_start, total_seconds)
        if not trimmed:
            current_start = next_start
            continue
        trim_start, trim_end = trimmed

        clip_definitions.append(
            [
                _seconds_to_timestamp(trim_start),
                f"Clip_{clip_index + 1:03d}",
                clip_index,
                _seconds_to_timestamp(trim_end),
            ]
        )
        clip_index += 1
        current_start = next_start

    if not clip_definitions:
        raise ValueError("No clips could be generated from uploaded source video.")

    return clip_definitions

