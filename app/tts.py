"""语音合成：DashScope CosyVoice，直接输出 16kHz 单声道 PCM16。"""

import asyncio

from dashscope.audio.tts_v2 import AudioFormat, SpeechSynthesizer

from app.config import Settings


async def synthesize(settings: Settings, text: str) -> bytes:
    def call() -> bytes:
        # SDK 的合成器实例不可复用，每句新建一个
        synthesizer = SpeechSynthesizer(
            model=settings.tts_model, voice=settings.tts_voice, format=AudioFormat.PCM_16000HZ_MONO_16BIT
        )
        audio = synthesizer.call(text)
        if not audio:
            raise RuntimeError(f"语音合成没有返回音频: {text!r}")
        return audio

    return await asyncio.to_thread(call)
