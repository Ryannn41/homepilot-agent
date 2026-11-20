"""定时提醒：SQLite 存储，后台调度器到点后交给投递函数（在线时推送到音箱，离线时发微信）。"""

import asyncio
import logging
import sqlite3
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

log = logging.getLogger(__name__)

REPEATS = {"once": "一次", "daily": "每天", "weekdays": "工作日", "weekly": "每周"}
CHECK_INTERVAL_S = 10
LATE_LIMIT = timedelta(minutes=30)  # 服务停机等原因错过超过这么久的提醒不再投递


@dataclass(frozen=True)
class Reminder:
    id: int
    device_id: str
    content: str
    at: datetime  # 下一次提醒时间
    repeat: str

    def describe(self) -> str:
        when = f"{self.at:%m月%d日 %H:%M}" if self.repeat == "once" else f"{REPEATS[self.repeat]} {self.at:%H:%M}"
        return f"#{self.id} {when} {self.content}（下次 {self.at:%Y-%m-%d %H:%M}）"


def next_time(at: datetime, repeat: str) -> datetime:
    if repeat == "weekly":
        return at + timedelta(days=7)
    at += timedelta(days=1)
    while repeat == "weekdays" and at.weekday() >= 5:
        at += timedelta(days=1)
    return at


class ReminderStore:
    def __init__(self, path: str | Path, tz):
        self._tz = tz
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(path)
        self._db.execute(
            "CREATE TABLE IF NOT EXISTS reminders ("
            "id INTEGER PRIMARY KEY, device_id TEXT NOT NULL, content TEXT NOT NULL, "
            "at INTEGER NOT NULL, repeat TEXT NOT NULL)"
        )

    def add(self, device_id: str, content: str, at: datetime, repeat: str) -> Reminder:
        if repeat not in REPEATS:
            raise ValueError(f"不支持的重复方式: {repeat}")
        at = at.replace(microsecond=0)  # 数据库按秒存储
        now = datetime.now(self._tz)
        if at <= now:
            if repeat == "once":
                raise ValueError(f"提醒时间 {at:%Y-%m-%d %H:%M} 已经过去了")
            while at <= now:
                at = next_time(at, repeat)
        with self._db:
            cursor = self._db.execute(
                "INSERT INTO reminders (device_id, content, at, repeat) VALUES (?, ?, ?, ?)",
                (device_id, content, int(at.timestamp()), repeat),
            )
        return Reminder(cursor.lastrowid, device_id, content, at, repeat)

    def for_device(self, device_id: str) -> list[Reminder]:
        return self._select("WHERE device_id = ? ORDER BY at", device_id)

    def delete(self, device_id: str, reminder_id: int) -> bool:
        with self._db:
            cursor = self._db.execute("DELETE FROM reminders WHERE id = ? AND device_id = ?", (reminder_id, device_id))
        return cursor.rowcount > 0

    def due(self, now: datetime) -> list[Reminder]:
        return self._select("WHERE at <= ? ORDER BY at", int(now.timestamp()))

    def advance(self, reminder: Reminder, now: datetime) -> None:
        """一次性提醒删除，重复提醒推到 now 之后的下一次。"""
        with self._db:
            if reminder.repeat == "once":
                self._db.execute("DELETE FROM reminders WHERE id = ?", (reminder.id,))
                return
            at = reminder.at
            while at <= now:
                at = next_time(at, reminder.repeat)
            self._db.execute("UPDATE reminders SET at = ? WHERE id = ?", (int(at.timestamp()), reminder.id))

    def _select(self, where: str, *params) -> list[Reminder]:
        rows = self._db.execute(f"SELECT id, device_id, content, at, repeat FROM reminders {where}", params)
        return [Reminder(id_, dev, content, datetime.fromtimestamp(at, self._tz), rep) for id_, dev, content, at, rep in rows]


async def run_scheduler(store: ReminderStore, tz, deliver: Callable[[Reminder], Awaitable[None]]) -> None:
    deliveries: set[asyncio.Task] = set()
    while True:
        now = datetime.now(tz)
        for reminder in store.due(now):
            store.advance(reminder, now)
            if now - reminder.at > LATE_LIMIT:
                log.warning("提醒 #%d 已错过 %s，不再投递", reminder.id, now - reminder.at)
                continue
            log.info("提醒到点: %s", reminder.describe())
            task = asyncio.create_task(deliver(reminder))
            deliveries.add(task)
            task.add_done_callback(deliveries.discard)
        await asyncio.sleep(CHECK_INTERVAL_S)
