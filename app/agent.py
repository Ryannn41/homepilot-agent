"""对话 Agent：Claude Agent SDK 经 DeepSeek 的 Anthropic 兼容接口生成回复，可调用 app/tools.py 中的工具。
一个语音会话对应一个 Agent。"""

import asyncio
import re
import tempfile
from collections.abc import AsyncIterator
from pathlib import Path

from claude_agent_sdk import ClaudeAgentOptions, ClaudeSDKClient, ResultMessage, StreamEvent
from claude_agent_sdk.types import McpSdkServerConfig

from app.config import Settings
from app.tools import ALLOWED_TOOLS, SERVER_NAME

# 句末标点（连续的算一个，紧跟的右引号、右括号也归入本句）；按句切分后可以边生成边合成语音
_SENTENCE = re.compile(r"[^。！？；!?;\n]*[。！？；!?;\n]+[”’」』）)\"']*")


def split_sentences(text: str) -> tuple[list[str], str]:
    """把 text 切成完整句子和剩余的未完成部分。只有标点的片段会被丢弃，TTS 无法合成它们。"""
    sentences, end = [], 0
    for m in _SENTENCE.finditer(text):
        if _speakable(sentence := m.group().strip()):
            sentences.append(sentence)
        end = m.end()
    return sentences, text[end:]


def _speakable(text: str) -> bool:
    return re.search(r"\w", text) is not None


def _options(settings: Settings, tools: McpSdkServerConfig, context: str) -> ClaudeAgentOptions:
    model = settings.deepseek_model
    # CLI 会把工作目录、git、Shell 等环境信息注入给模型，DeepSeek 看到后会凭空输出 bash 调用；
    # 放在空目录里运行，避免暴露项目信息
    workdir = Path(tempfile.gettempdir()) / "homepilot-agent"
    workdir.mkdir(exist_ok=True)
    return ClaudeAgentOptions(
        system_prompt=settings.system_prompt + context,
        model=model,
        cwd=workdir,
        tools=[],  # 关闭 Claude Code 内置工具（Bash、Read 等）
        mcp_servers={SERVER_NAME: tools},
        allowed_tools=ALLOWED_TOOLS,
        setting_sources=[],  # 不读取 ~/.claude 等本机设置，避免覆盖下面的接口配置
        include_partial_messages=True,
        env={
            "ANTHROPIC_BASE_URL": settings.deepseek_base_url,
            "ANTHROPIC_AUTH_TOKEN": settings.deepseek_api_key.get_secret_value(),
            "ANTHROPIC_MODEL": model,
            "ANTHROPIC_SMALL_FAST_MODEL": model,
            "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1",
        },
    )


class Agent:
    """同一个会话内多轮对话共享上下文。"""

    def __init__(self, settings: Settings, tools: McpSdkServerConfig, context: str = ""):
        """tools：app.tools.build_tools() 构造的工具；context：追加到系统提示词的会话背景。"""
        self._client = ClaudeSDKClient(_options(settings, tools, context))
        # 启动 CLI 子进程约需 1 秒，在后台进行，不阻塞音频接收
        self._connecting = asyncio.create_task(self._client.connect())
        self._unfinished = False  # 上一轮回复被取消时还没读完

    async def ask(self, text: str) -> AsyncIterator[str]:
        """发送用户输入，逐句产出回复。"""
        await self._connecting
        if self._unfinished:
            async for _ in self._client.receive_response():
                pass
        await self._client.query(text)
        self._unfinished = True
        buffer = ""
        async for msg in self._client.receive_response():
            if isinstance(msg, StreamEvent):
                buffer += _text_delta(msg.event)
                sentences, buffer = split_sentences(buffer)
                for sentence in sentences:
                    yield sentence
            elif isinstance(msg, ResultMessage):
                self._unfinished = False
                if msg.is_error:
                    raise RuntimeError(f"模型调用失败: {msg.errors or msg.result or msg.subtype}")
        if _speakable(buffer):
            yield buffer.strip()

    async def interrupt(self) -> None:
        """停止正在生成的回复，下一次 ask 前会丢弃它的剩余输出。"""
        if self._unfinished:
            await self._client.interrupt()

    async def close(self) -> None:
        try:
            await self._connecting
        finally:
            await self._client.disconnect()


def _text_delta(event: dict) -> str:
    if event.get("type") == "content_block_delta" and event["delta"].get("type") == "text_delta":
        return event["delta"]["text"]
    return ""
