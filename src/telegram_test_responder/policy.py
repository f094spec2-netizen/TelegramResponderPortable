from __future__ import annotations

import random
import re
from dataclasses import dataclass
from typing import Protocol

from .config import RULE_FIXED, Settings


class RandomSource(Protocol):
    def uniform(self, a: float, b: float) -> float: ...


@dataclass(frozen=True, slots=True)
class IncomingMessage:
    message_id: int
    sender_id: int | None
    chat_id: int | None
    text: str
    is_forward: bool = False


@dataclass(frozen=True, slots=True)
class ReplyPlan:
    message_id: int
    chat_id: int
    text: str
    delay_seconds: float


KEYWORD_MARKER = re.compile(
    r"只回复下面的关键词\s*[：:]?\s*(?:\r?\n)+\s*([^\r\n]+)",
    flags=re.IGNORECASE,
)


def extract_reply_text(text: str) -> str | None:
    """Extract the single keyword requested by the attendance-test message."""
    match = KEYWORD_MARKER.search(text)
    if match is None:
        return None
    keyword = match.group(1).strip()
    if not keyword or len(keyword) > 64:
        return None
    return keyword


def plan_reply(
    message: IncomingMessage,
    settings: Settings,
    *,
    rng: RandomSource | None = None,
) -> ReplyPlan | None:
    """Return a reply plan only when every test-environment guard passes."""
    if not settings.target_ids or settings.test_account_id is None:
        return None
    rule = settings.rule_for(message.sender_id)
    if rule is None:
        return None
    if message.chat_id not in settings.allowed_chat_ids:
        return None
    if message.is_forward or not message.text:
        return None
    if settings.required_prefix and not message.text.startswith(settings.required_prefix):
        return None
    reply_text = (
        rule.reply_text if rule.mode == RULE_FIXED else extract_reply_text(message.text)
    )
    if not reply_text:
        return None

    source = rng or random.SystemRandom()
    delay = source.uniform(settings.min_delay_seconds, settings.max_delay_seconds)
    return ReplyPlan(
        message_id=message.message_id,
        chat_id=message.chat_id,
        text=reply_text,
        delay_seconds=delay,
    )
