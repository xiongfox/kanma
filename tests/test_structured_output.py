"""结构化输出通道的容错行为。

背景：不同 OpenAI 兼容服务支持的通道差异极大，且部分网关（vLLM 前置代理）
在冷却期会以 **404** 拒答结构化输出请求、十余秒后自行恢复。若写死单一通道
或失败即放弃，评审会在语义审查这一步整段降级（表现为 `[步骤2.3] 语义审查失败：404`）。

本文件锁定三条不变量：
1. 通道按优先级回退，首个成功的通道被缓存复用；
2. 可重试的暂时性失败会退避重试（404 仅在端点曾成功过时才算暂时性）；
3. 全通道失败时抛出带诊断信息的 StructuredOutputError，而不是裸 SDK 异常。
"""

from __future__ import annotations

import pytest

from kanma import llm as llm_mod
from kanma.llm import LLMContext, StructuredOutputError, invoke_structured


class _Resp404(Exception):
    """模拟网关 404 拒答。"""

    def __init__(self, status: int = 404) -> None:
        super().__init__(f"Error code: {status} - {{'detail': 'Not Found'}}")
        self.response = type("R", (), {"status_code": status})()


class FakeChat:
    """按 method 决定成功/失败的假 chat 模型。"""

    def __init__(self, ok_methods: set[str], fail_status: int = 404) -> None:
        self.ok_methods = ok_methods
        self.fail_status = fail_status
        self.calls: list[str] = []
        self.ok_calls = 0

    def with_structured_output(self, schema, method=None, **kwargs):
        outer = self

        class _R:
            def invoke(self, messages):
                outer.calls.append(method)
                if method in outer.ok_methods:
                    outer.ok_calls += 1
                    return {"method": method}
                raise _Resp404(outer.fail_status)

        return _R()


def _ctx(chat: FakeChat, base: str = "http://x/v1", model: str = "m") -> LLMContext:
    return LLMContext(available=True, model_name=model, chat=chat, base_url=base)


@pytest.fixture(autouse=True)
def _clean_cache(monkeypatch):
    llm_mod._STRUCTURED_CACHE.clear()
    monkeypatch.setattr(llm_mod.time, "sleep", lambda *_a, **_k: None)  # 退避不等真实时间
    yield
    llm_mod._STRUCTURED_CACHE.clear()


def test_fallback_to_next_channel_and_cache_it():
    """首选通道不可用时应回退到下一个，并记住成功通道。"""
    chat = FakeChat(ok_methods={"function_calling"})
    ctx = _ctx(chat)
    out = invoke_structured(ctx, dict, [])
    assert out == {"method": "function_calling"}
    assert llm_mod._STRUCTURED_CACHE[("m", "http://x/v1")] == "function_calling"
    # json_schema 先试且失败，不应重试（端点从未成功过，404 视为地址问题）
    assert chat.calls.count("json_schema") == 1


def test_cached_method_is_tried_first():
    """已验证过的通道必须排在最前，避免每次都走一遍失败通道。"""
    llm_mod._STRUCTURED_CACHE[("m", "http://x/v1")] = "json_mode"
    chat = FakeChat(ok_methods={"json_mode"})
    invoke_structured(ctx := _ctx(chat), dict, [])
    assert chat.calls[0] == "json_mode"
    assert len(chat.calls) == 1


def test_404_retried_with_backoff_once_endpoint_proven():
    """端点曾成功过 → 404 属暂时拒答，必须退避重试而不是直接换通道。"""
    llm_mod._STRUCTURED_CACHE[("m", "http://x/v1")] = "json_schema"
    chat = FakeChat(ok_methods=set())  # 全部失败
    with pytest.raises(StructuredOutputError):
        invoke_structured(_ctx(chat), dict, [])
    # 每通道 1 次 + 2 次重试 = 3 次；三个通道都试过
    assert chat.calls.count("json_schema") == 3
    assert len(chat.calls) == 9


def test_404_not_retried_when_endpoint_never_succeeded():
    """端点从未成功 → 404 更像地址/通道不匹配，快速失败换通道，不做无谓等待。"""
    chat = FakeChat(ok_methods=set())
    with pytest.raises(StructuredOutputError):
        invoke_structured(_ctx(chat), dict, [])
    assert len(chat.calls) == len(llm_mod.STRUCTURED_METHODS)


def test_retry_succeeds_on_second_attempt():
    """退避重试应当能救回「冷却期」导致的失败。"""
    llm_mod._STRUCTURED_CACHE[("m", "http://x/v1")] = "json_schema"
    state = {"n": 0}
    chat = FakeChat(ok_methods=set())

    orig = chat.with_structured_output

    def flaky(schema, method=None, **kw):
        runnable = orig(schema, method=method, **kw)
        call = runnable.invoke

        def _invoke(messages):
            state["n"] += 1
            if state["n"] >= 2:  # 第 2 次起放行
                return {"method": method, "recovered": True}
            return call(messages)

        runnable.invoke = _invoke
        return runnable

    chat.with_structured_output = flaky
    out = invoke_structured(_ctx(chat), dict, [])
    assert out["recovered"] is True


def test_all_channels_fail_raises_diagnostic_error():
    """全失败时必须给出端点与已试通道，便于用户自查而非看到裸 404。"""
    chat = FakeChat(ok_methods=set())
    with pytest.raises(StructuredOutputError) as ei:
        invoke_structured(_ctx(chat, base="http://gw:4000/v1"), dict, [])
    msg = str(ei.value)
    assert "http://gw:4000/v1" in msg
    for m in llm_mod.STRUCTURED_METHODS:
        assert m in msg


def test_unavailable_context_raises_immediately():
    ctx = LLMContext(available=False, model_name="", reason="未配置模型")
    with pytest.raises(RuntimeError):
        invoke_structured(ctx, dict, [])
