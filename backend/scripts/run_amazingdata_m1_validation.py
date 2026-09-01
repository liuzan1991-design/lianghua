"""运行M1五股真实数据验证并生成脱敏报告。"""
from __future__ import annotations

import json
from pathlib import Path

import pandas as pd

from app.plugins.amazingdata.provider import AmazingDataProvider
from scripts.reconcile_amazingdata_m1 import ReconcileConfig, _standardize_local, reconcile

ROOT = Path(r"D:\Documents\ChatGPT\daily work\日内做T策略\quant_app\data")
OUT = Path(r"D:\Codex输出\tickflow-stock-panel-review\original\backend\validation_outputs\amazingdata_m1")
SAMPLES = {
    "601619.SH": "主板样本",
    "300308.SZ": "创业板样本",
    "688256.SH": "科创板/高价样本",
    "002594.SZ": "深市主板样本",
    "600900.SH": "除权与分红样本",
}
DATE = "2026-08-21"


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    provider = AmazingDataProvider()
    rows = []
    try:
        for symbol, category in SAMPLES.items():
            code = symbol.split(".")[0]
            local_path = ROOT / f"stock_{code}_1m.csv"
            local = _standardize_local(local_path, symbol)
            local = local[local["datetime"].dt.date == pd.Timestamp(DATE).date()]
            minute = provider.get_minute([symbol], DATE, DATE).to_pandas()
            detail, summary = reconcile(local, minute, ReconcileConfig())
            detail.to_csv(OUT / f"{code}_minute_reconcile.csv", index=False, encoding="utf-8-sig")
            daily = provider.get_daily([symbol], DATE, DATE)
            factor = provider.get_adj_factors([symbol], DATE, DATE)
            rows.append({
                "symbol": symbol,
                "category": category,
                "minute_local_rows": summary["local_rows"],
                "minute_provider_rows": summary["provider_rows"],
                "timestamp_match_rate": summary["timestamp_match_rate"],
                "full_row_match_rate": summary["full_row_match_rate"],
                "daily_rows": daily.height,
                "adj_factor_rows": factor.height,
                "duplicate_local": summary["duplicate_local"],
                "duplicate_provider": summary["duplicate_provider"],
            })
    finally:
        provider.close()

    summary_frame = pd.DataFrame(rows)
    summary_frame.to_csv(OUT / "m1_five_stock_summary.csv", index=False, encoding="utf-8-sig")
    go = bool(
        len(summary_frame) == 5
        and (summary_frame["timestamp_match_rate"] == 1.0).all()
        and (summary_frame["full_row_match_rate"] == 1.0).all()
        and (summary_frame["daily_rows"] > 0).all()
        and (summary_frame["adj_factor_rows"] > 0).all()
    )
    report = {
        "validation_date": DATE,
        "samples": len(summary_frame),
        "go": go,
        "criteria": {
            "timestamp_match_rate": 1.0,
            "full_row_match_rate": 1.0,
            "daily_rows_positive": True,
            "adj_factor_rows_positive": True,
        },
        "rows": rows,
    }
    (OUT / "m1_validation.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    lines = [
        "# AmazingData M1 五股真实数据验收报告",
        "",
        f"- 验证日期：{DATE}",
        f"- 样本数：{len(summary_frame)}",
        f"- 结论：{'GO' if go else 'NO-GO'}",
        "- 数据来源：AmazingData真实接口 + 现有做T项目本地分钟CSV",
        "- 注意：报告不包含用户名、密码、主机、Token或权限列表。",
        "",
        summary_frame.to_markdown(index=False),
    ]
    (OUT / "M1验收报告.md").write_text("\n".join(lines), encoding="utf-8")
    print(summary_frame.to_string(index=False))
    print("M1_GO=" + str(go))
    if not go:
        raise SystemExit(2)


if __name__ == "__main__":
    main()
