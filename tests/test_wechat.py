import asyncio
import json
from datetime import datetime, timedelta, timezone

import pytest

from app import wechat as wechat_module
from app.wechat import BIND_REPLY, REFRESH_REPLY, WeChat, WeChatError


async def test_run_waits_for_login_then_binds_sender(tmp_path, monkeypatch):
    monkeypatch.setattr(wechat_module, "LOGIN_CHECK_S", 0.01)
    path = tmp_path / "wechat.json"
    wechat = WeChat(path)
    posted = []

    async def fake_post(api, payload):
        posted.append((api, payload))
        if api.endswith("getupdates"):
            if len(posted) > 1:
                await asyncio.sleep(10)
            return {"get_updates_buf": "c1", "msgs": [{"message_type": 1, "from_user_id": "u1", "context_token": "t1"}]}
        return {}

    monkeypatch.setattr(wechat, "_post", fake_post)
    task = asyncio.create_task(wechat.run())
    await asyncio.sleep(0.05)
    assert posted == []
    path.write_text(json.dumps({"bot_token": "b", "base_url": "https://x"}))
    await asyncio.sleep(0.1)
    task.cancel()
    await wechat.close()

    state = json.loads(path.read_text())
    assert (state["user_id"], state["context_token"], state["cursor"]) == ("u1", "t1", "c1")
    assert state.get("context_token_at")
    sent = [p for api, p in posted if api.endswith("sendmessage")]
    assert sent[0]["msg"]["item_list"][0]["text_item"]["text"] == BIND_REPLY


async def test_send_uses_api_even_with_old_context_timestamp(tmp_path, monkeypatch):
    path = tmp_path / "wechat.json"
    old = (datetime.now(timezone.utc) - timedelta(days=2)).isoformat()
    path.write_text(
        json.dumps({"bot_token": "b", "base_url": "https://x", "user_id": "u", "context_token": "t", "context_token_at": old})
    )
    wechat = WeChat(path)
    posted = []

    async def fake_post(api, payload):
        posted.append(api)
        return {}

    monkeypatch.setattr(wechat, "_post", fake_post)
    await wechat.send("hi")
    assert len(posted) == 1
    await wechat.close()


async def test_prepare_failed_records_delivery_failure(tmp_path, monkeypatch):
    path = tmp_path / "wechat.json"
    fresh = datetime.now(timezone.utc).isoformat()
    path.write_text(
        json.dumps({"bot_token": "b", "base_url": "https://x", "user_id": "u", "context_token": "t", "context_token_at": fresh})
    )
    wechat = WeChat(path)

    async def fake_post(api, payload):
        raise WeChatError('iLink 接口错误: prepare failed')

    monkeypatch.setattr(wechat, "_post", fake_post)
    with pytest.raises(WeChatError, match="prepare failed"):
        await wechat.send("hi", failure_meta={"reminder_id": 2, "content": "开会"})
    state = json.loads(path.read_text())
    assert state["delivery_failures"][0]["reminder_id"] == 2
    await wechat.close()


def test_schedule_wechat_hint_when_unbound(tmp_path):
    path = tmp_path / "wechat.json"
    path.write_text(json.dumps({"bot_token": "b", "base_url": "https://x"}))
    wechat = WeChat(path)
    assert "发一条消息" in wechat.schedule_wechat_hint()


def test_schedule_wechat_hint_when_bound(tmp_path):
    path = tmp_path / "wechat.json"
    path.write_text(
        json.dumps({"bot_token": "b", "base_url": "https://x", "user_id": "u", "context_token": "t"})
    )
    wechat = WeChat(path)
    assert wechat.schedule_wechat_hint() == ""


async def test_inbound_after_delivery_failure_sends_refresh_reply(tmp_path, monkeypatch):
    monkeypatch.setattr(wechat_module, "LOGIN_CHECK_S", 0.01)
    path = tmp_path / "wechat.json"
    path.write_text(
        json.dumps(
            {
                "bot_token": "b",
                "base_url": "https://x",
                "user_id": "u1",
                "context_token": "t0",
                "delivery_failures": [{"at": "x", "reminder_id": 1, "content": "健身", "error": "prepare failed"}],
            }
        )
    )
    wechat = WeChat(path)
    posted = []

    async def fake_post(api, payload):
        posted.append((api, payload))
        if api.endswith("getupdates"):
            return {"get_updates_buf": "c", "msgs": [{"message_type": 1, "from_user_id": "u1", "context_token": "t1"}]}
        return {}

    monkeypatch.setattr(wechat, "_post", fake_post)
    task = asyncio.create_task(wechat.run())
    await asyncio.sleep(0.05)
    task.cancel()
    await wechat.close()
    sent = [p for api, p in posted if api.endswith("sendmessage")]
    assert sent[0]["msg"]["item_list"][0]["text_item"]["text"] == REFRESH_REPLY


async def test_fresh_inbound_does_not_auto_reply(tmp_path, monkeypatch):
    monkeypatch.setattr(wechat_module, "LOGIN_CHECK_S", 0.01)
    fresh = datetime.now(timezone.utc).isoformat()
    path = tmp_path / "wechat.json"
    path.write_text(
        json.dumps(
            {"bot_token": "b", "base_url": "https://x", "user_id": "u1", "context_token": "t0", "context_token_at": fresh}
        )
    )
    wechat = WeChat(path)
    posted = []

    async def fake_post(api, payload):
        posted.append(api)
        if api.endswith("getupdates"):
            return {"get_updates_buf": "c", "msgs": [{"message_type": 1, "from_user_id": "u1", "context_token": "t1"}]}
        return {}

    monkeypatch.setattr(wechat, "_post", fake_post)
    task = asyncio.create_task(wechat.run())
    await asyncio.sleep(0.05)
    task.cancel()
    await wechat.close()
    assert not any(api.endswith("sendmessage") for api in posted)


async def test_inbound_clears_delivery_failures(tmp_path, monkeypatch):
    monkeypatch.setattr(wechat_module, "LOGIN_CHECK_S", 0.01)
    path = tmp_path / "wechat.json"
    path.write_text(
        json.dumps(
            {
                "bot_token": "b",
                "base_url": "https://x",
                "user_id": "u1",
                "context_token": "t0",
                "delivery_failures": [{"at": "x", "reminder_id": 1, "content": "a", "error": "e"}],
            }
        )
    )
    wechat = WeChat(path)

    async def fake_post(api, payload):
        if api.endswith("getupdates"):
            return {"get_updates_buf": "c", "msgs": [{"message_type": 1, "from_user_id": "u1", "context_token": "t1"}]}
        return {}

    monkeypatch.setattr(wechat, "_post", fake_post)
    task = asyncio.create_task(wechat.run())
    await asyncio.sleep(0.05)
    task.cancel()
    await wechat.close()
    state = json.loads(path.read_text())
    assert state["delivery_failures"] == []
    assert state["context_token"] == "t1"