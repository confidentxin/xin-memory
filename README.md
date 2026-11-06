# 记忆内核

> 给 AI 加一个记性、一个闹钟，和一双手。

一个**本地自托管**的外挂内核，给 DeepSeek Harness（DSH）提供三件事：

- **记得住你**：把对话压成事实，跨会话保留
- **到点会响**：cron / 一次性提醒，睡过头会补跑
- **能自己干活**：到点直接执行，做完再汇报

代码在你的机器上，数据在你的 Postgres 里，不经过任何第三方服务器。

## 它解决什么问题

| 痛点 | 具体表现 |
|---|---|
| 记不住 | 每开一个新对话，第一件事是重新自我介绍 |
| 想不起 | 聊到第十轮，第三轮说过的已经忘了 |
| 叫不动 | 到点该做的事，还是得你亲自开口 |

## 它提供什么

**1. 三层记忆**

```
L0  原始对话，只在当前会话内，按 token 预算截断
L1  原子事实（用户住在武汉 / 用户叫自信鑫），跨会话
L2  常驻画像（身份 / 地点 / 工作 / 偏好 / 备注），永远带着
```

写的时候压缩：对话 → 事实 → 画像。
读的时候按预算拼：先画像，再挑相关事实，最后放近期对话。

**2. 主动提醒**

意图（要做什么）和触发器（什么时候）分两张表存。
心跳每 30 秒查一次，到点写一条消息进会话。
cron 按**本地时间**匹配，错过那一分钟会自动补一次。

**3. 无人值守执行**

`mode=act` 的意图，到点后提示词变成「直接做完，不要等确认」。
模型带着记忆上下文（你是谁、之前聊过什么）自己动手，做完汇报。

有一条急停开关：建一个 `~/.dsh/ACT_HALT` 文件，所有自动执行立刻降级为提醒。

**4. 会话纪要**

三天没动静的会话，自动压成三五条要点，存成可跨会话召回的事实——
否则那些对话只会躺在库里，再也不会被读到。

## 它不是什么

- **不是通用 agent 框架**。它只服务 DSH，插件和这个仓库是绑定的。
- **不会自己规划项目**。任务是你（或上一次运行）写下的那句话，它不会分解目标。
- **没有云端**。全部本机，断网也能跑（除了调模型那一步）。
- **不保证做得对**。它说做完了就是做完了，没有自动验收。

## 从这里开始

| 我想… | 看哪 |
|---|---|
| 快速理解三层记忆怎么运作 | 下面的「架构」 |
| 部署、排错、急停、建定时任务 | [AUTONOMY.md](AUTONOMY.md) |
| 为什么这样设计、有哪些取舍 | [ARCHITECTURE.md](ARCHITECTURE.md) |

## 实现要点

自托管的三层记忆：原始对话留在 **L0**，原子事实留在 **L1**，常驻短画像留在 **L2**。热路径同步写入，冷路径再从新对话里抽事实、处理冲突，并按 token 预算把上下文装进提示词。

一期只用 Postgres 16 + pgvector。没有 `OPENAI_API_KEY` 时，抽取走内置规则，向量列留空，检索用关键词（中文按字二元组，英文按词，并配合 `tsvector` / `pg_trgm`）。不需要付费 API 也能把 demo 和测试跑通。

## 架构

```
append_episode ──► L0 episodes（只追加）
remember ────────► L1 facts（origin=explicit）+ 刷新 L2
                         │
冷路径 worker ◄── 未抽取的 user episode
     │  mock 规则，或 OpenAI JSON
     ▼
  与有效 L1 比对
     ├─ 对象相同 → 跳过
     ├─ 单值谓词（姓名、居住地、雇主、职业…）对象不同
     │     → 旧行 valid_to + superseded_by，再 ADD
     └─ 多值谓词（喜好、过敏、备注）→ 再 ADD
                 │
                 ▼
           重写 L2 profile_blocks

对话里的「提醒我 / 帮我盯着 / 周五前…」
     └─► intents + triggers（once_at）
tick ──► 到期的 once_at / due_at
     ├─ 普通待办：组装 L2 + 相关 L1 + intent，写一条 assistant 提醒回 L0
     └─ 转账、付款、删除、发邮件等：只记录 proposed_action，不执行

recall
  1. 按 user / agent / as_of 过滤
  2. 关键词候选；若已有向量，再取一路 pgvector
  3. 相关度 ×（0.55 + 0.45×importance）×（0.65 + 0.35×时间衰减）
     两路都命中时，把 RRF（k=60）以 25% 混进相关度
  4. 预算：L2 ≤ 800，L1 ≤ 1200，没用完的额度顺延，最后给本会话最近的 L0
```

失效是软删除：`valid_to` 有值的事实还在表里，默认检索看不到。`relations` 一期不做。

| 层 | 表 | 作用 |
| --- | --- | --- |
| L0 | `episodes` | 原始对话。`extracted_at` 为空表示冷路径还没处理 |
| L1 | `facts` | 主谓宾事实，带有效期、来源 episode、importance、可选 embedding、生成列 `tsv` |
| L2 | `profile_blocks` | `label / content / token_limit / version` |
| — | `entities` | 从事实里带出的人、地点、组织 |
| — | `intents` | 待办。状态 `open` / `in_progress` / `done` / `cancelled` |
| — | `triggers` | `cron` / `once_at` / `on_event`。到期扫描目前跑 `once_at` 和 `due_at`；`cron` 只匹配五段数字或 `*`；`on_event` 先存着 |
| — | `memory_audit` | 追加、写入、失效、遗忘、画像刷新、待办触发 |

托管画像标签：`identity`、`location`、`work`、`preferences`、`notes`。`remember` 和冷路径会按当前有效事实重写它们。其它标签（`profile set`）会保留。

`as_of` 只影响 L0/L1，以及按该时刻事实临时拼出的 L2。画像表本身不保存历史版本。

## 目录

```
src/memory_kernel/
  sql/001_init.sql   一期 schema
  sql/002_intents.sql 待办与触发器（可在已有库上重复执行）
  store.py           Postgres 读写
  extract.py         mock / OpenAI 抽取
  agenda.py          待办启发式、风险策略、cron 匹配
  reconcile.py       重复跳过、单值失效
  retrieve.py        排序与 token 组装
  service.py         append / remember / recall / forget / tick
  api.py             HTTP
  cli.py             命令行
  demo.py            L0→L1→L2 演示
  intent_demo.py     到期待办演示
  kernel_client.py   给 MCP / 插件用的 HTTP 客户端
  mcp_server.py      stdio 与 Streamable HTTP
integrations/dsh/    dsh --patch 示例
integrations/dsh-memory-plugin/   Cordis 自动注入与落库
```

## 启动

Postgres 映射在本机 **54329**，避免碰到本机默认的 5432。

```bash
docker compose up -d
python -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
memory-kernel migrate
```

`docker compose up -d` 只起数据库。初始化 SQL 挂在 `docker-entrypoint-initdb.d`，**只在数据卷第一次创建时执行**。`memory-kernel migrate` 是幂等的，换机器或卷已经存在时再跑一次即可。

连上 API（可选）：

```bash
memory-kernel serve
# 或连同数据库一起起服务
docker compose --profile app up --build
```

交互文档在 [http://127.0.0.1:8741/docs](http://127.0.0.1:8741/docs)。

## 环境变量

复制 `.env.example` 为 `.env`。进程里已经设置的变量优先，不会被 `.env` 覆盖。

| 变量 | 默认 | 说明 |
| --- | --- | --- |
| `DATABASE_URL` | `postgresql://memory:memory@127.0.0.1:54329/memory` | 库连接串。Compose 里的账号仅供本地 |
| `OPENAI_API_KEY` | 空 | 空则 mock 抽取，不写 embedding |
| `OPENAI_BASE_URL` | `https://api.openai.com/v1` | 兼容网关可改这里 |
| `OPENAI_EXTRACT_MODEL` | `gpt-4o-mini` | 冷路径 / `remember` 的结构化抽取 |
| `OPENAI_EMBED_MODEL` | `text-embedding-3-small` | 维度必须是 1536，与 schema 一致 |
| `MEMORY_API_HOST` | `127.0.0.1` | Compose 的 api 服务会设为 `0.0.0.0` |
| `MEMORY_API_PORT` | `8741` | HTTP 端口 |

有 key 时，抽取失败会退回 mock，嵌入失败则该批事实不写向量，检索继续走关键词。

## Demo

```bash
memory-kernel demo
```

脚本会清掉 `demo-linxia` 的旧数据，然后：

1. 写入几轮对话（L0）：林夏、杭州、星澜科技，晨跑和美式，再搬到成都。
2. `remember("林夏对花生过敏")`，同步写 L1 并刷新 L2。
3. 冷路径把未抽取的 user 发言抽成事实；杭州被成都失效（`valid_to` + `superseded_by`）。助手发言只标记已抽取，不进事实。
4. `recall` 打印组装后的提示词：画像、相关事实、本会话原文。
5. 用另一个用户再召回一次，确认看不到林夏。

L0 原文里仍会留下「住在杭州」这句话。失效的是事实，不是当时的对话。

## 可执行记忆

待办不是第四层事实，它挂在同一次对话上：写进 `intents`，到期后由 `tick` 把结果追加回 L0。

两种写入方式：

- 热路径：用户发言里出现「提醒我」「帮我盯着」「记得提醒我」「别忘了」，`append` 时直接建待办。
- 冷路径：`extract` 再用规则扫「周五前…」「周日前把…」。没有 API key 也能抽。

带截止时间的待办会配一条 `once_at` 触发器。`memory-kernel tick` 找到 `at` 或 `due_at` 已到、且状态仍是 `open` / `in_progress` 的记录，用现有 `recall` 装上 L2、相关 L1 和这则 intent：

- 普通事项执行 `notify`：写一条 `assistant` episode，正文以「提醒：」开头，并把该 intent 标成 `in_progress`。`once_at` 触发后关闭，不会每轮 tick 再响。
- 转账、付款、删除、发邮件、下单这类高风险动作不执行。episode 写明「没有自动执行」，`metadata.proposed_action` 记下提议，状态保持 `open`。

`complete` / `cancel` 会关掉触发器。完成后即使截止时间已过，也不会再响。

```bash
memory-kernel demo-intent

memory-kernel append --user demo-agenda --session s1 --role user --content "提醒我周五前把方案交了"
memory-kernel intent list --user demo-agenda
memory-kernel intent due --user demo-agenda --id <uuid> --at 2026-11-01T00:00:00+00:00
memory-kernel tick --user demo-agenda
memory-kernel intent complete --user demo-agenda --id <uuid>
```

已经用一期库建过的数据卷，再执行一次 `memory-kernel migrate` 就会补上 `002_intents.sql`。新的 `docker compose up` 会按文件名顺序跑 `001` 和 `002`。

## 集成：MCP 和 Cordis 插件

两边都只访问上面的 HTTP API，不另开数据库，也不另做 `tick`。对照见 [ARCHITECTURE.md](ARCHITECTURE.md)，命令见 [integrations/README.md](integrations/README.md)。

dsh 的 MCP 客户端是 `@deepseek-ai/dsh-mcp-client`。`serverName: memory_kernel` 时，工具名是 `mcp__memory_kernel__recall` 这种形式。

```bash
memory-kernel-mcp
# 或
dsh web --patch integrations/dsh/memory-kernel.mcp.cordis.yml
```

自动注入和自动落库是另一个 Cordis 插件，钩在 `agent/pre-step` 和 `agent/turn-stopping`：

```bash
dsh web --patch integrations/dsh/memory-kernel.plugin.cordis.yml
```

插件负责每轮读写 L0。开着插件时，不必再让模型调用 `append_episode`，避免同一句对白写两遍。MCP 可以不装。

装进 dsh profile（`headless` 给 cron，`web` 给交互，各装一次）：

```bash
KERNEL_URL=http://127.0.0.1:8741 MEMORY_USER_ID=default scripts/install-dsh-plugin.sh
DSH_PROFILE=web scripts/install-dsh-plugin.sh
```

到期后要拉起一轮 dsh，而不是只在库里写提醒：

```bash
memory-kernel wake --dry-run
scripts/tick-and-wake.sh
```

默认唤醒命令是 `dsh --profile "$DSH_PROFILE" -`，提示从标准输入送入。crontab 示例在 `integrations/README.md`。

## 测试

```bash
pytest
```

覆盖显式 `remember`、单值冲突失效、冷路径失效、用户隔离、agent 隔离、`as_of`、预算组装，以及待办的创建、到期触发、完成后不再触发、用户隔离。集成测试需要上面的 Postgres；连不上会跳过。单测（抽取规则、排序、预算、待办句式）不连库。

## HTTP

| 方法 | 路径 | 作用 |
| --- | --- | --- |
| `POST` | `/episodes` | 追加 L0 |
| `POST` | `/remember` | 显式事实 + 刷新画像 |
| `POST` | `/recall` | `{user_id, query, budget, session_id?, as_of?}` |
| `POST` | `/forget` | `{fact_id}` 或 `{query}`；`hard: true` 物理删除 |
| `POST` | `/extract` | 跑一轮冷路径，可带 `user_id` |
| `GET` | `/profile` | 当前画像块 |
| `PUT` | `/profile` | 手写一块；托管标签下次刷新会被事实重写 |
| `GET` | `/facts` | `include_inactive=true` 可看到失效行 |
| `POST` | `/intents` | 创建待办；有 `due_at` 时附带 `once_at` |
| `GET` | `/intents` | `user_id`，可选 `status` |
| `POST` | `/intents/{id}/complete` | 完成并关闭触发器 |
| `POST` | `/intents/{id}/cancel` | 取消并关闭触发器 |
| `POST` | `/tick` | 扫描到期项，可带 `user_id` 与 `now` |
| `GET` | `/episodes` `/entities` `/health` | 查看与健康检查 |

命令行同样可用：`append`、`remember`、`recall`、`forget`、`extract`、`profile`、`facts`、`intent`、`tick`。`extract --interval 5` 会循环处理新 episode。

```bash
memory-kernel append --user demo-linxia --session s1 --role user --content "我住在成都"
memory-kernel extract --user demo-linxia
memory-kernel recall --user demo-linxia --query "住在哪里" --budget 2000
```

忘记得默认是软删除。按 query 遗忘时，只处理相关度 ≥ 0.35 的前 5 条，避免一句泛问删掉整份画像。

## 检索说明

中文没有分词器。候选集用查询的二字片段、`pg_trgm` 和 `simple` 配置的 `tsvector`（空格/英文更有用）。问「住在哪」「偏好」这类短句时，会额外把对应谓词（`lives_in`、`likes` 等）拉进候选，否则画像类事实可能因为和问句没有公共二字而漏掉。

时间衰减半衰期 45 天。这是排序用的相对权重，不是评测分数。

Agent 维度：写入落在传入的 `agent_id`（空字符串表示用户全局）。召回某个 agent 时能看到该 agent 加上全局事实；不传 agent 时只看全局。不同 agent 的单值事实不会互相失效。

## 一期不做

图数据库、多模态、跨用户共享、关系边、画像的历史版本、中文专用分词、完整的日历 cron（列表、范围、步长）、以及任何基准跑分。主动行动这一期只做提醒和待办推进；高风险动作停在提议。
