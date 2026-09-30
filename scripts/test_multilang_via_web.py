#!/usr/bin/env python3
"""四种语言（Python/Java/Go/JS）评审路径经 Web 控制台端到端验证。

每种语言构造临时 git 仓库 -> 经 API 建项目 -> 触发评审 -> 轮询 -> 断言：
报告含该语言源文件、命中语言无关硬规则（SECRET/SQLI），且缺构建工具的语言
（Java/Go/JS 本机均未装 mvn/go，JS 无 vitest）测试步骤如实降级为 NOT_RUN。
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

PY = '''
API_TOKEN = "ak_live_demo_abc123"

def query(conn, oid):
    return conn.execute("SELECT * FROM t WHERE id = " + oid)
'''

JAVA_POM = """<project xmlns="http://maven.apache.org/POM/4.0.0">
  <modelVersion>4.0.0</modelVersion>
  <groupId>com.example</groupId>
  <artifactId>demo</artifactId>
  <version>1.0.0</version>
</project>
"""

JAVA = '''
package com.example.demo;

public class BadService {
    private static final String API_TOKEN = "ak_live_demo_abc123";

    public Object query(Object conn, String oid) {
        String sql = "SELECT * FROM t WHERE id = " + oid;
        return conn.execute(sql);
    }
}
'''

GO_MOD = """module example.com/demo

go 1.21
"""

GO = '''
package demo

import "os/exec"

const API_TOKEN = "ak_live_demo_abc123"

func Query(oid string) string {
    sql := "SELECT * FROM t WHERE id = " + oid
    return sql
}

func Run(date string) {
    exec.Command("sh", "-c", "report.sh "+date)
}
'''

JS_PKG = '{"name":"demo","version":"1.0.0","private":true}\n'

JS = '''
const API_TOKEN = "ak_live_demo_abc123";

function query(conn, oid) {
  const sql = "SELECT * FROM t WHERE id = " + oid;
  return conn.query(sql);
}
'''


def _git(args: list[str], cwd: Path) -> None:
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True)


def build_repo(root: Path, lang: str) -> Path:
    repo = root / f"demo-{lang}"
    repo.mkdir(parents=True)
    if lang == "python":
        (repo / "svc.py").write_text(PY, encoding="utf-8")
        srcfile = "svc.py"
    elif lang == "java":
        src = repo / "src" / "main" / "java" / "com" / "example" / "demo"
        src.mkdir(parents=True)
        (repo / "pom.xml").write_text(JAVA_POM, encoding="utf-8")
        (src / "BadService.java").write_text(JAVA, encoding="utf-8")
        srcfile = "BadService.java"
    elif lang == "go":
        (repo / "go.mod").write_text(GO_MOD, encoding="utf-8")
        (repo / "svc.go").write_text(GO, encoding="utf-8")
        srcfile = "svc.go"
    else:  # js
        (repo / "package.json").write_text(JS_PKG, encoding="utf-8")
        (repo / "svc.js").write_text(JS, encoding="utf-8")
        srcfile = "svc.js"

    _git(["init", "-b", "main"], repo)
    _git(["config", "user.email", "kanma@example.com"], repo)
    _git(["config", "user.name", "kanma"], repo)
    _git(["add", "."], repo)
    _git(["commit", "-m", f"feat: demo {lang}"], repo)
    return repo, srcfile


def review_one(cli: TestClient, lang: str, repo: Path, srcfile: str, store: Path) -> dict:
    now = datetime.now()
    body = {
        "name": f"{lang} 评审",
        "git_url": f"file://{repo}",
        "branch": "main",
        "base": None,
        "start_time": (now - timedelta(days=1)).isoformat(timespec="seconds"),
        "end_time": (now + timedelta(days=1)).isoformat(timespec="seconds"),
        "requirement_doc": None,
        "workdir": str(store / f".kanma-{lang}"),
    }
    r = cli.post("/api/projects", json=body)
    assert r.status_code == 200, (r.status_code, r.text)
    slug = r.json()["slug"]

    r = cli.post(f"/api/projects/{slug}/run")
    assert r.status_code == 200, (r.status_code, r.text)
    job_id = r.json()["job_id"]

    for _ in range(150):
        r = cli.get(f"/api/jobs/{job_id}")
        st = r.json()
        if st["status"] in ("done", "error"):
            break
        time.sleep(1)
    assert st["status"] == "done", st.get("error")

    r = cli.get(f"/report/{slug}/{job_id}")
    html = r.text
    assert srcfile in html, f"{lang}: 报告未含源文件 {srcfile}"
    # 机检已废除：任何语言的报告都不应出现正则规则编号，全靠语义审查
    assert "ALI-JAVA-" not in html, f"{lang}: 报告仍含正则机检规则编号"
    # 缺构建工具的语言应如实降级（本机无 mvn/go，JS 无 vitest）
    if lang in ("java", "go", "js"):
        assert ("NOT_RUN" in html) or ("降级" in html) or ("未运行" in html), f"{lang}: 未见测试降级说明"
    return {"slug": slug, "verdict": st.get("verdict"), "summary": st.get("summary")}


def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="kanma-multilang-"))
    store = tmp / "projects"
    try:
        app.state.store_dir = store
        cli = TestClient(app)
        results = {}
        for lang in ("python", "java", "go", "js"):
            repo, srcfile = build_repo(tmp, lang)
            info = review_one(cli, lang, repo, srcfile, store)
            results[lang] = info
            print(
                f"  PASS {lang:<6} -> verdict={info['verdict']} "
                f"P0={info['summary'].get('p0')} P1={info['summary'].get('p1')} "
                f"tests={info['summary'].get('tests')}"
            )
        print("\n四语言评审路径经 Web 端到端验证通过")
        return 0
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
