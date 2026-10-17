"""Ingest new Hermes messages into the durable companion event outbox."""

from __future__ import annotations

from datetime import datetime, timezone

from .events import CompanionEvent
from .controls import CompanionControls
from .hermes_sessiondb import HermesSessionReader
from .store import EventStore


class SessionDbIngestor:
    def __init__(
        self,
        reader: HermesSessionReader,
        store: EventStore,
        *,
        consumer_name: str = "hermes-weixin",
        user_id: str = "primary-user",
        controls: CompanionControls | None = None,
    ):
        self.reader = reader
        self.store = store
        self.consumer_name = consumer_name
        self.user_id = user_id
        self.controls = controls

    def poll_once(self, limit: int = 100) -> int:
        cursor = self.store.get_cursor(self.consumer_name)
        messages = self.reader.read_after(cursor, limit=limit)
        inserted = 0
        for message in messages:
            is_user_message = message.role == "user"
            event = CompanionEvent(
                event_type=("USER_MESSAGE_RECEIVED" if is_user_message else "ASSISTANT_MESSAGE_SENT"),
                user_id=self.user_id,
                timestamp=datetime.fromtimestamp(message.timestamp, tz=timezone.utc),
                source="hermes-sessiondb:weixin",
                payload=(
                    {"text": message.text, "platform_message_id": message.platform_message_id}
                    if is_user_message else {"platform_message_id": message.platform_message_id}
                ),
                idempotency_key=f"hermes-weixin-message:{message.row_id}",
            )
            if self.controls and is_user_message:
                self.controls.apply_message(event)
            if self.store.publish_and_advance(
                event, self.consumer_name, message.row_id
            ):
                inserted += 1
        return inserted
