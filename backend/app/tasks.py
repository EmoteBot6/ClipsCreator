from celery import Celery
from celery.result import AsyncResult
from app.scripts.video_editor import createClip, get_list
import os
import logging
import json
import time
from app.state import is_aborted
from app.state import r as redis_client
from app.scripts.video_import import resolve_source_video_path
from app.scripts.ai_pipeline import run_ai_pipeline_for_clip
from app.scripts.single_clip_pipeline import run_centered_mobile_pipeline, run_single_clip_pipeline
from app.scripts.sitcom_pipeline import run_sitcom_pipeline




celery = Celery('tasks')

celery.conf.update(
    broker_url='redis://redis:6379/0',
    result_backend='redis://redis:6379/0'
)

logging.basicConfig(
    level=getattr(logging, os.getenv("LEAGUECLIPS_LOG_LEVEL", "INFO").upper(), logging.INFO),
    format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
)
logger = logging.getLogger(__name__)

SITCOM_EDIT_LOCK_KEY = "lock:sitcom_edit_pipeline"
SITCOM_EDIT_LOCK_TTL_SECONDS = int(os.getenv("LEAGUECLIPS_SITCOM_LOCK_TTL_SECONDS", "28800"))
SITCOM_LOCK_POLL_SECONDS = float(os.getenv("LEAGUECLIPS_SITCOM_LOCK_POLL_SECONDS", "2.0"))
MULTICLIP_SPLIT_LOCK_KEY = "lock:multiclip_split_pipeline"
MULTICLIP_SPLIT_LOCK_TTL_SECONDS = int(os.getenv("LEAGUECLIPS_SPLIT_LOCK_TTL_SECONDS", "28800"))
MULTICLIP_LOCK_POLL_SECONDS = float(os.getenv("LEAGUECLIPS_SPLIT_LOCK_POLL_SECONDS", "2.0"))


def _acquire_multiclip_lock(task):
    task_id = getattr(getattr(task, "request", None), "id", "") or "unknown"
    while True:
        if is_aborted(task_id):
            task.update_state(
                state="REVOKED",
                meta={"message": "Task was stopped before it acquired the split lock."},
            )
            return None
        try:
            acquired = redis_client.set(
                MULTICLIP_SPLIT_LOCK_KEY,
                task_id,
                nx=True,
                ex=MULTICLIP_SPLIT_LOCK_TTL_SECONDS,
            )
        except Exception:
            # If Redis locking fails, proceed so we do not block all splitting.
            return False
        if acquired:
            return True

        owner = None
        try:
            owner = redis_client.get(MULTICLIP_SPLIT_LOCK_KEY)
        except Exception:
            owner = None
        owner = owner or "another task"
        owner_state = ""
        if isinstance(owner, str) and owner not in {"", task_id, "another task"}:
            try:
                owner_state = str(AsyncResult(owner, app=celery).state or "")
            except Exception:
                owner_state = ""
            if owner_state in {"SUCCESS", "FAILURE", "REVOKED"}:
                try:
                    if redis_client.get(MULTICLIP_SPLIT_LOCK_KEY) == owner:
                        redis_client.delete(MULTICLIP_SPLIT_LOCK_KEY)
                        continue
                except Exception:
                    pass
        task.update_state(
            state="SPLIT_QUEUED",
            meta={
                "message": f"Queued: waiting for {owner} to finish.",
                "queued_for": "multiclip_split_lock",
                "lock_owner_state": owner_state,
            },
        )
        time.sleep(max(0.5, MULTICLIP_LOCK_POLL_SECONDS))


def _refresh_multiclip_lock(task_id):
    try:
        owner = redis_client.get(MULTICLIP_SPLIT_LOCK_KEY)
        if owner == task_id:
            redis_client.expire(MULTICLIP_SPLIT_LOCK_KEY, MULTICLIP_SPLIT_LOCK_TTL_SECONDS)
    except Exception:
        return


def _release_multiclip_lock(task_id):
    try:
        owner = redis_client.get(MULTICLIP_SPLIT_LOCK_KEY)
        if owner == task_id:
            redis_client.delete(MULTICLIP_SPLIT_LOCK_KEY)
    except Exception:
        return


def _acquire_sitcom_lock(task):
    task_id = getattr(getattr(task, "request", None), "id", "") or "unknown"
    while True:
        try:
            acquired = redis_client.set(
                SITCOM_EDIT_LOCK_KEY,
                task_id,
                nx=True,
                ex=SITCOM_EDIT_LOCK_TTL_SECONDS,
            )
        except Exception:
            # If Redis locking fails, proceed so we do not block all edits.
            return False
        if acquired:
            return True

        owner = None
        try:
            owner = redis_client.get(SITCOM_EDIT_LOCK_KEY)
        except Exception:
            owner = None
        owner = owner or "another task"
        task.update_state(
            state="SITCOM_QUEUED",
            meta={
                "message": f"Queued: waiting for {owner} to finish.",
                "queued_for": "sitcom_edit_lock",
            },
        )
        time.sleep(max(0.5, SITCOM_LOCK_POLL_SECONDS))


def _refresh_sitcom_lock(task_id):
    try:
        owner = redis_client.get(SITCOM_EDIT_LOCK_KEY)
        if owner == task_id:
            redis_client.expire(SITCOM_EDIT_LOCK_KEY, SITCOM_EDIT_LOCK_TTL_SECONDS)
    except Exception:
        return


def _release_sitcom_lock(task_id):
    try:
        owner = redis_client.get(SITCOM_EDIT_LOCK_KEY)
        if owner == task_id:
            redis_client.delete(SITCOM_EDIT_LOCK_KEY)
    except Exception:
        return

@celery.task(bind=True)
def process_videos_task(self, source_url="", source_filename="", auto_subtitles=False):
    task_id = getattr(getattr(self, "request", None), "id", "") or "unknown"
    if isinstance(auto_subtitles, str):
        auto_subtitles = auto_subtitles.strip().lower() in {"1", "true", "yes", "on"}
    else:
        auto_subtitles = bool(auto_subtitles)
    lock_acquired = _acquire_multiclip_lock(self)
    if lock_acquired is None:
        return {
            "status": "aborted",
            "source_filename": source_filename,
            "total": 0,
            "clips": [],
            "auto_subtitles": auto_subtitles,
            "auto_subtitle_succeeded": 0,
            "auto_subtitle_failed": 0,
        }
    source_name = source_filename
    source_video_path = ""
    try:
        source_name, source_video_path = resolve_source_video_path(source_filename)
        video_list = get_list(source_url or "", source_filename=source_name)
        total = len(video_list)
        completed_clips = []
        errors = []
        auto_subtitle_success = 0
        auto_subtitle_failures = 0

        for i, item in enumerate(video_list):
            _refresh_multiclip_lock(task_id)
            if is_aborted(task_id):
                logger.info("Split task stopped by abort marker: task_id=%s", task_id)
                return {
                    'status': 'aborted',
                    'source_filename': source_name,
                    'total': total,
                    'clips': completed_clips,
                    'auto_subtitles': auto_subtitles,
                    'auto_subtitle_succeeded': auto_subtitle_success,
                    'auto_subtitle_failed': auto_subtitle_failures,
                }

            try:
                clip_index = item[-2]
                clip_label = item[1]
                clip_subdir = str(clip_index)
                clip_filename = f"clip_{clip_index}.mp4"
                failure_stage = "split"
                self.update_state(
                    state='PROGRESS',
                    meta={
                        'index': clip_index,
                        'clip': clip_label,
                        'frame': 0,
                        'total': 1,
                        'message': f"Preparing clip {clip_label}",
                        'source_filename': source_name,
                        'auto_subtitles': auto_subtitles,
                    }
                )
                createClip(item, task=self, source_video_path=source_video_path)
                completed_clips.append(clip_filename)

                if auto_subtitles:
                    failure_stage = "auto_subtitles"
                    self.update_state(
                        state="AI_PREP",
                        meta={
                            "index": clip_index,
                            "clip": clip_label,
                            "subdir": clip_subdir,
                            "message": f"Generating subtitles for {clip_label}",
                            "source_filename": source_name,
                            "auto_subtitles": True,
                        },
                    )
                    run_ai_pipeline_for_clip(
                        subdir=clip_subdir,
                        filename=clip_filename,
                        show_items=False,
                        vine_boom=False,
                        overlay_title="",
                        progress_callback=lambda state, meta, clip_index=clip_index, clip_label=clip_label: self.update_state(
                            state=state,
                            meta={
                                "index": clip_index,
                                "clip": clip_label,
                                "source_filename": source_name,
                                "auto_subtitles": True,
                                **(meta or {}),
                            },
                        ),
                    )
                    auto_subtitle_success += 1

            except Exception as e:
                logger.exception("Failed to process clip %s (%s)", i, item)
                if failure_stage == "auto_subtitles":
                    auto_subtitle_failures += 1
                errors.append(
                    {
                        "index": item[-2],
                        "clip": item[1],
                        "stage": failure_stage,
                        "error": str(e),
                    }
                )

        if errors:
            failure_payload = {
                "status": "failed",
                "source_filename": source_name,
                "total": total,
                "succeeded": len(completed_clips),
                "failed": len(errors),
                "clips": completed_clips,
                "auto_subtitles": auto_subtitles,
                "auto_subtitle_succeeded": auto_subtitle_success,
                "auto_subtitle_failed": auto_subtitle_failures,
                "errors": errors,
                "message": "One or more clips failed during splitting or subtitle processing.",
            }
            raise RuntimeError(json.dumps(failure_payload, ensure_ascii=True))

        return {
            'status': 'done',
            'source_filename': source_name,
            'total': total,
            'succeeded': len(completed_clips),
            'failed': 0,
            'clips': completed_clips,
            'auto_subtitles': auto_subtitles,
            'auto_subtitle_succeeded': auto_subtitle_success,
            'auto_subtitle_failed': auto_subtitle_failures,
        }
    finally:
        if lock_acquired:
            _release_multiclip_lock(task_id)


@celery.task(bind=True)
def process_clip_ai_task(self, subdir, filename, show_items=False, vine_boom=False, overlay_title=""):
    try:
        return run_ai_pipeline_for_clip(
            subdir=subdir,
            filename=filename,
            show_items=show_items,
            vine_boom=vine_boom,
            overlay_title=overlay_title,
            progress_callback=lambda state, meta: self.update_state(state=state, meta=meta),
        )
    except Exception as exc:
        raise RuntimeError(json.dumps({"status": "failed", "message": str(exc)}, ensure_ascii=True))


@celery.task(bind=True)
def process_single_clip_task(self, source_filename="", source_url="", overlay_title=""):
    try:
        task_id = getattr(getattr(self, "request", None), "id", "") or "single_clip"
        return run_single_clip_pipeline(
            task_id=task_id,
            source_filename=source_filename,
            source_url=source_url,
            overlay_title=overlay_title,
            progress_callback=lambda state, meta: self.update_state(state=state, meta=meta),
        )
    except Exception as exc:
        raise RuntimeError(json.dumps({"status": "failed", "message": str(exc)}, ensure_ascii=True))


@celery.task(bind=True)
def process_centered_mobile_task(self, source_filename="", source_url="", tier_list_enabled=False):
    try:
        task_id = getattr(getattr(self, "request", None), "id", "") or "centered_mobile"
        return run_centered_mobile_pipeline(
            task_id=task_id,
            source_filename=source_filename,
            source_url=source_url,
            tier_list_enabled=tier_list_enabled,
            progress_callback=lambda state, meta: self.update_state(state=state, meta=meta),
        )
    except Exception as exc:
        raise RuntimeError(json.dumps({"status": "failed", "message": str(exc)}, ensure_ascii=True))


@celery.task(bind=True)
def process_sitcom_edit_task(
    self,
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
):
    task_id = getattr(getattr(self, "request", None), "id", "") or "unknown"
    lock_acquired = _acquire_sitcom_lock(self)

    def _progress_callback(state, meta):
        _refresh_sitcom_lock(task_id)
        self.update_state(state=state, meta=meta)

    try:
        return run_sitcom_pipeline(
            task_id=task_id,
            source_filename=source_filename,
            remove_ranges=remove_ranges,
            ai_remove_query=ai_remove_query,
            ai_auto_edit=ai_auto_edit,
            target_keep_ratio=target_keep_ratio,
            mobile_mode=mobile_mode,
            split_into_clips=split_into_clips,
            clip_max_seconds=clip_max_seconds,
            clip_min_seconds=clip_min_seconds,
            generate_subtitles=generate_subtitles,
            burn_subtitles=burn_subtitles,
            subtitle_language=subtitle_language,
            progress_callback=_progress_callback,
        )
    except Exception as exc:
        raise RuntimeError(json.dumps({"status": "failed", "message": str(exc)}, ensure_ascii=True))
    finally:
        if lock_acquired:
            _release_sitcom_lock(task_id)
