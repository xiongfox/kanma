# 勘码 · KanMa

企业级代码 Review Agent。以**项目维度**对一个或多个源码工程在某段时间内的**代码变更（diff）**做多维度审查：
安全、性能、正确性、命名、注释、规范，并**真实生成并运行测试**来验证变更是否符合需求文档。

一个项目可包含**多个源码工程**，每个工程独立指定 git 地址、分支与起止时间；勘码逐工程采集 diff、
逐工程生成与执行测试，缺陷标注所属工程后合并成一份报告。

核心人格定义在 [`AGENTS.MD`](./AGENTS.MD)——**硬规则与六步流程的唯一事实源**，改行为先改这个文件。

---

## 快速开始

```bash
# 1. 建虚拟环境并安装
python3.13 -m venv .venv && .venv/bin/pip install -e ".[dev]"

# 2. 环境自检（git / docker / LLM / 规则表）
kanma check

# 3. 选择模型模式（可选；不配则进入离线模式，跳过语义审查）

#   云端模式（OpenAI / 通义 / DeepSeek / 企业网关）
export KANMA_LLM_MODE=cloud
export KANMA_MODEL=gpt-4o-mini
export KANMA_API_KEY=sk-xxx
export KANMA_BASE_URL=https://your-gateway/v1   # 企业内网网关可选

#   或本地模式（Ollama / vLLM / LM Studio / Xinference，OpenAI 兼容协议）
export KANMA_LLM_MODE=local
export KANMA_LOCAL_MODEL=qwen2.5-coder:7b
export KANMA_LOCAL_BASE_URL=http://127.0.0.1:11434/v1   # Ollama 默认

# 4. 执行评审
kanma review -c config/project.example.yaml

# 5. 离线全链路冒烟（自建样例仓库跑通六步，无需 LLM）
python scripts/smoke.py
```

退出码：`0` 无 P0，`1` 存在 P0（可直接接 CI 门禁）。

---

## 六步流程

```
① 采集 diff ──→ ② 静态 review ──→ ③ 生成测试用例 ──→ ④ 生成临时测试代码
   git 时间窗       LLM 逐章语义精读    需求/行为/回归/复现      pytest/JUnit/go test
                                                                        │
                    ⑥ 合并生成报告 ←──────── ⑤ 沙箱运行测试 ←─────────┘
                      交叉印证 + 四选一结论        Docker/进程隔离
```

| 步骤 | 模块 | 产出 |
| --- | --- | --- |
| 1 采集 diff | `nodes/diff_collect.py` | `DiffBundle`（文件/行号/hunk） |
| 2 静态 review | `nodes/static_review.py` | `StaticFinding[]`（带 file:line 证据） |

步骤 2 在 `graph.py` 里是**每个规约章节一个并发节点**（`static_review_ch_*`，数量由
`rules/data/*.md` 决定）+ `static_review_merge` 汇总去重节点，并发子任务共享一条
`[步骤2.M]` 编号序列。按章节而非维度扇出：每章规约只注入一次（6 单元 / 12.9K 字符），
按维度扇出会重复注入 3.26 倍（13 单元 / 42K 字符）。

**步骤 2 并发度阈值（系统配置项）**：各章节节点会并发打给模型，本地模型（Ollama / vLLM 等）
可能顶不住 6 路同时请求。该阈值限制**同时打给模型的审查请求数**，**系统默认两路并发（2）**，
本地模型压不住可下调为 1，云端强模型设 `0` 放开到全量并发；所有章节仍会被审查到，只是串行成若干批。

- **系统设置（推荐）**：Web 控制台首页的「系统设置」卡片（可收起），填写「静态审查并发度」并点保存，
  全局对所有评审项目生效，落盘于 `{store_dir}/system.json`。这是该阈值的**首选配置入口**。
- **环境变量**：`KANMA_STATIC_REVIEW_MAX_PARALLEL=N`（无页面 / CI 场景；不设则取默认 2）。

**章节按序启动**：并发受限时，各章节的**启动顺序严格按规约章节 一→六**（由 `OrderedGate`
保证，与线程调度顺序无关）：限 2 路时按「一、二 → 三、四 → 五、六」成对推进，限 1 路退化为
严格的 1→6 串行；同章后续分片只受额度限制。并发度设 `0`（不限流）时六路同时起跑，不保证顺序。
- **CLI 旗标**：`kanma review --max-static-parallel N`（仅当次命令生效，优先级最高）。
- 优先级（高→低）：CLI 旗标 > 系统设置 > 环境变量 > 默认两路并发（2）。

**多工程按工程串行**：一次评审含多个源码工程时，步骤 2 在**工程维度上串行**——同一工程的
所有章节并发跑完，才放行下一个工程（章节节点循环前后各有一道工程级屏障）。这样「6 章 × N 工程」
的模型请求不会交叉叠加，本地模型压力被限制为「单工程并发度」；单工程评审时屏障自动退化为无同步，
并行度不受影响。注意这与上面的「并发度阈值」是正交的两层控制：阈值限制*同时打给模型的请求数*，
工程串行限制*哪些工程可以同时在飞*。
| 3 生成用例 | `nodes/testcase_gen.py` | `TestCase[]`（三类齐备） |
| 4 生成测试代码 | `nodes/testcode_gen.py` | `TestArtifact`（写到 `.kanma/tmp/`） |
| 5 沙箱运行 | `nodes/sandbox_run.py` | `TestResult[]`（真实执行结果） |
| 6 合并报告 | `nodes/report_merge.py` | `ReviewReport` → `docs/review-report-*.md` |

编排在 `graph.py`（LangGraph）：diff 为空或测试产物不可用时**短路到报告并写入降级说明**，
但绝不会静默跳过——按 HR-09，任何跳步都必须在报告第 7 章显式声明。

### 调用链：Web 全局 job 与 LangGraph 的关系
```
# Web 全局 job 路径
web/app.py:255  api_run_review(slug)
  └─> web/jobs.py:121  start_review(slug, cfg, store)   # 全局串行锁 + 返回 job 句柄
        └─> 起 daemon 线程 _run(job, cfg, store)        jobs.py:163
              └─> graph.run_review(...)                 jobs.py:233   # 与 CLI 同一函数
                    └─> build_graph(...).compile()      graph.py:256 → 242
                    └─> app.invoke({config: ...})       graph.py:257  ← 此处 LangGraph 执行 DAG

# CLI 路径（同一函数，无外层 web 调度）
cli.py:66  report, state = run_review(cfg, ctx=ctx, sandbox=sandbox, output_dir=...)  → 同上
```

职责切分（`jobs.py` 负责 LangGraph 之外的三件事，绝不编排节点）：

| 职责 | 是否 LangGraph | 位置 |
| --- | --- | --- |
| 全局串行锁（同一时刻仅 1 个 job，超了抛 `BusyError`） | 否 | `jobs.py:134` 持锁检查 + 注册 |
| daemon 线程（页面关闭/跳走不中断，跑到底落盘） | 否 | `jobs.py:139` |
| SSE 事件总线 + 进度/跳过原因追踪 + 磁盘 sidecar 重放 | 否 | `ReviewJob.emit` / `_write_sidecar` |
| 六个节点 + 步骤 2 按章并发扇出 + 条件路由 | **是** | `graph.py` 的 `StateGraph` / `add_conditional_edges` |
| `app.invoke` 实际执行 DAG | **是** | `graph.py:257` |

页面看到的实时轨迹来自 `on_step` 回调：`_wrap_node`（`graph.py:102`）在 LangGraph 每个节点
start/done/live 时触发 `on_step` → `on_step`（`jobs.py:171`）经 `job.emit` 推 SSE。**它是管道，不是替代。**

---

## 目录结构

```
KanMa/
├── AGENTS.MD                # 人格定义：硬规则 HR-01~HR-11 + 六步流程（唯一事实源）
├── config/project.example.yaml   # 评审项目配置（多源码工程：git/分支/时间窗 + 需求文档）
├── docs/requirement.example.md   # 需求开发文档示例
├── scripts/smoke.py         # 离线全链路冒烟
├── src/kanma/
│   ├── models/schemas.py    # Pydantic 契约（全链路数据结构）
│   ├── state.py             # LangGraph 状态
│   ├── graph.py             # 六步流水线编排
│   ├── llm.py               # 模型接入 + 离线降级判定
│   ├── config.py            # 项目配置加载
│   ├── rules/specs.py       # 规约加载器（rules/data/*.md 按章注入；适用语言见 spec.yaml）
│   ├── rules/scan.py        # diff 新增行迭代 + 缺陷排序
│   ├── nodes/               # 六个步骤节点
│   ├── tools/git_tool.py    # 克隆 / 时间窗筛 commit / diff 解析
│   ├── tools/sandbox.py     # Docker 优先，退化为进程隔离
│   ├── report/renderer.py   # 固定 8 章节 Markdown 报告
│   └── cli.py               # kanma review / check / rules / web
│   └── web/                 # FastAPI 控制台：页面 + JSON 接口 + 后台评审任务
└── tests/                   # 单元测试（规则/合并/报告/离线全链路）
```

---

## 关键设计约束（摘自 AGENTS.MD）

- **只评 diff**：范围外的既有问题进「顺带发现」，不计入结论（HR-01）。
- **证据可溯源**：每个缺陷必带 `文件:行号` + 代码片段，无证据不报（HR-02）。
- **不碰源码树**：测试代码只写 `.kanma/tmp/`，勘码只出报告不改代码（HR-03）。
- **安全零容忍**：注入、硬编码密钥、敏感日志、不安全反序列化一律 P0（HR-05）。
- **测试必须真跑**：跑不了就标 `NOT_RUN`，不伪造通过，且不计入通过率（HR-06）。
- **沙箱隔离**：Docker 无网络 + 只读挂载 + 限额；退化到子进程也带超时强杀（HR-07）。
- **结论四选一**：拒绝合入 / 修改后合入 / 建议修改后合入 / 通过（HR-10）。

---

## 离线模式

未配置 `KANMA_MODEL` 时自动进入离线模式：

- 步骤 2 跳过语义审查，静态审查无结论（并在报告中显式降级）
- 步骤 3 降级为基于 diff 的推断回归用例，标题标注「[推断]」
- 步骤 4 生成 pytest 模板骨架，断言未实现 → 执行结果为 `SKIPPED`，记为 `NOT_RUN`
- 报告中写明降级原因，结论不会因「没测」而判为通过

任何降级都会出现在报告的**第 7 章 流程降级说明**。

---

## Web 控制台（项目录入页）

提供图形化页面，供使用者创建评审项目。一个项目可录入**多个源码工程**，每个工程独立填写
**git 地址 / 分支 / 对比基线 / 起止时间**，并指定**需求开发文档本地路径**，一键发起评审。
保存的配置为 YAML，与 CLI 完全互通（可直接 `kanma review -c projects/<slug>.yaml`）。

页面创建表单可动态增删源码工程；工程名缺省时从 git 地址推导，且不可重复（重复会被拒绝，
因为克隆目录按工程名隔离，重名会互相覆盖）。

页面顶部提供**大模型设置卡片**（见下节），可在「本地模式 / 云端模式」之间手工切换，
参数保存在 `projects/llm.json`，新发起的评审立即生效。

### 一键启动前后台

`scripts/serve.sh` 是 Web 控制台的封装脚本，覆盖前台/后台两种启动方式与服务生命周期管理：

```bash
scripts/serve.sh                  # 前台启动（Ctrl+C 停止）
scripts/serve.sh start -d         # 后台启动（写 pid 与日志）
scripts/serve.sh start -d --open  # 后台启动 + 自动打开浏览器
scripts/serve.sh stop             # 停止后台实例
scripts/serve.sh restart          # 重启（默认后台，保留 --port/--host/--store 覆盖）
scripts/serve.sh status           # 查看后台实例
scripts/serve.sh logs             # 实时跟踪日志（Ctrl+C 退出）
scripts/serve.sh logs -n 200      # 看最近 200 行
scripts/serve.sh check            # 环境自检（git / docker / 模型）
```

可选参数：`--port 端口`、`--host 地址`、`--store 目录`、`--log-dir 目录`；环境变量
`KANMA_WEB_PORT` / `KANMA_WEB_HOST` / `KANMA_LOG_DIR` / `KANMA_PY` 可作为默认值。
后台实例的 pid 与日志位于 `.kanma-logs/` 下。

### 手动启动（不带封装脚本）

```bash
# 启动页面（默认 127.0.0.1:8765，项目配置存于 ./projects）
kanma web

# 自定义监听地址与项目目录
kanma web --host 0.0.0.0 --port 8080 --store /data/kanma-projects
```

页面功能：

- **创建项目**：项目名称、仓库地址（SSH/HTTPS）、目标分支、对比基线（可选）、变更起始/结束时间、需求开发文档的**服务器本地路径**（可选，留空走需求缺失降级）、工作区目录（可选）。
- **必填校验**：项目名 / 仓库 / 分支 / 时间窗为必填；结束时间不得早于开始时间（422 返回具体原因）。
- **需求文档校验**：填写的本地路径必须存在，否则 422 明确提示「需求文档路径不存在」。
- **项目列表**：展示已保存项目，支持「立即评审」「编辑」「删除」；新增与编辑均走弹窗表单。
- **立即评审 / 再次评审**：点后进入评审页，**过程流式展示** —— 六步流水线逐个点亮（含每步耗时），
  执行日志实时滚动，报告正文按块推送（不再是跑完才一次性出现）。
- **评审流接口**：`GET /api/jobs/{job_id}/stream`（Server-Sent Events），事件序列
  `snapshot`（全量快照）→ 若干 `step` / `report` → `done` / `error`。
  服务重启后内存任务丢失也无妨：重连时按磁盘 sidecar 重放，历史报告照样完整打开。
- **状态徽标**：列表实时轮询最近一次评审结果（评审中 / 完成 / 失败）。

接口一览（`/api/...`）：`GET /`、`POST /api/projects`、`GET /api/projects`、`GET /api/projects/{slug}`、
`DELETE /api/projects/{slug}`、`POST /api/projects/{slug}/run`、`GET /api/jobs/{job_id}`、
`GET /api/projects/{slug}/runs`、`GET /api/runs`、`GET /api/runs/{job_id}/markdown`、
`GET /api/jobs/{job_id}/stream`（SSE 评审流）、`GET /api/llm`、`PUT /api/llm`、`POST /api/llm/test`、
`POST /api/llm/probe`、`GET /report/{slug}/{job_id}`。

> 需求开发文档填写的是**勘码服务端可访问的绝对路径**（如 `/data/docs/需求.md`），而非浏览器上传；后台按路径读取正文用于步骤 3 用例生成与需求符合性核对。

---

## 大模型设置：本地模式 / 云端模式

勘码只走 **OpenAI 兼容协议** 一种通道，两种模式只是连的地址不同：

- **本地模式（local）**：连内网/本机的自部署推理服务（Ollama、vLLM、LM Studio、Xinference、LocalAI …），
  通常不需要 API Key，代码与 diff 不出内网。
- **云端模式（cloud）**：连云端或企业网关的模型 API（OpenAI、DeepSeek、通义、自建网关 …），
  需要 API Key。

两种模式各自独立保存（互不污染），切换不重启即生效；新发起的评审与已有项目立刻按新模式解析。

### 页面切换（推荐）

Web 页面顶部「大模型设置」卡片：

- **本地模式 / 云端模式** 分段按钮，一键切换；右下角实时显示「当前生效」状态。
- 每种模式独立填写 **服务地址 / 模型名 / API Key / 温度 / 超时**；密钥回显脱敏（仅显示 `sk-12****abcd`）。
- 三个动作：**保存并切换**（落盘到 `projects/llm.json`，权限 600）、**测试连接**（真实发一条 `ping`）、
  **探测本地服务**（并发扫描 Ollama/LM Studio/vLLM/Xinference/LocalAI 5 个常见端口，一键填入）。
- 「启用大模型」复选框 = 关闭后跳过语义审查（离线模式）。

### CLI 切换

```bash
# 查看当前设置
kanma llm show

# 探测本机推理服务
kanma llm probe

# 切到本地模式并落盘（保存到 projects/llm.json）
kanma llm set --mode local \
  --model qwen2.5-coder:7b \
  --base-url http://127.0.0.1:11434/v1

# 切到云端模式
kanma llm set --mode cloud \
  --model gpt-4o \
  --base-url https://api.openai.com/v1 \
  --api-key sk-xxx

# 清空已保存的 API Key
kanma llm set --mode cloud --clear-key

# 关闭大模型（跳过语义审查，离线模式）
kanma llm set --mode off

# 单次评审覆盖（不落盘）
kanma review -c config/project.example.yaml \
  --llm-mode local --llm-model llama3:8b

# 连通性自检（不修改任何设置）
kanma llm test
kanma llm test --mode local --model qwen2.5-coder:7b --base-url http://127.0.0.1:11434/v1
```

### 环境变量预置（CI / 容器场景）

| 变量 | 含义 |
| --- | --- |
| `KANMA_LLM_MODE` | `local` / `cloud` / `off` |
| `KANMA_MODEL` / `KANMA_API_KEY` / `KANMA_BASE_URL` | 云端模式（OpenAI 兼容网关） |
| `KANMA_LOCAL_MODEL` / `KANMA_LOCAL_API_KEY` / `KANMA_LOCAL_BASE_URL` | 本地模式（默认 `qwen2.5-coder:7b` / `http://127.0.0.1:11434/v1`） |

环境变量只在对应字段为空时作为默认值；保存页面后以落盘配置为准。

### 在报告与评审历史中体现

报告概览新增「模型」行（`{model}（本地模式） · {base_url}`），同时记录到评审历史的 JSON sidecar，
以便事后追溯每次评审用的是哪个模型与模式。

---

## 常见问题

**Q：未配 LLM 时能跑评审吗？**
能，会进入**离线模式**：步骤 2 跳过语义审查（静态审查无结论）；步骤 3 降级为「基于 diff 推断的回归用例」；
步骤 4 生成 pytest 模板骨架，断言未实现 → 执行记 `NOT_RUN`，**不计入通过率**。报告第 7 章会写明降级。
结论可信度受限，但不会因为"没测"就判"通过"。

**Q：本地模式要装什么？**
任意一个能提供 OpenAI 兼容协议 `/v1` 端点的自部署服务即可：

| 服务 | 端口 | 安装参考 |
| --- | --- | --- |
| Ollama | 11434 | `brew install ollama && ollama pull qwen2.5-coder:7b` |
| LM Studio | 1234 | 官方 GUI 启动后开启 Local Server |
| vLLM | 8000 | `vllm serve Qwen/Qwen2.5-Coder-7B-Instruct --port 8000` |
| Xinference | 9997 | `pip install xinference && xinfer launch` |
| LocalAI | 8080 | 官方 docker 一行启动 |

页面「探测本地服务」会扫描这 5 个端口，找到后一键填入。

**Q：报告页 5 秒自动刷新可以关掉吗？**
评审完成后停止轮询；如果想强制刷新，按 F5 即可。

**Q：怎么清理某个项目的所有评审历史？**
删项目时一并清理：`DELETE /api/projects/{slug}`，或在页面项目列表点「删除」。

**Q：服务怎么后台常驻？**
```bash
bash scripts/serve.sh start -d
# 状态：   bash scripts/serve.sh status
# 实时日志：bash scripts/serve.sh logs
# 停止：   bash scripts/serve.sh stop
```
pid 与日志在 `.kanma-logs/`，与项目配置 `projects/` 同级。

**Q：API Key 落盘安全吗？**
落盘文件 `projects/llm.json` 权限 600（仅当前用户可读）；API 返回时密钥脱敏（`sk-12****abcd`）。
若需更严格，可在生产环境用环境变量 `KANMA_API_KEY` + 关闭页面录入。

