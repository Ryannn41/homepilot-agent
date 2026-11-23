"""将 reminders.db 中的提醒映射为客户端看板可用的 schedule.items。"""

import logging
import uuid
from datetime import datetime

from app.notes import NoteStore
from app.reminders import KINDS, Reminder, ReminderStore

log = logging.getLogger(__name__)

SCHEDULE_VERSION = 1
SCHEDULE_ITEM_NS = uuid.UUID("8f4e3c2a-1b5d-4e6f-9a0b-1c2d3e4f5061")
NOTE_ITEM_NS = uuid.UUID("a1b2c3d4-e5f6-4789-a012-3456789abcde")
ALLOWED_KINDS = frozenset({"reminder", "task", "countdown"})
ALLOWED_COLUMNS = frozenset({"upcoming", "todo", "done"})
OPTIONAL_KEYS = frozenset({"due_at", "countdown_sec", "column"})


def schedule_item_id(reminder_id: int) -> str:
    """由库内自增 id 生成稳定 UUID，供看板与后续 notify 对齐。"""
    return str(uuid.uuid5(SCHEDULE_ITEM_NS, str(reminder_id)))


def note_item_id(note_id: int) -> str:
    return str(uuid.uuid5(NOTE_ITEM_NS, str(note_id)))


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


def note_to_item(note_id: int, list_name: str, text: str) -> dict[str, object]:
    title = text if list_name == "待办" else f"「{list_name}」{text}"
    item: dict[str, object] = {
        "id": note_item_id(note_id),
        "kind": "task",
        "title": title,
        "status": "pending",
        "column": "todo",
    }
    _assert_client_shape(item)
    return item


def build_schedule_snapshot(
    store: ReminderStore,
    device_id: str,
    *,
    notes: NoteStore | None = None,
    now: datetime | None = None,
) -> dict:
    now = now or datetime.now(store._tz)
    items = [it for r in store.for_device(device_id) if (it := reminder_to_item(r, now))]
    if notes is not None:
        for note_id, list_name, text in notes.entries(device_id):
            if item := _safe_note_item(note_id, list_name, text):
                items.append(item)
    return {"version": SCHEDULE_VERSION, "items": items}


def _safe_note_item(note_id: int, list_name: str, text: str) -> dict[str, object] | None:
    try:
        return note_to_item(note_id, list_name, text)
    except Exception as e:
        log.warning("备忘 #%d 映射 schedule 失败，已跳过: %s", note_id, e)
        return None


def _kind_and_title(reminder: Reminder) -> tuple[str, str]:
    if reminder.kind == "briefing":
        return "reminder", f"{KINDS['briefing']}{reminder.content}"
    if reminder.repeat == "once" and "计时到了" in reminder.content:
        title = reminder.content.split("，", 1)[0] if "，" in reminder.content else "倒计时"
        return "countdown", title
    return "reminder", reminder.content


def _infer_column(reminder: Reminder, now: datetime, kind: str) -> str | None:
    """定时提醒、倒计时一律进「即将到期」；备忘清单由 note_to_item 进「待办」。"""
    if kind == "countdown" and reminder.at <= now:
        return None
    return "upcoming"


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