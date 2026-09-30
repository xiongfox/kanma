"""勘码系统级设置：全局开关，对所有评审项目生效。

与「项目配置（ProjectConfig，每个评审项目一份）」不同，系统设置是**全局**的，
描述「这次部署环境下评审怎么跑」，例如步骤 2 的并发度阈值——它取决于本机模型的
承载能力（本地模型压不住时要限流），与具体评审哪个项目无关，故放在系统层而非项目层。

持久化在 `{store_dir}/system.json`；同时支持环境变量预置（无页面 / CLI 场景）。

环境变量（仅在对应字段为空时作为默认值）：
- KANMA_STATIC_REVIEW_MAX_PARALLEL   步骤 2 静态审查并发度上限（0 = 不限制；不设则取系统默认 2 路）
"""

from __future__ import annotations

import json
import os
import threading
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field


class SystemSettings(BaseModel):
    """系统级设置整体（全局生效，对所有评审项目共享）。"""

    # 步骤 2 并发度阈值：限制**同时打给模型的章节审查请求数**。
    # 默认 2 路（本地模型压得住、云端模型也够快）；0 = 不限制（强模型 / 网关场景可放开）。
    static_review_max_parallel: int = Field(
        default=2,
        ge=0,
        description="步骤2静态审查并发度上限：同时打给模型的章节审查请求数；0=不限制，默认2路",
    )

    @property
    def max_parallel(self) -> int:
        """规整后的非负整数（防御越界 / 脏数据）。"""
        return max(0, int(self.static_review_max_parallel))

    def to_api(self) -> dict[str, Any]:
        """返回给页面的视图（系统设置不含密钥，无需脱敏）。"""
        return {"static_review_max_parallel": self.max_parallel}


def _env_default() -> int:
    """环境变量默认值：KANMA_STATIC_REVIEW_MAX_PARALLEL。

    不设 → 系统默认 2 路并发；设为 0 表示显式不限制；非法值回退到 2。
    """
    raw = os.environ.get("KANMA_STATIC_REVIEW_MAX_PARALLEL", "").strip()
    if not raw:
        return 2
    try:
        return max(0, int(raw))
    except (TypeError, ValueError):
        return 2


def default_settings() -> SystemSettings:
    """从环境变量构造默认设置（无磁盘文件时的回退）。"""
    return SystemSettings(static_review_max_parallel=_env_default())


# ---------------------------------------------------------------------------
# 持久化与进程内单例
# ---------------------------------------------------------------------------

_CURRENT: SystemSettings | None = None
_LOCK = threading.Lock()


def get_settings() -> SystemSettings:
    """进程内当前生效的设置（未显式配置时取环境变量默认值）。"""
    global _CURRENT
    with _LOCK:
        if _CURRENT is None:
            _CURRENT = default_settings()
        return _CURRENT


def configure(settings: SystemSettings) -> SystemSettings:
    """设置进程内生效的配置（不落盘）。"""
    global _CURRENT
    with _LOCK:
        _CURRENT = settings
        return _CURRENT


def reset_settings() -> None:
    """恢复为环境变量默认值（主要用于测试）。"""
    global _CURRENT
    with _LOCK:
        _CURRENT = None


def settings_path(store_dir: str | Path) -> Path:
    return Path(store_dir) / "system.json"


def load_settings(store_dir: str | Path) -> SystemSettings:
    """从项目目录读取设置；文件不存在时回退到环境变量默认值。"""
    p = settings_path(store_dir)
    if p.is_file():
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
            return configure(SystemSettings.model_validate(data))
        except Exception:  # 损坏文件不阻断启动，回退默认值
            return configure(default_settings())
    return configure(default_settings())


def save_settings(settings: SystemSettings, store_dir: str | Path) -> Path:
    """落盘为 JSON 并置为进程内生效。"""
    p = settings_path(store_dir)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(
        json.dumps(settings.model_dump(mode="json"), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    configure(settings)
    return p
