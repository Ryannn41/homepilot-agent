import json
import uuid

import pytest

from app.protocol import HEADER, KIND_MIC, KIND_TTS, ProtocolError, event, pack_audio, parse_event, unpack_audio

TURN = str(uuid.uuid4())


def mic_frame(seq: int = 0, pcm: bytes = b"\x01\x00" * 4, **override) -> bytes:
    fields = {"version": 1, "kind": KIND_MIC, "reserved": 0} | override
    return HEADER.pack(fields["version"], fields["kind"], fields["reserved"], uuid.UUID(TURN).bytes, seq) + pcm


def test_pack_audio_roundtrip():
    frame = unpack_audio(pack_audio(TURN, 7, b"\x00\x01" * 10))
    assert (frame.kind, frame.turn_id, frame.seq, frame.pcm) == (KIND_TTS, TURN, 7, b"\x00\x01" * 10)


def test_pack_audio_header_layout():
    data = pack_audio(TURN, 1, b"")
    assert data[:4] == bytes([1, KIND_TTS, 0, 0])
    assert data[4:20] == uuid.UUID(TURN).bytes
    assert data[20:24] == (1).to_bytes(4, "little")


def test_pack_audio_rejects_odd_pcm():
    with pytest.raises(ValueError):
        pack_audio(TURN, 0, b"\x00")


def test_unpack_mic_frame():
    frame = unpack_audio(mic_frame(seq=3))
    assert (frame.kind, frame.turn_id, frame.seq) == (KIND_MIC, TURN, 3)


@pytest.mark.parametrize(
    "data",
    [
        b"\x01" * 10,
        mic_frame(version=2),
        mic_frame(kind=9),
        mic_frame(reserved=1),
        mic_frame(pcm=b"\x00"),
    ],
)
def test_unpack_rejects_invalid(data):
    with pytest.raises(ProtocolError):
        unpack_audio(data)


def test_parse_event():
    assert parse_event('{"type": "session.start", "session_id": "s"}')["type"] == "session.start"
    for bad in ("not json", "[]", '{"no_type": 1}'):
        with pytest.raises(ProtocolError):
            parse_event(bad)


def test_event_omits_missing_ids():
    assert json.loads(event("error", None, code="x", message="y")) == {"type": "error", "code": "x", "message": "y"}
    assert json.loads(event("response.done", "s", "t")) == {"type": "response.done", "session_id": "s", "turn_id": "t"}
