"""大模型设置：本地 / 云端 模式的解析、合并、持久化。"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from kanma.llm_settings import (  # noqa: E402
    LOCAL_PRESETS,
    LLMMode,
    LLMSettings,
    ModelProfile,
    default_settings,
    load_settings,
    reset_settings,
    save_settings,
)


@pytest.fixture(autouse=True)
def _reset():
    """每条测试前后清空单例，避免污染。"""
    reset_settings()
    yield
    reset_settings()


def test_default_settings_has_two_independent_profiles():
    s = default_settings()
    assert isinstance(s, LLMSettings)
    assert isinstance(s.local, ModelProfile)
    assert isinstance(s.cloud, ModelProfile)
    assert s.mode in (LLMMode.LOCAL, LLMMode.CLOUD)


def test_resolve_unconfigured_is_offline():
    s = LLMSettings(mode=LLMMode.LOCAL, enabled=True, local=ModelProfile(model="", base_url=""))
    res = s.resolve()
    assert res.model == ""
    assert res.api_key_set is False


def test_resolve_disabled_is_offline_even_with_model():
    s = LLMSettings(
        mode=LLMMode.LOCAL,
        enabled=False,
        local=ModelProfile(model="qwen2.5:7b", base_url="http://x"),
    )
    res = s.resolve()
    assert res.model == ""
    assert res.mode is LLMMode.LOCAL


def test_resolve_keeps_api_key_literal():
    s = LLMSettings(
        mode=LLMMode.CLOUD,
        cloud=ModelProfile(model="gpt-4o", base_url="https://api.openai.com/v1", api_key="sk-abc"),
    )
    res = s.resolve()
    assert res.model == "gpt-4o"
    assert res.base_url == "https://api.openai.com/v1"
    assert res.api_key == "sk-abc"
    assert res.api_key_set is True


def test_resolve_local_keeps_empty_api_key():
    """本地模式 api_key 为空是合法的（服务不校验）。"""
    s = LLMSettings(
        mode=LLMMode.LOCAL,
        local=ModelProfile(model="qwen2.5:7b", base_url="http://127.0.0.1:11434/v1"),
    )
    res = s.resolve()
    assert res.api_key == ""
    assert res.api_key_set is False


def test_masked_does_not_leak_secret():
    p = ModelProfile(model="m", base_url="http://x", api_key="sk-abcdefghijklmnop")
    m = p.masked()
    assert m["api_key"] == ""
    assert m["api_key_set"] is True
    assert "****" in m["api_key_hint"]


def test_with_incoming_preserves_api_key_when_empty():
    s = LLMSettings(
        mode=LLMMode.LOCAL,
        local=ModelProfile(model="qwen", base_url="http://x", api_key="old-secret"),
    )
    out = s.with_incoming({"mode": "local", "local": {"model": "llama"}})
    assert out.local.model == "llama"
    assert out.local.api_key == "old-secret"
    assert out.local.base_url == "http://x"


def test_with_incoming_overrides_api_key_when_value():
    s = LLMSettings(
        mode=LLMMode.LOCAL,
        local=ModelProfile(model="qwen", base_url="http://x", api_key="old"),
    )
    out = s.with_incoming({"local": {"api_key": "new-secret"}})
    assert out.local.api_key == "new-secret"


def test_with_incoming_clears_api_key_with_sentinel():
    s = LLMSettings(
        mode=LLMMode.LOCAL,
        local=ModelProfile(model="qwen", base_url="http://x", api_key="old"),
    )
    out = s.with_incoming({"local": {"api_key": "__CLEAR__"}})
    assert out.local.api_key == ""


def test_with_incoming_switches_mode_independently():
    s = LLMSettings(
        mode=LLMMode.CLOUD,
        cloud=ModelProfile(model="gpt-4o", base_url="https://x", api_key="sk-1"),
        local=ModelProfile(model="qwen", base_url="http://y", api_key=""),
    )
    out = s.with_incoming({"mode": "local", "local": {"model": "llama3"}})
    assert out.mode is LLMMode.LOCAL
    assert out.cloud.model == "gpt-4o"
    assert out.cloud.api_key == "sk-1"
    assert out.local.model == "llama3"
    assert out.local.base_url == "http://y"


def test_to_api_carries_both_profiles_and_active_state():
    s = LLMSettings(
        mode=LLMMode.LOCAL,
        local=ModelProfile(model="qwen2.5:7b", base_url="http://127.0.0.1:11434/v1", api_key="k"),
    )
    api = s.to_api()
    assert api["mode"] == "local"
    assert api["local"]["model"] == "qwen2.5:7b"
    assert api["local"]["api_key"] == ""  # 永远脱敏
    assert api["active"]["ready"] is True
    assert api["active"]["model"] == "qwen2.5:7b"
    assert "本地模式" in api["active"]["label"]
    assert len(api["local_presets"]) >= 5


def test_save_load_roundtrip(tmp_path: Path):
    s = LLMSettings(
        mode=LLMMode.CLOUD,
        cloud=ModelProfile(
            model="gpt-4o", base_url="https://api.openai.com/v1", api_key="sk-x"
        ),
    )
    p = save_settings(s, tmp_path)
    assert p.exists()
    loaded = load_settings(tmp_path)
    assert loaded.mode is LLMMode.CLOUD
    assert loaded.cloud.model == "gpt-4o"
    assert loaded.cloud.api_key == "sk-x"
    data = json.loads(p.read_text(encoding="utf-8"))
    assert data["mode"] == "cloud"
    assert data["cloud"]["model"] == "gpt-4o"


def test_load_returns_default_when_file_missing(tmp_path: Path):
    s = load_settings(tmp_path)
    assert s.mode in (LLMMode.LOCAL, LLMMode.CLOUD)


def test_local_presets_cover_common_servers():
    names = {p["name"] for p in LOCAL_PRESETS}
    assert {"Ollama", "LM Studio", "vLLM / SGLang", "Xinference", "LocalAI"} <= names
    for p in LOCAL_PRESETS:
        assert p["base_url"].startswith("http://")
        assert p["base_url"].endswith("/v1")
