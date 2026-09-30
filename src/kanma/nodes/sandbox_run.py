"""步骤 5 · 沙箱运行测试。

铁律：必须真实执行（HR-06），必须在隔离环境执行（HR-07）。
禁止伪造结果；跑不了就如实标记 NOT_RUN，且 NOT_RUN 不计入通过率。

语言差异（命令、环境准备、结果解析、依赖缓存）全部由 `kanma/runners/` 承担。
"""

from __future__ import annotations

import logging
from pathlib import Path

from kanma.models.schemas import (
    ProjectConfig,
    TestArtifact,
    TestCase,
    TestResult,
    TestStatus,
)
from kanma.nodes.step_log import StepLog
from kanma.runners import get_runner
from kanma.runners.base import RunnerContext
from kanma.state import ReviewState
from kanma.tools.sandbox import Sandbox

logger = logging.getLogger(__name__)


def _extract_failure_detail(stdout: str, name: str, limit: int = 800) -> str:
    """抓取失败用例的 traceback / 断言差异片段。"""
    lines = stdout.splitlines()
    for i, line in enumerate(lines):
        if f"_{name}" in line or f"::{name}" in line or f"{name} " in line:
            if any(k in line for k in ("FAILED", "ERROR", "FAIL", "✕", "×")):
                return "\n".join(lines[i : i + 12])[:limit]
    return ""


def sandbox_run(state: ReviewState, sandbox: Sandbox | None = None) -> dict:
    cfg: ProjectConfig = state["config"]
    artifacts: list[TestArtifact] = state.get("test_artifacts", [])
    cases: list[TestCase] = state.get("test_cases", [])

    if sandbox is None:
        from kanma.tools.sandbox import pick_sandbox

        sandbox = pick_sandbox()

    log = StepLog(5)
    notes: list[str] = []

    # HR-04（新语义）：无需求文档时步骤 3/4 仍会产出 BEHAVIOR/REGRESSION 用例与测试代码，
    # 本步照常真实执行；此处不再按需求文档设门。跳过判定只看「是否有测试产物」。
    if not artifacts or all(not a.files for a in artifacts):
        reason = next((a.degraded_reason for a in artifacts if a.degraded_reason), "无测试产物") or "无测试产物"
        results = [
            TestResult(case_id=c.id, status=TestStatus.NOT_RUN, message=reason) for c in cases
        ]
        return {
            "test_results": results,
            "steps_log": [log.add(f"跳过：{reason}")],
            "degradation_notes": [f"步骤5 降级：{reason}，功能验证缺失"],
        }

    # 依赖缓存只读挂载：隔离环境无网，缺缓存必然构建失败
    mounts = [d for r in artifacts for d in get_runner(r.language).cache_dirs if Path(d).exists()]
    for repo in cfg.repos:
        rd = cfg.repo_dir_for(repo)
        if rd.exists():
            mounts.append(rd)

    all_results: list[TestResult] = []
    any_ran = False

    total_artifacts = len(artifacts)
    for art_idx, artifact in enumerate(artifacts, start=1):
        # 沙箱执行是易长跑的一步，按工程报「第 i/N 个」让用户看得到推进
        log.progress(art_idx, total_artifacts, f"沙箱执行 {artifact.workspace.name}")
        if not artifact.files:
            reason = artifact.degraded_reason or "该工程无测试产物"
            all_results.extend(
                TestResult(case_id=c.id, status=TestStatus.NOT_RUN, message=reason)
                for c in cases
                if c.repo_name == artifact.workspace.name
            )
            continue

        runner = get_runner(artifact.language)
        runner_ctx = RunnerContext(
            cfg=cfg, sandbox=sandbox, test_dir=artifact.test_dir, workspace=artifact.workspace
        )

        command = runner.prepare(runner_ctx, artifact)
        if not command:
            reason = runner.unavailable_reason or f"语言 {artifact.language.value} 无可用运行命令"
            all_results.extend(
                TestResult(case_id=c.id, status=TestStatus.NOT_RUN, message=reason)
                for c in cases
                if c.repo_name == artifact.workspace.name
            )
            notes.append(f"步骤5 降级（{artifact.workspace.name}）：{reason}，功能验证缺失")
            continue

        log.add(
            f"工程「{artifact.workspace.name}」沙箱后端：{sandbox.resolved_backend}，"
            f"框架 {runner.framework}，超时 {sandbox.timeout}s"
        )

        def _exec() -> "object":
            return sandbox.run(
                command, cwd=artifact.workspace, read_only_mounts=mounts or None
            )

        exec_result = _exec()
        parsed = runner.parse(exec_result.stdout + exec_result.stderr, cases, runner_ctx)

        # 环境类失败（完全没跑起来）最多重试 1 次，仅用于排除环境抖动
        if exec_result.error or (exec_result.exit_code != 0 and not parsed):
            exec_result = _exec()
            parsed = runner.parse(exec_result.stdout + exec_result.stderr, cases, runner_ctx)
            for r in parsed.values():
                r.retried = True
            log.add(f"工程「{artifact.workspace.name}」首次执行异常，已重试 1 次")

        for case_id, r in parsed.items():
            if r.status in (TestStatus.FAILED, TestStatus.ERROR):
                r.log_excerpt = _extract_failure_detail(exec_result.stdout, case_id)
            if r.status is TestStatus.NOT_RUN and not r.message:
                r.message = "用例被跳过或未匹配到执行结果"

        counts: dict[TestStatus, int] = {}
        for r in parsed.values():
            counts[r.status] = counts.get(r.status, 0) + 1
        log.add(
            f"工程「{artifact.workspace.name}」结果："
            + "、".join(f"{s.value}×{n}" for s, n in counts.items() if n)
        )

        if exec_result.error:
            notes.append(
                f"步骤5 降级（{artifact.workspace.name}）：沙箱执行异常"
                f"（{exec_result.error}），相关用例记为 NOT_RUN"
            )
        if exec_result.timed_out:
            notes.append(
                f"步骤5 降级（{artifact.workspace.name}）：沙箱执行超时被强杀，"
                f"未完成用例记为 TIMEOUT"
            )
        ran = sum(1 for r in parsed.values() if r.counted)
        if ran == 0 and parsed:
            notes.append(
                f"步骤5 提示（{artifact.workspace.name}）：所有用例均未真实执行（NOT_RUN），"
                "功能验证为空，结论不可据此认定功能正确"
            )
        any_ran = any_ran or ran > 0
        all_results.extend(parsed.values())

    # 未匹配到任何工程的用例，记为 NOT_RUN 以防漏算
    covered = {r.case_id for r in all_results}
    for c in cases:
        if c.id not in covered:
            all_results.append(
                TestResult(case_id=c.id, status=TestStatus.NOT_RUN, message="用例未纳入任何工程的执行")
            )

    return {"test_results": all_results, "steps_log": log.lines, "degradation_notes": notes}


def make_sandbox_run_node(sandbox: Sandbox) -> object:
    def _node(state: ReviewState) -> dict:
        return sandbox_run(state, sandbox)

    return _node
