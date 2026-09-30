"""SSE 实时通道序列化防护。

回归背景：曾经 jobs.py 的 step 事件把函数对象 `step_label` 本身写进了载荷
（`"label": step_label` 而非 `label`），`json.dumps` 抛 TypeError 一路冒泡，
把整条 SSE 长连接打断 —— 页面从此收不到任何进度，评审像「卡死」。

本组用例锁定：
1. step 事件的 `label` 必须是字符串（动态解析出的中文名）；
2. 万一载荷里混进不可序列化对象，`_sse_frame` 必须降级而不是崩掉整条流。
"""

from __future__ import annotations

import json

from kanma.graph import step_label
from kanma.web.app import _sse_frame


def test_step_payload_label_is_string():
    """步骤事件载荷的 label 是 step_label(node) 的返回值（str），不是函数本身。"""
    node = "static_review_ch_01_coding_standards"
    label = step_label(node)
    payload = {
        "phase": "start",
        "node": node,
        "label": label,  # 曾经误写为 step_label（函数），会令 json.dumps 失败
        "elapsed_ms": 0.0,
        "lines": [],
        "total_lines": 0,
        "skip_reason": None,
    }
    # 必须能序列化且不报 TypeError
    text = _sse_frame("step", payload)
    assert 'event: step' in text
    frame = text.split("data: ", 1)[1].strip()
    decoded = json.loads(frame)
    assert isinstance(decoded["label"], str)
    assert decoded["label"] == label


def test_sse_frame_sanitizes_function_value():
    """载荷里万一混入函数对象，_sse_frame 降级为字符串而非整条流崩溃。"""
    def some_func() -> None:  # noqa: D401
        pass

    text = _sse_frame("live", {"node": "x", "label": some_func})
    frame = text.split("data: ", 1)[1].strip()
    decoded = json.loads(frame)  # 不抛异常即通过
    assert isinstance(decoded["label"], str)
    assert "some_func" in decoded["label"]


def test_sse_frame_recurses_into_nested():
    """嵌套 dict / list 里的不可序列化对象也要被降级。"""
    bad = {"a": {"b": [object()]}}

    class _C:
        pass

    text = _sse_frame("live", {"x": bad, "y": {"z": _C()}})
    frame = text.split("data: ", 1)[1].strip()
    decoded = json.loads(frame)  # 整条仍可用
    assert isinstance(decoded["x"]["a"]["b"][0], str)
    assert isinstance(decoded["y"]["z"], str)


def test_jobs_source_no_function_in_label():
    """静态守住根因：jobs.py 不得再把函数名写进 label 字段。"""
    from pathlib import Path

    src = Path("src/kanma/web/jobs.py").read_text(encoding="utf-8")
    assert '"label": step_label' not in src, "根因回归：label 字段误用函数对象"
    assert '"label": label' in src, "期望的修复写法未找到"
