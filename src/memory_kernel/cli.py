from __future__ import annotations

import argparse
import json
import logging
import sys
import time
from datetime import datetime

from pydantic import BaseModel

from memory_kernel.config import load_settings
from memory_kernel.kernel_client import KernelApiError
from memory_kernel.models import Scope
from memory_kernel.service import MemoryError, MemoryService
from memory_kernel.util import redact_dsn


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="memory-kernel", description="自托管三层记忆内核")
    sub = parser.add_subparsers(dest="cmd", required=True)

    migrate = sub.add_parser("migrate", help="执行幂等 SQL schema")
    migrate.set_defaults(func=cmd_migrate)

    serve = sub.add_parser("serve", help="启动 HTTP API")
    serve.set_defaults(func=cmd_serve)

    demo = sub.add_parser("demo", help="跑通 L0→L1→L2 与 recall")
    demo.set_defaults(func=cmd_demo)

    append = sub.add_parser("append", help="写一条 L0 episode")
    append.add_argument("--user", required=True)
    append.add_argument("--session", required=True)
    append.add_argument("--role", default="user", choices=["user", "assistant", "system", "tool"])
    append.add_argument("--content", required=True)
    append.add_argument("--agent", default="")
    append.set_defaults(func=cmd_append)

    remember = sub.add_parser("remember", help="显式写入 L1，并刷新 L2")
    remember.add_argument("--user", required=True)
    remember.add_argument("--text", required=True)
    remember.add_argument("--agent", default="")
    remember.add_argument("--importance", type=float, default=None)
    remember.set_defaults(func=cmd_remember)

    recall = sub.add_parser("recall", help="按 token 预算组装上下文")
    recall.add_argument("--user", required=True)
    recall.add_argument("--query", default="")
    recall.add_argument("--agent", default="")
    recall.add_argument("--session", default=None)
    recall.add_argument("--budget", type=int, default=2500)
    recall.add_argument("--as-of", dest="as_of", default=None, help="ISO 时间，用于查看当时有效的事实")
    recall.set_defaults(func=cmd_recall)

    forget = sub.add_parser("forget", help="软删除（valid_to）或硬删除事实")
    forget.add_argument("--user", required=True)
    forget.add_argument("--agent", default="")
    forget.add_argument("--fact-id", dest="fact_id", default=None)
    forget.add_argument("--query", default=None)
    forget.add_argument("--hard", action="store_true")
    forget.set_defaults(func=cmd_forget)

    extract = sub.add_parser("extract", help="冷路径：从新 episode 抽取事实")
    extract.add_argument("--limit", type=int, default=50)
    extract.add_argument("--user", default=None, help="只处理该用户；默认全部")
    extract.add_argument("--interval", type=float, default=0, help="大于 0 时循环，单位秒")
    extract.set_defaults(func=cmd_extract)

    profile = sub.add_parser("profile", help="读取或手写画像块")
    profile_sub = profile.add_subparsers(dest="profile_cmd", required=True)
    profile_get = profile_sub.add_parser("get")
    profile_get.add_argument("--user", required=True)
    profile_get.add_argument("--agent", default="")
    profile_get.set_defaults(func=cmd_profile_get)
    profile_set = profile_sub.add_parser("set")
    profile_set.add_argument("--user", required=True)
    profile_set.add_argument("--agent", default="")
    profile_set.add_argument("--label", required=True)
    profile_set.add_argument("--content", required=True)
    profile_set.add_argument("--token-limit", dest="token_limit", type=int, default=200)
    profile_set.set_defaults(func=cmd_profile_set)

    facts = sub.add_parser("facts", help="列出事实")
    facts.add_argument("--user", required=True)
    facts.add_argument("--agent", default="")
    facts.add_argument("--all", action="store_true", help="包含已失效事实")
    facts.set_defaults(func=cmd_facts)

    intent = sub.add_parser("intent", help="待办：创建、列出、完成、取消、改期")
    intent_sub = intent.add_subparsers(dest="intent_cmd", required=True)
    intent_create = intent_sub.add_parser("create")
    intent_create.add_argument("--user", required=True)
    intent_create.add_argument("--agent", default="")
    intent_create.add_argument("--session", default=None)
    intent_create.add_argument("--title", required=True)
    intent_create.add_argument("--description", default="")
    intent_create.add_argument("--due", default=None, help="ISO 时间，写入 once_at 触发器")
    intent_create.add_argument("--priority", type=int, default=0)
    intent_create.add_argument(
        "--mode",
        default="notify",
        choices=["notify", "propose", "act"],
        help="act 到点自动执行（无人值守，可用 ACT_HALT 急停）",
    )
    intent_create.add_argument(
        "--cron",
        default=None,
        help='每 N 分钟循环，如 "0 9 * * *"；与 --due 互斥',
    )
    intent_create.set_defaults(func=cmd_intent_create)
    intent_list = intent_sub.add_parser("list")
    intent_list.add_argument("--user", required=True)
    intent_list.add_argument("--agent", default="")
    intent_list.add_argument("--status", default=None)
    intent_list.set_defaults(func=cmd_intent_list)
    intent_complete = intent_sub.add_parser("complete")
    intent_complete.add_argument("--user", required=True)
    intent_complete.add_argument("--agent", default="")
    intent_complete.add_argument("--id", required=True)
    intent_complete.set_defaults(func=cmd_intent_complete)
    intent_cancel = intent_sub.add_parser("cancel")
    intent_cancel.add_argument("--user", required=True)
    intent_cancel.add_argument("--agent", default="")
    intent_cancel.add_argument("--id", required=True)
    intent_cancel.set_defaults(func=cmd_intent_cancel)
    intent_due = intent_sub.add_parser("due", help="改截止时间，并重新武装 once_at")
    intent_due.add_argument("--user", required=True)
    intent_due.add_argument("--agent", default="")
    intent_due.add_argument("--id", required=True)
    intent_due.add_argument("--at", required=True, help="ISO 时间")
    intent_due.set_defaults(func=cmd_intent_due)

    tick = sub.add_parser("tick", help="扫描到期待办，提醒写回 L0；高风险只提议")
    tick.add_argument("--user", default=None)
    tick.add_argument("--now", default=None, help="ISO 时间，默认当前")
    tick.add_argument("--limit", type=int, default=50)
    tick.set_defaults(func=cmd_tick)

    wake = sub.add_parser("wake", help="调用 tick，再对本次新触发的待办拉起一次 dsh")
    wake.add_argument("--user", default=None, help="默认 MEMORY_USER_ID；两者都空则扫描全部用户")
    wake.add_argument("--now", default=None, help="传给 tick 的 ISO 时间")
    wake.add_argument("--limit", type=int, default=50)
    wake.add_argument("--dry-run", action="store_true", help="不启动 dsh、不写桌面收件箱。仍会调用 tick")
    wake.add_argument(
        "--target",
        choices=("desktop", "headless"),
        default=None,
        help="默认 WAKE_TARGET，未设置时是 desktop 收件箱",
    )
    wake.add_argument("--session", default=None, help="桌面会话 id，默认 WAKE_SESSION_ID")
    wake.add_argument("--wake-cmd", default=None, help="设置后改为 headless：提示从标准输入送入")
    wake.set_defaults(func=cmd_wake)

    demo_intent = sub.add_parser("demo-intent", help="演示周五前待办 → 到期 tick → 提醒写回 L0")
    demo_intent.set_defaults(func=cmd_demo_intent)

    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    try:
        args.func(args)
    except MemoryError as exc:
        print(exc.message, file=sys.stderr)
        raise SystemExit(exc.status_code or 1) from exc
    except KernelApiError as exc:
        print(exc.detail, file=sys.stderr)
        raise SystemExit(exc.status_code or 1) from exc


def _service() -> MemoryService:
    settings = load_settings()
    logging.getLogger(__name__).info(
        "数据库 %s，抽取器 %s",
        redact_dsn(settings.database_url),
        "openai" if settings.llm_enabled else "mock",
    )
    return MemoryService(settings)


def _emit(value) -> None:
    if isinstance(value, BaseModel):
        payload = value.model_dump(mode="json")
    elif isinstance(value, list):
        payload = [item.model_dump(mode="json") if isinstance(item, BaseModel) else item for item in value]
    else:
        payload = value
    print(json.dumps(payload, ensure_ascii=False, indent=2))


def _scope(args, *, session: str | None = None) -> Scope:
    return Scope(user_id=args.user, agent_id=getattr(args, "agent", "") or "", session_id=session)


def cmd_migrate(_args) -> None:
    _service().migrate()
    print("schema ok")


def cmd_serve(_args) -> None:
    import uvicorn

    settings = load_settings()
    uvicorn.run(
        "memory_kernel.api:app",
        host=settings.api_host,
        port=settings.api_port,
        factory=False,
    )


def cmd_demo(_args) -> None:
    from memory_kernel.demo import run_demo

    run_demo()


def cmd_append(args) -> None:
    episode = _service().append_episode(_scope(args, session=args.session), args.role, args.content)
    _emit(episode)


def cmd_remember(args) -> None:
    result = _service().remember(_scope(args), args.text, importance=args.importance)
    _emit(result)


def cmd_recall(args) -> None:
    as_of = datetime.fromisoformat(args.as_of) if args.as_of else None
    result = _service().recall(
        _scope(args, session=args.session),
        args.query,
        budget=args.budget,
        as_of=as_of,
    )
    _emit(result)


def cmd_forget(args) -> None:
    from uuid import UUID

    fact_id = UUID(args.fact_id) if args.fact_id else None
    result = _service().forget(_scope(args), fact_id=fact_id, query=args.query, hard=args.hard)
    _emit(result)


def cmd_extract(args) -> None:
    service = _service()
    while True:
        report = service.process_pending(limit=args.limit, user_id=args.user)
        _emit(report)
        if args.interval <= 0:
            return
        time.sleep(args.interval)


def cmd_profile_get(args) -> None:
    _emit(_service().get_profile(_scope(args)))


def cmd_profile_set(args) -> None:
    block = _service().update_profile(
        _scope(args),
        args.label,
        args.content,
        token_limit=args.token_limit,
    )
    _emit(block)


def cmd_facts(args) -> None:
    _emit(_service().list_facts(_scope(args), include_inactive=args.all))


def _intent_id(value: str):
    from uuid import UUID

    return UUID(value)


def cmd_intent_create(args) -> None:
    due = datetime.fromisoformat(args.due) if args.due else None
    if due is not None and args.cron:
        raise SystemExit("--due 与 --cron 不能同时使用")
    intent = _service().create_intent(
        _scope(args, session=args.session),
        args.title,
        description=args.description,
        due_at=due,
        priority=args.priority,
        metadata=None if args.mode == "notify" else {"mode": args.mode},
        cron=args.cron,
    )
    _emit(intent)


def cmd_intent_list(args) -> None:
    _emit(_service().list_intents(_scope(args), status=args.status))


def cmd_intent_complete(args) -> None:
    _emit(_service().complete_intent(_scope(args), _intent_id(args.id)))


def cmd_intent_cancel(args) -> None:
    _emit(_service().cancel_intent(_scope(args), _intent_id(args.id)))


def cmd_intent_due(args) -> None:
    _emit(_service().reschedule_intent(_scope(args), _intent_id(args.id), datetime.fromisoformat(args.at)))


def cmd_tick(args) -> None:
    now = datetime.fromisoformat(args.now) if args.now else None
    report = _service().tick(user_id=args.user, now=now, limit=args.limit)
    _emit(report)


def cmd_wake(args) -> None:
    import os

    from memory_kernel.kernel_client import KernelClient
    from memory_kernel.wake import default_wake_command, desktop_inbox, truthy, wake_via_kernel

    user_id = args.user if args.user is not None else os.getenv("MEMORY_USER_ID") or None
    explicit_cmd = args.wake_cmd or os.getenv("WAKE_CMD")
    target = args.target or os.getenv("WAKE_TARGET") or ("headless" if explicit_cmd else "desktop")
    profile = os.getenv("DSH_PROFILE") or "headless"
    command = explicit_cmd or default_wake_command(profile)
    session_id = args.session if args.session is not None else os.getenv("WAKE_SESSION_ID") or None
    dry_run = args.dry_run or truthy(os.getenv("WAKE_DRY_RUN"))
    client = KernelClient(user_id=user_id or "default")
    try:
        result = wake_via_kernel(
            client,
            user_id=user_id,
            command=command,
            dry_run=dry_run,
            profile=profile,
            now=args.now,
            limit=args.limit,
            target=target,
            session_id=session_id,
            inbox=desktop_inbox(),
        )
    finally:
        client.close()
    _emit(result)
    if not result["ok"]:
        raise SystemExit(1)


def cmd_demo_intent(_args) -> None:
    from memory_kernel.intent_demo import run_intent_demo

    run_intent_demo()
