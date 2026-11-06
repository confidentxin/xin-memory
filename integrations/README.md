# 接到 DeepSeek Harness

记忆内核继续独占 Postgres。下面两个进程都只调用 `http://127.0.0.1:8741`。

```sh
docker compose up -d
pip install -e ".[dev]"
memory-kernel migrate
memory-kernel serve
```

另开一个终端跑到期扫描。插件和 MCP 都不会代劳。要让 dsh 真正开工，用 `memory-kernel wake`（先 tick，再只对本次新触发的条目拉起一次 headless 会话）。

## 把 Cordis 插件装进 dsh

`web` 和 `headless` 是两个 profile。交互用 web，cron 唤醒用 headless。装到哪一个，就只在那一个里注入记忆。

```sh
export KERNEL_URL=http://127.0.0.1:8741
export MEMORY_USER_ID=default
scripts/install-dsh-plugin.sh                 # 默认 DSH_PROFILE=headless
DSH_PROFILE=web scripts/install-dsh-plugin.sh
```

脚本会 `dsh plugin --profile <profile> add` 这个仓库里的 `integrations/dsh-memory-plugin`。pnpm 把它 link 进 `$DSH_HOME/profiles/<profile>`。包名是 `@memory-kernel/dsh-memory`，`package.json` 里的 `dsh.bundle.patch` 指向 `cordis.patch.yml`，dsh 会把它加进 bundle 层。`@deepseek-ai/cordis` 是可选 peer，由 dsh 自己提供，不必在这个包里安装。

还没装 dsh 时，脚本只打印那条 `plugin add` 命令。dsh 不在 PATH 上时它以非零退出。

确认行已经合成进去：

```sh
dsh --profile headless --dump-config | grep memory-kernel-plugin
```

确认注入：内核开着，且 `MEMORY_USER_ID` 和插件配置是同一个人。

```sh
memory-kernel remember --user "$MEMORY_USER_ID" --text "验证口令是青柑"
KERNEL_URL="$KERNEL_URL" MEMORY_USER_ID="$MEMORY_USER_ID" \
  dsh --profile headless "我的验证口令是什么？只根据已经注入的记忆回答。"
```

临时挂载、不写入 profile 时，用 `--patch`，不要和 `plugin add` 叠用同一个 id：

```sh
dsh --profile headless --patch integrations/dsh/memory-kernel.plugin.cordis.yml \
  "我的验证口令是什么？"
```

`dsh web --patch ...` 只影响这一次 web 进程。

## tick 然后唤醒 dsh

当前 headless 约定是：`dsh --profile headless "任务"` 跑一轮然后退出；任务也可以从标准输入来，位置参数写成单独的 `-`。唤醒默认就是这条：

```sh
dsh --profile "$DSH_PROFILE" -
```

提示词走标准输入，不拼进 shell，避免引号把命令截断。`WAKE_CMD` 可以换成别的命令，同样从标准输入读提示。另外会带上 `WAKE_PROMPT`、`WAKE_TITLE`、`WAKE_INTENT_ID`、`WAKE_ACTION`、`WAKE_USER_ID`。

```sh
KERNEL_URL=http://127.0.0.1:8741 MEMORY_USER_ID=default DSH_PROFILE=headless \
  memory-kernel wake --dry-run
```

`--dry-run`（或 `WAKE_DRY_RUN=1`）仍会调用 `POST /tick`，所以到期的 `once_at` 会被记成已触发，只是不启动 dsh。内核里的提醒 episode 已经写好。想看提示又不想消耗触发器，不要对真实到期数据跑 dry-run。

没有新触发时退出码 0，不会调用 dsh。某次唤醒失败则退出码 1，也不会把同一条再触发一次。

```cron
* * * * * KERNEL_URL=http://127.0.0.1:8741 MEMORY_USER_ID=default DSH_PROFILE=headless /absolute/path/to/scripts/tick-and-wake.sh >> /tmp/memory-kernel-wake.log 2>&1
```

headless 要先装好插件，并且配好 `DEEPSEEK_API_KEY`，否则唤醒进程起得来但模型不可用。这条 cron 不在 Cordis 插件里，插件里没有第二套调度。

## MCP

`memory-kernel-mcp` 把 HTTP API 包成 MCP 工具：`recall`、`remember`、`append_episode`、`get_profile`、`list_intents`、`create_intent`。

stdio（dsh 和 Hermes 都用这个）：

```sh
KERNEL_URL=http://127.0.0.1:8741 MEMORY_USER_ID=default memory-kernel-mcp
```

Streamable HTTP 在 `8742`，`POST /mcp`。客户端带 `text/event-stream` 时返回一条 SSE，否则返回 JSON。

```sh
memory-kernel-mcp --transport http --port 8742
```

dsh 通过 `@deepseek-ai/dsh-mcp-client` 发现工具。`serverName` 必须是 `memory_kernel`（`[A-Za-z0-9_-]{1,32}`）。模型侧名字是：

- `mcp__memory_kernel__recall`
- `mcp__memory_kernel__remember`
- `mcp__memory_kernel__append_episode`
- `mcp__memory_kernel__get_profile`
- `mcp__memory_kernel__list_intents`
- `mcp__memory_kernel__create_intent`

```sh
dsh web --patch integrations/dsh/memory-kernel.mcp.cordis.yml
```

HTTP 传输用 `integrations/dsh/memory-kernel.mcp-http.cordis.yml`。要长期保留，把该文件里的单个 `insert` 并进 `$DSH_HOME/profiles/<profile>/cordis.patch.yml` 或 `$DSH_HOME/cordis.patch.yml`，不要整文件覆盖已有补丁。

Hermes 只要把同一个 stdio 命令配进它的 MCP 列表即可。Hermes 一般直接用工具名 `recall`，不会加 `mcp__memory_kernel__` 前缀。

## Cordis 插件

`integrations/dsh-memory-plugin` 在 `agent/pre-step` 注入回忆，在 `agent/turn-stopping` 把这一轮对话写入 L0，并可选地触发一次冷抽取。未完成待办只是列出来，不会被插件触发。

```sh
dsh plugin --profile web add "$PWD/integrations/dsh-memory-plugin"
```

或者只挂 overlay：

```sh
dsh web --patch integrations/dsh/memory-kernel.plugin.cordis.yml
```

两种启用方式二选一，否则 `memory-kernel-plugin` 这个 id 会重复。

插件和 MCP 一起用时，用 `integrations/dsh/memory-kernel.both.cordis.yml`，并且不要再 `dsh plugin add` 同一包。自动落库交给插件；MCP 留给模型显式 `remember` / `recall` / 待办。不要让模型对同一句对白再调 `append_episode`。

## 环境变量

| 变量 | 默认 | 谁用 |
| --- | --- | --- |
| `KERNEL_URL` | `http://127.0.0.1:8741` | MCP 与插件 |
| `MEMORY_USER_ID` | `default` | 写入和召回的 `user_id` |
| `MEMORY_AGENT_ID` | 空 | 可选 agent 命名空间 |
| `MEMORY_SESSION_ID` | 空 | MCP 默认会话；插件优先用 dsh agent id |
| `MEMORY_BUDGET` | `1200` | 插件注入的 recall 预算 |
| `KERNEL_MCP_URL` | `http://127.0.0.1:8742/mcp` | 仅 HTTP 版 MCP overlay |
