"""AmazingData Provider 原型契约测试; 不访问真实网络或凭证。"""
from __future__ import annotations

import os
from datetime import UTC, datetime

import pandas as pd
import pytest

from app.plugins.amazingdata import provider as ap
from app.plugins.amazingdata.provider import AmazingDataProvider


class _PeriodValue:
    def __init__(self, value):
        self.value = value


class _Period:
    day = _PeriodValue("DAY")
    min1 = _PeriodValue("MIN1")


class _Market:
    def __init__(self, calendar):
        self.calendar = calendar

    def query_kline(self, symbols, begin_date, end_date, period):
        assert begin_date <= end_date
        query_day = datetime.strptime(str(begin_date), "%Y%m%d")
        return {
            symbol: pd.DataFrame([{
                "kline_time": query_day.replace(hour=9, minute=30),
                "open": 10.0, "high": 10.2, "low": 9.9, "close": 10.1,
                "volume": 123400, "amount": 1_234_000,
            }])
            for symbol in symbols
        }


class _Base:
    def get_calendar(self):
        return object()

    def get_backward_factor(self, symbols, is_local=False):
        assert not is_local
        index = pd.to_datetime(["2026-08-26", "2026-08-27", "2026-08-28"])
        return pd.DataFrame({symbol: [1.0, 1.2, 1.2] for symbol in symbols}, index=index)


class _FakeAmazingData:
    constant = type("Constant", (), {"Period": _Period})
    login_calls = 0

    @classmethod
    def login(cls, **kwargs):
        cls.login_calls += 1
        assert kwargs["password"] == "secret"
        print('logon json: {"Token":"must-not-leak"}')
        print("login success")

    BaseData = _Base
    MarketData = _Market


@pytest.fixture(autouse=True)
def reset(monkeypatch):
    for key, value in {
        "AMAZINGDATA_USERNAME": "user",
        "AMAZINGDATA_PASSWORD": "secret",
        "AMAZINGDATA_HOST": "127.0.0.1",
        "AMAZINGDATA_PORT": "1234",
    }.items():
        monkeypatch.setenv(key, value)
    _FakeAmazingData.login_calls = 0
    ap._reset_connection()
    monkeypatch.setattr(ap.importlib, "import_module", lambda name: _FakeAmazingData)
    yield
    ap._reset_connection()


def test_availability_missing_credentials(monkeypatch):
    monkeypatch.delenv("AMAZINGDATA_PASSWORD")
    ok, reason = ap.availability()
    assert not ok
    assert "AMAZINGDATA_PASSWORD" in reason
    assert "secret" not in reason


def test_connection_is_reused_in_process():
    provider = AmazingDataProvider()
    provider.get_daily(["000001.SZ"], "2026-08-28", "2026-08-28")
    provider.get_minute(["000001.SZ"], "2026-08-28", "2026-08-28")
    assert _FakeAmazingData.login_calls == 1


def test_login_output_is_suppressed(capsys):
    AmazingDataProvider().get_daily(["000001.SZ"], "2026-08-28", "2026-08-28")
    captured = capsys.readouterr()
    assert "must-not-leak" not in captured.out
    assert "must-not-leak" not in captured.err


def test_daily_contract_uses_raw_ohlcv():
    frame = AmazingDataProvider().get_daily(["000001.SZ"], "2026-08-28", "2026-08-28")
    assert frame.columns == ap._DAILY_COLUMNS
    row = frame.to_dicts()[0]
    assert row["symbol"] == "000001.SZ"
    assert row["date"].isoformat() == "2026-08-28"
    assert row["volume"] == 123400.0
    assert row["amount"] == 1_234_000.0


def test_adj_factor_contract_and_date_filter():
    frame = AmazingDataProvider().get_adj_factors(
        ["000001.SZ"], "2026-08-27", "2026-08-28"
    )
    assert frame.columns == ap._ADJ_FACTOR_COLUMNS
    assert frame.height == 1
    row = frame.to_dicts()[0]
    assert row["symbol"] == "000001.SZ"
    assert row["trade_date"].isoformat() == "2026-08-27"
    assert row["ex_factor"] == pytest.approx(1.2)


def test_minute_contract_is_beijing_naive_wallclock():
    frame = AmazingDataProvider().get_minute(["600519.SH"], "2026-08-28", "2026-08-28")
    row = frame.to_dicts()[0]
    assert frame.columns == ap._MINUTE_COLUMNS
    assert row["datetime"] == datetime(2026, 8, 28, 9, 30)
    assert row["datetime"].tzinfo is None


def test_unknown_bare_symbol_fails_closed():
    with pytest.raises(ValueError, match="无法仅凭代码识别交易所"):
        ap._normalize_symbol("999999")


def test_only_verified_datasets_are_declared():
    provider = AmazingDataProvider()
    assert set(provider.config.datasets) == {"daily", "adj_factor", "minute"}
    assert "realtime" not in provider.config.datasets


def test_callbacks_report_current_and_total_batches(monkeypatch):
    monkeypatch.setattr(ap, "_MAX_SYMBOLS_PER_CHUNK", 1)
    calls = []
    AmazingDataProvider().get_daily(
        ["000001.SZ", "300308.SZ"],
        "2026-08-28",
        "2026-08-28",
        on_chunk_done=lambda current, total: calls.append((current, total)),
    )
    assert calls == [(1, 2), (2, 2)]


def test_minute_callbacks_include_date_chunks(monkeypatch):
    monkeypatch.setattr(ap, "_MAX_MINUTE_DAYS_PER_CHUNK", 1)
    calls = []
    AmazingDataProvider().get_minute(
        ["000001.SZ"],
        "2026-08-27",
        "2026-08-28",
        on_chunk_done=lambda current, total: calls.append((current, total)),
    )
    assert calls == [(1, 2), (2, 2)]


def test_empty_symbols_do_not_connect_and_have_explicit_schema():
    frame = AmazingDataProvider().get_minute([], None, None)
    assert _FakeAmazingData.login_calls == 0
    assert frame.schema == ap._MINUTE_SCHEMA


def test_none_dates_default_without_error():
    frame = AmazingDataProvider().get_daily([], None, None)
    assert frame.schema == ap._DAILY_SCHEMA


def test_timezone_aware_value_converts_to_shanghai_wallclock():
    value = datetime(2026, 8, 28, 1, 30, tzinfo=UTC)
    assert ap._to_shanghai_wallclock(value) == datetime(2026, 8, 28, 9, 30)


def test_test_dataset_dispatches_adj_factor(monkeypatch):
    provider = AmazingDataProvider()
    called = []
    monkeypatch.setattr(
        provider,
        "get_adj_factors",
        lambda *args, **kwargs: called.append("adj_factor") or ap._empty(ap._ADJ_FACTOR_SCHEMA),
    )
    monkeypatch.setattr(
        provider,
        "get_minute",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("must not call minute")),
    )
    result = provider.test_dataset("adj_factor")
    assert called == ["adj_factor"]
    assert result["columns"] == ap._ADJ_FACTOR_COLUMNS
    assert result["ok"] is True
    assert result["empty"] is True


def test_test_dataset_empty_non_trading_window_is_success(monkeypatch):
    provider = AmazingDataProvider()
    monkeypatch.setattr(
        provider,
        "get_daily",
        lambda *args, **kwargs: ap._empty(ap._DAILY_SCHEMA),
    )
    result = provider.test_dataset("daily")
    assert result["ok"] is True
    assert result["empty"] is True
    assert "error" not in result


def test_test_dataset_reports_error_without_leaking_password(monkeypatch):
    monkeypatch.setattr(ap, "_query", lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("query failed")))
    result = AmazingDataProvider().test_dataset("daily")
    assert result["rows"] == 0
    assert "query failed" in result["error"]
    assert os.environ["AMAZINGDATA_PASSWORD"] not in result["error"]
