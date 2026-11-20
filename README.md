# homepilot-agent
The cloud backend for HomePilot, providing AI agent orchestration, real-time voice processing, personal reminders, device management, and multi-channel notifications.

HomePilot Agent 是 [homepilot-client](../homepilot-client) 的云端服务：接收客户端上传的语音，完成识别、对话和语音合成，再把回复语音发回客户端。通信协议见 [homepilot-client/docs/protocol.md](../homepilot-client/docs/protocol.md)。

> 当前进度：语音对话闭环 + 基础工具（当前时间、天气、联网搜索）+ 定时提醒（音箱播报，离线时发微信）。设备控制尚未实现。

## 工作方式

```
麦克风音频 -> ASR（DashScope Paraformer 实时识别）
          -> Agent（Claude Agent SDK，模型为 DeepSeek）
          -> 按句 TTS（DashScope CosyVoice）-> 回复语音
```

| 文件 | 职责 |
| --- | --- |
| `app/main.py` | 启动 WebSocket 服务，校验路径 `/ws/voice/` 和 Bearer 令牌 |
| `app/session.py` | 每个连接一个 `VoiceSession`：处理协议事件，管理当前轮次，编排识别、对话和合成 |
| `app/asr.py` | `Recognizer`：一轮输入对应一次实时识别 |
| `app/agent.py` | `Agent`：一个会话对应一个 `ClaudeSDKClient`，多轮追问共享上下文；回复流式按句切分 |
| `app/tools.py` | Agent 可调用的工具 |
| `app/reminders.py` | 定时提醒：SQLite 存储和到点检查 |
| `app/wechat.py` | 微信通知：扫码登录、记录联系人、发消息 |
| `app/tts.py` | `synthesize()`：把一句话流式合成为 16kHz 单声道 PCM16 |
| `app/protocol.py` | 二进制音频帧和 JSON 事件的编解码 |
| `app/config.py` | 配置项 |

一轮对话的过程：收到 `input.end` 后取识别结果，发送 `transcript`；Agent 每生成一句，就先发 `response.text`，再流式合成语音，以 `response.audio.start`、若干 100ms 音频帧、`response.audio.end` 的形式边合成边发出；全部结束后发送 `response.done`。流式合成约 0.6 秒出第一块音频，整句合成需要约 1.8 秒。出错时改发 `error`。收到 `turn.cancel` 或新的 `input.start` 时，立即停止当前轮的生成和发送。

DeepSeek 通过它的 Anthropic 兼容接口（`https://api.deepseek.com/anthropic`）接入 Claude Agent SDK。Claude Code 的内置工具（Bash、文件读写等）全部关闭，也不读取本机的 `~/.claude` 设置；CLI 在临时目录 `homepilot-agent` 中运行，因为它会把工作目录、git 等环境信息注入给模型，DeepSeek 看到后容易凭空输出 bash 调用。

## 工具

工具在 `app/tools.py` 中用 SDK 的 `@tool` 定义，以进程内 MCP 服务的形式挂到 Agent 上，模型按需调用，无需确认。

| 工具 | 实现 | 说明 |
| --- | --- | --- |
| `get_current_time` | 本地计算 | 日期、星期、时间，时区为 `HOMEPILOT_TIMEZONE` |
| `get_weather` | [Open-Meteo](https://open-meteo.com/)，免费、无需 key | 实时天气和三天预报；没说地点时查 `HOMEPILOT_HOME_CITY`。只支持城市名，不支持区县名 |
| `web_search` | 通义千问联网搜索（`enable_search`），复用 DashScope key | 返回整理好的事实要点，约 3~5 秒 |
| `create_reminder` / `list_reminders` / `delete_reminder` | `app/reminders.py` | 定时提醒，见下节 |

搜索较慢，系统提示词要求模型在搜索前先说一句「我查一下」，这句话会立即合成播放。工具出错时把原因作为结果返回给模型，由模型告诉用户。

新增工具：在 `build_tools()` 中用 `@tool` 定义函数，加入 `create_sdk_mcp_server` 的 `tools` 列表即可（整个 `home` 服务已在 `ALLOWED_TOOLS` 中放行）。

## 定时提醒

对音箱说「每天早上 9 点提醒我开会」，模型调用 `create_reminder`（内容、时间、重复方式：一次 / 每天 / 工作日 / 每周）。提醒按设备存放在 `data/reminders.db`，设备 ID 取自客户端连接时的 `X-Device-Id` 请求头；每台设备只能查询和删除自己的提醒。

服务端每 10 秒检查一次到点的提醒：

到点后音箱和微信同时提醒，任一渠道失败不影响另一个：

- 音箱（设备在线时）：合成「提醒你：开会」，以 `notify.start`、音频帧、`notify.end` 推送给客户端（协议见客户端 `docs/protocol.md`）。客户端如果正在对话，会等当前回复播完再播；播完进入追问状态，用户可以直接回应，Agent 知道刚才播报了什么。
- 微信：发给已绑定的联系人。
- 服务停机期间错过的提醒，超过 30 分钟就不再补发；重复提醒顺延到下一次。

### 绑定微信

微信通知使用腾讯 iLink Bot 接口，需要一次性登录和绑定：

```bash
python -m app.wechat login   # 终端显示二维码，用微信扫码确认
python -m app.main           # 启动服务
```

服务运行时，用你的微信给这个 bot 发任意一条消息，收到「已绑定 HomePilot」即完成绑定；之后提醒会同时发到这个微信。登录信息保存在 `data/wechat.json`（已加入 `.gitignore`）。未登录时服务照常运行，提醒只在音箱播报；服务每 10 秒检查一次登录状态，扫码登录后不用重启。

## 环境

- Linux（开发环境为 WSL2 Ubuntu）
- Python 3.11（conda）
- `claude-agent-sdk` 的 wheel 已内置 Claude Code CLI，无需另装 Node.js

```bash
conda create -y -n homepilot_agent python=3.11
conda activate homepilot_agent
pip install -r requirements.txt
```

默认镜像返回 403 时，可加 `-i https://mirrors.aliyun.com/pypi/simple/`。

## 配置

```bash
cp .env.example .env   # 填入两个 API key
```

| 环境变量 | 默认值 | 说明 |
| --- | --- | --- |
| `HOMEPILOT_HOST` | `0.0.0.0` | 监听地址 |
| `HOMEPILOT_PORT` | `8000` | 监听端口 |
| `HOMEPILOT_AUTH_TOKEN` | 空 | 客户端令牌，与客户端的 `HOMEPILOT_AUTH_TOKEN` 一致；留空不校验 |
| `HOMEPILOT_DASHSCOPE_API_KEY` | 空 | 阿里云百炼 API key（ASR 和 TTS） |
| `HOMEPILOT_ASR_MODEL` | `paraformer-realtime-v2` | 实时识别模型 |
| `HOMEPILOT_TTS_MODEL` | `cosyvoice-v2` | 语音合成模型 |
| `HOMEPILOT_TTS_VOICE` | `longxiaochun_v2` | 音色，需与模型匹配 |
| `HOMEPILOT_DEEPSEEK_API_KEY` | 空 | DeepSeek API key |
| `HOMEPILOT_DEEPSEEK_BASE_URL` | `https://api.deepseek.com/anthropic` | Anthropic 兼容接口地址 |
| `HOMEPILOT_DEEPSEEK_MODEL` | `deepseek-chat` | 模型名 |
| `HOMEPILOT_SYSTEM_PROMPT` | 见 `app/config.py` | 助手设定：简短口语，不用 Markdown，按需调用工具 |
| `HOMEPILOT_TIMEZONE` | `Asia/Shanghai` | 当前时间工具使用的时区 |
| `HOMEPILOT_HOME_CITY` | `上海` | 没说地点时查询天气的城市 |
| `HOMEPILOT_SEARCH_MODEL` | `qwen-flash` | 联网搜索用的通义千问模型；`qwen-plus` 更详细但慢一倍 |
| `HOMEPILOT_DATA_DIR` | `data` | 提醒数据库和微信登录信息的存放目录 |
| `HOMEPILOT_LOG_LEVEL` | `INFO` | `DEBUG` 时打印收到的每个事件 |

优先级：系统环境变量 > `.env` > 默认值。

## 启动

```bash
python -m app.main
```

Ctrl+C 退出。日志中会打印每一轮的识别文本和逐句回复。

每个语音会话会启动一个 Claude Code CLI 子进程，耗时约 1 秒。这一步在收到 `session.start` 后于后台进行，和用户开口说话的时间重叠，一般感觉不到。

## 与客户端联调

客户端默认连接 `ws://127.0.0.1:8000/ws/voice/`。服务端在 WSL 中运行时，Windows 上的客户端可以直接用这个地址；部署到云端后，把客户端的 `HOMEPILOT_SERVER_URL` 改成 `ws://<host>:8000/ws/voice/`（生产环境建议在前面加 TLS 反向代理，改用 `wss://`）。

```powershell
# Windows，homepilot-client 目录
python -m app.main --ws-test 3   # 不用唤醒词，连续说 3 句
python -m app.main               # 完整流程：说「你好小余」唤醒，可以连续追问
```

## 测试

```bash
python -m pytest -v
```

单元测试用假的 ASR、Agent 和 TTS 驱动完整时序，不需要 API key，也不访问网络。
