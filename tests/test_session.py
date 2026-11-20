import asyncio
import json
import uuid

from app.config import Settings
from app.protocol import HEADER, KIND_MIC, KIND_TTS, unpack_audio
from app.session import TTS_CHUNK, VoiceSession

SESSION = str(uuid.uuid4())


class FakeWebSocket:
    def __init__(self):
        self.incoming: asyncio.Queue = asyncio.Queue()
        self.sent: list[str | bytes] = []

    def __aiter__(self):
        return self

    async def __anext__(self):
        message = await self.incoming.get()
        if message is None:
            raise StopAsyncIteration
        return message

    async def send(self, message):
        self.sent.append(message)

    def events(self) -> list[dict]:
        return [json.loads(m) for m in self.sent if isinstance(m, str)]

    def types(self) -> list[str]:
        return [e["type"] for e in self.events()]


class FakeRecognizer:
    text = "今天天气怎么样"

    def __init__(self, settings):
        self.pcm = b""
        self.closed = False

    def feed(self, pcm):
        self.pcm += pcm

    async def finish(self):
        return self.text if self.pcm else ""

    def close(self):
        self.closed = True


class FakeAgent:
    instances: list["FakeAgent"] = []

    def __init__(self, context, sentences=("晴天。", "二十度。"), delay=0.0, error=None):
        self.context, self.sentences, self.delay, self.error = context, sentences, delay, error
        self.asked: list[str] = []
        self.interrupted = self.closed = False
        FakeAgent.instances.append(self)

    async def ask(self, text):
        self.asked.append(text)
        for sentence in self.sentences:
            await asyncio.sleep(self.delay)
            yield sentence
        if self.error:
            raise self.error

    async def interrupt(self):
        self.interrupted = True

    async def close(self):
        self.closed = True


async def fake_tts(settings, text):
    # 共 TTS_CHUNK + 200 字节，块长不规则且有奇数：应重新切成一整帧 + 200 字节
    for size in (1001, TTS_CHUNK - 1001 - 1, 1, 200):
        yield b"\x01" * size


def mic(turn_id: str, seq: int) -> bytes:
    return HEADER.pack(1, KIND_MIC, 0, uuid.UUID(turn_id).bytes, seq) + b"\x00\x00" * 320


def ev(type_: str, **fields) -> str:
    return json.dumps({"type": type_, "session_id": SESSION, **fields})


def new_session(agent_factory=FakeAgent):
    ws = FakeWebSocket()
    session = VoiceSession(ws, Settings(), agent_factory, new_recognizer=FakeRecognizer, synthesize=fake_tts)
    return ws, session


async def start(agent_factory=FakeAgent):
    ws, session = new_session(agent_factory)
    task = asyncio.create_task(session.run())
    await ws.incoming.put(ev("session.start", device_id="dev", client_version="test"))
    return ws, task


async def speak(ws, turn_id: str, frames: int = 3):
    await ws.incoming.put(ev("input.start", turn_id=turn_id, sample_rate=16000, channels=1, format="pcm16"))
    for seq in range(frames):
        await ws.incoming.put(mic(turn_id, seq))
    await ws.incoming.put(ev("input.end", turn_id=turn_id, frames=frames, truncated=False))


async def wait_for(ws, type_: str, timeout: float = 1.0):
    async with asyncio.timeout(timeout):
        while type_ not in ws.types():
            await asyncio.sleep(0.005)


async def close(ws, task):
    await ws.incoming.put(None)
    await task


async def test_full_turn():
    ws, task = await start()
    turn = str(uuid.uuid4())
    await speak(ws, turn)
    await wait_for(ws, "response.done")
    await close(ws, task)

    assert ws.types() == [
        "transcript",
        "response.text", "response.audio.start", "response.audio.end",
        "response.text", "response.audio.start", "response.audio.end",
        "response.done",
    ]  # fmt: skip
    events = ws.events()
    assert all(e["session_id"] == SESSION and e["turn_id"] == turn for e in events)
    assert events[0] == {**events[0], "text": FakeRecognizer.text, "final": True}
    assert [e["text"] for e in events if e["type"] == "response.text"] == ["晴天。", "二十度。"]
    assert events[2]["sample_rate"] == 16000 and events[3]["frames"] == 2

    frames = [unpack_audio(m) for m in ws.sent if isinstance(m, bytes)]
    assert [(f.kind, f.turn_id, f.seq) for f in frames] == [(KIND_TTS, turn, s) for s in (0, 1, 0, 1)]
    assert [len(f.pcm) for f in frames[:2]] == [TTS_CHUNK, 200]

    agent = FakeAgent.instances[-1]
    assert agent.asked == [FakeRecognizer.text] and agent.closed


async def test_empty_transcript_skips_agent():
    ws, task = await start()
    await speak(ws, str(uuid.uuid4()), frames=0)
    await wait_for(ws, "response.done")
    await close(ws, task)
    assert ws.types() == ["transcript", "response.done"]
    assert FakeAgent.instances[-1].asked == []


async def test_agent_error_sends_error():
    ws, task = await start(lambda s: FakeAgent(s, sentences=(), error=RuntimeError("boom")))
    await speak(ws, str(uuid.uuid4()))
    await wait_for(ws, "error")
    await close(ws, task)
    assert ws.types() == ["transcript", "error"]
    assert ws.events()[-1]["message"] == "boom"


async def test_turn_cancel_stops_reply():
    ws, task = await start(lambda s: FakeAgent(s, delay=10))
    turn = str(uuid.uuid4())
    await speak(ws, turn)
    await wait_for(ws, "transcript")
    await ws.incoming.put(ev("turn.cancel", turn_id=turn, reason="timeout"))
    await asyncio.sleep(0.05)
    await close(ws, task)
    assert ws.types() == ["transcript"]
    assert FakeAgent.instances[-1].interrupted


async def test_new_input_replaces_old_turn_and_drops_stale_frames():
    ws, task = await start(lambda s: FakeAgent(s, delay=0.05))
    old, new = str(uuid.uuid4()), str(uuid.uuid4())
    await speak(ws, old)
    await wait_for(ws, "transcript")
    await speak(ws, new)
    await ws.incoming.put(mic(old, 3))  # 旧轮次的迟到帧
    await wait_for(ws, "response.done")
    await close(ws, task)
    assert {e["turn_id"] for e in ws.events() if e["type"] != "transcript"} == {new}


async def test_notify_outside_session():
    ws, session = new_session()
    await session.notify("提醒你：开会")
    start, end = ws.events()
    assert start == {"type": "notify.start", "notify_id": start["notify_id"], "text": "提醒你：开会", "sample_rate": 16000}
    assert end == {"type": "notify.end", "notify_id": start["notify_id"], "frames": 2}
    frames = [unpack_audio(m) for m in ws.sent if isinstance(m, bytes)]
    assert [(f.kind, f.turn_id, f.seq) for f in frames] == [(KIND_TTS, start["notify_id"], 0), (KIND_TTS, start["notify_id"], 1)]


async def test_session_after_notify_gets_notice_context():
    ws, session = new_session()
    await session.notify("提醒你：开会")
    task = asyncio.create_task(session.run())
    await ws.incoming.put(ev("session.start"))
    await close(ws, task)
    assert "提醒你：开会" in FakeAgent.instances[-1].context


async def test_notify_during_session_is_told_to_agent_once():
    ws, session = new_session()
    task = asyncio.create_task(session.run())
    await ws.incoming.put(ev("session.start"))
    await asyncio.sleep(0.02)
    await session.notify("提醒你：开会")
    for _ in range(2):
        await speak(ws, str(uuid.uuid4()))
        await asyncio.sleep(0.05)
    await close(ws, task)
    first, second = FakeAgent.instances[-1].asked
    assert first == "（刚才你主动播报了定时提醒：提醒你：开会）今天天气怎么样"
    assert second == "今天天气怎么样"


async def test_events_from_other_session_ignored():
    ws, task = await start()
    await ws.incoming.put(json.dumps({"type": "input.start", "session_id": "other", "turn_id": str(uuid.uuid4())}))
    await ws.incoming.put("not json")
    await ws.incoming.put(b"\x00" * 3)
    await asyncio.sleep(0.02)
    await close(ws, task)
    assert ws.sent == []
