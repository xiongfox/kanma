"""LLM 接入层。

约定：
- **本地模式 / 云端模式**两种接法，参数来自 `kanma.llm_settings`（可在页面切换，
  也可用环境变量预置）。两种模式都走 OpenAI 兼容协议 —— 本地模式连 Ollama /
  vLLM / LM Studio / Xinference 等自部署服务，云端模式连厂商或企业网关。
- **没有可用模型时进入离线模式**：语义型审查与用例生成标记为降级（勘码无正则机检，
  离线时静态审查无结论），绝不伪造 LLM 结论（HR-06 / HR-09）。
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass
from typing import Any

from langchain_core.messages import AnyMessage

from kanma.llm_settings import LLMMode, LLMSettings, ResolvedLLM, get_settings
from kanma.models.schemas import SEVERITY_STANDARD, Severity

logger = logging.getLogger(__name__)

# OpenAI SDK 要求 api_key 非空；本地推理服务与免鉴权网关一般不做校验，填占位即可
_PLACEHOLDER_KEY = "kanma"

#: 结构化输出通道的尝试顺序。
#: 不同 OpenAI 兼容服务支持的通道差异很大：vLLM / SGLang 部分版本只认
#: `response_format=json_schema`，带 `tools` 或 `response_format=json_object`
#: 时直接返回 404；OpenAI 官方与多数网关则相反。因此不能写死单一通道，
#: 按此顺序逐个试，首个成功的通道缓存复用。
STRUCTURED_METHODS: tuple[str, ...] = ("json_schema", "function_calling", "json_mode")

#: 退避重试参数。部分 OpenAI 兼容网关（vLLM 前置代理等）在冷却期/高负载时
#: 会以 **404** 拒绝结构化输出请求，等十余秒后自行恢复。这类失败必须退避重试，
#: 否则评审会在语义审查这一步整段降级。
STRUCTURED_RETRIES = 2  # 每个通道的额外重试次数
STRUCTURED_BACKOFF_BASE = 3.0  # 退避基数：3s → 6s

#: 典型的可重试状态码（不含 404，404 需结合"端点是否曾成功"单独判断）
_RETRYABLE_STATUS = frozenset({408, 409, 429, 500, 502, 503, 504})

#: (model, base_url) -> 首次成功的通道；避免每个分块都重复试错
_STRUCTURED_CACHE: dict[tuple[str, str], str] = {}
_STRUCTURED_LOCK = threading.Lock()


class StructuredOutputError(RuntimeError):
    """结构化输出在所有通道上均失败。"""


@dataclass
class LLMContext:
    """LLM 可用性上下文。available=False 时，调用方必须写降级说明。"""

    available: bool
    model_name: str
    reason: str = ""
    chat: Any = None  # BaseChatModel | None
    mode: LLMMode = LLMMode.CLOUD
    base_url: str = ""
    temperature: float = 0.0
    timeout: int = 120
    api_key_set: bool = False

    def require(self) -> None:
        if not self.available:
            raise RuntimeError(f"LLM 不可用，无法执行该步骤：{self.reason}")

    @property
    def mode_label(self) -> str:
        return "本地模式" if self.mode is LLMMode.LOCAL else "云端模式"

    @property
    def label(self) -> str:
        """展示用的模型描述，如 `qwen2.5-coder:7b（本地模式）`。"""
        if not self.available:
            return "离线模式（未配置模型）"
        text = f"{self.model_name}（{self.mode_label}"
        if not self.api_key_set:
            text += "，无密钥"
        return text + "）"

    @property
    def endpoint(self) -> str:
        return self.base_url or self.model_name

    @property
    def degradation_note(self) -> str:
        return f"LLM 不可用（{self.reason}），依赖模型的环节已跳过，结论可信度降级"

    def to_dict(self) -> dict[str, Any]:
        """供报告/任务记录落盘。"""
        return {
            "available": self.available,
            "model": self.model_name,
            "mode": self.mode.value,
            "mode_label": self.mode_label,
            "base_url": self.base_url,
            "label": self.label,
            "reason": self.reason,
        }


def _build_chat(resolved: ResolvedLLM, temperature: float | None = None):
    """按解析后的参数构造 chat 模型。

    优先 `langchain_openai.ChatOpenAI`（OpenAI 兼容协议，本地/云端通用）；
    若未安装该包，回退到 `langchain` 的 `init_chat_model`。
    """
    temp = resolved.temperature if temperature is None else temperature
    kwargs: dict[str, Any] = {
        "model": resolved.model,
        "temperature": temp,
        "timeout": resolved.timeout,
        "api_key": resolved.api_key or _PLACEHOLDER_KEY,
    }
    if resolved.base_url:
        kwargs["base_url"] = resolved.base_url

    try:
        from langchain_openai import ChatOpenAI
    except ImportError:
        ChatOpenAI = None  # type: ignore[assignment]
    if ChatOpenAI is not None:
        return ChatOpenAI(**kwargs)

    from langchain.chat_models import init_chat_model  # 延迟导入，缺失时由调用方降级

    fallback = dict(kwargs)
    fallback.pop("timeout", None)
    return init_chat_model(**fallback)


def get_llm(
    model: str | None = None,
    temperature: float = 0.0,
    settings: LLMSettings | None = None,
    mode: LLMMode | str | None = None,
) -> LLMContext:
    """构建 LLM 上下文。不可用时返回 available=False 而非抛异常。

    - model：临时覆盖模型名（CLI `--model`）
    - mode：临时覆盖接入模式（CLI `--llm-mode`）
    - settings：默认取进程内设置（页面保存后即生效）
    """
    s = settings or get_settings()
    if mode is not None:
        s = s.model_copy(update={"mode": LLMMode(str(mode).lower())})
    resolved = s.resolve()
    if model:
        resolved = resolved.model_copy(update={"model": model, "api_key_set": resolved.api_key_set})

    base = dict(
        mode=resolved.mode,
        base_url=resolved.base_url,
        temperature=resolved.temperature,
        timeout=resolved.timeout,
        api_key_set=resolved.api_key_set,
    )
    if not resolved.model:
        reason = "已关闭大模型（离线模式）" if not s.enabled else f"{s.mode_label}未配置模型名"
        return LLMContext(False, "", reason=reason, **base)

    try:
        chat = _build_chat(resolved, temperature=temperature)
    except ImportError as e:
        return LLMContext(False, resolved.model, reason=f"缺少模型接入依赖: {e}", **base)
    except Exception as e:  # 模型初始化失败（如缺少依赖/非法配置）
        return LLMContext(False, resolved.model, reason=str(e), **base)
    return LLMContext(True, resolved.model, chat=chat, **base)


def structured_llm(ctx: LLMContext, schema: type):
    """把模型包装成结构化输出（Pydantic schema）。

    仅用于"只要个 runnable、调用方自己 invoke"的场景；**不做通道探测**，
    失败不会自动换通道。需要容错请用 `invoke_structured`。
    """
    ctx.require()
    return ctx.chat.with_structured_output(
        schema, method=_structured_method_order(ctx)[0]
    )


def _structured_method_order(ctx: LLMContext) -> tuple[str, ...]:
    """该端点应尝试的通道顺序：已验证过的排最前，其余保序跟上。"""
    ctx.require()
    key = (ctx.model_name, ctx.base_url)
    with _STRUCTURED_LOCK:
        cached = _STRUCTURED_CACHE.get(key)
    if not cached:
        return STRUCTURED_METHODS
    return (cached, *(m for m in STRUCTURED_METHODS if m != cached))


def _remember_structured_method(ctx: LLMContext, method: str) -> None:
    key = (ctx.model_name, ctx.base_url)
    with _STRUCTURED_LOCK:
        if _STRUCTURED_CACHE.get(key) != method:
            _STRUCTURED_CACHE[key] = method


def _describe_failure(ctx: LLMContext, method: str, exc: Exception) -> str:
    """把 SDK 异常翻成人能排查的话，重点提示端点/通道不匹配。"""
    status = getattr(getattr(exc, "response", None), "status_code", None)
    text = f"{type(exc).__name__}: {exc}"
    if status == 404:
        return (
            f"{text}（端点 {ctx.endpoint} 对「{method}」通道返回 404："
            f"若地址路径有误请去掉/补上 /v1；若地址正确，多为网关冷却期拒答，已自动退避重试）"
        )
    return text


def _is_retryable(ctx: LLMContext, exc: Exception) -> bool:
    """判断该失败是否值得退避重试。

    404 通常是路径写错（重试无意义）；但该端点**此前成功过**就说明地址是通的，
    此时 404 属于网关暂时拒答 —— 实测等待十余秒即恢复，必须重试。
    """
    status = getattr(getattr(exc, "response", None), "status_code", None)
    if status is None:  # 连接错误 / 超时
        return True
    if status in _RETRYABLE_STATUS:
        return True
    if status == 404:
        with _STRUCTURED_LOCK:
            return (ctx.model_name, ctx.base_url) in _STRUCTURED_CACHE
    return False


def invoke_structured(
    ctx: LLMContext,
    schema: type,
    messages: list[AnyMessage],
) -> Any:
    """按通道优先级调用结构化输出，首个成功的通道缓存复用。

    直接在内部完成"选通道 → invoke"，因为通道是否可用只有真正发请求才知道
    （`with_structured_output` 本身永远不报错，404 发生在 invoke 阶段）。
    """
    ctx.require()
    order = _structured_method_order(ctx)
    errors: dict[str, str] = {}
    for method in order:
        for attempt in range(STRUCTURED_RETRIES + 1):
            try:
                result = ctx.chat.with_structured_output(schema, method=method).invoke(messages)
            except Exception as exc:  # noqa: BLE001 - 需要跨通道兜底，异常向上汇总
                errors[method] = _describe_failure(ctx, method, exc)
                retryable = _is_retryable(ctx, exc)
                if attempt < STRUCTURED_RETRIES and retryable:
                    delay = STRUCTURED_BACKOFF_BASE * (2**attempt)
                    logger.warning(
                        "结构化输出通道 %s 第 %d 次失败，%.0fs 后重试（%s）",
                        method,
                        attempt + 1,
                        delay,
                        exc,
                    )
                    time.sleep(delay)
                    continue
                logger.warning("结构化输出通道 %s 失败：%s", method, exc)
                break
            _remember_structured_method(ctx, method)
            return result
    raise StructuredOutputError(
        f"结构化输出在所有通道上均失败（已试：{' → '.join(order)}；"
        f"端点 {ctx.endpoint}）：" + " | ".join(errors[m] for m in order if m in errors)
    )


def probe_structured(ctx: LLMContext) -> dict[str, Any]:
    """探测该端点可用的结构化输出通道。

    评审的语义审查 / 用例生成 / 测试代码生成全走结构化输出，而各服务支持的
    通道差异很大（vLLM 直连常见只支持 json_schema，带 tools 即 404），
    所以在"测试连接"时就探明并缓存，不要等到评审中途失败。
    """
    from langchain_core.messages import HumanMessage
    from pydantic import BaseModel

    class _Ping(BaseModel):
        ok: bool

    tried: list[dict[str, Any]] = []
    for method in STRUCTURED_METHODS:
        try:
            ctx.chat.with_structured_output(_Ping, method=method).invoke(
                [HumanMessage(content='Reply with JSON only: {"ok": true}')]
            )
        except Exception as exc:  # noqa: BLE001 - 逐个通道探测，失败即换下一个
            tried.append({"method": method, "ok": False, "error": str(exc)[:180]})
            continue
        _remember_structured_method(ctx, method)
        tried.append({"method": method, "ok": True})
        return {"ok": True, "method": method, "tried": tried}
    return {"ok": False, "method": "", "tried": tried}


def ping_llm(
    settings: LLMSettings | None = None,
    model: str | None = None,
    mode: LLMMode | str | None = None,
    timeout: float = 20.0,
) -> dict[str, Any]:
    """连通性自检：真实发一条最小请求，验证地址/密钥/模型是否可用。

    只用于页面"测试连接"，不写任何项目文件。
    """
    import time

    ctx = get_llm(model=model, settings=settings, mode=mode)
    if not ctx.available:
        return {"ok": False, "mode": ctx.mode.value, "model": "", "error": ctx.reason}

    started = time.perf_counter()
    try:
        from langchain_core.messages import HumanMessage

        resp = ctx.chat.invoke([HumanMessage(content="ping")])
        content = getattr(resp, "content", "") or ""
        elapsed = int((time.perf_counter() - started) * 1000)
        return {
            "ok": True,
            "mode": ctx.mode.value,
            "mode_label": ctx.mode_label,
            "model": ctx.model_name,
            "base_url": ctx.base_url,
            "latency_ms": elapsed,
            "reply": str(content)[:120],
            # 评审全流程都依赖结构化输出，连通性自检必须顺带报出哪个通道可用，
            # 否则要等到评审跑到语义审查才暴露（表现为步骤 2 报 404）。
            "structured": probe_structured(ctx),
        }
    except Exception as e:
        return {
            "ok": False,
            "mode": ctx.mode.value,
            "model": ctx.model_name,
            "base_url": ctx.base_url,
            "error": f"{type(e).__name__}: {e}",
        }


# 供提示词复用的级别说明：内容取自 SEVERITY_STANDARD（HR-08 定义唯一），
# 与报告里展示给用户的「3.1 严重级别标准」是同一份口径，不会两边说法不一致。
SEVERITY_GUIDE = "\n".join(
    f"- {sev.value}：{meaning}（{action}）"
    for sev, (meaning, action) in SEVERITY_STANDARD.items()
)
