"""M2独立做T回测服务扩展: 主进程调度, 子进程执行。"""
from __future__ import annotations

import asyncio
import json
import mimetypes
import os
import shutil
import subprocess
import sys
import threading
import time
import uuid
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import FileResponse, StreamingResponse
from pydantic import BaseModel, Field, model_validator

from app.extensions import BACKEND_EXTENSION_API_VERSION, BackendExtensionRegistrar

EXTENSION_ID = "m2.intraday-t"
EXTENSION_API_VERSION = BACKEND_EXTENSION_API_VERSION

_ENGINE_ROOT = Path(os.getenv("INTRADAY_T_ENGINE_ROOT", r"D:\Documents\ChatGPT\daily work\日内做T策略\quant_app"))
_DATA_ROOT = Path(os.getenv("INTRADAY_T_DATA_ROOT", str(_ENGINE_ROOT / "data")))
_STATE_ROOT = Path(os.getenv("M2_BACKTEST_STATE_ROOT", str(Path.cwd() / "data" / "m2_backtests")))
_WORKER = Path(__file__).with_name("m2_worker.py")
_PYTHON = Path(os.getenv("M2_WORKER_PYTHON", sys.executable))
_TERMINAL = {"completed", "failed", "cancelled", "interrupted", "timeout"}
_STRATEGIES = {
    "intraday_t": {"name": "日内做T", "description": "日内做T策略"},
    "grid_trade": {"name": "移动网格", "description": "移动网格策略"},
    "sentiment_t": {"name": "大盘情绪做T", "description": "大盘情绪做T策略"},
    "ma_swing": {"name": "均线波段", "description": "均线波段策略"},
}


def _quant_python() -> str:
    """quant_app 生产解释器 —— 主进程 venv 没有 pandas，不能直接 import strategies。"""
    env = os.getenv("QUANT_PYTHON", "").strip()
    if env and Path(env).is_file():
        return env
    for cand in (r"D:\Anaconda3\python.exe", r"C:\ProgramData\Anaconda3\python.exe"):
        if Path(cand).is_file():
            return cand
    return sys.executable


# 读取策略参数元数据的脚本：借 quant_app 的解释器跑，JSON 写到 argv[2]（写文件比
# 解析 stdout 稳 —— Anaconda 启动时可能往 stdout 混入告警行）。
_META_SCRIPT = '''# -*- coding: utf-8 -*-
import json
import sys

sys.path.insert(0, sys.argv[1])
from strategies import STRATEGIES  # noqa: E402

out = {}
for sid, cls in STRATEGIES.items():
    out[sid] = {
        "name": getattr(cls, "name", sid),
        "description": getattr(cls, "description", ""),
        "param_meta": [
            {
                "key": m.key, "label": m.label, "default": m.default,
                "kind": m.kind, "min": m.min, "max": m.max, "step": m.step,
                "options": list(m.options) if m.options else None,
                "help": m.help, "group": m.group,
            }
            for m in getattr(cls, "param_meta", [])
        ],
    }
with open(sys.argv[2], "w", encoding="utf-8") as handle:
    json.dump(out, handle, ensure_ascii=False)
'''

_STRATEGY_META_CACHE: dict[str, Any] | None = None
_STRATEGY_META_DONE = False
_STRATEGY_META_LOCK = threading.Lock()


def strategy_meta(engine_root: Path) -> dict[str, Any] | None:
    """读取 quant_app 各策略的 param_meta（含默认值/min/max/分组），进程内缓存。

    失败后不再重试（避免每次 /strategies 请求都起子进程）；重启进程才重读。
    失败时返回 None，list_strategies 退回静态表 —— 页面仍可用，只是没有参数表单。
    """
    global _STRATEGY_META_CACHE, _STRATEGY_META_DONE
    with _STRATEGY_META_LOCK:
        if _STRATEGY_META_DONE:
            return _STRATEGY_META_CACHE
        _STRATEGY_META_DONE = True
        _STRATEGY_META_CACHE = _load_strategy_meta(engine_root)
        return _STRATEGY_META_CACHE


def _load_strategy_meta(engine_root: Path) -> dict[str, Any] | None:
    import tempfile

    try:
        with tempfile.TemporaryDirectory(prefix="m2_meta_") as tmp:
            script = Path(tmp) / "dump_meta.py"
            out = Path(tmp) / "meta.json"
            script.write_text(_META_SCRIPT, encoding="utf-8")
            proc = subprocess.run(
                [_quant_python(), str(script), str(engine_root), str(out)],
                capture_output=True, text=True, encoding="utf-8", errors="replace",
                timeout=120,
            )
            if proc.returncode != 0 or not out.is_file():
                return None
            data = json.loads(out.read_text(encoding="utf-8"))
            return data if isinstance(data, dict) and data else None
    except Exception:  # noqa: BLE001  元数据读取失败不应影响回测主链路
        return None


class BacktestRequest(BaseModel):
    strategy_id: str
    symbol: str = Field(pattern=r"^\d{6}(?:\.(?:SH|SZ|BJ))?$")
    start: datetime | None = None
    end: datetime | None = None
    init_cash: float = Field(default=1_000_000.0, gt=0)
    params: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def validate_window(self):
        if self.start and self.end and self.start > self.end:
            raise ValueError("start must not be after end")
        if self.start and self.end and (self.end - self.start).days > 731:
            raise ValueError("date range exceeds 2 years")
        if len(json.dumps(self.params, ensure_ascii=False).encode("utf-8")) > 32 * 1024:
            raise ValueError("params exceed 32KB")
        return self


@dataclass
class RunRecord:
    run_id: str
    status: str = "pending"
    progress: int = 0
    message: str = "queued"
    created_at: str = field(default_factory=lambda: datetime.now(UTC).isoformat())
    started_at: str | None = None
    finished_at: str | None = None
    request: dict[str, Any] = field(default_factory=dict)
    result: dict[str, Any] | None = None
    error_code: str | None = None
    artifacts: list[str] = field(default_factory=list)
    worker_pid: int | None = None
    events: list[dict[str, Any]] = field(default_factory=list)
    next_event_id: int = 1


class M2BacktestService:
    def __init__(self, engine_root: Path = _ENGINE_ROOT, data_root: Path = _DATA_ROOT,
                 state_root: Path = _STATE_ROOT, timeout_seconds: float = 600.0,
                 max_concurrent: int = 1, max_queue: int = 3,
                 worker_python: Path | None = None, retention_days: int = 30,
                 max_total_bytes: int = 5 * 1024 * 1024 * 1024):
        self.engine_root = engine_root
        self.data_root = data_root
        self.state_root = state_root
        self.timeout_seconds = timeout_seconds
        self.max_concurrent = max_concurrent
        self.max_queue = max_queue
        self.worker_python = worker_python or Path(os.getenv("M2_WORKER_PYTHON", str(_PYTHON)))
        self.retention_days = retention_days
        self.max_total_bytes = max_total_bytes
        self.state_root.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._runs: dict[str, RunRecord] = {}
        self._processes: dict[str, subprocess.Popen] = {}
        self._slots = threading.BoundedSemaphore(max_concurrent)
        self._restore()
        self.cleanup()

    def list_strategies(self) -> list[dict[str, Any]]:
        # 元数据来自 quant_app 真实注册表（含 ParamMeta），读到就以它为准；
        # 读不到退回静态表（无 param_meta，前端不渲染参数表单但其余功能正常）。
        meta = strategy_meta(self.engine_root)
        items: list[dict[str, Any]] = []
        for key, value in _STRATEGIES.items():
            entry = dict(meta[key]) if meta and key in meta else dict(value)
            items.append({"id": key, **entry})
        return items

    def create(self, request: BacktestRequest) -> dict:
        if request.strategy_id not in _STRATEGIES:
            raise ValueError("STRATEGY_NOT_FOUND")
        with self._lock:
            active = sum(item.status in {"pending", "running"} for item in self._runs.values())
            if active >= self.max_concurrent + self.max_queue:
                raise RuntimeError("QUEUE_FULL")
            run_id = uuid.uuid4().hex
            record = RunRecord(run_id=run_id, request=request.model_dump(mode="json"))
            self._runs[run_id] = record
            self._event_locked(record, "queued", 0, "queued")
            self._save_locked(record)
        threading.Thread(target=self._supervise, args=(run_id,), daemon=True).start()
        return self.get(run_id)

    def get(self, run_id: str) -> dict:
        with self._lock:
            record = self._runs.get(run_id)
            if record is None:
                raise KeyError(run_id)
            return json.loads(json.dumps(asdict(record)))

    def cancel(self, run_id: str) -> dict:
        with self._lock:
            record = self._runs.get(run_id)
            if record is None:
                raise KeyError(run_id)
            if record.status in _TERMINAL:
                return self.get(run_id)
            process = self._processes.get(run_id)
            if process is not None and process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=3)
            record.status = "cancelled"
            record.message = "cancelled"
            record.finished_at = datetime.now(UTC).isoformat()
            self._event_locked(record, "cancelled", record.progress, "cancelled")
            self._save_locked(record)
            return self.get(run_id)

    def _supervise(self, run_id: str) -> None:
        record = self._runs[run_id]
        acquired = self._slots.acquire(timeout=self.timeout_seconds)
        if not acquired:
            self._finish_error(record, "TIMEOUT", "queue wait timeout", "timeout")
            return
        try:
            with self._lock:
                if record.status == "cancelled":
                    return
                record.status = "running"
                record.progress = 10
                record.message = "worker starting"
                record.started_at = datetime.now(UTC).isoformat()
                self._event_locked(record, "running", 10, record.message)
                run_dir = self.state_root / run_id
                request_file = run_dir / "request.json"
                request_payload = {**record.request, "run_id": run_id,
                                   "engine_root": str(self.engine_root), "data_root": str(self.data_root)}
                self._atomic_json(request_file, request_payload)
                env = os.environ.copy()
                env["AMAZINGDATA_ALLOW_SDK"] = "0"
                process = subprocess.Popen(
                    [str(self.worker_python), str(_WORKER), "--request", str(request_file), "--output", str(run_dir)],
                    env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                )
                self._processes[run_id] = process
                record.worker_pid = process.pid
                self._save_locked(record)
            try:
                return_code = process.wait(timeout=self.timeout_seconds)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)
                self._finish_error(record, "TIMEOUT", "backtest timeout", "timeout")
                return
            with self._lock:
                if record.status == "cancelled":
                    return
            result_path = self.state_root / run_id / "worker_result.json"
            payload = json.loads(result_path.read_text(encoding="utf-8")) if result_path.is_file() else {}
            if return_code == 0 and payload.get("status") == "completed":
                with self._lock:
                    record.status = "completed"
                    record.progress = 100
                    record.message = "completed"
                    record.result = payload.get("summary")
                    record.artifacts = payload.get("artifacts", [])
                    record.finished_at = datetime.now(UTC).isoformat()
                    self._event_locked(record, "completed", 100, "completed")
                    self._save_locked(record)
            else:
                code = payload.get("error_code", "WORKER_FAILED")
                message = payload.get("message") or "backtest worker failed"
                # worker_result.json 里的 message 可能是无信息量的兜底句
                # （旧版 worker 写死 "backtest worker failed"）。
                # 这种情况从 worker_error.log 的 traceback 末行取真实异常。
                if code == "WORKER_FAILED" and (
                        "work failed" in message or not message.strip()):
                    tail = self._worker_error_tail(run_id)
                    if tail:
                        message = tail
                self._finish_error(record, code, message, "failed")
        finally:
            with self._lock:
                self._processes.pop(run_id, None)
            self._slots.release()

    def _worker_error_tail(self, run_id: str, max_chars: int = 400) -> str:
        """从 `worker_error.log` 取 traceback 的最后一行 —— 那就是真正的异常原因。

        取末行而不是全文：全文几十行 Python 调用栈对用户没用，
        `ValueError: 初始资金不足：...` 这一行才是有用的。
        """
        path = self.state_root / run_id / "worker_error.log"
        if not path.is_file():
            return ""
        try:
            lines = [ln.strip() for ln in
                     path.read_text(encoding="utf-8", errors="replace").splitlines()
                     if ln.strip()]
        except Exception:  # noqa: BLE001
            return ""
        return lines[-1][:max_chars] if lines else ""

    def _finish_error(self, record: RunRecord, code: str, message: str, status: str) -> None:
        with self._lock:
            record.status = status
            record.error_code = code
            record.message = message
            record.finished_at = datetime.now(UTC).isoformat()
            self._event_locked(record, status, record.progress, message)
            self._save_locked(record)

    def _event_locked(self, record: RunRecord, event: str, progress: int, message: str) -> None:
        item = {"id": record.next_event_id, "event": event, "progress": progress,
                "message": message, "timestamp": datetime.now(UTC).isoformat()}
        record.events.append(item)
        record.next_event_id += 1
        record.events = record.events[-1000:]
        self._append_event_locked(record.run_id, item)

    def _save_locked(self, record: RunRecord) -> None:
        self._atomic_json(self.state_root / record.run_id / "state.json", asdict(record))

    def _append_event_locked(self, run_id: str, event: dict[str, Any]) -> None:
        path = self.state_root / run_id / "events.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(event, ensure_ascii=False) + "\n")
            handle.flush()
            os.fsync(handle.fileno())

    @staticmethod
    def _atomic_json(path: Path, payload: dict) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        temp = path.with_suffix(path.suffix + ".tmp")
        temp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(temp, path)

    def artifact_names(self, run_id: str) -> list[str]:
        snapshot = self.get(run_id)
        return list(snapshot["artifacts"])

    def artifact_path(self, run_id: str, name: str, max_bytes: int = 100 * 1024 * 1024) -> Path:
        if Path(name).name != name or name not in self.artifact_names(run_id):
            raise ValueError("ARTIFACT_NOT_ALLOWED")
        root = (self.state_root / run_id).resolve()
        path = (root / name).resolve()
        if path.parent != root or not path.is_file():
            raise FileNotFoundError(name)
        if path.stat().st_size > max_bytes:
            raise ValueError("ARTIFACT_TOO_LARGE")
        return path

    def cleanup(self) -> list[str]:
        now = datetime.now(UTC)
        removed: list[str] = []
        with self._lock:
            terminal = [record for record in self._runs.values() if record.status in _TERMINAL]
            terminal.sort(key=lambda item: item.finished_at or item.created_at)
            for record in list(terminal):
                stamp = datetime.fromisoformat(record.finished_at or record.created_at)
                if (now - stamp).days >= self.retention_days:
                    shutil.rmtree(self.state_root / record.run_id, ignore_errors=True)
                    self._runs.pop(record.run_id, None)
                    removed.append(record.run_id)
            total = sum(path.stat().st_size for path in self.state_root.rglob("*") if path.is_file())
            for record in terminal:
                if total <= self.max_total_bytes or record.run_id in removed:
                    continue
                folder = self.state_root / record.run_id
                size = sum(path.stat().st_size for path in folder.rglob("*") if path.is_file())
                shutil.rmtree(folder, ignore_errors=True)
                self._runs.pop(record.run_id, None)
                removed.append(record.run_id)
                total -= size
        return removed

    def _restore(self) -> None:
        for path in self.state_root.glob("*/state.json"):
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
                record = RunRecord(**payload)
                if record.status in {"pending", "running"}:
                    record.status = "interrupted"
                    record.error_code = "INTERRUPTED"
                    record.message = "service restarted"
                    record.finished_at = datetime.now(UTC).isoformat()
                    self._event_locked(record, "interrupted", record.progress, record.message)
                    self._save_locked(record)
                self._runs[record.run_id] = record
            except Exception:
                continue


SERVICE = M2BacktestService()
router = APIRouter(prefix="/api/custom/m2", tags=["custom-m2"])


@router.get("/health")
def health() -> dict:
    return {"status": "ok", "mode": "backtest-only", "process_isolation": True,
            "amazingdata_sdk_allowed": False, "engine_available": SERVICE.engine_root.is_dir(),
            "data_available": SERVICE.data_root.is_dir(), "worker_available": _WORKER.is_file(),
            "strategy_meta_loaded": strategy_meta(SERVICE.engine_root) is not None}


@router.get("/backtests/{run_id}/artifacts")
def list_artifacts(run_id: str) -> dict:
    try:
        return {"run_id": run_id, "artifacts": SERVICE.artifact_names(run_id)}
    except KeyError as exc:
        raise HTTPException(status_code=404, detail={"code": "RUN_NOT_FOUND"}) from exc


@router.get("/backtests/{run_id}/artifacts/{name}")
def download_artifact(run_id: str, name: str) -> FileResponse:
    try:
        path = SERVICE.artifact_path(run_id, name)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail={"code": "RUN_NOT_FOUND"}) from exc
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail={"code": "ARTIFACT_NOT_FOUND"}) from exc
    except ValueError as exc:
        code = str(exc)
        status = 413 if code == "ARTIFACT_TOO_LARGE" else 403
        raise HTTPException(status_code=status, detail={"code": code}) from exc
    media_type = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
    return FileResponse(path, media_type=media_type, filename=path.name)


@router.get("/strategies")
def strategies() -> list[dict[str, Any]]:
    return SERVICE.list_strategies()


@router.post("/backtests", status_code=202)
def create_backtest(request: BacktestRequest) -> dict:
    try:
        return SERVICE.create(request)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail={"code": str(exc)}) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=429, detail={"code": str(exc)}) from exc


@router.get("/backtests/{run_id}")
def get_backtest(run_id: str) -> dict:
    try:
        return SERVICE.get(run_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail={"code": "RUN_NOT_FOUND"}) from exc


@router.post("/backtests/{run_id}/cancel")
def cancel_backtest(run_id: str) -> dict:
    try:
        return SERVICE.cancel(run_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail={"code": "RUN_NOT_FOUND"}) from exc


@router.get("/backtests/{run_id}/events")
async def backtest_events(request: Request, run_id: str, after_id: int = Query(default=0, ge=0)) -> StreamingResponse:
    try:
        SERVICE.get(run_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail={"code": "RUN_NOT_FOUND"}) from exc
    header_id = request.headers.get("last-event-id")
    cursor = max(after_id, int(header_id) if header_id and header_id.isdigit() else 0)

    async def stream():
        nonlocal cursor
        last_heartbeat = time.monotonic()
        while True:
            if await request.is_disconnected():
                break
            snapshot = SERVICE.get(run_id)
            pending = [event for event in snapshot["events"] if event["id"] > cursor]
            for event in pending:
                cursor = event["id"]
                yield f"id: {event['id']}\nevent: {event['event']}\ndata: {json.dumps(event, ensure_ascii=False)}\n\n"
            if snapshot["status"] in _TERMINAL:
                break
            if time.monotonic() - last_heartbeat >= 15:
                yield ": heartbeat\n\n"
                last_heartbeat = time.monotonic()
            await asyncio.sleep(0.1)

    return StreamingResponse(stream(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


def setup(registrar: BackendExtensionRegistrar) -> None:
    registrar.include_router(router)
