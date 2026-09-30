"""进度观测性：StepLog 实时流出 + i/N 进度 + 节点结束不重复补发。

背景：节点（尤其 LLM 语义审查、沙箱执行）常跑几十秒，而日志若只在节点结束时
一次性交付，页面在整个节点期间是「黑屏」的——六步粗粒度看不出在做什么。
本组用例锁定「执行中即可观测」这一行为，防止回退成批量补发。
"""

from __future__ import annotations

from kanma.graph import _wrap_node
from kanma.nodes.step_log import StepLog, reset_emitter, set_emitter


def _collect_emitted() -> tuple[list[dict], object]:
    """登记一个发射器，返回 (收集到的载荷列表, 复位用的 token)。"""
    out: list[dict] = []

    def _emit(payload: dict) -> None:
        out.append(payload)

    return out, set_emitter(_emit)


def test_add_streams_line_immediately():
    """add() 在追加的同时就把该行推出去，而不是攒到节点结束。"""
    out, token = _collect_emitted()
    try:
        log = StepLog(2)
        line = log.add("语义审查共 14 条")
    finally:
        reset_emitter(token)

    assert len(out) == 1
    assert out[0]["type"] == "log"
    assert out[0]["step"] == 2
    assert out[0]["seq"] == 1
    assert out[0]["line"] == line == "[步骤2.1] 语义审查共 14 条"
    assert out[0]["text"] == "语义审查共 14 条"


def test_no_emitter_is_noop():
    """无发射器（CLI/离线模式）时行为不变，不抛异常。"""
    log = StepLog(1)
    assert log.add("无需发射") == "[步骤1.1] 无需发射"
    log.progress(1, 2, "x")  # 不应抛错
    assert len(log.lines) == 1


def test_progress_not_written_to_lines():
    """progress 是瞬时状态，不得进入 lines —— 否则会污染报告「流程轨迹」。"""
    out, token = _collect_emitted()
    try:
        log = StepLog(5)
        log.progress(2, 7, "沙箱执行 order-service")
        log.add("结果：PASSED×3")
    finally:
        reset_emitter(token)

    assert [o["type"] for o in out] == ["progress", "log"]
    assert out[0] == {
        "type": "progress",
        "step": 5,
        "current": 2,
        "total": 7,
        "label": "沙箱执行 order-service",
    }
    # 进度不落进正式日志
    assert log.lines == ["[步骤5.1] 结果：PASSED×3"]


def test_progress_ignores_non_positive_total():
    out, token = _collect_emitted()
    try:
        StepLog(3).progress(0, 0, "空")
    finally:
        reset_emitter(token)
    assert out == []


def test_wrap_node_streams_live_and_does_not_repeat():
    """节点执行中逐条流出；结束时只补发未经 StepLog 的行，避免前端重复渲染。"""
    events: list[tuple[str, list[str], dict | None]] = []

    def on_step(phase, node, elapsed_ms, lines, live=None):
        events.append((phase, list(lines), live))

    def fn(state):
        log = StepLog(2)
        log.add("语义审查共 14 条")
        log.progress(1, 3, "语义审查")
        log.add("语义补充 3 条")
        return {"steps_log": log.lines}

    _wrap_node("static_review", fn, on_step)({})

    phases = [e[0] for e in events]
    assert phases[0] == "start"
    assert phases[-1] == "done"
    assert phases.count("live") == 3  # 2 行日志 + 1 次进度

    live_logs = [e[2] for e in events if e[0] == "live" and e[2]["type"] == "log"]
    assert [x["line"] for x in live_logs] == [
        "[步骤2.1] 语义审查共 14 条",
        "[步骤2.2] 语义补充 3 条",
    ]

    done_lines = [e for e in events if e[0] == "done"][0][1]
    assert done_lines == [], "已实时流出的行不得在 done 里再发一遍"


def test_wrap_node_delivers_manual_lines_at_done():
    """节点内手工构造（未经 StepLog）的日志行仍应在结束时补发。"""
    events: list[tuple[str, list[str], dict | None]] = []

    def on_step(phase, node, elapsed_ms, lines, live=None):
        events.append((phase, list(lines), live))

    def fn(state):
        log = StepLog(1)
        log.add("实时行")
        return {"steps_log": log.lines + ["[步骤1.9] 手工补的行"]}

    _wrap_node("diff_collect", fn, on_step)({})

    done_lines = [e for e in events if e[0] == "done"][0][1]
    assert done_lines == ["[步骤1.9] 手工补的行"]


def test_wrap_node_resets_emitter_on_exception():
    """节点抛异常时发射器必须复位，否则会污染同一线程上的后续节点。"""
    events: list[tuple[str, list[str], dict | None]] = []

    def on_step(phase, node, elapsed_ms, lines, live=None):
        events.append((phase, list(lines), live))

    def boom(state):
        StepLog(2).add("崩溃前")
        raise RuntimeError("boom")

    try:
        _wrap_node("static_review", boom, on_step)({})
    except RuntimeError:
        pass
    else:  # pragma: no cover
        raise AssertionError("异常应继续向上抛")

    # 发射器已复位：此后再建 StepLog 不应再推向本次的收集器
    out, token = _collect_emitted()
    try:
        StepLog(2).add("另一个节点")
    finally:
        reset_emitter(token)
    assert [o["text"] for o in out] == ["另一个节点"]
    assert [e[0] for e in events][-1] == "error"
