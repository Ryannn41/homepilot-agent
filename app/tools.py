"""Agent 可调用的工具：时间日历、天气、联网搜索、计算、定时提醒和简报、计时、备忘清单、发微信。
以进程内 MCP 服务的形式挂到 Agent 上。"""

import asyncio
import logging
from collections.abc import Awaitable, Callable
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import httpx
from claude_agent_sdk import create_sdk_mcp_server, tool
from claude_agent_sdk.types import McpSdkServerConfig
from dashscope import Generation

from app.calc import calculate
from app.cn_calendar import calendar_info, parse_date
from app.config import Settings
from app.notes import NoteStore
from app.reminders import REPEATS, ReminderStore
from app.wechat import WeChat

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
DEFAULT_LIST = "备忘"


def build_tools(
    settings: Settings,
    device_id: str,
    *,
    reminders: ReminderStore,
    notes: NoteStore,
    wechat: WeChat,
    on_schedule_changed: Callable[[], Awaitable[None]] | None = None,
) -> McpSdkServerConfig:
    """device_id：提醒、清单属于哪台设备，提醒到点后在这台设备上播放。"""
    tz = ZoneInfo(settings.timezone)

    async def _notify_board(result: str) -> str:
        if on_schedule_changed is not None:
            await on_schedule_changed()
        return result

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

    schedule_schema = {
        "time": {"type": "string", "description": "第一次的时间，格式 YYYY-MM-DD HH:MM"},
        "repeat": {
            "type": "string",
            "enum": list(REPEATS),
            "description": "once 只一次；daily 每天；weekdays 工作日（周一到周五）；weekly 每周（与第一次同一星期几）",
        },
    }

    async def schedule(args, kind: str) -> str:
        at = datetime.strptime(args["time"], "%Y-%m-%d %H:%M").replace(tzinfo=tz)
        msg = f"已创建 {reminders.add(device_id, args['content'], at, args['repeat'], kind).describe()}"
        return await _notify_board(msg)

    @tool(
        "create_reminder",
        "创建定时提醒，到点后语音播报并发微信。先用 get_current_time 确认现在的时间再计算 time。"
        "「N分钟后」这类相对时间用 start_timer",
        {
            "type": "object",
            "properties": {"content": {"type": "string", "description": "提醒内容，如：开会、吃药"}, **schedule_schema},
            "required": ["content", "time", "repeat"],
        },
    )
    async def create_reminder(args):
        log.info("调用工具 create_reminder %s", args)
        return await _run(schedule(args, "reminder"))

    @tool(
        "create_briefing",
        "创建定时简报，如「每天早上7点给我播报天气和今天的安排」。到点后你会按 content 的要求调用工具"
        "现场整理一段播报，在音箱播报并发微信。先用 get_current_time 确认现在的时间再计算 time。"
        "可用 list_reminders / delete_reminder 查看和取消",
        {
            "type": "object",
            "properties": {
                "content": {
                    "type": "string",
                    "description": "到点时要播报什么，写成给你自己的指令，如：播报今天的天气、今天的提醒和购物清单",
                },
                **schedule_schema,
            },
            "required": ["content", "time", "repeat"],
        },
    )
    async def create_briefing(args):
        log.info("调用工具 create_briefing %s", args)
        return await _run(schedule(args, "briefing"))

    @tool("list_reminders", "列出所有定时提醒、简报和计时及编号", {})
    async def list_reminders(args):
        log.info("调用工具 list_reminders")
        items = reminders.for_device(device_id)
        return _text("\n".join(r.describe() for r in items) if items else "没有任何提醒")

    @tool("delete_reminder", "按编号删除定时提醒；不知道编号时先调用 list_reminders", {"id": int})
    async def delete_reminder(args):
        log.info("调用工具 delete_reminder %s", args)
        deleted = reminders.delete(device_id, int(args["id"]))
        msg = f"已删除提醒 #{args['id']}" if deleted else f"没有编号为 {args['id']} 的提醒"
        if deleted:
            msg = await _notify_board(msg)
        return _text(msg)

    @tool(
        "start_timer",
        "倒计时：「8分钟后叫我」「煮面计时10分钟」这类相对时间用它，不用 create_reminder。"
        "到点后语音播报并发微信；可用 list_reminders / delete_reminder 查看和取消",
        {
            "type": "object",
            "properties": {
                "seconds": {"type": "integer", "description": "倒计时秒数，如 8 分钟为 480"},
                "label": {"type": "string", "description": "计时的事情，如：煮面；用户没说可以不传"},
            },
            "required": ["seconds"],
        },
    )
    async def start_timer(args):
        log.info("调用工具 start_timer %s", args)

        async def start() -> str:
            seconds = int(args["seconds"])
            if seconds <= 0:
                raise ValueError("计时时长必须大于 0")
            duration = format_duration(seconds)
            content = f"{args['label']}，{duration}计时到了" if args.get("label") else f"{duration}计时到了"
            reminder = reminders.add(device_id, content, datetime.now(tz) + timedelta(seconds=seconds), "once")
            return await _notify_board(
                f"已开始计时 #{reminder.id} {duration}，{reminder.at:%H:%M:%S} 到点"
            )

        return await _run(start())

    @tool(
        "get_calendar_info",
        "查询某天的星期、农历、是否放假或调休上班、距今几天。"
        "例如：下周一上班吗、今天农历几号、离春节还有几天（春节传 农历YYYY-01-01）",
        {
            "type": "object",
            "properties": {
                "date": {
                    "type": "string",
                    "description": "公历 YYYY-MM-DD，或农历 农历YYYY-MM-DD；不传表示今天。先用 get_current_time 确认今天日期",
                }
            },
        },
    )
    async def get_calendar_info(args):
        log.info("调用工具 get_calendar_info %s", args)

        async def info() -> str:
            today = datetime.now(tz).date()
            return calendar_info(parse_date(args["date"]) if args.get("date") else today, today)

        return await _run(info())

    @tool(
        "calculate",
        "精确计算数学表达式，需要算数时一定用它，不要心算。支持 + - * / // % ** 括号，以及 sqrt abs round log log10",
        {"expression": str},
    )
    async def calculate_tool(args):
        log.info("调用工具 calculate %s", args)

        async def calc() -> str:
            return f"{args['expression']} = {calculate(args['expression'])}"

        return await _run(calc())

    list_name_schema = {"type": "string", "description": f"清单名，如：购物清单、待办；默认「{DEFAULT_LIST}」"}

    @tool(
        "add_note",
        "往清单里记东西，如「记一下要买牛奶和鸡蛋」",
        {
            "type": "object",
            "properties": {
                "items": {"type": "array", "items": {"type": "string"}, "description": "要记的条目，每项一件事"},
                "list": list_name_schema,
            },
            "required": ["items"],
        },
    )
    async def add_note(args):
        log.info("调用工具 add_note %s", args)
        list_name = args.get("list") or DEFAULT_LIST
        notes.add(device_id, list_name, args["items"])
        return _text(f"已记到「{list_name}」，现在有：{'、'.join(notes.lists(device_id, list_name).get(list_name, []))}")

    @tool(
        "list_notes",
        "查看清单内容；不传 list 时列出所有清单",
        {"type": "object", "properties": {"list": {"type": "string", "description": "清单名"}}},
    )
    async def list_notes(args):
        log.info("调用工具 list_notes %s", args)
        lists = notes.lists(device_id, args.get("list"))
        return _text("\n".join(f"{name}：{'、'.join(items)}" for name, items in lists.items()) or "清单是空的")

    @tool(
        "remove_note",
        "从清单中划掉包含关键词的条目，如「牛奶买了」",
        {
            "type": "object",
            "properties": {"keyword": {"type": "string"}, "list": list_name_schema},
            "required": ["keyword"],
        },
    )
    async def remove_note(args):
        log.info("调用工具 remove_note %s", args)
        list_name = args.get("list") or DEFAULT_LIST
        removed = notes.remove(device_id, list_name, args["keyword"])
        return _text(f"已从「{list_name}」划掉：{'、'.join(removed)}" if removed else f"「{list_name}」里没有「{args['keyword']}」")

    @tool(
        "clear_notes",
        "清空一个清单",
        {"type": "object", "properties": {"list": list_name_schema}},
    )
    async def clear_notes(args):
        log.info("调用工具 clear_notes %s", args)
        list_name = args.get("list") or DEFAULT_LIST
        return _text(f"已清空「{list_name}」，共 {notes.clear(device_id, list_name)} 条")

    @tool(
        "send_to_wechat",
        "把文字发到用户的微信。内容较长或用户需要保存时使用，如菜谱步骤、地址电话、清单、链接；"
        "发完在语音里只简单说一句已发到微信",
        {"text": str},
    )
    async def send_to_wechat(args):
        log.info("调用工具 send_to_wechat (%d 字)", len(args["text"]))

        async def send() -> str:
            await wechat.send(args["text"])
            return "已发送到微信"

        return await _run(send())

    return create_sdk_mcp_server(
        SERVER_NAME,
        tools=[
            get_current_time, get_calendar_info, get_weather, web_search, calculate_tool,
            create_reminder, create_briefing, list_reminders, delete_reminder, start_timer,
            add_note, list_notes, remove_note, clear_notes, send_to_wechat,
        ],
    )  # fmt: skip


def format_time(now: datetime) -> str:
    return f"{now:%Y年%m月%d日} 星期{WEEKDAYS[now.weekday()]} {now:%H:%M}"


def format_duration(seconds: int) -> str:
    hours, rest = divmod(seconds, 3600)
    minutes, seconds = divmod(rest, 60)
    parts = [f"{hours}小时" if hours else "", f"{minutes}分钟" if minutes else "", f"{seconds}秒" if seconds else ""]
    return "".join(parts)


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
