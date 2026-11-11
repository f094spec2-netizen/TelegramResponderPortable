from __future__ import annotations

import random
from dataclasses import replace

import pytest

from telegram_test_responder.config import Settings
from telegram_test_responder.policy import IncomingMessage, extract_reply_text, plan_reply


@pytest.fixture
def settings() -> Settings:
    return Settings.from_env(
        {
            "TG_API_ID": "123",
            "TG_API_HASH": "hash",
            "TG_DEVICE_MODEL": "Gigabyte Aero X16",
            "TG_TEST_ACCOUNT_USERNAME": "test_endpoint",
            "TG_TEST_ACCOUNT_ID": "111",
            "TG_TARGET_BOT_USERNAME": "sy_agent003_bot",
            "TG_TARGET_BOT_ID": "222",
            "TG_ALLOWED_CHAT_IDS": "222",
            "TG_MIN_DELAY_SECONDS": "30",
            "TG_MAX_DELAY_SECONDS": "300",
            "TG_REQUIRED_PREFIX": "[LOADTEST]",
        }
    )


def message(**updates: object) -> IncomingMessage:
    base = IncomingMessage(
        message_id=9,
        sender_id=222,
        chat_id=222,
        text="[LOADTEST] 请只回复下面的关键词：\n\n清风\n\n回复时限说明：",
    )
    return replace(base, **updates)


def test_reply_is_extracted_and_delay_is_in_range(settings: Settings) -> None:
    plan = plan_reply(message(), settings, rng=random.Random(7))

    assert plan is not None
    assert plan.text == "清风"
    assert 30 <= plan.delay_seconds <= 300
    assert plan.message_id == 9


@pytest.mark.parametrize(
    "candidate",
    [
        message(sender_id=999),
        message(chat_id=999),
        message(text=""),
        message(text="请只回复下面的关键词：\n\n清风"),
        message(is_forward=True),
    ],
)
def test_guard_failures_are_ignored(settings: Settings, candidate: IncomingMessage) -> None:
    assert plan_reply(candidate, settings) is None


def test_prefix_can_be_disabled_for_an_isolated_chat(settings: Settings) -> None:
    without_prefix = replace(settings, required_prefix=None)

    plan = plan_reply(
        message(text="请在20分钟内只回复下面的关键词：\n\n明月\n\n回复时限说明："),
        without_prefix,
        rng=random.Random(2),
    )

    assert plan is not None
    assert plan.text == "明月"


def test_real_message_format_extracts_only_keyword() -> None:
    text = """🔔 查岗提醒（编号 35126）

嗨，伊森 👋

查岗小助手来啦～

请在20分钟内只回复下面的关键词：

清风

回复时限说明：
20分钟内回复：正常响应。"""

    assert extract_reply_text(text) == "清风"


def test_message_without_keyword_marker_is_rejected(settings: Settings) -> None:
    without_prefix = replace(settings, required_prefix=None)

    assert plan_reply(message(text="普通消息"), without_prefix) is None


def test_fixed_reply_rule_is_applied_per_target() -> None:
    fixed_settings = Settings.from_env(
        {
            "TG_API_ID": "123",
            "TG_API_HASH": "hash",
            "TG_TEST_ACCOUNT_USERNAME": "test_endpoint",
            "TG_TEST_ACCOUNT_ID": "111",
            "TG_TARGET_IDS": "222,333",
            "TG_ALLOWED_CHAT_IDS": "222,333",
            "TG_TARGET_RULES_JSON": (
                '{"222":{"mode":"keyword"},'
                '"333":{"mode":"fixed","text":"已收到"}}'
            ),
            "TG_MIN_DELAY_SECONDS": "30",
            "TG_MAX_DELAY_SECONDS": "300",
        }
    )

    plan = plan_reply(
        message(sender_id=333, chat_id=333, text="任何文字"),
        fixed_settings,
        rng=random.Random(3),
    )

    assert plan is not None
    assert plan.text == "已收到"


def test_keyword_rule_does_not_use_another_targets_fixed_text() -> None:
    mixed_settings = Settings.from_env(
        {
            "TG_API_ID": "123",
            "TG_API_HASH": "hash",
            "TG_TEST_ACCOUNT_USERNAME": "test_endpoint",
            "TG_TEST_ACCOUNT_ID": "111",
            "TG_TARGET_IDS": "222,333",
            "TG_TARGET_RULES_JSON": (
                '{"222":{"mode":"keyword"},'
                '"333":{"mode":"fixed","text":"已收到"}}'
            ),
        }
    )

    assert plan_reply(message(text="普通消息"), mixed_settings) is None
