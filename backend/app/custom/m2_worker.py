"""M2做T回测独立子进程入口。"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import traceback
from datetime import UTC, datetime
from pathlib import Path


def _atomic_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + ".tmp")
    temp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(temp, path)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _write_error(output_dir: Path, code: str, message: str) -> None:
    _atomic_json(output_dir / "worker_result.json", {
        "status": "failed",
        "error_code": code,
        "message": message,
    })


def run(request_path: Path, output_dir: Path) -> int:
    os.environ["AMAZINGDATA_ALLOW_SDK"] = "0"
    try:
        request = json.loads(request_path.read_text(encoding="utf-8"))
        engine_root = Path(request["engine_root"])
        data_root = Path(request["data_root"])
        code = request["symbol"].split(".")[0]
        data_path = data_root / f"stock_{code}_1m.csv"
        if not data_path.is_file():
            _write_error(output_dir, "DATA_NOT_FOUND", "local minute data not found")
            return 20
        sys.path.insert(0, str(engine_root))
        import pandas as pd
        from strategies import STRATEGIES, get_strategy

        strategy_id = request["strategy_id"]
        if strategy_id not in STRATEGIES:
            _write_error(output_dir, "STRATEGY_NOT_FOUND", "strategy is not registered")
            return 21
        frame = pd.read_csv(data_path, parse_dates=["time"])
        start = request.get("start")
        end = request.get("end")
        if start:
            frame = frame[frame["time"] >= pd.Timestamp(start).tz_localize(None)]
        if end:
            frame = frame[frame["time"] <= pd.Timestamp(end).tz_localize(None)]
        if frame.empty:
            _write_error(output_dir, "NO_DATA_IN_RANGE", "no local data in requested range")
            return 22
        strategy = get_strategy(strategy_id)
        params = strategy.default_params()
        supplied = request.get("params") or {}
        unknown = sorted(set(supplied) - set(params))
        if unknown:
            _write_error(output_dir, "INVALID_REQUEST", "unknown strategy parameter")
            return 23
        metadata = {item.key: item for item in strategy.param_meta}
        for key, value in supplied.items():
            meta = metadata[key]
            if meta.kind in {"number", "slider"} and (isinstance(value, bool) or not isinstance(value, (int, float))):
                _write_error(output_dir, "INVALID_REQUEST", "invalid parameter type")
                return 23
            if meta.kind == "checkbox" and not isinstance(value, bool):
                _write_error(output_dir, "INVALID_REQUEST", "invalid parameter type")
                return 23
            if meta.min is not None and value < meta.min:
                _write_error(output_dir, "INVALID_REQUEST", "parameter below minimum")
                return 23
            if meta.max is not None and value > meta.max:
                _write_error(output_dir, "INVALID_REQUEST", "parameter above maximum")
                return 23
            if meta.options is not None and value not in meta.options:
                _write_error(output_dir, "INVALID_REQUEST", "parameter outside allowed options")
                return 23
        params.update(supplied)
        result = strategy.run(frame, float(request["init_cash"]), params, {"code": code})
        output_dir.mkdir(parents=True, exist_ok=True)
        result.equity.to_csv(output_dir / "equity.csv", index=False, encoding="utf-8")
        result.trades.to_csv(output_dir / "trades.csv", index=False, encoding="utf-8")
        result.rejected.to_csv(output_dir / "rejected.csv", index=False, encoding="utf-8")
        if "time" in result.trades.columns and "t_pnl" in result.trades.columns:
            daily_t = result.trades.assign(
                trade_date=pd.to_datetime(result.trades["time"]).dt.date,
                t_pnl=pd.to_numeric(result.trades["t_pnl"], errors="coerce").fillna(0.0),
            ).groupby("trade_date", as_index=False)["t_pnl"].sum()
        else:
            daily_t = pd.DataFrame(columns=["trade_date", "t_pnl"])
        daily_t.to_csv(output_dir / "daily_t.csv", index=False, encoding="utf-8")
        summary = {
            "strategy_id": result.strategy_id,
            "strategy_name": result.strategy_name,
            "final_equity": float(result.final_equity),
            "total_return": float(result.total_return),
            "annual_return": float(result.annual_return),
            "max_drawdown": float(result.max_drawdown),
            "total_fees": float(result.total_fees),
            "trade_count": len(result.trades),
            "equity_rows": len(result.equity),
            "avg_exposure": float(result.avg_exposure),
            "max_exposure": float(result.max_exposure),
            "capital_return": float(result.capital_return),
            "params": result.params,
            "data_source": "local_csv",
            "data_adjustment": result.data_adjustment,
        }
        _atomic_json(output_dir / "summary.json", summary)
        _atomic_json(output_dir / "manifest.json", {
            "created_at": datetime.now(UTC).isoformat(),
            "run_id": request["run_id"],
            "strategy_id": strategy_id,
            "strategy_class": type(strategy).__name__,
            "symbol": request["symbol"],
            "start": request.get("start"),
            "end": request.get("end"),
            "init_cash": request["init_cash"],
            "params": params,
            "data_adjustment": result.data_adjustment,
            "data_sha256": _sha256(data_path),
            "data_file_name": data_path.name,
            "engine_root_name": engine_root.name,
            "amazingdata_sdk_allowed": False,
        })
        _atomic_json(output_dir / "worker_result.json", {
            "status": "completed",
            "summary": summary,
            "artifacts": ["request.json", "summary.json", "manifest.json", "equity.csv",
                          "trades.csv", "rejected.csv", "daily_t.csv"],
        })
        return 0
    except Exception as exc:  # noqa: BLE001
        output_dir.mkdir(parents=True, exist_ok=True)
        (output_dir / "worker_error.log").write_text(traceback.format_exc(), encoding="utf-8")
        # 必须把异常原文带出去：原来这里只写死一句 "backtest worker failed"，
        # 真实原因（如"初始资金不足：底仓 1000 股 × 527.00 = 527,000 元"）只在
        # traceback 里，用户侧只看到一个没有信息量的 WORKER_FAILED。
        _write_error(output_dir, "WORKER_FAILED", f"{type(exc).__name__}: {exc}")
        return 30


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--request", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    raise SystemExit(run(args.request, args.output))


if __name__ == "__main__":
    main()
