"""语言运行器抽象。

每个被测语言实现一套：框架探测、运行命令、环境准备、结果解析、依赖缓存挂载。

职责边界：
- 运行器**不生成断言逻辑**（那是 LLM 或模板的事），只负责「让测试跑起来并读懂结果」。
- 模板骨架一律生成 skip 形式，执行后如实记为 NOT_RUN（HR-06：绝不记为通过）。
- 依赖缓存目录要暴露给沙箱，否则隔离环境下 Java/Go/JS 拉不到依赖必然失败。
"""

from __future__ import annotations

import re
import shutil
from dataclasses import dataclass, field
from pathlib import Path

from kanma.models.schemas import (
    CodeLanguage,
    ProjectConfig,
    TestCase,
    TestResult,
    TestStatus,
)
from kanma.tools.sandbox import Sandbox

# 用例编号 → 各语言测试函数名的统一映射规则
# TC-001 → python: test_tc_001 / java: testTc001 / go: TestTc001 / js: 'TC-001 ...'
def case_fn_python(case_id: str) -> str:
    return f"test_{case_id.lower().replace('-', '_')}"


def case_fn_camel(case_id: str) -> str:
    """TC-001 → testTc001"""
    head, _, num = case_id.lower().partition("-")
    return f"test{head.capitalize()}{num}"


def case_fn_exported(case_id: str) -> str:
    """TC-001 → TestTc001（Go 要求 Test 开头且导出）"""
    return case_fn_camel(case_id)[0].upper() + case_fn_camel(case_id)[1:]


def one_line(text: str | None, limit: int = 400, fallback: str = "") -> str:
    """把用例字段压成**安全单行**，供模板注释 / 字符串字面量拼接。

    用例字段（title / precondition / input_desc / expected）来自 LLM 或人工录入，
    可能内嵌换行——直接拼进 `//` 注释时第二行会变成裸代码（javac 报
    「非法的类型开始」），拼进字符串字面量则直接产生非法语法。
    换行一律折叠为「；」，超长截断到 limit。
    """
    if not text:
        return fallback
    s = re.sub(r"\r\n?|\n", "；", str(text)).strip()
    s = re.sub(r"；{2,}", "；", s)
    return s[:limit]


@dataclass
class RunnerContext:
    """运行器所需的全部上下文。"""

    cfg: ProjectConfig
    sandbox: Sandbox
    test_dir: Path  # 测试工作台内的测试文件目录
    workspace: Path  # 仓库副本根目录（Go/Java 需要测试文件位于模块内）


@dataclass
class LanguageRunner:
    language: CodeLanguage
    framework: str
    dependencies: list[str] = field(default_factory=list)
    run_command: list[str] = field(default_factory=list)
    # 依赖缓存目录：Docker 模式下只读挂载，避免隔离环境无网导致构建失败
    cache_dirs: list[Path] = field(default_factory=list)
    unavailable_reason: str = ""

    @property
    def available(self) -> bool:
        return not self.unavailable_reason

    def detect(self, workspace: Path) -> bool:
        """探测工作台里是否存在该语言的构建文件。"""
        return True

    def prepare(self, ctx: RunnerContext, artifact: "object" = None) -> list[str]:
        """准备环境并返回最终运行命令；不可用时返回空列表。

        ``artifact`` 为可选的 ``TestArtifact``，便于运行器知道本次要编译/运行的
        具体测试文件（如 Java 独立执行模式只编译生成出来的那几个测试文件，
        而非整个工程源码）。不传时为 None，运行器自行探测。
        """
        return self.run_command

    def render_template(self, cases: list[TestCase], cfg: ProjectConfig) -> dict[str, str]:
        """生成 skip 骨架。断言未实现 → 执行结果为 skip → 记为 NOT_RUN。"""
        raise NotImplementedError

    def parse(self, stdout: str, cases: list[TestCase], ctx: RunnerContext) -> dict[str, TestResult]:
        raise NotImplementedError

    # ------------------------------------------------------------------
    # 公共辅助
    # ------------------------------------------------------------------
    def _not_run_all(self, cases: list[TestCase], message: str) -> dict[str, TestResult]:
        return {
            c.id: TestResult(case_id=c.id, status=TestStatus.NOT_RUN, message=message)
            for c in cases
        }

    def _fill_unmatched(
        self, parsed: dict[str, TestResult], cases: list[TestCase], message: str
    ) -> dict[str, TestResult]:
        for c in cases:
            parsed.setdefault(
                c.id, TestResult(case_id=c.id, status=TestStatus.NOT_RUN, message=message)
            )
        return parsed


def tool_missing(name: str) -> str:
    return f"未检测到 {name}，无法执行该语言的测试"


def has_tool(name: str) -> bool:
    return shutil.which(name) is not None
