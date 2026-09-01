"""比较原策略直接run与M2 worker输出。"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import numpy as np
import pandas as pd

ENGINE = Path(r"D:\Documents\ChatGPT\daily work\日内做T策略\quant_app")
DATA = ENGINE / "data"
OUT = Path(r"D:\Codex输出\tickflow-stock-panel-review\original\backend\validation_outputs\m2_golden")
WORKER = Path(__file__).parents[1] / "app" / "custom" / "m2_worker.py"
PYTHON = Path(r"C:\Users\27329\.workbuddy\binaries\python\envs\default\Scripts\python.exe")
SYMBOL = "688256.SH"
START = "2023-01-03T00:00:00"
END = "2026-08-21T23:59:59"


def _frames_equal(left: pd.DataFrame, right: pd.DataFrame, *, numeric_atol: float = 1e-8) -> bool:
    if list(left.columns) != list(right.columns) or len(left) != len(right):
        return False
    for column in left.columns:
        lhs, rhs = left[column], right[column]
        if "time" in column or column == "trade_date":
            if not pd.to_datetime(lhs).reset_index(drop=True).equals(pd.to_datetime(rhs).reset_index(drop=True)):
                return False
        elif column in {"direction", "reason", "score"}:
            def normalize(series, field=column):
                values = series.fillna("").astype(str).replace({"nan": "", "None": ""})
                if field == "score":
                    values = values.str.replace(r"\.0$", "", regex=True)
                return values
            if not normalize(lhs).reset_index(drop=True).equals(normalize(rhs).reset_index(drop=True)):
                return False
        elif pd.api.types.is_numeric_dtype(lhs) or pd.api.types.is_numeric_dtype(rhs):
            left_num = pd.to_numeric(lhs, errors="coerce").to_numpy(dtype=float)
            right_num = pd.to_numeric(rhs, errors="coerce").to_numpy(dtype=float)
            if not np.allclose(left_num, right_num, rtol=1e-10, atol=numeric_atol, equal_nan=True):
                return False
        else:
            if not lhs.fillna("").astype(str).reset_index(drop=True).equals(
                rhs.fillna("").astype(str).reset_index(drop=True)
            ):
                return False
    return True


def _normalize_trade(frame: pd.DataFrame) -> pd.DataFrame:
    columns = ["time", "direction", "price", "shares", "reason", "score", "fee", "t_pnl"]
    output = frame.reindex(columns=columns).copy()
    if "time" in output:
        output["time"] = pd.to_datetime(output["time"])
    return output.reset_index(drop=True)


def main() -> None:
    sys.path.insert(0, str(ENGINE))
    from strategies import STRATEGIES, get_strategy

    code = SYMBOL.split(".")[0]
    frame = pd.read_csv(DATA / f"stock_{code}_1m.csv", parse_dates=["time"])
    frame = frame[(frame["time"] >= pd.Timestamp(START)) & (frame["time"] <= pd.Timestamp(END))]
    rows = []
    OUT.mkdir(parents=True, exist_ok=True)
    for strategy_id in STRATEGIES:
        strategy = get_strategy(strategy_id)
        params = strategy.default_params()
        direct = strategy.run(frame.copy(), 1_000_000.0, params, {"code": code})
        run_dir = OUT / strategy_id
        run_dir.mkdir(parents=True, exist_ok=True)
        request = {
            "run_id": strategy_id, "strategy_id": strategy_id, "symbol": SYMBOL,
            "start": START, "end": END, "init_cash": 1_000_000.0, "params": {},
            "engine_root": str(ENGINE), "data_root": str(DATA),
        }
        request_path = run_dir / "request.json"
        request_path.write_text(json.dumps(request, ensure_ascii=False), encoding="utf-8")
        env = os.environ.copy()
        env["AMAZINGDATA_ALLOW_SDK"] = "0"
        completed = subprocess.run(
            [str(PYTHON), str(WORKER), "--request", str(request_path), "--output", str(run_dir)],
            env=env, capture_output=True, text=True, timeout=300, check=False,
        )
        if completed.returncode != 0:
            raise RuntimeError(f"worker failed for {strategy_id}")
        summary = json.loads((run_dir / "summary.json").read_text(encoding="utf-8"))
        worker_equity = pd.read_csv(run_dir / "equity.csv")
        worker_trades = _normalize_trade(pd.read_csv(run_dir / "trades.csv"))
        worker_rejected = pd.read_csv(run_dir / "rejected.csv")
        direct_trades = _normalize_trade(direct.trades)
        direct_equity = direct.equity.copy()
        checks = {
            "final_equity": abs(summary["final_equity"] - float(direct.final_equity)) <= 1e-8,
            "total_return": abs(summary["total_return"] - float(direct.total_return)) <= 1e-12,
            "max_drawdown": abs(summary["max_drawdown"] - float(direct.max_drawdown)) <= 1e-12,
            "total_fees": abs(summary["total_fees"] - float(direct.total_fees)) <= 1e-8,
            "trade_count": len(worker_trades) == len(direct_trades),
            "trades": _frames_equal(worker_trades, direct_trades),
            "rejected": _frames_equal(worker_rejected, direct.rejected),
            "equity": _frames_equal(worker_equity, direct_equity),
        }
        rows.append({"strategy_id": strategy_id, "trade_count": len(direct_trades),
                     "all_match": all(checks.values()), **checks})
    report = {"symbol": SYMBOL, "start": START, "end": END,
              "all_match": all(row["all_match"] for row in rows), "rows": rows}
    (OUT / "m2_golden_regression.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8",
    )
    print(json.dumps(report, ensure_ascii=False, indent=2))
    if not report["all_match"]:
        raise SystemExit(2)


if __name__ == "__main__":
    main()
