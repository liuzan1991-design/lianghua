# -*- coding: utf-8 -*-
"""Live 状态扩展（live-status）：quant_app 实盘引擎的仪表盘 + 实盘口径写入口（H64）。

定位
----
状态页（心跳/观察单元/生效配置）仍是**只读**；2026-10-28 用户明确要求"能自己随时
调整实盘口径"，故新增 `POST /config` 写入口（H64），但带三道护栏：值域校验、
**可行性校验**（按最新价算底仓，<100 股直接拒绝）、写前备份 + `config_audit.jsonl` 留痕。
⚠️ 原红线（H57 方案"绝不提供写入口"）自此**作废**，改为"可写但必须可追溯、且次日生效"。

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
import re
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


_STRATEGY_LABELS = {
    "grid_trade": "网格交易",
    "ma_swing": "均线波段",
    "intraday_t": "日内做T",
    "sentiment_t": "情绪T",
}


def _supported_strategies() -> list[dict]:
    """可用策略 = quant_app 的 `_SUPPORTED_LIVE_STRATEGIES`（**唯一真相源**）。

    2026-10-28 教训：面板里再抄一份白名单，必然与引擎漂移（H38 的"改一处漏两处"）。
    这里直接读 `live_match_loop.py` 的集合字面量；读不到则退回 grid/ma_swing。
    """
    ids = ["grid_trade", "ma_swing"]
    try:
        import re as _re
        txt = (ENGINE_ROOT / "live_match_loop.py").read_text(encoding="utf-8", errors="replace")
        m = _re.search(r"_SUPPORTED_LIVE_STRATEGIES\s*=\s*\{([^}]*)\}", txt)
        if m:
            found = _re.findall(r'"([a-z_]+)"', m.group(1))
            if found:
                ids = found
    except Exception:  # noqa: BLE001
        pass
    return [{"id": i, "label": _STRATEGY_LABELS.get(i, i)} for i in ids]


@router.get("/strategies")
def strategies_view() -> dict:
    """面板策略下拉的数据源。"""
    return {"items": _supported_strategies()}


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
    return {"config_applied": applied, "audit_tail": audit,
            "editable": _editable_view()}


# ── H64：实盘口径的**写入口**（2026-10-28 用户授权） ────────────────────────
# 原红线是"面板绝不提供 live_config 写入口"（H57 方案，S5 未落地前）。
# 用户 2026-10-28 明确要求"能自己随时调整"，故开写口，但必须带三道护栏：
#   ① 值域校验；② **可行性校验**（按最新价算 base_position，<100 股直接拒绝）；
#   ③ 写前备份 + config_audit.jsonl 留痕，并且明示"次日启动才生效"。
_CFG_PATH = ENGINE_ROOT / "data" / "live_config.json"
_AUDIT_PATH = _LIVE_DIR / "config_audit.jsonl"

_RISK_SPEC = {
    "max_symbol_position_pct": (0.05, 0.50, "单标的上限"),
    "max_order_value_pct": (0.02, 0.50, "单笔委托上限"),
    "max_daily_loss_pct": (0.005, 0.20, "日内亏损上限"),
    "max_symbol_cumulative_loss_pct": (0.02, 0.50, "单标的累计亏损上限"),
    "max_account_position_pct": (0.10, 1.00, "账户总仓位上限"),
}


def _trade_start() -> str:
    """从 live_match_loop.py 读 TRADE_START_DATE（避免在本文件里重复一份而漂移）。"""
    try:
        import re as _re
        txt = (ENGINE_ROOT / "live_match_loop.py").read_text(encoding="utf-8", errors="replace")
        m = _re.search(r'TRADE_START_DATE\s*=\s*pd\.Timestamp\("([\d-]+)"\)', txt)
        if m:
            return m.group(1)
    except Exception:  # noqa: BLE001
        pass
    return "2026-09-07"


def _price_at_trade_start(code: str) -> float | None:
    """引擎冻结参数用的价格 = **TRADE_START_DATE 起首个 close**（不是最新价！）。

    2026-10-28 踩过：面板按最新价 263.63 判"能买 100 股"，引擎按起点价 358.00 算
    只有 83 股 ⇒ 整百 0 股 ⇒ 启动直接抛"仓位不足100股"。校验口径必须与执行口径一致。
    """
    start = _trade_start()
    for p in (ENGINE_ROOT / "data" / "live" / f"stock_{code}_1m.csv",
              ENGINE_ROOT / "data" / f"stock_{code}_1m.csv"):
        try:
            if not p.is_file():
                continue
            with p.open("r", encoding="utf-8", errors="replace") as fh:
                fh.readline()                     # 表头
                for line in fh:
                    parts = line.split(",")
                    if len(parts) >= 5 and parts[0][:10] >= start:
                        return float(parts[4])    # close
        except Exception:  # noqa: BLE001
            continue
    return None


def _latest_price(code: str) -> float | None:
    """取最新价：优先 live 文件末根，其次历史 CSV 末根（都是 quant_app 产物）。"""
    for p in (ENGINE_ROOT / "data" / "live" / f"stock_{code}_1m.csv",
              ENGINE_ROOT / "data" / f"stock_{code}_1m.csv"):
        try:
            if p.is_file():
                lines = p.read_text(encoding="utf-8", errors="replace").splitlines()
                if len(lines) > 1:
                    return float(lines[-1].split(",")[4])
        except Exception:  # noqa: BLE001
            continue
    return None


def _editable_view() -> dict:
    """当前磁盘配置 + 每个标的的可行性预判（供界面实时提示）。"""
    raw = _read_json(_CFG_PATH) or {}
    cash = raw.get("initial_cash")
    risk_pct = raw.get("risk_pct", 0.10)
    risk = dict(raw.get("risk") or {})
    positions = raw.get("positions") or []
    checks = []
    for pos in positions:
        if not pos.get("enabled", True):
            continue
        code = str(pos.get("code") or "")
        px = _price_at_trade_start(code) or _latest_price(code)  # 与引擎冻结口径一致
        base = None
        if px and cash:
            base = int((float(cash) * float(risk_pct)) / px) // 100 * 100
        checks.append({
            "code": code, "strategy_id": pos.get("strategy_id"),
            "price": px, "base_position": base,
            "ok": (base is not None and base >= 100),
            "lota_pct": (round(px * 100 / float(cash), 4) if (px and cash) else None),
        })
    return {
        "path": str(_CFG_PATH),
        "exists": _CFG_PATH.is_file(),
        "current": {"initial_cash": cash, "risk_pct": risk_pct, "risk": risk,
                    "positions": positions},
        "risk_spec": {k: {"min": v[0], "max": v[1], "label": v[2]}
                      for k, v in _RISK_SPEC.items()},
        "position_checks": checks,
        "strategies": _supported_strategies(),
        "takes_effect": "次日 04:30 启动时生效（运行中进程不热改）",
    }


@router.post("/config")
def config_save(payload: dict) -> dict:
    """保存实盘口径（H64）。校验不过**一律拒绝**，绝不静默截断。"""
    raw = _read_json(_CFG_PATH) or {"version": 1, "positions": []}
    errs: list[str] = []

    cash = payload.get("initial_cash", raw.get("initial_cash"))
    if cash is None:
        errs.append("initial_cash 必须填写（实盘资金基准）")
    else:
        cash = float(cash)
        if not (50_000 <= cash <= 10_000_000):
            errs.append(f"initial_cash={cash:,.0f} 超出允许区间 [50,000, 10,000,000]")

    risk_pct = float(payload.get("risk_pct", raw.get("risk_pct", 0.10)))
    if not (0.02 <= risk_pct <= 0.50):
        errs.append(f"risk_pct={risk_pct} 超出 [0.02, 0.50]")

    risk = dict(raw.get("risk") or {})
    for k, v in dict(payload.get("risk") or {}).items():
        if k not in _RISK_SPEC:
            errs.append(f"未知风控字段：{k}")
            continue
        lo, hi, label = _RISK_SPEC[k]
        v = float(v)
        if not (lo <= v <= hi):
            errs.append(f"{label}({k})={v} 超出 [{lo}, {hi}]")
        risk[k] = v

    positions = payload.get("positions", raw.get("positions") or [])
    # 策略白名单与代码格式：面板能选什么，引擎就必须支持什么（否则启动才报错）
    _supported = {s["id"] for s in _supported_strategies()}
    for pos in positions:
        _code = str(pos.get("code") or "")
        _sid = str(pos.get("strategy_id") or "")
        if not re.fullmatch(r"\d{6}", _code):
            errs.append(f"代码格式不对：{_code!r}（应为 6 位数字）")
        if _sid not in _supported:
            errs.append(f"策略 {_sid!r} 不在 live 支持列表 {sorted(_supported)}")
    # 可行性：按**引擎冻结价**算底仓，<100 股直接拒绝（30 万 × 12% ÷ 358 = 100 股才勉强可行）
    _checked: set[str] = set()      # 同一 code 多个策略只报一次
    for pos in positions:
        if not pos.get("enabled", True):
            continue
        code = str(pos.get("code") or "")
        if code in _checked:
            continue
        _checked.add(code)
        px = _price_at_trade_start(code) or _latest_price(code)  # 与引擎冻结口径一致
        if px is None:
            errs.append(f"{code}: 拿不到最新价，无法校验 1 手是否买得起")
            continue
        base = int((cash * risk_pct) / px) // 100 * 100
        if base < 100:
            need = int(px * 100)
            errs.append(
                f"{code}: {cash:,.0f}×{risk_pct:.0%}={cash * risk_pct:,.0f} 元，"
                f"按引擎冻结价(09-07起) {px:.2f} 只够 {base} 股（1 手需 {need:,} 元）"
                f"⇒ 请提高资金/比例或换更便宜的标的")

    if errs:
        return {"ok": False, "errors": errs}

    new_cfg = dict(raw)
    new_cfg.update({"version": raw.get("version", 1),
                    "initial_cash": cash, "risk_pct": risk_pct,
                    "risk": risk, "positions": positions})
    new_cfg["updated_at"] = datetime.now().isoformat(timespec="seconds")

    # 写前备份（保留最近 5 份）
    try:
        if _CFG_PATH.is_file():
            bak_dir = _CFG_PATH.parent / "_config_backup"
            bak_dir.mkdir(parents=True, exist_ok=True)
            bak = bak_dir / f"live_config_{datetime.now():%Y%m%d_%H%M%S}.json"
            bak.write_text(_CFG_PATH.read_text(encoding="utf-8"), encoding="utf-8")
            olds = sorted(bak_dir.glob("live_config_*.json"))[:-5]
            for old in olds:
                old.unlink(missing_ok=True)
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "errors": [f"备份旧配置失败，已中止写入：{e!r}"]}

    tmp = _CFG_PATH.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(new_cfg, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, _CFG_PATH)

    try:
        _AUDIT_PATH.parent.mkdir(parents=True, exist_ok=True)
        with _AUDIT_PATH.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps({
                "at": datetime.now().isoformat(timespec="seconds"),
                "source": "tickflow-panel",
                "initial_cash": cash, "risk_pct": risk_pct, "risk": risk,
                "positions": [f"{p.get('code')}×{p.get('strategy_id')}" for p in positions],
            }, ensure_ascii=False) + "\n")
    except Exception:  # noqa: BLE001 — 审计写失败不回滚配置，但要在返回里说明
        return {"ok": True, "warning": "配置已保存，但审计日志写入失败",
                "takes_effect": "次日 04:30 启动时生效", "path": str(_CFG_PATH)}

    return {"ok": True, "takes_effect": "次日 04:30 启动时生效（运行中进程不热改）",
            "path": str(_CFG_PATH)}


def setup(registrar: BackendExtensionRegistrar) -> None:
    registrar.include_router(router)
