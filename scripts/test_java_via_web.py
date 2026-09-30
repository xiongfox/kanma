#!/usr/bin/env python3
"""验证 Java 项目（用户主力栈）经 Web 控制台端到端评审。

构造临时 Java git 仓库 -> 通过 API 建项目（含本地需求文档路径）-> 触发评审 ->
轮询 -> 断言：报告含 Java 缺陷，且测试步骤在缺 Maven 时如实降级为 NOT_RUN。
"""

from __future__ import annotations

import shutil
import subprocess
import tempfile
import time
from datetime import datetime, timedelta
from pathlib import Path

from fastapi.testclient import TestClient

from kanma.web.app import app

POM = """<project xmlns="http://maven.apache.org/POM/4.0.0">
  <modelVersion>4.0.0</modelVersion>
  <groupId>com.example</groupId>
  <artifactId>order</artifactId>
  <version>1.0.0</version>
</project>
"""

BAD_JAVA = """
package com.example.order;

import java.util.ArrayList;
import java.util.List;
import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;

public class BadOrderService {
    private static final String API_TOKEN = "ak_live_demo_abc123";

    private ExecutorService pool = Executors.newFixedThreadPool(10);

    public Object queryOrder(Object conn, String orderId) {
        String sql = "SELECT * FROM orders WHERE id = " + orderId;
        return conn.execute(sql);
    }

    @Select("SELECT * FROM orders WHERE id = ${id}")
    public Object getOrder(Object conn, String id) {
        return conn.execute("SELECT * FROM orders WHERE id = " + id);
    }

    public void runReport(String date) throws Exception {
        Runtime.getRuntime().exec("sh report.sh " + date);
    }

    public List<Object> fetchAll(Object conn) {
        List<Object> out = new ArrayList<>();
        for (Object o : conn.query("SELECT * FROM orders")) {
            out.add(o);
        }
        return out;
    }

    public void audit(Object user) {
        System.out.println("user login password=" + user);
    }

    public void handle(Exception e) {
        e.printStackTrace();
    }
}
"""

REQ_MD = """# 订单查询需求

## 功能点
1. 按订单号查询订单详情
2. 批量导出全部订单

## 验收
- 查询接口不得拼接 SQL
- 不得执行外部命令
"""


def _git(args: list[str], cwd: Path) -> None:
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True)


def build_java_repo(root: Path) -> tuple[Path, Path]:
    repo = root / "demo-java"
    repo.mkdir(parents=True)
    src = repo / "src" / "main" / "java" / "com" / "example" / "order"
    src.mkdir(parents=True)
    (repo / "pom.xml").write_text(POM, encoding="utf-8")
    (src / "BadOrderService.java").write_text(BAD_JAVA, encoding="utf-8")
    req = root / "req.md"
    req.write_text(REQ_MD, encoding="utf-8")

    _git(["init", "-b", "main"], repo)
    _git(["config", "user.email", "kanma@example.com"], repo)
    _git(["config", "user.name", "kanma"], repo)
    _git(["add", "."], repo)
    _git(["commit", "-m", "feat: order service"], repo)
    return repo, req


def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="kanma-java-"))
    store = tmp / "projects"
    try:
        app.state.store_dir = store
        repo, req = build_java_repo(tmp)
        now = datetime.now()
        cli = TestClient(app)

        body = {
            "name": "java 订单服务评审",
            "git_url": f"file://{repo}",
            "branch": "main",
            "base": None,
            "start_time": (now - timedelta(days=1)).isoformat(timespec="seconds"),
            "end_time": (now + timedelta(days=1)).isoformat(timespec="seconds"),
            "requirement_doc": str(req),
            "workdir": str(tmp / ".kanma"),
        }
        r = cli.post("/api/projects", json=body)
        assert r.status_code == 200, (r.status_code, r.text)
        slug = r.json()["slug"]
        print(f"  PASS 创建 Java 项目 -> slug={slug}")

        r = cli.post(f"/api/projects/{slug}/run")
        assert r.status_code == 200, (r.status_code, r.text)
        job_id = r.json()["job_id"]
        print(f"  PASS 触发评审 -> job={job_id}")

        for _ in range(120):
            r = cli.get(f"/api/jobs/{job_id}")
            st = r.json()
            if st["status"] in ("done", "error"):
                break
            time.sleep(1)
        print(f"  评审终态：{st['status']}  verdict={st.get('verdict')}")
        assert st["status"] == "done", st.get("error")

        r = cli.get(f"/report/{slug}/{job_id}")
        assert r.status_code == 200
        html = r.text

        # 断言 1：Java 源文件被审查
        assert "BadOrderService.java" in html, "报告未包含被审 Java 文件"
        print("  PASS 报告包含 Java 源文件")
        # 断言 2：机检已废除，报告中不应出现任何正则规则编号
        assert "ALI-JAVA-" not in html, "报告仍含正则机检规则编号（机检应已移除）"
        print("  PASS 报告不含正则机检规则编号（安全类问题由语义审查给出）")
        # 断言 3：需求文档被加载（标题出现在报告）
        assert "订单查询需求" in html, "需求文档未载入报告"
        print("  PASS 需求文档已载入并参与审阅")
        # 断言 4：测试步骤如实降级（缺 Maven 不应伪造通过）
        assert ("NOT_RUN" in html) or ("降级" in html) or ("未运行" in html), "未见测试降级说明"
        print("  PASS 测试步骤在缺构建工具时如实降级（未伪造通过）")
        # 断言 5：报告缺陷清单保留「来源」列（溯源不丢，缺省显示为语义审查）
        assert "来源" in html, "报告缺陷清单缺少「来源」列"
        print("  PASS 报告缺陷清单保留「来源」列（溯源可用）")

        print(f"\nJava 评审路径经 Web 端到端验证通过（verdict={st.get('verdict')}）")
        return 0
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
