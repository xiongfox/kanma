"""大模型接入设置：本地模式 / 云端模式。

勘码只走 **OpenAI 兼容协议** 一种通道，两种模式只是连的地址不同：

- **本地模式（local）**：连内网/本机的自部署推理服务（Ollama、vLLM、LM Studio、
  Xinference、LocalAI …），通常不需要 API Key，数据不出内网。
- **云端模式（cloud）**：连云端或企业网关的模型 API（OpenAI、DeepSeek、通义、
  自建网关 …），需要 API Key。

设置可在页面上手工切换，切换后对新发起的评审立即生效；
也支持环境变量预置，便于 CI / 命令行场景。

环境变量（仅在对应字段为空时作为默认值）：
- KANMA_LLM_MODE                 local | cloud | off
- KANMA_MODEL / KANMA_API_KEY / KANMA_BASE_URL          云端
- KANMA_LOCAL_MODEL / KANMA_LOCAL_API_KEY / KANMA_LOCAL_BASE_URL   本地
"""

from __future__ import annotations

import json
import os
import threading
from concurrent.futures import ThreadPoolExecutor
from enum import Enum
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

class LLMMode(str, Enum):
    """大模型接入模式。"""

    LOCAL = "local"
    CLOUD = "cloud"


MODE_LABEL = {
    LLMMode.LOCAL: "本地模式",
    LLMMode.CLOUD: "云端模式",
}

# 本地推理服务的常见 OpenAI 兼容端点，用于页面"一键探测"
LOCAL_PRESETS: list[dict[str, str]] = [
    {"name": "Ollama", "base_url": "http://127.0.0.1:11434/v1", "model": "qwen2.5-coder:7b"},
    {"name": "LM Studio", "base_url": "http://127.0.0.1:1234/v1", "model": "local-model"},
    {"name": "vLLM / SGLang", "base_url": "http://127.0.0.1:8000/v1", "model": "Qwen/Qwen2.5-Coder-7B-Instruct"},
    {"name": "Xinference", "base_url": "http://127.0.0.1:9997/v1", "model": "qwen2.5-coder"},
    {"name": "LocalAI", "base_url": "http://127.0.0.1:8080/v1", "model": "ggml-model"},
]


class ModelProfile(BaseModel):
    """一个模式的连接参数。"""

    model: str = Field(default="", description="模型名，如 qwen2.5-coder:7b / gpt-4o")
    base_url: str = Field(default="", description="OpenAI 兼容服务地址，如 http://127.0.0.1:11434/v1")
    api_key: str = Field(default="", description="API Key（本地服务通常留空）")
    temperature: float = Field(default=0.0, ge=0.0, le=2.0)
    timeout: int = Field(default=120, ge=5, le=3600, description="单次调用超时秒数")

    @property
    def configured(self) -> bool:
        return bool(self.model.strip())

    def masked(self) -> dict[str, Any]:
        """回显给页面：密钥脱敏，避免整页返回明文。"""
        return {
            "model": self.model,
            "base_url": self.base_url,
            "api_key": "",
            "api_key_set": bool(self.api_key),
            "api_key_hint": _mask_key(self.api_key),
            "temperature": self.temperature,
            "timeout": self.timeout,
            "configured": self.configured,
        }


class ResolvedLLM(BaseModel):
    """解析后的最终连接参数。"""

    mode: LLMMode
    model: str
    base_url: str = ""
    api_key: str = ""
    temperature: float = 0.0
    timeout: int = 120
    api_key_set: bool = Field(default=False, description="是否配置了真实密钥")

    @property
    def mode_label(self) -> str:
        return MODE_LABEL[self.mode]

    @property
    def endpoint(self) -> str:
        """展示用的连接目标：地址优先，缺省时给模型名。"""
        return self.base_url or self.model

    @property
    def label(self) -> str:
        """展示用完整描述。"""
        if not self.model:
            return f"离线模式（{self.mode_label}未配置模型）"
        text = f"{self.model}（{self.mode_label}"
        if not self.api_key_set:
            text += "，无密钥"
        return text + "）"


class LLMSettings(BaseModel):
    """模型设置整体：当前模式 + 两套独立保存的连接参数。"""

    mode: LLMMode = LLMMode.CLOUD
    enabled: bool = Field(default=True, description="关闭后跳过 LLM 语义审查（离线模式）")
    local: ModelProfile = Field(default_factory=ModelProfile)
    cloud: ModelProfile = Field(default_factory=ModelProfile)

    def profile_of(self, mode: LLMMode) -> ModelProfile:
        return self.local if mode is LLMMode.LOCAL else self.cloud

    @property
    def active(self) -> ModelProfile:
        return self.profile_of(self.mode)

    def resolve(self) -> ResolvedLLM:
        """按当前模式解析出实际连接参数；未启用时返回空模型名（走离线）。"""
        if not self.enabled:
            return ResolvedLLM(mode=self.mode, model="")
        p = self.active
        if not p.configured:
            return ResolvedLLM(mode=self.mode, model="")
        return ResolvedLLM(
            mode=self.mode,
            model=p.model.strip(),
            base_url=(p.base_url or "").strip(),
            api_key=p.api_key,
            api_key_set=bool(p.api_key),
            temperature=p.temperature,
            timeout=p.timeout,
        )

    def to_api(self) -> dict[str, Any]:
        """返回给页面的完整视图（密钥脱敏）。"""
        res = self.resolve()
        return {
            "mode": self.mode.value,
            "mode_label": self.mode_label,
            "enabled": self.enabled,
            "local": self.local.masked(),
            "cloud": self.cloud.masked(),
            "active": {
                "mode": res.mode.value,
                "mode_label": res.mode_label,
                "model": res.model,
                "base_url": res.base_url,
                "endpoint": res.endpoint,
                "temperature": res.temperature,
                "timeout": res.timeout,
                "ready": bool(res.model),
                "label": res.label,
            },
            "local_presets": LOCAL_PRESETS,
        }

    @property
    def mode_label(self) -> str:
        return MODE_LABEL[self.mode]

    def with_incoming(self, payload: dict[str, Any]) -> "LLMSettings":
        """用页面的提交合并出一份新设置。

        约定：api_key 传空字符串表示"不修改"，保留已保存的密钥；
        需要清空时前端会传 `__CLEAR__`。
        """
        data = self.model_dump()
        if "mode" in payload and payload["mode"]:
            m = str(payload["mode"]).lower()
            if m == "off":
                data["enabled"] = False
            else:
                data["mode"] = m
        if "enabled" in payload and payload["enabled"] is not None:
            data["enabled"] = bool(payload["enabled"])
        for key in ("local", "cloud"):
            incoming = payload.get(key)
            if not isinstance(incoming, dict):
                continue
            cur = data[key]
            for field in ("model", "base_url", "api_key", "temperature", "timeout"):
                if field not in incoming or incoming[field] is None:
                    continue
                value = incoming[field]
                if field == "api_key":
                    if value == "__CLEAR__":
                        cur["api_key"] = ""
                    elif value == "":
                        cur["api_key"] = self.profile_of(LLMMode(key)).api_key
                    else:
                        cur["api_key"] = str(value)
                    continue
                if field in ("temperature",):
                    cur[field] = float(value)
                elif field == "timeout":
                    cur[field] = int(value)
                else:
                    cur[field] = str(value).strip()
        return LLMSettings.model_validate(data)


def _mask_key(key: str) -> str:
    if not key:
        return ""
    if len(key) <= 8:
        return f"{key[:2]}****"
    return f"{key[:4]}****{key[-4:]}"


# ---------------------------------------------------------------------------
# 默认值：环境变量优先，便于无页面的命令行 / CI 场景
# ---------------------------------------------------------------------------


def _env_profile(
    model_key: str, key_key: str, base_key: str, default_model: str = "", default_base: str = ""
) -> ModelProfile:
    return ModelProfile(
        model=os.getenv(model_key, default_model) or default_model,
        base_url=os.getenv(base_key, default_base) or default_base,
        api_key=os.getenv(key_key, "") or "",
    )


def default_settings() -> LLMSettings:
    """从环境变量构造默认设置。"""
    mode_raw = (os.getenv("KANMA_LLM_MODE", "") or "").strip().lower()
    if mode_raw in ("local", "cloud"):
        mode = LLMMode(mode_raw)
    else:
        # 未显式指定时：配了云端模型就走云端，否则若配了本地模型走本地
        mode = LLMMode.CLOUD
        if os.getenv("KANMA_LOCAL_MODEL") and not os.getenv("KANMA_MODEL"):
            mode = LLMMode.LOCAL
    return LLMSettings(
        mode=mode,
        enabled=mode_raw != "off",
        local=_env_profile(
            "KANMA_LOCAL_MODEL",
            "KANMA_LOCAL_API_KEY",
            "KANMA_LOCAL_BASE_URL",
            default_model="qwen2.5-coder:7b",
            default_base="http://127.0.0.1:11434/v1",
        ),
        cloud=_env_profile("KANMA_MODEL", "KANMA_API_KEY", "KANMA_BASE_URL"),
    )


# ---------------------------------------------------------------------------
# 持久化与进程内单例
# ---------------------------------------------------------------------------

_CURRENT: LLMSettings | None = None
_LOCK = threading.Lock()


def get_settings() -> LLMSettings:
    """进程内当前生效的设置（未显式配置时取环境变量默认值）。"""
    global _CURRENT
    with _LOCK:
        if _CURRENT is None:
            _CURRENT = default_settings()
        return _CURRENT


def configure(settings: LLMSettings) -> LLMSettings:
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
    return Path(store_dir) / "llm.json"


def load_settings(store_dir: str | Path) -> LLMSettings:
    """从项目目录读取设置；文件不存在时回退到环境变量默认值。"""
    p = settings_path(store_dir)
    if p.is_file():
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
            s = LLMSettings.model_validate(data)
            return configure(s)
        except Exception:  # 损坏文件不阻断启动，回退默认值
            return configure(default_settings())
    return configure(default_settings())


def save_settings(settings: LLMSettings, store_dir: str | Path) -> Path:
    """落盘为 JSON（含密钥，权限收窄为 600）并置为进程内生效。"""
    p = settings_path(store_dir)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(
        json.dumps(settings.model_dump(mode="json"), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    try:
        p.chmod(0o600)
    except OSError:  # pragma: no cover - 部分文件系统不支持
        pass
    configure(settings)
    return p


# ---------------------------------------------------------------------------
# 本地推理服务探测
# ---------------------------------------------------------------------------


def _probe_one(base_url: str, timeout: float = 1.5) -> dict[str, Any]:
    """探测一个 OpenAI 兼容端点的 /v1/models。"""
    import httpx

    url = base_url.rstrip("/") + "/models"
    try:
        resp = httpx.get(url, timeout=timeout)
        if resp.status_code >= 400:
            return {"base_url": base_url, "ok": False, "error": f"HTTP {resp.status_code}"}
        payload = resp.json()
        models: list[str] = []
        data = payload.get("data") if isinstance(payload, dict) else None
        if isinstance(data, list):
            models = [str(m.get("id")) for m in data if isinstance(m, dict) and m.get("id")]
        return {"base_url": base_url, "ok": True, "models": models[:20]}
    except Exception as e:  # 端口未监听 / 超时
        return {"base_url": base_url, "ok": False, "error": type(e).__name__}


def probe_local_services(timeout: float = 1.5) -> list[dict[str, Any]]:
    """并发探测本地常见推理服务端口，返回可用的地址与模型列表。"""
    urls = [p["base_url"] for p in LOCAL_PRESETS]
    with ThreadPoolExecutor(max_workers=len(urls)) as pool:
        results = list(pool.map(lambda u: _probe_one(u, timeout), urls))
    by_url = {p["base_url"]: p for p in LOCAL_PRESETS}
    for r in results:
        preset = by_url.get(r["base_url"], {})
        r["name"] = preset.get("name", "")
        r["suggested_model"] = preset.get("model", "")
        if r["ok"] and not r.get("models") and preset.get("model"):
            r["models"] = [preset["model"]]
    return results
