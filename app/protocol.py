"""HomePilot 语音 WebSocket 协议 v1（服务端侧），详见 homepilot-client/docs/protocol.md。

下行 JSON 除对话轮次外，还有不带 session_id / turn_id 的 notify.* 与 schedule.*（如 schedule.snapshot）。
"""

import json
import struct
import uuid
from dataclasses import dataclass
from typing import Any

PROTOCOL_VERSION = 1
HEADER = struct.Struct("<BBH16sI")  # version, kind, reserved, turn_id, seq
KIND_MIC = 1
KIND_TTS = 2


class ProtocolError(ValueError):
    pass


@dataclass(frozen=True)
class AudioFrame:
    kind: int
    turn_id: str  # 与 JSON 事件中的 turn_id 同格式，便于直接比较
    seq: int
    pcm: bytes


def pack_audio(turn_id: str, seq: int, pcm: bytes) -> bytes:
    """打包一帧下行 TTS 音频。"""
    if len(pcm) % 2:
        raise ValueError("PCM16 数据必须是偶数长度")
    return HEADER.pack(PROTOCOL_VERSION, KIND_TTS, 0, uuid.UUID(turn_id).bytes, seq) + pcm


def unpack_audio(data: bytes) -> AudioFrame:
    if len(data) < HEADER.size:
        raise ProtocolError(f"音频帧过短: {len(data)} 字节")
    version, kind, reserved, turn, seq = HEADER.unpack_from(data)
    if version != PROTOCOL_VERSION:
        raise ProtocolError(f"不支持的协议版本: {version}")
    if kind not in (KIND_MIC, KIND_TTS):
        raise ProtocolError(f"未知音频类型: {kind}")
    if reserved != 0:
        raise ProtocolError("保留字段必须为 0")
    pcm = data[HEADER.size :]
    if len(pcm) % 2:
        raise ProtocolError("PCM16 数据长度必须为偶数")
    return AudioFrame(kind, str(uuid.UUID(bytes=turn)), seq, pcm)


def parse_event(text: str) -> dict[str, Any]:
    try:
        msg = json.loads(text)
    except json.JSONDecodeError as e:
        raise ProtocolError(f"JSON 解析失败: {e}") from None
    if not isinstance(msg, dict) or not isinstance(msg.get("type"), str):
        raise ProtocolError("事件必须是带 type 字段的 JSON 对象")
    return msg


def event(type_: str, session_id: str | None, turn_id: str | None = None, **fields: Any) -> str:
    msg: dict[str, Any] = {"type": type_}
    if session_id is not None:
        msg["session_id"] = session_id
    if turn_id is not None:
        msg["turn_id"] = turn_id
    msg.update(fields)
    return json.dumps(msg, ensure_ascii=False)
