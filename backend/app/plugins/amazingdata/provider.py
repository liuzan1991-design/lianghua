"""星耀数智 AmazingData Provider。"""
from __future__ import annotations

import contextlib
import importlib
import io
import logging
import os
import threading
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any
from zoneinfo import ZoneInfo

import polars as pl

from .runtime_guard import validate_single_process_runtime

logger = logging.getLogger(__name__)

_DATASETS = ("daily", "adj_factor", "minute")
_REQUIRED_ENV = (
    "AMAZINGDATA_USERNAME",
    "AMAZINGDATA_PASSWORD",
    "AMAZINGDATA_HOST",
    "AMAZINGDATA_PORT",
)
_DAILY_SCHEMA = {
    "symbol": pl.String,
    "date": pl.Date,
    "open": pl.Float64,
    "high": pl.Float64,
    "low": pl.Float64,
    "close": pl.Float64,
    "volume": pl.Float64,
    "amount": pl.Float64,
}
_MINUTE_SCHEMA = {
    "symbol": pl.String,
    "datetime": pl.Datetime("us"),
    "open": pl.Float64,
    "high": pl.Float64,
    "low": pl.Float64,
    "close": pl.Float64,
    "volume": pl.Float64,
    "amount": pl.Float64,
}
_ADJ_FACTOR_SCHEMA = {
    "symbol": pl.String,
    "trade_date": pl.Date,
    "ex_factor": pl.Float64,
}
_DAILY_COLUMNS = list(_DAILY_SCHEMA)
_MINUTE_COLUMNS = list(_MINUTE_SCHEMA)
_ADJ_FACTOR_COLUMNS = list(_ADJ_FACTOR_SCHEMA)
_SHANGHAI = ZoneInfo("Asia/Shanghai")
_MAX_SYMBOLS_PER_CHUNK = 20
_MAX_MINUTE_DAYS_PER_CHUNK = 31

_CONNECTION_LOCK = threading.RLock()
_AD_MODULE: Any | None = None


@dataclass
class _AmazingDataConfig:
    name: str = "amazingdata"
    display_name: str = "星耀数智 AmazingData"
    datasets: dict = field(default_factory=lambda: dict.fromkeys(_DATASETS))
    path: None = None
    builtin: bool = True


def availability() -> tuple[bool, str]:
    missing = [name for name in _REQUIRED_ENV if not os.getenv(name)]
    if missing:
        return False, f"缺少环境变量: {', '.join(missing)}"
    try:
        importlib.import_module("AmazingData")
    except Exception as exc:
        return False, f"AmazingData SDK 不可用: {exc}"
    return True, "ok"


def _credentials() -> dict[str, str | int]:
    missing = [name for name in _REQUIRED_ENV if not os.getenv(name)]
    if missing:
        raise RuntimeError(f"星耀数智凭证未配置: {', '.join(missing)}")
    try:
        port = int(os.environ["AMAZINGDATA_PORT"])
    except ValueError as exc:
        raise RuntimeError("AMAZINGDATA_PORT 必须是整数") from exc
    return {
        "username": os.environ["AMAZINGDATA_USERNAME"],
        "password": os.environ["AMAZINGDATA_PASSWORD"],
        "host": os.environ["AMAZINGDATA_HOST"],
        "port": port,
    }


@contextlib.contextmanager
def _suppress_sdk_output():
    """抑制星耀 SDK 在 Python 层和底层 fd 1/2 写出的登录信息。"""
    sink = io.StringIO()
    with contextlib.redirect_stdout(sink), contextlib.redirect_stderr(sink):
        saved_out = os.dup(1)
        saved_err = os.dup(2)
        null_fd = os.open(os.devnull, os.O_WRONLY)
        try:
            os.dup2(null_fd, 1)
            os.dup2(null_fd, 2)
            yield sink
        finally:
            os.dup2(saved_out, 1)
            os.dup2(saved_err, 2)
            os.close(saved_out)
            os.close(saved_err)
            os.close(null_fd)


def _connection():
    global _AD_MODULE
    validate_single_process_runtime()
    with _CONNECTION_LOCK:
        if _AD_MODULE is not None:
            return _AD_MODULE
        ad = importlib.import_module("AmazingData")
        try:
            with _suppress_sdk_output() as sink:
                ad.login(**_credentials())
        except SystemExit as exc:
            raise RuntimeError("星耀数智登录失败") from exc
        if "login fail" in sink.getvalue().lower():
            raise RuntimeError("星耀数智登录失败")
        _AD_MODULE = ad
        return ad


def _reset_connection() -> None:
    global _AD_MODULE
    with _CONNECTION_LOCK:
        _AD_MODULE = None


def _empty(schema: dict[str, pl.DataType]) -> pl.DataFrame:
    return pl.DataFrame(schema=schema)


def _normalize_symbol(symbol: str) -> str:
    text = str(symbol).strip().upper()
    if "." in text:
        return text
    if text.startswith(("60", "68", "51", "56", "58", "50")):
        return f"{text}.SH"
    if text.startswith(("00", "30", "12", "15", "16", "18")):
        return f"{text}.SZ"
    if text.startswith(("43", "83", "87", "88", "92")):
        return f"{text}.BJ"
    raise ValueError(f"无法仅凭代码识别交易所: {symbol}")


def _as_date(value: str | date | datetime | None, *, default: date) -> date:
    if value is None:
        return default
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    return date.fromisoformat(str(value)[:10])


def _date_bounds(start_time, end_time) -> tuple[date, date]:
    today = date.today()
    start = _as_date(start_time, default=today)
    end = _as_date(end_time, default=start)
    if start > end:
        raise ValueError("start_time 不能晚于 end_time")
    return start, end


def _to_vendor_date(value: date) -> int:
    return int(value.strftime("%Y%m%d"))


def _chunks(items: list[str], size: int):
    for offset in range(0, len(items), size):
        yield items[offset:offset + size]


def _date_chunks(start: date, end: date, max_days: int):
    cursor = start
    while cursor <= end:
        chunk_end = min(end, date.fromordinal(cursor.toordinal() + max_days - 1))
        yield cursor, chunk_end
        cursor = date.fromordinal(chunk_end.toordinal() + 1)


def _query(symbols, start: date, end: date, period: str) -> dict:
    normalized = [_normalize_symbol(symbol) for symbol in symbols]
    last_error: Exception | None = None
    for attempt in range(2):
        try:
            ad = _connection()
            calendar = ad.BaseData().get_calendar()
            market = ad.MarketData(calendar)
            period_value = getattr(ad.constant.Period, period).value
            result = market.query_kline(
                normalized,
                begin_date=_to_vendor_date(start),
                end_date=_to_vendor_date(end),
                period=period_value,
            )
            if result is None:
                raise RuntimeError("星耀接口返回空响应")
            return result
        except Exception as exc:
            last_error = exc
            _reset_connection()
            if attempt == 0:
                continue
    raise RuntimeError("星耀行情查询失败") from last_error


def _to_shanghai_wallclock(value) -> datetime:
    ts = value if isinstance(value, datetime) else datetime.fromisoformat(str(value))
    if ts.tzinfo is not None:
        ts = ts.astimezone(_SHANGHAI).replace(tzinfo=None)
    return ts


def _records(result: dict, *, minute: bool) -> list[dict]:
    output: list[dict] = []
    time_names = ("kline_time", "time", "datetime")
    for symbol, frame in result.items():
        if frame is None:
            continue
        if hasattr(frame, "reset_index"):
            frame = frame.reset_index(drop=True)
        rows = frame.to_dict("records") if hasattr(frame, "to_dict") else list(frame)
        for row in rows:
            time_value = next((row.get(name) for name in time_names if row.get(name) is not None), None)
            if time_value is None:
                raise ValueError(f"{symbol} 行情缺少时间字段")
            ts = _to_shanghai_wallclock(time_value)
            item = {
                "symbol": _normalize_symbol(symbol),
                "open": float(row["open"]),
                "high": float(row["high"]),
                "low": float(row["low"]),
                "close": float(row["close"]),
                "volume": float(row["volume"]),
                "amount": float(row.get("amount") or 0.0),
            }
            item["datetime" if minute else "date"] = ts if minute else ts.date()
            output.append(item)
    return output


def _frame(rows: list[dict], schema: dict[str, pl.DataType]) -> pl.DataFrame:
    return pl.DataFrame(rows, schema=schema) if rows else _empty(schema)


class AmazingDataProvider:
    name = "amazingdata"
    builtin = True
    minute_history_days = 0

    def __init__(self):
        self.config = _AmazingDataConfig()

    def close(self) -> None:
        _reset_connection()

    def get_daily(self, symbols, start_time, end_time, asset_type="stock", on_chunk_done=None):
        if asset_type not in {"stock", "etf", "index"}:
            raise ValueError(f"不支持的资产类型: {asset_type}")
        normalized = [_normalize_symbol(symbol) for symbol in symbols]
        if not normalized:
            return _empty(_DAILY_SCHEMA)
        start, end = _date_bounds(start_time, end_time)
        batches = list(_chunks(normalized, _MAX_SYMBOLS_PER_CHUNK))
        rows: list[dict] = []
        for index, batch in enumerate(batches, 1):
            rows.extend(_records(_query(batch, start, end, "day"), minute=False))
            if on_chunk_done:
                on_chunk_done(index, len(batches))
        return _frame(rows, _DAILY_SCHEMA)

    def get_adj_factors(
        self, symbols, start_time, end_time, asset_type="stock", on_chunk_done=None
    ):
        if asset_type != "stock":
            raise ValueError("星耀Provider当前仅验证A股股票复权因子")
        normalized = [_normalize_symbol(symbol) for symbol in symbols]
        if not normalized:
            return _empty(_ADJ_FACTOR_SCHEMA)
        start, end = _date_bounds(start_time, end_time)
        batches = list(_chunks(normalized, _MAX_SYMBOLS_PER_CHUNK))
        rows: list[dict] = []
        for index, batch in enumerate(batches, 1):
            # Tick Stock Panel 的 ex_factor 是单次除权事件比值, 不是每日累计因子。
            # AmazingData.get_backward_factor 返回随公司行为单调累积的后复权因子;
            # 相邻交易日累计因子的比值即该日事件因子, 只保留发生变化的日期。
            frame = _connection().BaseData().get_backward_factor(batch, is_local=False)
            if frame is not None and not frame.empty:
                result = frame.copy().sort_index()
                ratios = result.div(result.shift(1))
                ratios = ratios.where((ratios - 1.0).abs() > 1e-12)
                ratios.index = ratios.index.date
                ratios.index.name = "trade_date"
                result = ratios.reset_index().melt(
                    id_vars=["trade_date"], var_name="symbol", value_name="ex_factor"
                ).dropna(subset=["ex_factor"])
                result = result[(result["trade_date"] >= start) & (result["trade_date"] <= end)]
                rows.extend(result[_ADJ_FACTOR_COLUMNS].to_dict("records"))
            if on_chunk_done:
                on_chunk_done(index, len(batches))
        return _frame(rows, _ADJ_FACTOR_SCHEMA)

    def get_minute(
        self, symbols, start_time, end_time, asset_type="stock", on_chunk_done=None, freq="1m"
    ):
        if freq != "1m":
            raise ValueError("星耀Provider当前只支持1m分钟K")
        if asset_type not in {"stock", "etf", "index"}:
            raise ValueError(f"不支持的资产类型: {asset_type}")
        normalized = [_normalize_symbol(symbol) for symbol in symbols]
        if not normalized:
            return _empty(_MINUTE_SCHEMA)
        start, end = _date_bounds(start_time, end_time)
        symbol_batches = list(_chunks(normalized, _MAX_SYMBOLS_PER_CHUNK))
        date_batches = list(_date_chunks(start, end, _MAX_MINUTE_DAYS_PER_CHUNK))
        total = len(symbol_batches) * len(date_batches)
        rows: list[dict] = []
        current = 0
        for symbol_batch in symbol_batches:
            for date_start, date_end in date_batches:
                rows.extend(_records(_query(symbol_batch, date_start, date_end, "min1"), minute=True))
                current += 1
                if on_chunk_done:
                    on_chunk_done(current, total)
        return _frame(rows, _MINUTE_SCHEMA)

    def test_dataset(self, dataset: str, symbols=None) -> dict:
        symbols = symbols or ["000001.SZ"]
        if dataset not in self.config.datasets:
            return {"provider": self.name, "dataset": dataset, "rows": 0,
                    "columns": [], "preview": [], "error": "未声明该数据集"}
        # 设置页只验证接口可调用性。使用最近7个自然日覆盖周末/节假日,
        # 空结果也表示调用成功, 不能把非交易日误报为Provider故障。
        end = date.today()
        start = date.fromordinal(end.toordinal() - 7)
        try:
            if dataset == "daily":
                frame = self.get_daily(symbols, start, end)
            elif dataset == "adj_factor":
                frame = self.get_adj_factors(symbols, start, end)
            else:
                frame = self.get_minute(symbols, start, end)
            return {"provider": self.name, "dataset": dataset, "rows": frame.height,
                    "columns": frame.columns, "preview": frame.head(3).to_dicts(),
                    "ok": True, "empty": frame.is_empty()}
        except Exception as exc:
            logger.warning("AmazingData test_dataset failed: %s", type(exc).__name__)
            return {"provider": self.name, "dataset": dataset, "rows": 0,
                    "columns": [], "preview": [], "error": str(exc)}
