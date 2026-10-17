"""Read inbound messages from Hermes SessionDB without modifying it."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True, slots=True)
class InboundMessage:
    row_id: int
    session_id: str
    role: str
    platform_message_id: str | None
    user_id: str
    text: str
    timestamp: float


class HermesSessionReader:
    """Read one user's Weixin messages using Hermes' source timestamps."""

    def __init__(self, db_path: str | Path, target_user_id: str):
        self.db_path = Path(db_path).expanduser().resolve()
        if not target_user_id.strip():
            raise ValueError("target_user_id must not be empty")
        self.target_user_id = target_user_id

    def _connect(self) -> sqlite3.Connection:
        if not self.db_path.is_file():
            raise FileNotFoundError(self.db_path)
        connection = sqlite3.connect(
            f"{self.db_path.as_uri()}?mode=ro", uri=True, timeout=5.0
        )
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA query_only = ON")
        return connection

    def read_after(self, cursor: int, limit: int = 100) -> list[InboundMessage]:
        if cursor < 0:
            raise ValueError("cursor must not be negative")
        if limit < 1:
            raise ValueError("limit must be positive")
        with self._connect() as connection:
            self._check_schema(connection)
            rows = connection.execute(
                """
                SELECT m.id AS row_id, m.session_id, m.role, m.platform_message_id,
                       s.user_id, m.content AS text, m.timestamp
                FROM messages AS m
                JOIN sessions AS s ON s.id = m.session_id
                WHERE s.source = 'weixin'
                  AND m.role IN ('user', 'assistant')
                  AND (s.user_id = ? OR s.chat_id = ?)
                  AND m.id > ?
                ORDER BY m.id
                LIMIT ?
                """,
                (self.target_user_id, self.target_user_id, cursor, limit),
            ).fetchall()
        return [
            InboundMessage(
                row_id=int(row["row_id"]),
                session_id=str(row["session_id"]),
                role=str(row["role"]),
                platform_message_id=row["platform_message_id"],
                user_id=self.target_user_id,
                text=str(row["text"] or ""),
                timestamp=float(row["timestamp"]),
            )
            for row in rows
        ]

    @staticmethod
    def _check_schema(connection: sqlite3.Connection) -> None:
        required = {
            "sessions": {"id", "source", "user_id", "chat_id"},
            "messages": {
                "id", "session_id", "role", "content", "timestamp",
                "platform_message_id",
            },
        }
        for table, expected in required.items():
            actual = {
                row["name"]
                for row in connection.execute(f'PRAGMA table_info("{table}")')
            }
            missing = expected - actual
            if missing:
                raise RuntimeError(
                    f"Hermes SessionDB schema mismatch in {table}: missing "
                    + ", ".join(sorted(missing))
                )
