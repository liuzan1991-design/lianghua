"""M2独立做T回测API契约测试。"""
from __future__ import annotations

import json
import time
from pathlib import Path

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.custom import m2_intraday_t as m2


class _InstantProcess:
    next_pid = 9000

    def __init__(self, command, env):
        type(self).next_pid += 1
        self.pid = type(self).next_pid
        self.command = command
        self.env = env
        self.returncode = None

    def poll(self):
        return self.returncode

    def wait(self, timeout=None):
        output = Path(self.command[self.command.index("--output") + 1])
        output.mkdir(parents=True, exist_ok=True)
        (output / "worker_result.json").write_text(json.dumps({
            "status": "completed",
            "summary": {"trade_count": 1},
            "artifacts": ["summary.json"],
        }), encoding="utf-8")
        self.returncode = 0
        return 0

    def terminate(self):
        self.returncode = -15

    def kill(self):
        self.returncode = -9


def _client(tmp_path: Path, monkeypatch) -> TestClient:
    data_root = tmp_path / "data"
    state_root = tmp_path / "state"
    data_root.mkdir()
    (data_root / "stock_000001_1m.csv").write_text(
        "time,open,high,low,close,volume,amount\n"
        "2026-01-05 09:30:00,10,10,10,10,1000,10000\n",
        encoding="utf-8",
    )

    def factory(command, env, stdout, stderr):
        assert env["AMAZINGDATA_ALLOW_SDK"] == "0"
        return _InstantProcess(command, env)

    monkeypatch.setattr(m2.subprocess, "Popen", factory)
    service = m2.M2BacktestService(tmp_path, data_root, state_root)
    monkeypatch.setattr(m2, "SERVICE", service)
    app = FastAPI()
    app.include_router(m2.router)
    return TestClient(app)


def test_health_contract(tmp_path, monkeypatch):
    response = _client(tmp_path, monkeypatch).get("/api/custom/m2/health")
    assert response.status_code == 200
    assert response.json()["mode"] == "backtest-only"
    assert response.json()["process_isolation"] is True
    assert response.json()["amazingdata_sdk_allowed"] is False


def test_strategy_list_contract(tmp_path, monkeypatch):
    response = _client(tmp_path, monkeypatch).get("/api/custom/m2/strategies")
    assert response.status_code == 200
    assert {item["id"] for item in response.json()} == set(m2._STRATEGIES)


def test_create_query_and_events(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    response = client.post("/api/custom/m2/backtests", json={
        "strategy_id": "intraday_t", "symbol": "000001.SZ", "init_cash": 1_000_000,
    })
    assert response.status_code == 202
    run_id = response.json()["run_id"]
    for _ in range(100):
        state = client.get(f"/api/custom/m2/backtests/{run_id}").json()
        if state["status"] in m2._TERMINAL:
            break
        time.sleep(0.01)
    assert state["status"] == "completed"
    assert state["result"]["trade_count"] == 1
    events = client.get(f"/api/custom/m2/backtests/{run_id}/events?after_id=1")
    assert events.status_code == 200
    assert "event: completed" in events.text
    assert "id: 1\n" not in events.text


def test_unknown_strategy_and_run(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    response = client.post("/api/custom/m2/backtests", json={
        "strategy_id": "missing", "symbol": "000001.SZ",
    })
    assert response.status_code == 422
    assert response.json()["detail"]["code"] == "STRATEGY_NOT_FOUND"
    assert client.get("/api/custom/m2/backtests/missing").status_code == 404
    assert client.post("/api/custom/m2/backtests/missing/cancel").status_code == 404


def test_cancel_pending_run_is_idempotent(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    record = m2.RunRecord(run_id="cancel-me")
    m2.SERVICE._runs[record.run_id] = record
    first = client.post("/api/custom/m2/backtests/cancel-me/cancel")
    second = client.post("/api/custom/m2/backtests/cancel-me/cancel")
    assert first.status_code == 200
    assert first.json()["status"] == "cancelled"
    assert second.json()["status"] == "cancelled"


def test_request_limits_and_error_do_not_expose_paths(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    response = client.post("/api/custom/m2/backtests", json={
        "strategy_id": "intraday_t",
        "symbol": "000001.SZ",
        "start": "2020-01-01T00:00:00",
        "end": "2023-01-02T00:00:00",
    })
    assert response.status_code == 422
    assert str(tmp_path) not in response.text
