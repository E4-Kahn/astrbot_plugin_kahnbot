"""SQLite 数据层：流水、记忆索引、唤醒配额、元信息。

约定
----
- 一个进程一个连接，所有写操作串行（事件循环内同步执行，WAL + busy_timeout 兜底）。
- 表结构变更走「加列」：`_EXPECTED_COLUMNS` 里登记期望列，启动时缺哪列补哪列，
  这样 0.x 阶段改 schema 不需要写迁移脚本，也不会把已有流水清掉。
- md 是记忆条目的权威，`fact_index` 只是索引；索引随时可从 md 重建。
"""

from __future__ import annotations

import sqlite3
import time
from pathlib import Path

SCHEMA_VERSION = 1

_SCHEMA = """
CREATE TABLE IF NOT EXISTS message (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    ts          INTEGER NOT NULL,
    umo         TEXT    NOT NULL,
    platform    TEXT    NOT NULL,
    group_id    TEXT,
    sender_id   TEXT    NOT NULL,
    sender_name TEXT,
    is_owner    INTEGER NOT NULL DEFAULT 0,
    is_bot      INTEGER NOT NULL DEFAULT 0,
    text        TEXT    NOT NULL,
    msg_id      TEXT
);
CREATE INDEX IF NOT EXISTS idx_msg_umo_ts ON message(umo, ts);
CREATE INDEX IF NOT EXISTS idx_msg_sender ON message(sender_id, ts);

CREATE TABLE IF NOT EXISTS fact_index (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    fact_id     TEXT    NOT NULL UNIQUE,
    text        TEXT    NOT NULL,
    hash        TEXT    NOT NULL,
    scope       TEXT    NOT NULL,
    source      TEXT    NOT NULL DEFAULT 'owner',
    visibility  TEXT    NOT NULL DEFAULT 'private',
    created_ts  INTEGER NOT NULL,
    last_hit_ts INTEGER,
    pinned      INTEGER NOT NULL DEFAULT 0,
    archived    INTEGER NOT NULL DEFAULT 0
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_fact_hash ON fact_index(hash);

CREATE TABLE IF NOT EXISTS wake_quota (
    user_id TEXT    NOT NULL,
    day     TEXT    NOT NULL,
    count   INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (user_id, day)
);

CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT
);
"""

# 表 -> {列名: 加列时的 DDL 片段}
_EXPECTED_COLUMNS: dict[str, dict[str, str]] = {
    "message": {
        "group_id": "TEXT",
        "sender_name": "TEXT",
        "is_owner": "INTEGER NOT NULL DEFAULT 0",
        "is_bot": "INTEGER NOT NULL DEFAULT 0",
        "msg_id": "TEXT",
    },
    "fact_index": {
        "source": "TEXT NOT NULL DEFAULT 'owner'",
        "visibility": "TEXT NOT NULL DEFAULT 'private'",
        "last_hit_ts": "INTEGER",
        "pinned": "INTEGER NOT NULL DEFAULT 0",
        "archived": "INTEGER NOT NULL DEFAULT 0",
    },
    "wake_quota": {
        "count": "INTEGER NOT NULL DEFAULT 0",
    },
}


class Store:
    """`kahnbot.db` 的薄封装。方法都是同步的，调用方在事件循环里直接 await 之外调用即可。"""

    def __init__(self, db_path: Path) -> None:
        self.db_path = Path(db_path)
        self._conn: sqlite3.Connection | None = None

    # ------------------------------------------------------------------ 连接

    def open(self) -> None:
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(self.db_path, timeout=5.0)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute("PRAGMA busy_timeout=5000")
        self._conn = conn
        self._ensure_schema()

    def close(self) -> None:
        if self._conn is not None:
            self._conn.close()
            self._conn = None

    @property
    def conn(self) -> sqlite3.Connection:
        if self._conn is None:
            raise RuntimeError("Store 尚未 open()")
        return self._conn

    def _ensure_schema(self) -> None:
        conn = self.conn
        conn.executescript(_SCHEMA)
        for table, columns in _EXPECTED_COLUMNS.items():
            existing = {
                row["name"] for row in conn.execute(f"PRAGMA table_info({table})")
            }
            for column, ddl in columns.items():
                if column not in existing:
                    conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {ddl}")
        conn.commit()
        if self.get_meta("schema_version") != str(SCHEMA_VERSION):
            self.set_meta("schema_version", str(SCHEMA_VERSION))

    # ------------------------------------------------------------------ meta

    def get_meta(self, key: str) -> str | None:
        row = self.conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
        return None if row is None else row["value"]

    def set_meta(self, key: str, value: str) -> None:
        self.conn.execute(
            "INSERT INTO meta(key, value) VALUES(?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, value),
        )
        self.conn.commit()

    # ------------------------------------------------------------------ 流水

    def add_message(
        self,
        *,
        ts: int,
        umo: str,
        platform: str,
        group_id: str | None,
        sender_id: str,
        sender_name: str,
        is_owner: bool,
        is_bot: bool,
        text: str,
        msg_id: str = "",
    ) -> int:
        cur = self.conn.execute(
            "INSERT INTO message(ts, umo, platform, group_id, sender_id, sender_name,"
            " is_owner, is_bot, text, msg_id) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                ts,
                umo,
                platform,
                group_id,
                sender_id,
                sender_name,
                int(is_owner),
                int(is_bot),
                text,
                msg_id,
            ),
        )
        self.conn.commit()
        return int(cur.lastrowid or 0)

    def count_messages(self) -> int:
        row = self.conn.execute("SELECT COUNT(*) AS n FROM message").fetchone()
        return int(row["n"])

    def list_messages(self, limit: int = 10, offset: int = 0) -> list[sqlite3.Row]:
        limit = max(1, min(int(limit), 100))
        return list(
            self.conn.execute(
                "SELECT * FROM message ORDER BY id DESC LIMIT ? OFFSET ?",
                (limit, max(0, int(offset))),
            )
        )

    def delete_message(self, row_id: int) -> bool:
        cur = self.conn.execute("DELETE FROM message WHERE id = ?", (int(row_id),))
        self.conn.commit()
        return cur.rowcount > 0

    def last_name_of(self, sender_id: str) -> str | None:
        """最近一次见到的昵称（拍一拍事件没有昵称，用它兜底，见 wake-design §3.2）。"""
        if not sender_id:
            return None
        row = self.conn.execute(
            "SELECT sender_name FROM message WHERE sender_id = ? AND sender_name != ''"
            " ORDER BY id DESC LIMIT 1",
            (sender_id,),
        ).fetchone()
        return None if row is None else str(row["sender_name"])


def now_ts() -> int:
    return int(time.time())


__all__ = ["Store", "SCHEMA_VERSION", "now_ts"]
