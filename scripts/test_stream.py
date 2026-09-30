#!/usr/bin/env python3
"""评审流（SSE）端到端验证：确认点击进入评审页后页面能拿到逐帧推进的数据。

覆盖三点：
1. `snapshot` 先给全量快照，晚到的客户端不丢进度；
2. 过程中持续收到 `step`（六步流水线）+ `report`（报告正文分块 HTML）；
3. 结束后收到 `done`；服务重启（内存任务丢失）后仍能重放出完整报告。
"""

from __future__ import annotations

import json
import shutil
import sys
import tempfile
import time
from datetime import datetime
from pathlib import Path

from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_web import build_repo, _repo_window  # noqa: E402

from kanma.web.app import app  # noqa: E402
from kanma.web.jobs import list_jobs  # noqa: E402


def parse_sse(resp) -> list[dict]:
    """把 SSE 文本流解析成 [{event, data}]。"""
    events: list[dict] = []
    event = None
    for raw in resp.iter_lines():
        if raw is None:
            continue
        line = raw.decode("utf-8") if isinstance(raw, bytes) else raw
        if line.startswith("event: "):
            event = line[7:].strip()
        elif line.startswith("data: "):
            data = json.loads(line[6:])
            events.append({"event": event, "data": data})
        # 注释帧（保活）忽略
    return events


def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="kanma-stream-"))
    try:
        store = tmp / "projects"
        app.state.store_dir = store
        repo = build_repo(tmp)
        now = datetime.now()
        cli = TestClient(app)

        body = {
            "name": "流式评审验证",
            "repos": [dict(_repo_window(repo, now), name="stream-repo")],
            "workdir": str(tmp / ".kanma-stream"),
        }
        r = cli.post("/api/projects", json=body)
        assert r.status_code == 200, (r.status_code, r.text)
        slug = r.json()["slug"]
        print(f"  PASS 项目已创建：{slug}")

        r = cli.post(f"/api/projects/{slug}/run")
        assert r.status_code == 200, (r.status_code, r.text)
        job_id = r.json()["job_id"]
        print(f"  PASS 评审已启动：job_id={job_id}")

        # --- 1) 实时流 ---
        with cli.stream("GET", f"/api/jobs/{job_id}/stream") as resp:
            events = parse_sse(resp)

        kinds = [e["event"] for e in events]
        assert kinds, "SSE 未收到任何事件"
        assert kinds[0] == "snapshot", f"首个事件应为 snapshot，实际 {kinds[0]}"
        assert "step" in kinds, f"缺少 step 事件：{kinds}"
        assert "report" in kinds, f"缺少 report 事件：{kinds}"
        assert kinds[-1] == "done", f"末个事件应为 done，实际 {kinds[-1]}"
        print(f"  PASS SSE 事件序列：{kinds[:3]} … {kinds[-2:]}（共 {len(kinds)} 帧）")

        steps = [e["data"] for e in events if e["event"] == "step"]
        nodes = [s["node"] for s in steps if s["phase"] == "done"]
        assert nodes[0] == "diff_collect", f"首个完成节点应为 diff_collect，实际 {nodes[0]}"
        assert nodes[-1] == "report_merge", f"最后一个节点应为 report_merge，实际 {nodes[-1]}"
        started = [s["node"] for s in steps if s["phase"] == "start"]
        assert len(started) >= 2, "应至少记录两个节点的开始"
        print(f"  PASS 流水线轨迹：{' → '.join(nodes)}")

        chunks = [e["data"] for e in events if e["event"] == "report"]
        idxs = [c["index"] for c in chunks]
        assert idxs == list(range(len(chunks))), f"report 分块序号不连续：{idxs[:10]}"
        html_all = "".join(c["html"] for c in chunks)
        assert len(html_all) > 500, f"报告正文过短：{len(html_all)}"
        for kw in ("<h1", "<table"):
            assert kw in html_all, f"报告 HTML 缺少 {kw}（分块可能切断了结构）"
        print(f"  PASS 报告正文分块推送：{len(chunks)} 块 / {len(html_all)} 字符，HTML 结构完整")

        done = [e["data"] for e in events if e["event"] == "done"][-1]
        assert done["status"] == "done" and "verdict" in done
        assert set(done["summary"]) >= {"p0", "p1", "total", "tests"}
        print(f"  PASS done 事件带结论：{done['verdict']} / summary={done['summary']}")

        # --- 2) 服务重启后重放（内存任务已丢失） ---
        list_jobs(slug)  # 确认任务在内存中
        from kanma.web import jobs as jobsmod

        jobsmod._JOBS.clear()
        with cli.stream("GET", f"/api/jobs/{job_id}/stream") as resp:
            events2 = parse_sse(resp)
        kinds2 = [e["event"] for e in events2]
        assert kinds2[0] == "snapshot" and kinds2[-1] == "done", kinds2
        snap = events2[0]["data"]
        assert snap["status"] == "done" and len(snap["report_html"]) > 500, "重放快照缺报告全文"
        assert len(snap["steps"]) > 0, "重放快照缺步骤日志"
        print(f"  PASS 服务重启后重放：snapshot 含报告全文 {len(snap['report_html'])} 字符 + "
              f"{len(snap['steps'])} 行日志，并立刻给出 done")

        print("\n评审流（SSE）端到端验证通过")
        return 0
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
