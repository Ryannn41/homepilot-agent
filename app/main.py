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
from app.notes import NoteStore
from app.reminders import Reminder, ReminderStore, run_scheduler
from app.session import VoiceSession
from app.tools import build_tools
from app.wechat import WeChat

log = logging.getLogger("homepilot")

WS_PATH = "/ws/voice/"
MAX_MESSAGE_SIZE = 1 << 20  # 协议规定单条消息最大 1 MiB
BRIEFING_TIMEOUT_S = 120
BRIEFING_CONTEXT = (
    "\n现在是用户预约的定时简报时间，用户不在对话中。先默默调用工具获取需要的信息，调用前不要说「我查一下」之类的话；"
    "拿到信息后只输出简报本身：开头简单问候，内容简洁，不超过六句话。"
    "不要向用户提问；不要调用 send_to_wechat，简报会自动发到微信。"
)


async def serve_forever(settings: Settings) -> None:
    token = settings.auth_token.get_secret_value()
    tz = ZoneInfo(settings.timezone)
    data_dir = Path(settings.data_dir)
    reminders = ReminderStore(data_dir / "reminders.db", tz)
    notes = NoteStore(data_dir / "notes.db")
    wechat = WeChat(data_dir / "wechat.json")
    devices: dict[str, VoiceSession] = {}  # 在线设备 -> 它的连接

    async def refresh_schedule(device: str) -> None:
        if session := devices.get(device):
            await session.send_schedule_snapshot(reminders, device)

    def new_agent(device: str, context: str = "") -> Agent:
        return Agent(
            settings,
            build_tools(
                settings,
                device,
                reminders=reminders,
                notes=notes,
                wechat=wechat,
                on_schedule_changed=lambda: refresh_schedule(device),
            ),
            context,
        )

    async def compose_briefing(reminder: Reminder) -> str:
        agent = new_agent(reminder.device_id, BRIEFING_CONTEXT)
        try:
            return "".join([s async for s in agent.ask(reminder.content)])
        finally:
            await agent.close()

    async def deliver(reminder: Reminder) -> None:
        """音箱和微信同时提醒，任一渠道失败不影响另一个。简报先由 Agent 现场生成内容。"""
        if reminder.kind == "briefing":
            try:
                text = await asyncio.wait_for(compose_briefing(reminder), BRIEFING_TIMEOUT_S)
            except Exception:
                log.exception("简报 #%d 生成失败", reminder.id)
                return
            wechat_text = f"📋 简报（{reminder.at:%H:%M}）\n{text}"
        else:
            text = f"提醒你：{reminder.content}"
            wechat_text = f"⏰ {text}（{reminder.at:%H:%M}）"

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
                await wechat.send(wechat_text)
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
        session = VoiceSession(ws, settings, lambda context: new_agent(device, context))
        devices[device] = session
        try:
            await session.send_schedule_snapshot(reminders, device)
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
