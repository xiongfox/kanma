#!/usr/bin/env python3
"""勘码离线冒烟脚本。

不依赖 LLM：自行构造一个带缺陷的临时 git 仓库，跑通六步流程，
验证 diff 采集 → 语义审查（离线降级为无结论）→ 推断用例 → 模板测试代码 → 沙箱执行 → 报告生成。

用法：
    python scripts/smoke.py            # 完整跑（含沙箱执行测试）
    python scripts/smoke.py --no-sandbox   # 跳过第 5 步的真实执行
"""

from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
import tempfile
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from kanma.graph import run_review  # noqa: E402
from kanma.llm import LLMContext  # noqa: E402
from kanma.models.schemas import ProjectConfig, RequirementDoc, SourceRepo  # noqa: E402
from kanma.tools.sandbox import Sandbox  # noqa: E402

PYTHON_SAMPLE = '''
import os
import pickle
import subprocess
import logging

log = logging.getLogger(__name__)

API_TOKEN = "PLACEHOLDER_CREDENTIAL_VALUE"
DB_PASSWORD = "PLACEHOLDER_DB_CREDENTIAL"


def query_order(conn, order_id):
    sql = "SELECT * FROM orders WHERE id = " + order_id
    return conn.execute(sql)


def calc_discount(amount):
    if amount >= 200:
        return 30
    # TODO 处理边界
    return 10


def run_report(date):
    os.system("sh report.sh " + date)
    subprocess.run("rm -rf /tmp/" + date, shell=True)


def load_cache(data):
    return pickle.loads(data)


def audit(user, request):
    log.info("user login, password=%s, phone=%s", user.password, user.phone)
    return True


def sum_prices(prices):
    total = ""
    for p in prices:
        total += str(p)
    return total


def fetch_all(conn):
    for row in conn.query("SELECT * FROM orders"):
        print(row)
'''

JAVA_SAMPLE = '''package demo;

import java.sql.Connection;
import java.sql.ResultSet;
import java.sql.SQLException;
import java.util.logging.Logger;

public class OrderService {

    private static final Logger log = Logger.getLogger(OrderService.class.getName());
    private static final String API_TOKEN = "PLACEHOLDER_CREDENTIAL_VALUE";

    public ResultSet queryOrder(Connection conn, String orderId) throws SQLException {
        String sql = "SELECT * FROM orders WHERE id = " + orderId;
        return conn.createStatement().executeQuery(sql);
    }

    public int calcDiscount(int amount) {
        if (amount >= 200) {
            return 30;
        }
        // TODO 处理边界
        return 10;
    }

    public boolean audit(User user) {
        log.info("user login, password=" + user.getPassword());
        return true;
    }
}
'''

GO_SAMPLE = '''package orders

import (
\t"database/sql"
\t"log"
)

const APIToken = "PLACEHOLDER_CREDENTIAL_VALUE"

func QueryOrder(db *sql.DB, orderID string) (*sql.Rows, error) {
\tq := "SELECT * FROM orders WHERE id = " + orderID
\treturn db.Query(q)
}

func CalcDiscount(amount int) int {
\t// TODO 处理边界
\tif amount >= 200 {
\t\treturn 30
\t}
\treturn 10
}

func Audit(user *User) {
\tlog.Printf("user login, password=%s", user.Password)
}
'''

JS_SAMPLE = '''const API_TOKEN = "PLACEHOLDER_CREDENTIAL_VALUE";

function queryOrder(db, orderId) {
  const sql = "SELECT * FROM orders WHERE id = " + orderId;
  return db.query(sql);
}

function calcDiscount(amount) {
  // TODO 处理边界
  return amount >= 200 ? 30 : 10;
}

function audit(user) {
  console.log("user login, password=", user.password);
  return true;
}

module.exports = { queryOrder, calcDiscount, audit };
'''

# 语言 → (源码相对路径, 源码内容, 构建文件相对路径, 构建文件内容)
SAMPLES: dict[str, tuple[str, str, str, str]] = {
    "python": ("order_service.py", PYTHON_SAMPLE, "", ""),
    "java": (
        "src/main/java/demo/OrderService.java",
        JAVA_SAMPLE,
        "pom.xml",
        '<?xml version="1.0" encoding="UTF-8"?>\n<project>\n'
        "  <modelVersion>4.0.0</modelVersion>\n"
        "  <groupId>demo</groupId>\n  <artifactId>order-service</artifactId>\n"
        "  <version>1.0.0</version>\n</project>\n",
    ),
    "go": (
        "order.go",
        GO_SAMPLE,
        "go.mod",
        "module demo/orders\n\ngo 1.22\n",
    ),
    "js": (
        "order.js",
        JS_SAMPLE,
        "package.json",
        '{"name": "order-service", "version": "1.0.0"}\n',
    ),
}

REQUIREMENT = """# 订单折扣需求

- 单笔订单金额满 100 元减 10 元，满 200 元减 30 元。
- 折扣不可叠加，取满足条件的最大优惠。
- 订单金额为 0 或负数时返回参数错误。
"""


def _git(args: list[str], cwd: Path) -> None:
    subprocess.run(["git", *args], cwd=str(cwd), check=True, capture_output=True, text=True)


def build_sample_repo(root: Path, lang: str = "python") -> Path:
    src_rel, src, build_rel, build = SAMPLES[lang]
    repo = root / "demo-order-service"
    repo.mkdir(parents=True)
    _git(["init", "-b", "main"], repo)
    _git(["config", "user.email", "kanma@example.com"], repo)
    _git(["config", "user.name", "kanma"], repo)
    src_path = repo / src_rel
    src_path.parent.mkdir(parents=True, exist_ok=True)
    src_path.write_text(src, encoding="utf-8")
    if build_rel:
        (repo / build_rel).write_text(build, encoding="utf-8")
    (repo / "notes.txt").write_text("not source", encoding="utf-8")
    _git(["add", "."], repo)
    _git(["commit", "-m", f"feat: order discount ({lang})"], repo)
    return repo


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--no-sandbox", action="store_true", help="跳过第 5 步真实执行")
    ap.add_argument("--keep", action="store_true", help="保留临时目录")
    ap.add_argument(
        "--lang", default="python", choices=sorted(SAMPLES), help="样例项目语言（默认 python）"
    )
    args = ap.parse_args()

    tmp = Path(tempfile.mkdtemp(prefix="kanma-smoke-"))
    try:
        repo = build_sample_repo(tmp, args.lang)
        now = datetime.now()
        cfg = ProjectConfig(
            name=f"smoke-{args.lang}",
            repos=[
                SourceRepo(
                    name=f"smoke-{args.lang}",
                    git_url=str(repo),
                    branch="main",
                    start_time=now - timedelta(days=1),
                    end_time=now + timedelta(days=1),
                )
            ],
            requirement=RequirementDoc(title="订单折扣需求", raw_text=REQUIREMENT),
            workdir=tmp / "workdir",
        )
        ctx = LLMContext(False, "", reason="冒烟：未配置 KANMA_MODEL")
        sandbox = Sandbox(backend="local", timeout=120)

        print("勘码 · 离线冒烟")
        print(f"  样例语言：{args.lang}")
        print(f"  临时仓库：{repo}")
        print(f"  沙箱后端：{sandbox.resolved_backend}")
        print()

        report, state = run_review(cfg, ctx=ctx, sandbox=sandbox, output_dir=tmp / "docs")

        print("── 流程轨迹 ──")
        for line in state.get("steps_log", []):
            print(f"  {line}")
        print()

        print("── 降级说明 ──")
        for n in state.get("degradation_notes", []):
            print(f"  - {n}")
        print()

        print("── 静态缺陷 ──")
        for f in report.findings:
            mark = "⚠" if f.needs_human else " "
            print(f"  {mark} [{f.severity.value:<8}] {f.id} {f.file}:{f.line} · {f.title}")
        print()

        print("── 测试结果 ──")
        for r in report.test_results:
            print(f"  {r.case_id}: {r.status.value} {r.message}")
        print()

        print(f"结论：{report.verdict.value} —— {report.verdict_reason}")
        print(f"统计：P0×{report.p0_count}  P1×{report.p1_count}  合计 {len(report.findings)}")

        out = tmp / "docs"
        generated = sorted(out.glob("review-report-*.md")) if out.exists() else []
        if generated:
            target = Path.cwd() / "docs" / generated[-1].name
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy(generated[-1], target)
            print(f"报告：{target}")

        # 断言
        assert report.findings, "静态审查不应为空"
        assert report.p0_count > 0, "样例代码应命中 P0"
        log = "\n".join(state.get("steps_log", []))
        for step in ("步骤1", "步骤2", "步骤3", "步骤4", "步骤6"):
            assert step in log, f"{step} 未执行"
        if not args.no_sandbox:
            assert "步骤5" in log, "步骤5 未执行"
        assert any("LLM 不可用" in n for n in state.get("degradation_notes", [])), "未声明降级"
        print("\n冒烟通过 ✓")
        return 0
    finally:
        if args.keep:
            print(f"临时目录保留：{tmp}")
        else:
            shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
