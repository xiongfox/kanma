"""离线全链路冒烟：无 LLM 时六步流程仍完整执行，且降级必须显式声明。

验证点：
- 六步都留下轨迹（HR-09）
- 无 LLM 时走推断用例 + 模板骨架，不伪造通过（HR-06）
- 未实现断言的用例记为 NOT_RUN，不计入通过率
"""

from kanma.graph import run_review
from kanma.models.schemas import ReviewVerdict, TestStatus
from kanma.tools.sandbox import Sandbox


def test_offline_pipeline_end_to_end(java_config, offline_ctx):
    sandbox = Sandbox(backend="local", timeout=120)
    report, state = run_review(java_config, ctx=offline_ctx, sandbox=sandbox)

    # 六步轨迹
    log = "\n".join(state.get("steps_log", []))
    for step in ("步骤1", "步骤2", "步骤3", "步骤4", "步骤5", "步骤6"):
        assert step in log, f"{step} 未执行（HR-09：流程不可跳步）"

    # 降级必须显式声明
    notes = "\n".join(state.get("degradation_notes", []))
    assert "LLM 不可用" in notes, "离线模式必须写明降级原因"

    # 离线 + 无机检 = 静态审查无结论：不得凭空产出缺陷（HR-06 禁止伪造）
    assert report.findings == [], "离线模式跳过语义审查且无正则机检，不应产出任何缺陷"
    assert report.p0_count == 0

    # 结论仍须可判定（HR-10）：有降级项就不能判「通过」
    assert isinstance(report.verdict, ReviewVerdict)
    assert report.verdict is ReviewVerdict.SUGGEST_CHANGES

    # 测试链路真实执行过（沙箱 local）
    if report.test_results:
        statuses = {r.status for r in report.test_results}
        assert TestStatus.PASSED not in statuses, (
            "离线模板骨架未实现断言，不应出现 PASSED（HR-06：禁止伪造结果）"
        )

    # 报告渲染完整
    assert "# 勘码 · 代码 Review 报告" in report.markdown
    for i in range(1, 9):
        assert f"## {i}." in report.markdown


def test_empty_diff_short_circuits_with_degradation(sample_config, offline_ctx):
    """diff 为空时短路到报告，且必须写明被跳过的步骤。"""
    from datetime import datetime, timedelta

    from kanma.models.schemas import SourceRepo

    cfg = sample_config.model_copy(deep=True)
    cfg.repos = [
        SourceRepo(
            git_url=cfg.repos[0].git_url,
            branch="main",
            start_time=datetime.now() + timedelta(days=3),
            end_time=datetime.now() + timedelta(days=5),
        )
    ]
    report, state = run_review(cfg, ctx=offline_ctx, sandbox=Sandbox(backend="local"))
    assert report.findings == []
    assert any("降级" in n for n in state.get("degradation_notes", []))
    assert "## 7. 流程降级说明" in report.markdown
