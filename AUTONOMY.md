# 运行手册：主动与自主执行

这份文档只讲**这台机器上实际在跑的这套东西怎么用、坑在哪**。架构原理看
`ARCHITECTURE.md`，通用接口看 `README.md`。

## 日常怎么用（30 秒版）

| 我想… | 做法 |
|---|---|
| 让它某天提醒我一件事 | 对话里说「提醒我明天交方案」，或 `intent create --title … --due …` |
| 让它到点自己动手 | 同上加 `--mode act`，标题写清：做什么、在哪做、怎么算完成 |
| 让它每天自己判断该干什么 | `--mode act --cron "0 9 * * *"`（按本地时间） |
| 让它立刻停下 | `New-Item "$env:USERPROFILE\.dsh\ACT_HALT" -ItemType File` |
| 恢复自主执行 | `Remove-Item "$env:USERPROFILE\.dsh\ACT_HALT"` |
| 确认它还活着 | `tick.log` 尾部 + `curl /health` + `curl /intents?user_id=default` |

三句话记住：

1. **手动建任务一定带 `--session`**，否则投递会卡在收件箱里等不到会话。
2. **`act` 是无人值守的**，你不在电脑前它也会改文件。
3. **标题含糊 = 结果含糊**。写「整理电脑」它会自由发挥；写「删掉 `D:\tmp` 下超过 30 天的日志并汇报剩余空间」才稳。

## 组成与启动顺序

```
start-kernel.cmd   → .venv\Scripts\memory-kernel.exe serve   （HTTP，127.0.0.1:8741）
tick-loop.cmd      → 每 30 秒 wake --user default            （提醒的心跳）
Startup\memory-kernel.cmd → 先起内核，再起循环                （登录自启）
```

内核和循环都必须**独立于 DSH 进程**运行。DSH 重启会带走它的子进程树，所以
`start /b` 或 ShellExecute 启动，不要从 DSH 的 shell 里直接前台跑。

检查：

```powershell
Invoke-WebRequest http://127.0.0.1:8741/health -UseBasicParsing | % Content
# {"status":"ok","extractor":"openai","embeddings":"openai"}
```

## 三种到点行为

| mode | 到点后 | 触发后状态 |
|---|---|---|
| 默认（notify） | 弹提醒 | `in_progress` |
| 风险词命中 | 只记录提议，不执行 | `in_progress` |
| `--mode act` | 直接执行 | `done`（一次性）／保留（cron） |

风险词：转账｜汇款｜付款｜支付｜删除｜清空｜发邮件｜下单｜购买。`act` 会覆盖它。

## 创建任务

```powershell
$exe = 'C:\Users\86184\xin-memory\.venv\Scripts\memory-kernel.exe'

# 一次性提醒
& $exe intent create --user default --session session-<id> --title "交方案" --due 2026-11-04T09:00:00Z

# 一次性自动执行
& $exe intent create --user default --session session-<id> --mode act --title "跑测试并修失败用例" --due <UTC>

# 每天 09:00 自主运行（cron 按本地时间）
& $exe intent create --user default --session session-<id> --mode act --cron "0 9 * * *" --title "…"
```

**必须带 `--session`**。意图没有会话归属时，桌面端投递会因为无法确定目标会话而卡在
`wake-inbox` 里（`resolveAgent` 拿不到唯一 live agent）。

## 紧急停止

任意一条生效即把**所有** `act` 降级为提醒：

```powershell
# 持久
New-Item "$env:USERPROFILE\.dsh\ACT_HALT" -ItemType File

# 临时（仅对该进程）
$env:MEMORY_ACT_HALT = "1"
```

删除文件即恢复。

## 投递链路

```
tick → 写 wake-inbox\<intent>-<episode>.json → 插件每 2s 轮询 → agent.followup() → 会话新消息
```

`tick` 只触发和记录，**不写收件箱**；`wake` 在 tick 之上写文件。心跳脚本必须调 `wake`。
DSH 关闭时文件会滞留，下次启动自动送达。

## 已知坑（都是踩过的）

1. **`NO_PROXY` 含 `[::1]`** 会让 httpx 抛 `InvalidURL: Invalid port ':1]'`，抽取静默回退
   mock、嵌入不写向量。已在 `extract.py` / `embed.py` / `kernel_client.py` 用
   `trust_env=False` 绕过。
2. **时区**：`parse_due` 和 `cron_matches` 都按**本地时间**。曾经用 UTC，导致
   “明天”= 17:00、`0 9 * * *` = 17:00。
3. **提醒会污染 L0**：`intent_result` 类型的 episode 内嵌整段 recall 快照，回灌会自我嵌套。
   `store.recent_episodes` 已排除该类型。
4. **会话日志损坏**：插件注入的消息必须带 `id`，否则 DSH 冷启动校验失败、对话打不开。
   已由 `injectionMessage` 的 `randomUUID()` 保证。
5. **助手回复不入库**：`assistant/message` 的事件体是 `{turn, step, message, ...}`，
   消息在 `event.data.message` 里。`captureEvent` 已兼容两种形状。
6. **cron 只匹配那一分钟**：机器睡眠/关机错过 10:00，就整天不跑。已加补跑
   （`agenda.last_occurrence_today` + `store.stale_cron_triggers`）：当天只要还没跑过，
   开机后第一次 tick 会补一次，每天最多一次，多天漏跑只补一天。

## 验收

```powershell
cd C:\Users\86184\xin-memory
.venv\Scripts\python.exe -m pytest -q     # 期望 51 passed
```

## 速查

| 要做的事 | 命令 |
|---|---|
| 看健康 | `Invoke-WebRequest http://127.0.0.1:8741/health -UseBasicParsing` |
| 列待办 | `curl.exe -s "http://127.0.0.1:8741/intents?user_id=default"` |
| 立刻触发一次 | `curl.exe -s -X POST "http://127.0.0.1:8741/tick?user_id=default"` |
| 重启内核 | `C:\Users\86184\xin-memory\start-kernel.cmd` |
| 看心跳日志 | `Get-Content C:\Users\86184\xin-memory\tick.log -Encoding UTF8 -Tail 40` |
| 回收卡住的投递 | 检查 `~\.dsh\wake-inbox\*.json` 是否缺 `sessionId` |
