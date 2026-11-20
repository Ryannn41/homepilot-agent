import asyncio
import json

from app import wechat as wechat_module
from app.wechat import BIND_REPLY, WeChat


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
    sent = [p for api, p in posted if api.endswith("sendmessage")]
    assert sent[0]["msg"]["item_list"][0]["text_item"]["text"] == BIND_REPLY
