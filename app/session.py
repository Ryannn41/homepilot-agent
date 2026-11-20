"""一个 WebSocket 连接对应一个 VoiceSession：接收客户端事件和麦克风音频，编排 ASR -> Agent -> TTS。"""

import asyncio
import logging
from contextlib import suppress

from websockets.exceptions import ConnectionClosed

from app import tts
from app.agent import Agent
from app.asr import Recognizer
from app.config import SAMPLE_RATE, Settings
from app.protocol import KIND_MIC, ProtocolError, event, pack_audio, parse_event, unpack_audio

log = logging.getLogger(__name__)

TTS_CHUNK = SAMPLE_RATE * 2 // 10  # 下行每帧 100ms PCM16


class VoiceSession:
    def __init__(self, ws, settings: Settings, *, new_recognizer=Recognizer, new_agent=Agent, synthesize=tts.synthesize):
        self._ws = ws
        self._settings = settings
        self._new_recognizer = new_recognizer
        self._new_agent = new_agent
        self._synthesize = synthesize

        self._session_id: str | None = None
        self._agent: Agent | None = None
        # 当前轮次：输入阶段有 recognizer，input.end 之后由 reply 任务生成回复
        self._turn_id: str | None = None
        self._recognizer: Recognizer | None = None
        self._frames = 0
        self._next_seq = 0
        self._reply: asyncio.Task | None = None

    async def run(self) -> None:
        try:
            async for message in self._ws:
                if isinstance(message, bytes):
                    self._on_audio(message)
                else:
                    await self._on_event(message)
        except ConnectionClosed:
            pass
        finally:
            await self._end_session()

    async def _on_event(self, text: str) -> None:
        try:
            msg = parse_event(text)
        except ProtocolError as e:
            log.warning("非法事件: %s", e)
            return
        type_, turn_id = msg["type"], msg.get("turn_id")
        log.debug("收到事件 %s", msg)

        if type_ == "session.start":
            await self._end_session()
            self._session_id = msg.get("session_id")
            self._agent = self._new_agent(self._settings)
            log.info("会话开始 %s device=%s", self._session_id, msg.get("device_id"))
        elif msg.get("session_id") != self._session_id:
            log.warning("忽略非当前会话的事件 %s", type_)
        elif type_ == "input.start":
            await self._start_turn(turn_id)
        elif type_ == "input.end" and turn_id == self._turn_id and self._recognizer:
            if msg.get("frames") != self._frames:
                log.warning("turn %s 丢帧：客户端发送 %s 帧，收到 %d 帧", turn_id, msg.get("frames"), self._frames)
            recognizer, self._recognizer = self._recognizer, None
            self._reply = asyncio.create_task(self._respond(turn_id, recognizer, self._agent))
        elif type_ == "turn.cancel" and turn_id == self._turn_id:
            log.info("turn %s 被客户端取消: %s", turn_id, msg.get("reason"))
            await self._cancel_turn()
        elif type_ == "session.end":
            log.info("会话结束 %s: %s", self._session_id, msg.get("reason"))
            await self._end_session()
        else:
            log.warning("忽略事件 %s turn=%s", type_, turn_id)

    def _on_audio(self, data: bytes) -> None:
        try:
            frame = unpack_audio(data)
        except ProtocolError as e:
            log.warning("非法音频帧: %s", e)
            return
        if frame.kind != KIND_MIC or frame.turn_id != self._turn_id or self._recognizer is None:
            return  # 过期或不在输入阶段的帧
        if frame.seq != self._next_seq:
            log.warning("turn %s 音频跳号：期望 %d，收到 %d", frame.turn_id, self._next_seq, frame.seq)
        self._next_seq = frame.seq + 1
        self._frames += 1
        self._recognizer.feed(frame.pcm)

    async def _start_turn(self, turn_id: str) -> None:
        await self._cancel_turn()
        self._turn_id, self._frames, self._next_seq = turn_id, 0, 0
        try:
            self._recognizer = self._new_recognizer(self._settings)
        except Exception as e:
            log.exception("启动语音识别失败")
            await self._send(turn_id, "error", code="asr_error", message=str(e))

    async def _respond(self, turn_id: str, recognizer: Recognizer, agent: Agent) -> None:
        try:
            text = await recognizer.finish()
            log.info("用户: %s", text)
            await self._send(turn_id, "transcript", text=text, final=True)
            if text:
                async for sentence in agent.ask(text):
                    log.info("回复: %s", sentence)
                    await self._send(turn_id, "response.text", text=sentence)
                    await self._send_audio(turn_id, await self._synthesize(self._settings, sentence))
            await self._send(turn_id, "response.done")
        except Exception as e:
            log.exception("turn %s 处理失败", turn_id)
            await self._send(turn_id, "error", code="internal_error", message=str(e))

    async def _send_audio(self, turn_id: str, pcm: bytes) -> None:
        pcm = pcm[: len(pcm) // 2 * 2]
        chunks = [pcm[i : i + TTS_CHUNK] for i in range(0, len(pcm), TTS_CHUNK)]
        await self._send(turn_id, "response.audio.start", sample_rate=SAMPLE_RATE)
        for seq, chunk in enumerate(chunks):
            with suppress(ConnectionClosed):
                await self._ws.send(pack_audio(turn_id, seq, chunk))
        await self._send(turn_id, "response.audio.end", frames=len(chunks))

    async def _send(self, turn_id: str | None, type_: str, **fields) -> None:
        # 连接断开时由 run() 统一清理，这里不必报错
        with suppress(ConnectionClosed):
            await self._ws.send(event(type_, self._session_id, turn_id, **fields))

    async def _cancel_turn(self) -> None:
        if self._recognizer is not None:
            self._recognizer.close()
            self._recognizer = None
        if self._reply is not None and not self._reply.done():
            self._reply.cancel()
            await asyncio.wait([self._reply])
            if self._agent is not None:
                await self._agent.interrupt()
        self._reply = None
        self._turn_id = None

    async def _end_session(self) -> None:
        await self._cancel_turn()
        if self._agent is not None:
            agent, self._agent = self._agent, None
            try:
                await agent.close()
            except Exception:
                log.exception("关闭 Agent 失败")
        self._session_id = None
