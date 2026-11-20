"""语音合成：DashScope CosyVoice，流式输出 16kHz 单声道 PCM16。"""

import asyncio
from collections.abc import AsyncIterator

from dashscope.audio.tts_v2 import AudioFormat, ResultCallback, SpeechSynthesizer

from app.config import Settings

CHUNK_TIMEOUT_S = 10  # 这么久没收到新音频就认为合成卡住了


async def synthesize(settings: Settings, text: str) -> AsyncIterator[bytes]:
    """边合成边产出音频块，块的长度不固定。"""
    loop = asyncio.get_running_loop()
    events: asyncio.Queue[bytes | Exception | None] = asyncio.Queue()

    def put(item: bytes | Exception | None) -> None:
        loop.call_soon_threadsafe(events.put_nowait, item)

    class Callback(ResultCallback):  # 在 SDK 的线程中调用
        def on_data(self, data: bytes) -> None:
            put(data)

        def on_complete(self) -> None:
            put(None)

        def on_error(self, message) -> None:
            put(RuntimeError(f"语音合成失败: {message}"))

    # 设置了回调时，call 只负责建连和提交文本，音频、结束和错误都经回调交付；SDK 的合成器实例不可复用
    synthesizer = SpeechSynthesizer(
        model=settings.tts_model,
        voice=settings.tts_voice,
        format=AudioFormat.PCM_16000HZ_MONO_16BIT,
        callback=Callback(),
    )
    await asyncio.to_thread(synthesizer.call, text)
    while (item := await asyncio.wait_for(events.get(), CHUNK_TIMEOUT_S)) is not None:
        if isinstance(item, Exception):
            raise item
        yield item
