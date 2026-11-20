from datetime import date

import pytest

from app.calc import calculate
from app.cn_calendar import calendar_info, describe_lunar, describe_workday, lunar_day_name, parse_date
from app.notes import NoteStore
from app.tools import format_duration


@pytest.mark.parametrize("expr,result", [
    ("328/3", "109.3333333"),
    ("(1+2)*3", "9"),
    ("2**10", "1024"),
    ("6÷4", "1.5"),
    ("sqrt(16) + abs(-1)", "5"),
    ("round(3.14159, 2)", "3.14"),
    ("-7 // 2", "-4"),
])  # fmt: skip
def test_calculate(expr, result):
    assert calculate(expr) == result


@pytest.mark.parametrize("expr,match", [
    ("1/0", "除数"),
    ("__import__('os')", "不支持"),
    ("9**9**9", "指数太大"),
    ("1 +", "无法理解"),
])  # fmt: skip
def test_calculate_rejects(expr, match):
    with pytest.raises(ValueError, match=match):
        calculate(expr)


def test_lunar_day_names():
    assert [lunar_day_name(d) for d in (1, 10, 11, 20, 21, 30)] == ["初一", "初十", "十一", "二十", "廿一", "三十"]


def test_lunar_and_spring_festival():
    assert describe_lunar(date(2026, 11, 20)) == "农历十月十二，丙午年 (马年)"
    assert parse_date("农历2027-01-01") == date(2027, 2, 6)
    assert parse_date("2026-10-01") == date(2026, 10, 1)


def test_workday_holiday_and_make_up_day():
    assert describe_workday(date(2026, 10, 1)) == "国庆节放假"
    assert describe_workday(date(2026, 10, 10)) == "调休上班（国庆节）"  # 周六
    assert describe_workday(date(2026, 11, 20)) == "工作日"
    assert describe_workday(date(2026, 11, 21)) == "周末休息"
    assert describe_workday(date(2099, 1, 5)).startswith("暂无这一年")


def test_calendar_info_relative_days():
    info = calendar_info(date(2027, 2, 6), today=date(2026, 11, 20))
    assert info.startswith("2027年02月06日 星期六（78天后），农历正月初一")
    assert info.endswith("春节放假")


def test_format_duration():
    assert format_duration(480) == "8分钟"
    assert format_duration(90) == "1分钟30秒"
    assert format_duration(3600) == "1小时"


def test_notes_by_device_and_list(tmp_path):
    notes = NoteStore(tmp_path / "notes.db")
    notes.add("dev", "购物清单", ["牛奶", " 鸡蛋 ", ""])
    notes.add("dev", "备忘", ["交电费"])
    notes.add("other", "购物清单", ["苹果"])
    assert notes.lists("dev") == {"购物清单": ["牛奶", "鸡蛋"], "备忘": ["交电费"]}
    assert notes.remove("dev", "购物清单", "牛") == ["牛奶"]
    assert notes.remove("dev", "购物清单", "面包") == []
    assert notes.clear("dev", "购物清单") == 1
    assert notes.lists("dev", "购物清单") == {}
    assert notes.lists("other") == {"购物清单": ["苹果"]}
