import json
import os
import re
import subprocess
from datetime import datetime

import requests

from .video_editor import get_temp_dir
from .video_import import get_video_storage_dir


SITCOM_SOURCE_DIRNAME = "sitcom_sources"
SITCOM_EDIT_DIRNAME = "sitcom_edits"
ACTIVE_SITCOM_SOURCE_MARKER = ".active_sitcom_source"
SITCOM_METADATA_FILENAME = "sitcom_edit.json"
SITCOM_OUTPUT_FILENAME = "sitcom_edited.mp4"
SITCOM_SUBTITLES_SRT = "sitcom_subtitles.srt"
SITCOM_SUBTITLES_VTT = "sitcom_subtitles.vtt"
SITCOM_CLIP_PREFIX = "sitcom_clip_"
SUPPORTED_VIDEO_EXTENSIONS = {".mp4", ".mov", ".mkv", ".webm", ".m4v"}


def get_sitcom_source_dir():
    return os.path.join(get_video_storage_dir(), SITCOM_SOURCE_DIRNAME)


def get_sitcom_edit_dir():
    return os.path.join(get_video_storage_dir(), SITCOM_EDIT_DIRNAME)


def _active_source_marker_path():
    return os.path.join(get_video_storage_dir(), ACTIVE_SITCOM_SOURCE_MARKER)


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


def set_active_sitcom_source_filename(file_name):
    marker_path = _active_source_marker_path()
    if not file_name:
        if os.path.exists(marker_path):
            os.remove(marker_path)
        return
    with open(marker_path, "w", encoding="utf-8") as handle:
        handle.write(file_name)


def get_active_sitcom_source_filename():
    marker_path = _active_source_marker_path()
    if not os.path.exists(marker_path):
        return None
    try:
        with open(marker_path, "r", encoding="utf-8") as handle:
            value = (handle.read() or "").strip()
    except OSError:
        return None
    if not value:
        return None
    source_path = os.path.join(get_sitcom_source_dir(), value)
    if not os.path.exists(source_path):
        set_active_sitcom_source_filename(None)
        return None
    return value


def list_sitcom_sources():
    source_dir = get_sitcom_source_dir()
    os.makedirs(source_dir, exist_ok=True)
    active_name = get_active_sitcom_source_filename()
    items = []
    for file_name in os.listdir(source_dir):
        full_path = os.path.join(source_dir, file_name)
        if not os.path.isfile(full_path):
            continue
        ext = os.path.splitext(file_name)[1].lower()
        if ext not in SUPPORTED_VIDEO_EXTENSIONS:
            continue
        items.append(
            {
                "filename": file_name,
                "size_bytes": os.path.getsize(full_path),
                "modified_utc": datetime.utcfromtimestamp(os.path.getmtime(full_path)).isoformat() + "Z",
                "is_active": file_name == active_name,
            }
        )
    items.sort(key=lambda item: item["modified_utc"], reverse=True)
    return items


def save_uploaded_sitcom_source(file_storage):
    source_dir = get_sitcom_source_dir()
    os.makedirs(source_dir, exist_ok=True)

    original_name = _clean_filename(file_storage.filename)
    if not original_name:
        raise ValueError("Invalid file name.")
    ext = os.path.splitext(original_name)[1].lower()
    if ext not in SUPPORTED_VIDEO_EXTENSIONS:
        raise ValueError(f"Unsupported file type: {ext}")

    unique_name = _ensure_unique_filename(source_dir, original_name)
    final_path = os.path.join(source_dir, unique_name)
    temp_path = final_path + ".upload"
    file_storage.save(temp_path)
    os.replace(temp_path, final_path)
    set_active_sitcom_source_filename(unique_name)
    return unique_name, final_path


def activate_sitcom_source(file_name):
    clean_name = _clean_filename(file_name)
    if not clean_name:
        raise ValueError("Missing filename.")
    full_path = os.path.join(get_sitcom_source_dir(), clean_name)
    if not os.path.exists(full_path):
        raise FileNotFoundError(clean_name)
    set_active_sitcom_source_filename(clean_name)
    return clean_name, full_path


def resolve_sitcom_source_path(file_name=""):
    candidate = _clean_filename(file_name)
    if candidate:
        path = os.path.join(get_sitcom_source_dir(), candidate)
        if not os.path.exists(path):
            raise FileNotFoundError(candidate)
        return candidate, path

    active = get_active_sitcom_source_filename()
    if active:
        active_path = os.path.join(get_sitcom_source_dir(), active)
        if os.path.exists(active_path):
            return active, active_path
        set_active_sitcom_source_filename(None)
    raise ValueError("No active sitcom source video. Upload/select one first.")


def _run_cmd(args):
    completed = subprocess.run(args, capture_output=True, text=True)
    if completed.returncode != 0:
        stderr = (completed.stderr or "").strip()
        stdout = (completed.stdout or "").strip()
        raise RuntimeError(stderr or stdout or f"Command failed: {' '.join(args)}")
    return completed


def _probe_duration_seconds(video_path):
    completed = _run_cmd(
        [
            "ffprobe",
            "-v",
            "error",
            "-show_entries",
            "format=duration",
            "-of",
            "default=noprint_wrappers=1:nokey=1",
            video_path,
        ]
    )
    try:
        return float((completed.stdout or "").strip())
    except (TypeError, ValueError):
        raise RuntimeError("Could not parse video duration.")


def _probe_has_audio_stream(video_path):
    completed = _run_cmd(
        [
            "ffprobe",
            "-v",
            "error",
            "-select_streams",
            "a",
            "-show_entries",
            "stream=index",
            "-of",
            "default=noprint_wrappers=1:nokey=1",
            video_path,
        ]
    )
    return bool((completed.stdout or "").strip())


def _parse_timecode_seconds(value):
    if value is None:
        raise ValueError("Missing timestamp value.")
    if isinstance(value, (int, float)):
        return float(value)

    text = str(value).strip()
    if not text:
        raise ValueError("Empty timestamp value.")
    if re.fullmatch(r"\d+(?:\.\d+)?", text):
        return float(text)

    parts = text.split(":")
    if len(parts) == 2:
        minutes = int(parts[0])
        seconds = float(parts[1])
        return float(minutes * 60) + seconds
    if len(parts) == 3:
        hours = int(parts[0])
        minutes = int(parts[1])
        seconds = float(parts[2])
        return float(hours * 3600 + minutes * 60) + seconds

    raise ValueError(f"Unsupported timestamp value: {value}")


def _normalize_remove_ranges(raw_ranges, duration_seconds):
    cleaned = []
    for entry in raw_ranges or []:
        start_raw = None
        end_raw = None
        if isinstance(entry, dict):
            start_raw = entry.get("start")
            end_raw = entry.get("end")
        elif isinstance(entry, str):
            line = entry.strip()
            if not line:
                continue
            match = re.match(r"^\s*(.*?)\s*(?:->|to|-)\s*(.*?)\s*$", line, flags=re.IGNORECASE)
            if not match:
                continue
            start_raw, end_raw = match.group(1), match.group(2)
        else:
            continue

        try:
            start = max(0.0, min(float(duration_seconds), _parse_timecode_seconds(start_raw)))
            end = max(0.0, min(float(duration_seconds), _parse_timecode_seconds(end_raw)))
        except ValueError:
            continue
        if end <= start + 0.15:
            continue
        cleaned.append((start, end))

    if not cleaned:
        return []

    cleaned.sort(key=lambda pair: pair[0])
    merged = []
    for start, end in cleaned:
        if not merged:
            merged.append([start, end])
            continue
        prev_start, prev_end = merged[-1]
        if start <= prev_end + 0.05:
            merged[-1][1] = max(prev_end, end)
        else:
            merged.append([start, end])

    return [(float(pair[0]), float(pair[1])) for pair in merged]


def _invert_ranges(remove_ranges, duration_seconds):
    if duration_seconds <= 0:
        return []

    keep = []
    cursor = 0.0
    for start, end in remove_ranges:
        if start > cursor + 0.05:
            keep.append((cursor, start))
        cursor = max(cursor, end)
    if cursor < duration_seconds - 0.05:
        keep.append((cursor, duration_seconds))

    trimmed_keep = []
    for start, end in keep:
        if end - start <= 0.2:
            continue
        trimmed_keep.append((round(start, 3), round(end, 3)))
    return trimmed_keep


def _safe_keep_ratio(value, default=0.72):
    try:
        ratio = float(value)
    except (TypeError, ValueError):
        ratio = float(default)
    return max(0.35, min(0.95, ratio))


def _ranges_total_seconds(ranges):
    total = 0.0
    for start, end in ranges or []:
        total += max(0.0, float(end) - float(start))
    return float(total)


def _safe_clip_seconds(value, default, min_value, max_value):
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        parsed = float(default)
    return max(float(min_value), min(float(max_value), float(parsed)))


def _split_timeline_into_clip_ranges(duration_seconds, max_clip_seconds=60.0, min_clip_seconds=12.0):
    total = max(0.0, float(duration_seconds))
    if total <= 0.1:
        return []

    max_len = _safe_clip_seconds(max_clip_seconds, default=60.0, min_value=10.0, max_value=600.0)
    min_len = _safe_clip_seconds(min_clip_seconds, default=12.0, min_value=3.0, max_value=300.0)
    if min_len > max_len:
        min_len = max_len

    ranges = []
    cursor = 0.0
    while cursor < total - 0.05:
        end = min(total, cursor + max_len)
        remainder = total - end
        if 0.0 < remainder < min_len:
            end = total
        if end - cursor < min_len and ranges:
            prev_start, _ = ranges[-1]
            ranges[-1] = (prev_start, total)
            break
        ranges.append((round(cursor, 3), round(end, 3)))
        cursor = end

    return ranges


def _render_output_clip(source_path, start_seconds, end_seconds, output_path):
    _run_cmd(
        [
            "ffmpeg",
            "-y",
            "-ss",
            f"{float(start_seconds):.3f}",
            "-to",
            f"{float(end_seconds):.3f}",
            "-i",
            source_path,
            "-map",
            "0:v:0",
            "-map",
            "0:a?",
            "-c:v",
            "libx264",
            "-preset",
            "veryfast",
            "-crf",
            os.getenv("LEAGUECLIPS_OUTPUT_CRF", "18"),
            "-pix_fmt",
            "yuv420p",
            "-c:a",
            "aac",
            "-b:a",
            "160k",
            output_path,
        ]
    )


def _default_auto_remove_instruction(target_keep_ratio):
    keep_pct = int(round(_safe_keep_ratio(target_keep_ratio) * 100))
    return (
        "Automatically remove filler, dead air, repeated lines, and low-information transitions. "
        "Keep the strongest story beats and dialogue continuity for a vertical/mobile edit. "
        f"Target roughly {keep_pct}% kept footage."
    )


def _heuristic_auto_remove_ranges(transcript_segments, duration_seconds, target_keep_ratio=0.72):
    target_keep = float(duration_seconds) * _safe_keep_ratio(target_keep_ratio)
    target_remove = max(0.0, float(duration_seconds) - target_keep)
    if target_remove < 1.2:
        return []

    ordered = sorted(
        [
            (
                max(0.0, float(seg.get("start", 0.0))),
                min(float(duration_seconds), float(seg.get("end", seg.get("start", 0.0)))),
                str(seg.get("text", "")).strip(),
            )
            for seg in (transcript_segments or [])
        ],
        key=lambda item: item[0],
    )
    ordered = [item for item in ordered if item[1] > item[0] + 0.08]
    if not ordered:
        return []

    filler_words = {
        "uh",
        "um",
        "hmm",
        "huh",
        "yeah",
        "okay",
        "ok",
        "right",
        "well",
        "so",
        "like",
    }

    candidates = []
    cursor = 0.0
    for start, end, text in ordered:
        if start - cursor >= 1.3:
            candidates.append({"start": cursor, "end": start, "score": -2.5})
        cursor = max(cursor, end)
    if float(duration_seconds) - cursor >= 1.3:
        candidates.append({"start": cursor, "end": float(duration_seconds), "score": -2.5})

    for start, end, text in ordered:
        seg_len = end - start
        if seg_len <= 0.45:
            continue
        words = re.findall(r"[A-Za-z']+", text.lower())
        word_count = len(words)
        filler_hits = sum(1 for token in words if token in filler_words)
        density = word_count / max(0.2, seg_len)
        filler_ratio = (filler_hits / max(1, word_count))
        punctuation_boost = 0.3 if re.search(r"[!?]", text) else 0.0
        score = density - (1.45 * filler_ratio) + punctuation_boost
        if word_count >= 14:
            score += 0.4

        candidates.append(
            {
                "start": max(0.0, start - 0.08),
                "end": min(float(duration_seconds), end + 0.08),
                "score": score,
            }
        )

    candidates.sort(key=lambda item: item["score"])
    selected = []
    removed_total = 0.0
    max_segments = 14
    for candidate in candidates:
        if removed_total >= target_remove:
            break
        if len(selected) >= max_segments:
            break

        start = max(0.9, float(candidate["start"]))
        end = min(float(duration_seconds) - 0.9, float(candidate["end"]))
        if end <= start + 0.35:
            continue

        overlaps = any(not (end <= a or start >= b) for a, b in selected)
        if overlaps:
            continue

        selected.append((start, end))
        removed_total += (end - start)

    normalized = _normalize_remove_ranges(
        [{"start": start, "end": end} for start, end in selected],
        duration_seconds,
    )
    return normalized


def _format_srt_timestamp(seconds):
    seconds = max(0.0, float(seconds))
    millis = int(round(seconds * 1000))
    hours = millis // 3600000
    millis %= 3600000
    minutes = millis // 60000
    millis %= 60000
    secs = millis // 1000
    ms = millis % 1000
    return f"{hours:02d}:{minutes:02d}:{secs:02d},{ms:03d}"


def _format_vtt_timestamp(seconds):
    seconds = max(0.0, float(seconds))
    millis = int(round(seconds * 1000))
    hours = millis // 3600000
    millis %= 3600000
    minutes = millis // 60000
    millis %= 60000
    secs = millis // 1000
    ms = millis % 1000
    return f"{hours:02d}:{minutes:02d}:{secs:02d}.{ms:03d}"


def _write_srt(segments, path):
    with open(path, "w", encoding="utf-8") as handle:
        for index, segment in enumerate(segments, start=1):
            handle.write(f"{index}\n")
            handle.write(
                f"{_format_srt_timestamp(segment['start'])} --> {_format_srt_timestamp(segment['end'])}\n"
            )
            handle.write(f"{segment['text'].strip()}\n\n")


def _write_vtt(segments, path):
    with open(path, "w", encoding="utf-8") as handle:
        handle.write("WEBVTT\n\n")
        for segment in segments:
            handle.write(
                f"{_format_vtt_timestamp(segment['start'])} --> {_format_vtt_timestamp(segment['end'])}\n"
            )
            handle.write(f"{segment['text'].strip()}\n\n")


def _transcribe_with_faster_whisper(video_path, language=""):
    try:
        from faster_whisper import WhisperModel
    except ImportError:
        return "", [], ["faster-whisper not installed; subtitle generation skipped."]

    model_name = os.getenv("LEAGUECLIPS_AI_WHISPER_MODEL", "small")
    device = os.getenv("LEAGUECLIPS_AI_DEVICE", "cpu")
    compute_type = os.getenv("LEAGUECLIPS_AI_COMPUTE_TYPE", "int8")
    lang = (language or os.getenv("LEAGUECLIPS_AI_LANGUAGE") or "").strip() or None

    try:
        model = WhisperModel(model_name, device=device, compute_type=compute_type)
        raw_segments, _ = model.transcribe(
            video_path,
            beam_size=5,
            vad_filter=True,
            language=lang,
            word_timestamps=False,
        )
    except Exception as exc:
        return "", [], [f"Subtitle generation failed: {exc}"]

    transcript_parts = []
    segments = []
    for segment in raw_segments:
        text = (segment.text or "").strip()
        if not text:
            continue
        start = float(segment.start)
        end = float(segment.end)
        if end <= start + 0.05:
            continue
        segments.append({"start": start, "end": end, "text": text})
        transcript_parts.append(text)

    transcript = " ".join(transcript_parts).strip()
    return transcript, segments, []


def _remap_segments_to_keep_ranges(segments, keep_ranges):
    remapped = []
    offset = 0.0
    for keep_start, keep_end in keep_ranges:
        for segment in segments:
            seg_start = float(segment.get("start", 0.0))
            seg_end = float(segment.get("end", seg_start))
            if seg_end <= keep_start or seg_start >= keep_end:
                continue

            new_start = max(seg_start, keep_start) - keep_start + offset
            new_end = min(seg_end, keep_end) - keep_start + offset
            if new_end <= new_start + 0.05:
                continue
            remapped.append(
                {
                    "start": round(new_start, 3),
                    "end": round(new_end, 3),
                    "text": segment.get("text", "").strip(),
                }
            )
        offset += keep_end - keep_start
    return remapped


def _parse_json_response(text):
    if not text:
        return None
    try:
        parsed = json.loads(text)
        if isinstance(parsed, dict):
            return parsed
    except (TypeError, ValueError, json.JSONDecodeError):
        pass

    match = re.search(r"\{.*\}", str(text), re.DOTALL)
    if not match:
        return None
    try:
        parsed = json.loads(match.group(0))
        if isinstance(parsed, dict):
            return parsed
    except (TypeError, ValueError, json.JSONDecodeError):
        return None
    return None


def _normalize_ollama_host(raw_host):
    host = (raw_host or "http://127.0.0.1:11434").strip().rstrip("/")
    if not host:
        host = "http://127.0.0.1:11434"
    for suffix in ("/api/generate", "/api", "/v1/chat/completions", "/v1/completions"):
        if host.endswith(suffix):
            host = host[: -len(suffix)].rstrip("/")
    return host


def _chat_completion_text(payload):
    choices = payload.get("choices")
    if not isinstance(choices, list) or not choices:
        return ""
    first = choices[0] if isinstance(choices[0], dict) else {}
    message = first.get("message")
    if isinstance(message, dict) and isinstance(message.get("content"), str):
        return message["content"]
    text = first.get("text")
    return text if isinstance(text, str) else ""


def _llm_json_response(host, model, prompt):
    native = requests.post(
        f"{host}/api/generate",
        json={"model": model, "prompt": prompt, "stream": False, "format": "json"},
        timeout=70,
    )
    if native.status_code != 404:
        native.raise_for_status()
        return _parse_json_response((native.json() or {}).get("response", ""))

    chat = requests.post(
        f"{host}/v1/chat/completions",
        json={
            "model": model,
            "messages": [{"role": "user", "content": prompt}],
            "temperature": 0.1,
        },
        timeout=70,
    )
    chat.raise_for_status()
    return _parse_json_response(_chat_completion_text(chat.json() or {}))


def _maybe_generate_ai_remove_ranges(remove_query, transcript_segments, duration_seconds):
    query = str(remove_query or "").strip()
    if not query:
        return [], None
    if not transcript_segments:
        return [], "AI scene removal skipped because no transcript was available."

    model = (os.getenv("LEAGUECLIPS_OLLAMA_MODEL") or "").strip()
    if not model:
        return [], "AI scene removal skipped because LEAGUECLIPS_OLLAMA_MODEL is not set."

    host = _normalize_ollama_host(os.getenv("LEAGUECLIPS_OLLAMA_HOST", "http://127.0.0.1:11434"))
    compact_segments = []
    for seg in transcript_segments[:220]:
        compact_segments.append(
            {
                "start": round(float(seg["start"]), 2),
                "end": round(float(seg["end"]), 2),
                "text": str(seg.get("text", ""))[:180],
            }
        )

    prompt = (
        "You are assisting a video editor.\n"
        "Return STRICT JSON only.\n"
        "Given transcript segments and an edit instruction, choose ranges to remove.\n"
        "Output format: {\"remove_ranges\":[{\"start\":12.5,\"end\":34.2,\"reason\":\"...\"}]}\n"
        "Rules:\n"
        "- start/end must be numbers in seconds.\n"
        "- end must be greater than start.\n"
        "- Do not return more than 8 ranges.\n"
        "- Do not invent content not represented in transcript text.\n"
        f"Duration seconds: {round(float(duration_seconds), 3)}\n"
        f"Edit instruction: {query}\n"
        f"Transcript segments JSON: {json.dumps(compact_segments, ensure_ascii=False)}"
    )

    try:
        response = _llm_json_response(host, model, prompt) or {}
        raw_ranges = response.get("remove_ranges")
        if not isinstance(raw_ranges, list):
            return [], "AI scene removal returned no ranges."
        normalized = _normalize_remove_ranges(raw_ranges, duration_seconds)
        if not normalized:
            return [], "AI scene removal did not produce any valid ranges."
        return normalized, None
    except Exception as exc:
        return [], f"AI scene removal failed: {exc}"


def _render_mobile_cut_video(source_path, keep_ranges, output_path, mobile_mode="blur"):
    has_audio = _probe_has_audio_stream(source_path)
    filter_parts = []

    if len(keep_ranges) == 1:
        start, end = keep_ranges[0]
        filter_parts.append(f"[0:v]trim=start={start:.3f}:end={end:.3f},setpts=PTS-STARTPTS[vcat]")
        if has_audio:
            filter_parts.append(f"[0:a]atrim=start={start:.3f}:end={end:.3f},asetpts=PTS-STARTPTS[acat]")
    else:
        for index, (start, end) in enumerate(keep_ranges):
            filter_parts.append(f"[0:v]trim=start={start:.3f}:end={end:.3f},setpts=PTS-STARTPTS[v{index}]")
            if has_audio:
                filter_parts.append(
                    f"[0:a]atrim=start={start:.3f}:end={end:.3f},asetpts=PTS-STARTPTS[a{index}]"
                )
        if has_audio:
            concat_inputs = "".join([f"[v{i}][a{i}]" for i in range(len(keep_ranges))])
            filter_parts.append(
                f"{concat_inputs}concat=n={len(keep_ranges)}:v=1:a=1[vcat][acat]"
            )
        else:
            concat_inputs = "".join([f"[v{i}]" for i in range(len(keep_ranges))])
            filter_parts.append(f"{concat_inputs}concat=n={len(keep_ranges)}:v=1:a=0[vcat]")

    mode = str(mobile_mode or "blur").strip().lower()
    if mode == "crop":
        filter_parts.append(
            "[vcat]scale=1080:1920:force_original_aspect_ratio=increase,crop=1080:1920[vout]"
        )
    elif mode == "pad":
        filter_parts.append(
            "[vcat]scale=1080:1920:force_original_aspect_ratio=decrease,"
            "pad=1080:1920:(ow-iw)/2:(oh-ih)/2:black[vout]"
        )
    else:
        filter_parts.append("[vcat]split[vbg][vfg]")
        filter_parts.append(
            "[vbg]scale=1080:1920:force_original_aspect_ratio=increase,"
            "crop=1080:1920,gblur=sigma=24[vblur]"
        )
        filter_parts.append("[vfg]scale=1080:1920:force_original_aspect_ratio=decrease[vfit]")
        filter_parts.append("[vblur][vfit]overlay=(W-w)/2:(H-h)/2[vout]")

    cmd = [
        "ffmpeg",
        "-y",
        "-i",
        source_path,
        "-filter_complex",
        ";".join(filter_parts),
        "-map",
        "[vout]",
    ]
    if has_audio:
        cmd.extend(["-map", "[acat]"])
    else:
        cmd.append("-an")
    cmd.extend(
        [
            "-c:v",
            "libx264",
            "-preset",
            "veryfast",
            "-crf",
            os.getenv("LEAGUECLIPS_OUTPUT_CRF", "18"),
            "-pix_fmt",
            "yuv420p",
        ]
    )
    if has_audio:
        cmd.extend(["-c:a", "aac", "-b:a", "160k"])
    cmd.append(output_path)
    _run_cmd(cmd)


def _escape_subtitles_filter_path(path):
    escaped = os.path.realpath(path).replace("\\", "/")
    escaped = escaped.replace(":", r"\:")
    escaped = escaped.replace("'", r"\'")
    return escaped


def _burn_subtitles_into_video(source_path, srt_path, output_path):
    subtitles_filter = f"subtitles='{_escape_subtitles_filter_path(srt_path)}'"
    _run_cmd(
        [
            "ffmpeg",
            "-y",
            "-i",
            source_path,
            "-vf",
            subtitles_filter,
            "-map",
            "0:v:0",
            "-map",
            "0:a?",
            "-c:v",
            "libx264",
            "-preset",
            "veryfast",
            "-crf",
            os.getenv("LEAGUECLIPS_OUTPUT_CRF", "18"),
            "-pix_fmt",
            "yuv420p",
            "-c:a",
            "copy",
            output_path,
        ]
    )


def _artifact_url(job_id, file_name):
    return f"/sitcom/files/{job_id}/{file_name}"


def resolve_sitcom_artifact_path(job_id, file_name):
    safe_job_id = re.sub(r"[^A-Za-z0-9_-]", "", str(job_id or ""))
    safe_file_name = os.path.basename(file_name or "")
    if not safe_job_id or not safe_file_name:
        raise ValueError("Invalid artifact path.")

    base_dir = os.path.realpath(get_sitcom_edit_dir())
    job_dir = os.path.realpath(os.path.join(base_dir, safe_job_id))
    if not job_dir.startswith(base_dir):
        raise ValueError("Invalid job directory.")

    full_path = os.path.realpath(os.path.join(job_dir, safe_file_name))
    if not full_path.startswith(job_dir):
        raise ValueError("Invalid artifact file.")
    if not os.path.exists(full_path):
        raise FileNotFoundError(full_path)
    return full_path


def run_sitcom_pipeline(
    task_id,
    source_filename="",
    remove_ranges=None,
    ai_remove_query="",
    ai_auto_edit=False,
    target_keep_ratio=0.72,
    mobile_mode="blur",
    split_into_clips=True,
    clip_max_seconds=60.0,
    clip_min_seconds=12.0,
    generate_subtitles=True,
    burn_subtitles=True,
    subtitle_language="",
    progress_callback=None,
):
    source_name, source_path = resolve_sitcom_source_path(source_filename)
    safe_task_id = re.sub(r"[^A-Za-z0-9_-]", "", str(task_id or ""))
    if not safe_task_id:
        raise ValueError("Missing task id for sitcom processing.")

    edit_root = get_sitcom_edit_dir()
    os.makedirs(edit_root, exist_ok=True)
    job_dir = os.path.join(edit_root, safe_task_id)
    os.makedirs(job_dir, exist_ok=True)

    def emit(state, message, extra=None):
        if not progress_callback:
            return
        payload = {
            "message": message,
            "task_id": safe_task_id,
            "source": source_name,
        }
        if extra:
            payload.update(extra)
        progress_callback(state, payload)

    duration_seconds = _probe_duration_seconds(source_path)
    if duration_seconds <= 1.0:
        raise ValueError("Source video is too short to edit.")

    warnings = []
    transcript = ""
    transcript_segments = []
    safe_target_keep_ratio = _safe_keep_ratio(target_keep_ratio)
    safe_split_into_clips = bool(split_into_clips)
    safe_clip_max_seconds = _safe_clip_seconds(clip_max_seconds, default=60.0, min_value=10.0, max_value=600.0)
    safe_clip_min_seconds = _safe_clip_seconds(clip_min_seconds, default=12.0, min_value=3.0, max_value=240.0)

    needs_transcript = bool(generate_subtitles or str(ai_remove_query or "").strip() or ai_auto_edit)
    if needs_transcript:
        emit("SITCOM_TRANSCRIBING", "Transcribing source audio for subtitles/AI scene matching.")
        transcript, transcript_segments, transcribe_warnings = _transcribe_with_faster_whisper(
            source_path,
            language=subtitle_language,
        )
        warnings.extend(transcribe_warnings)

    manual_remove_ranges = _normalize_remove_ranges(remove_ranges or [], duration_seconds)
    ai_generated_ranges = []
    effective_ai_query = str(ai_remove_query or "").strip()
    ai_cut_strategy = "none"
    if ai_auto_edit and not effective_ai_query:
        effective_ai_query = _default_auto_remove_instruction(safe_target_keep_ratio)

    if effective_ai_query:
        emit("SITCOM_AI_MATCHING", "Finding removable scenes from AI instruction.")
        ai_generated_ranges, ai_warning = _maybe_generate_ai_remove_ranges(
            effective_ai_query,
            transcript_segments,
            duration_seconds,
        )
        if ai_warning:
            warnings.append(ai_warning)
        if ai_generated_ranges:
            ai_cut_strategy = "llm_query" if str(ai_remove_query or "").strip() else "llm_auto"
        elif ai_auto_edit:
            heuristic_ranges = _heuristic_auto_remove_ranges(
                transcript_segments,
                duration_seconds,
                target_keep_ratio=safe_target_keep_ratio,
            )
            if heuristic_ranges:
                ai_generated_ranges = heuristic_ranges
                ai_cut_strategy = "heuristic_auto"
                warnings.append("AI auto-cut used heuristic fallback because LLM ranges were unavailable.")

    if ai_generated_ranges and ai_auto_edit:
        max_remove_ratio = min(0.82, (1.0 - safe_target_keep_ratio) + 0.12)
        max_remove_seconds = float(duration_seconds) * max_remove_ratio
        ai_removed_seconds = _ranges_total_seconds(ai_generated_ranges)
        if ai_removed_seconds > max_remove_seconds + 0.2:
            capped_ranges = []
            consumed = 0.0
            for start, end in ai_generated_ranges:
                segment_len = max(0.0, end - start)
                if consumed >= max_remove_seconds:
                    break
                remaining = max_remove_seconds - consumed
                if segment_len <= remaining + 0.02:
                    capped_ranges.append((start, end))
                    consumed += segment_len
                    continue

                partial_end = start + max(0.0, remaining)
                if partial_end - start >= 0.35:
                    capped_ranges.append((start, partial_end))
                consumed = max_remove_seconds
                break

            ai_generated_ranges = _normalize_remove_ranges(
                [{"start": start, "end": end} for start, end in capped_ranges],
                duration_seconds,
            )
            warnings.append("AI auto-cut was trimmed to avoid over-cutting the source.")

    if ai_auto_edit and not ai_generated_ranges and not manual_remove_ranges:
        warnings.append("AI auto-cut did not find confident ranges; rendering full source with formatting/subtitles.")

    combined_remove_ranges = _normalize_remove_ranges(
        [{"start": start, "end": end} for start, end in (manual_remove_ranges + ai_generated_ranges)],
        duration_seconds,
    )
    keep_ranges = _invert_ranges(combined_remove_ranges, duration_seconds)
    if not keep_ranges:
        raise ValueError("All footage was removed. Reduce removal ranges.")

    temp_dir = get_temp_dir()
    os.makedirs(temp_dir, exist_ok=True)
    rendered_no_subtitles = os.path.join(temp_dir, f"{safe_task_id}_sitcom_base.mp4")
    final_output_path = os.path.join(job_dir, SITCOM_OUTPUT_FILENAME)
    srt_path = os.path.join(job_dir, SITCOM_SUBTITLES_SRT)
    vtt_path = os.path.join(job_dir, SITCOM_SUBTITLES_VTT)
    metadata_path = os.path.join(job_dir, SITCOM_METADATA_FILENAME)

    emit(
        "SITCOM_RENDERING",
        "Rendering vertical mobile edit.",
        {"keep_segments": len(keep_ranges), "remove_segments": len(combined_remove_ranges)},
    )
    _render_mobile_cut_video(source_path, keep_ranges, rendered_no_subtitles, mobile_mode=mobile_mode)

    subtitle_segments = []
    if generate_subtitles and transcript_segments:
        emit("SITCOM_SUBTITLES", "Writing subtitle files.")
        subtitle_segments = _remap_segments_to_keep_ranges(transcript_segments, keep_ranges)
        if subtitle_segments:
            _write_srt(subtitle_segments, srt_path)
            _write_vtt(subtitle_segments, vtt_path)
        else:
            warnings.append("No subtitle segments matched the kept footage.")

    if burn_subtitles and os.path.exists(srt_path):
        emit("SITCOM_FINALIZING", "Burning subtitles into final export.")
        _burn_subtitles_into_video(rendered_no_subtitles, srt_path, final_output_path)
        try:
            os.remove(rendered_no_subtitles)
        except OSError:
            pass
    else:
        if os.path.exists(final_output_path):
            os.remove(final_output_path)
        os.replace(rendered_no_subtitles, final_output_path)

    clip_artifacts = []
    if safe_split_into_clips:
        emit("SITCOM_SPLITTING", "Splitting final output into sequential clips.")
        final_duration = _probe_duration_seconds(final_output_path)
        clip_ranges = _split_timeline_into_clip_ranges(
            final_duration,
            max_clip_seconds=safe_clip_max_seconds,
            min_clip_seconds=safe_clip_min_seconds,
        )

        # If the timeline is effectively one clip already, expose the final output as clip 1.
        if len(clip_ranges) <= 1:
            clip_artifacts.append(
                {
                    "index": 1,
                    "filename": SITCOM_OUTPUT_FILENAME,
                    "url": _artifact_url(safe_task_id, SITCOM_OUTPUT_FILENAME),
                    "start": 0.0,
                    "end": round(float(final_duration), 3),
                    "duration": round(float(final_duration), 3),
                }
            )
        else:
            for idx, (start, end) in enumerate(clip_ranges, start=1):
                emit(
                    "SITCOM_SPLITTING",
                    f"Rendering clip {idx}/{len(clip_ranges)}.",
                    {"clip_index": idx, "clip_total": len(clip_ranges)},
                )
                clip_name = f"{SITCOM_CLIP_PREFIX}{idx:03d}.mp4"
                clip_path = os.path.join(job_dir, clip_name)
                _render_output_clip(final_output_path, start, end, clip_path)
                clip_artifacts.append(
                    {
                        "index": idx,
                        "filename": clip_name,
                        "url": _artifact_url(safe_task_id, clip_name),
                        "start": round(float(start), 3),
                        "end": round(float(end), 3),
                        "duration": round(float(end - start), 3),
                    }
                )

    kept_total_seconds = _ranges_total_seconds(keep_ranges)
    removed_total_seconds = _ranges_total_seconds(combined_remove_ranges)

    result = {
        "status": "done",
        "task_id": safe_task_id,
        "source_filename": source_name,
        "duration_seconds": round(float(duration_seconds), 2),
        "kept_seconds": round(kept_total_seconds, 3),
        "removed_seconds": round(removed_total_seconds, 3),
        "kept_ratio": round((kept_total_seconds / float(duration_seconds)) if duration_seconds else 0.0, 4),
        "kept_segments": [
            {
                "start": round(start, 3),
                "end": round(end, 3),
                "duration": round(end - start, 3),
            }
            for start, end in keep_ranges
        ],
        "removed_segments": [
            {
                "start": round(start, 3),
                "end": round(end, 3),
                "duration": round(end - start, 3),
            }
            for start, end in combined_remove_ranges
        ],
        "manual_removed_segments": [
            {
                "start": round(start, 3),
                "end": round(end, 3),
                "duration": round(end - start, 3),
            }
            for start, end in manual_remove_ranges
        ],
        "ai_removed_segments": [
            {
                "start": round(start, 3),
                "end": round(end, 3),
                "duration": round(end - start, 3),
            }
            for start, end in ai_generated_ranges
        ],
        "ai_remove_query": str(ai_remove_query or "").strip(),
        "ai_effective_query": effective_ai_query,
        "ai_cut_strategy": ai_cut_strategy,
        "options": {
            "mobile_mode": str(mobile_mode or "blur").strip().lower(),
            "generate_subtitles": bool(generate_subtitles),
            "burn_subtitles": bool(burn_subtitles),
            "subtitle_language": (subtitle_language or "").strip(),
            "ai_auto_edit": bool(ai_auto_edit),
            "target_keep_ratio": round(safe_target_keep_ratio, 4),
            "split_into_clips": safe_split_into_clips,
            "clip_max_seconds": round(safe_clip_max_seconds, 3),
            "clip_min_seconds": round(safe_clip_min_seconds, 3),
        },
        "transcript_excerpt": transcript[:1500],
        "warnings": warnings,
        "artifacts": {
            "video": _artifact_url(safe_task_id, SITCOM_OUTPUT_FILENAME),
            "subtitles_srt": _artifact_url(safe_task_id, SITCOM_SUBTITLES_SRT)
            if os.path.exists(srt_path)
            else None,
            "subtitles_vtt": _artifact_url(safe_task_id, SITCOM_SUBTITLES_VTT)
            if os.path.exists(vtt_path)
            else None,
            "metadata_json": _artifact_url(safe_task_id, SITCOM_METADATA_FILENAME),
            "clips": clip_artifacts,
        },
        "processed_at_utc": datetime.utcnow().isoformat() + "Z",
    }

    with open(metadata_path, "w", encoding="utf-8") as handle:
        json.dump(result, handle, ensure_ascii=False, indent=2)

    emit("SITCOM_DONE", "Sitcom edit complete.", {"result": result})
    return result


def list_sitcom_edits():
    edit_dir = get_sitcom_edit_dir()
    os.makedirs(edit_dir, exist_ok=True)

    items = []
    for job_id in os.listdir(edit_dir):
        job_dir = os.path.join(edit_dir, job_id)
        if not os.path.isdir(job_dir):
            continue
        output_path = os.path.join(job_dir, SITCOM_OUTPUT_FILENAME)
        metadata_path = os.path.join(job_dir, SITCOM_METADATA_FILENAME)
        if not os.path.exists(output_path):
            continue

        metadata = {}
        if os.path.exists(metadata_path):
            try:
                with open(metadata_path, "r", encoding="utf-8") as handle:
                    parsed = json.load(handle)
                if isinstance(parsed, dict):
                    metadata = parsed
            except (OSError, ValueError, json.JSONDecodeError):
                metadata = {}

        artifact_data = metadata.get("artifacts") if isinstance(metadata.get("artifacts"), dict) else {}
        clip_artifacts = artifact_data.get("clips") if isinstance(artifact_data.get("clips"), list) else []
        normalized_clips = []
        for clip in clip_artifacts:
            if not isinstance(clip, dict):
                continue
            url = clip.get("url")
            filename = clip.get("filename")
            if not url and filename:
                url = _artifact_url(job_id, filename)
            if not url:
                continue
            normalized_clips.append(
                {
                    "index": clip.get("index"),
                    "filename": filename,
                    "url": url,
                    "start": clip.get("start"),
                    "end": clip.get("end"),
                    "duration": clip.get("duration"),
                }
            )

        if not normalized_clips:
            fallback_names = sorted(
                [
                    name
                    for name in os.listdir(job_dir)
                    if name.startswith(SITCOM_CLIP_PREFIX) and name.lower().endswith(".mp4")
                ]
            )
            for idx, file_name in enumerate(fallback_names, start=1):
                normalized_clips.append(
                    {
                        "index": idx,
                        "filename": file_name,
                        "url": _artifact_url(job_id, file_name),
                    }
                )

        preview_url = normalized_clips[0]["url"] if normalized_clips else _artifact_url(job_id, SITCOM_OUTPUT_FILENAME)

        items.append(
            {
                "task_id": job_id,
                "source_filename": metadata.get("source_filename"),
                "processed_at_utc": metadata.get("processed_at_utc"),
                "size_bytes": os.path.getsize(output_path),
                "video": preview_url,
                "video_full": _artifact_url(job_id, SITCOM_OUTPUT_FILENAME),
                "clips": normalized_clips,
                "clip_count": len(normalized_clips),
                "subtitles_srt": _artifact_url(job_id, SITCOM_SUBTITLES_SRT)
                if os.path.exists(os.path.join(job_dir, SITCOM_SUBTITLES_SRT))
                else None,
                "subtitles_vtt": _artifact_url(job_id, SITCOM_SUBTITLES_VTT)
                if os.path.exists(os.path.join(job_dir, SITCOM_SUBTITLES_VTT))
                else None,
                "mobile_mode": (metadata.get("options") or {}).get("mobile_mode"),
                "removed_segments": metadata.get("removed_segments", []),
            }
        )

    items.sort(
        key=lambda item: item.get("processed_at_utc") or "",
        reverse=True,
    )
    return items
