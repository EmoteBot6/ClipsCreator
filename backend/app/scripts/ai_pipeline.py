import json
import os
import re
import subprocess
from collections import Counter
from datetime import datetime

import requests

from .video_editor import get_temp_dir
from .video_import import get_source_library_dir, get_source_video_path, get_video_storage_dir

LEAGUE_GLOSSARY = [
    "OP",
    "meta",
    "new meta",
    "outplay",
    "snowball",
    "macro",
    "micro",
    "mechanics",
    "laning phase",
    "solo kill",
    "1v2",
    "Korean solo queue",
    "rank 1",
    "smurf",
    "high elo",
    "teamfight",
    "power spike",
    "carry",
]
HEURISTIC_STOPWORDS = {
    "the",
    "and",
    "for",
    "with",
    "that",
    "this",
    "you",
    "your",
    "from",
    "have",
    "just",
    "they",
    "them",
    "are",
    "was",
    "were",
    "not",
    "but",
    "into",
}

CHAMPION_ALIASES = {
    "Aatrox": ["aatrox"],
    "Ahri": ["ahri"],
    "Akali": ["akali"],
    "Ashe": ["ashe"],
    "Azir": ["azir"],
    "Camille": ["camille"],
    "Darius": ["darius"],
    "Draven": ["draven"],
    "Ezreal": ["ezreal"],
    "Fiora": ["fiora"],
    "Graves": ["graves"],
    "Irelia": ["irelia"],
    "Jax": ["jax"],
    "Jhin": ["jhin"],
    "Kai'Sa": ["kaisa", "kai'sa", "kai sa"],
    "Katarina": ["katarina", "kat"],
    "Kayn": ["kayn"],
    "Kha'Zix": ["khazix", "kha'zix", "kha zix"],
    "Lee Sin": ["lee", "leesin", "lee sin"],
    "LeBlanc": ["leblanc", "lb"],
    "Morgana": ["morgana"],
    "Riven": ["riven"],
    "Sylas": ["sylas"],
    "Thresh": ["thresh"],
    "Vayne": ["vayne"],
    "Viego": ["viego"],
    "Yasuo": ["yasuo"],
    "Yone": ["yone"],
    "Zed": ["zed"],
}


DEFAULT_ITEMS_CROP = (1080, 970, 1240, 1080)
DEFAULT_ITEMS_CROP_EDITED = (760, 1470, 1080, 1920)
MIN_ITEMS_CROP_SIZE = 40
VINE_BOOM_FILENAME = "vine_boom_sound.mp3"
DEFAULT_OUTPUT_CRF = "18"
DEFAULT_AUDIO_BITRATE = "160k"
DEFAULT_ITEMS_OVERLAY_SECONDS = 2.0
DEFAULT_SUBTITLE_SCORELINE_Y = 600
DEFAULT_SUBTITLE_TITLE_Y = 170
DEFAULT_SUBTITLE_TITLE_SECONDS = 4.0
DEFAULT_SUB_WORD_MAX_VISIBLE = 1.0
DEFAULT_TIERLIST_Y = 120
DEFAULT_TIERLIST_X = 160
DEFAULT_CENTERED_MOBILE_SUBTITLE_OFFSET = 110
AI_ARTIFACT_FILENAMES = {
    "subtitles_srt": "ai_subtitles.srt",
    "subtitles_vtt": "ai_subtitles.vtt",
    "subtitles_ass": "ai_subtitles.ass",
    "edited_video": "ai_edited.mp4",
    "analysis_json": "ai_analysis.json",
}


def _is_within_directory(base_dir, candidate_path):
    try:
        return os.path.commonpath([base_dir, candidate_path]) == base_dir
    except ValueError:
        return False


def _env_float(name, default, min_value=None, max_value=None):
    try:
        value = float(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        value = float(default)
    if min_value is not None:
        value = max(float(min_value), value)
    if max_value is not None:
        value = min(float(max_value), value)
    return value


def _env_int(name, default, min_value=None, max_value=None):
    try:
        value = int(float(os.getenv(name, str(default))))
    except (TypeError, ValueError):
        value = int(default)
    if min_value is not None:
        value = max(int(min_value), value)
    if max_value is not None:
        value = min(int(max_value), value)
    return value


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


def _safe_clip_path(subdir, filename):
    base_dir = os.path.realpath(get_video_storage_dir())
    safe_filename = os.path.basename(filename or "")
    if not safe_filename or not safe_filename.lower().endswith(".mp4"):
        raise ValueError("Invalid clip filename.")

    safe_subdir = (subdir or "").strip().replace("\\", "/")
    safe_subdir = re.sub(r"/+", "/", safe_subdir).strip("/")
    if ".." in safe_subdir.split("/"):
        raise ValueError("Invalid clip path.")

    clip_dir = os.path.realpath(os.path.join(base_dir, safe_subdir))
    if not _is_within_directory(base_dir, clip_dir):
        raise ValueError("Invalid clip directory.")

    clip_path = os.path.realpath(os.path.join(clip_dir, safe_filename))
    if not _is_within_directory(clip_dir, clip_path):
        raise ValueError("Invalid clip file path.")
    if not os.path.exists(clip_path):
        raise FileNotFoundError(f"Clip not found: {safe_subdir}/{safe_filename}")

    return safe_subdir, safe_filename, clip_dir, clip_path


def _timestamp_to_seconds(timestamp):
    parts = [int(part) for part in str(timestamp).split(":")]
    if len(parts) == 2:
        minutes, seconds = parts
        return minutes * 60 + seconds
    if len(parts) == 3:
        hours, minutes, seconds = parts
        return hours * 3600 + minutes * 60 + seconds
    raise ValueError(f"Unsupported timestamp: {timestamp}")


def _normalize_crop_tuple(x1, y1, x2, y2):
    left = min(int(x1), int(x2))
    right = max(int(x1), int(x2))
    top = min(int(y1), int(y2))
    bottom = max(int(y1), int(y2))

    if right - left < MIN_ITEMS_CROP_SIZE or bottom - top < MIN_ITEMS_CROP_SIZE:
        return DEFAULT_ITEMS_CROP
    return (left, top, right, bottom)


def _crop_region_from_env(env_name, fallback):
    raw = (os.getenv(env_name) or "").strip()
    if raw:
        parts = [piece.strip() for piece in raw.split(",")]
        if len(parts) == 4:
            try:
                x1, y1, x2, y2 = [int(piece) for piece in parts]
                return _normalize_crop_tuple(x1, y1, x2, y2)
            except ValueError:
                pass
    return _normalize_crop_tuple(*fallback)


def _items_crop_region():
    return _crop_region_from_env("LEAGUECLIPS_ITEMS_CROP", DEFAULT_ITEMS_CROP)


def _items_crop_region_edited():
    return _crop_region_from_env("LEAGUECLIPS_ITEMS_CROP_EDITED", DEFAULT_ITEMS_CROP_EDITED)


def _resolve_source_video_path(clip_context):
    source_name = (clip_context or {}).get("source_video") or ""
    if source_name:
        candidate = os.path.join(get_source_library_dir(), os.path.basename(source_name))
        if os.path.exists(candidate):
            return candidate
    fallback = get_source_video_path()
    if os.path.exists(fallback):
        return fallback
    return None


def _resolve_vine_boom_path():
    # Keep static SFX in repo so it is part of image builds and versioned in git.
    repo_asset = os.path.realpath(
        os.path.join(os.path.dirname(__file__), "..", "assets", "audio", VINE_BOOM_FILENAME)
    )
    if os.path.exists(repo_asset):
        return repo_asset

    # Backward-compatible fallback for older local setups.
    candidate = os.path.join(get_source_library_dir(), VINE_BOOM_FILENAME)
    if os.path.exists(candidate):
        return candidate
    return None


def _overlay_items_intro(base_video_path, output_path, clip_context):
    source_video_path = _resolve_source_video_path(clip_context or {})
    if not source_video_path:
        return "Show Items enabled but source video was not found; skipped item overlay."

    start_timestamp = (clip_context or {}).get("start_timestamp")
    warning = None
    using_source_timing = True
    try:
        clip_start_seconds = float(_timestamp_to_seconds(start_timestamp))
    except Exception:
        # Older clips can miss clip_context metadata. Fallback to the edited clip itself.
        using_source_timing = False
        clip_start_seconds = 0.0
        source_video_path = base_video_path
        warning = (
            "Show Items enabled but clip timing metadata is missing; "
            "used in-clip crop fallback."
        )

    overlay_seconds = _env_float(
        "LEAGUECLIPS_ITEMS_OVERLAY_SECONDS",
        DEFAULT_ITEMS_OVERLAY_SECONDS,
        min_value=0.5,
        max_value=6.0,
    )

    if using_source_timing:
        x1, y1, x2, y2 = _items_crop_region()
        source_w, source_h = 1920, 1080
    else:
        x1, y1, x2, y2 = _items_crop_region_edited()
        source_w, source_h = 1080, 1920

    crop_x = max(0, min(source_w - 1, x1))
    crop_y = max(0, min(source_h - 1, y1))
    crop_w = max(MIN_ITEMS_CROP_SIZE, min(source_w - crop_x, x2 - x1))
    crop_h = max(MIN_ITEMS_CROP_SIZE, min(source_h - crop_y, y2 - y1))

    _run_cmd(
        [
            "ffmpeg",
            "-y",
            "-i",
            base_video_path,
            "-ss",
            f"{clip_start_seconds:.3f}",
            "-to",
            f"{(clip_start_seconds + overlay_seconds):.3f}",
            "-i",
            source_video_path,
            "-filter_complex",
            (
                f"[1:v]crop={crop_w}:{crop_h}:{crop_x}:{crop_y},scale=1080:-2,setsar=1[item];"
                f"[0:v][item]overlay=(W-w)/2:0:enable='lt(t,{overlay_seconds:.3f})'[v]"
            ),
            "-map",
            "[v]",
            "-map",
            "0:a?",
            *_h264_aac_encode_args(),
            output_path,
        ]
    )
    return warning


def _apply_vine_boom_intro(base_video_path, output_path):
    vine_boom_path = _resolve_vine_boom_path()
    if not vine_boom_path:
        return f"Vine Boom enabled but {VINE_BOOM_FILENAME} was not found; skipped intro sound."

    if _probe_has_audio_stream(base_video_path):
        filter_complex = (
            "[0:a]volume=1.0[base];"
            "[1:a]atrim=0:2.0,asetpts=PTS-STARTPTS,volume=1.25[boom];"
            "[base][boom]amix=inputs=2:duration=first:dropout_transition=0[a]"
        )
    else:
        filter_complex = "[1:a]atrim=0:2.0,asetpts=PTS-STARTPTS,volume=1.25[a]"

    _run_cmd(
        [
            "ffmpeg",
            "-y",
            "-i",
            base_video_path,
            "-i",
            vine_boom_path,
            "-filter_complex",
            filter_complex,
            "-map",
            "0:v",
            "-map",
            "[a]",
            "-c:v",
            "copy",
            "-c:a",
            "aac",
            "-b:a",
            "192k",
            "-shortest",
            output_path,
        ]
    )
    return None


def _format_subtitle_timestamp(seconds, millisecond_separator):
    seconds = max(0.0, float(seconds))
    millis = int(round(seconds * 1000))
    hours = millis // 3600000
    millis %= 3600000
    minutes = millis // 60000
    millis %= 60000
    secs = millis // 1000
    ms = millis % 1000
    return f"{hours:02d}:{minutes:02d}:{secs:02d}{millisecond_separator}{ms:03d}"


def _format_srt_timestamp(seconds):
    return _format_subtitle_timestamp(seconds, ",")


def _format_vtt_timestamp(seconds):
    return _format_subtitle_timestamp(seconds, ".")


def _write_srt(segments, path):
    with open(path, "w", encoding="utf-8") as handle:
        for idx, segment in enumerate(segments, start=1):
            handle.write(f"{idx}\n")
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


def _format_ass_timestamp(seconds):
    seconds = max(0.0, float(seconds))
    hours = int(seconds // 3600)
    minutes = int((seconds % 3600) // 60)
    secs = int(seconds % 60)
    centis = int(round((seconds - int(seconds)) * 100))
    if centis >= 100:
        centis = 0
        secs += 1
    if secs >= 60:
        secs = 0
        minutes += 1
    if minutes >= 60:
        minutes = 0
        hours += 1
    return f"{hours}:{minutes:02d}:{secs:02d}.{centis:02d}"


def _overlay_layout_from_context(clip_context):
    scoreline_y = _env_int(
        "LEAGUECLIPS_SUBTITLE_SCORELINE_Y",
        DEFAULT_SUBTITLE_SCORELINE_Y,
        min_value=60,
        max_value=1700,
    )
    title_y = _env_int(
        "LEAGUECLIPS_SUBTITLE_TITLE_Y",
        DEFAULT_SUBTITLE_TITLE_Y,
        min_value=40,
        max_value=620,
    )
    tierlist_y = _env_int(
        "LEAGUECLIPS_TIERLIST_Y",
        DEFAULT_TIERLIST_Y,
        min_value=24,
        max_value=720,
    )
    tierlist_x = _env_int(
        "LEAGUECLIPS_TIERLIST_X",
        DEFAULT_TIERLIST_X,
        min_value=40,
        max_value=540,
    )

    context = clip_context if isinstance(clip_context, dict) else {}
    if context.get("layout_mode") == "centered_mobile_pad":
        pad_top = max(0, int(context.get("pad_top", 0) or 0))
        rendered_height = max(1, int(context.get("rendered_height", 0) or 0))
        scoreline_y = max(
            820,
            min(
                1820,
                pad_top + rendered_height - DEFAULT_CENTERED_MOBILE_SUBTITLE_OFFSET,
            ),
        )
        if pad_top > 0:
            title_y = max(48, min(pad_top - 110, 180))
            tierlist_y = max(42, min(pad_top - 260, 120))
        else:
            title_y = max(48, title_y)
            tierlist_y = max(42, tierlist_y)

    return {
        "scoreline_y": scoreline_y,
        "title_y": title_y,
        "tierlist_y": tierlist_y,
        "tierlist_x": tierlist_x,
    }


def _ass_escape_text(text):
    return str(text or "").replace("{", r"\{").replace("}", r"\}")


def _write_ass_word_cues(word_cues, path, overlay_title="", clip_context=None, tier_list_cues=None):
    layout = _overlay_layout_from_context(clip_context)
    scoreline_y = layout["scoreline_y"]
    title_y = layout["title_y"]
    tierlist_y = layout["tierlist_y"]
    tierlist_x = layout["tierlist_x"]
    overlay_title = str(overlay_title or "").strip()
    if len(overlay_title) > 90:
        overlay_title = overlay_title[:90].rstrip()

    title_seconds = _env_float(
        "LEAGUECLIPS_SUBTITLE_TITLE_SECONDS",
        DEFAULT_SUBTITLE_TITLE_SECONDS,
        min_value=1.2,
        max_value=12.0,
    )

    header = """[Script Info]
ScriptType: v4.00+
PlayResX: 1080
PlayResY: 1920
WrapStyle: 2
ScaledBorderAndShadow: yes

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: TitlePop,Impact,88,&H00FFFFFF,&H00FFFFFF,&H00000000,&H82000000,-1,0,0,0,100,100,0,0,1,6,1,8,48,48,56,1
Style: WordPop,Arial Black,72,&H0000F4FF,&H0000F4FF,&H00000000,&H78000000,-1,0,0,0,100,100,1,0,1,5,1,8,60,60,120,1
Style: TierList,Arial Black,54,&H00FFFFFF,&H00FFFFFF,&H00000000,&H88000000,-1,0,0,0,100,100,0,0,1,4,1,7,44,44,44,1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(header)
        if overlay_title:
            title_start = _format_ass_timestamp(0.0)
            title_end = _format_ass_timestamp(title_seconds)
            title_text = _ass_escape_text(overlay_title)
            title_effect = (
                "\\an8"
                f"\\pos(540,{title_y})"
                "\\t(0,150,\\fscx108\\fscy108)"
                "\\t(150,320,\\fscx100\\fscy100)"
            )
            handle.write(
                f"Dialogue: 1,{title_start},{title_end},TitlePop,,0,0,0,,{{{title_effect}}}{title_text}\n"
            )

        for cue in tier_list_cues or []:
            text = _ass_escape_text(cue.get("text", ""))
            if not text:
                continue
            start = _format_ass_timestamp(cue["start"])
            end = _format_ass_timestamp(cue["end"])
            effect = (
                "\\an7"
                f"\\pos({tierlist_x},{tierlist_y})"
                "\\t(0,160,\\fscx102\\fscy102)"
                "\\t(160,320,\\fscx100\\fscy100)"
            )
            handle.write(f"Dialogue: 2,{start},{end},TierList,,0,0,0,,{{{effect}}}{text}\n")

        for cue in word_cues:
            text = _ass_escape_text(cue.get("text", ""))
            if not text:
                continue
            start = _format_ass_timestamp(cue["start"])
            end = _format_ass_timestamp(cue["end"])
            effect = (
                "\\an8"
                f"\\pos(540,{scoreline_y})"
                "\\t(0,120,\\fscx135\\fscy135)"
                "\\t(120,260,\\fscx100\\fscy100)"
            )
            handle.write(f"Dialogue: 0,{start},{end},WordPop,,0,0,0,,{{{effect}}}{text}\n")


def _build_word_cues(segments):
    cues = []
    for segment in segments or []:
        seg_start = float(segment.get("start", 0.0))
        seg_end = float(segment.get("end", seg_start))
        if seg_end <= seg_start:
            continue

        words_data = segment.get("words")
        if words_data:
            for wd in words_data:
                text = str(wd.get("word", "")).strip()
                if not text:
                    continue
                ws = max(seg_start, float(wd.get("start", seg_start)))
                we = min(seg_end, float(wd.get("end", ws + 0.06)))
                if we <= ws:
                    we = min(seg_end, ws + 0.12)
                cues.append({"start": ws, "end": we, "text": text})
            continue

        tokens = [w for w in str(segment.get("text", "")).split() if w.strip()]
        if not tokens:
            continue
        seg_len = seg_end - seg_start
        slice_len = max(0.08, seg_len / len(tokens))
        for idx, token in enumerate(tokens):
            ws = seg_start + idx * slice_len
            we = min(seg_end, ws + slice_len * 0.92)
            cues.append({"start": ws, "end": we, "text": token})
    return _apply_word_visibility_rules(cues)


def _normalize_tier_list_entry(value):
    text = re.sub(r"\s+", " ", str(value or "")).strip(" -_.,:;!?")
    if not text:
        return ""
    if len(text) > 28:
        text = text[:28].rstrip()
    return text


def _normalize_tier_list_payload(payload):
    items = []
    if isinstance(payload, dict):
        candidates = payload.get("ranked_terms") or payload.get("items") or payload.get("terms")
        if isinstance(candidates, list):
            items = candidates
    if not items:
        return None, "Ollama tier-list response was not valid JSON."

    normalized = []
    seen = set()
    for item in items:
        entry = _normalize_tier_list_entry(item)
        key = entry.lower()
        if not entry or key in seen:
            continue
        seen.add(key)
        normalized.append(entry)
        if len(normalized) >= 4:
            break
    if not normalized:
        return None, "Ollama tier-list response contained no usable entries."
    return normalized, None


def _maybe_generate_tier_list_with_ollama(transcript, duration_seconds, clip_context, champion_guess):
    model = (os.getenv("LEAGUECLIPS_OLLAMA_MODEL") or "").strip()
    if not model:
        return None, None

    host = _normalize_ollama_host(os.getenv("LEAGUECLIPS_OLLAMA_HOST", "http://127.0.0.1:11434"))
    context = _league_prompt_context(clip_context, transcript, champion_guess, duration_seconds)
    prompt = (
        "You create short ranked tier lists for League of Legends clips.\n"
        "Return STRICT JSON only with key ranked_terms.\n"
        "ranked_terms must be an array of exactly 4 short words or short phrases.\n"
        "Use terms grounded in the transcript/context only. Do not invent facts.\n"
        "Prefer concise punchy clip words that would look good as a numbered tier list.\n"
        f"Context JSON:\n{json.dumps(context, ensure_ascii=False)}"
    )
    try:
        payload = _llm_json_response(host, model, prompt)
        return _normalize_tier_list_payload(payload)
    except Exception as exc:
        return None, f"Ollama tier-list generation skipped: {exc}"


def _heuristic_tier_list_entries(transcript, champion_guess, clip_context):
    tokens = re.findall(r"[A-Za-z][A-Za-z0-9']+", (transcript or "").lower())
    banned = HEURISTIC_STOPWORDS | {
        "yeah",
        "okay",
        "gonna",
        "really",
        "literally",
        "actually",
        "there",
        "their",
        "about",
        "because",
        "still",
        "would",
        "could",
        "should",
        "that's",
        "dont",
        "didnt",
        "cant",
        "isnt",
    }
    filtered = [token for token in tokens if len(token) > 3 and token not in banned]
    ranked = []
    seen = set()

    champion = champion_guess.get("champion", "")
    if champion and champion != "Unknown":
        normalized = _normalize_tier_list_entry(champion)
        if normalized:
            ranked.append(normalized)
            seen.add(normalized.lower())

    for token, _count in Counter(filtered).most_common(12):
        normalized = _normalize_tier_list_entry(token)
        key = normalized.lower()
        if not normalized or key in seen:
            continue
        seen.add(key)
        ranked.append(normalized)
        if len(ranked) >= 4:
            break

    if len(ranked) < 4:
        for candidate in _extract_streamers_from_label(clip_context.get("label", "")):
            normalized = _normalize_tier_list_entry(candidate)
            key = normalized.lower()
            if not normalized or key in seen:
                continue
            seen.add(key)
            ranked.append(normalized)
            if len(ranked) >= 4:
                break

    while len(ranked) < 4:
        fallback = f"Clip {len(ranked) + 1}"
        ranked.append(fallback)
    return ranked[:4]


def _generate_tier_list_entries(transcript, duration_seconds, clip_context, champion_guess):
    llm_entries, llm_warning = _maybe_generate_tier_list_with_ollama(
        transcript,
        duration_seconds,
        clip_context,
        champion_guess,
    )
    warning = llm_warning if llm_warning else None
    if llm_entries:
        return llm_entries, warning
    return _heuristic_tier_list_entries(transcript, champion_guess, clip_context), warning


def _build_tier_list_cues(entries, duration_seconds):
    normalized_entries = [_normalize_tier_list_entry(entry) for entry in (entries or [])]
    normalized_entries = [entry for entry in normalized_entries if entry]
    if not normalized_entries or duration_seconds <= 0:
        return []

    total_slots = max(1, len(normalized_entries))
    intro = min(1.2, max(0.35, duration_seconds * 0.08))
    available = max(0.4, duration_seconds - intro)
    slot = available / float(total_slots)
    cues = []

    for idx in range(total_slots):
        start = intro + (slot * idx)
        end = duration_seconds if idx + 1 >= total_slots else intro + (slot * (idx + 1))
        lines = []
        for line_idx in range(4):
            prefix = f"{line_idx + 1}. "
            value = normalized_entries[line_idx] if line_idx <= idx and line_idx < len(normalized_entries) else ""
            lines.append(f"{prefix}{value}".rstrip())
        cues.append(
            {
                "start": round(max(0.0, start), 2),
                "end": round(max(start + 0.25, end), 2),
                "text": r"\N".join(lines),
            }
        )
    return cues


def _apply_word_visibility_rules(cues):
    if not cues:
        return cues
    max_visible = _env_float(
        "LEAGUECLIPS_SUB_WORD_MAX_VISIBLE",
        DEFAULT_SUB_WORD_MAX_VISIBLE,
        min_value=0.2,
        max_value=2.0,
    )

    # Use speech-aligned starts as-is. Only clamp ends for readability/overlap safety.
    ordered = sorted(cues, key=lambda item: (float(item["start"]), float(item["end"])))
    normalized = []
    for idx, cue in enumerate(ordered):
        start = max(0.0, float(cue["start"]))
        end = max(start + 0.05, float(cue["end"]))
        end = min(end, start + max_visible)

        next_start = None
        if idx + 1 < len(ordered):
            next_start = max(0.0, float(ordered[idx + 1]["start"]))
        if next_start is not None:
            # Ensure the current word vanishes slightly before the next one appears.
            end = min(end, max(start + 0.05, next_start - 0.01))

        normalized.append({"start": start, "end": end, "text": cue["text"]})
    return normalized


def _normalize_whisper_word(raw_word, segment_start, segment_end):
    token = (getattr(raw_word, "word", "") or "").strip()
    if not token:
        return None

    raw_start = getattr(raw_word, "start", None)
    raw_end = getattr(raw_word, "end", None)
    ws = float(raw_start if raw_start is not None else segment_start)
    we = float(raw_end if raw_end is not None else segment_end)

    # Some backends can return per-segment-relative word offsets.
    # If detected, convert to absolute clip offsets.
    if ws < segment_start - 0.05 or we < segment_start - 0.05:
        ws += segment_start
        we += segment_start

    # Clamp to segment bounds and enforce small positive duration.
    ws = max(segment_start, min(ws, segment_end))
    we = max(ws + 0.05, min(we, segment_end))
    return {"word": token, "start": ws, "end": we}


def _normalize_whisper_segment(raw_segment):
    text = (raw_segment.text or "").strip()
    if not text:
        return None

    segment_start = float(raw_segment.start)
    segment_end = float(raw_segment.end)
    words = []
    for raw_word in list(getattr(raw_segment, "words", None) or []):
        normalized_word = _normalize_whisper_word(raw_word, segment_start, segment_end)
        if normalized_word:
            words.append(normalized_word)
    return {
        "start": segment_start,
        "end": segment_end,
        "text": text,
        "words": words,
    }


def _ollama_default_analysis(context):
    return {
        "champion_guess": context["champion_guess"],
        "champion_confidence": context["champion_confidence"],
        "play_type": "highlight",
        "lingo_keywords": ["outplay", "mechanics"],
        "angle": "high-elo clip moment",
    }


def _normalize_generated_text_payload(gen_inner, analysis_inner, model):
    if not gen_inner:
        return None, "Ollama generation response was not valid JSON."

    title = (gen_inner.get("title") or "").strip()
    description = (gen_inner.get("description") or "").strip()
    summary = (gen_inner.get("summary") or "").strip()
    tags = gen_inner.get("tags") if isinstance(gen_inner.get("tags"), list) else []
    if not (title and description):
        return None, "Ollama response missing title/description."

    cleaned_tags = [str(tag).strip() for tag in tags if str(tag).strip()][:8]
    return {
        "title": title,
        "description": description,
        "summary": summary or description[:180],
        "tags": cleaned_tags,
        "analysis": analysis_inner,
        "model": model,
    }, None


def _transcribe_with_faster_whisper(clip_path):
    try:
        from faster_whisper import WhisperModel
    except ImportError:
        return "", [], [], ["faster-whisper not installed; skipped subtitle generation."]

    model_name = os.getenv("LEAGUECLIPS_AI_WHISPER_MODEL", "small")
    device = os.getenv("LEAGUECLIPS_AI_DEVICE", "cpu")
    compute_type = os.getenv("LEAGUECLIPS_AI_COMPUTE_TYPE", "int8")
    language = os.getenv("LEAGUECLIPS_AI_LANGUAGE") or None

    try:
        model = WhisperModel(model_name, device=device, compute_type=compute_type)
        raw_segments, _ = model.transcribe(
            clip_path,
            beam_size=5,
            vad_filter=True,
            language=language,
            word_timestamps=True,
        )
    except Exception as exc:
        return "", [], [], [f"Subtitle generation failed: {exc}"]

    segments = []
    transcript_parts = []
    fallback_word_timing_segments = 0
    for raw_segment in raw_segments:
        segment = _normalize_whisper_segment(raw_segment)
        if not segment:
            continue
        if not segment["words"]:
            fallback_word_timing_segments += 1
        segments.append(segment)
        transcript_parts.append(segment["text"])

    transcript = " ".join(transcript_parts).strip()
    word_cues = _build_word_cues(segments)
    warnings = []
    if fallback_word_timing_segments:
        warnings.append(
            f"Word-level timestamps missing for {fallback_word_timing_segments} segment(s); using estimated per-word timing there."
        )
    return transcript, segments, word_cues, warnings


def _json_dict_or_none(raw_text):
    try:
        parsed = json.loads(raw_text)
    except (TypeError, ValueError, json.JSONDecodeError):
        return None
    return parsed if isinstance(parsed, dict) else None


def _parse_json_response(response_text):
    if not response_text:
        return None
    parsed = _json_dict_or_none(response_text)
    if parsed is not None:
        return parsed

    fenced = re.search(r"\{.*\}", str(response_text), re.DOTALL)
    if fenced:
        return _json_dict_or_none(fenced.group(0))
    return None


def _read_json_file_dict(path):
    if not os.path.exists(path):
        return {}
    try:
        with open(path, "r", encoding="utf-8") as handle:
            loaded = json.load(handle)
            return loaded if isinstance(loaded, dict) else {}
    except (OSError, ValueError, json.JSONDecodeError):
        return {}


def _read_clip_context(clip_dir):
    return _read_json_file_dict(os.path.join(clip_dir, "clip_context.json"))


def _extract_streamers_from_label(label):
    text = (label or "").strip()
    if not text:
        return []
    tokens = re.findall(r"[A-Za-z0-9][A-Za-z0-9']*", text)
    ignore = {"clip", "outro", "intro", "part", "vs", "and"}
    names = []
    for token in tokens:
        lowered = token.lower()
        if lowered in ignore:
            continue
        if len(token) < 3:
            continue
        names.append(token)
    # preserve order, unique
    seen = set()
    unique = []
    for name in names:
        key = name.lower()
        if key in seen:
            continue
        seen.add(key)
        unique.append(name)
    return unique[:4]


def _guess_champion_from_text(*parts):
    combined = " ".join([part for part in parts if part]).lower()
    if not combined:
        return {"champion": "Unknown", "confidence": "low", "evidence": []}

    scores = []
    for champion, aliases in CHAMPION_ALIASES.items():
        hits = []
        for alias in aliases:
            if re.search(rf"\b{re.escape(alias.lower())}\b", combined):
                hits.append(alias)
        if hits:
            scores.append((len(hits), champion, hits))

    if not scores:
        return {"champion": "Unknown", "confidence": "low", "evidence": []}

    scores.sort(reverse=True, key=lambda item: item[0])
    top = scores[0]
    confidence = "high" if top[0] >= 2 else "medium"
    return {"champion": top[1], "confidence": confidence, "evidence": top[2]}


def _league_prompt_context(clip_context, transcript, champion_guess, duration_seconds):
    streamers = _extract_streamers_from_label(clip_context.get("label", ""))
    return {
        "duration_seconds": round(float(duration_seconds), 2),
        "clip_label": clip_context.get("label", ""),
        "clip_start": clip_context.get("start_timestamp", ""),
        "clip_end": clip_context.get("end_timestamp", ""),
        "source_video": clip_context.get("source_video", ""),
        "streamers": streamers,
        "champion_guess": champion_guess.get("champion", "Unknown"),
        "champion_confidence": champion_guess.get("confidence", "low"),
        "champion_evidence": champion_guess.get("evidence", []),
        "transcript_excerpt": (transcript or "")[:2500],
        "league_lingo": LEAGUE_GLOSSARY,
    }


def _normalize_ollama_host(raw_host):
    host = (raw_host or "http://127.0.0.1:11434").strip().rstrip("/")
    if not host:
        host = "http://127.0.0.1:11434"
    for suffix in ("/api/generate", "/api", "/v1/chat/completions", "/v1/completions"):
        if host.endswith(suffix):
            host = host[: -len(suffix)].rstrip("/")
    return host


def _ollama_request_timeout():
    connect_timeout = _env_float(
        "LEAGUECLIPS_OLLAMA_CONNECT_TIMEOUT_SECONDS",
        10.0,
        min_value=1.0,
        max_value=120.0,
    )
    read_timeout = _env_float(
        "LEAGUECLIPS_OLLAMA_TIMEOUT_SECONDS",
        90.0,
        min_value=5.0,
        max_value=900.0,
    )
    return (connect_timeout, read_timeout)


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


def _llm_json_response(host, model, prompt):
    timeout = _ollama_request_timeout()
    native = requests.post(
        f"{host}/api/generate",
        json={"model": model, "prompt": prompt, "stream": False, "format": "json"},
        timeout=timeout,
    )
    if native.status_code != 404:
        native.raise_for_status()
        return _parse_json_response((native.json() or {}).get("response", ""))

    # Fallback for OpenAI-compatible gateways that do not expose /api/generate.
    chat = requests.post(
        f"{host}/v1/chat/completions",
        json={
            "model": model,
            "messages": [{"role": "user", "content": prompt}],
            "temperature": 0.2,
        },
        timeout=timeout,
    )
    chat.raise_for_status()
    chat_text = _chat_completion_text(chat.json() or {})
    return _parse_json_response(chat_text)


def _maybe_generate_with_ollama(transcript, duration_seconds, clip_context, champion_guess):
    model = (os.getenv("LEAGUECLIPS_OLLAMA_MODEL") or "").strip()
    if not model:
        return None, None

    host = _normalize_ollama_host(os.getenv("LEAGUECLIPS_OLLAMA_HOST", "http://127.0.0.1:11434"))
    context = _league_prompt_context(clip_context, transcript, champion_guess, duration_seconds)

    analysis_prompt = (
        "You are a League of Legends highlights analyst.\n"
        "Return STRICT JSON only.\n"
        "Task: infer likely champion and play-type while avoiding fabricated claims.\n"
        "If uncertain, set confidence low and explain uncertainty.\n"
        "JSON keys required: champion_guess, champion_confidence, play_type, lingo_keywords, angle.\n"
        f"Context JSON:\n{json.dumps(context, ensure_ascii=False)}"
    )

    generation_prompt_template = (
        "You are a League of Legends highlights copywriter.\n"
        "Use authentic LoL terminology naturally (OP, meta, outplay, mechanics, high elo, etc.) "
        "only where justified by context.\n"
        "Do NOT invent facts.\n"
        "Return STRICT JSON only with keys: title, description, summary, tags.\n"
        "title must be <= 85 chars and punchy.\n"
        "description should be 2-4 short sentences, creator-friendly.\n"
        f"Context JSON:\n{json.dumps(context, ensure_ascii=False)}\n"
        "Analysis JSON:\n{analysis_json}"
    )
    try:
        analysis_inner = _llm_json_response(host, model, analysis_prompt)
        if not analysis_inner:
            analysis_inner = _ollama_default_analysis(context)

        generation_prompt = generation_prompt_template.replace(
            "{analysis_json}", json.dumps(analysis_inner, ensure_ascii=False)
        )
        gen_inner = _llm_json_response(host, model, generation_prompt)
        return _normalize_generated_text_payload(gen_inner, analysis_inner, model)
    except Exception as exc:
        return None, f"Ollama generation skipped: {exc}"


def _heuristic_title_description(transcript, clip_stem, duration_seconds, clip_context, champion_guess):
    words = re.findall(r"[A-Za-z][A-Za-z0-9']+", (transcript or "").lower())
    filtered = [w for w in words if len(w) > 3 and w not in HEURISTIC_STOPWORDS]
    top = [item[0] for item in Counter(filtered).most_common(3)]

    streamer_text = ", ".join(_extract_streamers_from_label(clip_context.get("label", "")))
    champion = champion_guess.get("champion", "Unknown")
    if champion != "Unknown":
        focus = champion
    else:
        focus = ", ".join(top).title() if top else "League of Legends"
    title = f"{focus} OP Outplay - High Elo Clip"
    if len(title) > 78:
        title = title[:75].rstrip() + "..."

    if transcript:
        excerpt = transcript[:240].strip()
        description = (
            f"AI-generated recap for {clip_stem} ({int(duration_seconds)}s): "
            f"{excerpt}{'...' if len(transcript) > 240 else ''}"
        )
    else:
        description = (
            f"AI-generated recap for {clip_stem} ({int(duration_seconds)}s). "
            "No transcript available; generated from visual timing and audio heuristics."
        )
    if streamer_text:
        description = f"Featured streamer(s): {streamer_text}. " + description
    if champion != "Unknown":
        description = f"Likely champion: {champion}. " + description

    summary = description[:180]
    return {"title": title, "description": description, "summary": summary, "tags": ["leagueoflegends", "high-elo", "outplay"]}


def _run_cmd(args):
    completed = subprocess.run(args, capture_output=True, text=True)
    if completed.returncode != 0:
        stderr = (completed.stderr or "").strip()
        stdout = (completed.stdout or "").strip()
        raise RuntimeError(stderr or stdout or f"Command failed: {' '.join(args)}")
    return completed


def _probe_duration_seconds(clip_path):
    completed = _run_cmd(
        [
            "ffprobe",
            "-v",
            "error",
            "-show_entries",
            "format=duration",
            "-of",
            "default=noprint_wrappers=1:nokey=1",
            clip_path,
        ]
    )
    try:
        return float((completed.stdout or "").strip())
    except (TypeError, ValueError):
        raise RuntimeError("Could not parse clip duration from ffprobe output.")


def _probe_has_audio_stream(clip_path):
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
            clip_path,
        ]
    )
    return bool((completed.stdout or "").strip())


def _ffmpeg_copy(input_path, output_path):
    if _same_path(input_path, output_path):
        return
    _run_cmd(["ffmpeg", "-y", "-i", input_path, "-c", "copy", output_path])


def _ffmpeg_copy_or_reencode(input_path, output_path):
    try:
        _ffmpeg_copy(input_path, output_path)
    except Exception:
        _run_cmd(
            [
                "ffmpeg",
                "-y",
                "-i",
                input_path,
                *_h264_aac_encode_args(),
                output_path,
            ]
        )


def _same_path(path_a, path_b):
    try:
        return os.path.samefile(path_a, path_b)
    except OSError:
        return os.path.realpath(path_a) == os.path.realpath(path_b)


def _stage_output_path(temp_dir, edited_path, label):
    stem = os.path.splitext(os.path.basename(edited_path))[0]
    return os.path.join(temp_dir, f"{stem}_{label}_{os.getpid()}.mp4")


def _render_subtitles_stage(input_path, subtitles_ass_path, output_path):
    _run_cmd(
        [
            "ffmpeg",
            "-y",
            "-i",
            input_path,
            "-vf",
            f"ass={subtitles_ass_path}",
            *_h264_aac_encode_args(),
            output_path,
        ]
    )


def _apply_warnable_stage(input_path, output_path, stage_func, warnings):
    warning = stage_func(input_path, output_path)
    if warning:
        warnings.append(warning)
        return input_path
    return output_path


def _finalize_edited_output(current_input, clip_path, edited_path):
    if _same_path(current_input, clip_path):
        _ffmpeg_copy_or_reencode(clip_path, edited_path)
        return False
    if _same_path(current_input, edited_path):
        return False

    try:
        if os.path.exists(edited_path):
            os.remove(edited_path)
        os.replace(current_input, edited_path)
        return True
    except OSError:
        _ffmpeg_copy(current_input, edited_path)
        return False


def _cleanup_files(paths):
    for path in paths:
        try:
            if os.path.exists(path):
                os.remove(path)
        except OSError:
            pass


def _create_edited_clip(
    clip_path,
    edited_path,
    subtitles_ass_path=None,
    show_items=False,
    vine_boom=False,
    clip_context=None,
):
    warnings = []
    temp_dir = get_temp_dir()
    os.makedirs(temp_dir, exist_ok=True)
    temp_stages = []
    current_input = clip_path
    clip_context = clip_context or {}

    try:
        if subtitles_ass_path and os.path.exists(subtitles_ass_path):
            subtitles_stage = _stage_output_path(temp_dir, edited_path, "subtitles")
            _render_subtitles_stage(current_input, subtitles_ass_path, subtitles_stage)
            temp_stages.append(subtitles_stage)
            current_input = subtitles_stage

        if show_items:
            items_stage = _stage_output_path(temp_dir, edited_path, "items")
            next_input = _apply_warnable_stage(
                current_input,
                items_stage,
                lambda base, out: _overlay_items_intro(base, out, clip_context),
                warnings,
            )
            if not _same_path(next_input, current_input):
                temp_stages.append(items_stage)
                current_input = next_input

        if vine_boom:
            boom_stage = _stage_output_path(temp_dir, edited_path, "vineboom")
            next_input = _apply_warnable_stage(
                current_input,
                boom_stage,
                _apply_vine_boom_intro,
                warnings,
            )
            if not _same_path(next_input, current_input):
                temp_stages.append(boom_stage)
                current_input = next_input

        moved_stage = _finalize_edited_output(current_input, clip_path, edited_path)
        if moved_stage:
            temp_stages = [path for path in temp_stages if not _same_path(path, current_input)]
    finally:
        _cleanup_files(temp_stages)
    return warnings


def _clip_url(subdir, file_name):
    return f"/videos/{subdir}/{file_name}" if subdir else f"/videos/root/{file_name}"


def _optional_clip_url(subdir, file_name, file_path):
    if not os.path.exists(file_path):
        return None
    return _clip_url(subdir, file_name)


def _artifact_paths(clip_dir):
    return {
        key: os.path.join(clip_dir, file_name)
        for key, file_name in AI_ARTIFACT_FILENAMES.items()
    }


def _artifact_urls(subdir, paths):
    return {
        "edited_video": _clip_url(subdir, AI_ARTIFACT_FILENAMES["edited_video"]),
        "subtitles_vtt": _optional_clip_url(
            subdir,
            AI_ARTIFACT_FILENAMES["subtitles_vtt"],
            paths["subtitles_vtt"],
        ),
        "subtitles_srt": _optional_clip_url(
            subdir,
            AI_ARTIFACT_FILENAMES["subtitles_srt"],
            paths["subtitles_srt"],
        ),
        "subtitles_ass": _optional_clip_url(
            subdir,
            AI_ARTIFACT_FILENAMES["subtitles_ass"],
            paths["subtitles_ass"],
        ),
        "analysis_json": _clip_url(subdir, AI_ARTIFACT_FILENAMES["analysis_json"]),
    }


def _write_transcript_artifacts(segments, srt_path, vtt_path):
    if not segments:
        return
    _write_srt(segments, srt_path)
    _write_vtt(segments, vtt_path)


def _generate_text_payload(transcript, clip_stem, duration_seconds, clip_context, champion_guess):
    llm_result, llm_warning = _maybe_generate_with_ollama(
        transcript,
        duration_seconds,
        clip_context,
        champion_guess,
    )
    warning = llm_warning if llm_warning else None
    if llm_result:
        return llm_result, warning
    return _heuristic_title_description(
        transcript,
        clip_stem,
        duration_seconds,
        clip_context,
        champion_guess,
    ), warning


def _full_highlight_window(duration_seconds):
    return [{"start": 0.0, "end": round(float(duration_seconds), 2)}]


def run_ai_pipeline_for_clip(
    subdir,
    filename,
    progress_callback=None,
    show_items=False,
    vine_boom=False,
    overlay_title="",
    tier_list_enabled=False,
):
    safe_subdir, safe_filename, clip_dir, clip_path = _safe_clip_path(subdir, filename)
    clip_stem = os.path.splitext(safe_filename)[0]
    clip_context = _read_clip_context(clip_dir)
    clip_label = clip_context.get("label", "")
    streamers = _extract_streamers_from_label(clip_label)
    overlay_title_text = str(overlay_title or "").strip()
    paths = _artifact_paths(clip_dir)
    srt_path = paths["subtitles_srt"]
    vtt_path = paths["subtitles_vtt"]
    ass_path = paths["subtitles_ass"]
    edited_path = paths["edited_video"]
    analysis_path = paths["analysis_json"]

    def emit(state, message, extra=None):
        if not progress_callback:
            return
        meta = {"clip": safe_filename, "subdir": safe_subdir, "message": message}
        if extra:
            meta.update(extra)
        progress_callback(state, meta)

    emit("AI_PREP", "Preparing AI processing context.")
    duration_seconds = _probe_duration_seconds(clip_path)
    if duration_seconds <= 0:
        raise ValueError("Could not read clip duration.")

    warnings = []
    emit("AI_TRANSCRIBING", "Generating subtitles with local speech model.")
    transcript, segments, word_cues, whisper_warnings = _transcribe_with_faster_whisper(clip_path)
    warnings.extend(whisper_warnings)
    _write_transcript_artifacts(segments, srt_path, vtt_path)

    champion_guess = _guess_champion_from_text(
        transcript,
        clip_label,
        clip_stem,
        clip_context.get("source_video", ""),
    )

    emit(
        "AI_ANALYZING",
        "Generating title and description." if not tier_list_enabled else "Generating title, description, and tier list.",
    )
    text_payload, llm_warning = _generate_text_payload(
        transcript,
        clip_stem,
        duration_seconds,
        clip_context,
        champion_guess,
    )
    if llm_warning:
        warnings.append(llm_warning)

    tier_list_entries = []
    tier_list_cues = []
    if tier_list_enabled:
        tier_list_entries, tier_warning = _generate_tier_list_entries(
            transcript,
            duration_seconds,
            clip_context,
            champion_guess,
        )
        if tier_warning:
            warnings.append(tier_warning)
        tier_list_cues = _build_tier_list_cues(tier_list_entries, duration_seconds)

    emit("AI_EDITING", "Rendering full clip with AI subtitles.")
    if word_cues or overlay_title_text or tier_list_cues:
        _write_ass_word_cues(
            word_cues,
            ass_path,
            overlay_title=overlay_title_text,
            clip_context=clip_context,
            tier_list_cues=tier_list_cues,
        )
    edit_warnings = _create_edited_clip(
        clip_path,
        edited_path,
        subtitles_ass_path=ass_path,
        show_items=show_items,
        vine_boom=vine_boom,
        clip_context=clip_context,
    )
    warnings.extend(edit_warnings)

    result = {
        "status": "done",
        "clip": safe_filename,
        "subdir": safe_subdir,
        "duration_seconds": round(duration_seconds, 2),
        "title": text_payload["title"],
        "description": text_payload["description"],
        "summary": text_payload.get("summary", ""),
        "tags": text_payload.get("tags", []),
        "streamers": streamers,
        "champion_guess": champion_guess,
        "clip_context": clip_context,
        "transcript_excerpt": transcript[:800],
        "highlights": _full_highlight_window(duration_seconds),
        "show_items": bool(show_items),
        "vine_boom": bool(vine_boom),
        "overlay_title": overlay_title_text,
        "tier_list_enabled": bool(tier_list_enabled),
        "tier_list_entries": tier_list_entries,
        "artifacts": _artifact_urls(safe_subdir, paths),
        "warnings": warnings,
        "llm_model": text_payload.get("model"),
        "processed_at_utc": datetime.utcnow().isoformat() + "Z",
    }

    with open(analysis_path, "w", encoding="utf-8") as handle:
        json.dump(result, handle, ensure_ascii=False, indent=2)

    emit("AI_DONE", "AI processing complete.", {"result": result})
    return result
