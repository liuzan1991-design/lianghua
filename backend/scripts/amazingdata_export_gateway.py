"""在AmazingData原生Anaconda环境中导出脱敏标准数据。"""
from __future__ import annotations

import argparse
import contextlib
import io
import json
import os
from datetime import datetime
from pathlib import Path

import AmazingData
import pandas as pd


def _login() -> None:
    sink = io.StringIO()
    with contextlib.redirect_stdout(sink), contextlib.redirect_stderr(sink):
        saved_out, saved_err = os.dup(1), os.dup(2)
        null_fd = os.open(os.devnull, os.O_WRONLY)
        try:
            os.dup2(null_fd, 1)
            os.dup2(null_fd, 2)
            AmazingData.login(
                username=os.environ["AMAZINGDATA_USERNAME"],
                password=os.environ["AMAZINGDATA_PASSWORD"],
                host=os.environ["AMAZINGDATA_HOST"],
                port=int(os.environ["AMAZINGDATA_PORT"]),
            )
        finally:
            os.dup2(saved_out, 1)
            os.dup2(saved_err, 2)
            os.close(saved_out)
            os.close(saved_err)
            os.close(null_fd)
    if "login fail" in sink.getvalue().lower():
        raise RuntimeError("AmazingData login failed")


def _call_with_retry(callback, attempts: int = 3):
    last_error = None
    for _ in range(attempts):
        try:
            return callback()
        except (TypeError, RuntimeError) as exc:
            last_error = exc
            _login()
    raise RuntimeError("AmazingData query failed after retry") from last_error


def _kline(symbol: str, start: int, end: int, period: str) -> pd.DataFrame:
    def query():
        base = AmazingData.BaseData()
        calendar = base.get_calendar()
        if calendar is None:
            raise RuntimeError("AmazingData calendar unavailable")
        market = AmazingData.MarketData(calendar)
        value = getattr(AmazingData.constant.Period, period).value
        return market.query_kline([symbol], begin_date=start, end_date=end, period=value)

    result = _call_with_retry(query)
    frame = result.get(symbol) if result else None
    if frame is None:
        return pd.DataFrame()
    output = frame.reset_index(drop=True).copy()
    output.insert(0, "symbol", symbol)
    return output


def _adj_events(symbol: str, start: str, end: str) -> pd.DataFrame:
    frame = _call_with_retry(
        lambda: AmazingData.BaseData().get_backward_factor([symbol], is_local=False)
    )
    if frame is None or frame.empty:
        return pd.DataFrame(columns=["symbol", "trade_date", "ex_factor"])
    series = frame[symbol].dropna().sort_index()
    ratio = series.div(series.shift(1))
    ratio = ratio[(ratio - 1.0).abs() > 1e-12]
    output = ratio.rename("ex_factor").reset_index()
    output.columns = ["trade_date", "ex_factor"]
    output["trade_date"] = pd.to_datetime(output["trade_date"]).dt.date
    output = output[(output["trade_date"] >= datetime.fromisoformat(start).date()) &
                    (output["trade_date"] <= datetime.fromisoformat(end).date())]
    output.insert(0, "symbol", symbol)
    return output


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--symbol", required=True)
    parser.add_argument("--start", required=True)
    parser.add_argument("--end", required=True)
    parser.add_argument("--output", required=True)
    args = parser.parse_args()
    output = Path(args.output)
    output.mkdir(parents=True, exist_ok=True)
    _login()
    start_int = int(args.start.replace("-", ""))
    end_int = int(args.end.replace("-", ""))
    daily = _kline(args.symbol, start_int, end_int, "day")
    minute = _kline(args.symbol, start_int, end_int, "min1")
    factors = _adj_events(args.symbol, args.start, args.end)
    daily.to_csv(output / "daily.csv", index=False, encoding="utf-8")
    minute.to_csv(output / "minute.csv", index=False, encoding="utf-8")
    factors.to_csv(output / "adj_factor.csv", index=False, encoding="utf-8")
    summary = {
        "symbol": args.symbol,
        "start": args.start,
        "end": args.end,
        "daily_rows": len(daily),
        "minute_rows": len(minute),
        "adj_factor_rows": len(factors),
    }
    (output / "summary.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")
    print(json.dumps(summary, ensure_ascii=True))


if __name__ == "__main__":
    main()
