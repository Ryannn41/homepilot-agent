import asyncio
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from app import reminders as rm
from app.reminders import ReminderStore, next_time, run_scheduler

TZ = ZoneInfo("Asia/Shanghai")


@pytest.fixture
def store(tmp_path):
    return ReminderStore(tmp_path / "r.db", TZ)


def test_next_time():
    friday = datetime(2026, 11, 20, 9, 0, tzinfo=TZ)
    assert next_time(friday, "daily") == datetime(2026, 11, 21, 9, 0, tzinfo=TZ)
    assert next_time(friday, "weekdays") == datetime(2026, 11, 23, 9, 0, tzinfo=TZ)  # 跳过周末
    assert next_time(friday, "weekly") == datetime(2026, 11, 27, 9, 0, tzinfo=TZ)


def test_briefing_kind(store):
    at = datetime.now(TZ).replace(hour=7, minute=0) + timedelta(days=1)
    b = store.add("dev", "播报天气和今天的提醒", at, "daily", "briefing")
    assert store.for_device("dev") == [b] and b.kind == "briefing"
    assert "07:00 简报：播报天气和今天的提醒" in b.describe()
    with pytest.raises(ValueError, match="类型"):
        store.add("dev", "x", at, "daily", "alarm")


def test_old_database_gets_kind_column(tmp_path):
    import sqlite3

    path = tmp_path / "old.db"
    db = sqlite3.connect(path)
    db.execute("CREATE TABLE reminders (id INTEGER PRIMARY KEY, device_id TEXT NOT NULL, content TEXT NOT NULL, "
               "at INTEGER NOT NULL, repeat TEXT NOT NULL)")
    at = int((datetime.now(TZ) + timedelta(hours=1)).timestamp())
    db.execute("INSERT INTO reminders (device_id, content, at, repeat) VALUES ('dev', '开会', ?, 'once')", (at,))
    db.commit()
    db.close()
    assert [r.kind for r in ReminderStore(path, TZ).for_device("dev")] == ["reminder"]


def test_add_list_delete_by_device(store):
    soon = datetime.now(TZ) + timedelta(hours=1)
    r = store.add("dev1", "开会", soon, "daily")
    assert store.for_device("dev1") == [r] and store.for_device("dev2") == []
    assert not store.delete("dev2", r.id)
    assert store.delete("dev1", r.id) and store.for_device("dev1") == []


def test_add_rejects_past_once_and_rolls_repeating_forward(store):
    past = datetime.now(TZ) - timedelta(hours=1)
    with pytest.raises(ValueError):
        store.add("dev", "取快递", past, "once")
    r = store.add("dev", "开会", past, "daily")
    assert r.at == past.replace(microsecond=0) + timedelta(days=1)
    with pytest.raises(ValueError):
        store.add("dev", "开会", past, "monthly")


def test_due_and_advance(store):
    now = datetime.now(TZ)
    once = store.add("dev", "取快递", now + timedelta(minutes=1), "once")
    daily = store.add("dev", "开会", now + timedelta(minutes=1), "daily")
    later = now + timedelta(minutes=2)
    assert {r.id for r in store.due(later)} == {once.id, daily.id}
    for r in store.due(later):
        store.advance(r, later)
    assert store.due(later) == []
    assert [(r.id, r.at) for r in store.for_device("dev")] == [(daily.id, daily.at + timedelta(days=1))]


async def test_scheduler_delivers_due_and_skips_long_missed(store, monkeypatch):
    monkeypatch.setattr(rm, "CHECK_INTERVAL_S", 0.01)
    now = datetime.now(TZ)
    fresh = store.add("dev", "开会", now + timedelta(hours=1), "once")
    missed = store.add("dev", "吃药", now + timedelta(hours=1), "daily")
    for reminder, at in ((fresh, now - timedelta(minutes=1)), (missed, now - timedelta(hours=2))):
        store._db.execute("UPDATE reminders SET at = ? WHERE id = ?", (int(at.timestamp()), reminder.id))

    delivered = []

    async def deliver(reminder):
        delivered.append(reminder.content)

    task = asyncio.create_task(run_scheduler(store, TZ, deliver))
    await asyncio.sleep(0.1)
    task.cancel()
    assert delivered == ["开会"]
    assert [r.content for r in store.for_device("dev")] == ["吃药"]  # 错过的重复提醒推到下一次，没有投递
    assert store.for_device("dev")[0].at > now
    assert fresh.id not in {r.id for r in store.for_device("dev")}
