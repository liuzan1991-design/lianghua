"""M2安全Artifacts与任务清理测试。"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.custom import m2_intraday_t as m2


def _service(tmp_path, retention_days=30, max_total_bytes=1024 * 1024):
    return m2.M2BacktestService(tmp_path, tmp_path, tmp_path / "state",
                                retention_days=retention_days, max_total_bytes=max_total_bytes)


def _record(service, run_id, status="completed", artifacts=None, age_days=0):
    finished = (datetime.now(UTC) - timedelta(days=age_days)).isoformat()
    record = m2.RunRecord(run_id=run_id, status=status, finished_at=finished,
                          artifacts=artifacts or [])
    service._runs[run_id] = record
    folder = service.state_root / run_id
    folder.mkdir(parents=True, exist_ok=True)
    service._atomic_json(folder / "state.json", m2.asdict(record))
    return record


def test_artifact_list_and_download_only_allow_manifest_names(tmp_path, monkeypatch):
    service = _service(tmp_path)
    _record(service, "run", artifacts=["summary.json"])
    (service.state_root / "run" / "summary.json").write_text('{"ok":true}', encoding="utf-8")
    monkeypatch.setattr(m2, "SERVICE", service)
    app = FastAPI()
    app.include_router(m2.router)
    client = TestClient(app)
    listing = client.get("/api/custom/m2/backtests/run/artifacts")
    assert listing.json()["artifacts"] == ["summary.json"]
    download = client.get("/api/custom/m2/backtests/run/artifacts/summary.json")
    assert download.status_code == 200
    assert download.headers["content-type"].startswith("application/json")
    assert download.json()["ok"] is True
    forbidden = client.get("/api/custom/m2/backtests/run/artifacts/not-listed.csv")
    assert forbidden.status_code == 403
    assert str(tmp_path) not in forbidden.text


def test_artifact_path_rejects_traversal_and_large_file(tmp_path):
    service = _service(tmp_path)
    _record(service, "run", artifacts=["large.csv", "../secret.txt"])
    (service.state_root / "run" / "large.csv").write_bytes(b"x" * 11)
    with pytest.raises(ValueError, match="ARTIFACT_NOT_ALLOWED"):
        service.artifact_path("run", "../secret.txt")
    with pytest.raises(ValueError, match="ARTIFACT_TOO_LARGE"):
        service.artifact_path("run", "large.csv", max_bytes=10)


def test_cleanup_removes_only_expired_terminal_tasks(tmp_path):
    service = _service(tmp_path, retention_days=30)
    _record(service, "old", age_days=31)
    _record(service, "new", age_days=1)
    _record(service, "running", status="running", age_days=100)
    removed = service.cleanup()
    assert removed == ["old"]
    assert not (service.state_root / "old").exists()
    assert service.get("new")["status"] == "completed"
    assert service.get("running")["status"] == "running"


def test_cleanup_enforces_capacity_oldest_first(tmp_path):
    service = _service(tmp_path, retention_days=365, max_total_bytes=20)
    _record(service, "older", age_days=2)
    _record(service, "newer", age_days=1)
    (service.state_root / "older" / "blob.bin").write_bytes(b"x" * 20)
    (service.state_root / "newer" / "blob.bin").write_bytes(b"x" * 20)
    removed = service.cleanup()
    assert "older" in removed
    assert service.state_root.exists()


def test_health_uses_service_instance_paths(tmp_path, monkeypatch):
    engine = tmp_path / "engine"
    data = tmp_path / "data"
    engine.mkdir()
    data.mkdir()
    service = m2.M2BacktestService(engine, data, tmp_path / "state")
    monkeypatch.setattr(m2, "SERVICE", service)
    result = m2.health()
    assert result["engine_available"] is True
    assert result["data_available"] is True
