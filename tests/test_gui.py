from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

from telegram_test_responder.gui import (
    activate_account,
    classify_health,
    countdown_text,
    ensure_project_layout,
    heartbeat_age,
    read_accounts,
    read_env_values,
    target_rules_from_env_values,
    update_env_values,
    write_accounts,
)


def test_healthy_status_requires_running_connected_and_fresh_heartbeat() -> None:
    now = datetime(2026, 10, 12, 9, 0, tzinfo=UTC)
    status = {
        "connected": True,
        "heartbeat_seconds": 30,
        "updated_at": (now - timedelta(seconds=10)).isoformat(),
    }

    assert heartbeat_age(status, now=now) == 10
    assert classify_health(status, process_running=True, now=now) == "HEALTHY"


def test_stale_or_stopped_status_is_not_healthy() -> None:
    now = datetime(2026, 10, 12, 9, 0, tzinfo=UTC)
    stale = {
        "connected": True,
        "heartbeat_seconds": 30,
        "updated_at": (now - timedelta(seconds=71)).isoformat(),
    }

    assert classify_health(stale, process_running=True, now=now) == "UNHEALTHY"
    assert classify_health(stale, process_running=False, now=now) == "STOPPED"


def test_countdown_shows_remaining_time_and_due_time() -> None:
    now = datetime(2026, 10, 13, 10, 0, tzinfo=UTC)
    status = {
        "pending_replies": 1,
        "next_reply_at": (now + timedelta(seconds=125)).isoformat(),
    }

    assert countdown_text(status, now=now).startswith("02:05 ·")
    assert countdown_text({"pending_replies": 0}, now=now) == "—"


def test_env_update_preserves_secrets_and_changes_only_delays(tmp_path: Path) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text(
        "TG_API_HASH=secret\nTG_MIN_DELAY_SECONDS=30\nTG_MAX_DELAY_SECONDS=300\n",
        encoding="utf-8",
    )

    update_env_values(
        env_file,
        {"TG_MIN_DELAY_SECONDS": "45", "TG_MAX_DELAY_SECONDS": "240"},
    )

    values = read_env_values(env_file)
    assert values["TG_API_HASH"] == "secret"
    assert values["TG_MIN_DELAY_SECONDS"] == "45"
    assert values["TG_MAX_DELAY_SECONDS"] == "240"


def test_target_rules_load_legacy_ids_and_per_target_rules() -> None:
    legacy = target_rules_from_env_values({"TG_ALLOWED_CHAT_IDS": "222,333"})
    configured = target_rules_from_env_values(
        {
            "TG_TARGET_RULES_JSON": (
                '{"222":{"mode":"keyword"},'
                '"333":{"mode":"fixed","text":"收到"}}'
            )
        }
    )

    assert [rule.target_id for rule in legacy] == [222, 333]
    assert configured[1].reply_text == "收到"


def test_portable_layout_and_account_selection(tmp_path: Path) -> None:
    ensure_project_layout(tmp_path)
    assert (tmp_path / ".env").is_file()
    assert (tmp_path / ".state").is_dir()
    assert (tmp_path / "logs").is_dir()

    data = {
        "active": None,
        "accounts": [
            {
                "key": "work",
                "label": "工作账号",
                "username": "work_user",
                "user_id": 789,
                "session": ".state/accounts/work",
            }
        ],
    }
    write_accounts(tmp_path, data)
    selected = activate_account(tmp_path, "work")

    assert selected["user_id"] == 789
    assert read_accounts(tmp_path)["active"] == "work"
    env_values = read_env_values(tmp_path / ".env")
    assert env_values["TG_TEST_ACCOUNT_ID"] == "789"
    assert env_values["TG_SESSION"] == ".state/accounts/work"
