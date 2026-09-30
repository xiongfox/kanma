"""README 上下文链路测试。

校验 AGENTS.MD「步骤 1 抽取 README → 步骤 2 注入审查提示词 → 报告第 7 章声明缺失」：

1. `extract_readme` 的查找优先级（根目录 README.md → README*.md → 一级子目录 README）与缺失处理；
2. `collect_diff_bundle` 把 README 摘要载入 `DiffBundle`（readme / readme_absent）；
3. 步骤 2 的 `_llm_review` 把 README 作为「项目概况」注入提示词；
4. 多工程时逐工程读取、各自传对应 README，绝不串用；
5. 报告第 7 章渲染 README 缺失声明，且不因此误判为流程降级（verdict 不被影响）。
"""
from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from kanma.llm import LLMContext
from kanma.models.schemas import DiffBundle, ReviewReport, ReviewVerdict
from kanma.report.renderer import render_report
from kanma.tools.git_tool import README_LIMIT, collect_diff_bundle, extract_readme


# ---------------------------------------------------------------------------
# 1. extract_readme 查找优先级
# ---------------------------------------------------------------------------
def _git(args: list[str], cwd: Path) -> None:
    subprocess.run(["git", *args], cwd=str(cwd), capture_output=True, text=True, check=True)


def test_extract_readme_prefers_root_readme(tmp_path: Path):
    (tmp_path / "README.md").write_text("# 订单服务\n这是订单服务说明", encoding="utf-8")
    assert "订单服务" in extract_readme(tmp_path)


def test_extract_readme_falls_back_to_glob(tmp_path: Path):
    # 根目录没有 README.md，但有 README-zh.md
    (tmp_path / "README-zh.md").write_text("项目中文说明", encoding="utf-8")
    (tmp_path / "order.py").write_text("x=1", encoding="utf-8")
    assert "项目中文说明" in extract_readme(tmp_path)


def test_extract_readme_falls_back_to_subdir(tmp_path: Path):
    sub = tmp_path / "docs"
    sub.mkdir()
    (sub / "README.md").write_text("子目录说明", encoding="utf-8")
    assert "子目录说明" in extract_readme(tmp_path)


def test_extract_readme_returns_empty_when_absent(tmp_path: Path):
    (tmp_path / "main.py").write_text("x=1", encoding="utf-8")
    assert extract_readme(tmp_path) == ""


def test_extract_readme_truncates_huge_file(tmp_path: Path):
    (tmp_path / "README.md").write_text("A" * (README_LIMIT * 3), encoding="utf-8")
    out = extract_readme(tmp_path)
    assert len(out) >= README_LIMIT
    assert "已截断" in out


# ---------------------------------------------------------------------------
# 2. collect_diff_bundle 载入 README
# ---------------------------------------------------------------------------
def _make_repo(tmp_path: Path, name: str, with_readme: bool) -> Path:
    repo = tmp_path / name
    repo.mkdir()
    _git(["init", "-b", "main"], repo)
    _git(["config", "user.email", "k@e.com"], repo)
    _git(["config", "user.name", "k"], repo)
    (repo / "svc.py").write_text("def f():\n    return 1\n", encoding="utf-8")
    if with_readme:
        (repo / "README.md").write_text("# 支付网关\n本工程是支付网关", encoding="utf-8")
    _git(["add", "."], repo)
    _git(["commit", "-m", "init"], repo)
    return repo


def _config_for(repo: Path, tmp_path: Path):
    from datetime import datetime, timedelta

    from kanma.models.schemas import ProjectConfig, SourceRepo

    now = datetime.now()
    return ProjectConfig(
        name="t",
        repos=[
            SourceRepo(
                name="svc",
                git_url=str(repo),
                branch="main",
                start_time=now - timedelta(days=1),
                end_time=now + timedelta(days=1),
            )
        ],
        workdir=tmp_path / "w",
    )


def test_collect_attaches_readme_when_present(tmp_path: Path):
    repo = _make_repo(tmp_path, "r1", with_readme=True)
    diff = collect_diff_bundle(_config_for(repo, tmp_path))
    assert diff.readme_absent is False
    assert "支付网关" in diff.readme


def test_collect_marks_readme_absent_when_missing(tmp_path: Path):
    repo = _make_repo(tmp_path, "r2", with_readme=False)
    diff = collect_diff_bundle(_config_for(repo, tmp_path))
    assert diff.readme == ""
    assert diff.readme_absent is True


# ---------------------------------------------------------------------------
# 3. _llm_review 把 README 注入提示词
# ---------------------------------------------------------------------------
def test_llm_review_injects_readme_into_prompt(monkeypatch, tmp_path):
    import sys

    import kanma.nodes.static_review  # noqa: F401

    sr = sys.modules["kanma.nodes.static_review"]
    from kanma.nodes.static_review import _LLMSemanticFindings

    captured = {}

    def fake_invoke(ctx, schema, messages):
        captured["messages"] = messages
        return _LLMSemanticFindings(findings=[])

    monkeypatch.setattr(sr, "invoke_structured", fake_invoke)

    ctx = LLMContext(True, "mock")
    readme_text = "本工程是**支付网关**，对外暴露 /pay 接口"
    sr._llm_review(
        ctx,
        "diff ...",
        "需求文档...",
        repo_name="pay-svc",
        readme=readme_text,
    )

    human = captured["messages"][1].content
    assert "项目概况" in human
    assert readme_text in human


def test_llm_review_falls_back_when_no_readme(monkeypatch):
    import sys

    import kanma.nodes.static_review  # noqa: F401

    sr = sys.modules["kanma.nodes.static_review"]
    from kanma.nodes.static_review import _LLMSemanticFindings

    captured = {}

    def fake_invoke(ctx, schema, messages):
        captured["messages"] = messages
        return _LLMSemanticFindings(findings=[])

    monkeypatch.setattr(sr, "invoke_structured", fake_invoke)

    ctx = LLMContext(True, "mock")
    sr._llm_review(ctx, "diff ...", "需求文档...", repo_name="x")

    human = captured["messages"][1].content
    assert "未提供 README" in human


# ---------------------------------------------------------------------------
# 4. review_chapter 逐工程传各自 README（不串用）
# ---------------------------------------------------------------------------
def _sr_module():
    import sys

    import kanma.nodes.static_review  # noqa: F401

    return sys.modules["kanma.nodes.static_review"]


def test_review_chapter_passes_per_repo_readme(monkeypatch):
    sr = _sr_module()
    from types import SimpleNamespace

    calls: list[dict] = []

    def rec(ctx, diff_text, requirement_text, repo_name="", guide="", guide_title="", focus=(), readme=""):
        calls.append({"repo": repo_name, "readme": readme})
        return []

    monkeypatch.setattr(sr, "_llm_review", rec)
    monkeypatch.setattr(sr, "chunk_diff", lambda d: ["diff 片段"])
    monkeypatch.setattr(sr, "_diff_languages", lambda d: {"java"})

    class _Diff:
        is_empty = False
        repo_name = "pay-svc"
        reviewable_files = []
        readme = "# 支付网关\n本工程是支付网关"

    state = {
        "config": SimpleNamespace(requirement=SimpleNamespace(raw_text="")),
        "diffs": [_Diff()],
    }
    sr.review_chapter(state, LLMContext(True, "mock"), "03-security-standards", "三、安全规约")

    assert calls, "章节节点没有发起审查"
    assert all(c["readme"] == _Diff.readme for c in calls), "README 未逐工程正确传递"


# ---------------------------------------------------------------------------
# 5. 报告第 7 章呈现 README 缺失，且不影响结论判定
# ---------------------------------------------------------------------------
def test_report_renders_readme_notes():
    report = ReviewReport(
        project="t",
        generated_at=__import__("datetime").datetime.now(),
        verdict=ReviewVerdict.APPROVE,
        verdict_reason="无缺陷",
        readme_notes=["工程「pay-svc」未提供 README.md，步骤2 静态审查在无项目上下文下进行"],
    )
    md = render_report(report, has_requirement=False)
    assert "项目上下文（README）" in md
    assert "未提供 README" in md


def test_readme_absence_does_not_downgrade_verdict():
    """README 缺失只是声明，不是流程降级 —— 无缺陷时结论仍应为「通过」。"""
    from kanma.nodes.report_merge import decide_verdict

    # decide_verdict 的 notes 不含 readme_notes，故缺失 README 不会把 APPROVE 拉成 SUGGEST_CHANGES
    verdict, _reason = decide_verdict([], [])
    assert verdict is ReviewVerdict.APPROVE
