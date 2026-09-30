"""diff 行迭代与缺陷排序测试。

勘码已移除全部正则机检规则（无规则表、无 `scan_diff`），缺陷由 LLM 按规约语义审查得出。
`kanma.rules.scan` 只保留两类通用工具，本文件锁定它们的行为：

- `iter_added_lines`：只产出**新增/修改行**且行号落在**新文件侧**（HR-01 / HR-02 的基础）；
- `sort_findings`：报告按 级别 → 维度 → 文件 → 行号 排序（安全类排在最前）。
"""

from kanma.models.schemas import (
    CodeLanguage,
    DiffBundle,
    DiffHunk,
    FileDiff,
    ReviewDimension,
    Severity,
    StaticFinding,
)
from kanma.rules.scan import iter_added_lines, iter_file_added_lines, sort_findings

JAVA_SAMPLE = r"""@@ -1,3 +1,13 @@
 package demo;
+public class OrderService {
+    private ExecutorService pool = Executors.newFixedThreadPool(10);
+
+    @Select("SELECT * FROM t_order WHERE user_id = ${userId}")
+    public List<OrderDO> list(Long userId) {
+        return mapper.select(userId);
+    }
+}
"""


def _java_diff() -> DiffBundle:
    fd = FileDiff(
        path="src/main/java/OrderService.java",
        language=CodeLanguage.JAVA,
        additions=8,
        deletions=0,
        hunks=[
            DiffHunk(
                old_start=1, old_lines=3, new_start=1, new_lines=13, content=JAVA_SAMPLE
            )
        ],
    )
    return DiffBundle(repo_name="demo", project="demo", branch="main", files=[fd])


def test_only_added_lines_are_yielded():
    """HR-01：迭代器只产出新增行，不产出上下文行与删除行。"""
    added = [(a.file, a.line, a.text) for a in iter_added_lines(_java_diff())]
    assert added, "Java 样本应有新增行"
    assert all(t.startswith("+") is False for _, _, t in added), "不应带 diff 前缀"
    assert all("package demo;" not in t for _, _, t in added), "上下文行不应被产出"


def test_added_line_numbers_follow_new_file():
    """HR-02：行号必须是新文件侧行号，与 hunk 头的 new_start 对齐。"""
    fd = _java_diff().reviewable_files[0]
    lines = list(iter_file_added_lines(fd))
    start = fd.hunks[0].new_start
    # 首行是上下文（package demo;），第二个新行即第一个新增行 → 行号 = start + 1
    assert lines[0].line == start + 1
    for i, a in enumerate(lines):
        assert a.line == start + 1 + i
        assert a.language == "java"
        assert a.file == fd.path


def test_sort_findings_puts_security_first():
    def f(sev, dim, file, line):
        return StaticFinding(
            id="X", dimension=dim, severity=sev, file=file, line=line,
            title="t", reason="r", suggestion="s",
        )

    items = [
        f(Severity.P2, ReviewDimension.NAMING, "a.java", 1),
        f(Severity.P0, ReviewDimension.CORRECTNESS, "z.java", 9),
        f(Severity.P0, ReviewDimension.SECURITY, "b.java", 5),
        f(Severity.P1, ReviewDimension.PERFORMANCE, "a.java", 2),
    ]
    ordered = sort_findings(items)
    assert [x.severity for x in ordered] == [Severity.P0, Severity.P0, Severity.P1, Severity.P2]
    assert ordered[0].dimension is ReviewDimension.SECURITY, "同级中安全维度排最前"
