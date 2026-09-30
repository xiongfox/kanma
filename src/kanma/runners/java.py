"""Java / JUnit5 运行器。

执行策略（按可用工具递进）：
1. **Maven**（pom.xml + mvn 可用）→ `mvn -B test`，结果解析 surefire XML。
2. **Gradle**（build.gradle* + gradle 可用）→ `gradle test`，结果解析。
3. **独立执行**（无构建文件，但有 java/javac + junit 控制台独立包）→
   用 `javac -sourcepath` 仅编译生成出来的测试文件及其在仓库内可达的源码，
   再用 `junit-platform-console-standalone` 运行。该路径**不依赖** Maven/Gradle
   工具链与构建文件，适合只在隔离环境验证纯逻辑类测试；若被测代码依赖外部
   jar（Spring/Netty 等）无法在隔离环境编译，则如实标记 NOT_RUN 并保留编译错误。

隔离环境拉依赖的对策：把本地 ~/.m2（或 ~/.gradle）只读挂载进容器；
本地进程模式下直接复用宿主缓存。缓存缺失时如实记为 NOT_RUN，不伪造结果。
"""

from __future__ import annotations

import os
import re
import shutil
import urllib.request
import xml.etree.ElementTree as ET
from pathlib import Path

from kanma.models.schemas import (
    CodeLanguage,
    ProjectConfig,
    TestArtifact,
    TestCase,
    TestResult,
    TestStatus,
)
from kanma.runners.base import (
    LanguageRunner,
    RunnerContext,
    case_fn_camel,
    has_tool,
    one_line,
)

# junit 控制台独立包：自带 jupiter + 引擎，无需 Maven/Gradle 即可运行 JUnit5。
_JUNIT_STANDALONE_VERSION = "1.10.2"
_JUNIT_STANDALONE_URL = (
    "https://repo1.maven.org/maven2/org/junit/platform/"
    f"junit-platform-console-standalone/{_JUNIT_STANDALONE_VERSION}/"
    f"junit-platform-console-standalone-{_JUNIT_STANDALONE_VERSION}.jar"
)

_TEMPLATE = '''package kanma;

import org.junit.jupiter.api.Disabled;
import org.junit.jupiter.api.Test;

/**
 * 勘码生成的临时测试代码（自动产物，勿提交）。
 *
 * <p>项目：{project}
 * <p>框架：JUnit5
 * <p>说明：LLM 不可用时的模板骨架，未实现真实断言，执行结果记为 skipped → NOT_RUN。
 */
class KanmaGeneratedTest {{

{body}}}
'''

# mvn 输出的汇总行：Tests run: 3, Failures: 0, Errors: 0, Skipped: 3
_MVN_SUMMARY = re.compile(
    r"Tests run:\s*(?P<total>\d+),\s*Failures:\s*(?P<fail>\d+),"
    r"\s*Errors:\s*(?P<err>\d+),\s*Skipped:\s*(?P<skip>\d+)"
)

# 独立执行模式下编译失败会在 stdout 写入此标记
_COMPILE_FAILED_MARK = "KANMA_COMPILE_FAILED"


def _common_bin_dirs() -> list[Path]:
    return [
        Path.home() / ".kanma" / "java",
        Path("/opt/homebrew/bin"),
        Path("/usr/local/bin"),
        Path("/usr/bin"),
    ]


def _find_executable(name: str) -> str | None:
    """在 PATH 与常见安装目录里查找可执行文件。

    服务进程（如 serve.sh 拉起的后台线程）往往没有 login shell 的完整 PATH，
    gradle/mvn 装在 /opt/homebrew/bin 下会被漏检，这里兜底扫描常见目录。
    """
    found = shutil.which(name)
    if found:
        return found
    for d in _common_bin_dirs():
        cand = d / name
        if cand.is_file() and os.access(str(cand), os.X_OK):
            return str(cand)
    return None


def _junit_jar_cache() -> Path:
    return Path.home() / ".kanma" / "java" / f"junit-platform-console-standalone-{_JUNIT_STANDALONE_VERSION}.jar"


def _ensure_junit_jar() -> str | None:
    """确保 junit 控制台独立包存在；缺失且可联网时下载一次，否则返回 None。"""
    jar = _junit_jar_cache()
    if jar.exists() and jar.stat().st_size > 1_000_000:
        return str(jar)
    try:
        jar.parent.mkdir(parents=True, exist_ok=True)
        urllib.request.urlretrieve(_JUNIT_STANDALONE_URL, jar)  # noqa: S310 - 固定官方地址
        if jar.exists() and jar.stat().st_size > 1_000_000:
            return str(jar)
    except Exception as e:  # 下载失败（离线）则退化为不可用
        import logging

        logging.getLogger(__name__).warning("下载 junit 独立包失败：%s", e)
    return None


class JavaRunner(LanguageRunner):
    def __init__(self) -> None:
        super().__init__(language=CodeLanguage.JAVA, framework="JUnit5", dependencies=[])
        self.build_tool: str = "maven"
        self.mode: str = ""  # maven | gradle | standalone
        self.standalone_jar: str = ""

    def detect(self, workspace: Path) -> bool:
        ws = Path(workspace)
        return (
            (ws / "pom.xml").exists()
            or (ws / "build.gradle").exists()
            or (ws / "build.gradle.kts").exists()
        )

    def _resolve_build_tool(self, workspace: Path, maven_path: str = "") -> str | None:
        ws = Path(workspace)
        if (ws / "pom.xml").exists():
            # 优先使用用户配置的 Maven 路径
            if maven_path and Path(maven_path).is_file() and os.access(maven_path, os.X_OK):
                return "maven"
            elif _find_executable("mvn"):
                return "maven"
        if ((ws / "build.gradle").exists() or (ws / "build.gradle.kts").exists()):
            if _find_executable("gradle"):
                return "gradle"
        return None

    def prepare(self, ctx: RunnerContext, artifact: "TestArtifact | None" = None) -> list[str]:
        maven_path = ctx.cfg.maven_path if ctx.cfg else ""
        maven_conf = ctx.cfg.maven_conf if ctx.cfg else ""
        tool = self._resolve_build_tool(ctx.workspace, maven_path)
        if tool is not None:
            self.mode = tool
            if tool == "maven":
                # 使用用户配置的 Maven 路径；配置了 settings.xml 时加 -s
                mvn_cmd = maven_path if maven_path and Path(maven_path).is_file() else "mvn"
                if maven_conf and Path(maven_conf).is_file():
                    self.run_command = [mvn_cmd, "-s", maven_conf, "-q", "-B", "test"]
                else:
                    self.run_command = [mvn_cmd, "-q", "-B", "test"]
                self.cache_dirs = [Path.home() / ".m2"]
            else:
                self.run_command = ["gradle", "test", "--console=plain"]
                self.cache_dirs = [Path.home() / ".gradle"]
            return self.run_command

        # 独立执行路径：无需构建文件，只要有 java/javac + junit 独立包即可
        java_bin = _find_executable("java")
        javac_bin = _find_executable("javac")
        if java_bin and javac_bin:
            jar = _ensure_junit_jar()
            if jar:
                self.mode = "standalone"
                self.standalone_jar = jar
                self.cache_dirs = []
                return self._standalone_command(ctx, artifact, java_bin, javac_bin, jar)
            self.unavailable_reason = (
                "未检测到可用的 Maven/Gradle 构建文件，且无法获取 JUnit 独立运行包"
                "（离线且本地无缓存），Java 测试无法执行"
            )
            return []
        self.unavailable_reason = "未检测到 java/javac 或 Maven/Gradle 工具链，Java 测试无法执行"
        return []

    def _standalone_command(
        self,
        ctx: RunnerContext,
        artifact: "TestArtifact | None",
        java_bin: str,
        javac_bin: str,
        jar: str,
    ) -> list[str]:
        """构造「编译生成测试 + 运行」的 bash 脚本命令。

        只编译本次生成出来的测试文件，并用 -sourcepath 让 javac 自动拉取仓库内
        可达的源码；外部依赖（Spring/Netty 等）缺失会编译失败并被如实记录。
        """
        ws = Path(ctx.workspace)
        out_dir = ws / "target" / "kanma-test-out"
        rpt_dir = ws / "target" / "kanma-junit-reports"
        # 本次要编译的测试文件（相对 workspace 的路径）
        test_files = list(artifact.files) if artifact and artifact.files else []
        # 用引号包裹，避免路径含空格/特殊字符
        files_quoted = " ".join(f'"{f}"' for f in test_files) or '""'

        script = (
            "set +e\n"
            f'JAVAC="{javac_bin}"\n'
            f'JAVA="{java_bin}"\n'
            f'JAR="{jar}"\n'
            f'OUT="{out_dir}"\n'
            f'RPT="{rpt_dir}"\n'
            "rm -rf \"$OUT\" \"$RPT\"; mkdir -p \"$OUT\" \"$RPT\"\n"
            # 编译：仅编译生成出来的测试文件，-sourcepath . 自动拉取仓库内源码
            f'\"$JAVAC\" -encoding UTF-8 -cp \"$JAR\" -sourcepath . -d \"$OUT\" {files_quoted} > compile.log 2>&1\n'
            "if [ $? -ne 0 ]; then\n"
            f'  echo "{_COMPILE_FAILED_MARK}"\n'
            "  sed 's/^/  /' compile.log\n"
            "  exit 0\n"
            "fi\n"
            # 运行：扫描 classpath 下所有测试，结果写入 XML 报告
            f'\"$JAVA\" -jar \"$JAR\" --class-path \"$OUT\" --scan-classpath --reports-dir \"$RPT\" 2>&1\n'
        )
        # 通过 bash -lc 执行；-l 让登录 shell 也能找到工具，-c 接脚本
        return ["bash", "-lc", script]

    def render_template(self, cases: list[TestCase], cfg: ProjectConfig) -> dict[str, str]:
        body: list[str] = []
        for c in cases:
            fn = case_fn_camel(c.id)
            # 用例字段必须压成单行：内嵌换行会让注释第二行变成裸 Java 代码，
            # javac 报「非法的类型开始」，整批测试编译失败（回归见 test_runners）。
            body += [
                f"    // 用例 {c.id} · {c.origin.value} · {one_line(c.title)}",
                f"    // 前置：{one_line(c.precondition, fallback='无')}",
                f"    // 输入：{one_line(c.input_desc, fallback='见用例')}",
                f"    // 期望：{one_line(c.expected)}",
                "    @Test",
                '    @Disabled("LLM 不可用，该用例为模板骨架，未实现真实断言")',
                f"    void {fn}() {{",
                "    }",
                "",
            ]
        return {
            "src/test/java/kanma/KanmaGeneratedTest.java": _TEMPLATE.format(
                project=cfg.name, body="\n".join(body)
            )
        }

    def parse(
        self, stdout: str, cases: list[TestCase], ctx: RunnerContext
    ) -> dict[str, TestResult]:
        # 独立执行模式：编译失败时直接整批 NOT_RUN，并保留编译错误
        if _COMPILE_FAILED_MARK in stdout:
            # 归因保持中性：可能是生成代码语法错误，也可能是隔离环境缺外部依赖，
            # 以 compile.log 的首条错误为准，不预设立场。
            msg = "Java 测试编译失败（生成代码语法错误或隔离环境缺少外部依赖，无法独立编译）"
            # 截取编译错误关键行，便于定位
            idx = stdout.find(_COMPILE_FAILED_MARK)
            tail = stdout[idx + len(_COMPILE_FAILED_MARK):].strip()
            if tail:
                msg += "：" + tail.splitlines()[0][:240]
            return self._not_run_all(cases, msg)

        # 优先解析 surefire / junit 控制台独立包 产出的 XML（最可靠）
        xml_results = self._parse_xml_reports(ctx.workspace, cases)
        if xml_results:
            return xml_results
        return self._parse_stdout(stdout, cases)

    def _parse_xml_reports(self, workspace: Path, cases: list[TestCase]) -> dict[str, TestResult]:
        # 同时兼容 Maven surefire（target/surefire-reports）与独立模式
        # （target/kanma-junit-reports）的 XML 产出
        reports = sorted(Path(workspace).glob("target/**/TEST-*.xml"))
        if not reports:
            return {}
        name_to_case = {case_fn_camel(c.id): c.id for c in cases}
        parsed: dict[str, TestResult] = {}
        for report in reports:
            try:
                root = ET.parse(report).getroot()
            except ET.ParseError:
                continue
            for tc in root.iter("testcase"):
                name = tc.get("name", "")
                case_id = name_to_case.get(name)
                if case_id is None:
                    continue
                if tc.find("failure") is not None:
                    status, msg = TestStatus.FAILED, "JUnit 断言失败"
                elif tc.find("error") is not None:
                    status, msg = TestStatus.ERROR, "JUnit 执行异常"
                elif tc.find("skipped") is not None:
                    status, msg = TestStatus.NOT_RUN, "JUnit 用例被 @Disabled 跳过"
                else:
                    status, msg = TestStatus.PASSED, "通过"
                parsed[case_id] = TestResult(case_id=case_id, status=status, message=msg)
        return parsed

    def _parse_stdout(self, stdout: str, cases: list[TestCase]) -> dict[str, TestResult]:
        m = _MVN_SUMMARY.search(stdout)
        message = (
            f"mvn 汇总：{m.group(0)}（逐用例结果解析失败，需人工核对）" if m else "未解析到测试输出"
        )
        # 汇总行无法区分到具体用例，只能整批标记，避免误判为通过
        status = TestStatus.NOT_RUN
        if m and int(m.group("total")) > 0 and int(m.group("skip")) == 0:
            status = (
                TestStatus.PASSED
                if int(m.group("fail")) + int(m.group("err")) == 0
                else TestStatus.FAILED
            )
        return (
            self._not_run_all(cases, message)
            if status is TestStatus.NOT_RUN
            else {
                c.id: TestResult(case_id=c.id, status=status, message=message) for c in cases
            }
        )
