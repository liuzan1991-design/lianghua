"""M2.1子进程执行骨架专项测试。"""
from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

from app.custom import m2_intraday_t as m2


class _FakeProcess:
    next_pid = 7000

    def __init__(self, mode: str, output_dir: Path, delay: float = 0.0):
        type(self).next_pid += 1
        self.pid = type(self).next_pid
        self.mode = mode
        self.output_dir = output_dir
        self.delay = delay
        self.returncode = None
        self.terminated = False
        self.killed = False

    def poll(self):
        return self.returncode

    def wait(self, timeout=None):
        if self.mode == "timeout" and not self.killed:
            raise m2.subprocess.TimeoutExpired("worker", timeout)
        if self.mode == "blocking" and not (self.terminated or self.killed):
            deadline = time.time() + (timeout or 1)
            while time.time() < deadline and not (self.terminated or self.killed):
                time.sleep(0.005)
        if self.terminated or self.killed:
            self.returncode = -15
            return self.returncode
        if self.delay:
            time.sleep(self.delay)
        self.output_dir.mkdir(parents=True, exist_ok=True)
        if self.mode == "success":
            (self.output_dir / "worker_result.json").write_text(json.dumps({
                "status": "completed", "summary": {"trade_count": 1},
                "artifacts": ["summary.json"],
            }), encoding="utf-8")
            self.returncode = 0
        elif self.mode == "crash":
            self.returncode = 9
        return self.returncode

    def terminate(self):
        self.terminated = True

    def kill(self):
        self.killed = True


def _request():
    return m2.BacktestRequest(strategy_id="intraday_t", symbol="000001.SZ")


def _wait_terminal(service: m2.M2BacktestService, run_id: str, timeout=2):
    deadline = time.time() + timeout
    while time.time() < deadline:
        snapshot = service.get(run_id)
        if snapshot["status"] in m2._TERMINAL:
            return snapshot
        time.sleep(0.01)
    raise AssertionError("run did not reach terminal state")


def _patch_process(monkeypatch, mode: str, holder: list[_FakeProcess]):
    def factory(command, env, stdout, stderr):
        assert str(m2._WORKER) in command
        assert env["AMAZINGDATA_ALLOW_SDK"] == "0"
        output_dir = Path(command[command.index("--output") + 1])
        process = _FakeProcess(mode, output_dir)
        holder.append(process)
        return process

    monkeypatch.setattr(m2.subprocess, "Popen", factory)


def test_success_runs_in_child_process_and_main_process_survives(tmp_path, monkeypatch):
    holder = []
    _patch_process(monkeypatch, "success", holder)
    service = m2.M2BacktestService(tmp_path, tmp_path, tmp_path / "state")
    run = service.create(_request())
    state = _wait_terminal(service, run["run_id"])
    assert state["status"] == "completed"
    assert state["worker_pid"] == holder[0].pid
    assert state["result"]["trade_count"] == 1
    assert os.getpid() != holder[0].pid
    assert service.list_strategies()


def test_worker_crash_isolated_and_sanitized(tmp_path, monkeypatch):
    holder = []
    _patch_process(monkeypatch, "crash", holder)
    service = m2.M2BacktestService(tmp_path, tmp_path, tmp_path / "state")
    state = _wait_terminal(service, service.create(_request())["run_id"])
    assert state["status"] == "failed"
    assert state["error_code"] == "WORKER_FAILED"
    assert str(tmp_path) not in state["message"]
    assert service.list_strategies()


def test_worker_timeout_is_killed(tmp_path, monkeypatch):
    holder = []
    _patch_process(monkeypatch, "timeout", holder)
    service = m2.M2BacktestService(
        tmp_path, tmp_path, tmp_path / "state", timeout_seconds=0.01,
    )
    state = _wait_terminal(service, service.create(_request())["run_id"])
    assert state["status"] == "timeout"
    assert state["error_code"] == "TIMEOUT"
    assert holder[0].killed is True


def test_running_cancel_terminates_child_and_is_idempotent(tmp_path, monkeypatch):
    holder = []
    _patch_process(monkeypatch, "blocking", holder)
    service = m2.M2BacktestService(tmp_path, tmp_path, tmp_path / "state", timeout_seconds=2)
    run = service.create(_request())
    deadline = time.time() + 1
    while time.time() < deadline and not holder:
        time.sleep(0.005)
    first = service.cancel(run["run_id"])
    second = service.cancel(run["run_id"])
    assert first["status"] == "cancelled"
    assert second["status"] == "cancelled"
    assert holder[0].terminated is True


def test_subprocess_command_uses_fixed_worker_entry(tmp_path, monkeypatch):
    captured = {}

    def factory(command, env, stdout, stderr):
        captured["command"] = command
        captured["env"] = env
        output_dir = Path(command[command.index("--output") + 1])
        return _FakeProcess("success", output_dir)

    monkeypatch.setattr(m2.subprocess, "Popen", factory)
    service = m2.M2BacktestService(tmp_path, tmp_path, tmp_path / "state")
    state = _wait_terminal(service, service.create(_request())["run_id"])
    assert state["status"] == "completed"
    assert captured["command"][0] == str(m2._PYTHON)
    assert captured["command"][1] == str(m2._WORKER)
    assert captured["env"]["AMAZINGDATA_ALLOW_SDK"] == "0"
    assert "strategies" not in sys.modules or service.list_strategies()
