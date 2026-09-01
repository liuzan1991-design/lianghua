from __future__ import annotations

import pandas as pd
import pytest

from scripts.reconcile_amazingdata_m1 import ReconcileConfig, reconcile


def frame(close=10.1):
    return pd.DataFrame([{
        "symbol": "300308.SZ",
        "datetime": pd.Timestamp("2026-08-28 09:30:00"),
        "open": 10.0, "high": 10.2, "low": 9.9, "close": close,
        "volume": 1000.0, "amount": 10050.0,
    }])


def test_identical_rows_pass():
    detail, summary = reconcile(frame(), frame(), ReconcileConfig())
    assert summary["timestamp_match_rate"] == 1.0
    assert summary["full_row_match_rate"] == 1.0
    assert bool(detail.iloc[0]["row_match"])


def test_value_difference_is_reported():
    detail, summary = reconcile(frame(), frame(close=10.3), ReconcileConfig())
    assert summary["timestamp_match_rate"] == 1.0
    assert summary["full_row_match_rate"] == 0.0
    assert detail.iloc[0]["close_abs_diff"] == pytest.approx(0.2)


def test_missing_timestamp_is_reported():
    provider = frame()
    provider["datetime"] = pd.Timestamp("2026-08-28 09:31:00")
    _, summary = reconcile(frame(), provider, ReconcileConfig())
    assert summary["timestamp_match_rate"] == 0.0
    assert summary["only_local"] == 1
    assert summary["only_provider"] == 1


def test_duplicate_counts_are_reported():
    local = pd.concat([frame(), frame()], ignore_index=True)
    _, summary = reconcile(local, frame(), ReconcileConfig())
    assert summary["duplicate_local"] == 1
    assert summary["duplicate_provider"] == 0
