"""汇总五股AmazingData真实数据与本地分钟CSV对账结果。"""
from __future__ import annotations

import json
from pathlib import Path

import pandas as pd
from reconcile_amazingdata_m1 import ReconcileConfig, _standardize_local, reconcile

ROOT = Path(r"D:\Documents\ChatGPT\daily work\日内做T策略\quant_app\data")
OUT = Path(r"D:\Codex输出\tickflow-stock-panel-review\original\backend\validation_outputs\amazingdata_m1")
SAMPLES = {
    "601619.SH": ("主板", "2026-08-17", "2026-08-21"),
    "300308.SZ": ("创业板", "2026-08-17", "2026-08-21"),
    "688256.SH": ("科创板", "2026-08-17", "2026-08-21"),
    "002594.SZ": ("高价股", "2026-08-17", "2026-08-21"),
    "600900.SH": ("除权样本", "2026-07-13", "2026-07-24"),
}


def main() -> None:
    rows = []
    for symbol, (category, start, end) in SAMPLES.items():
        code = symbol.split(".")[0]
        folder = OUT / code
        local = _standardize_local(ROOT / f"stock_{code}_1m.csv", symbol)
        start_ts = pd.Timestamp(start)
        end_ts = pd.Timestamp(end) + pd.Timedelta(days=1) - pd.Timedelta(microseconds=1)
        local = local[(local["datetime"] >= start_ts) & (local["datetime"] <= end_ts)]
        provider = pd.read_csv(folder / "minute.csv", parse_dates=["kline_time"])
        provider = provider.rename(columns={"kline_time": "datetime"})
        provider = provider[["symbol", "datetime", "open", "high", "low", "close", "volume", "amount"]]
        detail, summary = reconcile(local, provider, ReconcileConfig(abs_tolerance=1e-6, rel_tolerance=1e-8))
        detail.to_csv(folder / "minute_reconcile.csv", index=False, encoding="utf-8-sig")
        gateway = json.loads((folder / "summary.json").read_text(encoding="utf-8"))
        row = {"symbol": symbol, "category": category, **summary, **{
            "daily_rows": gateway["daily_rows"],
            "adj_factor_rows": gateway["adj_factor_rows"],
        }}
        rows.append(row)
    frame = pd.DataFrame(rows)
    frame.to_csv(OUT / "m1_five_stock_summary.csv", index=False, encoding="utf-8-sig")
    minute_keys_ok = bool((frame["timestamp_match_rate"] == 1.0).all())
    minute_values_ok = bool((frame["full_row_match_rate"] >= 0.999).all())
    daily_ok = bool((frame["daily_rows"] > 0).all())
    adj_window_ok = bool(frame.loc[frame["symbol"] == "600900.SH", "adj_factor_rows"].iloc[0] > 0)
    go = minute_keys_ok and minute_values_ok and daily_ok and adj_window_ok
    report = {
        "go": go,
        "criteria": {
            "minute_timestamp_all_100pct": minute_keys_ok,
            "minute_full_row_all_at_least_99_9pct": minute_values_ok,
            "daily_all_non_empty": daily_ok,
            "ex_right_window_contains_event": adj_window_ok,
        },
        "rows": rows,
    }
    (OUT / "m1_validation.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    lines = [
        "# AmazingData M1 五股真实数据验收报告",
        "",
        f"结论: {'GO' if go else 'NO-GO'}",
        "",
        "## 五股真实对账",
        "",
        "```text",
        frame.to_string(index=False),
        "```",
        "",
        "## 验收条件",
        "",
        *[f"- {key}: {value}" for key, value in report["criteria"].items()],
        "",
        "报告不含用户名、密码、主机、Token或权限列表。",
    ]
    (OUT / "M1验收报告.md").write_text("\n".join(lines), encoding="utf-8")
    print(frame.to_string(index=False))
    print("M1_GO=" + str(go))
    if not go:
        raise SystemExit(2)


if __name__ == "__main__":
    main()
