"""HR-04 需求对齐优先：无需求文档时的降级语义（2026-09-16 新版）。

规则原文（AGENTS.MD · HR-04，已按 cortex SKILL.md 重构）：

    无需求开发文档时**仍须**基于 diff 改动溯源到所有受影响对外入口生成
    **变更行为用例**并真实运行（步骤 3/4/5 不跳过），仅跳过「需求符合性用例」；
    报告不得声称功能验证通过，结论不得为「通过」。

本文件锁死五件事，防止回退到旧语义（旧的「跳过 3/4/5」已是错误行为）：
1. 无需求文档 → 步骤 3 仍产出用例，但**不得**出现 REQUIREMENT 来源；
2. 步骤 4 仍产出测试代码、步骤 5 仍真实执行（有产物就跑，不设需求文档门）；
3. 降级说明含「缺少需求开发文档（HR-04），未做需求符合性核对」，进报告第 7 章；
4. 结论**不得**为「通过」（HR-04 护栏，即使无缺陷、测试全绿）；
5. 有需求文档时行为不变：三类用例齐备，不得误降级。
"""

from __future__ import annotations

from kanma.models.schemas import (
    RequirementDoc,
    ReviewDimension,
    ReviewVerdict,
    Severity,
    StaticFinding,
)
from kanma.nodes.report_merge import decide_verdict
from kanma.nodes.testcase_gen import testcase_gen as run_testcase_gen
from kanma.nodes.testcode_gen import testcode_gen as run_testcode_gen
from kanma.tools.sandbox import Sandbox


def _without_requirement(cfg):
    cfg = cfg.model_copy(deep=True)
    cfg.requirement = RequirementDoc()
    return cfg


def test_pipeline_still_generates_cases_without_requirement(java_config, offline_ctx):
    """无需求文档：步骤 3 不跳过，仍产出用例，但不得有 REQUIREMENT 来源（HR-04 新语义）。"""
    from kanma.graph import run_review

    cfg = _without_requirement(java_config)
    report, state = run_review(
        cfg, ctx=offline_ctx, sandbox=Sandbox(backend="local", timeout=120)
    )

    assert report.test_cases, "无需求文档时仍须生成变更行为/回归用例（HR-04 新语义，不得跳过步骤 3）"
    origins = {c.origin.value for c in report.test_cases}
    assert "REQUIREMENT" not in origins, (
        f"缺少需求文档时不得生成需求符合性用例，实际来源：{origins}"
    )

    log = "\n".join(state.get("steps_log", []))
    assert "缺少需求开发文档" in log, "步骤 3 轨迹必须标注「缺少需求开发文档」"


def test_pipeline_still_generates_and_runs_tests_without_requirement(java_config, offline_ctx):
    """无需求文档：步骤 4 仍生成测试代码、步骤 5 仍执行（不设需求文档门）。"""
    from kanma.graph import run_review

    cfg = _without_requirement(java_config)
    report, state = run_review(
        cfg, ctx=offline_ctx, sandbox=Sandbox(backend="local", timeout=120)
    )

    assert [a for a in state.get("test_artifacts", []) if a.files], (
        "无需求文档时步骤 4 不得跳过：仍须产出测试代码（HR-04 新语义）"
    )
    assert report.test_results, "有测试产物时步骤 5 必须执行并产出结果（HR-06）"

    notes = "\n".join(state.get("degradation_notes", []))
    assert "缺少需求开发文档" in notes, "HR-04 降级必须写进第 7 章降级说明（HR-09）"
    assert "未做需求符合性核对" in notes, "降级说明须明确「未做需求符合性核对」"


def test_verdict_never_approve_without_requirement(java_config, offline_ctx):
    """HR-04 护栏：无需求文档时结论不得为「通过」——即使无缺陷、测试全绿。"""
    from kanma.graph import run_review

    cfg = _without_requirement(java_config)
    report, _state = run_review(
        cfg, ctx=offline_ctx, sandbox=Sandbox(backend="local", timeout=120)
    )

    assert report.verdict is not ReviewVerdict.APPROVE, (
        f"无需求文档时结论不得为「通过」，实际：{report.verdict.value}"
    )


def test_decide_verdict_guard_unit():
    """护栏单元测试：零缺陷 + 全绿也不得判通过；有需求文档时行为不变。"""
    from kanma.models.schemas import StaticFinding

    verdict, reason = decide_verdict([], [], requirement_available=False)
    assert verdict is ReviewVerdict.SUGGEST_CHANGES
    assert "缺少需求开发文档" in reason

    # 对照：有需求文档且确实干净 → 通过
    verdict, _ = decide_verdict(
        [StaticFinding(
            id="F-001", dimension=ReviewDimension.CONVENTION, severity=Severity.P3,
            file="a.java", line=1, title="t", evidence="e", reason="r", suggestion="s",
        )],
        [],
        requirement_available=True,
    )
    assert verdict is ReviewVerdict.SUGGEST_CHANGES, "有 P3 时应为建议修改后合入"

    verdict, _ = decide_verdict([], [], requirement_available=True)
    assert verdict is ReviewVerdict.APPROVE


def test_requirement_present_keeps_full_behaviour(java_config, offline_ctx):
    """有需求文档时行为不变：不得误降级、不得误判「未做需求符合性核对」。"""
    from kanma.graph import run_review

    report, state = run_review(
        java_config, ctx=offline_ctx, sandbox=Sandbox(backend="local", timeout=120)
    )
    assert java_config.requirement.available, "fixture 应带需求文档"
    assert report.test_cases, "有需求文档时不应跳过步骤 3"

    notes = "\n".join(state.get("degradation_notes", []))
    assert "未做需求符合性核对" not in notes, (
        "有需求文档时不得出现 HR-04 降级说明"
    )


def test_sandbox_node_is_always_reached(java_config, offline_ctx):
    """图里 testcode_gen 后固定接 sandbox_run：跳过判定在节点内，不在条件边。"""
    from kanma.graph import build_graph

    app = build_graph(ctx=offline_ctx, sandbox=Sandbox(backend="local", timeout=120))
    edges = {(e.source, e.target) for e in app.get_graph().edges}
    assert ("testcode_gen", "sandbox_run") in edges, (
        "testcode_gen 后必须直连 sandbox_run（跳过判定在节点内，不用条件边短路），"
        f"实际边：{sorted(edges)}"
    )
