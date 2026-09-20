import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from app import state, tasks, workers
from app.scripts import synapse_watcher as watcher
from app.views import _task_status_payload


class MemoryRedis:
    """Test storage; external worker/broker communication is mocked separately."""

    def __init__(self):
        self.values = {}
        self.ttls = {}

    def get(self, key):
        return self.values.get(key)

    def set(self, key, value, nx=False, ex=None):
        if nx and key in self.values:
            return False
        self.values[key] = value
        self.ttls[key] = ex if ex is not None else -1
        return True

    def delete(self, key):
        return int(self.values.pop(key, None) is not None)

    def ttl(self, key):
        return self.ttls.get(key, -2)

    def llen(self, key):
        return len(self.values.get(key, []))

    def eval(self, script, number_of_keys, key, owner, *args):
        assert number_of_keys == 1
        if self.get(key) != owner:
            return 0
        if args:
            self.ttls[key] = args[0]
            return 1
        return self.delete(key)


def ago(seconds):
    return (datetime.now(timezone.utc) - timedelta(seconds=seconds)).isoformat()


@pytest.fixture
def runtime(monkeypatch):
    store = MemoryRedis()
    monkeypatch.setenv("LEAGUECLIPS_AUTO_SYNAPSE_ENABLED", "true")
    monkeypatch.setattr(watcher, "redis_client", store)
    monkeypatch.setattr(tasks, "redis_client", store)
    monkeypatch.setattr(state, "r", store)
    inspector = Mock()
    inspector.registered.return_value = {"worker": [tasks.process_videos_task.name]}
    inspector.active.return_value = {"worker": []}
    inspector.reserved.return_value = {"worker": []}
    inspector.scheduled.return_value = {"worker": []}
    monkeypatch.setattr(watcher.celery.control, "inspect", Mock(return_value=inspector))
    revoke = Mock()
    monkeypatch.setattr(watcher.celery.control, "revoke", revoke)
    enqueue = Mock(return_value=SimpleNamespace(id="replacement"))
    monkeypatch.setattr(watcher.process_videos_task, "apply_async", enqueue)
    result = SimpleNamespace(state="PENDING", info=None)
    monkeypatch.setattr(watcher, "AsyncResult", Mock(return_value=result))
    feed = Mock(return_value=[])
    download = Mock()
    monkeypatch.setattr(watcher, "get_recent_synapse_video_metadata", feed)
    monkeypatch.setattr(watcher, "download_source_from_url", download)
    return SimpleNamespace(store=store, inspector=inspector, revoke=revoke, enqueue=enqueue, result=result, feed=feed, download=download)


def active_status(runtime, **updates):
    status = {
        "state": "processing",
        "active_task_id": "original",
        "active_video_id": "video",
        "active_source_url": "https://example.com/video",
        "active_source_filename": "saved.mp4",
        "active_started_at_utc": ago(600),
        **updates,
    }
    watcher._save_status(status)
    return status


def test_backend_and_worker_share_celery_and_track_start():
    assert tasks.celery is workers.celery
    assert tasks.celery.conf.task_track_started is True
    assert tasks.celery.conf.result_expires > watcher.AUTO_SYNAPSE_TASK_STALE_SECONDS
    assert tasks.celery.conf.worker_prefetch_multiplier == 1


def test_preparation_is_reported_before_reading_video(runtime, monkeypatch):
    update = Mock()
    monkeypatch.setattr(tasks.process_videos_task, "update_state", update)
    monkeypatch.setattr(tasks, "resolve_source_video_path", Mock(return_value=("saved.mp4", "source")))

    def get_list(*args, **kwargs):
        assert update.call_args.kwargs["state"] == "SPLIT_PREP"
        return []

    monkeypatch.setattr(tasks, "get_list", get_list)
    result = tasks.process_videos_task.run(source_filename="saved.mp4")
    assert result["status"] == "done"
    assert runtime.store.get(tasks.MULTICLIP_SPLIT_LOCK_KEY) is None


def test_cancelled_message_never_reads_source(runtime, monkeypatch):
    state.mark_aborted("unknown")
    monkeypatch.setattr(tasks.process_videos_task, "update_state", Mock())
    read_source = Mock()
    monkeypatch.setattr(tasks, "resolve_source_video_path", read_source)
    assert tasks.process_videos_task.run()["status"] == "aborted"
    read_source.assert_not_called()


def test_missing_worker_is_visible_and_does_not_download(runtime):
    runtime.inspector.registered.return_value = None
    status = watcher.run_auto_synapse_check()
    assert status["state"] == "waiting_for_worker"
    assert "celery service" in status["queue_message"]
    runtime.feed.assert_not_called()
    runtime.download.assert_not_called()
    runtime.enqueue.assert_not_called()


def test_unrelated_worker_does_not_count_as_clip_worker(runtime):
    runtime.inspector.registered.return_value = {"other": ["unrelated.task"]}
    assert watcher.run_auto_synapse_check()["state"] == "waiting_for_worker"


def test_pending_job_kept_when_workers_offline(runtime):
    active_status(runtime)
    runtime.inspector.registered.return_value = None
    status = watcher.run_auto_synapse_check(check_feed=False)
    assert status["active_task_id"] == "original"
    assert status["state"] == "waiting_for_worker"
    runtime.enqueue.assert_not_called()
    runtime.revoke.assert_not_called()


@pytest.mark.parametrize("location", ["active", "reserved", "scheduled", "broker", "priority"])
def test_known_queued_jobs_are_not_replaced(runtime, location):
    active_status(runtime)
    if location in {"broker", "priority"}:
        queue = watcher.celery.conf.task_default_queue
        if location == "priority":
            queue += "\x06\x163"
        runtime.store.values[queue] = ["message"]
    else:
        job = {"id": "original"}
        getattr(runtime.inspector, location).return_value = {"worker": [{"request": job} if location == "scheduled" else job]}
    for _ in range(2):
        status = watcher.run_auto_synapse_check(check_feed=False)
        assert status["active_task_id"] == "original"
    runtime.enqueue.assert_not_called()
    runtime.revoke.assert_not_called()


def test_partial_worker_response_never_replaces_job(runtime):
    active_status(runtime, pending_missing_checks=1)
    runtime.inspector.scheduled.return_value = None
    status = watcher.run_auto_synapse_check(check_feed=False)
    assert status["pending_missing_checks"] == 0
    runtime.enqueue.assert_not_called()


def test_lost_message_recovered_once_without_redownloading(runtime):
    active_status(runtime)
    runtime.store.set(tasks.MULTICLIP_SPLIT_LOCK_KEY, "original", ex=28800)
    first = watcher.run_auto_synapse_check(check_feed=False)
    assert first["active_task_id"] == "original"
    runtime.enqueue.assert_not_called()
    second = watcher.run_auto_synapse_check(check_feed=False)
    assert second["active_task_id"] == "replacement"
    assert runtime.store.get("abort:original") == "1"
    assert runtime.store.get(tasks.MULTICLIP_SPLIT_LOCK_KEY) is None
    runtime.revoke.assert_called_once_with("original", terminate=False)
    runtime.enqueue.assert_called_once_with(kwargs={
        "source_url": "https://example.com/video", "source_filename": "saved.mp4", "auto_subtitles": False,
    })
    watcher.run_auto_synapse_check(check_feed=False)
    assert runtime.enqueue.call_count == 1
    runtime.download.assert_not_called()
    assert second["video_failure_counts"] == {}


def test_recovery_does_not_replace_job_that_started_during_inspection(runtime, monkeypatch):
    active_status(runtime, pending_missing_checks=1)
    monkeypatch.setattr(watcher, "AsyncResult", Mock(side_effect=[runtime.result, SimpleNamespace(state="STARTED")]))
    status = watcher.run_auto_synapse_check(check_feed=False)
    assert status["pending_missing_checks"] == 0
    runtime.enqueue.assert_not_called()


def test_recovery_honors_stop_requested_during_worker_inspection(runtime):
    active_status(runtime, pending_missing_checks=1)

    def inspect_scheduled():
        watcher.request_auto_synapse_stop()
        return {"worker": []}

    runtime.inspector.scheduled.side_effect = inspect_scheduled
    status = watcher.run_auto_synapse_check(check_feed=False)
    assert status["stop_requested"] is True
    runtime.enqueue.assert_not_called()
    runtime.revoke.assert_not_called()


def test_running_job_progress_prevents_stale_cancellation(runtime):
    active_status(runtime, active_started_at_utc=ago(200000))
    runtime.result.state = "PROGRESS"
    runtime.result.info = {"frame": 20, "total": 100}
    status = watcher.run_auto_synapse_check(check_feed=False)
    assert status["active_task_id"] == "original"
    assert status["active_progress_at_utc"]
    runtime.revoke.assert_not_called()


def test_stale_job_is_cancelled_before_active_fields_are_cleared(runtime):
    runtime.result.state = "PROGRESS"
    runtime.result.info = {"frame": 20, "total": 100}
    signature = json.dumps([runtime.result.state, runtime.result.info], sort_keys=True)
    active_status(runtime, active_started_at_utc=ago(200000), active_progress_at_utc=ago(200000), active_progress_signature=signature)
    status = watcher.run_auto_synapse_check(check_feed=False)
    runtime.revoke.assert_called_once_with("original", terminate=True, signal="SIGTERM")
    assert runtime.store.get("abort:original") == "1"
    assert "active_task_id" not in status
    assert status["state"] == "error"


def test_task_monitoring_does_not_wait_for_feed_poll_interval(runtime, monkeypatch):
    active_status(runtime, last_feed_checked_at_utc=watcher._utcnow())
    check = Mock()
    monkeypatch.setattr(watcher, "run_auto_synapse_check", check)
    sleep = Mock(side_effect=StopIteration)
    monkeypatch.setattr(watcher.time, "sleep", sleep)
    with pytest.raises(StopIteration):
        watcher._watcher_loop()
    check.assert_called_once_with(trigger="scheduled", check_feed=False)
    sleep.assert_called_once_with(watcher.AUTO_SYNAPSE_TASK_POLL_SECONDS)
    assert watcher.AUTO_SYNAPSE_TASK_POLL_SECONDS < watcher.auto_synapse_poll_seconds()


def test_success_is_recorded_on_active_job_poll(runtime):
    active_status(runtime)
    runtime.result.state = "SUCCESS"
    runtime.result.info = {"status": "done", "total": 2, "clips": ["one.mp4", "two.mp4"]}
    status = watcher.run_auto_synapse_check(check_feed=False)
    assert "active_task_id" not in status
    assert status["processed_video_ids"] == ["video"]
    assert status["last_task_result"]["clip_count"] == 2


def test_fresh_check_lock_is_not_cleared_for_idle_status(runtime):
    runtime.store.set(watcher.AUTO_SYNAPSE_LOCK_KEY, "other", ex=watcher.AUTO_SYNAPSE_LOCK_TTL_SECONDS)
    assert watcher._clear_stale_check_lock_if_safe() is False
    assert runtime.store.get(watcher.AUTO_SYNAPSE_LOCK_KEY) == "other"


def test_old_owner_cannot_release_or_refresh_new_owners_lock(runtime):
    runtime.store.set(watcher.AUTO_SYNAPSE_LOCK_KEY, "new", ex=10)
    watcher._release_check_lock("old")
    watcher._refresh_check_lock("old")
    assert runtime.store.get(watcher.AUTO_SYNAPSE_LOCK_KEY) == "new"
    assert runtime.store.ttl(watcher.AUTO_SYNAPSE_LOCK_KEY) == 10


def test_status_api_reports_preparation_message():
    payload = _task_status_payload(SimpleNamespace(state="SPLIT_PREP", info={"message": "Reading video"}))
    assert payload == {"state": "SPLIT_PREP", "progress": {"message": "Reading video"}}
