"""AmazingData单进程运行约束。"""
from __future__ import annotations

import os
from pathlib import Path


def validate_single_process_runtime() -> None:
    """拒绝会造成AmazingData重复登录的多worker或生产热重载配置。"""
    workers = os.getenv("WEB_CONCURRENCY", "1").strip()
    if workers.isdigit() and int(workers) > 1:
        raise RuntimeError("AmazingData只允许单worker主服务访问; WEB_CONCURRENCY必须为1")
    if os.getenv("AMAZINGDATA_ALLOW_SDK", "1").strip().lower() not in {"1", "true", "yes"}:
        raise RuntimeError("当前进程禁止访问AmazingData SDK; 回测worker只能读取落盘数据")


def assert_main_process_marker(data_dir: str | Path) -> Path:
    """创建主服务标记文件, 供worker启动配置和运维检查使用。"""
    marker = Path(data_dir) / ".amazingdata-main-process"
    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.write_text(str(os.getpid()), encoding="ascii")
    return marker
