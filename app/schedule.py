"""将 reminders.db 中的提醒映射为客户端看板可用的 schedule.items。"""

import logging
import uuid
from datetime import datetime, timedelta

from app.reminders import KINDS, Reminder, ReminderStore

log = logging.getLogger(__name__)

SCHEDULE_VERSION = 1
SCHEDULE_ITEM_NS = uuid.UUID("8f4e3c2a-1b5d-4e6f-9a0b-1c2d3e4f5061")
UPCOMING_WINDOW = timedelta(hours=24)
ALLOWED_KINDS = frozenset({"reminder", "task", "countdown"})
ALLOWED_COLUMNS = frozenset({"upcoming", "todo", "done"})
OPTIONAL_KEYS = frozenset({"due_at", "countdown_sec", "column"})


def schedule_item_id(reminder_id: int) -> str:
    """由库内自增 id 生成稳定 UUID，供看板与后续 notify 对齐。"""
    return str(uuid.uuid5(SCHEDULE_ITEM_NS, str(reminder_id)))


def reminder_to_item(reminder: Reminder, now: datetime) -> dict[str, object] | None:
    try:
        kind, title = _kind_and_title(reminder)
        item: dict[str, object] = {
            "id": schedule_item_id(reminder.id),
            "kind": kind,
            "title": title,
            "status": "pending",
        }
        if kind == "countdown":
            sec = int((reminder.at - now).total_seconds())
            if sec > 0:
                item["countdown_sec"] = sec
        if reminder.at >= now or kind != "countdown":
            item["due_at"] = reminder.at.isoformat(timespec="seconds")
        if col := _infer_column(reminder, now, kind):
            item["column"] = col
        _assert_client_shape(item)
        return item
    except Exception as e:
        log.warning("提醒 #%d 映射 schedule 失败，已跳过: %s", reminder.id, e)
        return None


def build_schedule_snapshot(store: ReminderStore, device_id: str, *, now: datetime | None = None) -> dict:
    now = now or datetime.now(store._tz)
    items = [it for r in store.for_device(device_id) if (it := reminder_to_item(r, now))]
    return {"version": SCHEDULE_VERSION, "items": items}


def _kind_and_title(reminder: Reminder) -> tuple[str, str]:
    if reminder.kind == "briefing":
        return "reminder", f"{KINDS['briefing']}{reminder.content}"
    if reminder.repeat == "once" and "计时到了" in reminder.content:
        title = reminder.content.split("，", 1)[0] if "，" in reminder.content else "倒计时"
        return "countdown", title
    return "reminder", reminder.content


def _infer_column(reminder: Reminder, now: datetime, kind: str) -> str | None:
    if kind == "countdown" and reminder.at > now:
        return "upcoming"
    if reminder.at <= now + UPCOMING_WINDOW:
        return "upcoming"
    return "todo"


def _assert_client_shape(item: dict[str, object]) -> None:
    for key in ("id", "kind", "title", "status"):
        if not isinstance(item.get(key), str) or not item[key]:
            raise ValueError(f"非法字段 {key}")
    if item["kind"] not in ALLOWED_KINDS:
        raise ValueError(f"kind={item['kind']!r}")
    uuid.UUID(str(item["id"]))
    extra = set(item) - {"id", "kind", "title", "status"} - OPTIONAL_KEYS
    if extra:
        raise ValueError(f"多余字段 {extra}")
    if "column" in item and item["column"] not in ALLOWED_COLUMNS:
        raise ValueError(f"column={item['column']!r}")