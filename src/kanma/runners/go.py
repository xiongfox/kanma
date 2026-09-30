"""Go / go test 运行器。

Go 要求测试文件位于被测包内，因此依赖「测试工作台」机制：
测试代码写在仓库副本里，绝不污染被评审仓库（HR-03）。
"""

from __future__ import annotations

import re
from pathlib import Path

from kanma.models.schemas import CodeLanguage, ProjectConfig, TestCase, TestResult, TestStatus
from kanma.runners.base import LanguageRunner, RunnerContext, case_fn_exported, has_tool, one_line

_TEMPLATE = '''// 勘码生成的临时测试代码（自动产物，勿提交）。
//
// 项目：{project}
// 框架：go testing
// 说明：LLM 不可用时的模板骨架，未实现真实断言，执行结果记为 SKIP → NOT_RUN。

package {package}

import "testing"

{body}'''

_GO_LINE = re.compile(r"^\s*--- (?P<status>PASS|FAIL|SKIP):\s+(?P<name>\w+)")
_STATUS = {
    "PASS": TestStatus.PASSED,
    "FAIL": TestStatus.FAILED,
    "SKIP": TestStatus.NOT_RUN,
}


class GoRunner(LanguageRunner):
    def __init__(self) -> None:
        super().__init__(
            language=CodeLanguage.GO,
            framework="go testing",
            dependencies=[],
            run_command=["go", "test", "./...", "-v", "-count=1"],
            cache_dirs=[Path.home() / "go" / "pkg" / "mod"],
        )
        self.package = "kanma"

    def detect(self, workspace: Path) -> bool:
        return (Path(workspace) / "go.mod").exists()

    def prepare(self, ctx: RunnerContext, artifact: "object" = None) -> list[str]:
        if not has_tool("go"):
            self.unavailable_reason = "未检测到 go 工具链，Go 测试无法执行"
            return []
        self.package = _module_package(ctx.workspace)
        return self.run_command

    def render_template(self, cases: list[TestCase], cfg: ProjectConfig) -> dict[str, str]:
        body: list[str] = []
        for c in cases:
            fn = case_fn_exported(c.id)
            body += [
                f"// 用例 {c.id} · {c.origin.value} · {one_line(c.title)}",
                f"// 前置：{one_line(c.precondition, fallback='无')}",
                f"// 输入：{one_line(c.input_desc, fallback='见用例')}",
                f"// 期望：{one_line(c.expected)}",
                f"func {fn}(t *testing.T) {{",
                "\tt.Skip(\"LLM 不可用，该用例为模板骨架，未实现真实断言\")",
                "}",
                "",
            ]
        return {
            "kanma_generated_test.go": _TEMPLATE.format(
                project=cfg.name, package=self.package, body="\n".join(body)
            )
        }

    def parse(
        self, stdout: str, cases: list[TestCase], ctx: RunnerContext
    ) -> dict[str, TestResult]:
        name_to_case = {case_fn_exported(c.id): c.id for c in cases}
        parsed: dict[str, TestResult] = {}
        for line in stdout.splitlines():
            m = _GO_LINE.match(line)
            if not m:
                continue
            case_id = name_to_case.get(m.group("name"))
            if case_id is None:
                continue
            parsed[case_id] = TestResult(
                case_id=case_id,
                status=_STATUS.get(m.group("status"), TestStatus.NOT_RUN),
                message=line.strip(),
            )
        return self._fill_unmatched(parsed, cases, "测试输出中未找到该用例")


def _module_package(workspace: Path) -> str:
    """从 go.mod 取模块名，取最后一段作为包名。取不到则用 kanma。"""
    gomod = Path(workspace) / "go.mod"
    if not gomod.exists():
        return "kanma"
    for line in gomod.read_text(encoding="utf-8", errors="ignore").splitlines():
        line = line.strip()
        if line.startswith("module "):
            module = line.split(None, 1)[1].strip().strip('"')
            return module.rsplit("/", 1)[-1] or "kanma"
    return "kanma"
