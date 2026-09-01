"""M1 AmazingData Provider 与现有做T本地分钟CSV逐字段对账。

脚本默认只读, 不修改任何行情文件。真实Provider模式要求四个AMAZINGDATA_*环境变量;
未配置时可使用 --provider-csv 对Provider导出样本做离线对账。
"""
from __future__ import annotations

import argparse
import json
from dataclasses import dataclass
from pathlib import Path

import pandas as pd

from app.plugins.amazingdata.provider import AmazingDataProvider, availability

FIELDS = ["open", "high", "low", "close", "volume", "amount"]


@dataclass(frozen=True)
class ReconcileConfig:
    abs_tolerance: float = 1e-8
    rel_tolerance: float = 1e-8


def _standardize_local(path: Path, symbol: str) -> pd.DataFrame:
    frame = pd.read_csv(path, parse_dates=["time"])
    missing = {"time", *FIELDS} - set(frame.columns)
    if missing:
        raise ValueError(f"本地CSV缺少字段: {sorted(missing)}")
    frame = frame.rename(columns={"time": "datetime"})
    frame["symbol"] = symbol
    frame["datetime"] = pd.to_datetime(frame["datetime"]).dt.tz_localize(None)
    return frame[["symbol", "datetime", *FIELDS]].sort_values("datetime")


def _provider_frame(
    symbol: str, start: str, end: str, provider_csv: Path | None
) -> pd.DataFrame:
    if provider_csv is not None:
        frame = pd.read_csv(provider_csv, parse_dates=["datetime"])
    else:
        ok, reason = availability()
        if not ok:
            raise RuntimeError(reason)
        frame = AmazingDataProvider().get_minute([symbol], start, end).to_pandas()
    missing = {"symbol", "datetime", *FIELDS} - set(frame.columns)
    if missing:
        raise ValueError(f"Provider数据缺少字段: {sorted(missing)}")
    frame["datetime"] = pd.to_datetime(frame["datetime"]).dt.tz_localize(None)
    return frame[["symbol", "datetime", *FIELDS]].sort_values("datetime")


def reconcile(local: pd.DataFrame, provider: pd.DataFrame, cfg: ReconcileConfig):
    merged = local.merge(
        provider, on=["symbol", "datetime"], how="outer", suffixes=("_local", "_provider"),
        indicator=True,
    )
    for field in FIELDS:
        left = pd.to_numeric(merged[f"{field}_local"], errors="coerce")
        right = pd.to_numeric(merged[f"{field}_provider"], errors="coerce")
        scale = pd.concat([left.abs(), right.abs()], axis=1).max(axis=1).fillna(0.0)
        tolerance = cfg.abs_tolerance + cfg.rel_tolerance * scale
        merged[f"{field}_abs_diff"] = (left - right).abs()
        merged[f"{field}_match"] = (
            (left.isna() & right.isna()) | ((left - right).abs() <= tolerance)
        )
    both = merged["_merge"] == "both"
    merged["row_match"] = both & merged[[f"{field}_match" for field in FIELDS]].all(axis=1)
    summary = {
        "local_rows": len(local),
        "provider_rows": len(provider),
        "matched_keys": int(both.sum()),
        "only_local": int((merged["_merge"] == "left_only").sum()),
        "only_provider": int((merged["_merge"] == "right_only").sum()),
        "fully_matched_rows": int(merged["row_match"].sum()),
        "timestamp_match_rate": float(both.sum() / max(len(merged), 1)),
        "full_row_match_rate": float(merged["row_match"].sum() / max(int(both.sum()), 1)),
        "duplicate_local": int(local.duplicated(["symbol", "datetime"]).sum()),
        "duplicate_provider": int(provider.duplicated(["symbol", "datetime"]).sum()),
    }
    for field in FIELDS:
        summary[f"{field}_match_rate"] = float(
            merged.loc[both, f"{field}_match"].mean() if both.any() else 0.0
        )
    return merged, summary


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--symbol", required=True)
    parser.add_argument("--local-csv", type=Path, required=True)
    parser.add_argument("--start", required=True)
    parser.add_argument("--end", required=True)
    parser.add_argument("--provider-csv", type=Path)
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args()

    local = _standardize_local(args.local_csv, args.symbol)
    start_ts = pd.Timestamp(args.start)
    end_ts = pd.Timestamp(args.end) + pd.Timedelta(days=1) - pd.Timedelta(microseconds=1)
    local = local[(local["datetime"] >= start_ts) & (local["datetime"] <= end_ts)]
    provider = _provider_frame(args.symbol, args.start, args.end, args.provider_csv)
    merged, summary = reconcile(local, provider, ReconcileConfig())

    args.output_dir.mkdir(parents=True, exist_ok=True)
    merged.to_csv(args.output_dir / f"{args.symbol.replace('.', '_')}_minute_reconcile.csv",
                  index=False, encoding="utf-8-sig")
    (args.output_dir / f"{args.symbol.replace('.', '_')}_summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    if summary["timestamp_match_rate"] < 1.0 or summary["full_row_match_rate"] < 1.0:
        raise SystemExit(2)


if __name__ == "__main__":
    main()
