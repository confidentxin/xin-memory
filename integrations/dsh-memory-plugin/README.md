# @memory-kernel/dsh-memory

DeepSeek Harness 的 Cordis 插件。它在模型开口前调用记忆内核的 `POST /recall`，把画像、相关事实和未完成待办注入本轮上下文；轮次结束时把 user / assistant 原文 `POST /episodes`，并 `POST /extract` 交给已有冷路径。

它不调用 `POST /tick`。提醒仍然由 `memory-kernel tick` 触发。

注入的消息带 `source.kind = plugin`，插件自己的 `session/event` 会跳过它，避免把回忆再写回 L0。

## 启用

在仓库根目录：

```sh
export KERNEL_URL=http://127.0.0.1:8741
export MEMORY_USER_ID=default
scripts/install-dsh-plugin.sh
```

这会执行 `dsh plugin --profile "${DSH_PROFILE:-headless}" add <本包绝对路径>`。pnpm 用 link 装上 `@memory-kernel/dsh-memory`，并因为 `dsh.bundle.patch` 把它的 `cordis.patch.yml` 放进该 profile。交互界面再装一次：`DSH_PROFILE=web scripts/install-dsh-plugin.sh`。

临时进程、不写入 profile：

```sh
dsh --profile headless --patch integrations/dsh/memory-kernel.plugin.cordis.yml \
  "我的验证口令是什么？"
```

不要和 `plugin add` 叠用，否则 `memory-kernel-plugin` 这个 id 会重复。

`KERNEL_URL`、`MEMORY_USER_ID`、`MEMORY_AGENT_ID`、`MEMORY_BUDGET` 会覆盖 YAML。`autoExtract: false` 时只追加 episode，不跑冷路径。`@deepseek-ai/cordis` 是可选 peer，由 dsh 提供。
