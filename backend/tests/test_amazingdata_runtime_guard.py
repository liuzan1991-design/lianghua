"""AmazingData单进程运行约束测试。"""
from __future__ import annotations

import pytest

from app.plugins.amazingdata.runtime_guard import validate_single_process_runtime


def test_single_worker_is_allowed(monkeypatch):
    monkeypatch.setenv("WEB_CONCURRENCY", "1")
    monkeypatch.setenv("AMAZINGDATA_ALLOW_SDK", "1")
    validate_single_process_runtime()


def test_multiple_workers_are_rejected(monkeypatch):
    monkeypatch.setenv("WEB_CONCURRENCY", "2")
    monkeypatch.setenv("AMAZINGDATA_ALLOW_SDK", "1")
    with pytest.raises(RuntimeError, match="单worker"):
        validate_single_process_runtime()


def test_backtest_worker_sdk_access_is_rejected(monkeypatch):
    monkeypatch.setenv("WEB_CONCURRENCY", "1")
    monkeypatch.setenv("AMAZINGDATA_ALLOW_SDK", "0")
    with pytest.raises(RuntimeError, match="回测worker"):
        validate_single_process_runtime()
