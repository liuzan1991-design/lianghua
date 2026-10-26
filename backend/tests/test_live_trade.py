# -*- coding: utf-8 -*-
"""实盘交易扩展的闸门测试。

重点不是"接口能不能通"，而是**四道闸是否真的挡住**：
  ① 未设 QMT_TRADING_ARMED → 任何写操作 403
  ② 没 token / token 不匹配 → 403
  ③ 标的白名单 / 单笔额度 / 单日笔数 → 各自拦下
  ④ 会话没配好 → 只读接口 503 且说清缺什么（不能 500 崩）
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

BACKEND = Path(__file__).resolve().parents[1]
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

import app.custom.live_trade as lt  # noqa: E402

PHRASE = "确认下单"
SECRET = "test-secret-please-change"


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(lt, "_AUDIT", tmp_path / "audit.jsonl")
    monkeypatch.setattr(lt, "_LOG_DIR", tmp_path / "logs")
    monkeypatch.setattr(lt, "_broker_ref", None)
    monkeypatch.setattr(lt, "_arm_token", None)
    monkeypatch.setattr(lt, "_arm_expire_at", 0.0)
    monkeypatch.delenv("QMT_TRADING_ARMED", raising=False)
    monkeypatch.delenv("QMT_USERDATA_MINI", raising=False)
    # 自动探测必须被关掉：本机真的装了 QMT，"未配置"的测试前提会不成立
    # （不关的话测试结果依赖机器环境，换台机器就红/绿翻转）
    monkeypatch.setattr(lt, "_detect_userdata", lambda: "")
    monkeypatch.setenv("TRADE_ARM_SECRET", SECRET)
    monkeypatch.setenv("TRADE_ALLOWED_CODES", "300308")
    monkeypatch.setenv("TRADE_MAX_ORDER_NOTIONAL", "200000")
    monkeypatch.setenv("TRADE_MAX_ORDERS_PER_DAY", "20")
    app = FastAPI()
    app.include_router(lt.router)
    return TestClient(app)


def _arm(client, monkeypatch) -> str:
    monkeypatch.setenv("QMT_TRADING_ARMED", "1")
    r = client.post("/api/custom/trade/gate",
                    json={"action": "arm", "phrase": PHRASE, "secret": SECRET})
    assert r.status_code == 200, r.text
    return r.json()["token"]


# ── 闸 1：部署级 ─────────────────────────────────────────────────────────
def test_gate_arm_requires_env(client) -> None:
    r = client.post("/api/custom/trade/gate",
                    json={"action": "arm", "phrase": PHRASE, "secret": SECRET})
    assert r.status_code == 403
    assert "QMT_TRADING_ARMED" in r.json()["detail"]


def test_gate_arm_requires_secret_configured(client, monkeypatch) -> None:
    """没配共享密钥就绝不放行 —— 内网放行场景下的兜底。"""
    monkeypatch.setenv("QMT_TRADING_ARMED", "1")
    monkeypatch.delenv("TRADE_ARM_SECRET", raising=False)
    r = client.post("/api/custom/trade/gate",
                    json={"action": "arm", "phrase": PHRASE, "secret": SECRET})
    assert r.status_code == 503
    assert "TRADE_ARM_SECRET" in r.json()["detail"]


def test_gate_arm_wrong_secret(client, monkeypatch) -> None:
    monkeypatch.setenv("QMT_TRADING_ARMED", "1")
    r = client.post("/api/custom/trade/gate",
                    json={"action": "arm", "phrase": PHRASE, "secret": "wrong"})
    assert r.status_code == 403


def test_gate_arm_requires_phrase(client, monkeypatch) -> None:
    monkeypatch.setenv("QMT_TRADING_ARMED", "1")
    r = client.post("/api/custom/trade/gate",
                    json={"action": "arm", "phrase": "随便", "secret": SECRET})
    assert r.status_code == 400


def test_gate_arm_returns_token(client, monkeypatch) -> None:
    tok = _arm(client, monkeypatch)
    assert len(tok) == 32
    assert client.get("/api/custom/trade/gate").json()["session_armed"] is True


def test_gate_disarm(client, monkeypatch) -> None:
    _arm(client, monkeypatch)
    client.post("/api/custom/trade/gate", json={"action": "disarm"})
    assert client.get("/api/custom/trade/gate").json()["session_armed"] is False


def test_order_rejected_when_env_revoked_after_arm(client, monkeypatch) -> None:
    """纵深防御：拿到 token 后撤掉部署级 env，仍然必须拒单。

    这条是 mutation 补出来的 —— 原来只测了 /gate 处的 env，没测 _require_arm 处的 env，
    把 _require_arm 里的 env 判断拆掉时 16 例全绿，说明闸根本没被覆盖。
    """
    tok = _arm(client, monkeypatch)
    monkeypatch.delenv("QMT_TRADING_ARMED", raising=False)
    r = client.post("/api/custom/trade/orders", headers={"X-Trade-Arm": tok},
                    json={"code": "300308", "side": "BUY", "quantity": 100, "price": 100.0})
    assert r.status_code == 403
    assert "QMT_TRADING_ARMED" in r.json()["detail"]


# ── 闸 2：会话级 ─────────────────────────────────────────────────────────
def test_order_without_token_rejected(client, monkeypatch) -> None:
    _arm(client, monkeypatch)
    r = client.post("/api/custom/trade/orders",
                    json={"code": "300308", "side": "BUY", "quantity": 100, "price": 100.0})
    assert r.status_code == 403
    assert "token" in r.json()["detail"]


def test_order_with_wrong_token_rejected(client, monkeypatch) -> None:
    _arm(client, monkeypatch)
    r = client.post("/api/custom/trade/orders", headers={"X-Trade-Arm": "deadbeef"},
                    json={"code": "300308", "side": "BUY", "quantity": 100, "price": 100.0})
    assert r.status_code == 403


# ── 闸 3：额度级 ─────────────────────────────────────────────────────────
def test_order_code_not_allowed(client, monkeypatch) -> None:
    tok = _arm(client, monkeypatch)
    r = client.post("/api/custom/trade/orders", headers={"X-Trade-Arm": tok},
                    json={"code": "600900", "side": "BUY", "quantity": 100, "price": 10.0})
    assert r.status_code == 403
    assert "允许清单" in r.json()["detail"]


def test_order_notional_limit(client, monkeypatch) -> None:
    tok = _arm(client, monkeypatch)
    r = client.post("/api/custom/trade/orders", headers={"X-Trade-Arm": tok},
                    json={"code": "300308", "side": "BUY", "quantity": 1000, "price": 500.0})
    assert r.status_code == 400
    assert "上限" in r.json()["detail"]


def test_order_daily_count_limit(client, monkeypatch) -> None:
    tok = _arm(client, monkeypatch)
    monkeypatch.setattr(lt, "_today_orders", lambda: 20)
    r = client.post("/api/custom/trade/orders", headers={"X-Trade-Arm": tok},
                    json={"code": "300308", "side": "BUY", "quantity": 100, "price": 100.0})
    assert r.status_code == 400
    assert "上限" in r.json()["detail"]


# ── 降级：没配 QMT 时必须 503 而不是 500 ─────────────────────────────────
def test_read_endpoints_degrade_without_config(client) -> None:
    for path in ("/api/custom/trade/account", "/api/custom/trade/positions",
                 "/api/custom/trade/orders"):
        r = client.get(path)
        assert r.status_code == 503, f"{path} 应 503，实际 {r.status_code}"
        assert "QMT_USERDATA_MINI" in r.json()["detail"]


def test_health_reports_limits_and_gate(client) -> None:
    body = client.get("/api/custom/trade/health").json()
    assert body["limits"]["allowed_codes"] == ["300308"]
    assert body["gate"]["env_armed"] is False
    assert body["broker_ready"] is False


# ── userdata 探测：env 优先，未设则按 quant_app 同款规则自动探测 ────────────
def test_userdata_env_takes_precedence(client, monkeypatch, tmp_path) -> None:
    """env 显式配置时必须优先，且**不再调用探测**（避免两处配置漂移）。"""
    called = {"n": 0}

    def _spy():
        called["n"] += 1
        return str(tmp_path / "auto")

    monkeypatch.setattr(lt, "_detect_userdata", _spy)
    monkeypatch.setenv("QMT_USERDATA_MINI", str(tmp_path / "env"))
    body = client.get("/api/custom/trade/health").json()
    assert body["userdata_source"] == "env"
    assert body["userdata_path"] == str(tmp_path / "env")
    assert called["n"] == 0, "已显式配置时不该再去探测"


def test_userdata_auto_detected_when_env_absent(client, monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(lt, "_detect_userdata", lambda: str(tmp_path / "auto"))
    body = client.get("/api/custom/trade/health").json()
    assert body["userdata_source"] == "auto"
    assert body["userdata_configured"] is True


# ── 心跳新鲜度：heartbeat.json 跨进程留存，不能当成实时状态 ─────────────────
def _write_hb(root, *, age_seconds: float) -> None:
    from datetime import datetime, timedelta

    d = root / "live_outputs"
    d.mkdir(parents=True, exist_ok=True)
    ts = (datetime.now() - timedelta(seconds=age_seconds)).isoformat(timespec="seconds")
    (d / "heartbeat.json").write_text(
        json.dumps({"status": "running", "started_at": ts, "updated_at": ts,
                    "last_success_at": None, "collect_errors": 139,
                    "breaker_state": "closed"}, ensure_ascii=False),
        encoding="utf-8")


def test_live_stale_heartbeat_not_reported_as_running(client, monkeypatch, tmp_path) -> None:
    """4 小时前的残留心跳：既不是 running，也不能算 external_running。"""
    monkeypatch.setattr(lt, "_ENGINE_ROOT", tmp_path)
    _write_hb(tmp_path, age_seconds=4 * 3600)
    body = client.get("/api/custom/trade/live").json()
    assert body["running"] is False
    assert body["external_running"] is False
    assert body["heartbeat_fresh"] is False
    assert body["heartbeat_age_seconds"] >= 3600


def test_live_fresh_heartbeat_marks_external(client, monkeypatch, tmp_path) -> None:
    """心跳仍在更新但不是本页起的 ⇒ 必须标成 external，且不允许本页 stop。"""
    monkeypatch.setattr(lt, "_ENGINE_ROOT", tmp_path)
    _write_hb(tmp_path, age_seconds=10)
    body = client.get("/api/custom/trade/live").json()
    assert body["running"] is False
    assert body["managed_by_page"] is False
    assert body["external_running"] is True
    assert body["heartbeat_fresh"] is True


def test_live_stop_explains_external_instance(client, monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(lt, "_ENGINE_ROOT", tmp_path)
    _write_hb(tmp_path, age_seconds=10)
    tok = _arm(client, monkeypatch)
    r = client.post("/api/custom/trade/live/stop", headers={"X-Trade-Arm": tok})
    assert r.status_code == 409
    assert "计划任务" in r.json()["detail"]


# ── 正常路径：用假 broker 验证下单会被记账 ───────────────────────────────
class _FakeResult:
    ok = True
    order_id = 4242
    reason = ""


class _FakeBroker:
    def __init__(self) -> None:
        self.calls = []

    def submit(self, code, side, qty, price, remark=""):
        self.calls.append((code, side, qty, price, remark))
        return _FakeResult()

    def cancel(self, order_id):
        return True


def test_order_success_writes_audit(client, monkeypatch) -> None:
    tok = _arm(client, monkeypatch)
    fake = _FakeBroker()
    monkeypatch.setattr(lt, "_broker", lambda: fake)
    r = client.post("/api/custom/trade/orders", headers={"X-Trade-Arm": tok},
                    json={"code": "300308", "side": "BUY", "quantity": 100, "price": 100.0})
    assert r.status_code == 200, r.text
    assert r.json()["order_id"] == 4242
    assert fake.calls == [("300308", "BUY", 100, 100.0, "trade-manual")]
    events = [json.loads(x) for x in lt._AUDIT.read_text(encoding="utf-8").splitlines()]
    assert any(e["event"] == "order.submit" and e["ok"] for e in events)


def test_cancel_success(client, monkeypatch) -> None:
    tok = _arm(client, monkeypatch)
    monkeypatch.setattr(lt, "_broker", lambda: _FakeBroker())
    r = client.post("/api/custom/trade/orders/4242/cancel", headers={"X-Trade-Arm": tok})
    assert r.status_code == 200


def test_audit_endpoint(client, monkeypatch) -> None:
    tok = _arm(client, monkeypatch)
    monkeypatch.setattr(lt, "_broker", lambda: _FakeBroker())
    client.post("/api/custom/trade/orders", headers={"X-Trade-Arm": tok},
                json={"code": "300308", "side": "SELL", "quantity": 100, "price": 100.0})
    body = client.get("/api/custom/trade/audit").json()
    assert body["total"] >= 2  # arm + submit
