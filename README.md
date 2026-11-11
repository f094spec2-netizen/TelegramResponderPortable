# Telegram Test Responder

可直接下载 [release/TelegramResponderPortable.zip](release/TelegramResponderPortable.zip)，
解压缩整个资料夹后运行 `TelegramResponder.exe`。首次启动请在设定页填写自己的
Telegram API ID/hash 并登入测试账号；发布包不包含任何账号 session 或密钥。

用于隔离测试环境的 Telegram MTProto 测试客户端。它以专用测试用户账号登录，
只监听白名单中的目标 Telegram ID，并在随机延迟后按每组 ID 的独立规则回复。

> 不要用于真实员工账号、考勤规避或生产 HR 数据。Telegram Bot API 看不到其他
> bot 发出的消息，所以这里必须使用专用测试用户账号和 MTProto 客户端。

## 安全边界

- `run` 必须配置测试账号 ID 和至少一组目标 Telegram 数字 ID。
- 每组目标都同时检查 sender ID 与 chat ID，未知来源不会进入回复队列。
- 默认 `TG_DRY_RUN=true`，不会实际发送。
- 可用 `TG_REQUIRED_PREFIX=[LOADTEST]` 将处理范围限制为测试消息。
- 「关键词解析」只处理包含“只回复下面的关键词”格式的新文字；「固定文字」会对
  该 ID 发来的新文字回复自订内容。两者都忽略转发、空消息、编辑事件及非白名单来源。
- `TG_MAX_PENDING_REPLIES` 限制等待发送的任务数，防止测试失控。

## 安装

需要 Python 3.11 或更高版本。先在 Telegram 的
[API development tools](https://my.telegram.org/apps) 为专用测试账号创建 API ID/hash。

```powershell
cd 'C:\ai project\telegram_test_responder'
py -3.11 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -e '.[dev]'
Copy-Item .env.example .env
```

编辑 `.env`，先只填写 `TG_API_ID`、`TG_API_HASH`、session 路径和目标 username。
不要提交 `.env` 或 `.session` 文件。

## 首次登录和确认 ID

```powershell
telegram-test-responder inspect
```

首次执行会在终端要求测试手机号、登录验证码，以及账号启用两步验证时的密码。
命令只打印身份信息，不发送消息。把输出的账号 ID、bot ID 和 chat ID 复制到
`.env` 对应白名单中。`TG_TEST_ACCOUNT_USERNAME` 与 `TG_TEST_ACCOUNT_ID` 会同时校验，
因此电脑上登录多个 Telegram 账号也不会选错测试端点。

## 本地策略模拟

保留 `TG_DRY_RUN=true`，确认文字过滤和延迟范围：

```powershell
telegram-test-responder simulate '[LOADTEST] ping-001' --seed 7
```

## 联机 dry-run

```powershell
telegram-test-responder run
```

让目标 bot 向测试账号发送一条文字。日志应显示消息被安排、随机延迟和 `dry-run`
结果，但 Telegram 中不会出现回复。若配置了 `TG_REQUIRED_PREFIX=[LOADTEST]`，测试
消息也必须以该前缀开头。

## 允许实际发送

确认测试账号、目标 bot、chat 和测试前缀全部正确后，将 `.env` 中：

```dotenv
TG_DRY_RUN=false
```

然后重新运行 `telegram-test-responder run`。默认延迟范围是 30–300 秒，发送内容
依该 ID 的关键词解析或固定文字规则产生，并使用 Telegram 的“回复”关系关联原消息。

停止工具可按 `Ctrl+C`。尚未发送的等待任务会被取消。

完成首次登录和 dry-run 后，可在后台运行，不必一直开着终端：

```powershell
.\scripts\start-responder.ps1 -Background
```

运行日志写入 `logs\responder.log`。需要停止时：

```powershell
.\scripts\stop-responder.ps1
```

随时检查进程、Telegram 连接、心跳、等待队列及最近收发时间：

```powershell
.\scripts\status-responder.ps1
```

正常状态应显示 `Health: HEALTHY`、`Process: running`、
`Telegram connected: True`，且 `Last heartbeat` 的秒数持续保持在约一分钟以内。
查看最近 20 行日志：

```powershell
.\scripts\status-responder.ps1 -Tail 20
```

状态文件不会记录消息正文。实际发送模式下，可让目标 bot 发一条测试文字；随后
`Last received` 会先更新，随机等待结束后 `Last sent` 才会更新。若仍为 dry-run，
状态会明确显示 `DRY-RUN (no messages sent)`，不会误以为已经真实回复。

`TG_DEVICE_MODEL` 控制 Telegram“登入装置”中显示的设备名称；修改后重启后台
服务即可重新上报，不会改变 API ID 或账号授权。

电脑重启后需要重新执行一次后台启动命令；本项目不会擅自注册系统自启动任务。

## 简易 Windows 控制视窗

完整可携版位于 `dist\TelegramResponderPortable.zip`。解压缩后双击
`TelegramResponderPortable\TelegramResponder.exe` 即可使用；目标 Windows 10/11
电脑不需要安装 Python、虚拟环境或 PowerShell 脚本。第一次启动会自动建立本机
`.env`、`.state` 与 `logs`，并开启账号设定页。

「账号与装置设定」可填写 API ID/hash、自订 Telegram 登入装置名称、登入多组
Telegram 账号并选取目前使用账号。「检测对话 ID」会读取选取账号的私聊清单，
只显示名称、username、账号类型与数字 ID，不读取消息正文，可直接加入关键词回复名单。

主视窗可查看健康状态、LIVE/DRY-RUN、心跳、
等待队列、预计发送时间、秒级倒数和最近收发时间，并可启动或停止挂机。顶部的
「管理回复名单」可新增、编辑或删除多组 Telegram 数字 ID，并为每组独立选择
「关键词解析」或「固定文字」及其回复内容。不知道数字 ID 时，可在名单视窗输入
`@username`，由运行中的后台通过现有 Telegram 授权在本机解析并填入。左侧可
设定 0–1199 秒的最短/最长随机回复间隔；队列非空时禁止重启套用。右侧每两秒刷新即时日志，记录带
时间戳的收到、安排回复、回复成功或失败事件。控制视窗不会显示 API hash、session
内容或消息正文。关闭控制视窗不会停止后台挂机。

重新打包完整可携资料夹与 ZIP：

```powershell
.\scripts\build-gui.ps1
```

## 验证

```powershell
pytest
ruff check .
```

真实 Telegram 登录和发送未包含在自动化测试内，避免 CI 或开发机意外向外部账号发信。
