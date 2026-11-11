from __future__ import annotations

import asyncio
import ctypes
import json
import logging
import math
import os
import queue
import subprocess
import sys
import threading
import time
import tkinter as tk
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from tkinter import messagebox, simpledialog, ttk
from typing import Any, ClassVar
from uuid import uuid4

from dotenv import load_dotenv
from telethon import TelegramClient
from telethon.errors import SessionPasswordNeededError

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
from telegram_test_responder.runtime import run_responder

CREATE_NO_WINDOW = 0x08000000
CREATE_NEW_PROCESS_GROUP = 0x00000200
DETACHED_PROCESS = 0x00000008
PROCESS_QUERY_LIMITED_INFORMATION = 0x1000


def locate_project_root() -> Path:
    configured = os.environ.get("TELEGRAM_RESPONDER_HOME", "").strip()
    candidates: list[Path] = []
    if configured:
        candidates.append(Path(configured))
    candidates.extend([Path.cwd(), Path(sys.executable).resolve().parent])
    if getattr(sys, "frozen", False):
        candidates.append(Path(sys.executable).resolve().parent.parent)
    else:
        candidates.append(Path(__file__).resolve().parents[2])

    for candidate in candidates:
        if (
            (candidate / ".env").is_file()
            or (candidate / "portable.json").is_file()
        ):
            return candidate.resolve()
    raise FileNotFoundError(
        "找不到 Telegram responder 项目目录。请把 EXE 放在项目根目录或 dist 目录。"
    )


def ensure_project_layout(project_root: Path) -> None:
    (project_root / ".state").mkdir(parents=True, exist_ok=True)
    (project_root / "logs").mkdir(parents=True, exist_ok=True)
    env_path = project_root / ".env"
    if env_path.exists():
        return
    env_path.write_text(
        "\n".join(
            (
                "TG_API_ID=",
                "TG_API_HASH=",
                "TG_SESSION=",
                "TG_DEVICE_MODEL=Gigabyte Aero X16",
                "TG_TEST_ACCOUNT_USERNAME=",
                "TG_TEST_ACCOUNT_ID=",
                "TG_TARGET_IDS=",
                "TG_ALLOWED_CHAT_IDS=",
                "TG_TARGET_RULES_JSON=",
                "TG_MIN_DELAY_SECONDS=30",
                "TG_MAX_DELAY_SECONDS=300",
                "TG_DRY_RUN=false",
                "TG_MAX_PENDING_REPLIES=100",
                "TG_HEARTBEAT_SECONDS=30",
                "TG_STATUS_FILE=.state/status.json",
                "TG_REQUIRED_PREFIX=",
                "",
            )
        ),
        encoding="utf-8",
    )


def read_status(project_root: Path) -> dict[str, Any] | None:
    path = project_root / ".state" / "status.json"
    if not path.is_file():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def _process_path(pid: int) -> Path | None:
    if os.name != "nt":
        return None
    kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
    handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not handle:
        return None
    try:
        size = ctypes.c_ulong(32768)
        buffer = ctypes.create_unicode_buffer(size.value)
        if not kernel32.QueryFullProcessImageNameW(handle, 0, buffer, ctypes.byref(size)):
            return None
        return Path(buffer.value)
    finally:
        kernel32.CloseHandle(handle)


def _worker_executable(project_root: Path) -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve()
    return (project_root / ".venv" / "Scripts" / "pythonw.exe").resolve()


def responder_process(project_root: Path) -> tuple[bool, int | None]:
    pid_file = project_root / ".state" / "responder.pid"
    try:
        pid = int(pid_file.read_text(encoding="ascii").strip())
    except (OSError, ValueError):
        return False, None
    actual = _process_path(pid)
    expected = _worker_executable(project_root)
    if actual is None:
        return False, pid
    return os.path.normcase(actual) == os.path.normcase(expected), pid


def start_worker_process(project_root: Path) -> str:
    running, pid = responder_process(project_root)
    if running:
        return f"后台已经运行 · PID {pid}"
    executable = _worker_executable(project_root)
    if getattr(sys, "frozen", False):
        arguments = [str(executable), "--worker", "--project-root", str(project_root)]
    else:
        arguments = [
            str(executable),
            "-m",
            "telegram_test_responder.gui",
            "--worker",
            "--project-root",
            str(project_root),
        ]
    process = subprocess.Popen(
        arguments,
        cwd=project_root,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        creationflags=CREATE_NO_WINDOW | CREATE_NEW_PROCESS_GROUP | DETACHED_PROCESS,
        close_fds=True,
    )
    (project_root / ".state" / "responder.pid").write_text(
        str(process.pid),
        encoding="ascii",
    )
    time.sleep(0.5)
    if process.poll() is not None:
        raise RuntimeError("后台启动失败，请查看 logs/responder.log")
    return f"后台已启动 · PID {process.pid}"


def stop_worker_process(project_root: Path) -> str:
    running, pid = responder_process(project_root)
    pid_file = project_root / ".state" / "responder.pid"
    if not running or pid is None:
        pid_file.unlink(missing_ok=True)
        return "后台目前未运行"
    result = subprocess.run(
        ["taskkill.exe", "/PID", str(pid), "/T", "/F"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=10,
        creationflags=CREATE_NO_WINDOW,
        check=False,
    )
    if result.returncode != 0:
        raise OSError((result.stderr or result.stdout or "taskkill 失败").strip())
    pid_file.unlink(missing_ok=True)
    return f"后台已停止 · PID {pid}"


def heartbeat_age(status: dict[str, Any] | None, *, now: datetime | None = None) -> float | None:
    if not status or not status.get("updated_at"):
        return None
    try:
        updated = datetime.fromisoformat(str(status["updated_at"]))
    except ValueError:
        return None
    if updated.tzinfo is None:
        updated = updated.replace(tzinfo=UTC)
    current = now or datetime.now(UTC)
    return max(0.0, (current - updated.astimezone(UTC)).total_seconds())


def classify_health(
    status: dict[str, Any] | None,
    *,
    process_running: bool,
    now: datetime | None = None,
) -> str:
    if not process_running:
        return "STOPPED"
    if status is None:
        return "STARTING"
    age = heartbeat_age(status, now=now)
    threshold = int(status.get("heartbeat_seconds", 30)) * 2 + 10
    if status.get("connected") is True and age is not None and age <= threshold:
        return "HEALTHY"
    return "UNHEALTHY"


def _local_time(value: Any) -> str:
    if not value:
        return "—"
    try:
        parsed = datetime.fromisoformat(str(value))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=UTC)
        return parsed.astimezone().strftime("%Y-%m-%d %H:%M:%S")
    except ValueError:
        return str(value)


def countdown_text(status: dict[str, Any] | None, *, now: datetime | None = None) -> str:
    if not status or int(status.get("pending_replies", 0)) < 1:
        return "—"
    value = status.get("next_reply_at")
    if not value:
        return "等待旧版任务完成"
    try:
        due_at = datetime.fromisoformat(str(value))
    except ValueError:
        return "时间格式错误"
    if due_at.tzinfo is None:
        due_at = due_at.replace(tzinfo=UTC)
    current = now or datetime.now(UTC)
    remaining = max(0, math.ceil((due_at.astimezone(UTC) - current).total_seconds()))
    minutes, seconds = divmod(remaining, 60)
    hours, minutes = divmod(minutes, 60)
    if hours == 0:
        clock = f"{minutes:02d}:{seconds:02d}"
    else:
        clock = f"{hours:02d}:{minutes:02d}:{seconds:02d}"
    return f"{clock} · 预计 {_local_time(due_at.isoformat())}"


def read_env_values(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return values
    for line in lines:
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in line:
            continue
        name, value = line.split("=", 1)
        values[name.strip()] = value.strip()
    return values


def update_env_values(path: Path, updates: dict[str, str]) -> None:
    lines = path.read_text(encoding="utf-8").splitlines()
    output: list[str] = []
    remaining = dict(updates)
    for line in lines:
        if "=" in line and not line.lstrip().startswith("#"):
            name = line.split("=", 1)[0].strip()
            if name in remaining:
                output.append(f"{name}={remaining.pop(name)}")
                continue
        output.append(line)
    for name, value in remaining.items():
        output.append(f"{name}={value}")
    temporary = path.with_name(f"{path.name}.tmp")
    temporary.write_text("\n".join(output) + "\n", encoding="utf-8")
    os.replace(temporary, path)


def read_accounts(project_root: Path) -> dict[str, Any]:
    path = project_root / ".state" / "accounts.json"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(data, dict) and isinstance(data.get("accounts"), list):
            return data
    except (OSError, json.JSONDecodeError):
        pass

    env_values = read_env_values(project_root / ".env")
    username = env_values.get("TG_TEST_ACCOUNT_USERNAME", "").strip().lstrip("@")
    user_id = env_values.get("TG_TEST_ACCOUNT_ID", "").strip()
    session = env_values.get("TG_SESSION", "").strip()
    accounts: list[dict[str, Any]] = []
    active: str | None = None
    if username and user_id and session:
        active = "legacy"
        accounts.append(
            {
                "key": active,
                "label": f"@{username}",
                "username": username,
                "user_id": int(user_id),
                "session": session,
            }
        )
    data = {"active": active, "accounts": accounts}
    write_accounts(project_root, data)
    return data


def write_accounts(project_root: Path, data: dict[str, Any]) -> None:
    path = project_root / ".state" / "accounts.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f"{path.name}.tmp")
    temporary.write_text(
        json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    os.replace(temporary, path)


def activate_account(project_root: Path, account_key: str) -> dict[str, Any]:
    data = read_accounts(project_root)
    account = next(
        (item for item in data["accounts"] if item.get("key") == account_key),
        None,
    )
    if account is None:
        raise ValueError("找不到指定的 Telegram 账号")
    data["active"] = account_key
    write_accounts(project_root, data)
    update_env_values(
        project_root / ".env",
        {
            "TG_SESSION": str(account["session"]),
            "TG_TEST_ACCOUNT_USERNAME": str(account.get("username") or ""),
            "TG_TEST_ACCOUNT_ID": str(account["user_id"]),
        },
    )
    return account


def target_rules_from_env_values(values: dict[str, str]) -> tuple[TargetRule, ...]:
    raw_rules = values.get("TG_TARGET_RULES_JSON", "").strip()
    if raw_rules:
        return parse_target_rules(raw_rules)
    raw_ids = (
        values.get("TG_TARGET_IDS", "").strip()
        or values.get("TG_ALLOWED_CHAT_IDS", "").strip()
        or values.get("TG_TARGET_BOT_ID", "").strip()
    )
    if not raw_ids:
        return ()
    try:
        target_ids = {int(item.strip()) for item in raw_ids.split(",") if item.strip()}
    except ValueError as exc:
        raise ConfigError("目标 ID 必须是以逗号分隔的整数") from exc
    if any(item <= 0 for item in target_ids):
        raise ConfigError("目标 ID 必须是正整数")
    return tuple(TargetRule(item, RULE_KEYWORD) for item in sorted(target_ids))


class PortableSettingsDialog(tk.Toplevel):
    def __init__(self, parent: ResponderControl) -> None:
        super().__init__(parent)
        self.parent_control = parent
        self.title("Telegram 账号、装置与对话 ID")
        self.geometry("850x650")
        self.minsize(760, 580)
        self.transient(parent)
        self._request_id: str | None = None
        self._request_job: str | None = None
        self._request_deadline = 0.0
        self._login_busy = False

        env_values = read_env_values(parent.project_root / ".env")
        outer = ttk.Frame(self, padding=18)
        outer.pack(fill="both", expand=True)
        connection = ttk.LabelFrame(outer, text="Telegram API 与装置名称", padding=12)
        connection.pack(fill="x")
        ttk.Label(connection, text="API ID").grid(row=0, column=0, sticky="w")
        self.api_id_var = tk.StringVar(value=env_values.get("TG_API_ID", ""))
        ttk.Entry(connection, textvariable=self.api_id_var, width=18).grid(
            row=1, column=0, sticky="ew", padx=(0, 12), pady=(4, 0)
        )
        ttk.Label(connection, text="API Hash").grid(row=0, column=1, sticky="w")
        self.api_hash_var = tk.StringVar(value=env_values.get("TG_API_HASH", ""))
        ttk.Entry(connection, textvariable=self.api_hash_var, show="•", width=34).grid(
            row=1, column=1, sticky="ew", padx=(0, 12), pady=(4, 0)
        )
        ttk.Label(connection, text="TG 登入装置名称").grid(row=0, column=2, sticky="w")
        self.device_model_var = tk.StringVar(
            value=env_values.get("TG_DEVICE_MODEL", "Gigabyte Aero X16")
        )
        ttk.Entry(connection, textvariable=self.device_model_var, width=28).grid(
            row=1, column=2, sticky="ew", padx=(0, 12), pady=(4, 0)
        )
        ttk.Button(
            connection,
            text="保存连接设定",
            command=lambda: self._save_connection_settings(restart=True),
        ).grid(row=1, column=3, sticky="ew", pady=(4, 0))
        connection.columnconfigure(1, weight=1)
        connection.columnconfigure(2, weight=1)

        notebook = ttk.Notebook(outer)
        notebook.pack(fill="both", expand=True, pady=(14, 0))
        account_tab = ttk.Frame(notebook, padding=12)
        dialogs_tab = ttk.Frame(notebook, padding=12)
        notebook.add(account_tab, text="Telegram 账号")
        notebook.add(dialogs_tab, text="检测对话 ID")

        self.account_tree = ttk.Treeview(
            account_tab,
            columns=("active", "label", "username", "id"),
            show="headings",
            selectmode="browse",
        )
        self.account_tree.heading("active", text="使用中")
        self.account_tree.heading("label", text="账号名称")
        self.account_tree.heading("username", text="Username")
        self.account_tree.heading("id", text="数字 ID")
        self.account_tree.column("active", width=70, stretch=False, anchor="center")
        self.account_tree.column("label", width=190)
        self.account_tree.column("username", width=190)
        self.account_tree.column("id", width=150)
        self.account_tree.pack(fill="both", expand=True)
        account_buttons = ttk.Frame(account_tab, padding=(0, 10, 0, 0))
        account_buttons.pack(fill="x")
        self.login_button = ttk.Button(
            account_buttons,
            text="新增账号并登入",
            command=self._add_account,
        )
        self.login_button.pack(side="left")
        ttk.Button(
            account_buttons,
            text="使用选取账号",
            command=self._activate_selected_account,
        ).pack(side="left", padx=(8, 0))

        dialog_header = ttk.Frame(dialogs_tab)
        dialog_header.pack(fill="x", pady=(0, 8))
        ttk.Label(
            dialog_header,
            text="读取目前账号的私聊列表，不会读取或显示消息内容。",
            style="Sub.TLabel",
        ).pack(side="left")
        self.dialog_refresh_button = ttk.Button(
            dialog_header,
            text="读取对话",
            command=self._load_dialogs,
        )
        self.dialog_refresh_button.pack(side="right")
        self.dialog_tree = ttk.Treeview(
            dialogs_tab,
            columns=("id", "name", "username", "type"),
            show="headings",
            selectmode="browse",
        )
        self.dialog_tree.heading("id", text="对话数字 ID")
        self.dialog_tree.heading("name", text="名称")
        self.dialog_tree.heading("username", text="Username")
        self.dialog_tree.heading("type", text="类型")
        self.dialog_tree.column("id", width=150, stretch=False)
        self.dialog_tree.column("name", width=220)
        self.dialog_tree.column("username", width=200)
        self.dialog_tree.column("type", width=80, stretch=False)
        self.dialog_tree.pack(fill="both", expand=True)
        dialog_footer = ttk.Frame(dialogs_tab, padding=(0, 10, 0, 0))
        dialog_footer.pack(fill="x")
        self.dialog_status_var = tk.StringVar(value="")
        ttk.Label(
            dialog_footer,
            textvariable=self.dialog_status_var,
            style="Sub.TLabel",
        ).pack(side="left")
        ttk.Button(
            dialog_footer,
            text="加入关键词回复名单",
            command=self._add_selected_dialog,
        ).pack(side="right")

        ttk.Button(outer, text="关闭", command=self._close).pack(anchor="e", pady=(12, 0))
        self.protocol("WM_DELETE_WINDOW", self._close)
        self._refresh_accounts()
        self.grab_set()

    def _connection_values(self) -> tuple[int, str, str] | None:
        try:
            api_id = int(self.api_id_var.get().strip())
        except ValueError:
            messagebox.showerror("设定错误", "API ID 必须是整数。", parent=self)
            return None
        api_hash = self.api_hash_var.get().strip()
        device_model = self.device_model_var.get().strip()
        if api_id <= 0 or not api_hash:
            messagebox.showerror("设定错误", "请填写有效的 API ID 与 API Hash。", parent=self)
            return None
        if not device_model or len(device_model) > 64:
            messagebox.showerror(
                "设定错误",
                "TG 登入装置名称必须为 1-64 个字。",
                parent=self,
            )
            return None
        return api_id, api_hash, device_model

    def _save_connection_settings(self, *, restart: bool) -> bool:
        values = self._connection_values()
        if values is None:
            return False
        api_id, api_hash, device_model = values
        status = read_status(self.parent_control.project_root)
        if restart and status and int(status.get("pending_replies", 0)) > 0:
            messagebox.showwarning(
                "暂时无法套用",
                "目前有等待发送的回复，请等倒数完成后再更改连接设定。",
                parent=self,
            )
            return False
        try:
            update_env_values(
                self.parent_control.project_root / ".env",
                {
                    "TG_API_ID": str(api_id),
                    "TG_API_HASH": api_hash,
                    "TG_DEVICE_MODEL": device_model,
                },
            )
        except OSError as exc:
            messagebox.showerror("储存失败", str(exc), parent=self)
            return False
        if restart:
            self.parent_control._restart_for_settings(
                f"连接设定已保存；装置名称：{device_model}"
            )
        return True

    def _refresh_accounts(self) -> None:
        for item in self.account_tree.get_children():
            self.account_tree.delete(item)
        data = read_accounts(self.parent_control.project_root)
        active = data.get("active")
        for account in data["accounts"]:
            key = str(account["key"])
            self.account_tree.insert(
                "",
                "end",
                iid=key,
                values=(
                    "✓" if key == active else "",
                    account.get("label") or "—",
                    f"@{account['username']}" if account.get("username") else "—",
                    account.get("user_id"),
                ),
            )
        if active and active in self.account_tree.get_children():
            self.account_tree.selection_set(active)

    def _prompt_from_worker(
        self,
        title: str,
        prompt: str,
        *,
        secret: bool = False,
    ) -> str | None:
        result: queue.Queue[str | None] = queue.Queue(maxsize=1)

        def ask() -> None:
            options: dict[str, Any] = {"parent": self}
            if secret:
                options["show"] = "*"
            result.put(
                simpledialog.askstring(
                    title,
                    prompt,
                    **options,
                )
            )

        self.after(0, ask)
        return result.get()

    def _add_account(self) -> None:
        if self._login_busy or not self._save_connection_settings(restart=False):
            return
        label = simpledialog.askstring(
            "账号名称",
            "输入方便辨识的名称，例如：工作账号或测试账号",
            parent=self,
        )
        if label is None:
            return
        phone = simpledialog.askstring(
            "Telegram 手机号",
            "输入含国码的手机号，例如 +886...",
            parent=self,
        )
        if not phone:
            return
        values = self._connection_values()
        if values is None:
            return
        api_id, api_hash, device_model = values
        key = uuid4().hex[:12]
        relative_session = f".state/accounts/{key}"
        session_path = self.parent_control.project_root / relative_session
        session_path.parent.mkdir(parents=True, exist_ok=True)
        self._login_busy = True
        self.login_button.configure(state="disabled")

        async def authorize() -> dict[str, Any]:
            client = TelegramClient(
                str(session_path),
                api_id,
                api_hash,
                device_model=device_model,
            )
            await client.connect()
            try:
                sent = await client.send_code_request(phone.strip())
                code = self._prompt_from_worker(
                    "Telegram 验证码",
                    "输入 Telegram 收到的登入验证码",
                )
                if not code:
                    raise RuntimeError("已取消 Telegram 登入")
                try:
                    await client.sign_in(
                        phone=phone.strip(),
                        code=code.strip(),
                        phone_code_hash=sent.phone_code_hash,
                    )
                except SessionPasswordNeededError:
                    password = self._prompt_from_worker(
                        "Telegram 两步验证",
                        "输入 Telegram 两步验证密码",
                        secret=True,
                    )
                    if not password:
                        raise RuntimeError("已取消两步验证") from None
                    await client.sign_in(password=password)
                me = await client.get_me()
                return {
                    "key": key,
                    "label": label.strip() or f"Telegram {me.id}",
                    "username": me.username or "",
                    "user_id": me.id,
                    "session": relative_session,
                }
            finally:
                await client.disconnect()

        def worker() -> None:
            try:
                account = asyncio.run(authorize())
                error: str | None = None
            except Exception as exc:
                account = {}
                error = str(exc)
            self.after(0, lambda: self._account_login_finished(account, error))

        threading.Thread(target=worker, daemon=True).start()

    def _account_login_finished(
        self,
        account: dict[str, Any],
        error: str | None,
    ) -> None:
        self._login_busy = False
        self.login_button.configure(state="normal")
        if error:
            messagebox.showerror("Telegram 登入失败", error, parent=self)
            return
        data = read_accounts(self.parent_control.project_root)
        data["accounts"].append(account)
        if not data.get("active"):
            data["active"] = account["key"]
        write_accounts(self.parent_control.project_root, data)
        if data["active"] == account["key"]:
            activate_account(self.parent_control.project_root, str(account["key"]))
        self._refresh_accounts()
        messagebox.showinfo(
            "登入完成",
            f"已加入账号：{account['label']}\n数字 ID：{account['user_id']}",
            parent=self,
        )

    def _activate_selected_account(self) -> None:
        selection = self.account_tree.selection()
        if not selection:
            messagebox.showinfo("请选择账号", "请先选取一组 Telegram 账号。", parent=self)
            return
        status = read_status(self.parent_control.project_root)
        if status and int(status.get("pending_replies", 0)) > 0:
            messagebox.showwarning(
                "暂时无法切换",
                "目前有等待发送的回复，请等倒数完成后再切换账号。",
                parent=self,
            )
            return
        try:
            account = activate_account(
                self.parent_control.project_root,
                selection[0],
            )
        except (OSError, ValueError) as exc:
            messagebox.showerror("切换失败", str(exc), parent=self)
            return
        self._refresh_accounts()
        self.parent_control._restart_for_settings(f"已切换账号：{account['label']}")

    async def _direct_dialog_list(
        self,
        values: tuple[int, str, str],
    ) -> list[dict[str, Any]]:
        api_id, api_hash, device_model = values
        data = read_accounts(self.parent_control.project_root)
        active = next(
            (item for item in data["accounts"] if item.get("key") == data.get("active")),
            None,
        )
        if active is None:
            raise RuntimeError("请先新增并选择 Telegram 账号")
        session_path = self.parent_control.project_root / str(active["session"])
        client = TelegramClient(
            str(session_path),
            api_id,
            api_hash,
            device_model=device_model,
        )
        await client.connect()
        try:
            if not await client.is_user_authorized():
                raise RuntimeError("选取的 Telegram 账号尚未授权")
            dialogs: list[dict[str, Any]] = []
            async for dialog in client.iter_dialogs(limit=200):
                entity = dialog.entity
                if not hasattr(entity, "first_name") or getattr(entity, "is_self", False):
                    continue
                name = " ".join(
                    part
                    for part in (
                        getattr(entity, "first_name", None),
                        getattr(entity, "last_name", None),
                    )
                    if part
                ).strip()
                dialogs.append(
                    {
                        "target_id": entity.id,
                        "username": getattr(entity, "username", None) or "",
                        "name": name or getattr(entity, "username", None) or str(entity.id),
                        "is_bot": bool(getattr(entity, "bot", False)),
                    }
                )
            return dialogs
        finally:
            await client.disconnect()

    def _load_dialogs(self) -> None:
        if self._request_id is not None:
            return
        connection_values = self._connection_values()
        if connection_values is None:
            return
        self.dialog_refresh_button.configure(state="disabled")
        self.dialog_status_var.set("正在读取对话…")
        running, _ = responder_process(self.parent_control.project_root)
        if running:
            request_id = uuid4().hex
            state_dir = self.parent_control.project_root / ".state"
            request_path = state_dir / "resolve-request.json"
            response_path = state_dir / "resolve-response.json"
            temporary = request_path.with_name(f"{request_path.name}.tmp")
            try:
                response_path.unlink(missing_ok=True)
                temporary.write_text(
                    json.dumps(
                        {"request_id": request_id, "action": "list_dialogs"},
                        ensure_ascii=True,
                    ),
                    encoding="utf-8",
                )
                os.replace(temporary, request_path)
            except OSError as exc:
                self.dialog_refresh_button.configure(state="normal")
                messagebox.showerror("读取失败", str(exc), parent=self)
                return
            self._request_id = request_id
            self._request_deadline = time.monotonic() + 30
            self._poll_dialog_response()
            return

        def worker() -> None:
            try:
                dialogs = asyncio.run(self._direct_dialog_list(connection_values))
                error: str | None = None
            except Exception as exc:
                dialogs = []
                error = str(exc)
            self.after(0, lambda: self._dialogs_loaded(dialogs, error))

        threading.Thread(target=worker, daemon=True).start()

    def _poll_dialog_response(self) -> None:
        response_path = self.parent_control.project_root / ".state" / "resolve-response.json"
        response: dict[str, Any] | None = None
        try:
            candidate = json.loads(response_path.read_text(encoding="utf-8"))
            if isinstance(candidate, dict) and candidate.get("request_id") == self._request_id:
                response = candidate
        except (OSError, json.JSONDecodeError):
            pass
        if response is not None:
            response_path.unlink(missing_ok=True)
            self._request_id = None
            self._request_job = None
            if response.get("ok"):
                self._dialogs_loaded(list(response.get("dialogs", [])), None)
            else:
                self._dialogs_loaded([], str(response.get("error") or "读取失败"))
            return
        if time.monotonic() >= self._request_deadline:
            self._request_id = None
            self._request_job = None
            self._dialogs_loaded([], "读取对话逾时")
            return
        self._request_job = self.after(500, self._poll_dialog_response)

    def _dialogs_loaded(self, dialogs: list[dict[str, Any]], error: str | None) -> None:
        self.dialog_refresh_button.configure(state="normal")
        for item in self.dialog_tree.get_children():
            self.dialog_tree.delete(item)
        if error:
            self.dialog_status_var.set("读取失败")
            messagebox.showerror("读取对话失败", error, parent=self)
            return
        for item in dialogs:
            target_id = int(item["target_id"])
            self.dialog_tree.insert(
                "",
                "end",
                iid=str(target_id),
                values=(
                    target_id,
                    item.get("name") or "—",
                    f"@{item['username']}" if item.get("username") else "—",
                    "Bot" if item.get("is_bot") else "用户",
                ),
            )
        self.dialog_status_var.set(f"已读取 {len(dialogs)} 组私聊")

    def _add_selected_dialog(self) -> None:
        selection = self.dialog_tree.selection()
        if not selection:
            messagebox.showinfo("请选择对话", "请先选取一组对话。", parent=self)
            return
        target_id = int(selection[0])
        try:
            rules = list(
                target_rules_from_env_values(
                    read_env_values(self.parent_control.project_root / ".env")
                )
            )
        except ConfigError as exc:
            messagebox.showerror("名单错误", str(exc), parent=self)
            return
        if any(rule.target_id == target_id for rule in rules):
            messagebox.showinfo("已经加入", "这组对话已经在回复名单中。", parent=self)
            return
        rules.append(TargetRule(target_id, RULE_KEYWORD))
        if self.parent_control.apply_target_rules(tuple(rules)):
            messagebox.showinfo(
                "已加入",
                f"对话 ID {target_id} 已加入关键词解析名单。",
                parent=self,
            )

    def _close(self) -> None:
        if self._request_job is not None:
            self.after_cancel(self._request_job)
            self._request_job = None
        self.destroy()


class TargetRulesDialog(tk.Toplevel):
    MODE_LABELS: ClassVar[dict[str, str]] = {
        RULE_KEYWORD: "关键词解析",
        RULE_FIXED: "固定文字",
    }
    LABEL_MODES: ClassVar[dict[str, str]] = {
        label: mode for mode, label in MODE_LABELS.items()
    }

    def __init__(
        self,
        parent: ResponderControl,
        rules: tuple[TargetRule, ...],
        on_save: Callable[[tuple[TargetRule, ...]], bool],
    ) -> None:
        super().__init__(parent)
        self.title("管理回复名单")
        self.geometry("780x590")
        self.minsize(700, 520)
        self.transient(parent)
        self.parent_control = parent
        self.on_save = on_save
        self.rules = {rule.target_id: rule for rule in rules}
        self._resolve_job: str | None = None
        self._resolve_request_id: str | None = None
        self._resolve_deadline = 0.0

        outer = ttk.Frame(self, padding=18)
        outer.pack(fill="both", expand=True)
        ttk.Label(
            outer,
            text="每组 Telegram ID 可独立选择关键词解析或固定文字。",
            style="Sub.TLabel",
        ).pack(anchor="w", pady=(0, 12))

        form = ttk.LabelFrame(outer, text="新增或编辑", padding=12)
        form.pack(fill="x")
        ttk.Label(form, text="Telegram 数字 ID").grid(row=0, column=0, sticky="w")
        self.target_id_var = tk.StringVar()
        ttk.Entry(form, textvariable=self.target_id_var, width=20).grid(
            row=1, column=0, sticky="ew", padx=(0, 12), pady=(4, 0)
        )
        ttk.Label(form, text="回复规则").grid(row=0, column=1, sticky="w")
        self.mode_var = tk.StringVar(value=self.MODE_LABELS[RULE_KEYWORD])
        mode_box = ttk.Combobox(
            form,
            textvariable=self.mode_var,
            values=tuple(self.MODE_LABELS.values()),
            state="readonly",
            width=14,
        )
        mode_box.grid(row=1, column=1, sticky="ew", padx=(0, 12), pady=(4, 0))
        mode_box.bind("<<ComboboxSelected>>", lambda _event: self._update_text_state())
        ttk.Label(form, text="固定回复内容").grid(row=0, column=2, sticky="w")
        self.reply_text = tk.Text(form, height=3, width=32, wrap="word")
        self.reply_text.grid(row=1, column=2, sticky="ew", padx=(0, 12), pady=(4, 0))
        ttk.Button(form, text="新增/更新", command=self._upsert).grid(
            row=1, column=3, sticky="ns", pady=(4, 0)
        )
        ttk.Separator(form).grid(
            row=2, column=0, columnspan=4, sticky="ew", pady=(12, 10)
        )
        ttk.Label(form, text="可输入 @username 在本机解析数字 ID").grid(
            row=3, column=0, columnspan=4, sticky="w"
        )
        self.username_var = tk.StringVar()
        ttk.Entry(form, textvariable=self.username_var, width=24).grid(
            row=4, column=0, sticky="ew", padx=(0, 12), pady=(4, 0)
        )
        self.resolve_button = ttk.Button(
            form,
            text="解析并填入 ID",
            command=self._resolve_username,
        )
        self.resolve_button.grid(row=4, column=1, sticky="ew", padx=(0, 12), pady=(4, 0))
        self.resolve_status_var = tk.StringVar(value="")
        ttk.Label(
            form,
            textvariable=self.resolve_status_var,
            style="Sub.TLabel",
        ).grid(row=4, column=2, columnspan=2, sticky="w", pady=(4, 0))
        form.columnconfigure(2, weight=1)

        table_frame = ttk.Frame(outer)
        table_frame.pack(fill="both", expand=True, pady=(14, 0))
        self.tree = ttk.Treeview(
            table_frame,
            columns=("id", "mode", "text"),
            show="headings",
            selectmode="browse",
        )
        self.tree.heading("id", text="Telegram ID")
        self.tree.heading("mode", text="规则")
        self.tree.heading("text", text="固定回复内容")
        self.tree.column("id", width=150, stretch=False)
        self.tree.column("mode", width=110, stretch=False)
        self.tree.column("text", width=360)
        scrollbar = ttk.Scrollbar(table_frame, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=scrollbar.set)
        self.tree.pack(side="left", fill="both", expand=True)
        scrollbar.pack(side="right", fill="y")
        self.tree.bind("<<TreeviewSelect>>", self._load_selected)

        footer = ttk.Frame(outer, padding=(0, 12, 0, 0))
        footer.pack(fill="x")
        ttk.Button(footer, text="删除选取", command=self._delete_selected).pack(side="left")
        ttk.Button(footer, text="取消", command=self._close).pack(side="right")
        ttk.Button(
            footer,
            text="保存并套用",
            style="Accent.TButton",
            command=self._save_all,
        ).pack(side="right", padx=(0, 8))

        self._refresh_tree()
        self._update_text_state()
        self.protocol("WM_DELETE_WINDOW", self._close)
        self.grab_set()
        self.target_id_var.set("")

    def _update_text_state(self) -> None:
        is_fixed = self.LABEL_MODES.get(self.mode_var.get()) == RULE_FIXED
        self.reply_text.configure(state="normal" if is_fixed else "disabled")

    def _resolve_username(self) -> None:
        try:
            username = normalize_target_username(self.username_var.get())
        except ConfigError as exc:
            messagebox.showerror("username 格式错误", str(exc), parent=self)
            return
        running, _ = responder_process(self.parent_control.project_root)
        if not running:
            messagebox.showwarning(
                "后台未运行",
                "请先启动挂机服务，再解析 @username。",
                parent=self,
            )
            return

        state_dir = self.parent_control.project_root / ".state"
        request_path = state_dir / "resolve-request.json"
        response_path = state_dir / "resolve-response.json"
        request_id = uuid4().hex
        temporary = request_path.with_name(f"{request_path.name}.tmp")
        try:
            state_dir.mkdir(parents=True, exist_ok=True)
            response_path.unlink(missing_ok=True)
            temporary.write_text(
                json.dumps(
                    {"request_id": request_id, "username": username},
                    ensure_ascii=True,
                ),
                encoding="utf-8",
            )
            os.replace(temporary, request_path)
        except OSError as exc:
            messagebox.showerror("解析请求失败", str(exc), parent=self)
            return

        self._resolve_request_id = request_id
        self._resolve_deadline = time.monotonic() + 20
        self.resolve_button.configure(state="disabled")
        self.resolve_status_var.set(f"正在解析 @{username}…")
        self._poll_resolution()

    def _poll_resolution(self) -> None:
        response_path = self.parent_control.project_root / ".state" / "resolve-response.json"
        response: dict[str, Any] | None = None
        try:
            candidate = json.loads(response_path.read_text(encoding="utf-8"))
            if candidate.get("request_id") == self._resolve_request_id:
                response = candidate
        except (OSError, json.JSONDecodeError):
            pass

        if response is not None:
            response_path.unlink(missing_ok=True)
            self.resolve_button.configure(state="normal")
            if response.get("ok"):
                target_id = int(response["target_id"])
                username = str(response.get("username") or self.username_var.get()).lstrip("@")
                account_type = "Bot" if response.get("is_bot") else "用户"
                self.target_id_var.set(str(target_id))
                self.resolve_status_var.set(
                    f"已解析 @{username}（{account_type}）→ {target_id}"
                )
            else:
                error = str(response.get("error") or "找不到该 username")
                self.resolve_status_var.set("解析失败")
                messagebox.showerror("解析失败", error, parent=self)
            self._resolve_request_id = None
            self._resolve_job = None
            return

        if time.monotonic() >= self._resolve_deadline:
            self.resolve_button.configure(state="normal")
            self.resolve_status_var.set("解析逾时，请确认后台连接正常。")
            self._resolve_request_id = None
            self._resolve_job = None
            return
        self._resolve_job = self.after(500, self._poll_resolution)

    def _close(self) -> None:
        if self._resolve_job is not None:
            self.after_cancel(self._resolve_job)
            self._resolve_job = None
        self.destroy()

    def _upsert(self) -> None:
        try:
            target_id = int(self.target_id_var.get().strip())
        except ValueError:
            messagebox.showerror("设定错误", "Telegram ID 必须是整数。", parent=self)
            return
        if target_id <= 0:
            messagebox.showerror("设定错误", "Telegram ID 必须是正整数。", parent=self)
            return
        mode = self.LABEL_MODES.get(self.mode_var.get())
        if mode is None:
            messagebox.showerror("设定错误", "请选择回复规则。", parent=self)
            return
        reply_text: str | None = None
        if mode == RULE_FIXED:
            reply_text = self.reply_text.get("1.0", "end-1c").strip()
            if not reply_text:
                messagebox.showerror("设定错误", "固定文字模式必须填写回复内容。", parent=self)
                return
            if len(reply_text) > 1000:
                messagebox.showerror("设定错误", "固定回复内容不得超过 1000 字。", parent=self)
                return
        self.rules[target_id] = TargetRule(target_id, mode, reply_text)
        self._refresh_tree(select_id=target_id)

    def _refresh_tree(self, *, select_id: int | None = None) -> None:
        for item in self.tree.get_children():
            self.tree.delete(item)
        for rule in sorted(self.rules.values(), key=lambda item: item.target_id):
            item_id = str(rule.target_id)
            self.tree.insert(
                "",
                "end",
                iid=item_id,
                values=(
                    rule.target_id,
                    self.MODE_LABELS[rule.mode],
                    rule.reply_text or "—",
                ),
            )
        if select_id is not None and str(select_id) in self.tree.get_children():
            self.tree.selection_set(str(select_id))

    def _load_selected(self, _event: tk.Event[tk.Misc] | None = None) -> None:
        selection = self.tree.selection()
        if not selection:
            return
        rule = self.rules[int(selection[0])]
        self.target_id_var.set(str(rule.target_id))
        self.mode_var.set(self.MODE_LABELS[rule.mode])
        self.reply_text.configure(state="normal")
        self.reply_text.delete("1.0", "end")
        if rule.reply_text:
            self.reply_text.insert("1.0", rule.reply_text)
        self._update_text_state()

    def _delete_selected(self) -> None:
        selection = self.tree.selection()
        if not selection:
            return
        self.rules.pop(int(selection[0]), None)
        self._refresh_tree()
        self.target_id_var.set("")
        self.mode_var.set(self.MODE_LABELS[RULE_KEYWORD])
        self.reply_text.configure(state="normal")
        self.reply_text.delete("1.0", "end")
        self._update_text_state()

    def _save_all(self) -> None:
        if not self.rules:
            messagebox.showerror("设定错误", "名单至少要保留一组 Telegram ID。", parent=self)
            return
        rules = tuple(sorted(self.rules.values(), key=lambda item: item.target_id))
        if self.on_save(rules):
            self._close()


class ResponderControl(tk.Tk):
    def __init__(self, project_root: Path) -> None:
        super().__init__()
        self.project_root = project_root
        ensure_project_layout(project_root)
        self.title("Telegram 自动回复控制台")
        self.geometry("1120x720")
        self.minsize(980, 660)
        self.configure(bg="#f4f6f8")
        self._action_running = False
        self._log_position = 0
        self._status_job: str | None = None
        self._log_job: str | None = None
        self._build_ui()
        self.after(200, self.refresh_status)
        self.after(300, self.refresh_log)
        env_values = read_env_values(self.project_root / ".env")
        accounts = read_accounts(self.project_root)
        if not env_values.get("TG_API_ID") or not accounts.get("active"):
            self.after(700, self.open_portable_settings)

    def _build_ui(self) -> None:
        style = ttk.Style(self)
        style.theme_use("clam")
        style.configure("TFrame", background="#f4f6f8")
        style.configure("Card.TFrame", background="white")
        style.configure("Title.TLabel", background="#f4f6f8", font=("Segoe UI", 18, "bold"))
        style.configure("Sub.TLabel", background="#f4f6f8", foreground="#52606d")
        style.configure("Card.TLabel", background="white", font=("Segoe UI", 10))
        style.configure("Value.TLabel", background="white", font=("Segoe UI", 10, "bold"))
        style.configure("Accent.TButton", font=("Segoe UI", 10, "bold"), padding=(14, 8))
        style.configure("TButton", font=("Segoe UI", 10), padding=(12, 8))

        outer = ttk.Frame(self, padding=22)
        outer.pack(fill="both", expand=True)
        ttk.Label(outer, text="Telegram 自动回复", style="Title.TLabel").pack(anchor="w")
        ttk.Label(
            outer,
            text="关闭本视窗不会停止后台服务；只有“停止挂机”会停止。",
            style="Sub.TLabel",
        ).pack(anchor="w", pady=(3, 16))

        actions = ttk.Frame(outer, padding=(0, 0, 0, 12))
        actions.pack(fill="x")
        self.start_button = ttk.Button(
            actions,
            text="启动挂机",
            style="Accent.TButton",
            command=lambda: self.run_action("start", "启动"),
        )
        self.start_button.pack(side="left")
        self.stop_button = ttk.Button(
            actions,
            text="停止挂机",
            command=lambda: self.run_action("stop", "停止"),
        )
        self.stop_button.pack(side="left", padx=8)
        ttk.Button(actions, text="刷新", command=self.refresh_status).pack(side="left")
        ttk.Button(actions, text="打开日志文件", command=self.open_log).pack(
            side="left", padx=(8, 0)
        )
        self.targets_button = ttk.Button(
            actions,
            text="管理回复名单",
            command=self.open_target_rules,
        )
        self.targets_button.pack(side="left", padx=(8, 0))
        self.settings_button = ttk.Button(
            actions,
            text="账号与装置设定",
            command=self.open_portable_settings,
        )
        self.settings_button.pack(side="left", padx=(8, 0))

        content = ttk.Frame(outer)
        content.pack(fill="both", expand=True)
        left = ttk.Frame(content)
        left.pack(side="left", fill="y")
        right = ttk.Frame(content)
        right.pack(side="left", fill="both", expand=True, padx=(18, 0))

        card = ttk.Frame(left, style="Card.TFrame", padding=18, width=400)
        card.pack(fill="x")
        header = ttk.Frame(card, style="Card.TFrame")
        header.pack(fill="x", pady=(0, 14))
        self.indicator = tk.Canvas(header, width=20, height=20, bg="white", highlightthickness=0)
        self.indicator.pack(side="left")
        self.dot = self.indicator.create_oval(3, 3, 17, 17, fill="#9aa5b1", outline="")
        self.health_var = tk.StringVar(value="检查中…")
        ttk.Label(header, textvariable=self.health_var, style="Value.TLabel").pack(
            side="left", padx=(8, 0)
        )

        details = ttk.Frame(card, style="Card.TFrame")
        details.pack(fill="x")
        self.values: dict[str, tk.StringVar] = {}
        rows = [
            ("mode", "运行模式"),
            ("account", "回复账号"),
            ("target", "目标来源"),
            ("heartbeat", "最近心跳"),
            ("pending", "等待回复"),
            ("countdown", "发送倒数"),
            ("received", "最近收到"),
            ("sent", "最近发送"),
            ("error", "最近错误"),
        ]
        for index, (key, label) in enumerate(rows):
            ttk.Label(details, text=label, style="Card.TLabel").grid(
                row=index, column=0, sticky="w", pady=5
            )
            variable = tk.StringVar(value="—")
            self.values[key] = variable
            ttk.Label(
                details,
                textvariable=variable,
                style="Value.TLabel",
                wraplength=250,
            ).grid(
                row=index, column=1, sticky="w", padx=(22, 0), pady=5
            )
        details.columnconfigure(1, weight=1)

        env_values = read_env_values(self.project_root / ".env")
        delay_card = ttk.Frame(left, style="Card.TFrame", padding=14)
        delay_card.pack(fill="x", pady=(14, 0))
        ttk.Label(delay_card, text="随机回复间隔（秒）", style="Value.TLabel").grid(
            row=0, column=0, columnspan=4, sticky="w", pady=(0, 10)
        )
        ttk.Label(delay_card, text="最短", style="Card.TLabel").grid(row=1, column=0)
        self.min_delay_var = tk.StringVar(value=env_values.get("TG_MIN_DELAY_SECONDS", "30"))
        ttk.Spinbox(
            delay_card,
            from_=0,
            to=1199,
            width=7,
            textvariable=self.min_delay_var,
        ).grid(row=1, column=1, padx=(6, 16))
        ttk.Label(delay_card, text="最长", style="Card.TLabel").grid(row=1, column=2)
        self.max_delay_var = tk.StringVar(value=env_values.get("TG_MAX_DELAY_SECONDS", "300"))
        ttk.Spinbox(
            delay_card,
            from_=0,
            to=1199,
            width=7,
            textvariable=self.max_delay_var,
        ).grid(row=1, column=3, padx=(6, 0))
        self.apply_button = ttk.Button(
            delay_card,
            text="保存并套用",
            command=self.apply_delay_settings,
        )
        self.apply_button.grid(row=2, column=0, columnspan=4, sticky="ew", pady=(12, 0))
        ttk.Label(
            delay_card,
            text="有等待中的回复时会禁止重启套用。",
            style="Card.TLabel",
        ).grid(row=3, column=0, columnspan=4, sticky="w", pady=(8, 0))

        ttk.Label(right, text="即时日志（每 2 秒刷新）", style="Sub.TLabel").pack(
            anchor="w", pady=(0, 5)
        )
        self.output = tk.Text(
            right,
            wrap="word",
            font=("Consolas", 9),
            bg="#1f2933",
            fg="#e4e7eb",
            insertbackground="white",
            relief="flat",
            padx=10,
            pady=8,
            state="disabled",
        )
        self.output.pack(fill="both", expand=True)
        self._append_output(f"项目目录：{self.project_root}")

    def _append_output(self, message: str) -> None:
        stamp = datetime.now().strftime("%H:%M:%S")
        self.output.configure(state="normal")
        self.output.insert("end", f"[{stamp}] {message.strip()}\n")
        self.output.see("end")
        self.output.configure(state="disabled")

    def refresh_log(self) -> None:
        path = self.project_root / "logs" / "responder.log"
        try:
            size = path.stat().st_size
            if size < self._log_position:
                self._log_position = 0
            with path.open("rb") as handle:
                if self._log_position == 0:
                    data = handle.read()
                    lines = data.decode("utf-8", errors="replace").splitlines()[-200:]
                else:
                    handle.seek(self._log_position)
                    data = handle.read()
                    lines = data.decode("utf-8", errors="replace").splitlines()
                self._log_position = handle.tell()
            if lines:
                self.output.configure(state="normal")
                for line in lines:
                    self.output.insert("end", f"{line}\n")
                self.output.see("end")
                self.output.configure(state="disabled")
        except OSError:
            pass
        if self._log_job is not None:
            self.after_cancel(self._log_job)
        self._log_job = self.after(2000, self.refresh_log)

    def refresh_status(self) -> None:
        status = read_status(self.project_root)
        running, pid = responder_process(self.project_root)
        health = classify_health(status, process_running=running)
        color = {
            "HEALTHY": "#16a34a",
            "STARTING": "#f59e0b",
            "UNHEALTHY": "#dc2626",
            "STOPPED": "#9aa5b1",
        }[health]
        label = {
            "HEALTHY": f"运行正常 · PID {pid}",
            "STARTING": "正在启动…",
            "UNHEALTHY": "运行异常",
            "STOPPED": "未运行",
        }[health]
        self.indicator.itemconfigure(self.dot, fill=color)
        self.health_var.set(label)

        if status:
            mode = "实际发送（LIVE）" if not status.get("dry_run", True) else "演练模式（不发送）"
            age = heartbeat_age(status)
            heartbeat = _local_time(status.get("updated_at"))
            if age is not None:
                heartbeat += f" · {age:.0f} 秒前"
            self.values["mode"].set(mode)
            self.values["account"].set(f"@{status.get('account_username') or '—'}")
            target_ids = [str(item) for item in status.get("target_ids", [])]
            if target_ids:
                self.values["target"].set(f"{len(target_ids)} 组 · {', '.join(target_ids)}")
            else:
                self.values["target"].set(f"@{status.get('target_bot_username') or '—'}")
            self.values["heartbeat"].set(heartbeat)
            self.values["pending"].set(str(status.get("pending_replies", 0)))
            self.values["countdown"].set(countdown_text(status))
            self.values["received"].set(_local_time(status.get("last_received_at")))
            self.values["sent"].set(_local_time(status.get("last_reply_at")))
            self.values["error"].set(str(status.get("last_error") or "—"))
        else:
            for variable in self.values.values():
                variable.set("—")

        if not self._action_running:
            self.start_button.configure(state="disabled" if running else "normal")
            self.stop_button.configure(state="normal" if running else "disabled")
            pending_count = int(status.get("pending_replies", 0)) if status else 0
            self.apply_button.configure(state="disabled" if pending_count else "normal")
            self.targets_button.configure(state="normal")
            self.settings_button.configure(state="normal")
        if self._status_job is not None:
            self.after_cancel(self._status_job)
        self._status_job = self.after(1000, self.refresh_status)

    def open_target_rules(self) -> None:
        try:
            rules = target_rules_from_env_values(
                read_env_values(self.project_root / ".env")
            )
        except ConfigError as exc:
            messagebox.showerror("名单设定错误", str(exc), parent=self)
            return
        TargetRulesDialog(self, rules, self.apply_target_rules)

    def open_portable_settings(self) -> None:
        PortableSettingsDialog(self)

    def apply_target_rules(self, rules: tuple[TargetRule, ...]) -> bool:
        status = read_status(self.project_root)
        if status and int(status.get("pending_replies", 0)) > 0:
            messagebox.showwarning(
                "暂时无法套用",
                "目前有等待发送的回复。请等倒数完成后再修改名单。",
                parent=self,
            )
            return False
        target_ids = ",".join(str(rule.target_id) for rule in rules)
        try:
            update_env_values(
                self.project_root / ".env",
                {
                    "TG_TARGET_RULES_JSON": serialize_target_rules(rules),
                    "TG_TARGET_IDS": target_ids,
                    "TG_ALLOWED_CHAT_IDS": target_ids,
                },
            )
        except OSError as exc:
            messagebox.showerror("储存失败", str(exc), parent=self)
            return False

        keyword_count = sum(rule.mode == RULE_KEYWORD for rule in rules)
        fixed_count = len(rules) - keyword_count
        description = (
            f"名单已保存：{len(rules)} 组（关键词 {keyword_count}、固定文字 {fixed_count}）"
        )
        self._restart_for_settings(description)
        return True

    def _restart_for_settings(self, description: str) -> None:
        running, _ = responder_process(self.project_root)
        if not running:
            self._append_output(f"{description}；下次启动时生效。")
            return

        self._action_running = True
        self.start_button.configure(state="disabled")
        self.stop_button.configure(state="disabled")
        self.apply_button.configure(state="disabled")
        self.targets_button.configure(state="disabled")
        self.settings_button.configure(state="disabled")
        self._append_output(f"{description}；正在安全重启…")

        def worker() -> None:
            outputs: list[str] = []
            try:
                outputs.append(stop_worker_process(self.project_root))
                outputs.append(start_worker_process(self.project_root))
            except (OSError, RuntimeError) as exc:
                outputs.append(f"操作失败：{exc}")
            self.after(0, lambda: self._action_finished("\n".join(outputs)))

        threading.Thread(target=worker, daemon=True).start()

    def apply_delay_settings(self) -> None:
        try:
            minimum = int(self.min_delay_var.get().strip())
            maximum = int(self.max_delay_var.get().strip())
        except ValueError:
            messagebox.showerror("设定错误", "最短和最长间隔必须是整数秒。", parent=self)
            return
        if minimum < 0 or maximum > 1199 or maximum < minimum:
            messagebox.showerror(
                "设定错误",
                "间隔范围必须为 0-1199 秒，并且最短不得大于最长。",
                parent=self,
            )
            return
        status = read_status(self.project_root)
        if status and int(status.get("pending_replies", 0)) > 0:
            messagebox.showwarning(
                "暂时无法套用",
                "目前有等待发送的回复。请等倒数完成后再套用新间隔。",
                parent=self,
            )
            return

        try:
            update_env_values(
                self.project_root / ".env",
                {
                    "TG_MIN_DELAY_SECONDS": str(minimum),
                    "TG_MAX_DELAY_SECONDS": str(maximum),
                },
            )
        except OSError as exc:
            messagebox.showerror("储存失败", str(exc), parent=self)
            return

        self._restart_for_settings(f"间隔已保存：{minimum}-{maximum} 秒")

    def run_action(self, action: str, action_name: str) -> None:
        if self._action_running:
            return
        self._action_running = True
        self.start_button.configure(state="disabled")
        self.stop_button.configure(state="disabled")
        self.targets_button.configure(state="disabled")
        self.settings_button.configure(state="disabled")
        self._append_output(f"正在{action_name}后台服务…")

        def worker() -> None:
            try:
                output = (
                    start_worker_process(self.project_root)
                    if action == "start"
                    else stop_worker_process(self.project_root)
                )
            except (OSError, RuntimeError) as exc:
                output = f"操作失败：{exc}"
            self.after(0, lambda: self._action_finished(output))

        threading.Thread(target=worker, daemon=True).start()

    def _action_finished(self, output: str) -> None:
        self._action_running = False
        self._append_output(output)
        self.refresh_status()

    def open_log(self) -> None:
        path = self.project_root / "logs" / "responder.log"
        if not path.exists():
            messagebox.showinfo("尚无日志", "后台服务还没有产生日志文件。", parent=self)
            return
        os.startfile(path)  # type: ignore[attr-defined]


def main() -> None:
    if "--worker" in sys.argv:
        try:
            root_index = sys.argv.index("--project-root") + 1
            project_root = Path(sys.argv[root_index]).resolve()
        except (ValueError, IndexError):
            raise SystemExit("--worker requires --project-root") from None
        run_worker(project_root)
        return
    try:
        project_root = locate_project_root()
    except FileNotFoundError as exc:
        root = tk.Tk()
        root.withdraw()
        messagebox.showerror("无法启动", str(exc), parent=root)
        root.destroy()
        return
    app = ResponderControl(project_root)
    app.mainloop()


def run_worker(project_root: Path) -> None:
    ensure_project_layout(project_root)
    os.chdir(project_root)
    load_dotenv(project_root / ".env", override=True)
    log_path = project_root / "logs" / "responder.log"
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        handlers=[logging.FileHandler(log_path, encoding="utf-8")],
        force=True,
    )
    pid_file = project_root / ".state" / "responder.pid"
    try:
        settings = Settings.from_env(strict_allowlists=True)
        asyncio.run(run_responder(settings))
    except Exception:
        logging.exception("responder worker stopped unexpectedly")
        raise
    finally:
        try:
            if int(pid_file.read_text(encoding="ascii").strip()) == os.getpid():
                pid_file.unlink(missing_ok=True)
        except (OSError, ValueError):
            pass


if __name__ == "__main__":
    main()
