"""LangGraph 编排：勘码六步 Review 流水线。

    diff_collect ─┬─→ static_review_ch_01_coding_standards  ─┐
                  ├─→ static_review_ch_02_exception_logging  │
                  ├─→ …（rules/data 每个规约章节一个节点）      ├─→ static_review_merge
                  └─→ static_review_ch_06_design_standards   ─┘   （去重 + 统一编号）
                                                                        │
                                        → testcase_gen → testcode_gen  ↓
                                                       → sandbox_run → report_merge

步骤 2 是**每个规约章节一个节点并发执行**（同一 superstep 内并行跑，各自只注入
自己那一章），全部结束后由 `static_review_merge` 汇总去重；diff 为空或 LLM
不可用时短路到 report_merge / 汇总节点，并写入降级说明（HR-09）。

⚠️ 扇出单元必须是**章节**而不是维度：维度与章节多对多，按维度扇出会把同一章
规约重复注入 3.26 倍（13 单元 / 42K 字符 vs 6 单元 / 12.9K 字符）。
"""

from __future__ import annotations

import os
import threading
import time
from pathlib import Path
from typing import Any, Callable

from langgraph.graph import END, StateGraph

from kanma.llm import LLMContext, get_llm
from kanma.models.schemas import ProjectConfig, ReviewReport
from kanma.nodes.step_log import SeqAllocator, reset_emitter, set_emitter
from kanma.nodes import (
    diff_collect,
    report_merge,
    sandbox_run,
    static_review_merge,
    testcase_gen,
    testcode_gen,
)
from kanma.nodes.static_review import (
    GENERAL_REVIEW_TITLE,
    STATIC_REVIEW_GENERAL_NODE,
    STATIC_REVIEW_MERGE_NODE,
    STATIC_REVIEW_NODE_PREFIX,
    OrderedGate,
    chapter_node,
    review_chapter,
    review_units,
)
from kanma.rules import spec_chapters
from kanma.state import ReviewState
from kanma.tools.sandbox import Sandbox, pick_sandbox


def _route_after_diff(state: ReviewState, ctx: LLMContext | None = None) -> Any:
    """diff 采集后的路由：并发扇出到各章节节点，或短路到汇总/报告节点。

    返回节点名列表时，LangGraph 会在**同一 superstep 内并发**执行这些节点——
    各章互不依赖，语义审查的耗时因此从「六章串行」压到「最长那一条链」。
    章节清单由 `rules/data/*.md` 决定（增删章节文件不需要改这里）。
    """
    diffs = state.get("diffs", [])
    if not diffs or all(d.is_empty for d in diffs):
        return "report_merge"
    if ctx is not None and not ctx.available:
        # 离线：各章一并跳过（无模型可用，跑了也只会各自记一条降级），
        # 由汇总节点统一写一条「步骤2 降级」而不是每章一条。
        return STATIC_REVIEW_MERGE_NODE
    return [node for node, _chapter, _title in review_units(diffs)]


#: 固定节点 -> 中文步骤名（页面进度条展示用）
#: 步骤 2 展开为 N 个并发章节节点 + 一个汇总节点；章节节点名由 `rules/data/*.md`
#: 决定，故不在此硬编码，走 `step_label()` 动态解析（增删章节文件无需改代码）。
STEP_LABELS: dict[str, str] = {
    "diff_collect": "步骤 1 · 采集 diff",
    "static_review": "步骤 2 · 静态审查",
    STATIC_REVIEW_GENERAL_NODE: f"步骤 2 · 静态审查（{GENERAL_REVIEW_TITLE}）",
    STATIC_REVIEW_MERGE_NODE: "步骤 2 · 审查结果汇总",
    "testcase_gen": "步骤 3 · 生成回归用例",
    "testcode_gen": "步骤 4 · 生成测试代码",
    "sandbox_run": "步骤 5 · 沙箱执行测试",
    "report_merge": "步骤 6 · 汇总与结论",
}


def step_label(node: str) -> str:
    """节点 -> 中文步骤名；章节节点按 `rules/data` 动态解析。"""
    if node in STEP_LABELS:
        return STEP_LABELS[node]
    if node.startswith(STATIC_REVIEW_NODE_PREFIX):
        for key, title in spec_chapters():
            if chapter_node(key) == node:
                return f"步骤 2 · 静态审查（{title}）"
        return "步骤 2 · 静态审查"
    return node

#: 回调签名：on_step(phase, node, elapsed_ms, lines, live=None)
#: phase ∈ {start, done, error, live}；live 是节点执行中实时流出的一条载荷
#: （{"type": "log"|"progress", ...}，见 nodes/step_log.py），用于提升观测性：
#: 节点跑几十秒时，不必等到结束才看到它做了什么。
StepCallback = Callable[[str, str, float, list[str], "dict | None"], None]


def _wrap_node(node: str, fn, on_step: StepCallback | None):
    """给节点包一层壳，向外部汇报开始/结束与新增日志（用于页面流式展示）。

    节点执行期间会在当前线程登记一个实时发射器，节点内 `StepLog.add()` /
    `StepLog.progress()` 会立刻经 on_step("live", ...) 流出；节点结束时只补发
    **尚未流出**的日志行，避免前端把同一行渲染两遍。
    """
    if on_step is None:
        return fn

    def _wrapped(state):
        started = time.time()
        on_step("start", node, 0.0, [], None)
        streamed: list[str] = []

        def _emit(payload: dict) -> None:
            if payload.get("type") == "log":
                streamed.append(payload.get("line", ""))
            on_step(
                "live",
                node,
                int((time.time() - started) * 1000),
                [],
                payload,
            )

        token = set_emitter(_emit)
        try:
            delta = fn(state)
        except Exception as exc:  # 节点异常照常抛出，由 _route / graph 处理
            on_step("error", node, int((time.time() - started) * 1000), [str(exc)], None)
            raise
        finally:
            reset_emitter(token)
        lines = list(delta.get("steps_log", [])) if isinstance(delta, dict) else []
        degraded = list(delta.get("degradation_notes", [])) if isinstance(delta, dict) else []
        # 已实时流出的行不再重复下发；只补发节点内手工构造、未经 StepLog 的行
        if lines[: len(streamed)] == streamed:
            pending = lines[len(streamed):]
        else:  # 节点返回的并非 StepLog 全量（顺序/内容有出入），退化为按内容去重
            seen = set(streamed)
            pending = [ln for ln in lines if ln not in seen]
        on_step("done", node, int((time.time() - started) * 1000), pending, None)
        return delta

    return _wrapped


def _wrap_conditional(node: str, condition_fn, fn, on_step: StepCallback | None, taken_edge: str):
    """给条件分支节点包一层壳，当条件导致跳过某分支时记录 skip_reason。"""
    if on_step is None:
        def _wrapped(state):
            return fn(state)
        return _wrapped

    def _wrapped(state):
        started = time.time()
        # 条件分支不 emit start，直接执行
        delta = fn(state)
        elapsed_ms = int((time.time() - started) * 1000)
        # 条件跳过时，记录 skip_reason
        if condition_fn(state):
            # 条件为 True 表示走另一个分支（跳过当前）
            pass  # 由调用方决定
        return delta

    return _wrapped


def build_graph(
    ctx: LLMContext | None = None,
    sandbox: Sandbox | None = None,
    output_dir: Path | None = None,
    on_step: StepCallback | None = None,
    max_static_parallel: int = 0,
):
    """构建并编译流水线图。ctx/sandbox 可注入，便于测试与离线运行。

    on_step 会在每个节点开始/结束时被调用（`graph.STEP_LABELS` 提供中文步骤名），
    Web 控制台用它把执行轨迹实时推给页面。

    `max_static_parallel`：步骤 2 并发度阈值。它限制**同时打给模型的章节审查请求数**
    （每个 `_llm_review` 调用 = 一次模型请求），用于缓解本地模型被 6 路并发压垮的问题。
    ≤0 表示不限制（保持原高并发行为，6 章同时请求）。网关/强模型场景保持默认即可。

    多工程评审时，步骤 2 在**工程维度上串行**：同一工程的所有章节并发跑完，才放行下一工程
    （由 `review_chapter` 的工程级屏障实现），避免「6 章 × N 工程」的请求叠加压垮本地模型；
    单工程场景屏障自动退化为无同步，并行度不受影响。
    """
    ctx = ctx or get_llm()
    sandbox = sandbox or pick_sandbox()

    # 步骤 2 工程级串行：共享屏障持有器。屏障尺寸**必须在路由时**按实际扇出的章节节点数
    # 确定（而非注册节点数）—— 否则 Python 等非规约语言只扇出 1 个 general 节点时，
    # 用注册节点数（6）建屏障会让唯一节点永远等不齐而挂死。路由函数（`_route_with_barrier`）
    # 在扇出前写入正确尺寸；单节点/单工程时置 None（无同步，行为不变）。
    project_barrier: dict[str, Any] = {"barrier": None}

    graph = StateGraph(ReviewState)

    # 步骤 2 的二级日志编号：各章节节点并发执行，各自 StepLog 必须共享同一条序列，
    # 否则报告里会出现 N 条 [步骤2.1]（HR-09 禁止重复编号）。
    dim_seq = SeqAllocator()
    # 全量章节：图结构与具体工程无关，节点必须先注册齐；兜底节点一并注册，
    # 这样路由判定「无适用规约」时也有扇出目标。
    units = review_units(None)
    if not any(n == STATIC_REVIEW_GENERAL_NODE for n, _c, _t in units):
        units = [*units, (STATIC_REVIEW_GENERAL_NODE, "", GENERAL_REVIEW_TITLE)]

    # 步骤 2 并发度节流 + 有序调度：OrderedGate 严格先进先出 —— 首次启动按章节编号
    # （一→六）放行，额度释放后先尽着编号最小的未启动章节（已启动章节的后续分片
    # 让位，防止无序抢占把队列饿成「成对推进」）；≤0 不限制（保持原高并发）。
    parallel_gate = (
        OrderedGate(max_static_parallel, total=len(units))
        if max_static_parallel and max_static_parallel > 0
        else None
    )

    graph.add_node("diff_collect", _wrap_node("diff_collect", diff_collect, on_step))
    for i, (node, chapter, title) in enumerate(units):
        graph.add_node(
            node,
            _wrap_node(
                node,
                lambda s, ch=chapter, t=title, ord_=i: review_chapter(
                    s, ctx, ch, t, seq=dim_seq,
                    parallel_sem=parallel_gate.view(ord_) if parallel_gate else None,
                    project_barrier=project_barrier["barrier"],
                ),
                on_step,
            ),
        )
    graph.add_node(
        STATIC_REVIEW_MERGE_NODE,
        _wrap_node(
            STATIC_REVIEW_MERGE_NODE,
            lambda s: static_review_merge(s, ctx, seq=dim_seq),
            on_step,
        ),
    )
    graph.add_node("testcase_gen", _wrap_node("testcase_gen", lambda s: testcase_gen(s, ctx), on_step))
    graph.add_node("testcode_gen", _wrap_node("testcode_gen", lambda s: testcode_gen(s, ctx), on_step))
    graph.add_node("sandbox_run", _wrap_node("sandbox_run", lambda s: sandbox_run(s, sandbox), on_step))
    graph.add_node("report_merge", _wrap_node("report_merge", lambda s: report_merge(s, output_dir, ctx), on_step))

    def _route_with_barrier(state: ReviewState):
        """路由到各章节节点，并按实际扇出节点数建立工程级串行屏障。

        屏障尺寸 = 本次实际并发的章节节点数（列表长度）。只有 >1 个节点才需要屏障
        （单节点时各工程本就由同一线程顺序处理，无需同步）；字符串路由（短路到
        report_merge / 汇总节点）不建屏障（章节节点不会被调用）。
        并发闸门的「实际参与方数量」在此一并写入：OrderedGate 判断「全部章节已启动」
        必须按本次扇出数（而非注册节点数），否则非规约语言只扇出 1 个 general 节点时
        后续分片会永远等不齐。
        """
        result = _route_after_diff(state, ctx)
        if isinstance(result, list) and len(result) > 1:
            project_barrier["barrier"] = threading.Barrier(len(result))
        else:
            project_barrier["barrier"] = None
        if parallel_gate is not None and isinstance(result, list):
            parallel_gate.set_expected(len(result))
        return result

    graph.set_entry_point("diff_collect")
    graph.add_conditional_edges(
        "diff_collect",
        _route_with_barrier,
        {
            **{node: node for node, _c, _t in units},
            STATIC_REVIEW_MERGE_NODE: STATIC_REVIEW_MERGE_NODE,
            "report_merge": "report_merge",
        },
    )
    # 各章节节点全部结束后才进汇总（LangGraph 会等齐所有入边）
    for node, _chapter, _title in units:
        graph.add_edge(node, STATIC_REVIEW_MERGE_NODE)
    graph.add_edge(STATIC_REVIEW_MERGE_NODE, "testcase_gen")
    graph.add_edge("testcase_gen", "testcode_gen")
    # ⚠️ 测试代码被跳过（如 HR-04 无需求文档）时**不要**用条件边短路到 report_merge：
    # 边一断 sandbox_run 节点根本不执行，页面 DAG 的「⑤ 沙箱执行」永远是灰色
    # pending，看不出是「跳过」。故固定走 sandbox_run，由它自己判定并记
    # 「跳过：{原因}」（NOT_RUN + 降级说明），跳过原因统一可观测。
    graph.add_edge("testcode_gen", "sandbox_run")
    graph.add_edge("sandbox_run", "report_merge")
    graph.add_edge("report_merge", END)

    return graph.compile()


def _resolve_max_static_parallel(
    max_static_parallel: int | None = None,
    system_value: int | None = None,
) -> int:
    """把阈值参数规整为非负整数，优先级（高→低）：

    1. 显式传入（CLI `--max-static-parallel` / 测试）；
    2. 系统设置（Web 控制台系统设置卡，全局生效）；
    3. 环境变量 `KANMA_STATIC_REVIEW_MAX_PARALLEL`；
    4. 2 = 系统默认两路并发（本地模型压得住、云端也够快）。

    `system_value` 为 `None` 表示「未配置系统设置」，此时继续回退到环境变量；
    为具体整数（含 0）则直接采用——0 表示用户显式选择不限制，不再被环境变量覆盖。
    """
    if max_static_parallel is not None:
        try:
            return max(0, int(max_static_parallel))
        except (TypeError, ValueError):
            return 2
    if system_value is not None:
        try:
            return max(0, int(system_value))
        except (TypeError, ValueError):
            return 2
    raw = os.environ.get("KANMA_STATIC_REVIEW_MAX_PARALLEL", "").strip()
    if raw:
        try:
            return max(0, int(raw))
        except ValueError:
            return 2
    return 2


def run_review(
    config: ProjectConfig,
    ctx: LLMContext | None = None,
    sandbox: Sandbox | None = None,
    output_dir: Path | None = None,
    on_step: StepCallback | None = None,
    max_static_parallel: int | None = None,
    system_max_parallel: int | None = None,
) -> tuple[ReviewReport, dict[str, Any]]:
    """执行一次完整评审，返回 (报告, 终态 state)。

    on_step 会被传给每个节点包装壳，用于把执行轨迹实时推给调用方（页面流式展示）。
    `max_static_parallel` 见 `build_graph` 同名参数（步骤 2 并发度阈值）。
    `system_max_parallel` 来自系统设置（Web 控制台），优先级高于环境变量。
    """
    limit = _resolve_max_static_parallel(max_static_parallel, system_value=system_max_parallel)
    app = build_graph(
        ctx=ctx, sandbox=sandbox, output_dir=output_dir, on_step=on_step,
        max_static_parallel=limit,
    )
    final = app.invoke(
        {"config": config},
        config={"recursion_limit": 50},
    )
    report: ReviewReport = final["report"]
    return report, final
