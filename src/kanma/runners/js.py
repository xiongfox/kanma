"""JavaScript / TypeScript / vitest 运行器。

用 json reporter 解析，避免依赖终端 ANSI 输出格式。
若项目未配置 vitest 而只有 jest，退化为 jest --json。

注意：运行命令统一使用 `npx --no-install`，禁止自动联网安装测试框架
（HR-06 语境下的「诚实降级」——缺失依赖就记为 NOT_RUN，而非偷偷拉包、
既拖慢评审又可能在隔离/离线环境挂死）。
"""

from __future__ import annotations

import json
from pathlib import Path

from kanma.models.schemas import (
    CodeLanguage,
    ProjectConfig,
    TestCase,
    TestResult,
    TestStatus,
)
from kanma.runners.base import LanguageRunner, RunnerContext, has_tool, one_line

_TEMPLATE = """// 勘码生成的临时测试代码（自动产物，勿提交）。
//
// 项目：{project}
// 框架：vitest
// 说明：LLM 不可用时的模板骨架，未实现真实断言，执行结果记为 pending → NOT_RUN。

import {{ describe, it }} from 'vitest'

describe('勘码生成用例', () => {{
{body}}})
"""

_BODY = """
  // 用例 {cid} · {origin} · {title}
  it.skip('{cid} {title}', () => {{
    // 前置：{pre}
    // 输入：{inp}
    // 期望：{exp}
  }})
"""

_STATUS = {
    "passed": TestStatus.PASSED,
    "failed": TestStatus.FAILED,
    "pending": TestStatus.NOT_RUN,
    "skipped": TestStatus.NOT_RUN,
    "todo": TestStatus.NOT_RUN,
}


class JsRunner(LanguageRunner):
    def __init__(self, language: CodeLanguage = CodeLanguage.JAVASCRIPT) -> None:
        suffix = "ts" if language is CodeLanguage.TYPESCRIPT else "js"
        super().__init__(
            language=language,
            framework="vitest",
            dependencies=["vitest"],
            run_command=[
                "npx",
                "--no-install",
                "vitest",
                "run",
                "--reporter=json",
                "--reporter=verbose",
            ],
            cache_dirs=[Path.home() / ".npm"],
        )
        self.file_name = f"kanma.generated.test.{suffix}"

    def detect(self, workspace: Path) -> bool:
        return (Path(workspace) / "package.json").exists()

    def prepare(self, ctx: RunnerContext, artifact: "object" = None) -> list[str]:
        if not has_tool("npx"):
            self.unavailable_reason = "未检测到 npx（Node.js 工具链），JS/TS 测试无法执行"
            return []
        # 项目自带测试框架时沿用，避免强行拉 vitest
        pkg = Path(ctx.workspace) / "package.json"
        if pkg.exists():
            try:
                data = json.loads(pkg.read_text(encoding="utf-8"))
                deps = {**(data.get("dependencies") or {}), **(data.get("devDependencies") or {})}
                if "jest" in deps and "vitest" not in deps:
                    self.framework = "jest"
                    return ["npx", "--no-install", "jest", "--json"]
                if "vitest" in deps:
                    return ["npx", "--no-install", "vitest", "run", "--reporter=json"]
            except json.JSONDecodeError:
                pass
        return self.run_command

    def render_template(self, cases: list[TestCase], cfg: ProjectConfig) -> dict[str, str]:
        body = "".join(
            _BODY.format(
                cid=c.id,
                origin=c.origin.value,
                # 字段压成单行：内嵌换行会破坏 `//` 注释与字符串字面量（非法语法）
                title=one_line(c.title).replace("'", "\\'"),
                pre=one_line(c.precondition, fallback="无"),
                inp=one_line(c.input_desc, fallback="见用例"),
                exp=one_line(c.expected).replace("'", "\\'"),
            )
            for c in cases
        )
        return {self.file_name: _TEMPLATE.format(project=cfg.name, body=body)}

    def parse(
        self, stdout: str, cases: list[TestCase], ctx: RunnerContext
    ) -> dict[str, TestResult]:
        if self.framework == "jest":
            return self._parse_jest(stdout, cases)
        return self._parse_vitest(stdout, cases)

    def _parse_vitest(self, stdout: str, cases: list[TestCase]) -> dict[str, TestResult]:
        payload = _extract_json(stdout)
        if payload is None:
            return self._not_run_all(cases, "vitest 输出解析失败，未获得结构化结果")
        parsed: dict[str, TestResult] = {}
        for suite in payload.get("testResults", []):
            for assertion in suite.get("assertionResults", []):
                case_id = _case_id_from_title(assertion.get("fullName") or assertion.get("title", ""))
                if case_id is None:
                    continue
                parsed[case_id] = TestResult(
                    case_id=case_id,
                    status=_STATUS.get(assertion.get("status", ""), TestStatus.NOT_RUN),
                    message=assertion.get("title", ""),
                )
        return self._fill_unmatched(parsed, cases, "测试输出中未找到该用例")

    def _parse_jest(self, stdout: str, cases: list[TestCase]) -> dict[str, TestResult]:
        payload = _extract_json(stdout)
        if payload is None:
            return self._not_run_all(cases, "jest 输出解析失败，未获得结构化结果")
        parsed: dict[str, TestResult] = {}
        for suite in payload.get("testResults", []):
            for assertion in suite.get("assertionResults", []):
                case_id = _case_id_from_title(assertion.get("fullName") or assertion.get("title", ""))
                if case_id is None:
                    continue
                parsed[case_id] = TestResult(
                    case_id=case_id,
                    status=_STATUS.get(assertion.get("status", ""), TestStatus.NOT_RUN),
                    message=assertion.get("title", ""),
                )
        return self._fill_unmatched(parsed, cases, "测试输出中未找到该用例")


def _case_id_from_title(title: str) -> str | None:
    """从用例标题里取 TC-xxx 编号。"""
    import re

    m = re.search(r"TC-\d+", title)
    return m.group(0) if m else None


def _extract_json(stdout: str):
    start, end = stdout.find("{"), stdout.rfind("}")
    if start < 0 or end <= start:
        return None
    try:
        return json.loads(stdout[start : end + 1])
    except json.JSONDecodeError:
        return None
