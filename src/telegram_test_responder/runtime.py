from __future__ import annotations

import asyncio
import json
import logging
import os
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from typing import Any

from telethon import TelegramClient, events
from telethon.tl.custom import Message
from telethon.tl.types import User

from .config import ConfigError, Settings, normalize_target_username
from .policy import IncomingMessage, ReplyPlan, plan_reply

LOGGER = logging.getLogger(__name__)
SendReply = Callable[[ReplyPlan], Awaitable[None]]


def _utc_now() -> str:
    return datetime.now(UTC).isoformat()


def _write_status(path: os.PathLike[str], status: dict[str, Any]) -> None:
    """Atomically publish non-sensitive runtime health information."""
    status_path = os.fspath(path)
    parent = os.path.dirname(status_path) or "."
    os.makedirs(parent, exist_ok=True)
    temporary_path = f"{status_path}.tmp"
    with open(temporary_path, "w", encoding="utf-8") as handle:
        json.dump(status, handle, ensure_ascii=False, indent=2, sort_keys=True)
        handle.write("\n")
    os.replace(temporary_path, status_path)


def _incoming(event: events.NewMessage.Event) -> IncomingMessage:
    message: Message = event.message
    return IncomingMessage(
        message_id=event.id,
        sender_id=event.sender_id,
        chat_id=event.chat_id,
        text=event.raw_text or "",
        is_forward=message.forward is not None,
    )


async def inspect_identity(settings: Settings) -> None:
    settings.session.parent.mkdir(parents=True, exist_ok=True)
    async with TelegramClient(
        str(settings.session),
        settings.api_id,
        settings.api_hash,
        device_model=settings.device_model,
    ) as client:
        me = await client.get_me()
        actual_username = (me.username or "").casefold()
        expected_username = (settings.test_account_username or "").casefold()
        if expected_username and actual_username != expected_username:
            raise ConfigError(
                f"logged-in account @{me.username or '<no username>'} does not match "
                f"TG_TEST_ACCOUNT_USERNAME @{settings.test_account_username}"
            )
        print(f"TG_TEST_ACCOUNT_USERNAME={me.username or ''}")
        print(f"TG_TEST_ACCOUNT_ID={me.id}")
        if settings.target_ids:
            joined_ids = ",".join(str(item) for item in sorted(settings.target_ids))
            print(f"TG_TARGET_IDS={joined_ids}")
            print(f"TG_ALLOWED_CHAT_IDS={joined_ids}")
        else:
            target = await client.get_entity(settings.target_bot_username)
            print(f"TG_TARGET_BOT_USERNAME={settings.target_bot_username}")
            print(f"TG_TARGET_BOT_ID={target.id}")
            print(f"TG_TARGET_IDS={target.id}")
            print(f"TG_ALLOWED_CHAT_IDS={target.id}")
            print(f"target_is_bot={bool(getattr(target, 'bot', False))}")


async def recover_message(
    settings: Settings,
    message_id: int,
    target_id: int | None = None,
) -> None:
    """Safely reply to one missed allowlisted message after operational recovery."""
    settings.session.parent.mkdir(parents=True, exist_ok=True)
    async with TelegramClient(
        str(settings.session),
        settings.api_id,
        settings.api_hash,
        device_model=settings.device_model,
    ) as client:
        me = await client.get_me()
        actual_username = (me.username or "").casefold()
        expected_username = (settings.test_account_username or "").casefold()
        if (
            (expected_username and actual_username != expected_username)
            or me.id != settings.test_account_id
        ):
            raise ConfigError("logged-in account does not match the configured test account")

        if target_id is None:
            if len(settings.target_ids) != 1:
                raise ConfigError(
                    "recover requires --target-id when multiple targets are configured"
                )
            target_id = next(iter(settings.target_ids))
        if target_id not in settings.target_ids:
            raise ConfigError("recover target is not in TG_TARGET_IDS")

        message = await client.get_messages(target_id, ids=message_id)
        if message is None:
            raise ConfigError(f"message {message_id} was not found")
        incoming = IncomingMessage(
            message_id=message.id,
            sender_id=message.sender_id,
            chat_id=target_id,
            text=message.raw_text or "",
            is_forward=message.forward is not None,
        )
        plan = plan_reply(incoming, settings)
        if plan is None:
            raise ConfigError(f"message {message_id} did not pass the reply policy")
        if settings.dry_run:
            LOGGER.info("recovery dry-run message=%s", message_id)
            print(f"dry-run recovery accepted message={message_id}")
            return
        await client.send_message(target_id, plan.text, reply_to=message_id)
        LOGGER.info("recovery replied message=%s chat=%s", message_id, target_id)
        print(f"recovery reply sent message={message_id}")


async def run_responder(settings: Settings) -> None:
    settings.session.parent.mkdir(parents=True, exist_ok=True)
    client = TelegramClient(
        str(settings.session),
        settings.api_id,
        settings.api_hash,
        device_model=settings.device_model,
    )
    pending: set[asyncio.Task[None]] = set()
    scheduled: dict[asyncio.Task[None], tuple[ReplyPlan, datetime]] = {}
    heartbeat_task: asyncio.Task[None] | None = None
    resolver_task: asyncio.Task[None] | None = None
    status: dict[str, Any] = {
        "account_id": settings.test_account_id,
        "account_username": settings.test_account_username,
        "connected": False,
        "dry_run": settings.dry_run,
        "heartbeat_seconds": settings.heartbeat_seconds,
        "last_error": None,
        "last_received_at": None,
        "last_received_message_id": None,
        "last_reply_at": None,
        "last_replied_message_id": None,
        "next_reply_at": None,
        "next_reply_delay_seconds": None,
        "next_reply_message_id": None,
        "pending_replies": 0,
        "phase": "starting",
        "started_at": _utc_now(),
        "target_bot_id": settings.target_bot_id,
        "target_bot_username": settings.target_bot_username,
        "target_ids": sorted(settings.target_ids),
        "target_rules": [
            {"id": rule.target_id, "mode": rule.mode} for rule in settings.target_rules
        ],
    }

    def update_status(**updates: Any) -> None:
        status.update(updates)
        status["updated_at"] = _utc_now()
        status["pending_replies"] = len(pending)
        _write_status(settings.status_file, status)

    def update_next_reply() -> None:
        if not scheduled:
            update_status(
                next_reply_at=None,
                next_reply_delay_seconds=None,
                next_reply_message_id=None,
            )
            return
        plan, due_at = min(scheduled.values(), key=lambda item: item[1])
        update_status(
            next_reply_at=due_at.isoformat(),
            next_reply_delay_seconds=plan.delay_seconds,
            next_reply_message_id=plan.message_id,
        )

    def task_done(task: asyncio.Task[None]) -> None:
        pending.discard(task)
        scheduled.pop(task, None)
        if task.cancelled():
            update_next_reply()
            return
        error = task.exception()
        if error is not None:
            LOGGER.error("scheduled reply failed", exc_info=error)
            update_status(phase="reply_error", last_error=str(error))
            update_next_reply()
            return
        update_status(phase="connected", last_error=None)
        update_next_reply()

    update_status()

    await client.start()
    me = await client.get_me()
    actual_username = (me.username or "").casefold()
    expected_username = (settings.test_account_username or "").casefold()
    if expected_username and actual_username != expected_username:
        update_status(
            phase="identity_rejected",
            last_error="logged-in username did not match the configured test account",
        )
        await client.disconnect()
        raise ConfigError(
            f"logged-in account @{me.username or '<no username>'} does not match "
            f"TG_TEST_ACCOUNT_USERNAME @{settings.test_account_username}"
        )
    if me.id != settings.test_account_id:
        update_status(
            phase="identity_rejected",
            last_error=f"logged-in account {me.id} did not match the configured test account",
        )
        await client.disconnect()
        raise ConfigError(
            f"logged-in account {me.id} does not match TG_TEST_ACCOUNT_ID "
            f"{settings.test_account_id}"
        )

    if settings.target_bot_id is not None and settings.target_bot_id in settings.target_ids:
        target = await client.get_entity(settings.target_bot_username)
        if target.id != settings.target_bot_id:
            update_status(
                phase="target_rejected",
                last_error=f"resolved target ID {target.id} did not match the pinned legacy target",
            )
            await client.disconnect()
            raise ConfigError(
                f"resolved ID {target.id} does not match TG_TARGET_BOT_ID "
                f"{settings.target_bot_id}"
            )

    async def send_after_delay(plan: ReplyPlan) -> None:
        await asyncio.sleep(plan.delay_seconds)
        if settings.dry_run:
            LOGGER.info(
                "dry-run completed chat=%s message=%s",
                plan.chat_id,
                plan.message_id,
            )
            update_status(
                phase="dry_run_completed",
                last_replied_message_id=plan.message_id,
            )
            return
        await client.send_message(plan.chat_id, plan.text, reply_to=plan.message_id)
        LOGGER.info(
            "replied to chat=%s message=%s after %.2fs",
            plan.chat_id,
            plan.message_id,
            plan.delay_seconds,
        )
        update_status(
            phase="reply_sent",
            last_error=None,
            last_reply_at=_utc_now(),
            last_replied_message_id=plan.message_id,
        )

    @client.on(events.NewMessage(incoming=True))
    async def on_message(event: events.NewMessage.Event) -> None:
        incoming = _incoming(event)
        if (
            incoming.sender_id not in settings.target_ids
            or incoming.chat_id not in settings.allowed_chat_ids
        ):
            return
        LOGGER.info(
            "received message=%s sender=%s chat=%s",
            event.id,
            event.sender_id,
            event.chat_id,
        )
        update_status(
            phase="message_received",
            last_received_at=_utc_now(),
            last_received_message_id=event.id,
        )
        plan = plan_reply(incoming, settings)
        if plan is None:
            LOGGER.warning(
                "ignored message=%s sender=%s chat=%s because an allowlist guard failed",
                event.id,
                event.sender_id,
                event.chat_id,
            )
            update_status(phase="message_ignored")
            return
        if len(pending) >= settings.max_pending_replies:
            LOGGER.error(
                "pending-reply limit reached; dropping message=%s chat=%s",
                plan.message_id,
                plan.chat_id,
            )
            update_status(
                phase="queue_limit_reached",
                last_error="pending reply limit reached",
            )
            return
        LOGGER.info(
            "scheduled message=%s chat=%s delay=%.2fs dry_run=%s",
            plan.message_id,
            plan.chat_id,
            plan.delay_seconds,
            settings.dry_run,
        )
        task = asyncio.create_task(send_after_delay(plan))
        due_at = datetime.now(UTC) + timedelta(seconds=plan.delay_seconds)
        pending.add(task)
        scheduled[task] = (plan, due_at)
        task.add_done_callback(task_done)
        update_status(
            phase="reply_scheduled",
            last_error=None,
            next_reply_at=due_at.isoformat(),
            next_reply_delay_seconds=plan.delay_seconds,
            next_reply_message_id=plan.message_id,
        )

    async def heartbeat() -> None:
        while True:
            update_status(
                phase="connected",
                connected=client.is_connected(),
            )
            await asyncio.sleep(settings.heartbeat_seconds)

    async def resolve_username_requests() -> None:
        request_path = settings.status_file.with_name("resolve-request.json")
        response_path = settings.status_file.with_name("resolve-response.json")
        while True:
            try:
                request = json.loads(request_path.read_text(encoding="utf-8"))
            except FileNotFoundError:
                await asyncio.sleep(0.5)
                continue
            except (OSError, json.JSONDecodeError) as exc:
                LOGGER.warning("invalid username resolution request: %s", exc)
                request_path.unlink(missing_ok=True)
                await asyncio.sleep(0.5)
                continue

            request_id = str(request.get("request_id", ""))
            try:
                if not request_id:
                    raise ConfigError("解析请求缺少 request_id")
                action = str(request.get("action") or "resolve_username")
                if action == "list_dialogs":
                    dialogs: list[dict[str, Any]] = []
                    async for dialog in client.iter_dialogs(limit=200):
                        entity = dialog.entity
                        if not isinstance(entity, User) or entity.is_self:
                            continue
                        name = " ".join(
                            part
                            for part in (entity.first_name, entity.last_name)
                            if part
                        ).strip()
                        dialogs.append(
                            {
                                "target_id": entity.id,
                                "username": entity.username or "",
                                "name": name or entity.username or str(entity.id),
                                "is_bot": bool(entity.bot),
                            }
                        )
                    response = {
                        "request_id": request_id,
                        "ok": True,
                        "action": action,
                        "dialogs": dialogs,
                    }
                    LOGGER.info("listed %s direct Telegram dialogs", len(dialogs))
                else:
                    username = normalize_target_username(str(request.get("username", "")))
                    entity = await client.get_entity(username)
                    if not isinstance(entity, User):
                        raise ConfigError("该 username 不是 Telegram 用户或 Bot")
                    response = {
                        "request_id": request_id,
                        "ok": True,
                        "action": action,
                        "username": entity.username or username,
                        "target_id": entity.id,
                        "is_bot": bool(entity.bot),
                    }
                    LOGGER.info(
                        "resolved username=@%s target_id=%s is_bot=%s",
                        entity.username or username,
                        entity.id,
                        bool(entity.bot),
                    )
            except Exception as exc:
                LOGGER.warning("username resolution failed: %s", exc)
                response = {
                    "request_id": request_id,
                    "ok": False,
                    "error": str(exc),
                }
            _write_status(response_path, response)
            request_path.unlink(missing_ok=True)
            await asyncio.sleep(0.5)

    LOGGER.info(
        "listening as account=%s for target_ids=%s; allowed_chats=%s dry_run=%s",
        me.id,
        sorted(settings.target_ids),
        sorted(settings.allowed_chat_ids),
        settings.dry_run,
    )
    update_status(phase="connected", connected=True, last_error=None)
    heartbeat_task = asyncio.create_task(heartbeat())
    resolver_task = asyncio.create_task(resolve_username_requests())
    try:
        await client.run_until_disconnected()
    finally:
        if resolver_task is not None:
            resolver_task.cancel()
            await asyncio.gather(resolver_task, return_exceptions=True)
        if heartbeat_task is not None:
            heartbeat_task.cancel()
            await asyncio.gather(heartbeat_task, return_exceptions=True)
        for task in pending:
            task.cancel()
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)
        await client.disconnect()
        update_status(phase="stopped", connected=False)
