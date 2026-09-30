"""步骤 4 · 生成临时测试代码。

测试代码只写在**测试工作台**（仓库副本 `{workdir}/repos/<工程>/workspace`），
绝不写入被评审仓库（HR-03）。多工程时按工程分别准备工作台、分别生成与执行。

语言差异全部下沉到 `kanma/runners/`：本节点只负责「拿代码 → 落盘 → 产出工件」。
"""

from __future__ import annotations

import logging
from pathlib import Path

from langchain_core.messages import HumanMessage, SystemMessage
from pydantic import BaseModel, Field

from kanma.llm import LLMContext, invoke_structured
from kanma.models.schemas import ProjectConfig, TestArtifact, TestCase
from kanma.nodes.step_log import StepLog
from kanma.runners import detect_runner, prepare_workspace
from kanma.state import ReviewState
from kanma.utils import render_diffs, truncate

logger = logging.getLogger(__name__)

SYSTEM_PROMPT = """你是「勘码」，企业级代码评审 Agent，行为以 AGENTS.md 为唯一事实源。现在执行流程第 4 步：生成临时测试代码。

输入：第 3 步产出的测试用例、被测工程 diff、自动探测到的主语言与目标测试框架。
目标：把测试用例落成**能直接跑起来**的代码（后续在第 5 步真实执行，HR-06）。

## 铁律（不可违反）
- HR-03 绝不写入被评审仓库：测试代码**只写到给定的测试工作台（workdir）**，禁止修改被评审源码树、禁止覆盖被测业务文件。
- HR-07 测试执行安全底线：单元级测试在隔离环境执行——禁止访问外网、禁止连接生产库或真实中间件，
  外部依赖用 stub/mock 替代，每个用例可独立运行；禁止连接生产环境、禁止写入工作区外路径。

## 硬性约束
1. 沿用被测项目**既有**测试框架（Java→JUnit / Python→pytest / Go→testing 等），不要引入项目未使用的框架。
2. 每个用例对应一个独立测试函数，用例之间**不得有顺序依赖、不得共享可变状态**。
3. 断言必须与用例的 expected **严格对应**：只断言确定结果，禁止 to-do 式占位（assert True、空 test 方法、只打印不断言）。
4. 若被测代码无法在隔离环境直接导入（如依赖容器上下文），用最小化的本地桩复刻被测逻辑的关键分支，
   并在注释里说明这是兜底桩——断言仍须真实反映期望，不得为通过而放宽。
5. 每个测试文件顶部写注释：覆盖的用例编号、被测目标、生成说明。


## 测试函数命名（与用例编号严格对应）
- Python：`test_tc_001` 对应 TC-001
- Java：`testTc001` 对应 TC-001
- Go：`TestTc001` 对应 TC-001
- JS/TS：用例标题以 `TC-001 ` 开头

## Java 专项（runner.language=java 时必须遵守）
1. 禁止把测试代码生成到根项目、父工程或当前打开工程的 src/test/java 下。
2. 必须先根据 targetClassSourcePath 定位目标类所属模块：
   - 从 targetClassSourcePath 向上查找最近的 pom.xml / build.gradle / settings.gradle 所在目录，作为 moduleRoot。
   - 如果存在父 pom 或根 build.gradle，moduleRoot 仍然是包含该目标类源码的最近子工程目录。
3. 测试源码根必须是：
   - Maven / Gradle Java：<moduleRoot>/src/test/java
   - Kotlin：<moduleRoot>/src/test/kotlin
   - Android 单元测试：<moduleRoot>/src/test/java
   - Android 仪器测试：<moduleRoot>/src/androidTest/java
4. 测试类包名必须与目标类包名一致。
5. 测试类名默认：<目标类名>Test。
6. 生成代码前，先输出：
   - 目标类全限定名
   - 目标类源码路径
   - 推导出的 moduleRoot
   - 测试文件完整路径
   - 测试类包名
   如果无法确定 moduleRoot，停止生成并向我提问，不要猜测。
7. 测试依赖只能加到目标模块的构建文件中：
   - Maven：<moduleRoot>/pom.xml
   - Gradle：<moduleRoot>/build.gradle 或 build.gradle.kts
   不要默认修改根 pom.xml 或根 build.gradle。
8. 如果目标模块缺少 JUnit、Mockito、Spring Boot Test 等依赖，只输出需要在目标模块添加的依赖片段，并明确文件路径。
9. 不要使用根项目的启动类、根项目配置、根项目包路径，除非目标类本身就在根项目。
10. 生成后自检并输出：
   - 测试文件是否位于 moduleRoot 下？
   - 包名是否与目标类一致？
   - 依赖是否只加在目标模块？
   - 编译命令是否只针对目标模块？

## 输出
files 列表：path 为相对测试工作台根的路径，content 为完整文件内容（含 import、桩、断言）。
"""


class _LLMTestFile(BaseModel):
    path: str
    content: str


class _LLMTestCode(BaseModel):
    framework: str
    dependencies: list[str] = Field(default_factory=list)
    run_command: list[str] = Field(default_factory=list)
    files: list[_LLMTestFile]


def detect_primary_language(diffs: list) -> "object":
    """按变更行数最多的语言作为主语言（仅统计给定工程的 diff）。"""
    from kanma.models.schemas import CodeLanguage

    counter: dict[CodeLanguage, int] = {}
    for d in diffs:
        for fd in d.reviewable_files:
            counter[fd.language] = counter.get(fd.language, 0) + fd.added_lines + fd.removed_lines
    if not counter:
        return CodeLanguage.UNKNOWN
    return max(counter.items(), key=lambda kv: kv[1])[0]


def _write_test_files(workspace: Path, files: dict[str, str]) -> list[str]:
    written: list[str] = []
    for rel, content in files.items():
        target = workspace / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
        written.append(rel)
    return written


def testcode_gen(state: ReviewState, ctx: LLMContext | None = None) -> dict:
    cfg: ProjectConfig = state["config"]
    cases: list[TestCase] = state.get("test_cases", [])
    diffs = state.get("diffs", [])

    log = StepLog(4)
    notes: list[str] = []
    artifacts: list[TestArtifact] = []

    if not cases:
        return {
            "test_artifacts": [
                TestArtifact(
                    language=detect_primary_language(diffs),
                    framework="",
                    test_dir=Path("."),
                    degraded_reason="无测试用例，跳过测试代码生成",
                )
            ],
            "steps_log": [log.add("无用例，跳过")],
            "degradation_notes": notes,
        }

    if ctx is None:
        from kanma.llm import get_llm

        ctx = get_llm()

    # 按工程分组：每个工程在各自测试工作台内生成并执行测试
    total_repos = len(cfg.repos)
    for repo_idx, repo in enumerate(cfg.repos, start=1):
        log.progress(repo_idx, total_repos, f"生成测试代码 {repo.display_name}")
        repo_cases = [c for c in cases if c.repo_name == repo.display_name]
        repo_diffs = [d for d in diffs if d.repo_name == repo.display_name]
        if not repo_cases:
            log.add(f"工程「{repo.display_name}」无对应用例，跳过")
            continue

        lang = detect_primary_language(repo_diffs)
        skipped_files: list[str] = []
        workspace = prepare_workspace(cfg, repo, skipped=skipped_files)
        if not workspace:
            reason = f"工程「{repo.display_name}」仓库副本不可用，无法建立测试工作台"
            notes.append(f"步骤4 降级（{repo.display_name}）：{reason}，功能验证缺失")
            log.add(f"跳过：{reason}")
            continue
        if skipped_files:
            shown = "、".join(skipped_files[:5]) + ("…" if len(skipped_files) > 5 else "")
            log.add(
                f"工程「{repo.display_name}」工作台复制跳过 {len(skipped_files)} 个"
                f"不可读文件（敏感内容拒访/权限不足）：{shown}，不影响其余文件"
            )
            notes.append(
                f"步骤4（{repo.display_name}）：测试工作台复制跳过 {len(skipped_files)} 个"
                f"不可读文件（含敏感配置被环境拦截），已跳过不参与测试，其余文件正常复制"
            )

        runner = detect_runner(workspace, lang)
        log.add(
            f"工程「{repo.display_name}」工作台：{workspace}，运行器：{runner.framework}"
        )

        files: dict[str, str] = {}
        if ctx.available:
            messages = [
                SystemMessage(content=SYSTEM_PROMPT),
                HumanMessage(
                    content=(
                        f"## 被测工程：{repo.display_name}\n"
                        f"## 被测项目主语言：{runner.language.value}，目标框架：{runner.framework}\n\n"
                        f"## 测试用例\n\n{cases_to_text(repo_cases)}\n\n"
                        f"## 代码 diff（供定位被测目标）\n\n"
                        f"{truncate(render_diffs(repo_diffs), 12000)}"
                    )
                ),
            ]
            try:
                result: _LLMTestCode = invoke_structured(ctx, _LLMTestCode, messages)
                files = {f.path: f.content for f in result.files}
                if result.framework:
                    runner.framework = result.framework
                if result.dependencies:
                    runner.dependencies = result.dependencies
                if result.run_command:
                    runner.run_command = result.run_command
                log.add(
                    f"工程「{repo.display_name}」LLM 生成 {len(files)} 个测试文件（{runner.framework}）"
                )
            except Exception as e:
                logger.warning("测试代码生成失败（%s）：%s", repo.display_name, e)
                notes.append(
                    f"步骤4 降级（{repo.display_name}）：LLM 生成测试代码失败（{e}），改用模板骨架"
                )
                log.add(f"降级：{e}")

        if not files:
            try:
                files = runner.render_template(repo_cases, cfg)
            except NotImplementedError:
                reason = f"语言 {runner.language.value} 无可用测试骨架模板"
                notes.append(
                    f"步骤4 降级（{repo.display_name}）：{reason}，功能验证缺失"
                )
                log.add(f"工程「{repo.display_name}」跳过：{reason}")
                continue
            notes.append(
                f"步骤4 降级（{repo.display_name}）：使用 {runner.framework} 模板骨架，"
                f"断言未实现，执行结果记为 NOT_RUN"
            )
            log.add(f"工程「{repo.display_name}」使用 {runner.framework} 模板骨架")

        written = _write_test_files(workspace, files)
        artifacts.append(
            TestArtifact(
                language=runner.language,
                framework=runner.framework,
                test_dir=workspace,
                workspace=workspace,
                files=written,
                run_command=runner.run_command,
                dependencies=runner.dependencies,
                case_ids=[c.id for c in repo_cases],
            )
        )

    if not artifacts:
        return {
            "test_artifacts": [
                TestArtifact(
                    language=detect_primary_language(diffs),
                    framework="",
                    test_dir=Path("."),
                    degraded_reason="所有工程均无可用测试工作台，跳过测试代码生成",
                )
            ],
            "steps_log": log.lines,
            "degradation_notes": notes,
        }
    return {"test_artifacts": artifacts, "steps_log": log.lines, "degradation_notes": notes}


def cases_to_text(cases: list[TestCase]) -> str:
    out = []
    for c in cases:
        out.append(
            f"- [{c.id}]（{c.origin.value}）{c.title}\n"
            f"  前置：{c.precondition or '无'}\n"
            f"  输入：{c.input_desc or '见用例'}\n"
            f"  期望：{c.expected}\n"
            f"  定位：{c.diff_ref or '无'}"
            + (f"\n  关联缺陷：{c.finding_id}" if c.finding_id else "")
        )
    return "\n".join(out)
