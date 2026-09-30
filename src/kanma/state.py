"""LangGraph 状态定义。

状态里出现的每个 key 都在 models/schemas.py 里有对应契约；
steps_log 用于记录流程执行轨迹，满足 HR-09 的「降级必须显式声明」。
"""

from __future__ import annotations

from typing import Annotated, Any

from langgraph.graph.message import add_messages
from typing_extensions import TypedDict

from kanma.models.schemas import (
    DiffBundle,
    ProjectConfig,
    ReviewReport,
    StaticFinding,
    TestArtifact,
    TestCase,
    TestResult,
)


def merge_list(left: list[Any], right: list[Any]) -> list[Any]:
    """列表累加 reducer：节点返回的部分结果会被追加到已有结果上。"""
    return left + right


class ReviewState(TypedDict, total=False):
    # 输入
    config: ProjectConfig

    # 步骤 1 → 2（每个源码工程一份 diff）
    diffs: Annotated[list[DiffBundle], merge_list]

    # 步骤 2 六个并发维度节点的原始产出（汇总节点去重编号后才写进 findings）
    # —— findings 走累加 reducer，各维度直接写它就无法再统一重写，故分两个通道。
    raw_findings: Annotated[list[StaticFinding], merge_list]

    # 步骤 2 → 3 / 6（去重 + 统一编号后的最终缺陷清单）
    findings: Annotated[list[StaticFinding], merge_list]

    # 步骤 3 → 4
    test_cases: Annotated[list[TestCase], merge_list]

    # 步骤 4 → 5（每个源码工程一份测试工件）
    test_artifacts: Annotated[list[TestArtifact], merge_list]

    # 步骤 5 → 6
    test_results: Annotated[list[TestResult], merge_list]

    # 步骤 6
    report: ReviewReport

    # 流程轨迹与降级说明（HR-09）
    steps_log: Annotated[list[str], merge_list]
    degradation_notes: Annotated[list[str], merge_list]

    # LLM 对话消息轨迹
    messages: Annotated[list[Any], add_messages]
