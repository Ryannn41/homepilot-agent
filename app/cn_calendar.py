"""中国日历：农历、法定节假日和调休。"""

from datetime import date, datetime

import chinese_calendar
from zhdate import ZhDate

WEEKDAYS = "一二三四五六日"
LUNAR_MONTHS = "正二三四五六七八九十冬腊"
HOLIDAY_NAMES = {h.value: h.chinese for h in chinese_calendar.Holiday}


def parse_date(text: str) -> date:
    """支持公历 YYYY-MM-DD，以及农历「农历YYYY-MM-DD」（如春节为农历YYYY-01-01）。"""
    text = text.strip()
    if text.startswith("农历"):
        year, month, day = map(int, text.removeprefix("农历").split("-"))
        return ZhDate(year, month, day).to_datetime().date()
    return datetime.strptime(text, "%Y-%m-%d").date()


def lunar_day_name(day: int) -> str:
    if day == 10:
        return "初十"
    if day in (20, 30):
        return "二十" if day == 20 else "三十"
    digits = "一二三四五六七八九"
    prefix = "初十廿"[day // 10]
    return prefix + digits[day % 10 - 1]


def describe_lunar(day: date) -> str:
    lunar = ZhDate.from_datetime(datetime(day.year, day.month, day.day))
    month = ("闰" if lunar.leap_month else "") + LUNAR_MONTHS[lunar.lunar_month - 1] + "月"
    year = lunar.chinese().split(" ", 1)[1]  # 如「丙午年 (马年)」
    return f"农历{month}{lunar_day_name(lunar.lunar_day)}，{year}"


def describe_workday(day: date) -> str:
    try:
        is_workday = chinese_calendar.is_workday(day)
        _, holiday = chinese_calendar.get_holiday_detail(day)
    except NotImplementedError:
        return "暂无这一年的法定节假日安排，" + ("周末" if day.weekday() >= 5 else "平日")
    name = HOLIDAY_NAMES.get(holiday, holiday)
    if is_workday:
        return f"调休上班（{name}）" if day.weekday() >= 5 else "工作日"
    return f"{name}放假" if name else "周末休息"


def calendar_info(day: date, today: date) -> str:
    delta = (day - today).days
    relative = "今天" if delta == 0 else f"{delta}天后" if delta > 0 else f"{-delta}天前"
    return (
        f"{day:%Y年%m月%d日} 星期{WEEKDAYS[day.weekday()]}（{relative}），"
        f"{describe_lunar(day)}，{describe_workday(day)}"
    )
