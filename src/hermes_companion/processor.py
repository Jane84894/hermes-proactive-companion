"""Claim, extract, persist, and redact source-message events."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from collections.abc import Callable
import re

from .budget import BudgetExceeded
from .companion_state import CompanionStateRepository
from .controls import CompanionControls
from .events import CompanionEvent
from .extraction import ExtractionResult, MemoryExtractor
from .extraction_store import ExtractionStore
from .store import EventStore


@dataclass(frozen=True, slots=True)
class ProcessingReport:
    processed: int = 0
    failed: int = 0
    deferred: int = 0
    terminal_failures: int = 0
    created: dict[str, int] | None = None


class EventProcessor:
    _NONMEMORABLE_ACKS = {
        "嗯", "嗯嗯", "哦", "哦哦", "啊", "好", "好的", "好呀", "可以", "行", "行吧",
        "收到", "知道了", "明白", "谢谢", "谢谢你", "谢啦", "哈哈", "哈哈哈",
        "hhh", "lol", "hi", "hello", "你好", "早安", "晚安", "加油",
    }

    def __init__(
        self,
        store: EventStore,
        extractor: MemoryExtractor,
        *,
        worker_id: str,
        retry_delay_seconds: int = 60,
        max_attempts: int = 5,
        response_recorder: Callable[[CompanionEvent], object] | None = None,
    ):
        if not worker_id.strip():
            raise ValueError("worker_id must not be empty")
        self.store = store
        self.extractor = extractor
        self.worker_id = worker_id
        self.retry_delay_seconds = retry_delay_seconds
        self.max_attempts = max_attempts
        self.response_recorder = response_recorder
        self.persistence = ExtractionStore(store)
        self.controls = CompanionControls(store)
        self.companion_state = CompanionStateRepository(store)

    def process_once(
        self, *, limit: int = 1, now: datetime | None = None
    ) -> ProcessingReport:
        claimed = self.store.claim_events(
            self.worker_id,
            now=now,
            lease_seconds=300,
            limit=limit,
            event_types=("USER_MESSAGE_RECEIVED", "ASSISTANT_MESSAGE_SENT"),
        )
        counts = {"memories": 0, "episodes": 0, "events": 0, "topics": 0}
        processed = failed = deferred = terminal = 0
        for index, event in enumerate(claimed):
            try:
                if event.event_type == "ASSISTANT_MESSAGE_SENT":
                    processed += int(
                        self.store.mark_processed(
                            event.event_id, redact_payload=True, owner=self.worker_id
                        )
                    )
                    continue
                if self.response_recorder is not None:
                    self.response_recorder(event)
                self.controls.apply_message(event)
                message_text = event.payload.get("text")
                if self._is_nonmemorable_ack(message_text):
                    result = ExtractionResult((), (), (), ())
                else:
                    result = self.extractor.extract(event)
                persisted = self.persistence.persist(
                    user_id=event.user_id,
                    source_message_id=event.idempotency_key or event.event_id,
                    source=event.source,
                    source_timestamp=event.timestamp,
                    extraction=result,
                )
                self.companion_state.refresh(event.user_id, now=now)
                if self.store.mark_processed(
                    event.event_id, redact_payload=True, owner=self.worker_id
                ):
                    processed += 1
                    for key, value in persisted.created.items():
                        counts[key] += value
            except BudgetExceeded as exc:
                for deferred_event in claimed[index:]:
                    deferred += int(
                        self.store.defer_claim(
                            deferred_event.event_id,
                            owner=self.worker_id,
                            retry_at=exc.retry_at,
                            reason_code="BudgetExceeded",
                        )
                    )
                break
            except Exception as exc:
                failed += 1
                validation_failure = isinstance(exc, ValueError)
                is_terminal = self.store.fail_claim(
                    event.event_id,
                    owner=self.worker_id,
                    error_code="OutputValidationError" if validation_failure else type(exc).__name__,
                    now=now,
                    max_attempts=min(self.max_attempts, 3) if validation_failure else self.max_attempts,
                    retry_delay_seconds=900 if validation_failure else self.retry_delay_seconds,
                )
                terminal += int(is_terminal)
        return ProcessingReport(
            processed=processed,
            failed=failed,
            deferred=deferred,
            terminal_failures=terminal,
            created=counts,
        )

    @classmethod
    def _is_nonmemorable_ack(cls, value: object) -> bool:
        if not isinstance(value, str):
            return False
        normalized = re.sub(r"[\s,.!?，。！？、~～…]+", "", value).casefold()
        return normalized in cls._NONMEMORABLE_ACKS
