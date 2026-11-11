from __future__ import annotations

import json
import os
import re
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path


class ConfigError(ValueError):
    """Raised when the responder configuration is incomplete or unsafe."""


RULE_KEYWORD = "keyword"
RULE_FIXED = "fixed"
VALID_RULE_MODES = frozenset({RULE_KEYWORD, RULE_FIXED})
TELEGRAM_USERNAME = re.compile(r"^[A-Za-z0-9_]{5,32}$")


def _required(env: Mapping[str, str], name: str) -> str:
    value = env.get(name, "").strip()
    if not value:
        raise ConfigError(f"{name} is required")
    return value


def normalize_target_username(value: str) -> str:
    username = value.strip().lstrip("@")
    if not TELEGRAM_USERNAME.fullmatch(username):
        raise ConfigError("username 必须是 5-32 位英文字母、数字或底线")
    return username


def _int(env: Mapping[str, str], name: str, *, default: int | None = None) -> int:
    raw = env.get(name, "").strip()
    if not raw:
        if default is None:
            raise ConfigError(f"{name} is required")
        return default
    try:
        return int(raw)
    except ValueError as exc:
        raise ConfigError(f"{name} must be an integer") from exc


def _bool(env: Mapping[str, str], name: str, *, default: bool) -> bool:
    raw = env.get(name, "").strip().lower()
    if not raw:
        return default
    if raw in {"1", "true", "yes", "on"}:
        return True
    if raw in {"0", "false", "no", "off"}:
        return False
    raise ConfigError(f"{name} must be true or false")


def _id_set(env: Mapping[str, str], name: str, *, required: bool) -> frozenset[int]:
    raw = env.get(name, "").strip()
    if not raw:
        if required:
            raise ConfigError(f"{name} is required")
        return frozenset()

    values: set[int] = set()
    for item in raw.split(","):
        item = item.strip()
        if not item:
            continue
        try:
            values.add(int(item))
        except ValueError as exc:
            raise ConfigError(f"{name} must contain comma-separated integer IDs") from exc
    if required and not values:
        raise ConfigError(f"{name} must contain at least one ID")
    return frozenset(values)


@dataclass(frozen=True, slots=True)
class TargetRule:
    target_id: int
    mode: str
    reply_text: str | None = None


def parse_target_rules(raw: str) -> tuple[TargetRule, ...]:
    """Parse the compact per-target rule map stored in the environment file."""
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ConfigError("TG_TARGET_RULES_JSON must be valid JSON") from exc
    if not isinstance(payload, dict) or not payload:
        raise ConfigError("TG_TARGET_RULES_JSON must contain at least one target")

    rules: list[TargetRule] = []
    for raw_id, value in payload.items():
        try:
            target_id = int(raw_id)
        except (TypeError, ValueError) as exc:
            raise ConfigError("target rule IDs must be integers") from exc
        if target_id <= 0:
            raise ConfigError("target rule IDs must be positive integers")
        if not isinstance(value, dict):
            raise ConfigError(f"target rule {target_id} must be an object")
        mode = str(value.get("mode", "")).strip().lower()
        if mode not in VALID_RULE_MODES:
            raise ConfigError(f"target rule {target_id} has an invalid mode")
        reply_text: str | None = None
        if mode == RULE_FIXED:
            reply_text = str(value.get("text", "")).strip()
            if not reply_text:
                raise ConfigError(f"target rule {target_id} requires fixed reply text")
            if len(reply_text) > 1000:
                raise ConfigError(f"target rule {target_id} reply text is too long")
        rules.append(TargetRule(target_id, mode, reply_text))
    return tuple(sorted(rules, key=lambda item: item.target_id))


def serialize_target_rules(rules: tuple[TargetRule, ...]) -> str:
    payload = {
        str(rule.target_id): {
            "mode": rule.mode,
            **({"text": rule.reply_text} if rule.mode == RULE_FIXED else {}),
        }
        for rule in sorted(rules, key=lambda item: item.target_id)
    }
    encoded = json.dumps(payload, ensure_ascii=True, separators=(",", ":"))
    return encoded.replace("#", "\\u0023").replace("$", "\\u0024")


@dataclass(frozen=True, slots=True)
class Settings:
    api_id: int
    api_hash: str
    session: Path
    device_model: str | None
    test_account_username: str | None
    test_account_id: int | None
    target_bot_username: str
    target_bot_id: int | None
    target_ids: frozenset[int]
    target_rules: tuple[TargetRule, ...]
    allowed_chat_ids: frozenset[int]
    min_delay_seconds: int
    max_delay_seconds: int
    dry_run: bool
    max_pending_replies: int
    heartbeat_seconds: int
    status_file: Path
    required_prefix: str | None

    def rule_for(self, target_id: int | None) -> TargetRule | None:
        if target_id is None:
            return None
        return next((rule for rule in self.target_rules if rule.target_id == target_id), None)

    @classmethod
    def from_env(
        cls,
        env: Mapping[str, str] | None = None,
        *,
        strict_allowlists: bool = True,
    ) -> Settings:
        values = os.environ if env is None else env
        username = values.get("TG_TARGET_BOT_USERNAME", "sy_agent003_bot").strip().lstrip("@")
        if not username:
            raise ConfigError("TG_TARGET_BOT_USERNAME is required")

        min_delay = _int(values, "TG_MIN_DELAY_SECONDS", default=30)
        max_delay = _int(values, "TG_MAX_DELAY_SECONDS", default=300)
        max_pending = _int(values, "TG_MAX_PENDING_REPLIES", default=100)
        heartbeat = _int(values, "TG_HEARTBEAT_SECONDS", default=30)
        if min_delay < 0:
            raise ConfigError("TG_MIN_DELAY_SECONDS cannot be negative")
        if max_delay < min_delay:
            raise ConfigError("TG_MAX_DELAY_SECONDS must be >= TG_MIN_DELAY_SECONDS")
        if max_pending < 1:
            raise ConfigError("TG_MAX_PENDING_REPLIES must be at least 1")
        if heartbeat < 5:
            raise ConfigError("TG_HEARTBEAT_SECONDS must be at least 5")

        account_id: int | None = None
        target_id: int | None = None
        account_username = values.get("TG_TEST_ACCOUNT_USERNAME", "").strip().lstrip("@")
        if strict_allowlists:
            account_id = _int(values, "TG_TEST_ACCOUNT_ID")
            if values.get("TG_TARGET_BOT_ID", "").strip():
                target_id = _int(values, "TG_TARGET_BOT_ID")
        else:
            if values.get("TG_TEST_ACCOUNT_ID", "").strip():
                account_id = _int(values, "TG_TEST_ACCOUNT_ID")
            if values.get("TG_TARGET_BOT_ID", "").strip():
                target_id = _int(values, "TG_TARGET_BOT_ID")

        target_ids = _id_set(values, "TG_TARGET_IDS", required=False)
        if not target_ids and target_id is not None:
            target_ids = frozenset({target_id})
        raw_rules = values.get("TG_TARGET_RULES_JSON", "").strip()
        if raw_rules:
            target_rules = parse_target_rules(raw_rules)
            rule_ids = frozenset(rule.target_id for rule in target_rules)
            if target_ids and target_ids != rule_ids:
                raise ConfigError("TG_TARGET_IDS must match TG_TARGET_RULES_JSON IDs")
            target_ids = rule_ids
        else:
            target_rules = tuple(TargetRule(item, RULE_KEYWORD) for item in sorted(target_ids))
        if strict_allowlists and not target_ids:
            raise ConfigError("TG_TARGET_IDS must contain at least one ID")

        allowed_chat_ids = _id_set(values, "TG_ALLOWED_CHAT_IDS", required=False)
        if not allowed_chat_ids:
            allowed_chat_ids = target_ids
        if strict_allowlists and not target_ids.issubset(allowed_chat_ids):
            raise ConfigError("TG_ALLOWED_CHAT_IDS must include every TG_TARGET_IDS value")

        prefix = values.get("TG_REQUIRED_PREFIX", "").strip() or None
        session = Path(values.get("TG_SESSION", ".state/telegram-load-test").strip())

        return cls(
            api_id=_int(values, "TG_API_ID"),
            api_hash=_required(values, "TG_API_HASH"),
            session=session,
            device_model=values.get("TG_DEVICE_MODEL", "").strip() or None,
            test_account_username=account_username or None,
            test_account_id=account_id,
            target_bot_username=username,
            target_bot_id=target_id,
            target_ids=target_ids,
            target_rules=target_rules,
            allowed_chat_ids=allowed_chat_ids,
            min_delay_seconds=min_delay,
            max_delay_seconds=max_delay,
            dry_run=_bool(values, "TG_DRY_RUN", default=True),
            max_pending_replies=max_pending,
            heartbeat_seconds=heartbeat,
            status_file=Path(values.get("TG_STATUS_FILE", ".state/status.json").strip()),
            required_prefix=prefix,
        )
