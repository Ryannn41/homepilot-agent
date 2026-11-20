"""HomePilot Agent 入口：python -m app.main"""

import asyncio
import hmac
import logging
from http import HTTPStatus
from pathlib import Path
from zoneinfo import ZoneInfo

import dashscope
from websockets.asyncio.server import ServerConnection, serve

from app.agent import Agent
from app.config import Settings
from app.reminders import Reminder, ReminderStore, run_scheduler
from app.session import VoiceSession
from app.tools import build_tools
from app.wechat import WeChat

log = logging.getLogger("homepilot")

WS_PATH = "/ws/voice/"
MAX_MESSAGE_SIZE = 1 << 20  # 协议规定单条消息最大 1 MiB


async def serve_forever(settings: Settings) -> None:
    token = settings.auth_token.get_secret_value()
    tz = ZoneInfo(settings.timezone)
    data_dir = Path(settings.data_dir)
    reminders = ReminderStore(data_dir / "reminders.db", tz)
    wechat = WeChat(data_dir / "wechat.json")
    devices: dict[str, VoiceSession] = {}  # 在线设备 -> 它的连接

    async def deliver(reminder: Reminder) -> None:
        """音箱和微信同时提醒，任一渠道失败不影响另一个。"""
        text = f"提醒你：{reminder.content}"

        async def to_speaker() -> None:
            if not (session := devices.get(reminder.device_id)):
                log.info("提醒 #%d：音箱不在线，跳过语音播报", reminder.id)
                return
            try:
                await session.notify(text)
                log.info("提醒 #%d 已在音箱播报", reminder.id)
            except Exception:
                log.exception("提醒 #%d 推送到音箱失败", reminder.id)

        async def to_wechat() -> None:
            try:
                await wechat.send(f"⏰ {text}（{reminder.at:%H:%M}）")
                log.info("提醒 #%d 已发到微信", reminder.id)
            except Exception as e:
                log.warning("提醒 #%d 微信发送失败: %s", reminder.id, e)

        await asyncio.gather(to_speaker(), to_wechat())

    def check_request(connection: ServerConnection, request):
        if request.path.split("?")[0] != WS_PATH:
            return connection.respond(HTTPStatus.NOT_FOUND, "not found\n")
        auth = request.headers.get("Authorization", "")
        if token and not hmac.compare_digest(auth, f"Bearer {token}"):
            log.warning("拒绝连接：认证失败 %s", connection.remote_address)
            return connection.respond(HTTPStatus.UNAUTHORIZED, "unauthorized\n")
        return None

    async def handle(ws: ServerConnection) -> None:
        device = ws.request.headers.get("X-Device-Id") or "unknown"
        log.info("客户端已连接: device=%s %s", device, ws.remote_address)
        session = VoiceSession(ws, settings, lambda context: Agent(settings, build_tools(settings, reminders, device), context))
        devices[device] = session
        try:
            await session.run()
        finally:
            if devices.get(device) is session:
                del devices[device]
            log.info("客户端已断开: device=%s", device)

    background = [asyncio.create_task(run_scheduler(reminders, tz, deliver)), asyncio.create_task(wechat.run())]
    try:
        async with serve(
            handle,
            settings.host,
            settings.port,
            process_request=check_request,
            compression=None,
            max_size=MAX_MESSAGE_SIZE,
        ) as server:
            log.info("服务已启动: ws://%s:%d%s", settings.host, settings.port, WS_PATH)
            await server.serve_forever()
    finally:
        for task in background:
            task.cancel()
        await wechat.close()


def main() -> None:
    settings = Settings()
    logging.basicConfig(level=settings.log_level, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)  # 微信长轮询每次请求都会打日志
    if not settings.dashscope_api_key.get_secret_value() or not settings.deepseek_api_key.get_secret_value():
        log.warning("未配置 HOMEPILOT_DASHSCOPE_API_KEY 或 HOMEPILOT_DEEPSEEK_API_KEY，对话会失败")
    dashscope.api_key = settings.dashscope_api_key.get_secret_value()
    try:
        asyncio.run(serve_forever(settings))
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
