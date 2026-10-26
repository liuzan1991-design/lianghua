# -*- coding: utf-8 -*-
"""Live 状态扩展（live-status）：quant_app 实盘引擎的**只读**仪表盘。

定位
----
对应 H57 方案（Live接面板与H57_S4_S5_实施方案.md）施工顺序第 1 步：
纯新增文件，零改 quant_app，零写接口 —— 面板红线是**绝不提供 live_config 的写入口**，
所有数据直读 quant_app 的 live_outputs/ 与 data/live/ 产物文件。

数据源（全部 quant_app 生产产物，只读）
--------------------------------------
- ``live_outputs/heartbeat.json``       心跳 14 字段（含巡检必读 5 字段）
- ``live_outputs/health.json``          采集水位 + 观察单元 orders/fills
- ``live_outputs/ALERT_ACTIVE.json``    集中度告警标志（存在即告警）
- ``live_outputs/<sym>/<strategy>/daily_summary.json``   当日摘要（权益/盈亏/一致性）
- ``live_outputs/<sym>/<strategy>/paper_state.json``      持仓
- ``data/live/config_applied.json``     本次启动实际生效的组合与参数来源（H57-S3 留痕）
- ``data/live/config_audit.jsonl``      S5 热更新审计（尚未存在，端点做缺省处理）

注意事项（都是从现场踩出来的）
------------------------------
① ``config_applied.json`` 在 ``data/live/``（LIVE_DIR），**不在** live_outputs/ ——
   方案文档初稿写错过这个路径，以 live_match_loop.py:43 为准。
② heartbeat.json 是跨进程持久化的，进程死了文件还在 ⇒ 必须算新鲜度
   （采集循环 ~36s 一刷，>180s 视为陈旧；与 live_trade.py /live 同口径）。
③ 单元目录名规则：live_outputs 下第一层是 `<symbol>`（如 300308.SZ），
   第二层是 `<strategy>`；判定依据 = 目录里有 daily_summary.json。
   其他文件（logs/_bak_*/_preKill_*）一律跳过。
④ 本模块不 import quant_app 任何 Python 代码 —— JSON 直读，与后端 venv 无 pandas 的
   约束无关；quant_app 路径复用 live_trade.INTRADAY_T_ENGINE_ROOT，不另存一份配置。
"""
from __future__ import annotations

import json
import os
from datetime import datetime
from pathlib import Path
from typing import Any

from fastapi import APIRouter

from app.extensions import BACKEND_EXTENSION_API_VERSION, BackendExtensionRegistrar
from app.custom.live_trade import _ENGINE_ROOT as ENGINE_ROOT

EXTENSION_ID = "tsp.live-status"
EXTENSION_API_VERSION = BACKEND_EXTENSION_API_VERSION

router = APIRouter(prefix="/api/custom/live-status", tags=["live-status"])

_LIVE_OUT = ENGINE_ROOT / "live_outputs"
_LIVE_DIR = ENGINE_ROOT / "data" / "live"
_HEARTBEAT_STALE_SECONDS = 180.0  # 采集循环 ~36s 一刷，3 个周期内算新鲜


def _read_json(path: Path) -> dict[str, Any] | None:
    try:
        if not path.is_file():
            return None
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001 — 产物文件可能正被引擎写入，读到半截就当缺失
        return None


def _heartbeat_fresh(raw: dict[str, Any] | None) -> tuple[float | None, bool]:
    """返回 (age_seconds, fresh)。时间戳解析失败返回 (None, False)。"""
    if not raw:
        return None, False
    ts = raw.get("updated_at")
    if not ts:
        return None, False
    try:
        dt = datetime.fromisoformat(str(ts))
        now = datetime.now(dt.tzinfo) if dt.tzinfo else datetime.now()
        age = (now - dt).total_seconds()
    except ValueError:
        return None, False
    return round(age), 0 <= age <= _HEARTBEAT_STALE_SECONDS


@router.get("/summary")
def summary() -> dict:
    """心跳 + 采集健康 + 生效配置来源 —— 仪表盘首屏一次拉全。"""
    hb = _read_json(_LIVE_OUT / "heartbeat.json")
    age, fresh = _heartbeat_fresh(hb)
    health = _read_json(_LIVE_OUT / "health.json")
    alert = (_LIVE_OUT / "ALERT_ACTIVE.json").is_file()
    return {
        "engine_root": str(ENGINE_ROOT),
        "heartbeat": hb,
        "heartbeat_age_seconds": age,
        "heartbeat_fresh": fresh,
        "collect": {
            "last_collected": (health or {}).get("last_collected") or {},
            "collect_errors": (health or {}).get("collect_errors"),
        },
        "alert_active": alert,
        "config_applied": _read_json(_LIVE_DIR / "config_applied.json"),
    }


@router.get("/units")
def units() -> dict:
    """观察单元列表：<symbol>/<strategy> 的当日摘要 + 持仓（只读）。"""
    out: list[dict[str, Any]] = []
    if not _LIVE_OUT.is_dir():
        return {"units": out}
    today = datetime.now().strftime("%Y-%m-%d")
    for sym_dir in sorted(p for p in _LIVE_OUT.iterdir() if p.is_dir()):
        for strat_dir in sorted(p for p in sym_dir.iterdir() if p.is_dir()):
            ds = _read_json(strat_dir / "daily_summary.json")
            if ds is None:
                continue  # 无当日摘要的不是观察单元（logs 之类）
            ps = _read_json(strat_dir / "paper_state.json") or {}
            acct = ps.get("account") or {}
            positions = acct.get("positions") or {}
            fills = ps.get("fills") or []
            out.append({
                "symbol": sym_dir.name,
                "strategy": strat_dir.name,
                "summary_date": ds.get("date"),
                "is_today": ds.get("date") == today,
                "equity": ds.get("equity"),
                "cash": ds.get("cash"),
                "day_pnl": ds.get("day_pnl"),
                "cum_pnl": ds.get("cum_pnl"),
                "cumulative_drawdown": ds.get("cumulative_drawdown"),
                "orders": ds.get("orders"),
                "fills": ds.get("fills"),
                "engine_consistency": (ds.get("signal_compare") or {}).get("engine_consistency"),
                "data_quality": ds.get("data_quality") or {},
                "stopped": ds.get("stopped"),
                "defense": ds.get("defense"),
                "positions": [
                    {
                        "symbol": k,
                        "quantity": v.get("quantity"),
                        "sellable_quantity": v.get("sellable_quantity"),
                        "average_cost": v.get("average_cost"),
                        "market_price": v.get("market_price"),
                        "realized_pnl": v.get("realized_pnl"),
                    }
                    for k, v in positions.items()
                ],
                "today_fills": [
                    f for f in fills
                    if str(f.get("filled_at", "")).startswith(today)
                ],
            })
    return {"units": out}


@router.get("/config")
def config_view() -> dict:
    """生效配置留痕 + S5 审计尾部（config_audit.jsonl 尚不存在时给空表）。"""
    applied = _read_json(_LIVE_DIR / "config_applied.json")
    audit: list[dict[str, Any]] = []
    audit_path = _LIVE_DIR / "config_audit.jsonl"
    if audit_path.is_file():
        for line in audit_path.read_text(encoding="utf-8").splitlines()[-20:]:
            try:
                audit.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return {"config_applied": applied, "audit_tail": audit}


def setup(registrar: BackendExtensionRegistrar) -> None:
    registrar.include_router(router)
