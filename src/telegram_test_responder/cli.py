from __future__ import annotations

import argparse
import asyncio
import logging
import random
import sys
from pathlib import Path

from dotenv import load_dotenv

from .config import ConfigError, Settings
from .policy import IncomingMessage, plan_reply
from .runtime import inspect_identity, recover_message, run_responder


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="telegram-test-responder",
        description="Allowlisted MTProto responder for isolated Telegram bot tests.",
    )
    parser.add_argument(
        "--env-file",
        type=Path,
        default=Path(".env"),
        help="environment file to load (default: .env)",
    )
    parser.add_argument(
        "--log-level",
        choices=("DEBUG", "INFO", "WARNING", "ERROR"),
        default="INFO",
    )
    parser.add_argument(
        "--log-file",
        type=Path,
        help="optional UTF-8 log file (recommended for background mode)",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("inspect", help="log in and print IDs without sending messages")
    subparsers.add_parser("run", help="listen and schedule allowlisted replies")
    recover = subparsers.add_parser(
        "recover",
        help="immediately process one allowlisted message ID after an interrupted run",
    )
    recover.add_argument("message_id", type=int)
    recover.add_argument(
        "--target-id",
        type=int,
        help="required when more than one target ID is configured",
    )
    simulate = subparsers.add_parser("simulate", help="exercise the policy without Telegram")
    simulate.add_argument("text", help="test message text")
    simulate.add_argument("--seed", type=int, default=1, help="deterministic random seed")
    return parser


async def _execute(args: argparse.Namespace) -> None:
    if args.command == "inspect":
        await inspect_identity(Settings.from_env(strict_allowlists=False))
        return

    settings = Settings.from_env(strict_allowlists=True)
    if args.command == "run":
        await run_responder(settings)
        return
    if args.command == "recover":
        await recover_message(settings, args.message_id, args.target_id)
        return

    chat_id = next(iter(settings.target_ids))
    plan = plan_reply(
        IncomingMessage(
            message_id=1,
            sender_id=chat_id,
            chat_id=chat_id,
            text=args.text,
        ),
        settings,
        rng=random.Random(args.seed),
    )
    if plan is None:
        print("message rejected by policy")
        return
    print(
        f"accepted: chat={plan.chat_id} delay={plan.delay_seconds:.2f}s "
        f"reply={plan.text!r} dry_run={settings.dry_run}"
    )


def main() -> None:
    args = _parser().parse_args()
    load_dotenv(args.env_file)
    handlers: list[logging.Handler] = []
    if sys.stderr is not None:
        handlers.append(logging.StreamHandler())
    if args.log_file:
        args.log_file.parent.mkdir(parents=True, exist_ok=True)
        handlers.append(logging.FileHandler(args.log_file, encoding="utf-8"))
    logging.basicConfig(
        level=getattr(logging, args.log_level),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        handlers=handlers or None,
    )
    try:
        asyncio.run(_execute(args))
    except (ConfigError, OSError) as exc:
        print(f"configuration error: {exc}", file=sys.stderr)
        raise SystemExit(2) from exc
    except KeyboardInterrupt:
        print("stopped", file=sys.stderr)


if __name__ == "__main__":
    main()
