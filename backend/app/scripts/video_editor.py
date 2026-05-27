from moviepy.editor import CompositeVideoClip, VideoFileClip
import multiprocessing
import os
import json
import math
import logging
import time

try:
    import cv2
except ImportError:
    cv2 = None

from .video_import import get_list, get_source_video_path, get_video_storage_dir


logger = logging.getLogger(__name__)


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


class FrontendLogger:
    def __init__(self, update_callback=None):
        self.update_callback = update_callback
        self.min_interval = _safe_float_env(
            "LEAGUECLIPS_PROGRESS_UPDATE_SECONDS",
            0.75,
            min_value=0.1,
            max_value=10.0,
        )
        self.min_percent_step = _safe_float_env(
            "LEAGUECLIPS_PROGRESS_UPDATE_PERCENT_STEP",
            1.0,
            min_value=0.0,
            max_value=25.0,
        )
        self._last_update_at = 0.0
        self._last_percent = -1.0

    def callback(self, **kwargs):
        if self.update_callback:
            self.update_callback(kwargs)

    def __call__(self, message):
        logger.debug("MoviePy: %s", message)

    def _should_emit_progress(self, index, total):
        if not self.update_callback:
            return False

        now = time.monotonic()
        total = max(0, int(total or 0))
        index = max(0, int(index or 0))
        if total <= 0:
            if now - self._last_update_at >= self.min_interval:
                self._last_update_at = now
                return True
            return False

        percent = (index / float(total)) * 100.0
        is_final = index >= total
        enough_time = now - self._last_update_at >= self.min_interval
        enough_progress = (
            self._last_percent < 0
            or percent - self._last_percent >= self.min_percent_step
        )
        if is_final or enough_time or enough_progress:
            self._last_update_at = now
            self._last_percent = percent
            return True
        return False

    def iter_bar(self, **kwargs):
        chunk = kwargs.get("t", None)
        if chunk is None:
            chunk = kwargs.get("chunk", None)

        if chunk is None:
            return iter([])

        total = len(chunk)
        for index, item in enumerate(chunk):
            progress_index = index + 1
            if self._should_emit_progress(progress_index, total):
                self.update_callback({"index": index + 1, "total": total})
            yield item


def get_screens_dir():
    configured = os.getenv("LEAGUECLIPS_SCREENS_DIR")
    if configured:
        return configured
    if os.path.isdir("/screens"):
        return "/screens"
    return os.path.abspath("screens")


def get_temp_dir():
    configured = os.getenv("LEAGUECLIPS_TEMP_DIR")
    if configured:
        return configured
    if os.path.isdir("/tmp"):
        return "/tmp"
    return os.path.join(get_video_storage_dir(), ".tmp")


def timestamp_to_seconds(timestamp):
    parts = [int(part) for part in timestamp.split(":")]
    if len(parts) == 2:
        minutes, seconds = parts
        return minutes * 60 + seconds
    if len(parts) == 3:
        hours, minutes, seconds = parts
        return hours * 3600 + minutes * 60 + seconds
    raise ValueError(f"Unsupported timestamp: {timestamp}")


def seconds_to_timestamp(seconds):
    total_seconds = max(0, int(float(seconds)))
    hours = total_seconds // 3600
    minutes = (total_seconds % 3600) // 60
    secs = total_seconds % 60
    if hours > 0:
        return f"{hours:02d}:{minutes:02d}:{secs:02d}"
    return f"{minutes:02d}:{secs:02d}"


processes = 10
default = 1250, 770, 1600, 1080
minimap = 1650, 800, 1920, 1080
MIN_FACECAM_EDGE = 120
DEFAULT_FACECAM_CONFIDENCE = 0.62
FACECAM_PANEL_WIDTH = 1080
FACECAM_PANEL_HEIGHT = 640
FACECAM_PANEL_ASPECT = FACECAM_PANEL_WIDTH / float(FACECAM_PANEL_HEIGHT)
FACE_DETECTOR = None


def getStreamerCamData(streamer):
    match streamer:
        case "Alphatwins":
            return 1200, 680, 1920, 1080
        case "Tyler1":
            return 1218, 827, 1657, 1080
        case "Gemi":
            return 1280, 920, 1520, 1080
        case "Lathyrus":
            return 1225, 770, 1600, 1080
        case "Broxah":
            return 1250, 850, 1600, 1080
        case "Baus":
            return 1300, 800, 1600, 1080
        case "Caedrel":
            return 0, 750, 400, 1080
        case "Dantes":
            return 1180, 819, 1530, 1080
        case "Quante":
            return 1500, 700, 1920, 1080
        case "Nemesis":
            return default
        case "Aatreus":
            return minimap
        case "Gilius":
            return 1300, 890, 1550, 1080
        case "Yamatosdeath":
            return minimap
        case "TobiasFate":
            return 1330, 770, 1920, 1080
        case "Lider":
            return 1480, 830, 1920, 1080
        case "Spearshot":
            return 1250, 800, 1550, 1080
        case "Tempest":
            return 1400, 790, 1920, 1080
        case "Solarbacca":
            return 1330, 850, 1600, 1080
        case "Repobah":
            return 1350, 710, 1920, 1080
        case "Dazestray":
            return minimap
        case "TvMaik":
            return minimap
        case "Quantum":
            return minimap
        case "TrundleTop1":
            return minimap
        case "TheAverageAsianGamer":
            return 1330, 830, 1630, 1060
        case "Kesha":
            return 0, 750, 500, 1080
        case "Pinkward":
            return 1260, 780, 1630, 1080
        case "MrDunkYaGirl":
            return 0, 100, 460, 400
        case "Yeahthony":
            return 1350, 750, 1600, 1080
        case _:
            return default


def _safe_facecam_confidence_threshold():
    try:
        value = float(
            os.getenv("LEAGUECLIPS_FACECAM_DETECT_MIN_CONFIDENCE", str(DEFAULT_FACECAM_CONFIDENCE))
        )
    except ValueError:
        value = DEFAULT_FACECAM_CONFIDENCE
    return max(0.0, min(1.0, value))


def _get_face_detector():
    global FACE_DETECTOR
    if cv2 is None:
        return None
    if FACE_DETECTOR is not None:
        return FACE_DETECTOR
    cascade_path = os.path.join(cv2.data.haarcascades, "haarcascade_frontalface_default.xml")
    detector = cv2.CascadeClassifier(cascade_path)
    if detector.empty():
        return None
    FACE_DETECTOR = detector
    return FACE_DETECTOR


def _clamp_crop(x1, y1, x2, y2, frame_w, frame_h):
    left = max(0, min(int(frame_w) - 1, int(x1)))
    top = max(0, min(int(frame_h) - 1, int(y1)))
    right = min(int(frame_w), max(left + 1, int(x2)))
    bottom = min(int(frame_h), max(top + 1, int(y2)))

    if right - left < MIN_FACECAM_EDGE:
        if left == 0:
            right = min(int(frame_w), left + MIN_FACECAM_EDGE)
        else:
            left = max(0, right - MIN_FACECAM_EDGE)
    if bottom - top < MIN_FACECAM_EDGE:
        if top == 0:
            bottom = min(int(frame_h), top + MIN_FACECAM_EDGE)
        else:
            top = max(0, bottom - MIN_FACECAM_EDGE)
    if right <= left or bottom <= top:
        return None
    return (left, top, right, bottom)


def _nearest_facecam_anchor(crop, frame_w, frame_h):
    left, top, right, bottom = [int(v) for v in crop]
    left_gap = max(0, left)
    top_gap = max(0, top)
    right_gap = max(0, int(frame_w) - right)
    bottom_gap = max(0, int(frame_h) - bottom)
    horiz = "left" if left_gap <= right_gap else "right"
    vert = "top" if top_gap <= bottom_gap else "bottom"
    return horiz, vert


def _fit_axis_to_length(start, end, target_length, limit, anchor):
    limit = max(1, int(limit))
    start = int(start)
    end = int(end)
    current_length = max(1, end - start)
    target_length = max(MIN_FACECAM_EDGE, min(limit, int(round(target_length))))

    if current_length < target_length:
        delta = target_length - current_length
        if anchor in {"left", "top"}:
            new_start = float(start)
            new_end = float(end + delta)
        elif anchor in {"right", "bottom"}:
            new_start = float(start - delta)
            new_end = float(end)
        else:
            new_start = float(start) - (delta / 2.0)
            new_end = float(end) + (delta / 2.0)
    elif current_length > target_length:
        if anchor in {"left", "top"}:
            new_start = float(start)
            new_end = float(start + target_length)
        elif anchor in {"right", "bottom"}:
            new_start = float(end - target_length)
            new_end = float(end)
        else:
            midpoint = (start + end) / 2.0
            new_start = midpoint - (target_length / 2.0)
            new_end = midpoint + (target_length / 2.0)
    else:
        return start, end

    if new_start < 0.0:
        new_end -= new_start
        new_start = 0.0
    if new_end > float(limit):
        new_start -= new_end - float(limit)
        new_end = float(limit)

    new_start = max(0.0, new_start)
    new_end = min(float(limit), new_end)

    if (new_end - new_start) < target_length:
        if new_start <= 0.0:
            new_end = min(float(limit), float(target_length))
        else:
            new_start = max(0.0, float(limit - target_length))

    return int(round(new_start)), int(round(new_end))


def _fit_facecam_crop_to_panel(crop, frame_w, frame_h):
    if not crop:
        return None

    base_crop = _clamp_crop(*crop, frame_w, frame_h)
    if not base_crop:
        return None

    left, top, right, bottom = base_crop
    horiz_anchor, vert_anchor = _nearest_facecam_anchor(base_crop, frame_w, frame_h)
    width = max(1, right - left)
    height = max(1, bottom - top)
    crop_aspect = width / float(height)

    # Fill the portrait top panel by trimming inside the selected webcam crop
    # instead of centering a mismatched aspect ratio with empty side space.
    if crop_aspect < FACECAM_PANEL_ASPECT:
        desired_height = max(MIN_FACECAM_EDGE, int(round(width / FACECAM_PANEL_ASPECT)))
        top, bottom = _fit_axis_to_length(top, bottom, desired_height, frame_h, vert_anchor)
    elif crop_aspect > FACECAM_PANEL_ASPECT:
        desired_width = max(MIN_FACECAM_EDGE, int(round(height * FACECAM_PANEL_ASPECT)))
        left, right = _fit_axis_to_length(left, right, desired_width, frame_w, horiz_anchor)

    fitted_crop = _clamp_crop(left, top, right, bottom, frame_w, frame_h)
    if not fitted_crop:
        return base_crop
    return fitted_crop


def _box_iou(a, b):
    ax1, ay1, ax2, ay2 = a
    bx1, by1, bx2, by2 = b
    ix1 = max(ax1, bx1)
    iy1 = max(ay1, by1)
    ix2 = min(ax2, bx2)
    iy2 = min(ay2, by2)
    iw = max(0, ix2 - ix1)
    ih = max(0, iy2 - iy1)
    inter = iw * ih
    if inter <= 0:
        return 0.0
    area_a = max(1, (ax2 - ax1) * (ay2 - ay1))
    area_b = max(1, (bx2 - bx1) * (by2 - by1))
    return inter / float(area_a + area_b - inter)


def _sigmoid(value):
    return 1.0 / (1.0 + math.exp(-float(value)))


def _facecam_candidate_from_face(face_box, frame_w, frame_h, level_weight=0.0):
    x, y, w, h = [int(v) for v in face_box]
    cam_w = max(int(w * 4.2), 220)
    cam_h = max(int(h * 4.0), 180)
    cam_x = int(x - cam_w * 0.22)
    cam_y = int(y - cam_h * 0.24)
    crop = _clamp_crop(cam_x, cam_y, cam_x + cam_w, cam_y + cam_h, frame_w, frame_h)
    if not crop:
        return None

    left, top, right, bottom = crop
    width = max(1, right - left)
    height = max(1, bottom - top)
    area_ratio = (width * height) / float(max(1, frame_w * frame_h))
    face_ratio = (w * h) / float(width * height)

    center_x = left + (width / 2.0)
    center_y = top + (height / 2.0)
    corners = ((0.0, 0.0), (frame_w, 0.0), (0.0, frame_h), (frame_w, frame_h))
    nearest_corner = min(
        math.hypot(center_x - cx, center_y - cy)
        for cx, cy in corners
    )
    max_corner_dist = max(1.0, math.hypot(frame_w, frame_h))
    corner_score = max(0.0, min(1.0, 1.0 - (nearest_corner / max_corner_dist)))

    area_score = max(0.0, 1.0 - abs(area_ratio - 0.11) / 0.11)
    face_ratio_score = max(0.0, 1.0 - abs(face_ratio - 0.18) / 0.18)
    detector_score = _sigmoid(level_weight) if level_weight else 0.5
    score = (0.42 * corner_score) + (0.23 * area_score) + (0.20 * face_ratio_score) + (0.15 * detector_score)
    return {"box": crop, "score": max(0.0, min(1.0, score))}


def _detect_facecam_region(source_video_path, start_seconds, end_seconds):
    detector = _get_face_detector()
    if detector is None:
        return None, {"status": "detector_unavailable", "confidence": 0.0}

    capture = cv2.VideoCapture(source_video_path) if cv2 else None
    if capture is None or not capture.isOpened():
        return None, {"status": "video_open_failed", "confidence": 0.0}

    try:
        span = max(2.0, float(end_seconds) - float(start_seconds))
        sample_count = max(5, min(9, int(span // 3) + 4))
        sample_times = [
            float(start_seconds) + ((idx + 1) / float(sample_count + 1)) * span
            for idx in range(sample_count)
        ]

        candidates = []
        frame_w = None
        frame_h = None

        for timestamp in sample_times:
            capture.set(cv2.CAP_PROP_POS_MSEC, max(0.0, timestamp) * 1000.0)
            ok, frame = capture.read()
            if not ok or frame is None:
                continue

            frame_h, frame_w = frame.shape[:2]
            gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
            try:
                faces, _, weights = detector.detectMultiScale3(
                    gray,
                    scaleFactor=1.1,
                    minNeighbors=6,
                    minSize=(42, 42),
                    outputRejectLevels=True,
                )
            except Exception:
                faces = detector.detectMultiScale(
                    gray,
                    scaleFactor=1.1,
                    minNeighbors=6,
                    minSize=(42, 42),
                )
                weights = [0.0 for _ in range(len(faces))]
            if len(faces) == 0:
                continue

            ranked = []
            for idx, face in enumerate(faces):
                weight = float(weights[idx]) if idx < len(weights) else 0.0
                candidate = _facecam_candidate_from_face(face, frame_w, frame_h, level_weight=weight)
                if candidate:
                    ranked.append(candidate)
            if ranked:
                ranked.sort(key=lambda item: item["score"], reverse=True)
                candidates.append(ranked[0])

        if not candidates or frame_w is None or frame_h is None:
            return None, {"status": "no_faces", "confidence": 0.0, "samples": len(sample_times)}

        candidates.sort(key=lambda item: item["score"], reverse=True)
        seed = candidates[0]
        supporters = [item for item in candidates if _box_iou(seed["box"], item["box"]) >= 0.26]
        if not supporters:
            supporters = [seed]

        total_weight = sum(max(0.001, s["score"]) for s in supporters)
        left = int(round(sum(s["box"][0] * s["score"] for s in supporters) / total_weight))
        top = int(round(sum(s["box"][1] * s["score"] for s in supporters) / total_weight))
        right = int(round(sum(s["box"][2] * s["score"] for s in supporters) / total_weight))
        bottom = int(round(sum(s["box"][3] * s["score"] for s in supporters) / total_weight))
        merged = _clamp_crop(left, top, right, bottom, frame_w, frame_h)
        if not merged:
            return None, {"status": "invalid_merge", "confidence": 0.0}

        mean_support_score = sum(s["score"] for s in supporters) / float(len(supporters))
        detected_ratio = len(supporters) / float(max(1, len(sample_times)))
        consensus_ratio = len(supporters) / float(max(1, len(candidates)))
        confidence = (0.50 * mean_support_score) + (0.30 * detected_ratio) + (0.20 * consensus_ratio)
        confidence = max(0.0, min(1.0, confidence))

        return merged, {
            "status": "ok",
            "confidence": confidence,
            "samples": len(sample_times),
            "detections": len(candidates),
            "supporters": len(supporters),
        }
    finally:
        capture.release()


def render_highlight_clip(
    source_video_path,
    clip_dir,
    clip_index,
    clip_label,
    start_seconds,
    end_seconds,
    progress_callback=None,
    start_timestamp=None,
    end_timestamp=None,
    temp_audio_token="",
):
    screens_dir = get_screens_dir()
    os.makedirs(screens_dir, exist_ok=True)

    fallback_crop = getStreamerCamData(clip_label)
    x1d, y1d, x2d, y2d = fallback_crop

    threshold = _safe_facecam_confidence_threshold()
    detection_crop, detection_meta = _detect_facecam_region(
        source_video_path,
        start_seconds,
        end_seconds,
    )
    if detection_crop and float(detection_meta.get("confidence", 0.0)) >= threshold:
        x1d, y1d, x2d, y2d = detection_crop
        facecam_used = "detected"
    else:
        facecam_used = "fallback"

    base_clip = VideoFileClip(source_video_path).subclip(start_seconds, end_seconds)
    frame_w, frame_h = [int(v) for v in base_clip.size]
    raw_facecam_crop = (x1d, y1d, x2d, y2d)
    fitted_facecam_crop = _fit_facecam_crop_to_panel(raw_facecam_crop, frame_w, frame_h) or raw_facecam_crop
    x1d, y1d, x2d, y2d = fitted_facecam_crop

    cam = (
        base_clip
        .crop(x1=x1d, y1=y1d, x2=x2d, y2=y2d)
        .resize(newsize=(FACECAM_PANEL_WIDTH, FACECAM_PANEL_HEIGHT))
    )
    score = (
        base_clip
        .crop(x1=1550, y1=0, x2=1920, y2=25)
        .resize(width=FACECAM_PANEL_WIDTH)
    )
    if raw_facecam_crop == default:
        safe_screen_name = "".join(
            ch if ch.isalnum() or ch in {"-", "_"} else "_"
            for ch in str(clip_label or f"clip_{clip_index}")
        ).strip("_") or f"clip_{clip_index}"
        base_clip.save_frame(os.path.join(screens_dir, f"{safe_screen_name}.png"), 2)
    game_clip = base_clip.resize(height=1920 - FACECAM_PANEL_HEIGHT - score.size[1])

    cam_height = FACECAM_PANEL_HEIGHT
    result = CompositeVideoClip(
        [
            cam.set_position((0, 0)),
            game_clip.set_position("bottom"),
            score.set_position((0, cam_height)),
        ],
        size=(FACECAM_PANEL_WIDTH, 1920),
    )

    def update_progress(info):
        if progress_callback and "index" in info:
            progress_callback(
                "RENDERING",
                {
                    "index": clip_index,
                    "clip": clip_label,
                    "frame": info["index"],
                    "total": info["total"],
                    "message": f"Rendering frame {info['index']} / {info['total']} for {clip_label}",
                },
            )

    moviepy_logger = FrontendLogger(update_callback=update_progress)
    os.makedirs(clip_dir, exist_ok=True)
    temp_dir = get_temp_dir()
    os.makedirs(temp_dir, exist_ok=True)

    output_path = os.path.join(clip_dir, f"clip_{clip_index}.mp4")
    task_marker = str(temp_audio_token or ("cb" if progress_callback else "local"))
    task_marker = "".join(ch if ch.isalnum() or ch in {"-", "_"} else "_" for ch in task_marker)
    temp_audio_name = f"clip_{clip_index}_{task_marker}_{os.getpid()}_temp_audio.m4a"
    temp_audio_path = os.path.join(temp_dir, temp_audio_name)
    try:
        result.write_videofile(
            output_path,
            fps=60,
            codec="libx264",
            audio_codec="aac",
            temp_audiofile=temp_audio_path,
            bitrate=os.getenv("LEAGUECLIPS_OUTPUT_BITRATE", "20M"),
            ffmpeg_params=["-crf", os.getenv("LEAGUECLIPS_OUTPUT_CRF", "16"), "-pix_fmt", "yuv420p"],
            logger=moviepy_logger,
        )
    finally:
        for clip_obj in (result, cam, score, game_clip, base_clip):
            try:
                clip_obj.close()
            except Exception:
                pass

    # Persist source clip metadata so downstream AI tasks can use streamer/timestamp context.
    metadata_path = os.path.join(clip_dir, "clip_context.json")
    clip_start_timestamp = start_timestamp or seconds_to_timestamp(start_seconds)
    clip_end_timestamp = end_timestamp or seconds_to_timestamp(end_seconds)
    try:
        with open(metadata_path, "w", encoding="utf-8") as handle:
            json.dump(
                {
                    "clip_index": clip_index,
                    "label": clip_label,
                    "start_timestamp": clip_start_timestamp,
                    "end_timestamp": clip_end_timestamp,
                    "source_video": os.path.basename(source_video_path),
                    "facecam_crop": [int(x1d), int(y1d), int(x2d), int(y2d)],
                    "facecam_raw_crop": [int(v) for v in raw_facecam_crop],
                    "facecam_output_size": [FACECAM_PANEL_WIDTH, FACECAM_PANEL_HEIGHT],
                    "facecam_detection_status": detection_meta.get("status"),
                    "facecam_detection_confidence": round(float(detection_meta.get("confidence", 0.0)), 4),
                    "facecam_detection_threshold": threshold,
                    "facecam_mode": facecam_used,
                    "facecam_fallback_crop": [int(v) for v in fallback_crop],
                },
                handle,
                ensure_ascii=False,
                indent=2,
            )
    except OSError as exc:
        logger.warning("Could not persist clip metadata for AI context: %s", exc)

    return output_path


def createClip(item, task=None, source_video_path=""):
    source_video_path = source_video_path or get_source_video_path()
    clip_index = item[-2]
    clip_dir = os.path.join(get_video_storage_dir(), str(clip_index))
    progress_callback = None
    if task:
        progress_callback = lambda state, meta: task.update_state(state=state, meta=meta)

    render_highlight_clip(
        source_video_path=source_video_path,
        clip_dir=clip_dir,
        clip_index=clip_index,
        clip_label=item[1],
        start_seconds=timestamp_to_seconds(item[0]),
        end_seconds=timestamp_to_seconds(item[-1]),
        progress_callback=progress_callback,
        start_timestamp=item[0],
        end_timestamp=item[-1],
        temp_audio_token=getattr(getattr(task, "request", None), "id", "") if task else "",
    )


def process_videos():
    video_list = get_list()
    with multiprocessing.Pool(processes=processes) as pool:
        pool.map(createClip, video_list)
