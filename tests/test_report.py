"""报告渲染测试：固定 8 章节，结论与统计表正确呈现。"""

from datetime import datetime

from kanma.models.schemas import (
    SEVERITY_STANDARD,
    ReviewVerdict,
    Severity,
    TestCase,
    TestCaseOrigin,
    TestCodeEntry,
    TestResult,
    TestStatus,
)
from kanma.report.renderer import _lang_for, color_severity, render_report
from tests.test_merge import _finding  # 复用构造辅助


def _report(**kw):
    from kanma.models.schemas import ReviewReport

    base = dict(
        project="demo",
        generated_at=datetime(2026, 9, 4, 10, 0, 0),
        verdict=ReviewVerdict.REJECT,
        verdict_reason="存在 1 个 P0",
        summary={"branch": "main", "steps_log": ["[步骤1] ok"]},
    )
    base.update(kw)
    return ReviewReport(**base)


def test_render_has_all_eight_sections():
    md = render_report(_report())
    for i in range(1, 9):
        assert f"## {i}." in md, f"缺少第 {i} 章节"


def test_render_shows_severity_standard():
    """报告必须展示 P0~P3 的判定标准，且内容与 SEVERITY_STANDARD 逐字一致（HR-08）。"""
    md = render_report(_report())
    assert "严重级别标准" in md, "报告缺少「严重级别标准」小节"
    for sev, (meaning, action) in SEVERITY_STANDARD.items():
        assert f"| {sev.value} |" in md, f"报告缺少 {sev.value} 的标准行"
        assert meaning in md, f"{sev.value} 的含义未展示"
        assert action in md, f"{sev.value} 的处置未展示"


def test_severity_guide_and_report_share_one_source():
    """提示词与报告必须同源：都从 SEVERITY_STANDARD 生成，避免两处说法不一致。"""
    from kanma.llm import SEVERITY_GUIDE

    for sev, (meaning, action) in SEVERITY_STANDARD.items():
        assert f"{sev.value}：{meaning}" in SEVERITY_GUIDE, (
            f"提示词缺少 {sev.value} 的标准说明：{SEVERITY_GUIDE}"
        )
        assert action in SEVERITY_GUIDE


def test_render_shows_verdict_and_findings():
    f = _finding(severity=Severity.P0)
    md = render_report(
        _report(
            findings=[f],
            test_results=[TestResult(case_id="TC-001", status=TestStatus.FAILED,
                                     message="期望10实际0")],
        )
    )
    assert "拒绝合入" in md
    assert "SEC-001" in md
    assert "a.py:10" in md
    assert "FAILED" in md


def test_render_marks_degradation():
    md = render_report(_report(degradation_notes=["步骤5 降级：沙箱不可用"]))
    assert "步骤5 降级：沙箱不可用" in md


def test_render_handles_empty_findings():
    md = render_report(
        _report(verdict=ReviewVerdict.APPROVE, verdict_reason="未发现缺陷", findings=[])
    )
    assert "本次变更未发现缺陷" in md
    assert "通过" in md


def test_render_includes_generated_test_cases():
    md = render_report(
        _report(
            test_cases=[
                TestCase(
                    id="TC-001",
                    title="登录失败应返回 401",
                    origin=TestCaseOrigin.BEHAVIOR,
                    precondition="用户未登录",
                    input_desc="调用 /api/me",
                    expected="HTTP 401",
                    diff_ref="auth.py:20",
                ),
                TestCase(
                    id="TC-002",
                    title="复现密钥硬编码",
                    origin=TestCaseOrigin.REPRODUCE,
                    expected="修复前失败",
                    finding_id="SEC-001",
                ),
            ]
        )
    )
    assert "### 5.4" in md
    assert "生成的测试用例" in md
    assert "TC-001" in md
    assert "登录失败应返回 401" in md
    assert "HTTP 401" in md
    assert "TC-002" in md
    assert "（缺陷 SEC-001）" in md


def test_render_includes_generated_test_code():
    md = render_report(
        _report(
            test_code=[
                TestCodeEntry(
                    repo_name="order-svc",
                    path="tests/test_order.py",
                    framework="pytest",
                    content="def test_order():\n    assert True",
                    case_ids=["TC-001"],
                    executed=False,
                )
            ]
        )
    )
    assert "### 5.5" in md
    assert "生成的测试代码" in md
    assert "tests/test_order.py" in md
    assert "def test_order():" in md
    # 未执行的代码也应保留（用户明确要求）
    assert "（未执行）" in md


def test_render_flow_trace_shows_hierarchical_substeps():
    """流程轨迹必须按 `[步骤N.M]` 层级编号分组展示，同一大步骤的多个小步骤要细分。

    回归保护：render_report 必须向模板传入 step_groups，否则流程轨迹会整段空白。
    """
    steps = [
        "[步骤1.1] 工程「svc」分支 main，时间窗 …",
        "[步骤1.2] 工程「svc」命中 3 个 commit",
        "[步骤2.1] 语义审查 · 编程规约",
        "[步骤2.2] 语义审查共 2 条",
    ]
    md = render_report(_report(summary={"branch": "main", "steps_log": steps}))
    assert "### 流程轨迹" in md
    # 层级小步骤必须出现（不能退化成扁平的 [步骤1]）
    assert "[步骤1.1]" in md
    assert "[步骤1.2]" in md
    assert "[步骤2.1]" in md
    assert "[步骤2.2]" in md
    # 不应再出现扁平无小数点的 [步骤1] / [步骤2]
    assert "[步骤1]" not in md
    assert "[步骤2]" not in md


def test_render_falls_back_when_no_test_artifacts():
    md = render_report(_report())
    assert "未生成测试用例" in md or "TC-" not in md
    assert "未生成测试代码" in md


def test_code_fence_escapes_nested_backticks():
    e = TestCodeEntry(path="x.py", content="```python\nprint(1)\n```")
    assert len(e.fence) >= 4  # 内容含 ``` 时围栏需更长，才不会撑破


def test_lang_for_maps_extension():
    assert _lang_for("a.py") == "python"
    assert _lang_for("A.java") == "java"
    assert _lang_for("noext") == ""


def test_color_severity_wraps_all_levels():
    html = "<th>P0</th><th>P1</th><th>P2</th><th>P3</th>"
    out = color_severity(html)
    assert '<span class="sev-P0">P0</span>' in out
    assert '<span class="sev-P1">P1</span>' in out
    assert '<span class="sev-P2">P2</span>' in out
    assert '<span class="sev-P3">P3</span>' in out


def test_color_severity_in_heading_and_cell():
    html = '<h4>F-001 · P0 · 标题</h4><td>P1</td>'
    out = color_severity(html)
    assert '<h4>F-001 · <span class="sev-P0">P0</span> · 标题</h4>' in out
    assert '<td><span class="sev-P1">P1</span></td>' in out


def test_color_severity_does_not_wrap_identifiers():
    # 标识符、越界数字、带后缀的写法都不应被误当成严重级别
    html = "someMethodP0() 与 P30 与 P0x 都不应包裹"
    assert color_severity(html) == html


def test_color_severity_idempotent_on_already_wrapped():
    # 对已包裹的 HTML 再次套用不应产生嵌套 span
    once = color_severity("<td>P0</td>")
    assert color_severity(once) == once


def test_render_markdown_keeps_severity_plain():
    # 下载用的 .md 纯文本仍是裸的 P0/P1，配色只在 HTML 后处理阶段加
    f = _finding(severity=Severity.P0)
    md = render_report(_report(findings=[f]))
    assert "| P0 | P1 | P2 | P3 |" in md
    assert "sev-P0" not in md
