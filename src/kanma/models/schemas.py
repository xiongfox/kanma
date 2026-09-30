"""勘码核心数据契约。

设计原则：
- 每个字段都能被下游节点或报告直接消费，不留"待解析"的中间字符串。
- 严重级别与测试状态使用枚举，禁止自由文本（对应 AGENTS.MD HR-08）。
- 凡涉及代码位置的结构，必带 file/line，保证证据可溯源（对应 HR-02）。
"""

from __future__ import annotations

from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field, computed_field, field_validator

import re as _re


def _safe_name(text: str) -> str:
    """把工程名/仓库地址转成可用的目录片段。"""
    s = _re.sub(r"[^\w\u4e00-\u9fff]+", "-", text).strip("-")
    return s or "repo"


def _repo_name_from_url(git_url: str) -> str:
    """从 git 地址推导仓库名（去掉 .git 与路径前缀）。"""
    if not git_url:
        return ""
    name = git_url.rstrip("/").split("/")[-1]
    if name.endswith(".git"):
        name = name[: -len(".git")]
    return name


class Severity(str, Enum):
    """严重级别。仅四个取值，见 AGENTS.MD HR-08。"""

    P0 = "P0"
    P1 = "P1"
    P2 = "P2"
    P3 = "P3"


#: HR-08 严重级别标准的**唯一来源**：级别 -> (含义, 处置)。
#: 提示词（kanma.llm.SEVERITY_GUIDE）、报告「3.1 严重级别标准」表、以及任何对外
#: 说明级别口径的地方都必须从这里取，禁止各处各写一套（HR-08 要求定义唯一）。
SEVERITY_STANDARD: dict[Severity, tuple[str, str]] = {
    Severity.P0: ("安全漏洞、数据损坏、与需求不符、必然崩溃", "禁止合入"),
    Severity.P1: ("明确的性能缺陷、逻辑错误、异常吞掉、关键路径缺注释", "合入前必须修"),
    Severity.P2: ("命名、可读性、非关键路径注释缺失、冗余代码", "建议修改"),
    Severity.P3: ("纯提示，无功能影响", "可选"),
}

SEVERITY_ORDER = {
    Severity.P0: 0,
    Severity.P1: 1,
    Severity.P2: 2,
    Severity.P3: 3,
}

# HR-08 允许上调级别时的档位顺序（用于测试复现后的交叉印证）
SEVERITY_ESCALATION = {
    Severity.P3: Severity.P2,
    Severity.P2: Severity.P1,
    Severity.P1: Severity.P0,
    Severity.P0: Severity.P0,
}


class ReviewDimension(str, Enum):
    """静态审查维度，见 AGENTS.MD 步骤 2。"""

    SECURITY = "安全"
    PERFORMANCE = "性能"
    CORRECTNESS = "正确性"
    NAMING = "命名"
    COMMENT = "注释"
    CONVENTION = "规范"


class TestStatus(str, Enum):
    """测试执行状态。NOT_RUN 用于沙箱不可用等客观失败，对应 HR-06。"""

    PASSED = "PASSED"
    FAILED = "FAILED"
    ERROR = "ERROR"
    TIMEOUT = "TIMEOUT"
    NOT_RUN = "NOT_RUN"


class ReviewVerdict(str, Enum):
    """最终结论，四选一，见 AGENTS.MD HR-10。"""

    REJECT = "拒绝合入"
    REQUEST_CHANGES = "修改后合入"
    SUGGEST_CHANGES = "建议修改后合入"
    APPROVE = "通过"


class CodeLanguage(str, Enum):
    """被测工程语言，决定测试框架与脚手架的选择。"""

    JAVA = "java"
    PYTHON = "python"
    GO = "go"
    JAVASCRIPT = "javascript"
    TYPESCRIPT = "typescript"
    UNKNOWN = "unknown"


# ---------------------------------------------------------------------------
# 项目输入契约（AGENTS.MD 第 3 节）
# ---------------------------------------------------------------------------


class SourceRepo(BaseModel):
    """一个源码工程：git 地址、分支、时间窗。

    一个评审项目可包含多个 SourceRepo（对应多个源码工程），各自独立指定
    git 地址、分支、开始/结束时间（AGENTS.MD 第 3 节）。
    """

    name: str = Field(default="", description="源码工程名（用于区分同一项目下的多个工程）")
    git_url: str = Field(description="仓库地址（SSH/HTTPS）")
    branch: str = Field(description="目标分支")
    base: str | None = Field(default=None, description="对比基线 commit/分支")
    start_time: datetime = Field(description="变更起始时间 ISO 8601")
    end_time: datetime = Field(description="变更结束时间 ISO 8601")

    @property
    def display_name(self) -> str:
        """用于报告与目录命名的工程名，缺省时从 git 地址推导。"""
        return self.name or _repo_name_from_url(self.git_url)

    @field_validator("end_time")
    @classmethod
    def _check_window(cls, v: datetime, info: Any) -> datetime:
        start = info.data.get("start_time")
        if start and v < start:
            raise ValueError("end_time 必须晚于或等于 start_time")
        return v


class RequirementDoc(BaseModel):
    """需求开发文档：第 3 步生成测试用例、HR-04 需求对齐的验收基准。"""

    path: Path | None = Field(default=None, description="文档路径")
    title: str = Field(default="", description="需求标题")
    raw_text: str = Field(default="", description="文档正文（PDF/HTML 抽取后的纯文本）")

    @property
    def available(self) -> bool:
        return bool(self.raw_text.strip())


class ProjectConfig(BaseModel):
    """一次评审项目的完整输入。可包含多个源码工程，需求文档缺失时走降级路径。"""

    name: str = Field(description="项目名，用于报告命名")
    repos: list[SourceRepo] = Field(description="源码工程列表，至少 1 个")
    requirement: RequirementDoc = Field(default_factory=RequirementDoc)
    workdir: Path = Field(default=Path(".kanma"), description="勘码工作区（临时产物存放处）")
    maven_path: str = Field(default="", description="Maven 可执行文件绝对路径（本机未安装 Maven 时可指定自定义路径）")
    maven_conf: str = Field(default="", description="Maven settings.xml 路径（可选）")
    source_extensions: list[str] = Field(
        default_factory=lambda: [
            ".java",
            ".py",
            ".go",
            ".js",
            ".ts",
            ".jsx",
            ".tsx",
            ".kt",
            ".rb",
            ".rs",
            ".c",
            ".cpp",
            ".h",
        ],
        description="视为源码的扩展名白名单（步骤 1 排除规则）",
    )
    exclude_globs: list[str] = Field(
        default_factory=lambda: [
            "**/target/**",
            "**/build/**",
            "**/dist/**",
            "**/node_modules/**",
            "**/*.min.js",
            "**/*_pb2.py",
            "**/*.generated.*",
            "**/migrations/**",
        ],
        description="排除路径：构建产物、生成代码、依赖目录",
    )

    @field_validator("repos")
    @classmethod
    def _check_repos(cls, v: list[SourceRepo]) -> list[SourceRepo]:
        if not v:
            raise ValueError("至少需要一个源码工程（repos 不能为空）")
        names = [r.display_name for r in v]
        if len(set(names)) != len(names):
            raise ValueError(f"源码工程名重复：{names}（请为每个工程指定唯一 name）")
        return v

    @property
    def tmp_dir(self) -> Path:
        """临时测试代码目录（HR-03：绝不在源码树内）。"""
        return self.workdir / "tmp"

    @property
    def repo_dir(self) -> Path:
        """兼容旧调用：返回首个源码工程的克隆目录。多工程请用 repo_dir_for(repo)。"""
        if self.repos:
            return self.repo_dir_for(self.repos[0])
        return self.workdir / "repo"

    def repo_dir_for(self, repo: SourceRepo) -> Path:
        """某个源码工程的克隆目录（按工程名隔离，避免多工程互相覆盖）。"""
        return self.workdir / "repos" / _safe_name(repo.display_name)


# ---------------------------------------------------------------------------
# 步骤 1 产出：DiffBundle
# ---------------------------------------------------------------------------


class DiffHunk(BaseModel):
    """一个 diff 片段。line 指的是**新文件**中的行号，便于报告直接定位。"""

    header: str = Field(default="", description="hunk 头，如 @@ -12,7 +12,9 @@")
    new_start: int = Field(description="新文件起始行号")
    new_lines: int = Field(default=0, description="新文件侧行数")
    added_line_numbers: list[int] = Field(default_factory=list, description="新增/修改行号")
    removed_line_numbers: list[int] = Field(default_factory=list, description="删除行号")
    content: str = Field(default="", description="hunk 原文（含 +/- 前缀）")


class FileDiff(BaseModel):
    """单个文件的变更。"""

    path: str = Field(description="相对仓库根的路径")
    change_type: str = Field(default="modified", description="added/modified/deleted/renamed")
    language: CodeLanguage = Field(default=CodeLanguage.UNKNOWN)
    hunks: list[DiffHunk] = Field(default_factory=list)
    added_lines: int = Field(default=0)
    removed_lines: int = Field(default=0)
    binary: bool = Field(default=False)
    skipped_reason: str | None = Field(default=None, description="被排除/跳过的说明")

    @property
    def all_added_line_numbers(self) -> list[int]:
        nums: list[int] = []
        for h in self.hunks:
            nums.extend(h.added_line_numbers)
        return sorted(set(nums))


class DiffBundle(BaseModel):
    """步骤 1 的聚合产出（每个源码工程一份）。"""

    project: str
    repo_name: str = Field(default="", description="所属源码工程名（多工程时区分归属）")
    branch: str
    base: str | None = None
    start_time: datetime | None = None
    end_time: datetime | None = None
    commits: list[str] = Field(default_factory=list, description="时间窗内的 commit sha 列表")
    files: list[FileDiff] = Field(default_factory=list)
    skipped_files: list[str] = Field(default_factory=list)
    degraded_reason: str | None = Field(default=None)
    # 步骤 1 顺带抽取的该工程 README 摘要（用于步骤 2 建立项目上下文，AGENTS.MD 步骤 1/2）。
    # 缺失时 readme 置空、readme_absent 置 True（报告第 7 章声明，不视为流程降级）。
    readme: str = Field(default="", description="工程 README 摘要（步骤2 建立项目上下文）")
    readme_absent: bool = Field(default=False, description="该工程是否未提供 README")

    @property
    def reviewable_files(self) -> list[FileDiff]:
        return [f for f in self.files if not f.binary and f.hunks and not f.skipped_reason]

    @property
    def total_added(self) -> int:
        return sum(f.added_lines for f in self.files)

    @property
    def total_removed(self) -> int:
        return sum(f.removed_lines for f in self.files)

    @property
    def is_empty(self) -> bool:
        return not self.reviewable_files


# ---------------------------------------------------------------------------
# 步骤 2 产出：StaticFinding
# ---------------------------------------------------------------------------


class StaticFinding(BaseModel):
    """一条静态审查缺陷。file/line 为强制项，对应 HR-02。"""

    id: str = Field(description="缺陷编号，如 SEC-001")
    dimension: ReviewDimension
    severity: Severity
    rule_id: str = Field(default="", description="缺陷来源标记，如 LLM-SEMANTIC / HR-04")
    source: str = Field(
        default="", description="规约出处，如「阿里规约 01-(七)并发处理」；无出处时留空（报告显示为语义审查）"
    )
    repo_name: str = Field(default="", description="所属源码工程（多工程时区分归属）")
    file: str
    line: int = Field(ge=0)
    title: str
    evidence: str = Field(default="", description="变更代码片段")
    reason: str = Field(description="判定理由链")
    suggestion: str = Field(description="可执行的修改建议")
    needs_human: bool = Field(default=False, description="证据不足，需人工确认（HR-11）")
    confirmed_by_test: bool | None = Field(
        default=None, description="复现用例运行结果：True 确认 / False 存疑 / None 无对应用例"
    )

    def escalate(self) -> None:
        """测试复现成功后上调一档，见步骤 6 合并规则。"""
        self.severity = SEVERITY_ESCALATION[self.severity]

    def demote(self) -> None:
        """测试通过 → 问题存疑，降级为 P3（HR-11）。"""
        self.severity = Severity.P3
        self.needs_human = True


# ---------------------------------------------------------------------------
# 步骤 3/4/5 产出：TestCase / TestArtifact / TestResult
# ---------------------------------------------------------------------------


class TestCaseOrigin(str, Enum):
    """用例来源，见步骤 3 三类齐备要求。"""

    REQUIREMENT = "需求符合性"
    BEHAVIOR = "变更行为"
    REGRESSION = "回归防护"
    REPRODUCE = "缺陷复现"


class TestCase(BaseModel):
    id: str = Field(description="用例编号，如 TC-001")
    title: str
    origin: TestCaseOrigin
    precondition: str = Field(default="", description="前置条件")
    input_desc: str = Field(default="", description="输入")
    expected: str = Field(description="期望输出，必须是可断言的确定值")
    requirement_ref: str = Field(default="", description="关联需求点")
    diff_ref: str = Field(default="", description="关联的 diff 位置 file:line")
    finding_id: str | None = Field(default=None, description="复现用例对应的缺陷编号")
    repo_name: str = Field(default="", description="所属源码工程（多工程时分组生成与执行）")


class TestArtifact(BaseModel):
    """步骤 4 产出：可运行的临时测试代码。

    workspace 是仓库副本（测试工作台），测试文件写在副本内，
    被评审仓库始终不被修改（HR-03）。
    """

    language: CodeLanguage
    framework: str = Field(default="", description="pytest / JUnit5 / go test / vitest 等")
    test_dir: Path = Field(description="测试文件所在目录（位于测试工作台内）")
    workspace: Path = Field(default=Path("."), description="测试工作台：仓库副本根目录")
    files: list[str] = Field(default_factory=list, description="生成的测试文件相对路径")
    run_command: list[str] = Field(default_factory=list, description="运行命令")
    dependencies: list[str] = Field(default_factory=list, description="需要的依赖")
    case_ids: list[str] = Field(default_factory=list)
    degraded_reason: str | None = None


class TestResult(BaseModel):
    """步骤 5 产出：真实执行结果。禁止伪造（HR-06）。"""

    case_id: str
    status: TestStatus
    duration_ms: int = Field(default=0)
    message: str = Field(default="", description="失败摘要 / 断言差异")
    log_excerpt: str = Field(default="", description="stdout/stderr/traceback 摘录")
    retried: bool = Field(default=False)

    @property
    def counted(self) -> bool:
        """是否计入通过率。NOT_RUN 不计入（HR-06）。"""
        return self.status is not TestStatus.NOT_RUN


class TestCodeEntry(BaseModel):
    """步骤 4 生成的测试代码文件，随报告持久化。

    即使第 5 步未真实执行（NOT_RUN / 降级），其内容也照常保留，
    便于审查者人工复核生成的测试代码（见 AGENTS.MD 步骤 5）。
    """

    repo_name: str = Field(default="", description="所属源码工程（多工程时区分归属）")
    path: str = Field(description="测试文件相对工程根的路径")
    framework: str = Field(default="", description="pytest / JUnit5 / go test / vitest 等")
    content: str = Field(default="", description="测试文件完整内容")
    case_ids: list[str] = Field(default_factory=list, description="该文件覆盖的用例编号")
    executed: bool = Field(
        default=False, description="该工程是否真实执行过对应测试（HR-06）"
    )

    @computed_field
    @property
    def fence(self) -> str:
        """代码围栏字符：内容里最长连续反引号数 + 1，至少 3 个。

        防止测试文件自身含 ``` 时撑破渲染围栏。
        """
        n = 3
        run = 0
        for ch in self.content:
            if ch == "`":
                run += 1
                if run + 1 > n:
                    n = run + 1
            else:
                run = 0
        return "`" * n


# ---------------------------------------------------------------------------
# 步骤 6 产出：ReviewReport
# ---------------------------------------------------------------------------


class RequirementCheckItem(BaseModel):
    """需求符合性核对表的一行：需求点 → 用例 → 结果。"""

    requirement_point: str
    case_ids: list[str] = Field(default_factory=list)
    passed: bool = False
    note: str = ""


class ReviewReport(BaseModel):
    """最终报告的结构化数据。渲染为 Markdown 由 report 层负责。"""

    project: str
    generated_at: datetime
    verdict: ReviewVerdict
    verdict_reason: str
    summary: dict[str, Any] = Field(default_factory=dict)
    findings: list[StaticFinding] = Field(default_factory=list)
    test_results: list[TestResult] = Field(default_factory=list)
    test_cases: list[TestCase] = Field(default_factory=list)
    test_code: list[TestCodeEntry] = Field(
        default_factory=list, description="生成的测试代码文件内容（步骤 4 产物，章节 5.5）"
    )
    requirement_checks: list[RequirementCheckItem] = Field(default_factory=list)
    out_of_scope: list[str] = Field(default_factory=list, description="diff 外的问题（HR-01）")
    degradation_notes: list[str] = Field(default_factory=list, description="流程降级说明（HR-09）")
    # README 缺失声明：仅在第 7 章展示，不影响 decide_verdict（缺失 README 不属于流程降级）。
    readme_notes: list[str] = Field(
        default_factory=list, description="各工程 README 缺失声明（AGENTS.MD 步骤 1/2）"
    )
    markdown: str = Field(default="", description="渲染后的 Markdown 全文")

    @property
    def p0_count(self) -> int:
        return sum(1 for f in self.findings if f.severity is Severity.P0)

    @property
    def p1_count(self) -> int:
        return sum(1 for f in self.findings if f.severity is Severity.P1)
