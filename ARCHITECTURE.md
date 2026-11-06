# 架构

记忆只有一份：Postgres 16 + pgvector。L0 `episodes`、L1 `facts`、L2 `profile_blocks`，加上 `intents` / `triggers`。HTTP API（`memory-kernel serve`，默认 `127.0.0.1:8741`）是对外边界。MCP 和 Cordis 插件都是这层 API 的客户端，不另开库，也不另写一套到期调度。

到期提醒仍由已有的 `memory-kernel tick` 或 `POST /tick` 触发。插件和 MCP 都不会自己轮询触发器。`memory-kernel wake` 只是调用这一次 tick，再对返回的新触发条目拉起 `dsh --profile headless -`。

细节、环境变量和 demo 见 [README.md](README.md)。

## 集成：MCP 和 Cordis 插件

DeepSeek Harness（`dsh`）把 MCP 工具挂成 `mcp__<serverName>__<tool>`。本仓库的 `serverName` 是 `memory_kernel`，所以模型看到的是 `mcp__memory_kernel__recall`、`mcp__memory_kernel__remember` 等。这是 `@deepseek-ai/dsh-mcp-client` 的命名，不是我们自造的前缀。

两条路可以单独开，也可以一起开：

| | MCP | Cordis 插件 `@memory-kernel/dsh-memory` |
| --- | --- | --- |
| 何时工作 | 模型自己调用工具 | 每轮自动：`agent/pre-step` 注入，`agent/turn-stopping` 落库 |
| 读 | `recall`，以及 `get_profile` / `list_intents` | 同一 `POST /recall`，并把未完成待办列在注入文本里 |
| 写 | `remember` 写 L1；`append_episode` 写 L0；`create_intent` 写待办 | 只把这一轮的 user / assistant 原文 `POST /episodes`，再 `POST /extract` 走已有冷路径 |
| 提醒 | 不调用 tick | 不调用 tick |

插件开着时，自动写入已经覆盖「每轮对话进 L0」。MCP 的 `append_episode` 留给显式补记；不要再让模型把同一句对白再 append 一次。默认把内核当成唯一的跨会话存储，MCP 工具可以不装。

Hermes 以及其他 MCP 客户端走同一条 stdio 命令 `memory-kernel-mcp`。它们的工具名通常不加 `mcp__` 前缀，以各自客户端为准。

配置示例在 `integrations/dsh/`。插件包在 `integrations/dsh-memory-plugin/`。
