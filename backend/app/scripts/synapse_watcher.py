import json
import logging
import os
import threading
import time
from datetime import datetime

from celery.result import AsyncResult

from app.state import r as redis_client
from app.tasks import celery, process_videos_task
from app.scripts.video_import import (
    download_source_from_url,
    get_recent_synapse_video_metadata,
)


logger = logging.getLogger(__name__)

AUTO_SYNAPSE_STATUS_KEY = "auto_synapse:status"
AUTO_SYNAPSE_LOCK_KEY = "lock:auto_synapse:check"
AUTO_SYNAPSE_LOCK_TTL_SECONDS = int(os.getenv("LEAGUECLIPS_AUTO_SYNAPSE_LOCK_TTL_SECONDS", "1800"))
AUTO_SYNAPSE_PROCESSED_IDS_LIMIT = int(os.getenv("LEAGUECLIPS_AUTO_SYNAPSE_PROCESSED_IDS_LIMIT", "25"))
AUTO_SYNAPSE_FEED_LIMIT = int(os.getenv("LEAGUECLIPS_AUTO_SYNAPSE_FEED_LIMIT", "10"))
AUTO_SYNAPSE_TASK_STALE_SECONDS = int(os.getenv("LEAGUECLIPS_AUTO_SYNAPSE_TASK_STALE_SECONDS", "86400"))
AUTO_SYNAPSE_MAX_VIDEO_FAILURES = int(os.getenv("LEAGUECLIPS_AUTO_SYNAPSE_MAX_VIDEO_FAILURES", "2"))
AUTO_SYNAPSE_STALE_LOCK_SECONDS = int(os.getenv("LEAGUECLIPS_AUTO_SYNAPSE_STALE_LOCK_SECONDS", "900"))

_watcher_lock = threading.Lock()
_watcher_thread = None


class AutoSynapseStopRequested(Exception):
    pass


def _utcnow():
    return datetime.utcnow().isoformat() + "Z"


def _truthy_env(name, default=False):
    value = os.getenv(name)
    if value is None:
        return bool(default)
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


def auto_synapse_enabled():
    return _truthy_env("LEAGUECLIPS_AUTO_SYNAPSE_ENABLED", default=False)


def auto_synapse_poll_seconds():
    try:
        value = float(os.getenv("LEAGUECLIPS_AUTO_SYNAPSE_POLL_SECONDS", "43200"))
    except ValueError:
        value = 43200.0
    return max(60.0, value)


def auto_synapse_auto_subtitles():
    return _truthy_env("LEAGUECLIPS_AUTO_SYNAPSE_AUTO_SUBTITLES", default=False)


def _thread_alive():
    return bool(_watcher_thread and _watcher_thread.is_alive())


def _default_status():
    return {
        "enabled": auto_synapse_enabled(),
        "poll_seconds": auto_synapse_poll_seconds(),
        "auto_subtitles": auto_synapse_auto_subtitles(),
        "state": "disabled" if not auto_synapse_enabled() else "idle",
        "thread_alive": _thread_alive(),
        "processed_video_ids": [],
    }


def _normalize_processed_ids(items):
    processed_ids = []
    seen = set()
    for item in items or []:
        value = str(item or "").strip()
        if not value or value in seen:
            continue
        processed_ids.append(value)
        seen.add(value)
        if len(processed_ids) >= AUTO_SYNAPSE_PROCESSED_IDS_LIMIT:
            break
    return processed_ids


def _normalize_failure_counts(value):
    if not isinstance(value, dict):
        return {}
    normalized = {}
    for key, count in value.items():
        video_id = str(key or "").strip()
        if not video_id:
            continue
        try:
            normalized[video_id] = max(0, int(count))
        except (TypeError, ValueError):
            normalized[video_id] = 0
    return normalized


def _utc_timestamp(value):
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    return parsed.timestamp()


def _seconds_since_utc(value):
    timestamp = _utc_timestamp(value)
    if timestamp is None:
        return None
    return max(0.0, time.time() - timestamp)


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
        "http error 500",
        "http error 502",
        "http error 503",
        "http error 504",
        "503 service unavailable",
        "504 gateway",
    )
    return any(token in message for token in transient_tokens)


def _status_suggests_stale_check_lock(status):
    state = str(status.get("state") or "").strip().lower()
    if state in {"idle", "disabled"}:
        return True

    if state in {"error", "download_failed"}:
        last_progress_age = _seconds_since_utc(status.get("last_download_progress_at_utc"))
        if last_progress_age is None:
            last_progress_age = _seconds_since_utc(status.get("last_checked_at_utc"))
        return last_progress_age is None or last_progress_age >= max(60, AUTO_SYNAPSE_STALE_LOCK_SECONDS)

    if state in {"downloading", "stopping"}:
        last_progress_age = _seconds_since_utc(status.get("last_download_progress_at_utc"))
        return last_progress_age is not None and last_progress_age >= max(300, AUTO_SYNAPSE_STALE_LOCK_SECONDS)

    return False


def _load_status():
    status = _default_status()
    raw = None
    try:
        raw = redis_client.get(AUTO_SYNAPSE_STATUS_KEY)
    except Exception:
        raw = None

    if raw:
        try:
            parsed = json.loads(raw)
        except (TypeError, ValueError, json.JSONDecodeError):
            parsed = None
        if isinstance(parsed, dict):
            status.update(parsed)

    status["enabled"] = auto_synapse_enabled()
    status["poll_seconds"] = auto_synapse_poll_seconds()
    status["auto_subtitles"] = auto_synapse_auto_subtitles()
    status["thread_alive"] = _thread_alive()
    status["processed_video_ids"] = _normalize_processed_ids(status.get("processed_video_ids"))
    status["failed_video_ids"] = _normalize_processed_ids(status.get("failed_video_ids"))
    status["video_failure_counts"] = _normalize_failure_counts(status.get("video_failure_counts"))
    if not status["enabled"] and not status.get("state"):
        status["state"] = "disabled"
    return status


def _save_status(status):
    payload = dict(status or {})
    payload["enabled"] = auto_synapse_enabled()
    payload["poll_seconds"] = auto_synapse_poll_seconds()
    payload["auto_subtitles"] = auto_synapse_auto_subtitles()
    payload["thread_alive"] = _thread_alive()
    payload["processed_video_ids"] = _normalize_processed_ids(payload.get("processed_video_ids"))
    payload["failed_video_ids"] = _normalize_processed_ids(payload.get("failed_video_ids"))
    payload["video_failure_counts"] = _normalize_failure_counts(payload.get("video_failure_counts"))
    try:
        redis_client.set(AUTO_SYNAPSE_STATUS_KEY, json.dumps(payload, ensure_ascii=True))
    except Exception:
        logger.exception("Failed to save auto Synapse watcher status to Redis")
    return payload


def _merge_status(**updates):
    status = _load_status()
    status.update(updates)
    return _save_status(status)


def _video_source_id(item):
    if not isinstance(item, dict):
        return ""
    return str(item.get("video_id") or item.get("url") or "").strip()


def _task_info_dict(info):
    if isinstance(info, dict):
        return info
    if hasattr(info, "args") and info.args:
        first = info.args[0]
        if isinstance(first, dict):
            return first
        if isinstance(first, str):
            try:
                decoded = json.loads(first)
            except (TypeError, ValueError, json.JSONDecodeError):
                return {}
            if isinstance(decoded, dict):
                return decoded
    return {}


def _task_info_message(info):
    decoded = _task_info_dict(info)
    if decoded:
        return str(decoded.get("message") or decoded.get("status") or json.dumps(decoded, ensure_ascii=True))
    return str(info or "").strip()


def _task_result_summary(task_result):
    info = getattr(task_result, "info", None)
    decoded = _task_info_dict(info)
    if decoded:
        summary = {}
        for key in ("status", "source_filename", "total", "succeeded", "failed", "message"):
            if key in decoded:
                summary[key] = decoded.get(key)
        clips = decoded.get("clips")
        if isinstance(clips, list):
            summary["clip_count"] = len(clips)
        errors = decoded.get("errors")
        if isinstance(errors, list):
            summary["error_count"] = len(errors)
        return summary
    message = _task_info_message(info)
    return {"message": message} if message else {}


def _clear_active_fields(status):
    for key in (
        "active_task_id",
        "active_task_state",
        "active_video_id",
        "active_title",
        "active_source_url",
        "active_source_filename",
        "active_started_at_utc",
    ):
        status.pop(key, None)


def _clear_candidate_fields(status):
    for key in (
        "candidate_video_id",
        "candidate_title",
        "candidate_url",
        "candidate_published_utc",
    ):
        status.pop(key, None)


def _remember_processed_video(status, video_id):
    current = _normalize_processed_ids(status.get("processed_video_ids"))
    status["processed_video_ids"] = _normalize_processed_ids([video_id] + current)
    failure_counts = _normalize_failure_counts(status.get("video_failure_counts"))
    failure_counts.pop(video_id, None)
    status["video_failure_counts"] = failure_counts
    failed_ids = [item for item in _normalize_processed_ids(status.get("failed_video_ids")) if item != video_id]
    status["failed_video_ids"] = failed_ids


def _remember_failed_video(status, video_id, reason):
    video_id = str(video_id or "").strip()
    if not video_id:
        return
    failure_counts = _normalize_failure_counts(status.get("video_failure_counts"))
    failure_counts[video_id] = failure_counts.get(video_id, 0) + 1
    status["video_failure_counts"] = failure_counts
    status["last_failed_video_id"] = video_id
    status["last_failed_reason"] = str(reason or "").strip()
    status["last_failed_at_utc"] = _utcnow()

    if failure_counts[video_id] >= max(1, AUTO_SYNAPSE_MAX_VIDEO_FAILURES):
        current = _normalize_processed_ids(status.get("failed_video_ids"))
        status["failed_video_ids"] = _normalize_processed_ids([video_id] + current)


def _active_task_is_stale(status, task_state):
    stale_after = max(300, AUTO_SYNAPSE_TASK_STALE_SECONDS)
    started_at = _utc_timestamp(status.get("active_started_at_utc"))
    if not started_at:
        return False
    age_seconds = time.time() - started_at
    if age_seconds < stale_after:
        return False
    return str(task_state or "").upper() not in {"SUCCESS", "FAILURE", "REVOKED"}


def get_auto_synapse_status():
    return _load_status()


def request_auto_synapse_stop(reason="Task was stopped by request."):
    status = _load_status()
    status["stop_requested"] = True
    status["stop_requested_at_utc"] = _utcnow()
    if status.get("state") in {"downloading", "processing"}:
        status["state"] = "stopping"
    status["last_error"] = str(reason or "Task was stopped by request.")
    return _save_status(status)


def stop_auto_synapse_task(task_id="", reason="Task was stopped by request."):
    status = _load_status()
    active_task_id = str(status.get("active_task_id") or "").strip()
    expected_task_id = str(task_id or "").strip()

    if expected_task_id and active_task_id and expected_task_id != active_task_id:
        return _save_status(status)

    if not active_task_id:
        status["state"] = "idle" if status.get("enabled", auto_synapse_enabled()) else "disabled"
        for key in (
            "stop_requested",
            "stop_requested_at_utc",
            "download_step",
            "download_progress",
            "last_download_progress_at_utc",
        ):
            status.pop(key, None)
        _clear_candidate_fields(status)
        return _save_status(status)

    finished_utc = _utcnow()
    status["state"] = "idle"
    status["last_task_state"] = "REVOKED"
    status["last_task_finished_at_utc"] = finished_utc
    status["last_task_result"] = {"message": str(reason or "Task was stopped.")}
    status["last_error"] = str(reason or "Task was stopped.")
    status["last_task_observed_at_utc"] = finished_utc
    for key in (
        "stop_requested",
        "stop_requested_at_utc",
        "download_step",
        "download_progress",
        "last_download_progress_at_utc",
    ):
        status.pop(key, None)
    _clear_active_fields(status)
    _clear_candidate_fields(status)
    return _save_status(status)


def _release_check_lock(owner):
    try:
        current_owner = redis_client.get(AUTO_SYNAPSE_LOCK_KEY)
    except Exception:
        current_owner = None
    if current_owner == owner:
        try:
            redis_client.delete(AUTO_SYNAPSE_LOCK_KEY)
        except Exception:
            logger.exception("Failed to release auto Synapse watcher lock")


def clear_auto_synapse_check_lock(reason="Manual watcher reset requested."):
    try:
        deleted = redis_client.delete(AUTO_SYNAPSE_LOCK_KEY)
    except Exception:
        logger.exception("Failed to clear auto Synapse watcher lock")
        return False
    if deleted:
        logger.warning("Cleared auto Synapse watcher lock: %s", reason)
    return bool(deleted)


def _clear_stale_check_lock_if_safe():
    status = _load_status()
    if not _status_suggests_stale_check_lock(status):
        return False
    try:
        current_owner = redis_client.get(AUTO_SYNAPSE_LOCK_KEY)
    except Exception:
        return False
    if not current_owner:
        return False
    try:
        redis_client.delete(AUTO_SYNAPSE_LOCK_KEY)
        logger.warning("Cleared stale auto Synapse check lock while watcher state was %s", status.get("state"))
        return True
    except Exception:
        logger.exception("Failed to clear stale auto Synapse check lock")
        return False


def _refresh_check_lock(owner):
    try:
        current_owner = redis_client.get(AUTO_SYNAPSE_LOCK_KEY)
        if current_owner == owner:
            redis_client.expire(AUTO_SYNAPSE_LOCK_KEY, AUTO_SYNAPSE_LOCK_TTL_SECONDS)
    except Exception:
        logger.debug("Failed to refresh auto Synapse watcher lock", exc_info=True)


def run_auto_synapse_check(trigger="poll"):
    now_utc = _utcnow()
    if not auto_synapse_enabled():
        logger.info("Auto Synapse watcher check skipped because it is disabled")
        return _merge_status(
            state="disabled",
            last_check_trigger=trigger,
            last_checked_at_utc=now_utc,
        )

    logger.info("Auto Synapse watcher check requested: trigger=%s", trigger)
    owner = f"{os.getpid()}:{threading.get_ident()}:{int(time.time())}"
    try:
        acquired = redis_client.set(
            AUTO_SYNAPSE_LOCK_KEY,
            owner,
            nx=True,
            ex=AUTO_SYNAPSE_LOCK_TTL_SECONDS,
        )
    except Exception:
        acquired = False

    if not acquired and _clear_stale_check_lock_if_safe():
        try:
            acquired = redis_client.set(
                AUTO_SYNAPSE_LOCK_KEY,
                owner,
                nx=True,
                ex=AUTO_SYNAPSE_LOCK_TTL_SECONDS,
            )
        except Exception:
            acquired = False

    if not acquired:
        logger.info("Auto Synapse watcher check skipped because lock is busy")
        return _merge_status(
            last_check_trigger=trigger,
            last_check_skipped="lock_busy",
            last_check_requested_at_utc=now_utc,
        )

    try:
        status = _load_status()
        _refresh_check_lock(owner)
        status.update(
            {
                "enabled": True,
                "state": status.get("state") or "idle",
                "last_check_trigger": trigger,
                "last_checked_at_utc": now_utc,
                "last_check_skipped": "",
            }
        )
        if status.get("stop_requested"):
            logger.info("Auto Synapse check acknowledged stop request before work started")
            return stop_auto_synapse_task(reason="Auto Synapse stop requested.")

        active_task_id = str(status.get("active_task_id") or "").strip()
        if active_task_id:
            task_result = AsyncResult(active_task_id, app=celery)
            task_state = str(task_result.state or "PENDING")
            status["active_task_state"] = task_state
            logger.info(
                "Auto Synapse active task observed: task_id=%s state=%s",
                active_task_id,
                task_state,
            )
            if _active_task_is_stale(status, task_state):
                reason = (
                    f"Active task {active_task_id} stayed in {task_state} for more than "
                    f"{AUTO_SYNAPSE_TASK_STALE_SECONDS} seconds."
                )
                active_video_id = str(status.get("active_video_id") or "").strip()
                _remember_failed_video(status, active_video_id, reason)
                status["state"] = "error"
                status["last_task_state"] = task_state
                status["last_error"] = reason
                status["last_task_observed_at_utc"] = _utcnow()
                logger.error("Auto Synapse task marked stale: %s", reason)
                _clear_active_fields(status)
                _clear_candidate_fields(status)
                return _save_status(status)
            if task_state in {"SUCCESS", "FAILURE", "REVOKED"}:
                finished_utc = _utcnow()
                status["last_task_state"] = task_state
                status["last_task_finished_at_utc"] = finished_utc
                status["last_task_result"] = _task_result_summary(task_result)
                if task_state == "SUCCESS":
                    active_video_id = str(status.get("active_video_id") or "").strip()
                    if active_video_id:
                        _remember_processed_video(status, active_video_id)
                        status["last_processed_video_id"] = active_video_id
                    status["last_processed_title"] = status.get("active_title") or ""
                    status["last_processed_source_filename"] = status.get("active_source_filename") or ""
                    status["last_processed_at_utc"] = finished_utc
                    status["last_error"] = ""
                    _clear_active_fields(status)
                    logger.info(
                        "Auto Synapse task completed successfully: task_id=%s video_id=%s",
                        active_task_id,
                        status.get("last_processed_video_id"),
                    )
                else:
                    status["state"] = "idle"
                    status["last_error"] = _task_info_message(task_result.info) or f"Task ended with state {task_state}."
                    if task_state == "FAILURE":
                        _remember_failed_video(
                            status,
                            status.get("active_video_id"),
                            status["last_error"],
                        )
                    logger.error(
                        "Auto Synapse task ended unsuccessfully: task_id=%s state=%s error=%s",
                        active_task_id,
                        task_state,
                        status["last_error"],
                    )
                    _clear_active_fields(status)
                    _clear_candidate_fields(status)
                    return _save_status(status)
            else:
                status["state"] = "processing"
                status["last_task_observed_at_utc"] = _utcnow()
                _refresh_check_lock(owner)
                return _save_status(status)

        _clear_candidate_fields(status)
        logger.info("Auto Synapse fetching recent feed items: limit=%s", AUTO_SYNAPSE_FEED_LIMIT)
        _refresh_check_lock(owner)
        feed_items = get_recent_synapse_video_metadata(limit=AUTO_SYNAPSE_FEED_LIMIT)
        logger.info("Auto Synapse feed returned %s items", len(feed_items or []))
        if feed_items:
            latest = feed_items[0]
            status["latest_video_id"] = latest.get("video_id") or ""
            status["latest_title"] = latest.get("title") or ""
            status["latest_url"] = latest.get("url") or ""
            status["latest_published_utc"] = latest.get("published_utc")
        status["last_feed_checked_at_utc"] = _utcnow()

        processed_ids = set(_normalize_processed_ids(status.get("processed_video_ids")))
        failed_ids = set(_normalize_processed_ids(status.get("failed_video_ids")))
        unseen_items = []
        for item in feed_items:
            item_id = _video_source_id(item)
            if not item_id or item_id in processed_ids or item_id in failed_ids:
                continue
            unseen_items.append(item)

        if not unseen_items:
            status["state"] = "idle"
            status["last_error"] = ""
            logger.info("Auto Synapse found no unseen feed items")
            return _save_status(status)

        candidate = unseen_items[0]
        candidate_id = _video_source_id(candidate)
        logger.info(
            "Auto Synapse selected candidate: video_id=%s title=%r url=%s",
            candidate_id,
            candidate.get("title") or "",
            candidate.get("url") or "",
        )
        status.update(
            {
                "state": "downloading",
                "download_step": "queued",
                "download_progress": {},
                "candidate_video_id": candidate_id,
                "candidate_title": candidate.get("title") or "",
                "candidate_url": candidate.get("url") or "",
                "candidate_published_utc": candidate.get("published_utc"),
            }
        )
        _save_status(status)

        def _download_progress(progress):
            current_status = _load_status()
            if current_status.get("stop_requested"):
                raise AutoSynapseStopRequested("Auto Synapse stop requested during download.")
            progress = progress if isinstance(progress, dict) else {}
            _refresh_check_lock(owner)
            _merge_status(
                state="downloading",
                download_step=progress.get("step") or "downloading",
                download_progress=progress,
                last_download_progress_at_utc=_utcnow(),
            )

        saved_name, source_path, clean_url = download_source_from_url(
            candidate.get("url") or "",
            progress_callback=_download_progress,
        )
        if _load_status().get("stop_requested"):
            raise AutoSynapseStopRequested("Auto Synapse stop requested after download.")
        _refresh_check_lock(owner)
        logger.info(
            "Auto Synapse download completed: video_id=%s filename=%s path=%s",
            candidate_id,
            saved_name,
            source_path,
        )
        status = _load_status()
        task = process_videos_task.apply_async(
            kwargs={
                "source_url": clean_url,
                "source_filename": saved_name,
                "auto_subtitles": auto_synapse_auto_subtitles(),
            }
        )
        status.update(
            {
                "state": "processing",
                "active_task_id": task.id,
                "active_task_state": "PENDING",
                "active_video_id": candidate_id,
                "active_title": candidate.get("title") or "",
                "active_source_url": clean_url,
                "active_source_filename": saved_name,
                "active_started_at_utc": _utcnow(),
                "last_task_state": "PENDING",
                "last_enqueued_at_utc": _utcnow(),
                "download_step": "done",
                "last_error": "",
            }
        )
        logger.info(
            "Auto Synapse enqueued processing task: task_id=%s video_id=%s filename=%s",
            task.id,
            candidate_id,
            saved_name,
        )
        return _save_status(status)
    except AutoSynapseStopRequested as exc:
        logger.info("Auto Synapse stop acknowledged: %s", exc)
        return stop_auto_synapse_task(reason=str(exc))
    except Exception as exc:
        logger.exception("Auto Synapse watcher check failed")
        status = _load_status()
        candidate_id = str(status.get("candidate_video_id") or "").strip()
        if candidate_id:
            _remember_failed_video(status, candidate_id, str(exc))
        download_failed = bool(candidate_id) and _is_transient_download_error(exc)
        status.update(
            {
                "state": "download_failed" if download_failed else "error",
                "last_check_trigger": trigger,
                "last_checked_at_utc": now_utc,
                "last_error": str(exc),
                "last_error_retryable": download_failed,
                "last_error_at_utc": _utcnow(),
            }
        )
        if download_failed:
            status["download_step"] = "failed_retryable"
        return _save_status(status)
    finally:
        _release_check_lock(owner)


def queue_auto_synapse_check(trigger="manual"):
    if not auto_synapse_enabled():
        _merge_status(
            state="disabled",
            last_check_trigger=trigger,
            last_checked_at_utc=_utcnow(),
        )
        return False

    def _runner():
        logger.info("Auto Synapse manual check thread started: trigger=%s", trigger)
        run_auto_synapse_check(trigger=trigger)

    thread = threading.Thread(
        target=_runner,
        name=f"auto-synapse-check-{int(time.time())}",
        daemon=True,
    )
    thread.start()
    return True


def _watcher_loop():
    logger.info("Auto Synapse watcher loop started")
    _merge_status(
        state="idle",
        watcher_started_at_utc=_utcnow(),
        last_error="",
    )

    while True:
        try:
            run_auto_synapse_check(trigger="scheduled")
        except Exception as exc:
            logger.exception("Auto Synapse scheduled check failed")
            _merge_status(
                state="error",
                last_check_trigger="scheduled",
                last_checked_at_utc=_utcnow(),
                last_error=str(exc),
            )
        time.sleep(auto_synapse_poll_seconds())


def start_auto_synapse_watcher():
    global _watcher_thread

    if not auto_synapse_enabled():
        logger.info("Auto Synapse watcher not started because it is disabled")
        _merge_status(state="disabled")
        return False

    werkzeug_state = os.getenv("WERKZEUG_RUN_MAIN")
    if werkzeug_state not in {None, "true"}:
        logger.info("Auto Synapse watcher not started in Werkzeug parent process")
        return False

    with _watcher_lock:
        if _watcher_thread and _watcher_thread.is_alive():
            logger.info("Auto Synapse watcher already running")
            return False
        _watcher_thread = threading.Thread(
            target=_watcher_loop,
            name="auto-synapse-watcher",
            daemon=True,
        )
        _watcher_thread.start()
        logger.info("Auto Synapse watcher thread started")
        return True
