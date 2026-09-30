#!/usr/bin/env python3
"""勘码 Web 控制台端到端验证（不依赖 pytest 的 conftest）。

流程：构造临时 git 仓库 -> 通过 API 创建项目 -> 触发评审 -> 轮询状态 -> 校验报告页。
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

SAMPLE = '''
def query_order(conn, order_id):
    sql = "SELECT * FROM orders WHERE id = " + order_id
    return conn.execute(sql)
'''

SAMPLE_B = '''
const API_TOKEN = "ak_live_demo_abc123";

function pay(conn, uid) {
  const sql = "SELECT * FROM pay WHERE uid = " + uid;
  return conn.query(sql);
}
'''


def _git(args: list[str], cwd: Path) -> None:
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True)


def build_repo(root: Path, name: str = "demo-repo", content: str = SAMPLE,
               srcfile: str = "svc.py") -> Path:
    repo = root / name
    repo.mkdir(parents=True)
    _git(["init", "-b", "main"], repo)
    _git(["config", "user.email", "kanma@example.com"], repo)
    _git(["config", "user.name", "kanma"], repo)
    (repo / srcfile).write_text(content, encoding="utf-8")
    _git(["add", "."], repo)
    _git(["commit", "-m", f"feat: {name}"], repo)
    return repo


def _repo_window(repo: Path, now: datetime, **extra) -> dict:
    return {
        "git_url": f"file://{repo}",
        "branch": "main",
        "base": None,
        "start_time": (now - timedelta(days=1)).isoformat(timespec="seconds"),
        "end_time": (now + timedelta(days=1)).isoformat(timespec="seconds"),
        **extra,
    }


def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="kanma-web-"))
    store = tmp / "projects"
    try:
        app.state.store_dir = store
        repo = build_repo(tmp)
        now = datetime.now()
        cli = TestClient(app)

        # 1) 首页
        r = cli.get("/")
        assert r.status_code == 200, r.status_code
        assert "勘码 KanMa" in r.text and 'id="projList"' in r.text
        print("  PASS GET /  -> 页面加载")

        # 2) 创建项目（无需求文档，走降级）
        body = {
            "name": "demo 评审",
            "git_url": f"file://{repo}",
            "branch": "main",
            "base": None,
            "start_time": (now - timedelta(days=1)).isoformat(timespec="seconds"),
            "end_time": (now + timedelta(days=1)).isoformat(timespec="seconds"),
            "requirement_doc": None,
            "workdir": str(tmp / ".kanma"),
        }
        r = cli.post("/api/projects", json=body)
        assert r.status_code == 200, (r.status_code, r.text)
        slug = r.json()["slug"]
        assert (store / f"{slug}.yaml").exists()
        print(f"  PASS POST /api/projects -> slug={slug}")

        # 3) 需求文档不存在 -> 422
        bad = dict(body, requirement_doc=str(tmp / "nope.md"))
        r = cli.post("/api/projects", json=bad)
        assert r.status_code == 422, r.status_code
        print("  PASS 需求文档缺失 -> 422")

        # 4) 时间窗非法（结束早于开始）-> 422
        bad2 = dict(body, end_time=(now - timedelta(days=2)).isoformat(timespec="seconds"))
        r = cli.post("/api/projects", json=bad2)
        assert r.status_code == 422, r.status_code
        print("  PASS 时间窗非法 -> 422")

        # 5) 列表
        r = cli.get("/api/projects")
        assert r.status_code == 200 and len(r.json()["projects"]) >= 1
        print("  PASS GET /api/projects -> 列表含项目")

        # 6) 触发评审 + 轮询
        r = cli.post(f"/api/projects/{slug}/run")
        assert r.status_code == 200, (r.status_code, r.text)
        job_id = r.json()["job_id"]
        print(f"  PASS POST run -> job_id={job_id}")
        for _ in range(120):
            r = cli.get(f"/api/jobs/{job_id}")
            st = r.json()
            if st["status"] in ("done", "error"):
                break
            time.sleep(1)
        print(f"  评审终态：{st['status']}")
        assert st["status"] in ("done", "error")

        # 7) 报告页
        r = cli.get(f"/report/{slug}/{job_id}")
        assert r.status_code == 200
        if st["status"] == "done":
            assert "<table" in r.text or "缺陷" in r.text, "报告内容缺失"
            print(f"  PASS GET /report -> 报告渲染（verdict={st.get('verdict')}）")
        else:
            print("  PASS GET /report -> 报告页（评审失败，见错误块）")
            print("    失败原因摘要：", st["error"].splitlines()[-1] if st["error"] else "")

        # 8) 删除：先跳过，到 15 步后再统一清理（否则 step 15 拿不到这个 slug）
        # r = cli.delete(f"/api/projects/{slug}")
        # assert r.status_code == 200 and not (store / f"{slug}.yaml").exists()
        # print("  PASS DELETE /api/projects/{slug}")

        # 9) 多源码工程项目：两个工程各自独立的 git 地址 / 分支 / 时间窗
        repo_b = build_repo(tmp, "pay-repo", SAMPLE_B, "pay.js")
        multi_body = {
            "name": "多工程评审",
            "repos": [
                dict(_repo_window(repo, now), name="order-service", branch="main"),
                dict(_repo_window(repo_b, now), name="pay-service", branch="main"),
            ],
            "requirement_doc": None,
            "workdir": str(tmp / ".kanma-multi"),
        }
        r = cli.post("/api/projects", json=multi_body)
        assert r.status_code == 200, (r.status_code, r.text)
        mslug = r.json()["slug"]
        print(f"  PASS POST /api/projects（多工程）-> slug={mslug}")

        # 列表须回显两个工程
        r = cli.get("/api/projects")
        item = next(p for p in r.json()["projects"] if p["slug"] == mslug)
        assert len(item["repos"]) == 2, item
        assert {x["name"] for x in item["repos"]} == {"order-service", "pay-service"}
        print("  PASS GET /api/projects -> 列表回显 2 个源码工程")

        # 10) 多工程评审：两份 diff 都要进报告
        r = cli.post(f"/api/projects/{mslug}/run")
        assert r.status_code == 200, (r.status_code, r.text)
        mjob = r.json()["job_id"]
        for _ in range(150):
            st = cli.get(f"/api/jobs/{mjob}").json()
            if st["status"] in ("done", "error"):
                break
            time.sleep(1)
        assert st["status"] == "done", st.get("error")
        html = cli.get(f"/report/{mslug}/{mjob}").text
        assert "svc.py" in html, "报告缺少 order-service 的源文件"
        assert "pay.js" in html, "报告缺少 pay-service 的源文件"
        assert "order-service" in html and "pay-service" in html, "报告未标注工程归属"
        print(f"  PASS 多工程评审 -> 两个工程变更均入报告（verdict={st.get('verdict')}）")

        # 11) 工程名重复 -> 422（否则克隆目录会互相覆盖）
        dup_body = {
            "name": "重复工程名",
            "repos": [
                dict(_repo_window(repo, now), name="same"),
                dict(_repo_window(repo_b, now), name="same"),
            ],
            "workdir": str(tmp / ".kanma-dup"),
        }
        r = cli.post("/api/projects", json=dup_body)
        assert r.status_code == 422, (r.status_code, r.text)
        print("  PASS 源码工程名重复 -> 422")

        # 12) 大模型设置：默认结构 + 模式切换 + api_key 保留/清空
        r = cli.get("/api/llm")
        assert r.status_code == 200
        body = r.json()
        assert "mode" in body and "local" in body and "cloud" in body
        assert "active" in body
        assert body["local"]["api_key"] == "" and body["cloud"]["api_key"] == ""
        print("  PASS GET /api/llm -> 默认结构（密钥脱敏）")

        # 切到本地模式 + 设模型 + 设密钥
        r = cli.put("/api/llm", json={
            "mode": "local",
            "enabled": True,
            "local": {
                "model": "qwen2.5-coder:7b",
                "base_url": "http://127.0.0.1:11434/v1",
                "api_key": "sk-local-test",
                "temperature": 0.0,
                "timeout": 60,
            },
        })
        assert r.status_code == 200, (r.status_code, r.text)
        body = r.json()
        assert body["mode"] == "local"
        assert body["active"]["ready"] is True
        assert body["active"]["model"] == "qwen2.5-coder:7b"
        assert body["active"]["label"].endswith("本地模式）")
        assert body["local"]["api_key"] == ""  # 回显脱敏
        assert "sk-l" in body["local"]["api_key_hint"]
        print("  PASS PUT /api/llm -> 切到本地模式 + 密钥落盘（回显脱敏）")

        # 空 api_key 表示保留：再 PUT 一次改 model 即可
        r = cli.put("/api/llm", json={"mode": "local", "local": {"model": "llama3:8b"}})
        assert r.status_code == 200
        body = r.json()
        assert body["active"]["model"] == "llama3:8b"
        assert body["local"]["api_key_set"] is True  # 上一轮的 sk-local-test 仍在
        print("  PASS api_key 空传 -> 保留已保存密钥")

        # __CLEAR__ 清空
        r = cli.put("/api/llm", json={"mode": "local", "local": {"api_key": "__CLEAR__"}})
        assert r.status_code == 200
        assert r.json()["local"]["api_key_set"] is False
        print("  PASS __CLEAR__ -> 清空密钥")

        # 非法 mode -> 422
        r = cli.put("/api/llm", json={"mode": "moon"})
        assert r.status_code == 422, r.text
        print("  PASS 非法 mode -> 422")

        # 关闭大模型（off）-> active.ready=False
        r = cli.put("/api/llm", json={"mode": "off"})
        assert r.status_code == 200, (r.status_code, r.text)
        assert r.json()["active"]["ready"] is False
        print("  PASS 关闭大模型 -> 离线模式生效")

        # 13) 测试连接：本地未启服务应 ok=False 且 error 含错误信息
        r = cli.put("/api/llm", json={
            "mode": "local",
            "local": {"model": "x", "base_url": "http://127.0.0.1:1/v1"},
        })
        r = cli.post("/api/llm/test", json={
            "mode": "local",
            "local": {"model": "x", "base_url": "http://127.0.0.1:1/v1", "timeout": 5},
        })
        assert r.status_code == 200
        d = r.json()
        assert d["ok"] is False, d
        assert d["mode"] == "local"
        print("  PASS POST /api/llm/test -> 不可用地址正确返回失败")

        # 14) 探测本地服务：至少返回 5 个候选（不管是否可用）
        r = cli.post("/api/llm/probe")
        assert r.status_code == 200
        results = r.json()["results"]
        assert len(results) >= 5
        for item in results:
            assert "name" in item and "base_url" in item
            assert "ok" in item
            assert isinstance(item["ok"], bool)
        print(f"  PASS POST /api/llm/probe -> 扫描 {len(results)} 个本地推理端点")

        # 15) 评审产物的 llm 字段已记录模型信息
        # 恢复一个可用配置（仍为离线语义；但 ctx.to_dict 至少带 mode/label）
        cli.put("/api/llm", json={
            "mode": "local",
            "enabled": True,
            "local": {"model": "qwen2.5-coder:7b", "base_url": "http://127.0.0.1:11434/v1"},
        })
        r = cli.post(f"/api/projects/{slug}/run")
        assert r.status_code == 200
        job_id = r.json()["job_id"]
        for _ in range(120):
            st = cli.get(f"/api/jobs/{job_id}").json()
            if st["status"] in ("done", "error"):
                break
            time.sleep(1)
        assert st["status"] in ("done", "error")
        llm = st.get("llm") or {}
        assert llm.get("mode") == "local", llm
        assert "qwen2.5-coder:7b" in llm.get("model", ""), llm
        print("  PASS 评审产物附带模型信息（mode=local, model=qwen2.5-coder:7b）")

        # 报告页要展示模型
        r = cli.get(f"/report/{slug}/{job_id}")
        assert r.status_code == 200
        if st["status"] == "done":
            assert "qwen2.5-coder:7b" in r.text, "报告页未显示模型名"
            assert "本地模式" in r.text, "报告页未显示模式"
            print("  PASS GET /report -> 顶部摘要展示模型")

        # 16) 编辑项目：改工程名与时间窗内的配置
        edited = {
            "name": "demo 评审",
            "repos": [dict(_repo_window(repo, now), name="edited-svc", branch="main")],
            "requirement_doc": None,
            "workdir": str(tmp / ".kanma"),
        }
        r = cli.put(f"/api/projects/{slug}", json=edited)
        assert r.status_code == 200, (r.status_code, r.text)
        assert r.json()["renamed"] is False
        got = cli.get(f"/api/projects/{slug}").json()
        assert got["repos"][0]["name"] == "edited-svc", got
        print("  PASS PUT /api/projects/{slug} -> 编辑生效（工程名已更新）")

        # 编辑不存在的 slug -> 404
        r = cli.put("/api/projects/no-such-project", json=edited)
        assert r.status_code == 404, r.status_code
        print("  PASS PUT 不存在的项目 -> 404")

        # 17) 编辑改名：slug 变更，评审历史随之迁移（不能丢）
        runs_before = cli.get(f"/api/projects/{slug}/runs").json()["runs"]
        assert runs_before, "前置条件：该项目应已有评审历史"
        r = cli.put(f"/api/projects/{slug}", json=dict(edited, name="demo 评审改名"))
        assert r.status_code == 200, (r.status_code, r.text)
        d = r.json()
        new_slug = d["slug"]
        assert d["renamed"] is True and new_slug != slug, d
        assert not (store / f"{slug}.yaml").exists(), "旧 slug 的配置文件应已删除"
        assert (store / f"{new_slug}.yaml").exists()
        runs_after = cli.get(f"/api/projects/{new_slug}/runs").json()["runs"]
        assert len(runs_after) == len(runs_before), (len(runs_after), len(runs_before))
        assert all(x["slug"] == new_slug for x in runs_after), "sidecar 的 slug 未纠正"
        print(f"  PASS 编辑改名 -> {slug} → {new_slug}，{len(runs_after)} 条历史已迁移")

        # 18) 改名撞上已有项目 -> 409
        r = cli.put(f"/api/projects/{new_slug}", json=dict(edited, name="多工程评审"))
        assert r.status_code == 409, (r.status_code, r.text)
        print("  PASS 编辑改名为已有项目名 -> 409")

        # 19) 同一项目已有评审在跑 -> 409（两个评审并发会互相踩工作区）
        from kanma.web.jobs import _JOBS, ReviewJob

        fake = ReviewJob(new_slug)
        fake.status = "running"
        _JOBS[fake.job_id] = fake
        try:
            r = cli.post(f"/api/projects/{new_slug}/run")
            assert r.status_code == 409, (r.status_code, r.text)
        finally:
            _JOBS.pop(fake.job_id, None)
        print("  PASS 已有评审进行中 -> 409")

        # 20) 历史列表补充字段；报告原文可下载
        runs = cli.get(f"/api/projects/{new_slug}/runs").json()["runs"]
        assert runs and "duration_seconds" in runs[0] and "has_markdown" in runs[0]
        md_runs = [x for x in runs if x["has_markdown"]]
        assert md_runs, "应至少有一条带报告原文的历史"
        jid = md_runs[0]["job_id"]
        r = cli.get(f"/api/runs/{jid}/markdown")
        assert r.status_code == 200, r.status_code
        assert len(r.text) > 200, "报告正文过短"
        assert "attachment" in r.headers.get("content-disposition", "")
        print(f"  PASS GET /api/runs/{jid}/markdown -> 报告原文可下载")

        r = cli.get("/api/runs/nonexistent000/markdown")
        assert r.status_code == 404, r.status_code
        print("  PASS 下载不存在的报告 -> 404")

        # 21) 报告页提供「再次评审」与「下载 Markdown」入口
        page = cli.get(f"/report/{new_slug}/{jid}").text
        assert "再次评审" in page, "报告页缺少再次评审入口"
        assert "/api/runs/" in page and "markdown" in page, "报告页缺少下载入口"
        print("  PASS 报告页 -> 含再次评审与下载入口")

        # 22) 全局评审报告列表（跨项目，按完成时间倒序）
        all_runs = cli.get("/api/runs").json()["runs"]
        assert all_runs, "全局报告列表不应为空"
        assert jid in [x["job_id"] for x in all_runs], "全局列表应包含已完成的评审"
        top = all_runs[0]
        assert "project_name" in top and "duration_seconds" in top
        assert "orphan" in top and top["orphan"] is False
        times = [x.get("finished_at") or x.get("started_at") or "" for x in all_runs]
        assert times == sorted(times, reverse=True), "报告列表未按时间倒序"
        print(f"  PASS GET /api/runs -> {len(all_runs)} 份报告，带项目名且按时间倒序")

        r = cli.get("/api/runs?limit=1")
        assert r.status_code == 200 and len(r.json()["runs"]) == 1, r.text
        print("  PASS GET /api/runs?limit=1 -> 条数受限")

        # 23) 首页结构：项目列表 + 报告列表 + 弹窗录入 + 可折叠模型设置
        home = cli.get("/").text
        for frag in ('id="projList"', 'id="repList"', 'id="mask"', 'id="form"', 'id="btnNew"',
                     "评审报告", 'id="llmToggle"', 'id="llmBody"'):
            assert frag in home, f"首页缺少 {frag}"
        # 项目以列表项渲染（可点击切换到报告视图）
        assert f'data-slug="{new_slug}"' in home, "项目列表未渲染已保存项目"
        print("  PASS 首页 -> 项目列表 / 报告列表 / 新增弹窗 / 折叠模型设置 齐备")

        # 24) 清理：删除改名后的 demo + 多工程
        r = cli.delete(f"/api/projects/{new_slug}")
        assert r.status_code == 200
        r = cli.delete(f"/api/projects/{mslug}")
        assert r.status_code == 200
        print("  PASS DELETE -> 清理 demo 与多工程")

        print("\nWeb 控制台端到端验证通过")
        return 0
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
