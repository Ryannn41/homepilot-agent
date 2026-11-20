"""语音识别：DashScope Paraformer 实时识别，一轮用户输入对应一个 Recognizer。"""

import asyncio
import logging

from dashscope.audio.asr import Recognition, RecognitionCallback, RecognitionResult
from dashscope.common.error import InvalidParameter

from app.config import SAMPLE_RATE, Settings

log = logging.getLogger(__name__)


class Recognizer:
    """start 和 feed 都不阻塞（SDK 在后台线程收发），只有 finish 需要等待最终结果。"""

    def __init__(self, settings: Settings):
        self._sentences: list[str] = []
        self._error: str | None = None
        self._recognition = Recognition(
            model=settings.asr_model, callback=_Callback(self), format="pcm", sample_rate=SAMPLE_RATE
        )
        self._recognition.start()

    def feed(self, pcm: bytes) -> None:
        try:
            self._recognition.send_audio_frame(pcm)
        except InvalidParameter:
            pass  # 识别已因出错停止，错误在 finish 中抛出

    async def finish(self) -> str:
        """结束输入并返回整段识别文本。"""
        await asyncio.to_thread(self._stop)
        if self._error:
            raise RuntimeError(f"语音识别失败: {self._error}")
        return "".join(self._sentences)

    def close(self) -> None:
        """放弃本轮识别，不等待结果。"""
        asyncio.get_running_loop().run_in_executor(None, self._stop)

    def _stop(self) -> None:
        try:
            self._recognition.stop()
        except InvalidParameter:
            pass  # 已经停止


class _Callback(RecognitionCallback):
    """在 SDK 的后台线程中被调用。"""

    def __init__(self, recognizer: Recognizer):
        self._r = recognizer

    def on_event(self, result: RecognitionResult) -> None:
        sentence = result.get_sentence()
        if isinstance(sentence, dict) and RecognitionResult.is_sentence_end(sentence):
            self._r._sentences.append(sentence.get("text", ""))

    def on_error(self, result: RecognitionResult) -> None:
        self._r._error = f"{result.code} {result.message}"
        log.error("语音识别出错: %s", self._r._error)
