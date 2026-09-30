"""多语言运行器测试：模板命名、结果解析、依赖缓存暴露。"""

from __future__ import annotations

import json
import xml.etree.ElementTree as ET
from pathlib import Path

from kanma.models.schemas import (
    CodeLanguage,
    ProjectConfig,
    TestCase,
    TestCaseOrigin,
    TestStatus,
)
from kanma.runners import detect_runner, get_runner
from kanma.runners.base import RunnerContext, case_fn_camel, case_fn_exported, case_fn_python
from kanma.runners.go import _module_package
from kanma.tools.sandbox import Sandbox


def _cfg(tmp_path: Path) -> ProjectConfig:
    from datetime import datetime, timedelta

    from kanma.models.schemas import RequirementDoc, SourceRepo

    return ProjectConfig(
        name="t",
        repos=[
            SourceRepo(
                name="demo",
                git_url="x",
                branch="main",
                start_time=datetime.now() - timedelta(days=1),
                end_time=datetime.now(),
            )
        ],
        requirement=RequirementDoc(),
        workdir=tmp_path / "w",
    )


def _cases() -> list[TestCase]:
    return [
        TestCase(id="TC-001", title="满减计算", origin=TestCaseOrigin.REQUIREMENT,
                 expected="优惠 10 元", diff_ref="a.py:1"),
        TestCase(id="TC-002", title="边界金额", origin=TestCaseOrigin.BEHAVIOR,
                 expected="抛参数异常", diff_ref="a.py:2"),
    ]


def _ctx(cfg: ProjectConfig, tmp_path: Path) -> RunnerContext:
    return RunnerContext(
        cfg=cfg,
        sandbox=Sandbox(backend="local"),
        test_dir=tmp_path / "w" / "tmp" / "workspace",
        workspace=tmp_path / "w" / "tmp" / "workspace",
    )


# ---------------------------------------------------------------------------
# 命名映射
# ---------------------------------------------------------------------------


def test_case_function_names():
    assert case_fn_python("TC-001") == "test_tc_001"
    assert case_fn_camel("TC-001") == "testTc001"
    assert case_fn_exported("TC-001") == "TestTc001"


# ---------------------------------------------------------------------------
# 模板
# ---------------------------------------------------------------------------


def test_python_template_names_and_skips(tmp_path):
    r = get_runner(CodeLanguage.PYTHON)
    files = r.render_template(_cases(), _cfg(tmp_path))
    content = list(files.values())[0]
    assert "def test_tc_001()" in content
    assert "def test_tc_002()" in content
    assert content.count("pytest.skip") == 2, "骨架必须显式 skip，不得留下空断言伪装通过"


def test_java_template_names_and_disabled(tmp_path):
    r = get_runner(CodeLanguage.JAVA)
    files = r.render_template(_cases(), _cfg(tmp_path))
    assert "src/test/java/kanma/KanmaGeneratedTest.java" in files
    content = files["src/test/java/kanma/KanmaGeneratedTest.java"]
    assert "void testTc001()" in content
    assert content.count("@Disabled") == 2


def test_go_template_names_and_skip(tmp_path):
    r = get_runner(CodeLanguage.GO)
    r.package = "orders"
    files = r.render_template(_cases(), _cfg(tmp_path))
    content = files["kanma_generated_test.go"]
    assert "package orders" in content
    assert "func TestTc001(t *testing.T)" in content
    assert content.count("t.Skip") == 2


def test_js_template_uses_case_id_in_title(tmp_path):
    r = get_runner(CodeLanguage.TYPESCRIPT)
    files = r.render_template(_cases(), _cfg(tmp_path))
    content = files["kanma.generated.test.ts"]
    assert "TC-001" in content and "TC-002" in content
    assert content.count("it.skip") == 2


def test_templates_survive_multiline_case_fields(tmp_path):
    """回归：用例字段内嵌换行时，模板注释/字符串不得把第二行变成裸代码。

    实际故障：input_desc 为多行步骤时，Java 模板 `// 输入：` 注释的第二行
    变成裸 Java 代码，javac 报「非法的类型开始」，整批测试编译失败。
    """
    import ast

    leaky = TestCase(
        id="TC-017",
        title="Setter/Getter 双向一致性\n2. 调用 setExecutorKey(\"EXEC-001\")",
        origin=TestCaseOrigin.BEHAVIOR,
        precondition="类已编译加载\n2. 修改其 taskCount 字段",
        input_desc="1. new PullTaskExecutorStatus()\n2. 调用 setExecutorKey(\"EXEC-001\")",
        expected="返回值类型严格匹配\n2. 附加断言行",
        diff_ref="a.java:1",
    )
    cfg = _cfg(tmp_path)

    # Java / Go：所有携带字段内容的行必须是注释行（换行被折叠为「；」）
    for lang, filename in (
        (CodeLanguage.JAVA, "src/test/java/kanma/KanmaGeneratedTest.java"),
        (CodeLanguage.GO, "kanma_generated_test.go"),
    ):
        r = get_runner(lang)
        if lang is CodeLanguage.GO:
            r.package = "orders"
        content = r.render_template([leaky], cfg)[filename]
        for ln in content.splitlines():
            if any(k in ln for k in ("setExecutorKey", "taskCount", "附加断言行")):
                assert ln.lstrip().startswith("//"), f"{lang.value} 字段内容泄漏为裸代码：{ln!r}"
        assert "\n2. " not in content.replace("；\n", ""), lang

    # Python：注释 + docstring，整个文件必须可被 ast 解析（含内嵌三引号防炸）
    r = get_runner(CodeLanguage.PYTHON)
    tricky = leaky.model_copy(update={"title": '标题带 """ 三引号\n2. 第二行'})
    content = r.render_template([tricky], cfg)["kanma_tests/test_kanma_generated.py"]
    ast.parse(content)  # 语法非法会直接抛异常

    # JS/TS：字符串字面量必须保持单行（换行进 '...' 是非法 JS）
    r = get_runner(CodeLanguage.TYPESCRIPT)
    content = r.render_template([leaky], cfg)["kanma.generated.test.ts"]
    for ln in content.splitlines():
        if "it.skip(" in ln:
            assert ln.rstrip().endswith("', () => {"), f"it.skip 标题跨行：{ln!r}"
            assert "；2. 调用" in ln, f"标题未折叠为单行：{ln!r}"


def test_go_package_from_gomod(tmp_path):
    (tmp_path / "go.mod").write_text("module github.com/acme/order-service\n\ngo 1.22\n")
    assert _module_package(tmp_path) == "order-service"
    assert _module_package(tmp_path / "none") == "kanma"


# ---------------------------------------------------------------------------
# 结果解析
# ---------------------------------------------------------------------------


def test_python_parse_maps_status(tmp_path):
    cfg = _cfg(tmp_path)
    r = get_runner(CodeLanguage.PYTHON)
    out = (
        "kanma_tests/test_kanma_generated.py::test_tc_001 PASSED [ 50%]\n"
        "kanma_tests/test_kanma_generated.py::test_tc_002 FAILED [100%]\n"
    )
    parsed = r.parse(out, _cases(), _ctx(cfg, tmp_path))
    assert parsed["TC-001"].status is TestStatus.PASSED
    assert parsed["TC-002"].status is TestStatus.FAILED
    assert parsed["TC-002"].counted is True


def test_python_parse_skip_is_not_run(tmp_path):
    cfg = _cfg(tmp_path)
    r = get_runner(CodeLanguage.PYTHON)
    out = "kanma_tests/test_x.py::test_tc_001 SKIPPED (LLM 不可用) [ 50%]\n"
    parsed = r.parse(out, _cases(), _ctx(cfg, tmp_path))
    assert parsed["TC-001"].status is TestStatus.NOT_RUN
    assert parsed["TC-001"].counted is False, "SKIP 绝不能计入通过率（HR-06）"
    assert parsed["TC-002"].status is TestStatus.NOT_RUN, "未出现在输出中的用例记为 NOT_RUN"


def test_go_parse(tmp_path):
    cfg = _cfg(tmp_path)
    r = get_runner(CodeLanguage.GO)
    out = "=== RUN   TestTc001\n--- PASS: TestTc001 (0.00s)\n--- SKIP: TestTc002 (0.00s)\n"
    parsed = r.parse(out, _cases(), _ctx(cfg, tmp_path))
    assert parsed["TC-001"].status is TestStatus.PASSED
    assert parsed["TC-002"].status is TestStatus.NOT_RUN


def test_java_parse_surefire_xml(tmp_path):
    cfg = _cfg(tmp_path)
    ws = tmp_path / "w" / "tmp" / "workspace"
    reports = ws / "target" / "surefire-reports"
    reports.mkdir(parents=True)
    root = ET.Element("testsuite", {"name": "kanma.KanmaGeneratedTest"})
    tc1 = ET.SubElement(root, "testcase", {"name": "testTc001", "classname": "kanma.KanmaGeneratedTest"})
    tc2 = ET.SubElement(root, "testcase", {"name": "testTc002", "classname": "kanma.KanmaGeneratedTest"})
    ET.SubElement(tc1, "failure", {"message": "expected 10 but was 0"})
    ET.SubElement(tc2, "skipped")
    ET.ElementTree(root).write(reports / "TEST-kanma.KanmaGeneratedTest.xml")

    r = get_runner(CodeLanguage.JAVA)
    parsed = r.parse("", _cases(), _ctx(cfg, tmp_path))
    assert parsed["TC-001"].status is TestStatus.FAILED
    assert parsed["TC-002"].status is TestStatus.NOT_RUN


def test_js_parse_vitest_json(tmp_path):
    cfg = _cfg(tmp_path)
    payload = {
        "testResults": [
            {
                "assertionResults": [
                    {"fullName": "勘码生成用例 > TC-001 满减计算", "status": "passed"},
                    {"fullName": "勘码生成用例 > TC-002 边界金额", "status": "pending"},
                ]
            }
        ]
    }
    out = "some noise\n" + json.dumps(payload) + "\ntrailing"
    r = get_runner(CodeLanguage.JAVASCRIPT)
    parsed = r.parse(out, _cases(), _ctx(cfg, tmp_path))
    assert parsed["TC-001"].status is TestStatus.PASSED
    assert parsed["TC-002"].status is TestStatus.NOT_RUN


def test_parse_failure_falls_back_to_not_run(tmp_path):
    """解析不出来时必须是 NOT_RUN，绝不能默认当成通过。"""
    cfg = _cfg(tmp_path)
    for lang in (CodeLanguage.PYTHON, CodeLanguage.GO, CodeLanguage.JAVASCRIPT):
        r = get_runner(lang)
        parsed = r.parse("unparsable garbage", _cases(), _ctx(cfg, tmp_path))
        assert all(v.status is TestStatus.NOT_RUN for v in parsed.values()), lang


# ---------------------------------------------------------------------------
# 依赖缓存与探测
# ---------------------------------------------------------------------------


def test_cache_dirs_exposed():
    """隔离环境无网，缓存目录必须暴露给沙箱挂载。"""
    assert get_runner(CodeLanguage.GO).cache_dirs
    assert get_runner(CodeLanguage.JAVASCRIPT).cache_dirs
    r = get_runner(CodeLanguage.JAVA)
    r.build_tool = "maven"
    from pathlib import Path as P

    r.cache_dirs = [P.home() / ".m2"]
    assert r.cache_dirs


def test_detect_runner_by_build_file(tmp_path):
    (tmp_path / "pom.xml").write_text("<project/>")
    assert detect_runner(tmp_path).framework == "JUnit5"

    (tmp_path / "pom.xml").unlink()
    (tmp_path / "go.mod").write_text("module x")
    assert detect_runner(tmp_path).framework == "go testing"

    (tmp_path / "go.mod").unlink()
    (tmp_path / "package.json").write_text("{}")
    assert detect_runner(tmp_path).framework == "vitest"


def test_detect_runner_prefers_build_file_over_hint(tmp_path):
    """diff 主语言是 Python，但仓库是 Maven 项目时，应以构建文件为准。"""
    (tmp_path / "pom.xml").write_text("<project/>")
    assert detect_runner(tmp_path, CodeLanguage.PYTHON).framework == "JUnit5"


def test_prepare_workspace_skips_unreadable_files(tmp_path, monkeypatch):
    """敏感文件被拒访时，工作台复制只跳过该文件，不炸整个 copytree。

    复刻线上故障：仓库含密钥类文件（如 TrustMerchant.properties），沙箱内容
    拦截 shim 对它拒访 → shutil.copytree 聚合抛 Error → 整个评审 job 崩掉。
    """
    import shutil as _shutil

    from kanma.runners import prepare_workspace

    cfg = _cfg(tmp_path)
    repo = cfg.repos[0]
    repo_dir = cfg.repo_dir_for(repo)
    # 造一个带子目录的正常仓库
    (repo_dir / "src" / "main").mkdir(parents=True)
    (repo_dir / "src" / "main" / "App.java").write_text("class App {}", encoding="utf-8")
    (repo_dir / "resources").mkdir()
    (repo_dir / "resources" / "app.properties").write_text("k=v", encoding="utf-8")
    sensitive = repo_dir / "resources" / "TrustMerchant.properties"
    sensitive.write_text("placeholder", encoding="utf-8")

    real_copy2 = _shutil.copy2

    def copy2_denying_sensitive(src, dst, *a, **kw):
        if Path(src).name == "TrustMerchant.properties":
            raise OSError("Sensitive content access was denied.")
        return real_copy2(src, dst, *a, **kw)

    def copytree_brokered_error(src, dst, *a, **kw):
        # 复刻沙箱 shim 的行为：整次 copytree 聚合抛 Error（注意 copytree 的
        # copy_function 是定义时绑定，patch 模块级 copy2 影响不到它，必须连
        # copytree 一起替换才贴近线上路径）
        raise _shutil.Error(
            [(str(sensitive), str(Path(dst) / sensitive.name), "Sensitive content access was denied.")]
        )

    monkeypatch.setattr(_shutil, "copytree", copytree_brokered_error)
    monkeypatch.setattr(_shutil, "copy2", copy2_denying_sensitive)

    skipped: list[str] = []
    ws = prepare_workspace(cfg, repo, skipped=skipped)

    assert ws.exists(), "工作台应建立成功"
    assert (ws / "src" / "main" / "App.java").exists(), "正常文件应照常复制"
    assert (ws / "resources" / "app.properties").exists()
    assert not (ws / "resources" / "TrustMerchant.properties").exists(), "敏感文件应被跳过"
    assert skipped == ["resources/TrustMerchant.properties"]
