"""M2.4输入资源约束与Artifacts专项测试。"""
from __future__ import annotations

import json
import os
import sys
import types
from typing import ClassVar

import pandas as pd
import pytest

from app.custom import m2_intraday_t as m2
from app.custom import m2_worker


class _Meta:
    def __init__(self, key, kind="number", minimum=None, maximum=None, options=None):
        self.key = key
        self.kind = kind
        self.min = minimum
        self.max = maximum
        self.options = options


class _Result:
    strategy_id = "intraday_t"
    strategy_name = "日内做T"
    final_equity = 1_001_000.0
    total_return = 0.001
    annual_return = 0.002
    max_drawdown = -0.001
    total_fees = 10.0
    avg_exposure = 0.4
    max_exposure = 0.8
    capital_return = 0.003
    data_adjustment = "不复权"
    params: ClassVar = {"fixed_shares": 100}
    equity = pd.DataFrame([{"time": "2026-01-05", "equity": 1_001_000}])
    trades = pd.DataFrame([{
        "time": "2026-01-05 10:00:00", "direction": "SELL", "price": 10,
        "shares": 100, "reason": "test", "score": "", "fee": 1, "t_pnl": 20,
    }])
    rejected = pd.DataFrame(columns=["time", "direction", "price", "shares", "reason"])


class _Strategy:
    param_meta: ClassVar = [_Meta("fixed_shares", minimum=0, maximum=1000)]

    @staticmethod
    def default_params():
        return {"fixed_shares": 100}

    def run(self, frame, init_cash, params, context):
        return _Result()


def _install_registry(monkeypatch):
    module = types.ModuleType("strategies")
    module.STRATEGIES = {"intraday_t": _Strategy}
    module.get_strategy = lambda strategy_id: _Strategy()
    monkeypatch.setitem(sys.modules, "strategies", module)


def _request(tmp_path, params=None):
    data_root = tmp_path / "data"
    engine_root = tmp_path / "engine"
    output = tmp_path / "output"
    data_root.mkdir(parents=True)
    engine_root.mkdir(parents=True)
    pd.DataFrame([{
        "time": "2026-01-05 09:30:00", "open": 10, "high": 10, "low": 10,
        "close": 10, "volume": 100, "amount": 1000,
    }]).to_csv(data_root / "stock_000001_1m.csv", index=False)
    payload = {
        "run_id": "run-1", "strategy_id": "intraday_t", "symbol": "000001.SZ",
        "start": None, "end": None, "init_cash": 1_000_000, "params": params or {},
        "engine_root": str(engine_root), "data_root": str(data_root),
    }
    request_path = output / "request.json"
    output.mkdir()
    request_path.write_text(json.dumps(payload), encoding="utf-8")
    return request_path, output


def test_request_limits_date_window_and_params_size():
    with pytest.raises(ValueError, match="2 years"):
        m2.BacktestRequest(strategy_id="intraday_t", symbol="000001.SZ",
                           start="2020-01-01", end="2023-01-02")
    with pytest.raises(ValueError, match="32KB"):
        m2.BacktestRequest(strategy_id="intraday_t", symbol="000001.SZ",
                           params={"x": "a" * (33 * 1024)})


def test_queue_and_concurrency_limits(tmp_path):
    service = m2.M2BacktestService(tmp_path, tmp_path, tmp_path / "state",
                                   max_concurrent=1, max_queue=3)
    for index in range(4):
        service._runs[str(index)] = m2.RunRecord(run_id=str(index), status="pending")
    with pytest.raises(RuntimeError, match="QUEUE_FULL"):
        service.create(m2.BacktestRequest(strategy_id="intraday_t", symbol="000001.SZ"))


def test_worker_rejects_unknown_and_out_of_range_parameters(tmp_path, monkeypatch):
    _install_registry(monkeypatch)
    request_path, output = _request(tmp_path, {"unknown": 1})
    assert m2_worker.run(request_path, output) == 23
    result = json.loads((output / "worker_result.json").read_text(encoding="utf-8"))
    assert result["error_code"] == "INVALID_REQUEST"

    request_path, output = _request(tmp_path / "second", {"fixed_shares": 2000})
    assert m2_worker.run(request_path, output) == 23


def test_worker_outputs_complete_artifacts_and_manifest(tmp_path, monkeypatch):
    _install_registry(monkeypatch)
    request_path, output = _request(tmp_path, {"fixed_shares": 100})
    assert m2_worker.run(request_path, output) == 0
    expected = {"request.json", "summary.json", "manifest.json", "equity.csv",
                "trades.csv", "rejected.csv", "daily_t.csv", "worker_result.json"}
    assert expected <= {path.name for path in output.iterdir()}
    summary = json.loads((output / "summary.json").read_text(encoding="utf-8"))
    assert summary["avg_exposure"] == 0.4
    assert summary["max_exposure"] == 0.8
    assert summary["data_source"] == "local_csv"
    manifest = json.loads((output / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["strategy_class"] == "_Strategy"
    assert manifest["data_sha256"]
    assert manifest["amazingdata_sdk_allowed"] is False
    assert os.environ["AMAZINGDATA_ALLOW_SDK"] == "0"
    daily = pd.read_csv(output / "daily_t.csv")
    assert daily["t_pnl"].iloc[0] == 20


def test_event_retention_is_bounded(tmp_path):
    service = m2.M2BacktestService(tmp_path, tmp_path, tmp_path / "state")
    record = m2.RunRecord(run_id="bounded")
    with service._lock:
        for index in range(1005):
            service._event_locked(record, "progress", index % 100, "progress")
    assert len(record.events) == 1000
    assert record.events[0]["id"] == 6
    assert record.events[-1]["id"] == 1005
