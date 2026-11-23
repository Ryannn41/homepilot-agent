import asyncio
import json
from datetime import datetime, timedelta, timezone

import pytest

from app import wechat as wechat_module
from app.wechat import BIND_REPLY, WeChat, WeChatError


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
    assert posted == []  # 未登录时不请求
    path.write_text(json.dumps({"bot_token": "b", "base_url": "https://x"}))
    await asyncio.sleep(0.1)
    task.cancel()
    await wechat.close()

    state = json.loads(path.read_text())
    assert (state["user_id"], state["context_token"], state["cursor"]) == ("u1", "t1", "c1")
    assert state.get("context_token_at")
    sent = [p for api, p in posted if api.endswith("sendmessage")]
    assert sent[0]["msg"]["item_list"][0]["text_item"]["text"] == BIND_REPLY


async def test_send_blocks_stale_context_without_api_call(tmp_path, monkeypatch):
    path = tmp_path / "wechat.json"
    old = (datetime.now(timezone.utc) - timedelta(hours=7)).isoformat()
    path.write_text(
        json.dumps({"bot_token": "b", "base_url": "https://x", "user_id": "u", "context_token": "t", "context_token_at": old})
    )
    wechat = WeChat(path, context_max_age=timedelta(hours=6))
    posted = []

    async def fake_post(api, payload):
        posted.append(api)
        return {}

    monkeypatch.setattr(wechat, "_post", fake_post)
    with pytest.raises(WeChatError, match="过期"):
        await wechat.send("hi", failure_meta={"reminder_id": 1, "content": "健身"})
    assert posted == []
    state = json.loads(path.read_text())
    assert len(state["delivery_failures"]) == 1
    assert state["delivery_failures"][0]["content"] == "健身"
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


def test_schedule_wechat_hint_when_stale(tmp_path):
    path = tmp_path / "wechat.json"
    old = (datetime.now(timezone.utc) - timedelta(hours=7)).isoformat()
    path.write_text(
        json.dumps({"bot_token": "b", "base_url": "https://x", "user_id": "u", "context_token": "t", "context_token_at": old})
    )
    wechat = WeChat(path, context_max_age=timedelta(hours=6))
    assert "很久没在微信" in wechat.schedule_wechat_hint()
    assert wechat.schedule_wechat_hint().startswith("。注意：")


def test_schedule_wechat_hint_fresh(tmp_path):
    path = tmp_path / "wechat.json"
    fresh = datetime.now(timezone.utc).isoformat()
    path.write_text(
        json.dumps({"bot_token": "b", "base_url": "https://x", "user_id": "u", "context_token": "t", "context_token_at": fresh})
    )
    wechat = WeChat(path)
    assert wechat.schedule_wechat_hint() == ""


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