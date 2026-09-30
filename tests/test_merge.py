"""步骤 6 合并规则与结论判定测试（HR-10）。"""

from kanma.models.schemas import (
    ReviewDimension,
    ReviewVerdict,
    Severity,
    StaticFinding,
    TestCase,
    TestCaseOrigin,
    TestResult,
    TestStatus,
)
from kanma.nodes.report_merge import (
    build_requirement_checks,
    decide_verdict,
    extract_requirement_points,
    merge_findings_with_tests,
)


def _finding(**kw) -> StaticFinding:
    base = dict(
        id="SEC-001",
        dimension=ReviewDimension.SECURITY,
        severity=Severity.P1,
        rule_id="HR-05-SECRET",
        file="a.py",
        line=10,
        title="硬编码密钥",
        evidence="TOKEN = 'x'",
        reason="明文密钥",
        suggestion="改用环境变量",
    )
    base.update(kw)
    return StaticFinding(**base)


def test_reproduce_failed_escalates_finding():
    f = _finding(severity=Severity.P1)
    cases = [TestCase(id="TC-001", title="复现", origin=TestCaseOrigin.REPRODUCE,
                      expected="修复后通过", finding_id="SEC-001")]
    results = [TestResult(case_id="TC-001", status=TestStatus.FAILED)]
    merged = merge_findings_with_tests([f], cases, results)
    assert merged[0].severity is Severity.P0
    assert merged[0].confirmed_by_test is True


def test_reproduce_passed_demotes_to_p3_and_flags_human():
    f = _finding(severity=Severity.P1)
    cases = [TestCase(id="TC-001", title="复现", origin=TestCaseOrigin.REPRODUCE,
                      expected="修复后通过", finding_id="SEC-001")]
    results = [TestResult(case_id="TC-001", status=TestStatus.PASSED)]
    merged = merge_findings_with_tests([f], cases, results)
    assert merged[0].severity is Severity.P3
    assert merged[0].needs_human is True


def test_failed_case_without_finding_adds_p1():
    cases = [TestCase(id="TC-002", title="满减计算", origin=TestCaseOrigin.REQUIREMENT,
                      expected="优惠 10 元", diff_ref="a.py:20")]
    results = [TestResult(case_id="TC-002", status=TestStatus.FAILED, message="期望10实际0")]
    merged = merge_findings_with_tests([], cases, results)
    assert len(merged) == 1
    assert merged[0].severity is Severity.P1
    assert merged[0].rule_id == "HR-04"


def test_verdict_mapping():
    v, _ = decide_verdict([_finding(severity=Severity.P0)], [])
    assert v is ReviewVerdict.REJECT

    v, _ = decide_verdict([_finding(severity=Severity.P1)], [])
    assert v is ReviewVerdict.REQUEST_CHANGES

    v, _ = decide_verdict([_finding(severity=Severity.P2)], [])
    assert v is ReviewVerdict.SUGGEST_CHANGES

    v, _ = decide_verdict([], [])
    assert v is ReviewVerdict.APPROVE

    # 无缺陷但有流程降级 → 不得直接判通过
    v, _ = decide_verdict([], ["步骤5 降级：沙箱不可用"])
    assert v is ReviewVerdict.SUGGEST_CHANGES


def test_requirement_points_extraction():
    text = "# 需求\n\n- 满100减10\n- 满200减30\n正文说明不算需求点\n"
    points = extract_requirement_points(text)
    assert points == ["满100减10", "满200减30"]


def test_requirement_checks_map_case_result():
    text = "- 满100减10\n"
    cases = [TestCase(id="TC-001", title="验证满100减10", origin=TestCaseOrigin.REQUIREMENT,
                      expected="优惠10", requirement_ref="满100减10")]
    results = [TestResult(case_id="TC-001", status=TestStatus.PASSED)]
    checks = build_requirement_checks(text, cases, results)
    assert len(checks) == 1
    assert checks[0].passed is True
    assert checks[0].case_ids == ["TC-001"]


def test_not_run_not_counted_in_pass_rate():
    r = TestResult(case_id="TC-001", status=TestStatus.NOT_RUN)
    assert r.counted is False
    assert TestResult(case_id="TC-002", status=TestStatus.PASSED).counted is True


def _tmp_config_and_workspace(name: str):
    """构造最小 ProjectConfig + 含测试文件的测试工作台。"""
    import tempfile
    from datetime import datetime
    from pathlib import Path

    from kanma.models.schemas import CodeLanguage, ProjectConfig, SourceRepo, TestArtifact

    ws = Path(tempfile.mkdtemp()) / name
    ws.mkdir(parents=True, exist_ok=True)
    (ws / "test_x.py").write_text("def test_x():\n    assert 1 == 1\n", encoding="utf-8")
    repo = SourceRepo(
        name=name,
        git_url=f"https://g/{name}.git",
        branch="main",
        start_time=datetime(2026, 1, 1),
        end_time=datetime(2026, 1, 2),
    )
    cfg = ProjectConfig(name="p", repos=[repo])
    art = TestArtifact(
        language=CodeLanguage.PYTHON,
        framework="pytest",
        test_dir=ws,
        workspace=ws,
        files=["test_x.py"],
        case_ids=["TC-001"],
    )
    return cfg, art


def test_collect_test_code_reads_disk_and_marks_unexecuted():
    from kanma.nodes.report_merge import _collect_test_code

    cfg, art = _tmp_config_and_workspace("order-svc")
    entries = _collect_test_code(cfg, [art], [])  # 无任何执行结果
    assert len(entries) == 1
    e = entries[0]
    assert e.path == "test_x.py"
    assert "def test_x()" in e.content
    assert e.executed is False  # 未执行也应保留内容
    assert e.repo_name == "order-svc"


def test_collect_test_code_marks_executed_when_result_present():
    from kanma.nodes.report_merge import _collect_test_code

    cfg, art = _tmp_config_and_workspace("pay-svc")
    results = [TestResult(case_id="TC-001", status=TestStatus.PASSED)]
    entries = _collect_test_code(cfg, [art], results)
    assert entries[0].executed is True


def test_collect_test_code_handles_missing_file():
    from kanma.nodes.report_merge import _collect_test_code
    from kanma.models.schemas import CodeLanguage, TestArtifact

    cfg, art = _tmp_config_and_workspace("gap-svc")
    # 指向不存在的文件：不应抛异常，应保留错误占位
    art.files = ["ghost.py"]
    entries = _collect_test_code(cfg, [art], [])
    assert entries[0].content.startswith("（无法读取测试文件")
