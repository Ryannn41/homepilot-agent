from datetime import datetime

from app.tools import _run, format_time, format_weather

PLACE = {"name": "上海", "admin1": "上海市"}
DATA = {
    "current": {
        "temperature_2m": 11.3,
        "apparent_temperature": 7.1,
        "relative_humidity_2m": 40,
        "weather_code": 0,
        "wind_speed_10m": 12.1,
    },
    "daily": {
        "time": ["2026-11-20", "2026-11-21"],
        "weather_code": [61, 999],
        "temperature_2m_max": [11.5, 13.0],
        "temperature_2m_min": [4.3, 4.1],
        "precipitation_probability_max": [80, 0],
    },
}


def test_format_time():
    assert format_time(datetime(2026, 11, 22, 8, 5)) == "2026年11月22日 星期日 08:05"


def test_format_weather():
    assert format_weather(PLACE, DATA).splitlines() == [
        "上海（上海市）现在晴，气温11度，体感7度，湿度40%，风速每小时12公里。",
        "今天小雨，4到12度，降水概率80%。",
        "明天未知天气，4到13度，降水概率0%。",
    ]


async def test_run_reports_errors_to_model():
    async def fail():
        raise ValueError("找不到城市「火星」")

    async def ok():
        return "晴"

    assert await _run(ok()) == {"content": [{"type": "text", "text": "晴"}]}
    result = await _run(fail())
    assert result["is_error"] and "火星" in result["content"][0]["text"]
