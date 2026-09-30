"""步骤日志的二级编号（步骤 N.M）。

报告「流程轨迹」与 Web 控制台日志窗靠这个编号定位具体子步骤，
每个节点内的多行日志必须顺序递增、且跨节点不串号。
"""

import re

import pytest

from kanma.nodes.step_log import StepLog

_SUBSTEP = re.compile(r"^\[步骤([1-6])\.(\d+)\] ")


def test_sequential_numbering_within_step():
    log = StepLog(2)
    log.add("语义审查共 14 条")
    log.add("语义型审查补充 3 条")
    assert log.lines == [
        "[步骤2.1] 语义审查共 14 条",
        "[步骤2.2] 语义型审查补充 3 条",
    ]


def test_add_returns_numbered_line_for_inline_use():
    log = StepLog(3)
    line = log.add("diff 为空，跳过")
    assert line == "[步骤3.1] diff 为空，跳过"
    assert log.lines == [line]


def test_each_node_counts_independently():
    a = StepLog(1)
    b = StepLog(2)
    a.add("第一条")
    b.add("第一条")
    a.add("第二条")
    assert a.lines[0] == "[步骤1.1] 第一条"
    assert b.lines[0] == "[步骤2.1] 第一条"
    assert a.lines[1] == "[步骤1.2] 第二条"


def test_extend_keeps_order():
    log = StepLog(4)
    assert log.extend(["甲", "乙", "丙"]) == [
        "[步骤4.1] 甲",
        "[步骤4.2] 乙",
        "[步骤4.3] 丙",
    ]


def test_text_gets_stripped():
    log = StepLog(1)
    log.add("  首尾空白  ")
    assert log.lines[0] == "[步骤1.1] 首尾空白"


def test_len_and_iter():
    log = StepLog(5)
    log.add("甲")
    log.add("乙")
    assert len(log) == 2
    assert list(log) == log.lines


def test_step_number_must_be_positive():
    with pytest.raises(ValueError):
        StepLog(0)


def test_every_pipeline_log_line_is_subnumbered(sample_config, offline_ctx):
    """端到端：六步产出的每一行日志都要带二级编号，且同一步内序号连续。"""
    from kanma.graph import run_review
    from kanma.tools.sandbox import Sandbox

    _, state = run_review(
        sample_config, ctx=offline_ctx, sandbox=Sandbox(backend="local", timeout=120)
    )
    lines = state.get("steps_log", [])
    assert lines, "六步流程必须留下轨迹（HR-09）"

    seen: dict[str, list[int]] = {}
    for line in lines:
        m = _SUBSTEP.match(line)
        assert m, f"日志行缺少二级编号「步骤 N.M」：{line}"
        seen.setdefault(m.group(1), []).append(int(m.group(2)))

    for step, seq in seen.items():
        assert seq == list(range(1, len(seq) + 1)), (
            f"步骤{step} 的二级序号必须从 1 连续递增，实际 {seq}"
        )

    # 六步都要出现
    assert set(seen) == {"1", "2", "3", "4", "5", "6"}, f"缺少步骤：{sorted(seen)}"


def test_extract_skip_reason_handles_substep_prefix():
    """`extract_skip_reason` 必须能去掉 `[步骤N.M] ` 前缀识别出整步跳过原因。

    这是「执行进度里展示跳过的大步骤原因」功能能生效的前提：节点日志经
    `StepLog` 编号后不再是「跳过：」开头，检测逻辑必须先去前缀再判断。
    """
    from kanma.web.jobs import extract_skip_reason

    cases = [
        # 开头型整步跳过（sandbox_run / testcode_gen）
        ("[步骤5.1] 跳过：无测试产物", "跳过：无测试产物"),
        ("[步骤4.3] 工程「r」跳过：无测试工作台", "工程「r」跳过：无测试工作台"),
        # 中途出现的整步跳过（diff 为空导致后续步骤跳过）
        ("[步骤3.1] 所有工程 diff 为空，跳过用例生成", "所有工程 diff 为空，跳过用例生成"),
        ("[步骤2.1] 所有工程 diff 均为空，跳过静态审查", "所有工程 diff 均为空，跳过静态审查"),
        ("[步骤4.2] 工程「r」无对应用例，跳过", "工程「r」无对应用例，跳过"),
        ("[步骤2.3] 跳过语义审查：未配置语义模型", "跳过语义审查：未配置语义模型"),
    ]
    for line, expect in cases:
        assert extract_skip_reason([line]) == expect, f"未正确识别跳过原因：{line}"

    # 反例：diff_collect 的「跳过 N 个非源码文件」不是整步跳过，不应被当作跳过原因
    assert extract_skip_reason(["[步骤1.3] 跳过 5 个非源码文件"]) is None
    # 普通日志不应误判
    assert extract_skip_reason(["[步骤2.1] 语义审查共 14 条"]) is None

