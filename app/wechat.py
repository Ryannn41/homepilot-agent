"""微信通知：通过腾讯 iLink Bot API 给绑定的微信发消息，接口用法参考 wechat-bot 项目。

1. 扫码登录：python -m app.wechat login
2. 服务运行时（登录前后启动都可以），用微信给 Bot 发任意一条消息完成绑定。iLink 只允许给发过消息的人主动推送，
   所以服务运行期间会持续接收消息，记下最近一次发消息的人。
"""

import asyncio
import base64
import json
import logging
import os
import random
import sys
import time
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import httpx
import qrcode

from app.config import Settings

log = logging.getLogger(__name__)

BASE_URL = "https://ilinkai.weixin.qq.com"
BIND_REPLY = "已绑定 HomePilot。定时提醒会同时发到这里。"
REFRESH_REPLY = "微信提醒已恢复，定时消息会正常发到这儿。"
LOGIN_CHECK_S = 10
STALE_USER_HINT = "很久没在微信跟 HomePilot 说过话了，请给 Bot 发任意一条消息，微信提醒才能正常收到。"
MAX_DELIVERY_FAILURES = 10


class WeChatError(RuntimeError):
    pass


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _parse_iso(value: str) -> datetime | None:
    try:
        dt = datetime.fromisoformat(value)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def _is_stale_token_error(exc: BaseException) -> bool:
    msg = str(exc).lower()
    return "prepare failed" in msg or "会话可能已过期" in msg


class WeChat:
    def __init__(self, state_path: str | Path, *, context_max_age: timedelta | None = None):
        self._path = Path(state_path)
        self._state: dict[str, Any] = {}
        self._context_max_age = context_max_age or timedelta(hours=6)
        self._load()
        self._http = httpx.AsyncClient(timeout=httpx.Timeout(45, connect=10))

    def _load(self) -> None:
        if self._path.exists():
            self._state = json.loads(self._path.read_text("utf-8"))

    @property
    def logged_in(self) -> bool:
        return bool(self._state.get("bot_token"))

    def is_context_stale(self) -> bool:
        if not self._state.get("context_token"):
            return False
        at = self._state.get("context_token_at")
        if not at or not isinstance(at, str):
            return True
        updated = _parse_iso(at)
        if updated is None:
            return True
        return _utc_now() - updated > self._context_max_age

    def record_delivery_failure(self, *, reminder_id: int | None, content: str, error: str) -> None:
        failures = list(self._state.get("delivery_failures") or [])
        failures.append(
            {
                "at": _utc_now().isoformat(),
                "reminder_id": reminder_id,
                "content": content,
                "error": error,
            }
        )
        self._save(delivery_failures=failures[-MAX_DELIVERY_FAILURES:])
        log.warning("微信提醒投递失败已记录（共 %d 条待用户处理）", len(self._state.get("delivery_failures") or []))

    def status_payload(self) -> dict[str, Any] | None:
        failures = self._state.get("delivery_failures") or []
        stale = self.is_context_stale()
        if not failures and not stale:
            return None
        return {
            "context_stale": stale,
            "context_token_at": self._state.get("context_token_at"),
            "delivery_failures": failures,
            "hint": STALE_USER_HINT,
        }

    def schedule_wechat_hint(self) -> str:
        """创建定时提醒/简报/倒计时时，若微信可能发不出去，拼进工具返回给 Agent。"""
        if not self.logged_in or not self._state.get("context_token"):
            return ""
        if self.is_context_stale():
            return f"。注意：{STALE_USER_HINT}"
        return ""

    def session_context(self) -> str:
        payload = self.status_payload()
        if not payload:
            return ""
        parts: list[str] = []
        if payload["context_stale"]:
            parts.append(f"\n当前微信主动推送可能不可用：{STALE_USER_HINT}")
        if payload["delivery_failures"]:
            missed = "、".join(str(f["content"]) for f in payload["delivery_failures"])
            parts.append(f"\n以下定时提醒没能发到微信：{missed}。若用户问起，说明原因并提醒：{STALE_USER_HINT}")
        return "".join(parts)

    async def close(self) -> None:
        await self._http.aclose()

    async def send(self, text: str, *, failure_meta: dict[str, Any] | None = None) -> None:
        if not self._state.get("context_token"):
            err = WeChatError("还没有绑定微信：请先用微信给 Bot 发一条消息")
            if failure_meta:
                self.record_delivery_failure(error=str(err), **failure_meta)
            raise err
        if self.is_context_stale():
            err = WeChatError(f"微信会话可能已过期（{STALE_USER_HINT}）")
            if failure_meta:
                self.record_delivery_failure(error=str(err), **failure_meta)
            raise err
        try:
            await self._post(
                "/ilink/bot/sendmessage",
                {
                    "msg": {
                        "from_user_id": "",
                        "to_user_id": self._state["user_id"],
                        "client_id": f"homepilot:{int(time.time() * 1000)}:{uuid.uuid4().hex[:8]}",
                        "message_type": 2,
                        "message_state": 2,
                        "context_token": self._state["context_token"],
                        "item_list": [{"type": 1, "text_item": {"text": text}}],
                    }
                },
            )
        except WeChatError as e:
            if failure_meta and _is_stale_token_error(e):
                self.record_delivery_failure(error=str(e), **failure_meta)
            raise

    def _note_inbound_context(self, msg: dict[str, Any]) -> None:
        """用户发来微信消息时刷新 context_token，并清除已记录的投递失败。"""
        self._save(
            user_id=msg["from_user_id"],
            context_token=msg["context_token"],
            context_token_at=_utc_now().isoformat(),
            delivery_failures=[],
        )

    async def run(self) -> None:
        """长轮询接收消息，记下发消息的人作为提醒的接收者。未登录时等待 login 写入凭证，无需重启服务。"""
        if not self.logged_in:
            log.warning("微信未登录，提醒只在音箱播报。登录：python -m app.wechat login")
        while not self.logged_in:
            await asyncio.sleep(LOGIN_CHECK_S)
            self._load()
        log.info("微信已登录，%s", "提醒会同时发到微信" if self._state.get("context_token") else "请用微信给 Bot 发一条消息完成绑定")
        if self.is_context_stale():
            log.warning("微信 context_token 可能已过期，主动推送会失败，直到用户给 Bot 发消息：%s", STALE_USER_HINT)
        while True:
            try:
                result = await self._post("/ilink/bot/getupdates", {"get_updates_buf": self._state.get("cursor", "")})
            except (httpx.TimeoutException, httpx.NetworkError) as e:
                log.debug("微信长轮询暂时失败: %s", e)
                await asyncio.sleep(3)
                continue
            except Exception:
                log.exception("微信长轮询失败，30 秒后重试")
                await asyncio.sleep(30)
                continue
            if cursor := result.get("get_updates_buf"):
                self._save(cursor=cursor)
            for msg in result.get("msgs") or []:
                if msg.get("message_type") in (None, 1) and msg.get("from_user_id") and msg.get("context_token"):
                    is_new = msg["from_user_id"] != self._state.get("user_id")
                    was_stale = not is_new and self.is_context_stale()
                    self._note_inbound_context(msg)
                    if is_new:
                        log.info("微信已绑定: %s", msg["from_user_id"])
                        try:
                            await self.send(BIND_REPLY)
                        except Exception:
                            log.exception("微信回复失败")
                    elif was_stale:
                        log.info("微信 context_token 已刷新")
                        try:
                            await self.send(REFRESH_REPLY)
                        except Exception:
                            log.exception("微信回复失败")

    async def login(self) -> None:
        resp = await self._http.get(f"{BASE_URL}/ilink/bot/get_bot_qrcode", params={"bot_type": 3})
        payload = resp.raise_for_status().json()
        if not payload.get("qrcode") or not payload.get("qrcode_img_content"):
            raise WeChatError(f"获取登录二维码失败: {payload}")
        print("请用微信扫描下方二维码，并在手机上确认：")
        qr = qrcode.QRCode(border=1)
        qr.add_data(payload["qrcode_img_content"])
        qr.print_ascii(invert=True)
        print(f"二维码无法识别时，打开：{payload['qrcode_img_content']}")

        base_url = BASE_URL
        while True:
            try:
                resp = await self._http.get(
                    f"{base_url}/ilink/bot/get_qrcode_status",
                    params={"qrcode": payload["qrcode"]},
                    headers={"iLink-App-ClientVersion": "1"},
                )
                status = resp.raise_for_status().json()
            except httpx.ReadTimeout:
                continue
            match status.get("status"):
                case "confirmed":
                    self._state = {"bot_token": status["bot_token"], "base_url": status.get("baseurl") or BASE_URL}
                    self._save()
                    print(f"登录成功，凭证已保存到 {self._path}。服务运行时，用微信给 Bot 发一条消息完成绑定。")
                    return
                case "expired":
                    raise WeChatError("二维码已过期，请重新登录")
                case "scaned_but_redirect" | "binded_redirect" if status.get("redirect_host"):
                    base_url = status["redirect_host"].rstrip("/")
                case "need_verifycode" | "verify_code_blocked":
                    raise WeChatError(f"登录需要验证（{status['status']}），请在微信上处理后重新登录")
            await asyncio.sleep(1)

    async def _post(self, path: str, payload: dict[str, Any]) -> dict[str, Any]:
        if not self.logged_in:
            raise WeChatError("微信未登录：python -m app.wechat login")
        payload["base_info"] = {"channel_version": "1.0", "bot_agent": "homepilot-agent"}
        body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode()
        resp = await self._http.post(
            f"{self._state['base_url'].rstrip('/')}{path}",
            content=body,
            headers={
                "Content-Type": "application/json",
                "AuthorizationType": "ilink_bot_token",
                "Authorization": f"Bearer {self._state['bot_token']}",
                "X-WECHAT-UIN": base64.b64encode(str(random.getrandbits(32)).encode()).decode(),
            },
        )
        result = resp.raise_for_status().json()
        if result.get("ret", 0) != 0:
            raise WeChatError(f"iLink 接口错误: {result.get('errmsg') or result}")
        return result

    def _save(self, **changes: Any) -> None:
        self._state.update(changes)
        self._path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self._path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self._state, ensure_ascii=False, indent=2), "utf-8")
        os.chmod(tmp, 0o600)
        tmp.replace(self._path)


async def _login() -> None:
    wechat = WeChat(Path(Settings().data_dir) / "wechat.json")
    try:
        await wechat.login()
    finally:
        await wechat.close()


if __name__ == "__main__":
    if sys.argv[1:] != ["login"]:
        sys.exit("用法：python -m app.wechat login")
    asyncio.run(_login())