"""M2四策略真实子进程固定样本验证。"""
from __future__ import annotations

import json
import os
import time
from pathlib import Path

from app.custom.m2_intraday_t import _TERMINAL, BacktestRequest, M2BacktestService

ENGINE = Path(r"D:\Documents\ChatGPT\daily work\日内做T策略\quant_app")
DATA = ENGINE / "data"
OUT = Path(r"D:\Codex输出\tickflow-stock-panel-review\original\backend\validation_outputs\m2")
PYTHON = Path(r"C:\Users\27329\.workbuddy\binaries\python\envs\default\Scripts\python.exe")


def main() -> None:
    os.environ["M2_WORKER_PYTHON"] = str(PYTHON)
    service = M2BacktestService(ENGINE, DATA, OUT / "runs", timeout_seconds=240,
                                worker_python=PYTHON)
    rows = []
    for strategy_id in ("intraday_t", "grid_trade", "sentiment_t", "ma_swing"):
        run = service.create(BacktestRequest(
            strategy_id=strategy_id,
            symbol="601619.SH",
            start="2026-08-17T00:00:00",
            end="2026-08-21T23:59:59",
        ))
        deadline = time.time() + 260
        while time.time() < deadline:
            snapshot = service.get(run["run_id"])
            if snapshot["status"] in _TERMINAL:
                break
            time.sleep(0.2)
        rows.append({
            "strategy_id": strategy_id,
            "run_id": run["run_id"],
            "status": snapshot["status"],
            "error_code": snapshot["error_code"],
            "result": snapshot["result"],
            "artifacts": snapshot["artifacts"],
        })
    OUT.mkdir(parents=True, exist_ok=True)
    report = {"all_completed": all(row["status"] == "completed" for row in rows), "rows": rows}
    (OUT / "m2_real_validation.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8",
    )
    print(json.dumps(report, ensure_ascii=False, indent=2))
    if not report["all_completed"]:
        raise SystemExit(2)


if __name__ == "__main__":
    main()
