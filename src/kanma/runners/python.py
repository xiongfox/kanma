"""Python / pytest 运行器。"""

from __future__ import annotations

import re
import sys

from kanma.models.schemas import CodeLanguage, ProjectConfig, TestCase, TestResult, TestStatus
from kanma.runners.base import LanguageRunner, RunnerContext, case_fn_python, one_line

_PYTEST_LINE = re.compile(
    r"^(?P<node>.+?)::(?P<name>\w+)\s+(?P<status>PASSED|FAILED|ERROR|SKIPPED|XFAIL|XPASS)"
)
_STATUS = {
    "PASSED": TestStatus.PASSED,
    "FAILED": TestStatus.FAILED,
    "ERROR": TestStatus.ERROR,
    "SKIPPED": TestStatus.NOT_RUN,
    "XFAIL": TestStatus.PASSED,
    "XPASS": TestStatus.PASSED,
}

TEMPLATE = '''"""勘码生成的临时测试代码（自动产物，勿提交）。

项目：{project}
框架：pytest
说明：LLM 不可用时的模板骨架，断言未实现，执行结果记为 SKIPPED → NOT_RUN。
"""

import pytest

# 被测代码位于本工作台（仓库副本），不在被评审仓库内（HR-03）
# import sys, pathlib
# sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

{body}'''


class PythonRunner(LanguageRunner):
    def __init__(self) -> None:
        super().__init__(
            language=CodeLanguage.PYTHON,
            framework="pytest",
            dependencies=["pytest"],
            run_command=["python", "-m", "pytest", "kanma_tests", "-v", "--tb=short"],
        )

    def detect(self, workspace: Path) -> bool:
        from pathlib import Path as _P

        ws = _P(workspace)
        return (
            (ws / "pyproject.toml").exists()
            or (ws / "setup.py").exists()
            or (ws / "requirements.txt").exists()
            or any(ws.glob("*.py"))
        )

    def prepare(self, ctx: RunnerContext, artifact: "object" = None) -> list[str]:
        """本地后端下建独立 venv 装 pytest，避免污染宿主环境。"""
        if ctx.sandbox.resolved_backend != "local":
            return self.run_command
        venv_dir = ctx.cfg.tmp_dir / "venv"
        py = venv_dir / "bin" / "python"
        if not py.exists():
            ctx.sandbox.run([sys.executable, "-m", "venv", str(venv_dir)], timeout=300)
        pkgs = [d for d in self.dependencies if d]
        if pkgs:
            ctx.sandbox.run([str(py), "-m", "pip", "install", "-q", *pkgs], timeout=600)
        return [str(py), "-m", "pytest", "kanma_tests", "-v", "--tb=short"]

    def render_template(self, cases: list[TestCase], cfg: ProjectConfig) -> dict[str, str]:
        body: list[str] = []
        for c in cases:
            fn = case_fn_python(c.id)
            # 字段压成单行：内嵌换行会让 `#` 注释第二行变成裸代码；
            # docstring 额外中和内嵌三引号，防止提前闭合。
            title = one_line(c.title).replace('"""', "'''")
            body += [
                f"# 用例 {c.id} · {c.origin.value}",
                f"# 标题：{title}",
                f"# 前置：{one_line(c.precondition, fallback='无')}",
                f"# 输入：{one_line(c.input_desc, fallback='见用例')}",
                f"# 期望：{one_line(c.expected)}",
                f"def {fn}():",
                f'    """{title}（{c.diff_ref or "无 diff 定位"}）"""',
                "    pytest.skip('LLM 不可用，该用例为模板骨架，未实现真实断言')",
                "",
                "",
            ]
        return {"kanma_tests/test_kanma_generated.py": TEMPLATE.format(project=cfg.name, body="\n".join(body))}

    def parse(
        self, stdout: str, cases: list[TestCase], ctx: RunnerContext
    ) -> dict[str, TestResult]:
        name_to_case = {case_fn_python(c.id): c.id for c in cases}
        parsed: dict[str, TestResult] = {}
        for line in stdout.splitlines():
            m = _PYTEST_LINE.match(line.strip())
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
