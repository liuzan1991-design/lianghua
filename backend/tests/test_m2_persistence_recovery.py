"""M2.2任务持久化与重启恢复专项测试。"""
from __future__ import annotations

import json
from pathlib import Path

from app.custom import m2_intraday_t as m2


def _write_state(root: Path, run_id: str, status: str, next_event_id: int = 1):
    record = m2.RunRecord(run_id=run_id, status=status, next_event_id=next_event_id)
    folder = root / run_id
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "state.json").write_text(
        json.dumps(m2.asdict(record), ensure_ascii=False), encoding="utf-8",
    )


def test_state_snapshot_is_atomic_and_readable(tmp_path):
    service = m2.M2BacktestService(tmp_path, tmp_path, tmp_path / "state")
    record = m2.RunRecord(run_id="atomic")
    with service._lock:
        service._runs[record.run_id] = record
        service._event_locked(record, "queued", 0, "queued")
        service._save_locked(record)
    path = tmp_path / "state" / "atomic" / "state.json"
    payload = json.loads(path.read_text(encoding="utf-8"))
    assert payload["run_id"] == "atomic"
    assert not path.with_suffix(".json.tmp").exists()


def test_events_are_appended_to_jsonl_with_increasing_ids(tmp_path):
    service = m2.M2BacktestService(tmp_path, tmp_path, tmp_path / "state")
    record = m2.RunRecord(run_id="events")
    with service._lock:
        service._event_locked(record, "queued", 0, "queued")
        service._event_locked(record, "running", 10, "running")
        service._save_locked(record)
    lines = (tmp_path / "state" / "events" / "events.jsonl").read_text(encoding="utf-8").splitlines()
    assert [json.loads(line)["id"] for line in lines] == [1, 2]
    assert record.next_event_id == 3


def test_completed_task_survives_service_restart(tmp_path):
    root = tmp_path / "state"
    _write_state(root, "done", "completed")
    service = m2.M2BacktestService(tmp_path, tmp_path, root)
    snapshot = service.get("done")
    assert snapshot["status"] == "completed"
    assert snapshot["error_code"] is None


def test_running_and_pending_tasks_become_interrupted_on_restart(tmp_path):
    root = tmp_path / "state"
    _write_state(root, "running", "running", 4)
    _write_state(root, "pending", "pending", 8)
    service = m2.M2BacktestService(tmp_path, tmp_path, root)
    for run_id, expected_next_id in (("running", 5), ("pending", 9)):
        snapshot = service.get(run_id)
        assert snapshot["status"] == "interrupted"
        assert snapshot["error_code"] == "INTERRUPTED"
        assert snapshot["events"][-1]["event"] == "interrupted"
        assert snapshot["events"][-1]["id"] == expected_next_id - 1
        persisted = json.loads((root / run_id / "state.json").read_text(encoding="utf-8"))
        assert persisted["status"] == "interrupted"


def test_get_returns_detached_snapshot(tmp_path):
    service = m2.M2BacktestService(tmp_path, tmp_path, tmp_path / "state")
    record = m2.RunRecord(run_id="snapshot")
    with service._lock:
        service._runs[record.run_id] = record
        service._event_locked(record, "queued", 0, "queued")
        service._save_locked(record)
    snapshot = service.get("snapshot")
    snapshot["status"] = "tampered"
    snapshot["events"][0]["message"] = "tampered"
    fresh = service.get("snapshot")
    assert fresh["status"] == "pending"
    assert fresh["events"][0]["message"] == "queued"


def test_corrupt_state_isolated_without_breaking_other_records(tmp_path):
    root = tmp_path / "state"
    _write_state(root, "good", "completed")
    bad = root / "bad"
    bad.mkdir(parents=True)
    (bad / "state.json").write_text("{bad json", encoding="utf-8")
    service = m2.M2BacktestService(tmp_path, tmp_path, root)
    assert service.get("good")["status"] == "completed"
    try:
        service.get("bad")
    except KeyError:
        pass
    else:
        raise AssertionError("corrupt record must not be loaded")
