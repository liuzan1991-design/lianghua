# -*- coding: utf-8 -*-
"""实盘交易扩展（live-trade）：账户/持仓/委托 + 手动下单/撤单 + live 进程起停。

定位
----
平台本身是「选股 + 监控 + 回测」，**没有任何执行层**（全仓 `xttrader`/`order_stock` 命中 0 处）。
本扩展补上执行层，成交与撮合全部在券商侧完成，本模块**不做任何撮合**。

与 M2 的关系
-----------
M2（`m2_intraday_t.py`）已经把 quant_app 的四个策略接进来做**回测**（子进程调 worker）。
本扩展复用同一套「按路径挂载 quant_app」的做法，但指向**实盘**：
交易能力来自 quant_app 的 `core/qmt_broker.py`（已带 ARM 闸门与参数校验）。

安全设计：四道闸，缺一不可
-------------------------
L1 **部署级**：环境变量 `QMT_TRADING_ARMED=1` 才可能下单（`core/qmt_broker.armed()` 同源）。
L2 **会话级**：`POST /gate` 需要「口令 + 共享密钥」双要素，发放 15 分钟有效的 token；
   所有写操作必须带 `X-Trade-Arm: <token>`。**重启进程即失效**。
   ⚠️ 为什么还要共享密钥：平台 `main.py` 的鉴权中间件在**未设访问密码时对本机/内网直接放行**，
   局域网里任何人只要能访问端口就能调接口。只靠口令等于把下单权限摊给整个内网。
L3 **额度级**：单笔名义金额上限 + 单日委托笔数上限 + 标的允许清单，任一超限直接拒。
L4 **审计级**：每一次 arm / 下单 / 撤单 / 起停都落 `audit.jsonl`（含来源 IP 与 token 指纹，不存原文）。

为什么默认全关
-------------
国盛 QMT 无模拟盘，**首次下单即实盘**；且 quant_app 侧 H59（成本台账不一致）尚未修复。
所以本扩展的默认状态是：没配 env 就完全不可下单，只读接口照常可用。

配置（全部走环境变量）
--------------------
    INTRADAY_T_ENGINE_ROOT   quant_app 根目录（默认与 M2 相同）
    TRADE_STATE_ROOT            状态/审计目录（默认 ./data/live_trade）
    QMT_USERDATA_MINI        miniQMT 的 userdata_mini 路径（**必填，否则交易接口 503**）
    QMT_ACCOUNT_ID           资金账号（可选，不填则取会话里第一个）
    QMT_TRADING_ARMED        "1" 才允许下单
    TRADE_ARM_SECRET            发放下单 token 的共享密钥（**必填，否则拒绝 arm**）
    TRADE_ALLOWED_CODES         允许下单的代码，逗号分隔（默认 300308）
    TRADE_MAX_ORDER_NOTIONAL    单笔上限，元（默认 200000）
    TRADE_MAX_ORDERS_PER_DAY    单日委托上限，笔（默认 20）
    TRADE_ARM_TTL_SECONDS       token 有效期（默认 900）
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import subprocess
import sys
import threading
import time
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Header, HTTPException, Query, Request
from pydantic import BaseModel, Field

from app.extensions import BACKEND_EXTENSION_API_VERSION, BackendExtensionRegistrar

EXTENSION_ID = "tsp.live-trade"
EXTENSION_API_VERSION = BACKEND_EXTENSION_API_VERSION

_ENGINE_ROOT = Path(os.getenv(
    "INTRADAY_T_ENGINE_ROOT", r"D:\Documents\ChatGPT\daily work\日内做T策略\quant_app"))
_STATE_ROOT = Path(os.getenv("TRADE_STATE_ROOT", str(Path.cwd() / "data" / "live_trade")))
_AUDIT = _STATE_ROOT / "audit.jsonl"
_LOG_DIR = _STATE_ROOT / "logs"
_ARM_PHRASE = "确认下单"

router = APIRouter(prefix="/api/custom/trade", tags=["live-trade"])

_lock = threading.Lock()
_arm_token: str | None = None
_arm_expire_at: float = 0.0
_broker_ref: Any = None
_runner: subprocess.Popen | None = None


# ── 配置 ────────────────────────────────────────────────────────────────
def _detect_userdata() -> str:
    """按 quant_app 既有规则探测 `userdata_mini`，找到返回路径。

    ⚠️ 不在本扩展里另存一份配置 —— 那会和 `quant_app/scripts/tool_qmt_trader_probe.py:49
    _qmt_userdata()` 漂移成两套。这里只是**复用同一套探测规则**：
      ① xtdata 报的数据目录（去掉末尾 /datadir）
      ② 券商客户端默认安装位置
    安全不受影响：能否下单只由 QMT_TRADING_ARMED + token + 共享密钥决定，
    userdata 只是"连哪个客户端"，不是授权。
    """
    cands: list[Path] = []
    try:
        from xtquant.xtdata import get_client  # noqa: PLC0415

        info = getattr(get_client(), "get_data_dir", lambda: "")()
        if info:
            p = Path(str(info))
            cands.append(p.parent if p.name.lower() == "datadir" else p)
    except Exception:  # noqa: BLE001
        pass
    cands.append(Path(r"D:\迅投QMT交易终端   国盛证券实盘版\userdata_mini"))
    for c in cands:
        if c.is_dir():
            return str(c)
    return ""


def _cfg() -> dict:
    def _int(name: str, default: int) -> int:
        try:
            return int(os.getenv(name, "").strip() or default)
        except ValueError:
            return default

    codes = [c.strip() for c in (os.getenv("TRADE_ALLOWED_CODES", "300308").split(",")) if c.strip()]
    env_ud = os.getenv("QMT_USERDATA_MINI", "").strip()
    auto_ud = "" if env_ud else _detect_userdata()
    return {
        "engine_root": str(_ENGINE_ROOT),
        "userdata_mini": env_ud or auto_ud,
        "userdata_source": "env" if env_ud else ("auto" if auto_ud else "missing"),
        "account_id": os.getenv("QMT_ACCOUNT_ID", "").strip() or None,
        "max_notional": float(_int("TRADE_MAX_ORDER_NOTIONAL", 200_000)),
        "max_orders_per_day": _int("TRADE_MAX_ORDERS_PER_DAY", 20),
        "arm_ttl": _int("TRADE_ARM_TTL_SECONDS", 900),
        "arm_secret": os.getenv("TRADE_ARM_SECRET", "").strip(),
        "allowed_codes": set(codes),
    }


def _now() -> str:
    return datetime.now(UTC).astimezone().isoformat(timespec="seconds")


# ── 审计 ────────────────────────────────────────────────────────────────
def _audit(event: str, payload: dict, *, client: str = "", ok: bool = True) -> None:
    rec = {"ts": _now(), "event": event, "ok": ok, "client": client, "payload": payload}
    try:
        _AUDIT.parent.mkdir(parents=True, exist_ok=True)
        with _AUDIT.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
    except Exception:  # 审计失败不能影响主流程，但也绝不静默成功——写进 stderr
        print(f"[trade] audit write failed: {rec}", file=sys.stderr)


def _today_orders() -> int:
    """当日已下单笔数（从审计流水统计，跨进程重启仍可延续）。"""
    if not _AUDIT.is_file():
        return 0
    today = datetime.now().strftime("%Y-%m-%d")
    n = 0
    try:
        for line in _AUDIT.read_text(encoding="utf-8").splitlines():
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            if rec.get("event") == "order.submit" and str(rec.get("ts", "")).startswith(today):
                n += 1
    except OSError:
        return 0
    return n


# ── ARM 闸 ──────────────────────────────────────────────────────────────
def _env_armed() -> bool:
    return os.getenv("QMT_TRADING_ARMED", "").strip() == "1"


def _arm_state() -> dict:
    left = max(0, int(_arm_expire_at - time.time())) if _arm_expire_at else 0
    return {"env_armed": _env_armed(), "session_armed": bool(_arm_token) and left > 0,
            "expires_in": left}


def _require_arm(token: str | None) -> None:
    """写操作的第二道闸：env + 会话 token 都必须在。"""
    if not _env_armed():
        raise HTTPException(403, "部署级未授权：QMT_TRADING_ARMED 未设为 1")
    if not _arm_token or time.time() >= _arm_expire_at:
        raise HTTPException(403, "会话未授权：请先 POST /gate 获取 token（15 分钟内有效）")
    if not token or token != _arm_token:
        raise HTTPException(403, "token 不匹配")


# ── broker 懒加载 ────────────────────────────────────────────────────────
def _broker():
    """按需建立 QMT 会话。任何一步不满足都抛 503 并说清缺什么。"""
    global _broker_ref
    with _lock:
        if _broker_ref is not None:
            return _broker_ref
        cfg = _cfg()
        engine = Path(cfg["engine_root"])
        if not (engine / "core" / "qmt_broker.py").is_file():
            raise HTTPException(503, f"engine_root 下找不到 core/qmt_broker.py：{engine}")
        if not cfg["userdata_mini"]:
            raise HTTPException(503, "未配置 QMT_USERDATA_MINI（miniQMT 的 userdata_mini 路径）")
        if str(engine) not in sys.path:
            sys.path.insert(0, str(engine))
        try:
            from core.qmt_broker import QmtBroker, build_session  # noqa: PLC0415
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(503, f"导入 core.qmt_broker 失败：{type(exc).__name__}: {exc}") from exc
        # session_id 必须与在用会话错开（撞号会顶掉对方）⇒ 每次进程启动取一个新的
        session_id = int(os.getenv("TRADE_QMT_SESSION_ID", "0") or 0) or (
            int(time.time()) % 100000 + 9000)
        try:
            trader, account = build_session(cfg["userdata_mini"], session_id, cfg["account_id"])
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(503, f"QMT 会话建立失败：{type(exc).__name__}: {exc}") from exc
        _broker_ref = QmtBroker(trader, account, strategy_name="tsp-trade")
        return _broker_ref


def _dump(obj, fields: tuple[str, ...]) -> dict | None:
    """xtquant 对象 → dict（只取白名单字段，避免把内部结构吐给前端）。"""
    if obj is None:
        return None
    out = {f: getattr(obj, f, None) for f in fields}
    return out


_ASSET_FIELDS = ("cash", "frozen_cash", "market_value", "total_asset")
_POS_FIELDS = ("stock_code", "volume", "can_use_volume", "open_price",
               "market_value", "frozen_volume", "on_road_volume", "avg_price")
_ORDER_FIELDS = ("order_id", "stock_code", "order_type", "order_volume", "price",
                 "traded_volume", "order_status", "status_msg", "order_time",
                 "traded_price", "strategy_name", "order_remark")


# ── 请求模型 ────────────────────────────────────────────────────────────
class GateIn(BaseModel):
    action: str = Field(pattern="^(arm|disarm)$")
    phrase: str = ""
    secret: str = ""


class OrderIn(BaseModel):
    code: str = Field(min_length=6, max_length=9)
    side: str = Field(pattern="^(BUY|SELL|buy|sell|买入|卖出)$")
    quantity: int = Field(gt=0)
    price: float = Field(gt=0)
    remark: str = Field(default="", max_length=64)


# ── 路由：只读 ──────────────────────────────────────────────────────────
@router.get("/health")
def health() -> dict:
    cfg = _cfg()
    engine = Path(cfg["engine_root"])
    qmt_py = (engine / "core" / "qmt_broker.py").is_file()
    return {
        "extension": EXTENSION_ID,
        "api_version": EXTENSION_API_VERSION,
        "engine_root": str(engine),
        "engine_ok": qmt_py,
        "userdata_configured": bool(cfg["userdata_mini"]),
        "userdata_source": cfg["userdata_source"],   # env / auto / missing
        "userdata_path": cfg["userdata_mini"] or None,
        "broker_ready": _broker_ref is not None,
        "limits": {
            "max_order_notional": cfg["max_notional"],
            "max_orders_per_day": cfg["max_orders_per_day"],
            "orders_used_today": _today_orders(),
            "allowed_codes": sorted(cfg["allowed_codes"]),
        },
        "gate": _arm_state(),
    }


@router.get("/gate")
def gate_state() -> dict:
    return _arm_state()


@router.post("/gate")
def gate_set(body: GateIn, request: Request) -> dict:
    global _arm_token, _arm_expire_at
    client = request.client.host if request.client else ""
    if body.action == "disarm":
        _arm_token, _arm_expire_at = None, 0.0
        _audit("gate.disarm", {}, client=client)
        return {"ok": True, **_arm_state()}
    if not _env_armed():
        raise HTTPException(403, "部署级未授权：先把 QMT_TRADING_ARMED 设为 1 并重启后端")
    cfg = _cfg()
    if not cfg["arm_secret"]:
        # 没配密钥就绝不放行：平台在内网未设密码时是放行的，只靠口令等于敞开
        raise HTTPException(503, "未配置 TRADE_ARM_SECRET（共享密钥），拒绝发放下单 token")
    if not hmac.compare_digest(body.secret.strip(), cfg["arm_secret"]):
        _audit("gate.bad_secret", {}, client=client, ok=False)
        raise HTTPException(403, "共享密钥不正确")
    if body.phrase.strip() != _ARM_PHRASE:
        raise HTTPException(400, f"口令不正确（需填写「{_ARM_PHRASE}」）")
    ttl = cfg["arm_ttl"]
    _arm_token = uuid.uuid4().hex
    _arm_expire_at = time.time() + ttl
    _audit("gate.arm", {"ttl": ttl, "token_fp": hashlib.sha256(_arm_token.encode()).hexdigest()[:12]},
           client=client)
    return {"ok": True, "token": _arm_token, "ttl": ttl, **_arm_state()}


@router.get("/account")
def account() -> dict:
    return {"asset": _dump(_broker().query_asset(), _ASSET_FIELDS)}


@router.get("/positions")
def positions() -> dict:
    raw = _broker().query_positions() or []
    return {"positions": [p for p in (_dump(x, _POS_FIELDS) for x in raw) if p]}


@router.get("/orders")
def orders(limit: int = Query(default=50, ge=1, le=500)) -> dict:
    raw = _broker().query_orders() or []
    rows = [o for o in (_dump(x, _ORDER_FIELDS) for x in raw) if o]
    return {"orders": rows[-limit:], "total": len(rows)}


# ── 路由：写（需 arm）───────────────────────────────────────────────────
@router.post("/orders")
def place_order(body: OrderIn, request: Request,
                x_trade_arm: str | None = Header(default=None)) -> dict:
    client = request.client.host if request.client else ""
    _require_arm(x_trade_arm)

    cfg = _cfg()
    code = body.code.strip().split(".")[0]
    notional = float(body.price) * int(body.quantity)

    # L3 额度闸：任一超限直接拒，不做“部分成交”这种自作聪明
    if cfg["allowed_codes"] and code not in cfg["allowed_codes"]:
        _audit("order.reject", {"reason": "code_not_allowed", "code": code}, client=client, ok=False)
        raise HTTPException(403, f"标的 {code} 不在允许清单内：{sorted(cfg['allowed_codes'])}")
    if notional > cfg["max_notional"]:
        _audit("order.reject", {"reason": "notional", "notional": notional},
               client=client, ok=False)
        raise HTTPException(400, f"单笔名义金额 {notional:,.0f} 超过上限 {cfg['max_notional']:,.0f}")
    if _today_orders() >= cfg["max_orders_per_day"]:
        _audit("order.reject", {"reason": "daily_count"}, client=client, ok=False)
        raise HTTPException(400, f"当日委托已达上限 {cfg['max_orders_per_day']} 笔")

    try:
        res = _broker().submit(code, body.side, int(body.quantity), float(body.price),
                               remark=(body.remark or "trade-manual")[:16])
    except HTTPException:
        raise
    except Exception as exc:  # noqa: BLE001
        _audit("order.error", {"code": code, "err": f"{type(exc).__name__}: {exc}"},
               client=client, ok=False)
        raise HTTPException(400, f"下单失败：{type(exc).__name__}: {exc}") from exc

    payload = {"code": code, "side": body.side, "qty": body.quantity,
               "price": body.price, "notional": notional,
               "order_id": res.order_id, "ok": res.ok}
    _audit("order.submit", payload, client=client, ok=bool(res.ok))
    if not res.ok:
        raise HTTPException(502, f"券商拒单：{res.reason}")
    return {"ok": True, **payload}


@router.post("/orders/{order_id}/cancel")
def cancel_order(order_id: int, request: Request,
                 x_trade_arm: str | None = Header(default=None)) -> dict:
    client = request.client.host if request.client else ""
    _require_arm(x_trade_arm)
    try:
        ok = _broker().cancel(int(order_id))
    except Exception as exc:  # noqa: BLE001
        _audit("order.cancel.error", {"order_id": order_id, "err": str(exc)},
               client=client, ok=False)
        raise HTTPException(400, f"撤单失败：{type(exc).__name__}: {exc}") from exc
    _audit("order.cancel", {"order_id": order_id, "ok": bool(ok)}, client=client, ok=bool(ok))
    if not ok:
        raise HTTPException(502, "券商拒绝撤单（可能已成交/已撤）")
    return {"ok": True, "order_id": order_id}


# ── 路由：实盘进程起停 ──────────────────────────────────────────────────
@router.get("/live")
def live_status() -> dict:
    """实盘进程状态。

    ⚠️ 两个坑（都实测踩过，不能只看 running 字段）：
    ① heartbeat.json 是**跨进程持久化**的，进程死了它还在。只看 started_at 会把
       几小时前失败退出的实例误读成"刚跑过" ⇒ 必须给新鲜度（heartbeat_age / fresh）。
    ② `_runner` 是**内存变量**，后端一重启就丢。计划任务起的实例、或重启前由本页起的
       实例，都会显示 running=false 却确实在跑 ⇒ 用「心跳新鲜」推断外部实例。
    """
    global _runner
    alive = _runner is not None and _runner.poll() is None
    hb = Path(_cfg()["engine_root"]) / "live_outputs" / "heartbeat.json"
    summary = None
    age: float | None = None
    if hb.is_file():
        try:
            raw = json.loads(hb.read_text(encoding="utf-8"))
            summary = {k: raw.get(k) for k in
                       ("status", "started_at", "last_success_at", "collect_errors",
                        "breaker_state", "updated_at")}
            ts = raw.get("updated_at")
            if ts:
                dt = datetime.fromisoformat(str(ts))
                now = datetime.now(dt.tzinfo) if dt.tzinfo else datetime.now()
                age = (now - dt).total_seconds()
        except Exception:  # noqa: BLE001
            summary = None
    # 采集循环每 ~36s 刷一次心跳 ⇒ 180s 内有更新就认为有实例在跑
    fresh = age is not None and age <= 180
    return {
        "running": alive,
        "pid": _runner.pid if alive else None,
        "managed_by_page": alive,            # true 才允许本页 /live/stop
        "external_running": (not alive) and fresh,
        "heartbeat": summary,
        "heartbeat_age_seconds": round(age) if age is not None else None,
        "heartbeat_fresh": fresh,
    }


@router.post("/live/start")
def live_start(request: Request, x_trade_arm: str | None = Header(default=None)) -> dict:
    """启动 quant_app 的 live_run.py（自动跑策略）。与计划任务同一入口。"""
    global _runner
    client = request.client.host if request.client else ""
    _require_arm(x_trade_arm)
    if _runner is not None and _runner.poll() is None:
        raise HTTPException(409, f"已有实例在跑（pid={_runner.pid}）")
    engine = Path(_cfg()["engine_root"])
    entry = engine / "live_run.py"
    if not entry.is_file():
        raise HTTPException(503, f"找不到 {entry}")
    _LOG_DIR.mkdir(parents=True, exist_ok=True)
    log = _LOG_DIR / f"live_{datetime.now():%Y%m%d_%H%M%S}.log"
    try:
        _runner = subprocess.Popen(  # noqa: S603
            [sys.executable, str(entry)], cwd=str(engine),
            stdout=log.open("w", encoding="utf-8"), stderr=subprocess.STDOUT,
            env={**os.environ, "TRADE_STARTED_BY": "tsp-trade"})
    except Exception as exc:  # noqa: BLE001
        _audit("live.start.error", {"err": str(exc)}, client=client, ok=False)
        raise HTTPException(500, f"启动失败：{type(exc).__name__}: {exc}") from exc
    _audit("live.start", {"pid": _runner.pid, "log": str(log)}, client=client)
    return {"ok": True, "pid": _runner.pid, "log": str(log)}


@router.post("/live/stop")
def live_stop(request: Request, x_trade_arm: str | None = Header(default=None)) -> dict:
    global _runner
    client = request.client.host if request.client else ""
    _require_arm(x_trade_arm)
    if _runner is None or _runner.poll() is not None:
        # 区分两种"没得停"：真的没跑 / 有实例但不是本页起的（不能盲杀）
        st = live_status()
        if st.get("external_running"):
            raise HTTPException(
                409,
                "检测到计划任务启动的实例仍在跑（心跳仍在更新），本页无权停止。"
                "请到 Windows 计划任务 / 任务管理器结束该 python 进程。")
        raise HTTPException(409, "当前没有 live 实例在运行（心跳已停止更新）")
    pid = _runner.pid
    _runner.terminate()
    try:
        _runner.wait(timeout=15)
    except subprocess.TimeoutExpired:
        _runner.kill()
    _audit("live.stop", {"pid": pid}, client=client)
    return {"ok": True, "pid": pid}


# ── 路由：审计 ──────────────────────────────────────────────────────────
@router.get("/audit")
def audit_tail(limit: int = Query(default=100, ge=1, le=1000)) -> dict:
    if not _AUDIT.is_file():
        return {"events": [], "total": 0}
    lines = _AUDIT.read_text(encoding="utf-8").splitlines()
    out = []
    for line in lines[-limit:]:
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return {"events": out, "total": len(lines)}


def setup(registrar: BackendExtensionRegistrar) -> None:
    registrar.include_router(router)
