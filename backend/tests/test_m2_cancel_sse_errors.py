"""M2.3真取消、SSE续传和稳定错误模型专项测试。"""
from __future__ import annotations

import json
from pathlib import Path
from typing import ClassVar

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.custom import m2_intraday_t as m2


class _RunningProcess:
    def __init__(self):
        self.pid = 4567
        self.returncode = None
        self.terminated = False
        self.killed = False

    def poll(self):
        return self.returncode

    def terminate(self):
        self.terminated = True
        self.returncode = -15

    def kill(self):
        self.killed = True
        self.returncode = -9

    def wait(self, timeout=None):
        return self.returncode


def _service(tmp_path: Path):
    return m2.M2BacktestService(tmp_path, tmp_path, tmp_path / "state")


def _client(service, monkeypatch):
    monkeypatch.setattr(m2, "SERVICE", service)
    app = FastAPI()
    app.include_router(m2.router)
    return TestClient(app)


def test_cancel_running_process_waits_for_exit_and_is_idempotent(tmp_path):
    service = _service(tmp_path)
    record = m2.RunRecord(run_id="running", status="running", worker_pid=4567)
    process = _RunningProcess()
    service._runs[record.run_id] = record
    service._processes[record.run_id] = process
    first = service.cancel(record.run_id)
    second = service.cancel(record.run_id)
    assert process.terminated is True
    assert first["status"] == "cancelled"
    assert second["status"] == "cancelled"
    assert [event["event"] for event in second["events"]].count("cancelled") == 1


def test_cancel_pending_does_not_start_worker(tmp_path):
    service = _service(tmp_path)
    record = m2.RunRecord(run_id="pending", status="pending")
    service._runs[record.run_id] = record
    snapshot = service.cancel(record.run_id)
    assert snapshot["status"] == "cancelled"
    assert record.run_id not in service._processes


def test_sse_after_id_and_last_event_id_resume(tmp_path, monkeypatch):
    service = _service(tmp_path)
    record = m2.RunRecord(run_id="events", status="completed")
    with service._lock:
        service._runs[record.run_id] = record
        service._event_locked(record, "queued", 0, "queued")
        service._event_locked(record, "running", 10, "running")
        service._event_locked(record, "completed", 100, "completed")
        service._save_locked(record)
    client = _client(service, monkeypatch)
    by_query = client.get("/api/custom/m2/backtests/events/events?after_id=1")
    assert "id: 1\n" not in by_query.text
    assert "id: 2\n" in by_query.text and "id: 3\n" in by_query.text
    by_header = client.get("/api/custom/m2/backtests/events/events", headers={"Last-Event-ID": "2"})
    assert "id: 2\n" not in by_header.text
    assert "id: 3\n" in by_header.text
    assert by_header.headers["cache-control"] == "no-cache"


@pytest.mark.asyncio
async def test_sse_disconnect_stops_generator(tmp_path, monkeypatch):
    service = _service(tmp_path)
    record = m2.RunRecord(run_id="live", status="running")
    service._runs[record.run_id] = record
    monkeypatch.setattr(m2, "SERVICE", service)

    class Request:
        headers: ClassVar = {}
        calls = 0

        async def is_disconnected(self):
            self.calls += 1
            return self.calls > 1

    response = await m2.backtest_events(Request(), "live", 0)
    chunks = []
    async for chunk in response.body_iterator:
        chunks.append(chunk)
    assert chunks == []


@pytest.mark.asyncio
async def test_sse_heartbeat_for_live_connection(tmp_path, monkeypatch):
    service = _service(tmp_path)
    record = m2.RunRecord(run_id="heartbeat", status="running")
    service._runs[record.run_id] = record
    monkeypatch.setattr(m2, "SERVICE", service)
    ticks = iter([0.0, 20.0, 20.0])
    monkeypatch.setattr(m2.time, "monotonic", lambda: next(ticks, 20.0))
    class Request:
        headers: ClassVar = {}
        calls = 0

        async def is_disconnected(self):
            self.calls += 1
            return self.calls > 2

    response = await m2.backtest_events(Request(), "heartbeat", 0)
    chunks = []
    async for chunk in response.body_iterator:
        chunks.append(chunk)
        break
    assert chunks[0] == ": heartbeat\n\n"


def test_api_error_codes_do_not_leak_paths(tmp_path, monkeypatch):
    service = _service(tmp_path)
    client = _client(service, monkeypatch)
    missing = client.get("/api/custom/m2/backtests/not-found")
    assert missing.status_code == 404
    assert missing.json()["detail"]["code"] == "RUN_NOT_FOUND"
    assert str(tmp_path) not in missing.text

    response = client.post("/api/custom/m2/backtests", json={
        "strategy_id": "missing", "symbol": "000001.SZ",
    })
    assert response.status_code == 422
    assert response.json()["detail"]["code"] == "STRATEGY_NOT_FOUND"
    assert str(tmp_path) not in response.text


def test_worker_failure_message_is_stable_and_sanitized(tmp_path):
    service = _service(tmp_path)
    record = m2.RunRecord(run_id="failed")
    service._runs[record.run_id] = record
    service._finish_error(record, "WORKER_FAILED", "backtest worker failed", "failed")
    snapshot = service.get(record.run_id)
    assert snapshot["error_code"] == "WORKER_FAILED"
    assert snapshot["message"] == "backtest worker failed"
    assert str(tmp_path) not in json.dumps(snapshot)
