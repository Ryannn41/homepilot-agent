import json
import uuid
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from app.reminders import ReminderStore
from app.schedule import build_schedule_snapshot, reminder_to_item, schedule_item_id

TZ = ZoneInfo("Asia/Shanghai")


def test_schedule_item_id_stable():
    a, b = schedule_item_id(42), schedule_item_id(42)
    assert a == b and uuid.UUID(a)


def test_reminder_maps_with_due_at_and_column(tmp_path):
    store = ReminderStore(tmp_path / "r.db", TZ)
    at = datetime(2026, 11, 22, 15, 30, tzinfo=TZ)
    now = datetime(2026, 11, 22, 10, 0, tzinfo=TZ)
    reminder = store.add("dev", "抢四点吴奥的票", at, "once")
    item = reminder_to_item(reminder, now)
    assert item is not None
    assert item == {
        "id": schedule_item_id(reminder.id),
        "kind": "reminder",
        "title": "抢四点吴奥的票",
        "status": "pending",
        "due_at": "2026-11-22T15:30:00+08:00",
        "column": "upcoming",
    }


def test_timer_maps_countdown(tmp_path):
    store = ReminderStore(tmp_path / "r.db", TZ)
    now = datetime(2026, 11, 21, 12, 0, tzinfo=TZ)
    r = store.add("dev", "煮面，8分钟计时到了", now + timedelta(minutes=8), "once")
    item = reminder_to_item(r, now)
    assert item["kind"] == "countdown"
    assert item["title"] == "煮面"
    assert item["countdown_sec"] == 8 * 60
    assert "due_at" in item


def test_build_snapshot_json_shape(tmp_path):
    store = ReminderStore(tmp_path / "r.db", TZ)
    at = datetime(2026, 11, 22, 15, 30, tzinfo=TZ)
    store.add("dev", "开会", at, "daily")
    snap = build_schedule_snapshot(store, "dev", now=datetime(2026, 11, 21, 9, 0, tzinfo=TZ))
    assert snap["version"] == 1
    assert len(snap["items"]) == 1
    msg = json.loads(json.dumps(snap, ensure_ascii=False))
    assert msg["items"][0]["title"] == "开会"