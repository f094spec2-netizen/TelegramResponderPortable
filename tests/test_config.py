from __future__ import annotations

import pytest

from telegram_test_responder.config import (
    RULE_FIXED,
    RULE_KEYWORD,
    ConfigError,
    Settings,
    TargetRule,
    normalize_target_username,
    parse_target_rules,
    serialize_target_rules,
)


def valid_env() -> dict[str, str]:
    return {
        "TG_API_ID": "123",
        "TG_API_HASH": "hash",
        "TG_DEVICE_MODEL": "Gigabyte Aero X16",
        "TG_TEST_ACCOUNT_USERNAME": "@test_endpoint",
        "TG_TEST_ACCOUNT_ID": "111",
        "TG_TARGET_BOT_USERNAME": "@sy_agent003_bot",
        "TG_TARGET_BOT_ID": "222",
        "TG_ALLOWED_CHAT_IDS": "222,333",
    }


def test_defaults_are_safe() -> None:
    settings = Settings.from_env(valid_env())

    assert settings.target_bot_username == "sy_agent003_bot"
    assert settings.device_model == "Gigabyte Aero X16"
    assert settings.test_account_username == "test_endpoint"
    assert settings.target_ids == frozenset({222})
    assert settings.target_rules[0].mode == RULE_KEYWORD
    assert settings.allowed_chat_ids == frozenset({222, 333})
    assert settings.min_delay_seconds == 30
    assert settings.max_delay_seconds == 300
    assert settings.dry_run is True
    assert settings.heartbeat_seconds == 30
    assert settings.status_file.as_posix() == ".state/status.json"


def test_missing_chat_allowlist_defaults_to_target_ids() -> None:
    env = valid_env()
    del env["TG_ALLOWED_CHAT_IDS"]

    settings = Settings.from_env(env)

    assert settings.allowed_chat_ids == frozenset({222})


def test_multiple_targets_can_have_individual_rules() -> None:
    env = valid_env() | {
        "TG_TARGET_IDS": "222,444",
        "TG_ALLOWED_CHAT_IDS": "222,444",
        "TG_TARGET_RULES_JSON": (
            '{"222":{"mode":"keyword"},'
            '"444":{"mode":"fixed","text":"收到"}}'
        ),
    }

    settings = Settings.from_env(env)

    assert settings.target_ids == frozenset({222, 444})
    assert settings.rule_for(222).mode == RULE_KEYWORD  # type: ignore[union-attr]
    assert settings.rule_for(444).mode == RULE_FIXED  # type: ignore[union-attr]
    assert settings.rule_for(444).reply_text == "收到"  # type: ignore[union-attr]


def test_target_rule_serialization_is_dotenv_safe_and_round_trips() -> None:
    rules = (TargetRule(444, RULE_FIXED, "收到 #1\n${NAME}"),)

    encoded = serialize_target_rules(rules)
    decoded = parse_target_rules(encoded)

    assert "#" not in encoded
    assert "$" not in encoded
    assert decoded == rules


def test_target_username_normalization() -> None:
    assert normalize_target_username(" @Sample_bot ") == "Sample_bot"

    with pytest.raises(ConfigError, match="5-32"):
        normalize_target_username("bad name")


def test_account_username_is_optional_when_numeric_id_is_pinned() -> None:
    env = valid_env()
    del env["TG_TEST_ACCOUNT_USERNAME"]

    settings = Settings.from_env(env)

    assert settings.test_account_username is None
    assert settings.test_account_id == 111


@pytest.mark.parametrize(
    ("updates", "message"),
    [
        ({"TG_MIN_DELAY_SECONDS": "-1"}, "cannot be negative"),
        (
            {"TG_MIN_DELAY_SECONDS": "10", "TG_MAX_DELAY_SECONDS": "9"},
            "must be >=",
        ),
        ({"TG_MAX_PENDING_REPLIES": "0"}, "at least 1"),
        ({"TG_HEARTBEAT_SECONDS": "4"}, "at least 5"),
        ({"TG_DRY_RUN": "maybe"}, "true or false"),
        (
            {
                "TG_TARGET_IDS": "222,444",
                "TG_TARGET_RULES_JSON": '{"222":{"mode":"keyword"}}',
            },
            "must match",
        ),
    ],
)
def test_invalid_values_are_rejected(updates: dict[str, str], message: str) -> None:
    env = valid_env() | updates

    with pytest.raises(ConfigError, match=message):
        Settings.from_env(env)
