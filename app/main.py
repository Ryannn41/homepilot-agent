"""HomePilot Agent 入口：python -m app.main"""

import asyncio
import hmac
import logging
from http import HTTPStatus

import dashscope
from websockets.asyncio.server import ServerConnection, serve

from app.config import Settings
from app.session import VoiceSession

log = logging.getLogger("homepilot")

WS_PATH = "/ws/voice/"
MAX_MESSAGE_SIZE = 1 << 20  # 协议规定单条消息最大 1 MiB


async def serve_forever(settings: Settings) -> None:
    token = settings.auth_token.get_secret_value()

    def check_request(connection: ServerConnection, request):
        if request.path.split("?")[0] != WS_PATH:
            return connection.respond(HTTPStatus.NOT_FOUND, "not found\n")
        auth = request.headers.get("Authorization", "")
        if token and not hmac.compare_digest(auth, f"Bearer {token}"):
            log.warning("拒绝连接：认证失败 %s", connection.remote_address)
            return connection.respond(HTTPStatus.UNAUTHORIZED, "unauthorized\n")
        return None

    async def handle(ws: ServerConnection) -> None:
        device = ws.request.headers.get("X-Device-Id")
        log.info("客户端已连接: device=%s %s", device, ws.remote_address)
        await VoiceSession(ws, settings).run()
        log.info("客户端已断开: device=%s", device)

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


def main() -> None:
    settings = Settings()
    logging.basicConfig(level=settings.log_level, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    if not settings.dashscope_api_key.get_secret_value() or not settings.deepseek_api_key.get_secret_value():
        log.warning("未配置 HOMEPILOT_DASHSCOPE_API_KEY 或 HOMEPILOT_DEEPSEEK_API_KEY，对话会失败")
    dashscope.api_key = settings.dashscope_api_key.get_secret_value()
    try:
        asyncio.run(serve_forever(settings))
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
