"""备忘清单：购物清单、待办等，按设备存放在 SQLite。"""

import sqlite3
from pathlib import Path


class NoteStore:
    def __init__(self, path: str | Path):
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(path)
        self._db.execute(
            "CREATE TABLE IF NOT EXISTS notes ("
            "id INTEGER PRIMARY KEY, device_id TEXT NOT NULL, list TEXT NOT NULL, item TEXT NOT NULL)"
        )

    def add(self, device_id: str, list_name: str, items: list[str]) -> None:
        with self._db:
            self._db.executemany(
                "INSERT INTO notes (device_id, list, item) VALUES (?, ?, ?)",
                [(device_id, list_name, item.strip()) for item in items if item.strip()],
            )

    def lists(self, device_id: str, list_name: str | None = None) -> dict[str, list[str]]:
        """清单名 -> 条目（按添加顺序）；list_name 为空时返回所有清单。"""
        sql, params = "SELECT list, item FROM notes WHERE device_id = ?", [device_id]
        if list_name:
            sql, params = sql + " AND list = ?", params + [list_name]
        result: dict[str, list[str]] = {}
        for name, item in self._db.execute(sql + " ORDER BY id", params):
            result.setdefault(name, []).append(item)
        return result

    def remove(self, device_id: str, list_name: str, keyword: str) -> list[str]:
        """删除清单中包含 keyword 的条目，返回被删除的条目。"""
        rows = self._db.execute(
            "SELECT id, item FROM notes WHERE device_id = ? AND list = ? AND instr(item, ?) > 0",
            (device_id, list_name, keyword),
        ).fetchall()
        with self._db:
            self._db.executemany("DELETE FROM notes WHERE id = ?", [(id_,) for id_, _ in rows])
        return [item for _, item in rows]

    def clear(self, device_id: str, list_name: str) -> int:
        with self._db:
            cursor = self._db.execute("DELETE FROM notes WHERE device_id = ? AND list = ?", (device_id, list_name))
        return cursor.rowcount
