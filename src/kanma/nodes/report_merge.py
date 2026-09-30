"""步骤 6 · 合并静态审查与测试结果，生成最终报告。

合并规则（AGENTS.MD 步骤 6）：
- 有静态问题 + 复现用例 FAILED → 问题确认，级别上调一档（上限 P0）
- 有静态问题 + 复现用例 PASSED → 问题存疑，降级 P3 并标 NEEDS_HUMAN
- 无静态问题 + 用例 FAILED → 新增 P1：代码行为与需求不符
结论四选一（HR-10）。
"""

from __future__ import annotations

import re
from datetime import datetime
from pathlib import Path

from kanma.llm import LLMContext
from kanma.models.schemas import (
    RequirementCheckItem,
    ReviewDimension,
    ReviewReport,
    ReviewVerdict,
    Severity,
    StaticFinding,
    TestCase,
    TestCodeEntry,
    TestResult,
    TestStatus,
)
from kanma.nodes.step_log import StepLog
from kanma.report.renderer import render_report
from kanma.rules.scan import sort_findings
from kanma.state import ReviewState

# 需求点：以 -、*、数字. 或「需求点 N」开头的行
_REQ_POINT = re.compile(r"^\s*(?:[-*]\s+|\d+[.、)]\s+)(?P<text>\S.{2,120})$")


def extract_requirement_points(text: str, limit: int = 40) -> list[str]:
    if not text.strip():
        return []
    points: list[str] = []
    for line in text.splitlines():
        m = _REQ_POINT.match(line)
        if m:
            t = m.group("text").strip().rstrip("。.")
            if t and t not in points:
                points.append(t)
        if len(points) >= limit:
            break
    return points


def merge_findings_with_tests(
    findings: list[StaticFinding],
    test_cases: list[TestCase],
    test_results: list[TestResult],
) -> list[StaticFinding]:
    """按三条合并规则做交叉印证，返回新的 finding 列表。"""
    case_by_id = {c.id: c for c in test_cases}
    result_by_case = {tr.case_id: tr for tr in test_results}

    # finding_id → 复现用例执行结果
    for f in findings:
        repro_cases = [c for c in test_cases if c.finding_id == f.id]
        if not repro_cases:
            continue
        statuses = [result_by_case.get(c.id) for c in repro_cases]
        statuses = [s for s in statuses if s is not None]
        if not statuses:
            continue
        if any(s.status is TestStatus.FAILED or s.status is TestStatus.ERROR for s in statuses):
            f.confirmed_by_test = True
            f.escalate()
            f.reason += "（复现用例执行失败，问题已确认，级别上调一档）"
        elif all(s.status is TestStatus.PASSED for s in statuses):
            f.confirmed_by_test = False
            f.demote()
            f.reason += "（复现用例执行通过，问题存疑，需人工确认）"

    merged = list(findings)

    # 无静态问题但用例失败 → 新增 P1
    covered_files = {(f.file, f.line) for f in findings}
    seq = 0
    for tr in test_results:
        if tr.status not in (TestStatus.FAILED, TestStatus.ERROR):
            continue
        case = case_by_id.get(tr.case_id)
        if case is None:
            continue
        file_part, _, line_part = (case.diff_ref or "").rpartition(":")
        try:
            line = int(line_part)
        except ValueError:
            line = 0
        if (file_part, line) in covered_files:
            continue
        seq += 1
        merged.append(
            StaticFinding(
                id=f"TEST-{seq:03d}",
                dimension=ReviewDimension.CORRECTNESS,
                severity=Severity.P1,
                rule_id="HR-04",
                file=file_part or "-",
                line=line,
                title=f"用例 {case.id} 未通过：代码行为与需求不符",
                evidence=tr.log_excerpt[:300],
                reason=f"用例「{case.title}」执行结果为 {tr.status.value}，说明变更后的行为不满足预期：{case.expected}",
                suggestion="对照需求文档核对被测逻辑，修复后重新执行该用例",
                confirmed_by_test=True,
            )
        )
    return sort_findings(merged)


def build_requirement_checks(
    requirement_text: str, test_cases: list[TestCase], test_results: list[TestResult]
) -> list[RequirementCheckItem]:
    """需求点 → 用例 → 结果。需求文档缺失时返回空，由报告标注原因。"""
    points = extract_requirement_points(requirement_text)
    if not points:
        return []
    result_by_case = {tr.case_id: tr for tr in test_results}
    items: list[RequirementCheckItem] = []
    for p in points:
        key = p[:12]
        related = [c for c in test_cases if key and key in (c.requirement_ref + c.title)]
        if not related:
            items.append(
                RequirementCheckItem(requirement_point=p, note="无对应用例覆盖")
            )
            continue
        cases_results = [result_by_case.get(c.id) for c in related]
        passed = all(
            r is not None and r.status is TestStatus.PASSED for r in cases_results if r
        ) and any(r is not None for r in cases_results)
        items.append(
            RequirementCheckItem(
                requirement_point=p,
                case_ids=[c.id for c in related],
                passed=passed,
                note="" if any(r is not None for r in cases_results) else "用例未执行",
            )
        )
    return items


def decide_verdict(
    findings: list[StaticFinding],
    report_notes: list[str],
    requirement_available: bool = True,
) -> tuple[ReviewVerdict, str]:
    """HR-10：结论必须四选一。

    HR-04 护栏：缺少需求开发文档时未做需求符合性核对，**不得**判定「通过」——
    即使无缺陷、测试全绿，也只能给出「建议修改后合入」并注明降级原因。
    """
    p0 = [f for f in findings if f.severity is Severity.P0]
    p1 = [f for f in findings if f.severity is Severity.P1]
    p2p3 = [f for f in findings if f.severity in (Severity.P2, Severity.P3)]

    if p0:
        return ReviewVerdict.REJECT, (
            f"存在 {len(p0)} 个 P0 级问题（"
            + "、".join(f"{f.id} {f.title}" for f in p0[:3])
            + "），禁止合入"
        )
    if p1:
        return ReviewVerdict.REQUEST_CHANGES, (
            f"无 P0，但存在 {len(p1)} 个 P1 级问题需修复后合入"
        )
    if p2p3:
        return ReviewVerdict.SUGGEST_CHANGES, f"仅存在 {len(p2p3)} 个非阻塞问题，建议修改后合入"
    if not requirement_available:
        return ReviewVerdict.SUGGEST_CHANGES, (
            "缺少需求开发文档（HR-04），未做需求符合性核对，功能验证可信度降级，"
            "不得判定通过，建议人工复核后合入"
        )
    if report_notes:
        return ReviewVerdict.SUGGEST_CHANGES, (
            "静态审查未发现缺陷，但流程存在降级项（见第 7 章），"
            "结论可信度受限，建议人工复核后合入"
        )
    return ReviewVerdict.APPROVE, "未发现缺陷且测试全部通过"


def _collect_test_code(
    cfg: ProjectConfig,
    artifacts: list,
    test_results: list[TestResult],
) -> list[TestCodeEntry]:
    """从测试工作台读取步骤 4 生成的测试文件内容，随报告持久化。

    - 内容在报告生成时即时从磁盘读取，因此即便第 5 步未执行（NOT_RUN）或降级，
      生成的测试代码仍随报告保留，供人工复核（见 AGENTS.MD 步骤 5）。
    - `executed` 依据该工程覆盖的用例是否有非 NOT_RUN 的执行结果判定。
    - 工作台文件已被清理时，记录可读的错误占位，不抛异常。
    """
    from kanma.models.schemas import _safe_name

    executed_case_ids = {
        tr.case_id for tr in test_results if tr.status is not TestStatus.NOT_RUN
    }
    safe_to_name = {_safe_name(r.display_name): r.display_name for r in cfg.repos}
    entries: list[TestCodeEntry] = []
    for a in artifacts:
        if not getattr(a, "files", None):
            continue
        ws = Path(getattr(a, "workspace", None) or getattr(a, "test_dir", None) or ".")
        repo_name = safe_to_name.get(ws.name, ws.name)
        executed = bool(set(getattr(a, "case_ids", [])) & executed_case_ids) if getattr(a, "case_ids", None) else False
        for rel in a.files:
            p = ws / rel
            try:
                content = p.read_text(encoding="utf-8")
            except Exception:
                content = f"（无法读取测试文件：{p}，可能已被清理）"
            entries.append(
                TestCodeEntry(
                    repo_name=repo_name,
                    path=rel,
                    framework=getattr(a, "framework", "") or "",
                    content=content,
                    case_ids=list(getattr(a, "case_ids", []) or []),
                    executed=executed,
                )
            )
    return entries


def report_merge(
    state: ReviewState,
    output_dir: Path | None = None,
    ctx: LLMContext | None = None,
) -> dict:
    cfg = state["config"]
    diffs = state.get("diffs", [])
    findings = list(state.get("findings", []))
    test_cases = list(state.get("test_cases", []))
    test_results = list(state.get("test_results", []))
    notes = list(state.get("degradation_notes", []))
    steps_log = list(state.get("steps_log", []))

    merged_findings = merge_findings_with_tests(findings, test_cases, test_results)
    requirement_checks = build_requirement_checks(
        cfg.requirement.raw_text, test_cases, test_results
    )
    verdict, verdict_reason = decide_verdict(
        merged_findings, notes, requirement_available=cfg.requirement.available
    )

    # README 缺失声明：仅在第 7 章展示，**不**并入 decide_verdict 的 notes，
    # 否则会让「无缺陷但无 README」误判为 SUGGEST_CHANGES（README 缺失不是流程降级）。
    readme_notes = [
        f"工程「{d.repo_name or cfg.name}」未提供 README.md，步骤2 静态审查在无项目上下文下进行"
        f"（不影响结论判定，但理解深度受限）"
        for d in diffs
        if getattr(d, "readme_absent", False)
    ]

    # 多工程聚合概览
    all_files = [fd for d in diffs for fd in d.reviewable_files]
    all_skipped = [s for d in diffs for s in d.skipped_files]
    all_commits = [c for d in diffs for c in d.commits]
    repos_summary = []
    for repo in cfg.repos:
        repos_summary.append(
            {
                "name": repo.display_name,
                "git_url": repo.git_url,
                "branch": repo.branch,
                "window": f"{repo.start_time:%Y-%m-%d %H:%M} ~ {repo.end_time:%Y-%m-%d %H:%M}",
            }
        )
    artifacts = state.get("test_artifacts") or []
    frameworks = sorted({a.framework for a in artifacts if a.framework})
    # 读取步骤 4 生成的测试代码内容（即使未执行也随报告保留）
    test_code = _collect_test_code(cfg, artifacts, test_results)
    summary = {
        "git_url": cfg.repos[0].git_url if cfg.repos else "",
        "branch": cfg.repos[0].branch if cfg.repos else "",
        "window": repos_summary[0]["window"] if repos_summary else "",
        "repos": repos_summary,
        "repo_count": len(repos_summary),
        "commit_count": len(all_commits),
        "file_count": len(all_files),
        "skipped_count": len(all_skipped),
        "added": sum(d.total_added for d in diffs),
        "removed": sum(d.total_removed for d in diffs),
        "sandbox": ",".join(frameworks) if frameworks else "-",
        "llm": ctx.to_dict() if ctx is not None else {"available": False, "label": "未指定"},
        "steps_log": steps_log,
    }

    report = ReviewReport(
        project=cfg.name,
        generated_at=datetime.now(),
        verdict=verdict,
        verdict_reason=verdict_reason,
        summary=summary,
        findings=merged_findings,
        test_cases=test_cases,
        test_results=test_results,
        test_code=test_code,
        requirement_checks=requirement_checks,
        out_of_scope=[],
        degradation_notes=notes,
        readme_notes=readme_notes,
    )
    report.markdown = render_report(
        report,
        has_requirement=cfg.requirement.available,
        requirement_doc_title=cfg.requirement.title if cfg.requirement.available else "",
    )

    out_dir = Path(output_dir or Path("docs"))
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / f"review-report-{cfg.name}-{datetime.now():%Y%m%d%H%M%S}.md"
    out_path.write_text(report.markdown, encoding="utf-8")

    return {
        "report": report,
        "steps_log": [
            StepLog(6).add(f"结论：{verdict.value}（{verdict_reason}），报告：{out_path}")
        ],
    }
