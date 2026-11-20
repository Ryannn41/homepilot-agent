"""Agent 可调用的工具：当前时间、天气、联网搜索、定时提醒。以进程内 MCP 服务的形式挂到 Agent 上。"""

import asyncio
import logging
from datetime import datetime
from zoneinfo import ZoneInfo

import httpx
from claude_agent_sdk import create_sdk_mcp_server, tool
from claude_agent_sdk.types import McpSdkServerConfig
from dashscope import Generation

from app.config import Settings
from app.reminders import REPEATS, ReminderStore

log = logging.getLogger(__name__)

SERVER_NAME = "home"
ALLOWED_TOOLS = [f"mcp__{SERVER_NAME}"]  # 该服务下的所有工具免确认

WEEKDAYS = "一二三四五六日"
# Open-Meteo 使用的 WMO 天气代码
WEATHER_CODES = {
    0: "晴", 1: "晴间多云", 2: "多云", 3: "阴", 45: "雾", 48: "雾凇",
    51: "小毛毛雨", 53: "毛毛雨", 55: "大毛毛雨", 56: "冻毛毛雨", 57: "冻毛毛雨",
    61: "小雨", 63: "中雨", 65: "大雨", 66: "冻雨", 67: "冻雨",
    71: "小雪", 73: "中雪", 75: "大雪", 77: "米雪",
    80: "阵雨", 81: "阵雨", 82: "强阵雨", 85: "阵雪", 86: "强阵雪",
    95: "雷阵雨", 96: "雷阵雨伴冰雹", 99: "强雷阵雨伴冰雹",
}  # fmt: skip
DAY_NAMES = ("今天", "明天", "后天")


def build_tools(settings: Settings, reminders: ReminderStore, device_id: str) -> McpSdkServerConfig:
    """device_id：提醒属于哪台设备，到点后在这台设备上播放。"""
    tz = ZoneInfo(settings.timezone)

    @tool("get_current_time", "获取当前的日期、星期和时间", {})
    async def get_current_time(args):
        log.info("调用工具 get_current_time")
        return _text(format_time(datetime.now(tz)))

    @tool(
        "get_weather",
        f"查询城市的实时天气和未来三天预报。用户没说地点时不传 city，默认查询{settings.home_city}",
        {
            "type": "object",
            "properties": {"city": {"type": "string", "description": "城市名，如：上海、杭州；不支持区县名"}},
        },
    )
    async def get_weather(args):
        log.info("调用工具 get_weather %s", args)
        return await _run(fetch_weather(args.get("city") or settings.home_city))

    @tool("web_search", "联网搜索新闻、实时信息，以及你不确定的事实", {"query": str})
    async def web_search(args):
        log.info("调用工具 web_search %s", args)
        return await _run(search(settings, args["query"]))

    @tool(
        "create_reminder",
        "创建定时提醒，到点后用语音播报；音箱不在线时改发微信。先用 get_current_time 确认现在的时间再计算 time",
        {
            "type": "object",
            "properties": {
                "content": {"type": "string", "description": "提醒内容，如：开会、吃药"},
                "time": {"type": "string", "description": "第一次提醒的时间，格式 YYYY-MM-DD HH:MM"},
                "repeat": {
                    "type": "string",
                    "enum": list(REPEATS),
                    "description": "once 只提醒一次；daily 每天；weekdays 工作日（周一到周五）；weekly 每周（与第一次同一星期几）",
                },
            },
            "required": ["content", "time", "repeat"],
        },
    )
    async def create_reminder(args):
        log.info("调用工具 create_reminder %s", args)

        async def create() -> str:
            at = datetime.strptime(args["time"], "%Y-%m-%d %H:%M").replace(tzinfo=tz)
            return f"已创建提醒 {reminders.add(device_id, args['content'], at, args['repeat']).describe()}"

        return await _run(create())

    @tool("list_reminders", "列出所有定时提醒及编号", {})
    async def list_reminders(args):
        log.info("调用工具 list_reminders")
        items = reminders.for_device(device_id)
        return _text("\n".join(r.describe() for r in items) if items else "没有任何提醒")

    @tool("delete_reminder", "按编号删除定时提醒；不知道编号时先调用 list_reminders", {"id": int})
    async def delete_reminder(args):
        log.info("调用工具 delete_reminder %s", args)
        deleted = reminders.delete(device_id, int(args["id"]))
        return _text(f"已删除提醒 #{args['id']}" if deleted else f"没有编号为 {args['id']} 的提醒")

    return create_sdk_mcp_server(
        SERVER_NAME,
        tools=[get_current_time, get_weather, web_search, create_reminder, list_reminders, delete_reminder],
    )


def format_time(now: datetime) -> str:
    return f"{now:%Y年%m月%d日} 星期{WEEKDAYS[now.weekday()]} {now:%H:%M}"


_places: dict[str, dict] = {}  # 城市名 -> 地理编码结果，城市坐标不会变


async def fetch_weather(city: str) -> str:
    async with httpx.AsyncClient(timeout=10) as http:
        if city not in _places:
            geo = await http.get(
                "https://geocoding-api.open-meteo.com/v1/search", params={"name": city, "count": 1, "language": "zh"}
            )
            places = geo.raise_for_status().json().get("results")
            if not places:
                raise ValueError(f"找不到城市「{city}」")
            _places[city] = places[0]
        place = _places[city]
        forecast = await http.get(
            "https://api.open-meteo.com/v1/forecast",
            params={
                "latitude": place["latitude"],
                "longitude": place["longitude"],
                "timezone": "auto",
                "forecast_days": len(DAY_NAMES),
                "current": "temperature_2m,apparent_temperature,relative_humidity_2m,weather_code,wind_speed_10m",
                "daily": "weather_code,temperature_2m_max,temperature_2m_min,precipitation_probability_max",
            },
        )
    return format_weather(place, forecast.raise_for_status().json())


def format_weather(place: dict, data: dict) -> str:
    now, daily = data["current"], data["daily"]
    lines = [
        f"{place['name']}（{place.get('admin1', '')}）现在{WEATHER_CODES.get(now['weather_code'], '未知天气')}，"
        f"气温{now['temperature_2m']:.0f}度，体感{now['apparent_temperature']:.0f}度，"
        f"湿度{now['relative_humidity_2m']}%，风速每小时{now['wind_speed_10m']:.0f}公里。"
    ]
    for i, day in enumerate(DAY_NAMES[: len(daily["time"])]):
        lines.append(
            f"{day}{WEATHER_CODES.get(daily['weather_code'][i], '未知天气')}，"
            f"{daily['temperature_2m_min'][i]:.0f}到{daily['temperature_2m_max'][i]:.0f}度，"
            f"降水概率{daily['precipitation_probability_max'][i]}%。"
        )
    return "\n".join(lines)


async def search(settings: Settings, query: str) -> str:
    """用通义千问的联网搜索回答问题，返回整理好的事实要点。"""

    def call() -> str:
        resp = Generation.call(
            model=settings.search_model,
            messages=[
                {"role": "system", "content": "根据联网搜索结果，简洁地给出事实要点，不超过200字。"},
                {"role": "user", "content": query},
            ],
            enable_search=True,
            result_format="message",
        )
        if resp.status_code != 200:
            raise RuntimeError(f"搜索失败: {resp.code} {resp.message}")
        return resp.output.choices[0].message.content

    return await asyncio.to_thread(call)


def _text(text: str) -> dict:
    return {"content": [{"type": "text", "text": text}]}


async def _run(text_coro) -> dict:
    """工具出错时把原因告诉模型，由它决定怎么回复用户。"""
    try:
        return _text(await text_coro)
    except Exception as e:
        log.warning("工具调用失败: %s", e)
        return {**_text(f"出错了：{e}"), "is_error": True}
