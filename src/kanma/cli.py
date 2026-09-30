"""勘码命令行入口。

    kanma review -c config/project.example.yaml
    kanma check          # 只做环境自检与硬规则加载校验
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from kanma.config import load_project_config
from kanma.llm import get_llm
from kanma.llm_settings import LLMMode, get_settings
from kanma.rules import extract_rules, spec_applies_to, spec_chapters
from kanma.tools import git_available
from kanma.tools.sandbox import Sandbox


def _apply_llm_overrides(args: argparse.Namespace) -> None:
    """命令行覆盖模型设置：--llm-mode / --llm-model / --llm-base-url。"""
    s = get_settings()
    if getattr(args, "llm_mode", None):
        mode = args.llm_mode
        if mode == "off":
            s = s.model_copy(update={"enabled": False})
        else:
            s = s.model_copy(update={"mode": LLMMode(mode), "enabled": True})
    prof_key = "local" if s.mode is LLMMode.LOCAL else "cloud"
    prof = s.profile_of(s.mode)
    changed = False
    if getattr(args, "llm_model", None):
        prof = prof.model_copy(update={"model": args.llm_model})
        changed = True
    if getattr(args, "llm_base_url", None):
        prof = prof.model_copy(update={"base_url": args.llm_base_url})
        changed = True
    if changed:
        s = s.model_copy(update={prof_key: prof})
    from kanma.llm_settings import configure

    configure(s)


def _cmd_review(args: argparse.Namespace) -> int:
    from kanma.graph import run_review

    cfg = load_project_config(args.config)
    if args.workdir:
        cfg.workdir = Path(args.workdir).expanduser()
    _apply_llm_overrides(args)
    ctx = get_llm(args.model)
    sandbox = Sandbox(backend=args.sandbox, timeout=args.timeout)
    print(f"勘码 · 评审项目：{cfg.name}")
    for i, repo in enumerate(cfg.repos, start=1):
        print(f"  工程{i}：{repo.display_name}  仓库：{repo.git_url}  分支：{repo.branch}")
        print(
            f"    时间窗：{repo.start_time:%Y-%m-%d %H:%M} ~ {repo.end_time:%Y-%m-%d %H:%M}"
        )
    print(f"  需求文档：{cfg.requirement.path or '（缺失）'}")
    print(f"  LLM：{ctx.label}")
    print(f"  沙箱：{sandbox.resolved_backend}")
    print(
        f"  步骤2并发度："
        f"{args.max_static_parallel if args.max_static_parallel else '不限制'}"
        f"（系统设置 / 环境变量 KANMA_STATIC_REVIEW_MAX_PARALLEL 亦可设置）"
    )
    print()

    # 系统设置（Web 控制台）里若有配置，作为默认值带进来；CLI 显式 --max-static-parallel 仍优先。
    from kanma.system_settings import load_settings as load_sys_settings

    sys_cfg = load_sys_settings(Path("projects"))
    report, state = run_review(
        cfg, ctx=ctx, sandbox=sandbox, output_dir=args.output,
        max_static_parallel=args.max_static_parallel,
        system_max_parallel=sys_cfg.static_review_max_parallel,
    )

    print("── 流程轨迹 ──")
    for line in state.get("steps_log", []):
        print(f"  {line}")
    print()
    print(f"结论：{report.verdict.value} —— {report.verdict_reason}")
    print(f"缺陷：P0×{report.p0_count}  P1×{report.p1_count}  合计 {len(report.findings)}")
    print(f"报告：{args.output}/review-report-{cfg.name}-*.md")
    return 0 if report.p0_count == 0 else 1


def _cmd_check(_: argparse.Namespace) -> int:
    from kanma import PERSONA_FILE, persona_path

    print("勘码 · 环境自检")
    persona = persona_path()
    print(
        f"  人格文件：{persona if persona else f'未找到（期望 {PERSONA_FILE}）'}"
    )
    print(f"  git：{'可用' if git_available() else '不可用（步骤1 将失败）'}")
    docker = Sandbox.docker_available()
    print(f"  docker：{'可用（沙箱默认走容器）' if docker else '不可用（沙箱退化为本地进程隔离）'}")
    llm = get_llm()
    print(f"  LLM：{llm.label}")
    chapters = spec_chapters()
    langs = spec_applies_to()
    scope = "、".join(langs) if langs else "不限语言"
    print(f"  规约章节：已加载 {len(chapters)} 章（适用语言：{scope}）")
    return 0


def _cmd_rules(_: argparse.Namespace) -> int:
    """列出规约章节。

    勘码不做正则机检（无规则表），缺陷全部由 LLM 按这些章节逐章语义审查得出。
    """
    chapters = spec_chapters()
    langs = spec_applies_to()
    scope = "、".join(langs) if langs else "不限语言"
    print(f"勘码 · 规约章节（{len(chapters)} 章，适用语言：{scope}）")
    print("说明：勘码无正则机检规则，缺陷由 LLM 按下列章节逐章语义审查得出。\n")
    print(f"{'章节':<28}{'强制条文':<10}")
    print("-" * 46)
    for key, title in chapters:
        n = len(extract_rules(key))
        print(f"{title:<28}{n:<10}")
    return 0


def _cmd_web(args: argparse.Namespace) -> int:
    import uvicorn

    from kanma.llm_settings import load_settings
    from kanma.web.app import app

    store = Path(args.store).expanduser()
    store.mkdir(parents=True, exist_ok=True)
    load_settings(store)
    app.state.store_dir = store

    url = f"http://{args.host}:{args.port}"
    print(f"勘码 · Web 控制台已启动")
    print(f"  地址：{url}")
    print(f"  项目目录：{store}")
    s = get_settings()
    print(f"  当前模型：{s.mode_label}（{s.active.model or '未配置'}）")
    print("  按 Ctrl+C 停止")
    uvicorn.run(app, host=args.host, port=args.port, log_level="info")
    return 0


# ---------------------------------------------------------------------------
# kanma llm：本地 / 云端 模式管理（CLI 等价页面）
# ---------------------------------------------------------------------------


def _cmd_llm_show(_: argparse.Namespace) -> int:
    s = get_settings()
    info = s.to_api()
    print(f"勘码 · 大模型设置")
    print(f"  模式：{s.mode_label}（enabled={s.enabled}）")
    for key, label in (("local", "本地模式"), ("cloud", "云端模式")):
        p = info[key]
        print(
            f"  [{label}] model={p['model'] or '（未设置）'}"
            f"  base_url={p['base_url'] or '（未设置）'}"
            f"  api_key={p['api_key_hint'] or '（未设置）'}"
            f"  temperature={p['temperature']}  timeout={p['timeout']}s"
        )
    a = info["active"]
    print(f"  当前生效：{a['label']}  ready={a['ready']}")
    return 0


def _cmd_llm_test(args: argparse.Namespace) -> int:
    from kanma.llm import ping_llm

    s = get_settings()
    if args.mode or args.model or args.base_url:
        prof: dict = {"model": args.model or "", "base_url": args.base_url or ""}
        s = s.with_incoming({"mode": args.mode, args.mode: prof} if args.mode else {args.mode or "local": prof})
    r = ping_llm(settings=s, model=args.model)
    if r["ok"]:
        reply = r.get("reply", "")[:80]
        print(f"OK · {r['model']}（{r['mode_label']}）· {r['latency_ms']}ms · 回复：{reply}")
        return 0
    print(f"FAIL · {r.get('error', '未知错误')}")
    return 1


def _cmd_llm_probe(_: argparse.Namespace) -> int:
    from kanma.llm_settings import probe_local_services

    print("勘码 · 探测本机推理服务")
    for r in probe_local_services():
        if r["ok"]:
            models = ", ".join(r.get("models", [])[:3]) or "未列出"
            print(f"  ✓ {r['name']:<12} {r['base_url']}  模型: {models}")
        else:
            print(f"  ✗ {r['name']:<12} {r['base_url']}  ({r.get('error', '无响应')})")
    return 0


def _cmd_llm_set(args: argparse.Namespace) -> int:
    """CLI 切模式：kanma llm set --mode local --model qwen2.5-coder:7b --base-url ... [--api-key ...]"""
    s = get_settings()
    mode = args.mode
    if mode == "off":
        s = s.model_copy(update={"enabled": False})
    else:
        updates: dict = {"mode": LLMMode(mode), "enabled": True}
        prof_key = "local" if mode == "local" else "cloud"
        prof = s.profile_of(LLMMode(mode))
        if args.model:
            prof = prof.model_copy(update={"model": args.model})
        if args.base_url:
            prof = prof.model_copy(update={"base_url": args.base_url})
        if args.api_key:
            prof = prof.model_copy(update={"api_key": args.api_key})
        elif args.clear_key:
            prof = prof.model_copy(update={"api_key": ""})
        updates[prof_key] = prof
        s = s.model_copy(update=updates)
    from kanma.llm_settings import save_settings

    path = save_settings(s, args.store)
    print(f"已保存：{path}")
    _cmd_llm_show(argparse.Namespace())
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="kanma", description="勘码 · 企业级代码 Review Agent")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_review = sub.add_parser("review", help="执行完整评审")
    p_review.add_argument("-c", "--config", required=True, help="项目配置 YAML")
    p_review.add_argument("--workdir", default=None, help="覆盖工作区目录")
    p_review.add_argument("--model", default=None, help="覆盖模型名（仅当前命令）")
    p_review.add_argument(
        "--llm-mode", choices=["local", "cloud", "off"],
        help="切换模型模式：local=本地推理，cloud=云端API，off=离线（跳过语义审查）",
    )
    p_review.add_argument("--llm-model", default=None, help="覆盖当前模式下的模型名")
    p_review.add_argument("--llm-base-url", default=None, help="覆盖当前模式下的服务地址")
    p_review.add_argument("--sandbox", default="auto", choices=["auto", "docker", "local"])
    p_review.add_argument("--timeout", type=int, default=300, help="单条命令超时秒数")
    p_review.add_argument("-o", "--output", default="docs", help="报告输出目录")
    p_review.add_argument(
        "--max-static-parallel", type=int, default=None,
        help="步骤2并发度阈值：同时打给模型的审查请求数（0/缺省=不限制；本地模型压不住设 2~3）",
    )
    p_review.set_defaults(func=_cmd_review)

    p_check = sub.add_parser("check", help="环境自检")
    p_check.set_defaults(func=_cmd_check)

    p_rules = sub.add_parser("rules", help="列出规则型机检规则表")
    p_rules.set_defaults(func=_cmd_rules)

    p_web = sub.add_parser("web", help="启动评审项目管理页面")
    p_web.add_argument("--host", default="127.0.0.1", help="监听地址")
    p_web.add_argument("--port", type=int, default=8765, help="监听端口")
    p_web.add_argument("--store", default="projects", help="项目配置存放目录")
    p_web.set_defaults(func=_cmd_web)

    p_llm = sub.add_parser("llm", help="管理大模型设置（本地/云端模式切换）")
    llm_sub = p_llm.add_subparsers(dest="llm_cmd", required=True)
    p_llm_show = llm_sub.add_parser("show", help="查看当前模型设置")
    p_llm_show.set_defaults(func=_cmd_llm_show)

    p_llm_test = llm_sub.add_parser("test", help="连通性自检（不修改任何设置）")
    p_llm_test.add_argument("--mode", choices=["local", "cloud"], help="临时按指定模式测")
    p_llm_test.add_argument("--model", help="临时指定模型名")
    p_llm_test.add_argument("--base-url", help="临时指定服务地址")
    p_llm_test.set_defaults(func=_cmd_llm_test)

    p_llm_probe = llm_sub.add_parser("probe", help="探测本机推理服务（Ollama / LM Studio / vLLM / Xinference / LocalAI）")
    p_llm_probe.set_defaults(func=_cmd_llm_probe)

    p_llm_set = llm_sub.add_parser("set", help="切换并保存模型设置")
    p_llm_set.add_argument("--mode", choices=["local", "cloud", "off"], required=True)
    p_llm_set.add_argument("--model", help="当前模式下的模型名")
    p_llm_set.add_argument("--base-url", help="当前模式下的服务地址")
    p_llm_set.add_argument("--api-key", help="当前模式下的 API Key")
    p_llm_set.add_argument("--clear-key", action="store_true", help="清空已保存的 API Key")
    p_llm_set.add_argument("--store", type=Path, default=Path("projects"),
                            help="落盘目录（与 kanma web 的 --store 一致）")
    p_llm_set.set_defaults(func=_cmd_llm_set)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
