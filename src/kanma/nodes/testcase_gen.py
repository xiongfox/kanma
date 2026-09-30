"""步骤 3 · 生成测试用例。

三类用例须齐备（需求符合性 / 变更行为 / 回归防护），
另为 P0、P1 缺陷生成复现用例（先证明问题存在）。
期望输出必须是**可断言的确定值**，禁止"应该正常工作"这类描述。

HR-04（2026-09-16 新语义）：无需求开发文档时**不跳过**本步——仍基于 diff 生成
BEHAVIOR / REGRESSION 用例（测试入口为溯源到的对外接口），仅跳过 REQUIREMENT
需求符合性用例；降级说明统一标注「缺少需求开发文档（HR-04），未做需求符合性核对」。
"""

from __future__ import annotations

import logging
from typing import Any

from langchain_core.messages import HumanMessage, SystemMessage
from pydantic import BaseModel, Field

from kanma.llm import LLMContext, invoke_structured
from kanma.models.schemas import Severity, StaticFinding, TestCase, TestCaseOrigin
from kanma.nodes.step_log import StepLog
from kanma.state import ReviewState
from kanma.utils import chunk_diff, truncate

logger = logging.getLogger(__name__)

SYSTEM_PROMPT = """你是「勘码」，企业级代码评审 Agent，行为以 AGENTS.md 为唯一事实源。现在执行流程第 3 步：生成测试用例。

输入：本次代码变更的 diff、需求开发文档（验收基准）、静态审查发现的缺陷。
目标：把「这次变更应该做什么」变成**可执行的验收条件**，后续会被落成真实运行的测试代码。

## 用例来源（三类，须齐备）
1. REQUIREMENT 需求符合性 —— 直接映射需求开发文档的功能点与验收标准。需求文档是验收基准，此类用例优先级最高，须覆盖关键验收点。
2. BEHAVIOR 变更行为 —— 覆盖 diff 中新增/修改的分支、边界、异常路径。
3. REGRESSION 回归防护 —— 覆盖被修改逻辑的既有行为，防止改坏。

> HR-04：若需求开发文档缺失（下方文档为空），**不得**生成 REQUIREMENT 用例，
> 只产出 BEHAVIOR / REGRESSION 用例；不得臆造需求点。
>
> 备注：针对静态审查中标记为 P0/P1 的缺陷，系统会**自动追加** REPRODUCE 复现用例（先证明问题存在），你**无需**生成此类用例；你只需保证上述齐备。

## 硬性要求
- 每条用例必须写全以下字段：标题、前置条件（precondition）、输入（input）、期望输出（expected）、关联点（requirement_ref / diff_ref）：
  - REQUIREMENT：requirement_ref 必须引用需求文档中的**具体条款或验收标准原文**，禁止写"需求相关""见需求"等空泛表述。
  - BEHAVIOR / REGRESSION：diff_ref 必须写成 `文件路径:行号`，定位到 diff 中实际变更的行（HR-02 证据铁律）。
- 期望输出（expected）必须是**可断言的确定值**（具体返回值、明确异常类型、明确状态码、明确业务状态），
  禁止"应该正常工作""不报错""符合预期"这类无法断言的描述。
- 用例之间**不得有顺序依赖**，每条都能独立运行。
- 少而准：每条用例要么对应一个需求验收点、要么对应一个真实变更分支/边界，禁止为凑数生成无意义用例。

## 禁止
- 禁止臆造不存在的需求点或 diff 位置（无证据链的"疑似"不写）。
- 禁止在缺少需求文档时生成 REQUIREMENT 用例或声称需求验证通过（HR-04）。
"""


class _LLMTestCase(BaseModel):
    title: str
    origin: TestCaseOrigin
    precondition: str = ""
    input_desc: str = ""
    expected: str = Field(description="可断言的确定期望结果")
    requirement_ref: str = ""
    diff_ref: str = ""
    finding_id: str | None = None


class _LLMTestCases(BaseModel):
    cases: list[_LLMTestCase]


def _fallback_cases(state: ReviewState) -> list[TestCase]:
    """LLM 不可用时的兜底：基于各工程 diff 推断最小回归用例集（标注为推断）。"""
    cases: list[TestCase] = []
    seq = 0
    for d in state.get("diffs", []):
        for fd in d.reviewable_files:
            seq += 1
            lines = fd.all_added_line_numbers[:5]
            cases.append(
                TestCase(
                    id=f"TC-{seq:03d}",
                    title=f"[推断] {fd.path} 变更基本可用（回归）",
                    origin=TestCaseOrigin.REGRESSION,
                    repo_name=d.repo_name,
                    input_desc="按变更函数签名构造典型入参",
                    expected="不抛异常，返回值符合函数既有契约",
                    diff_ref=f"{fd.path}:{lines[0] if lines else 0}",
                )
            )
    return cases


def _reproduce_cases(findings: list[StaticFinding]) -> list[TestCase]:
    """为 P0/P1 缺陷生成复现用例。"""
    cases: list[TestCase] = []
    seq = 0
    for f in findings:
        if f.severity not in (Severity.P0, Severity.P1):
            continue
        seq += 1
        cases.append(
            TestCase(
                id=f"TC-R{seq:03d}",
                title=f"[复现] {f.id} · {f.title}",
                origin=TestCaseOrigin.REPRODUCE,
                repo_name=f.repo_name,
                precondition=f"命中位置 {f.file}:{f.line}",
                input_desc=f"构造能触发该问题的输入（证据：{f.evidence[:120]}）",
                expected="修复前：问题可复现（测试失败）；修复后：问题消失（测试通过）",
                diff_ref=f"{f.file}:{f.line}",
                finding_id=f.id,
            )
        )
    return cases


def _llm_cases(ctx: LLMContext, diff_chunk: str, requirement_text: str) -> list[TestCase]:
    messages = [
        SystemMessage(content=SYSTEM_PROMPT),
        HumanMessage(
            content=(
                f"## 代码 diff\n\n{diff_chunk}\n\n"
                f"## 需求开发文档\n\n{truncate(requirement_text, 6000) or '（内容为空，请勿生成 REQUIREMENT 用例，仅产出 BEHAVIOR/REGRESSION，requirement_ref 留空）'}"
            )
        ),
    ]
    result: _LLMTestCases = invoke_structured(ctx, _LLMTestCases, messages)
    return [
        TestCase(
            id="",
            title=c.title,
            origin=c.origin,
            precondition=c.precondition,
            input_desc=c.input_desc,
            expected=c.expected,
            requirement_ref=c.requirement_ref,
            diff_ref=c.diff_ref,
            finding_id=c.finding_id,
        )
        for c in result.cases
    ]


def testcase_gen(state: ReviewState, ctx: LLMContext | None = None) -> dict:
    findings = state.get("findings", [])
    requirement = state["config"].requirement

    if ctx is None:
        from kanma.llm import get_llm

        ctx = get_llm()

    log = StepLog(3)
    notes: list[str] = []
    cases: list[TestCase] = []

    # HR-04（新语义）：无需求开发文档时**不跳过**本步——仍基于 diff 生成
    # BEHAVIOR / REGRESSION 用例（LLM 提示词在文档为空时被明确禁止产出 REQUIREMENT 用例），
    # 仅跳过「需求符合性用例」，并在第 7 章降级说明统一标注。
    requirement_available = bool(requirement.available)
    if not requirement_available:
        notes.append("步骤3：缺少需求开发文档（HR-04），未做需求符合性核对，仅生成变更行为/回归用例")
        log.add("缺少需求开发文档（HR-04）：跳过 REQUIREMENT 用例，仅生成 BEHAVIOR/REGRESSION 用例")

    diffs = state.get("diffs", [])
    if all(d.is_empty for d in diffs):
        return {
            "test_cases": [],
            "steps_log": [log.add("所有工程 diff 为空，跳过用例生成")],
            "degradation_notes": notes,
        }

    def _gen_for_diff(d) -> list[TestCase]:
        """为单个工程生成用例（LLM 优先，失败则推断）。"""
        if ctx.available:
            try:
                generated: list[TestCase] = []
                for chunk in chunk_diff(d):
                    generated.extend(_llm_cases(ctx, chunk, requirement.raw_text))
                for c in generated:
                    c.repo_name = d.repo_name
                return generated
            except Exception as e:
                logger.warning("用例生成失败（%s）：%s", d.repo_name, e)
                notes.append(
                    f"步骤3 降级（{d.repo_name}）：LLM 用例生成失败（{e}），改用推断用例"
                )
        # 离线/失败：基于该工程 diff 推断
        fallback = [
            c for c in _fallback_cases(state) if c.repo_name == d.repo_name
        ]
        if not ctx.available:
            notes.append(
                f"步骤3 降级（{d.repo_name}）：{ctx.degradation_note}，改用推断用例"
            )
        return fallback

    todo = [d for d in diffs if not d.is_empty]
    total_diffs = len(todo)
    for idx, d in enumerate(todo, start=1):
        log.progress(idx, total_diffs, f"生成用例 {d.repo_name}")
        cases.extend(_gen_for_diff(d))

    log.add(f"生成 {len(cases)} 条用例")

    cases.extend(_reproduce_cases(findings))
    # 统一编号
    for i, c in enumerate(cases, start=1):
        c.id = f"TC-{i:03d}"

    # 用例齐全性检查：有需求文档时三类齐备；无需求文档时 REQUIREMENT 缺席属预期，不提示
    origins = {c.origin for c in cases}
    expected_origins = (
        (TestCaseOrigin.REQUIREMENT, TestCaseOrigin.BEHAVIOR, TestCaseOrigin.REGRESSION)
        if requirement_available
        else (TestCaseOrigin.BEHAVIOR, TestCaseOrigin.REGRESSION)
    )
    missing = [o.value for o in expected_origins if o not in origins]
    if missing:
        notes.append(f"步骤3 提示：缺少以下来源的用例 → {'、'.join(missing)}")

    log.add(
        "用例分布："
        + "、".join(f"{o.value}×{sum(1 for c in cases if c.origin is o)}" for o in origins)
    )
    return {"test_cases": cases, "steps_log": log.lines, "degradation_notes": notes}


def make_testcase_node(ctx: LLMContext) -> Any:
    def _node(state: ReviewState) -> dict:
        return testcase_gen(state, ctx)

    return _node
