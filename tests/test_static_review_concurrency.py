"""步骤 2 的按章并发：图扇出、真正并发、日志编号唯一、汇总去重。

锁定本次改造的五条性质，防止回退成「一个大节点串行审」或「按维度扇出」：

1. diff 非空时 `diff_collect` 扇出到**每个规约章节一个**节点，全部结束后才进汇总节点；
2. 各章**真的同时跑**（同一时刻有多路审查在飞），而不是顺序执行；
3. 并发节点共享同一条日志编号序列 —— 报告里不能出现两条 `[步骤2.M]`（HR-09）；
4. 汇总节点去重 + 统一编号，且离线时「步骤2 降级」只写一条而不是每章一条；
5. **每章规约只注入一次**（按维度扇出会重复注入 3.26x，见下方 token 用例）。
"""
from __future__ import annotations

import sys
import threading
import time
from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest

from kanma.graph import _route_after_diff, run_review
from kanma.llm import LLMContext
from kanma.models.schemas import (
    ProjectConfig,
    RequirementDoc,
    ReviewDimension,
    Severity,
    SourceRepo,
    StaticFinding,
)
from kanma.nodes.static_review import (
    STATIC_REVIEW_GENERAL_NODE,
    STATIC_REVIEW_MERGE_NODE,
    STATIC_REVIEW_NODE_PREFIX,
    chapter_node,
    review_chapter,
    review_units,
    static_review_merge,
)
from kanma.nodes.step_log import parse_substep
from kanma.rules import build_guide, chapter_dimensions, spec_chapters
from kanma.tools.sandbox import Sandbox

# 单章审查的模拟耗时：若串行跑就是 N 倍，足以区分并发与串行。
# 取 0.6s 而非 0.4s：节点内还有 build_guide 等固定开销（受 GIL 与机器负载影响，
# 实测约 1s），延迟太小会被固定开销淹没，导致并发判据在全量跑时抖动。
_FAKE_LATENCY = 0.6


@pytest.fixture
def available_ctx() -> LLMContext:
    """「模型可用」的上下文；真正的 LLM 调用由用例用 _llm_review 替身接管。"""
    return LLMContext(True, "mock-model", reason="")


@pytest.fixture
def no_requirement(java_config):
    """去掉需求文档：HR-04（新语义）下步骤 3/4/5 照常执行，
    但本模块的用例只关注步骤 2 的并发——测试链路由下方 autouse 替身空转。"""
    cfg = java_config.model_copy(deep=True)
    cfg.requirement = RequirementDoc()
    return cfg


@pytest.fixture(autouse=True)
def _stub_test_chain(monkeypatch):
    """把步骤 3/4/5 换成空转替身：本模块只测步骤 2 的并发与日志。

    graph.py 的节点闭包在**调用时**才解析模块级名字（`lambda s: testcase_gen(s, ctx)`），
    所以 monkeypatch `kanma.graph` 里的绑定即可整体短路测试链，
    避免 HR-04 新语义（无需求文档也跑 3/4/5）拖慢/干扰并发判据。
    """
    import kanma.graph as g

    def _fake_node(state, *args, **kwargs):
        return {"steps_log": [], "degradation_notes": []}

    monkeypatch.setattr(g, "testcase_gen", _fake_node)
    monkeypatch.setattr(g, "testcode_gen", _fake_node)
    monkeypatch.setattr(g, "sandbox_run", _fake_node)


def _node_module():
    """取 static_review 真模块。

    注意：`import kanma.nodes.static_review as m` 绑到的是 `nodes/__init__.py`
    重导出的**函数**，setattr 会打在函数上 —— 必须走 sys.modules。
    """
    import kanma.nodes.static_review  # noqa: F401

    return sys.modules["kanma.nodes.static_review"]


def _patch_slow_review(monkeypatch):
    """把 LLM 审查换成慢速替身，并统计并发度与各章的调用情况。"""
    sr = _node_module()

    calls: list[dict] = []
    inflight = {"now": 0, "max": 0}
    timeline: list[tuple[float, float]] = []   # 每次审查调用的 (开始, 结束) 墙钟
    lock = threading.Lock()

    def slow_review(
        ctx,
        diff_text,
        requirement_text,
        repo_name="",
        guide="",
        guide_title="",
        focus=(),
        readme="",
    ):
        t0 = time.time()
        with lock:
            inflight["now"] += 1
            inflight["max"] = max(inflight["max"], inflight["now"])
        time.sleep(_FAKE_LATENCY)
        with lock:
            inflight["now"] -= 1
            calls.append(
                {
                    "focus": tuple(focus),
                    "thread": threading.current_thread().name,
                    "guide": guide,
                }
            )
            timeline.append((t0, time.time()))
        return []

    monkeypatch.setattr(sr, "_llm_review", slow_review)
    return calls, inflight, timeline


def _call_records(monkeypatch, findings=None):
    """换成不做延时的替身，只记录每次调用注入的规约与审查重点。"""
    sr = _node_module()
    calls: list[dict] = []

    def fake_review(
        ctx,
        diff_text,
        requirement_text,
        repo_name="",
        guide="",
        guide_title="",
        focus=(),
        readme="",
    ):
        calls.append(
            {"guide": guide, "focus": tuple(focus), "title": guide_title, "readme": readme}
        )
        return list(findings) if findings is not None else []

    monkeypatch.setattr(sr, "_llm_review", fake_review)
    return calls


# ---------------------------------------------------------------------------
# 1. 图结构：每章一个节点，汇总节点等齐所有入边
# ---------------------------------------------------------------------------
def test_graph_has_one_node_per_chapter_plus_merge(available_ctx):
    from kanma.graph import build_graph

    app = build_graph(ctx=available_ctx, sandbox=Sandbox(backend="local"))
    names = {n for n in app.get_graph().nodes}

    for node, _chapter, _title in review_units(None):
        assert node in names, f"图里缺少章节节点 {node}"
    assert STATIC_REVIEW_MERGE_NODE in names
    # 兜底节点也要注册：语言不适用规约时路由会指向它
    assert STATIC_REVIEW_GENERAL_NODE in names
    assert "testcase_gen" in names


def test_route_after_diff_fans_out_to_all_chapters(available_ctx):
    diffs = [SimpleNamespace(is_empty=False, repo_name="svc", reviewable_files=[])]
    route = _route_after_diff({"diffs": diffs}, available_ctx)
    expected = [node for node, _c, _t in review_units(diffs)]
    assert sorted(route) == sorted(expected)


def test_route_short_circuits_on_empty_diff(available_ctx):
    assert _route_after_diff({"diffs": [SimpleNamespace(is_empty=True)]}, available_ctx) == (
        "report_merge"
    )


def test_route_skips_chapter_nodes_when_llm_unavailable(offline_ctx):
    """离线时各章一并跳过，降级说明交给汇总节点统一写一条。"""
    diffs = [SimpleNamespace(is_empty=False)]
    assert _route_after_diff({"diffs": diffs}, offline_ctx) == STATIC_REVIEW_MERGE_NODE


def test_route_falls_back_to_general_node_without_applicable_spec(available_ctx):
    """非规约语言（如 Python）没有可注入章节，退化为单个兜底节点。"""
    diffs = [SimpleNamespace(is_empty=False, repo_name="py", reviewable_files=[])]
    # 造一个「语言不适用」的 diff：reviewable_files 为空 → 语言集为空 → spec_applies 为假
    units = review_units(diffs)
    assert units == [
        (STATIC_REVIEW_GENERAL_NODE, "", "通用审查")
    ], f"无适用规约时应退化为兜底节点：{units}"


def test_chapter_node_name_is_stable_and_unique():
    keys = [k for k, _ in spec_chapters()]
    nodes = [chapter_node(k) for k in keys]
    assert len(nodes) == len(set(nodes)), "章节 key 不同却撞了同一个节点名"
    assert all(n.startswith(STATIC_REVIEW_NODE_PREFIX) for n in nodes)


# ---------------------------------------------------------------------------
# 2. 真并发：同一时刻有多路审查在飞
# ---------------------------------------------------------------------------
def test_chapters_really_run_in_parallel(
    no_requirement, available_ctx, monkeypatch, tmp_path
):
    calls, inflight, timeline = _patch_slow_review(monkeypatch)

    run_review(
        no_requirement,
        ctx=available_ctx,
        sandbox=Sandbox(backend="local", timeout=120),
        output_dir=tmp_path,
        # 显式传 0 = 不限制：本测试验证的是「无节流时真并发」，
        # 不能被系统设置/环境变量的默认阈值（现为 2 路）压住。
        max_static_parallel=0,
    )

    # 每一个章节都至少发起过一次审查
    total_chapters = len(spec_chapters())
    assert len(calls) >= total_chapters, f"调用次数 {len(calls)} < 章节数 {total_chapters}"

    # 并发判据一：同一时刻有多路审查在飞（串行跑这里只会是 1）
    assert inflight["max"] >= min(4, total_chapters), (
        f"最大并发仅 {inflight['max']}，各章疑似串行执行"
    )

    # 并发判据二：实际墙钟远小于「各次调用耗时累加」（= 串行跑完的时间）。
    # ⚠️ 基线必须用**实测的调用耗时之和**，不能写死 `_FAKE_LATENCY * 调用数`：
    # 每章还有 build_guide 等固定开销（GIL 下无法并行），机器忙时这部分可达 2.7s/6 章，
    # 会把「并发省下的时间」完全淹没 → 判据假红（实测 3.3s vs 写死基线 3.6s）。
    # 用累加基线时，固定开销同时出现在分子分母里，判据与机器负载无关。
    assert timeline, "没抓到审查调用的执行窗口"
    wall = max(t1 for _t0, t1 in timeline) - min(t0 for t0, _t1 in timeline)
    serial = sum(t1 - t0 for t0, t1 in timeline)
    assert wall < serial * 0.7, (
        f"步骤2 墙钟 {wall:.2f}s 接近串行基线 {serial:.2f}s，疑似未并发"
    )


def test_static_review_respects_max_parallel(
    no_requirement, available_ctx, monkeypatch, tmp_path
):
    """新增阈值：max_static_parallel 限制同时打给模型的审查请求数。

    把并发度压到 2 时，任何时刻在飞的审查调用不得超过 2；且所有章节仍会被执行
    （节流只降并发、不丢章节）。信号量包在 review_chapter 调用 _llm_review 的外层，
    即便 _llm_review 被测试替身替换，节流依然生效。
    """
    calls, inflight, _timeline = _patch_slow_review(monkeypatch)

    run_review(
        no_requirement,
        ctx=available_ctx,
        sandbox=Sandbox(backend="local", timeout=120),
        output_dir=tmp_path,
        max_static_parallel=2,
    )

    # 所有章节都被审查到（节流不丢章节）
    assert len(calls) >= len(spec_chapters()), (
        f"存在章节未被审查：调用 {len(calls)} < 章节数 {len(spec_chapters())}"
    )
    # 并发度被压到阈值内
    assert inflight["max"] <= 2, (
        f"并发度阈值=2 时最大在飞应为 ≤2，实际 {inflight['max']}"
    )


def test_waiting_state_events_when_throttled(
    no_requirement, available_ctx, monkeypatch, tmp_path
):
    """并发受限时推送 node_state（waiting/running），页面据此区分「在跑/排队」。

    锁定三条性质：
    1. max_static_parallel=2 时必然出现 waiting 事件（有章节抢不到额度在排队）；
    2. 事件序列自洽：每个 waiting 都被同节点的 running 恢复，评审结束时
       没有任何节点停留在 waiting（页面不会把审完的章节显示成排队）；
    3. 不限制（=0）时不得出现任何 waiting 事件（无节流就无排队）。

    ⚠️ 不锁「running 状态数 ≤ 阈值」：running 只表示「节点在干活」，节点完成
    一个分片释放额度、进入下一分片间隙时状态仍是 running，此时其它节点拿到
    额度推 running，running 状态数会短暂超过信号量值——这是真实进度，不是失真。
    持锁数 ≤ 阈值已由 test_static_review_respects_max_parallel 的 inflight 统计锁定。
    """
    _calls, _inflight, _timeline = _patch_slow_review(monkeypatch)
    events: list[tuple[str, str]] = []

    def on_step(phase, node, elapsed_ms, lines, live=None):
        if phase == "live" and live and live.get("type") == "node_state":
            events.append((node, live.get("state", "")))

    run_review(
        no_requirement,
        ctx=available_ctx,
        sandbox=Sandbox(backend="local", timeout=120),
        output_dir=tmp_path,
        on_step=on_step,
        max_static_parallel=2,
    )

    assert any(st == "waiting" for _n, st in events), (
        f"并发受限时应出现排队事件，实际：{events}"
    )
    assert any(st == "running" for _n, st in events), (
        f"排队后应推送恢复事件，实际：{events}"
    )

    # 序列自洽：每个 waiting 最终都被同节点的 running 闭合（多分片章节可能
    # 多次排队，但每次排队后必然恢复），评审结束时无节点停留在 waiting。
    last: dict[str, str] = {}
    for node, st in events:
        last[node] = st
    pending_wait: set[str] = set()
    for node, st in events:
        if st == "waiting":
            pending_wait.add(node)
        elif st == "running":
            pending_wait.discard(node)
    assert not pending_wait, f"这些节点排队后未被恢复事件闭合：{pending_wait}"
    all_nodes = {n for n, _st in events}
    assert all(last.get(n) != "waiting" for n in all_nodes), (
        f"评审结束时仍有节点停在排队态：{last}"
    )

    # 不限制时：没有任何排队/恢复事件
    events.clear()
    run_review(
        no_requirement,
        ctx=available_ctx,
        sandbox=Sandbox(backend="local", timeout=120),
        output_dir=tmp_path,
        on_step=on_step,
        max_static_parallel=0,
    )
    assert not events, f"无节流时不应出现 node_state 事件：{events}"


def test_concurrent_nodes_share_one_log_sequence(
    no_requirement, available_ctx, monkeypatch, tmp_path
):
    _patch_slow_review(monkeypatch)
    _report, state = run_review(
        no_requirement,
        ctx=available_ctx,
        sandbox=Sandbox(backend="local", timeout=120),
        output_dir=tmp_path,
    )

    seqs = [
        parse_substep(line)[1]
        for line in state.get("steps_log", [])
        if parse_substep(line) and parse_substep(line)[0] == 2
    ]
    assert len(seqs) >= len(spec_chapters()), f"步骤2 的日志条数异常：{state.get('steps_log')}"
    assert len(seqs) == len(set(seqs)), f"出现重复的 [步骤2.M] 编号：{seqs}"


def test_step_number_aligned_with_chapter_order(
    no_requirement, available_ctx, monkeypatch, tmp_path
):
    """回归锁死：步骤 2 的 `[步骤2.N]` 必须与章节自然顺序（一/二/三…）对齐，

    而不是按线程到达顺序发号——否则报告里会出现「步骤号与章节名对不上」的混乱
    （如 `[步骤2.4] 六、设计规约` 这种错位）。
    """
    _patch_slow_review(monkeypatch)
    _report, state = run_review(
        no_requirement,
        ctx=available_ctx,
        sandbox=Sandbox(backend="local", timeout=120),
        output_dir=tmp_path,
    )

    nums: list[int] = []
    for _key, title in spec_chapters():
        matched = [
            parse_substep(line)[1]
            for line in state.get("steps_log", [])
            if parse_substep(line)
            and parse_substep(line)[0] == 2
            and title in line
        ]
        assert matched, f"章节「{title}」没有产出日志行"
        # 同一章的多行属于同一段确定性编号块，首行即该章块首
        nums.append(matched[0])

    # 首章（一、编程规约）必须永远是 [步骤2.1]，与并发到达顺序无关
    assert nums[0] == 1, f"首章「一、编程规约」必须是 [步骤2.1]，实际 [步骤2.{nums[0]}]"
    # 各章编号严格递增且不重复 ⇒ 与章节顺序一一对齐
    assert nums == sorted(nums), f"章节编号未按章节顺序递增（与章节顺序不一致）：{nums}"
    assert len(nums) == len(set(nums)), f"存在重复章节编号：{nums}"


def test_offline_writes_step2_degradation_only_once(
    no_requirement, offline_ctx, tmp_path
):
    _report, state = run_review(
        no_requirement,
        ctx=offline_ctx,
        sandbox=Sandbox(backend="local", timeout=120),
        output_dir=tmp_path,
    )

    step2_lines = [
        line for line in state.get("steps_log", []) if line.startswith("[步骤2")
    ]
    assert len(step2_lines) == 1, f"离线时步骤2 只应留一行汇总/跳过说明：{step2_lines}"
    assert "跳过语义审查" in step2_lines[0]

    step2_notes = [n for n in state.get("degradation_notes", []) if "步骤2" in n]
    assert len(step2_notes) == 1, f"「步骤2 降级」应只写一条：{step2_notes}"


# ---------------------------------------------------------------------------
# 3. 省 token：每章规约只注入一次，不因维度重复展开
# ---------------------------------------------------------------------------
def test_each_chapter_is_injected_exactly_once(no_requirement, available_ctx, monkeypatch, tmp_path):
    """核心性质：注入的规约文本总量 == 全量章节之和，不存在重复注入。"""
    calls = _call_records(monkeypatch)
    run_review(
        no_requirement,
        ctx=available_ctx,
        sandbox=Sandbox(backend="local", timeout=120),
        output_dir=tmp_path,
    )

    injected = [c["guide"] for c in calls if c["guide"]]
    assert injected, "没有任何一次调用注入了规约"

    full = {k: build_guide(chapters=[k], max_chars=9000)[0] for k, _ in spec_chapters()}
    # 注入的文本必须逐一对上某一章的原文，且每章最多出现一次（同一 diff 分片下）
    matched = []
    for text in injected:
        hit = [k for k, g in full.items() if g == text]
        assert len(hit) == 1, f"注入的规约文本对不上任何一章：{text[:60]}…"
        matched.append(hit[0])

    chunks = len(injected) // len(spec_chapters())
    for key in full:
        assert matched.count(key) == chunks, (
            f"章节 {key} 被注入了 {matched.count(key)} 次，应为 {chunks} 次（每个 diff 分片一次）"
        )


def test_no_chapter_is_injected_more_than_once_per_unit(available_ctx, monkeypatch):
    """按维度扇出会把同一章重复注入（如 01 章给命名/注释/正确性/规范）——这里锁死不会。

    回归信号：若有人把扇出单元改回维度，注入总量会从 12.9K 涨到 42K。
    """
    calls = _call_records(monkeypatch)
    sr = _node_module()

    class _Diff:
        is_empty = False
        repo_name = "order-service"
        reviewable_files = []
        readme = ""

    with monkeypatch.context() as m:
        m.setattr(sr, "chunk_diff", lambda d: ["diff 片段"])
        m.setattr(sr, "_diff_languages", lambda d: {"java"})
        for _node, chapter, title in review_units(None):
            review_chapter(
                {"config": SimpleNamespace(requirement=SimpleNamespace(raw_text="")),
                 "diffs": [_Diff()]},
                available_ctx,
                chapter,
                title,
            )

    # 一章一个节点、一个 diff 分片 ⇒ 总共恰好 N 次调用，注入 N 份互不相同的规约
    assert len(calls) == len(spec_chapters()), f"调用次数 {len(calls)} != 章节数 {len(spec_chapters())}"
    guides = [c["guide"] for c in calls]
    assert len(guides) == len(set(guides)), "同一章规约被重复注入了"


def test_total_injected_chars_equals_full_spec_size():
    """数据层断言：各章注入量之和 == 全量规约（不存在跨章重复的展开）。"""
    total = sum(len(build_guide(chapters=[k], max_chars=9000)[0]) for k, _ in spec_chapters())
    assert total > 0
    # 6 章合计约 12.9K；若哪天按维度展开（13 单元 / 42K）这个量级会被打破
    assert total < 20000, f"规约注入总量 {total} 异常膨胀，疑似重复展开"


def test_each_chapter_has_a_focus_label():
    """每章都要有审查重点，否则提示词退回「六维全念」，LLM 容易跑偏。"""
    for key, _title in spec_chapters():
        assert chapter_dimensions(key), f"章节 {key} 没有声明 chapter_dimensions"


# ---------------------------------------------------------------------------
# 4. 汇总节点：去重 + 统一编号
# ---------------------------------------------------------------------------
def _finding(dim: ReviewDimension, title: str, line: int = 10) -> StaticFinding:
    return StaticFinding(
        id="",
        dimension=dim,
        severity=Severity.P1,
        file="OrderService.java",
        line=line,
        title=title,
        reason="证据：diff 新增行",
        suggestion="改为参数化查询",
    )


def test_merge_dedups_and_renumbers(available_ctx):
    dup = _finding(ReviewDimension.SECURITY, "SQL 拼接注入")
    state = {
        "raw_findings": [
            dup,
            dup.model_copy(deep=True),  # 同一处被两个章节节点重复报出
            _finding(ReviewDimension.SECURITY, "硬编码密钥", line=12),
            _finding(ReviewDimension.NAMING, "缩写不达意"),
        ]
    }
    out = static_review_merge(state, available_ctx)

    assert [f.id for f in out["findings"]] == ["SEC-001", "SEC-002", "NAM-001"]
    assert "去重" in out["steps_log"][0]


def test_merge_reports_offline_degradation(offline_ctx):
    out = static_review_merge({"raw_findings": []}, offline_ctx)
    assert out["findings"] == []
    assert any("步骤2 降级" in n for n in out["degradation_notes"])


def test_review_chapter_injects_only_its_own_chapter(java_config, available_ctx, monkeypatch):
    """单个章节节点只把「自己那一章」交给 LLM，结果进 raw_findings。"""
    sr = _node_module()

    class _Empty:
        is_empty = False
        repo_name = "order-service"
        reviewable_files = []
        language = None
        readme = ""

    # diff 为空：该章直接跳过
    out = review_chapter({"config": java_config, "diffs": []}, available_ctx, "03-security-standards", "三、安全规约")
    assert out["raw_findings"] == []
    assert out["steps_log"][0].endswith("跳过")

    # 有 diff 时：注入的规约必须只来自安全章，focus 取该章声明的重点
    calls = _call_records(monkeypatch, findings=[_finding(ReviewDimension.SECURITY, "越权")])
    with monkeypatch.context() as m:
        m.setattr(sr, "chunk_diff", lambda d: ["diff 片段"])
        m.setattr(sr, "_diff_languages", lambda d: {"java"})
        out = review_chapter(
            {"config": java_config, "diffs": [_Empty()]},
            available_ctx,
            "03-security-standards",
            "三、安全规约",
        )

    assert calls, "章节节点没有发起审查"
    expected_guide = build_guide(chapters=["03-security-standards"], max_chars=9000)[0]
    assert all(c["guide"] == expected_guide for c in calls), "注入了非本章的规约"
    assert all(c["focus"] == chapter_dimensions("03-security-standards") for c in calls)
    assert len(out["raw_findings"]) == len(calls)


# ---------------------------------------------------------------------------
# 5. 多工程：步骤 2 按工程维度串行（不交叉并发）
# ---------------------------------------------------------------------------
def _overlap_detector():
    """构造一个 _llm_review 替身，检测「不同工程是否在同一时刻交叉并发」。

    维护一个「当前在飞的工程名集合」：若某次调用进入时集合里已有**不同的**工程名，
    说明工程边界没被串行化，置 bad=True。返回 (替身, 共享状态)。
    """
    state = {"set": set(), "bad": False}
    lock = threading.Lock()

    def fake_review(
        ctx, diff_text, requirement_text, repo_name="",
        guide="", guide_title="", focus=(), readme="",
    ):
        with lock:
            others = state["set"] - {repo_name}
            if others:
                state["bad"] = True
            state["set"].add(repo_name)
        time.sleep(0.05)
        with lock:
            state["set"].discard(repo_name)
        return []

    return fake_review, state


def test_review_chapter_serializes_by_project(monkeypatch, available_ctx):
    """工程级串行：6 章并发审查 2 个工程时，不同工程的模型请求不得交叉并发。

    直接驱动 `review_chapter`（与图里一致的入口），共享一个工程级屏障；若屏障失效，
    工程 0（order-service）与工程 1（pay-service）的审查会交错在飞，断言失败。
    """
    sr = _node_module()
    monkeypatch.setattr(sr, "chunk_diff", lambda d: ["diff 片段"])
    monkeypatch.setattr(sr, "_diff_languages", lambda d: {"java"})

    fake_review, probe = _overlap_detector()
    monkeypatch.setattr(sr, "_llm_review", fake_review)

    class _Diff:
        is_empty = False
        repo_name = ""
        reviewable_files = []
        readme = ""

    diffs = [_Diff(), _Diff()]
    diffs[0].repo_name = "order-service"
    diffs[1].repo_name = "pay-service"
    state = {
        "config": SimpleNamespace(requirement=SimpleNamespace(raw_text="")),
        "diffs": diffs,
    }

    K = len(spec_chapters())
    barrier = threading.Barrier(K)

    threads = []
    for key, title in spec_chapters():
        t = threading.Thread(
            target=review_chapter,
            args=(state, available_ctx, key, title),
            kwargs={"project_barrier": barrier},
        )
        threads.append(t)
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert not probe["bad"], "不同工程的审查请求发生了交叉并发，未按工程串行"


def test_run_review_serializes_projects(monkeypatch, available_ctx, tmp_path):
    """集成验证：run_review 评审 2 个 Java 工程时，步骤 2 的请求按工程串行、不交叉。

    两个真实 git 仓库（各含一次 Java commit），空需求文档（步骤 3/4/5 由 autouse 替身空转，
    聚焦步骤 2）；替身 _llm_review 检测跨工程交叉并发。
    """
    import subprocess

    def _git(args, cwd):
        subprocess.run(["git", *args], cwd=str(cwd), capture_output=True, text=True, check=True)

    def _java_repo(name: str) -> str:
        repo = tmp_path / name
        repo.mkdir()
        _git(["init", "-b", "main"], repo)
        _git(["config", "user.email", "k@e.com"], repo)
        _git(["config", "user.name", "kanma"], repo)
        (repo / "Service.java").write_text(
            "package demo;\npublic class Svc {\n  public void m() { int x = 1; }\n}\n",
            encoding="utf-8",
        )
        _git(["add", "."], repo)
        _git(["commit", "-m", "feat"], repo)
        return str(repo)

    now = datetime.now()
    cfg = ProjectConfig(
        name="multi-java",
        repos=[
            SourceRepo(
                name="order-service", git_url=_java_repo("r_order"), branch="main",
                start_time=now - timedelta(days=1), end_time=now + timedelta(days=1),
            ),
            SourceRepo(
                name="pay-service", git_url=_java_repo("r_pay"), branch="main",
                start_time=now - timedelta(days=1), end_time=now + timedelta(days=1),
            ),
        ],
        requirement=RequirementDoc(),  # 空需求 → 步骤 3/4/5 由 autouse 替身空转，聚焦步骤 2
        workdir=tmp_path / "wd",
    )

    sr = _node_module()
    fake_review, probe = _overlap_detector()
    monkeypatch.setattr(sr, "_llm_review", fake_review)

    run_review(
        cfg,
        ctx=available_ctx,
        sandbox=Sandbox(backend="local", timeout=120),
        output_dir=tmp_path,
    )
    assert not probe["bad"], "run_review 中不同工程的审查请求交叉并发，未按工程串行"


# ---------------------------------------------------------------------------
# 4. 有序调度：并发受限时章节启动顺序严格 一→六
# ---------------------------------------------------------------------------
def test_ordered_gate_starts_in_ordinal_order():
    """OrderedGate 单元：额度受限时启动顺序严格按编号 0→N（与线程启动顺序无关）。"""
    from kanma.nodes.static_review import OrderedGate

    gate = OrderedGate(2, total=6)
    order: list[int] = []
    lock = threading.Lock()

    def worker(i: int) -> None:
        v = gate.view(i)
        v.acquire()
        with lock:
            order.append(i)
        time.sleep(0.02)
        v.release()

    # 故意打乱线程启动顺序：3,0,5,1,4,2
    threads = [threading.Thread(target=worker, args=(i,)) for i in (3, 0, 5, 1, 4, 2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert order == [0, 1, 2, 3, 4, 5], f"启动顺序应严格 0→5：{order}"


def test_ordered_gate_unlimited_starts_all_immediately():
    """不限流（limit<=0 / limit>=total）时所有章节立即放行，行为与旧信号量一致。"""
    from kanma.nodes.static_review import OrderedGate

    gate = OrderedGate(0, total=6)
    assert gate.view(3).try_acquire() is True
    assert gate.view(5).try_acquire() is True

    gate2 = OrderedGate(6, total=6)
    assert all(gate2.view(i).try_acquire() for i in range(6))


def test_ordered_gate_finish_unblocks_later_chapters():
    """某章从未启动即收尾（提前返回/异常终止）时，编号更大的章节必须能拿到额度而非挂死。"""
    from kanma.nodes.static_review import OrderedGate

    gate = OrderedGate(1, total=3)
    gate.view(0).finish()   # 章节0从未启动即了结
    gate.view(1).finish()   # 章节1同样
    gate.view(2).acquire()  # 不应阻塞：前置章节均已了结
    gate.view(2).release()


def test_ordered_gate_reacquire_yields_to_unstarted():
    """后续分片让位：额度释放后必须先给编号最小的未启动章节，不允许已启动章插队。

    用户语义：限 2 路时「一、二执行中」，一/二任一先执行完，下一个进入执行中的
    必定是三 —— 前提是已启动章节的分片间隙不能把自己刚释放的额度抢回去。
    """
    from kanma.nodes.static_review import OrderedGate

    gate = OrderedGate(2, total=6)
    v = [gate.view(i) for i in range(6)]
    # 一、二启动
    assert v[0].try_acquire() and v[1].try_acquire()
    # 一释放额度（分片间隙）：其后续分片必须拿不到，额度留给未启动的三
    v[0].release()
    assert v[0].try_acquire() is False, "已启动章节的后续分片不得插队到未启动章节之前"
    assert v[2].try_acquire() is True, "释放的额度应先给编号最小的未启动章节（三）"
    # 二释放：同样先给四，一仍不得复用
    v[1].release()
    assert v[3].try_acquire() is True
    assert v[0].try_acquire() is False
    # 三/四启动后额度已满，五/六排队；逐一释放后五、六依次启动
    v[2].release()
    assert v[4].try_acquire() is True
    v[3].release()
    assert v[5].try_acquire() is True
    # 全部章节都启动过：后续分片恢复自由竞争
    v[4].release()
    assert v[0].try_acquire() is True, "全部章节启动后，后续分片应能正常复用额度"


def test_ordered_gate_expected_single_participant():
    """路由写入实际参与方数：只扇出 1 个 general 节点时，「全部已启动」按 1 判定。

    图按全量章节注册（total=7，general 序号 6），若按注册数判断，唯一节点的
    后续分片会永远等不齐而挂死（直到 600s 兜底）。
    """
    from kanma.nodes.static_review import OrderedGate

    gate = OrderedGate(2, total=7)
    gate.set_expected(1)
    g = gate.view(6)
    assert g.try_acquire() is True
    g.release()
    assert g.try_acquire() is True, "唯一参与方已启动，其后续分片不应被阻塞"


def test_ordered_gate_reacquire_yields_under_threads():
    """线程级：0 号章有多个分片时，其第 2 个分片必须等所有章节都启动过才开始。"""
    from kanma.nodes.static_review import OrderedGate

    gate = OrderedGate(2, total=6)
    events: list[tuple[str, int]] = []
    lock = threading.Lock()

    def worker(ordinal: int, chunks: int) -> None:
        view = gate.view(ordinal)
        for _ in range(chunks):
            if not view.try_acquire():
                view.acquire()
            with lock:
                events.append(("run", ordinal))
            time.sleep(0.001)
            view.release()

    threads = [threading.Thread(target=worker, args=(0, 4))] + [
        threading.Thread(target=worker, args=(i, 1)) for i in range(1, 6)
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    starts = [o for kind, o in events if kind == "run"]
    first_starts = []
    seen: set[int] = set()
    for o in starts:
        if o not in seen:
            seen.add(o)
            first_starts.append(o)
    assert first_starts == [0, 1, 2, 3, 4, 5], f"首次启动应严格 0→5：{first_starts}"
    # 0 号章的第 2 个分片不得早于 5 号章首次启动（额度先尽着未启动章节）
    second_of_zero = [i for i, o in enumerate(starts) if o == 0][1]
    fifth_start = starts.index(5)
    assert second_of_zero > fifth_start, (
        f"0 号章第 2 分片（位置 {second_of_zero}）不得早于 5 号章启动（位置 {fifth_start}）"
    )


def test_chapters_start_in_ordinal_order(no_requirement, available_ctx, monkeypatch, tmp_path):
    """端到端：max_static_parallel=2 时各章首次审查按 一→六 的规约章节顺序启动。"""
    sr = _node_module()
    order: list[str] = []
    lock = threading.Lock()

    def fake(ctx, diff_text, requirement_text, repo_name="", guide="", guide_title="",
             focus=(), readme=""):
        with lock:
            if guide_title and guide_title not in order:
                order.append(guide_title)
        time.sleep(0.03)
        return []

    monkeypatch.setattr(sr, "_llm_review", fake)

    run_review(
        no_requirement,
        ctx=available_ctx,
        sandbox=Sandbox(backend="local", timeout=120),
        output_dir=tmp_path,
        max_static_parallel=2,
    )

    canonical = [t for _k, t in spec_chapters()]
    assert len(order) == len(canonical), f"应有 {len(canonical)} 章启动，实际 {order}"
    assert order == canonical, f"章节启动顺序应按 一→六：{order}（期望 {canonical}）"
