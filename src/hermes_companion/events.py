"""Versioned events shared by the companion engines."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any
from uuid import uuid4


@dataclass(frozen=True, slots=True)
class CompanionEvent:
    event_type: str
    user_id: str
    timestamp: datetime
    source: str
    payload: dict[str, Any]
    event_id: str = field(default_factory=lambda: str(uuid4()))
    correlation_id: str = field(default_factory=lambda: str(uuid4()))
    schema_version: int = 1
    causation_id: str | None = None
    idempotency_key: str | None = None

    def __post_init__(self) -> None:
        if not self.event_type.strip():
            raise ValueError("event_type must not be empty")
        if not self.user_id.strip():
            raise ValueError("user_id must not be empty")
        if not self.source.strip():
            raise ValueError("source must not be empty")
        if self.timestamp.tzinfo is None or self.timestamp.utcoffset() is None:
            raise ValueError("timestamp must be timezone-aware")
        if self.schema_version < 1:
            raise ValueError("schema_version must be positive")

    @classmethod
    def from_storage(cls, row: dict[str, Any]) -> "CompanionEvent":
        import json

        return cls(
            event_id=row["event_id"],
            event_type=row["event_type"],
            user_id=row["user_id"],
            timestamp=datetime.fromisoformat(row["event_timestamp"]),
            source=row["source"],
            payload=json.loads(row["payload_json"]),
            correlation_id=row["correlation_id"],
            schema_version=row["schema_version"],
            causation_id=row["causation_id"],
            idempotency_key=row["idempotency_key"],
        )
