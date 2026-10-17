"""Composition root for the proactive companion service."""

from __future__ import annotations

import logging
import json
import os
import signal
import socket
import threading
from dataclasses import asdict, dataclass
from datetime import datetime, time as datetime_time
from pathlib import Path

from .budget import BudgetedModel
from .companion_agent import CompanionAgent
from .config import AppConfig
from .delivery import ActionDispatcher, HermesCLISender
from .discovery import DiscoveryWorker
from .extraction import MemoryExtractor
from .hermes_env import load_hermes_environment
from .hermes_sessiondb import HermesSessionReader
from .ingest import SessionDbIngestor
from .model import MimoModel
from .memory import MemoryRepository
from .processor import EventProcessor
from .shadow import ShadowDecisionRunner
from .social import SocialPolicy
from .store import EventStore
from .thought_queue import ThoughtQueueRepository


@dataclass(frozen=True, slots=True)
class TickReport:
    ingested: int
    processed: int
    failed: int
    deferred: int
    shadow_decision: dict[str, object] | None = None
    delivery_result: dict[str, object] | None = None
    discovery_result: dict[str, object] | None = None


class CompanionRuntime:
    def __init__(self, config_path: str | Path):
        self.config = AppConfig.load(config_path)
        if not self.config.enabled:
            raise RuntimeError("companion is disabled in configuration")
        if self.config.mode not in {"shadow", "live"}:
            raise RuntimeError("mode must be shadow or live")
        self.environment = load_hermes_environment(self.config.hermes_env_file)
        self.reader = HermesSessionReader(
            self.config.hermes_state_db, self.environment.target_user_id
        )
        self.store = EventStore(self.config.companion_db)
        self.discovery = DiscoveryWorker(
            self.store,
            interval_hours=self.config.discovery_interval_hours,
            github_enabled=self.config.github_discovery_enabled,
            rss_feeds=self.config.discovery_rss_feeds,
        )
        self.model = MimoModel(api_key=self.environment.api_key)
        social_policy = SocialPolicy(
            timezone=self.config.timezone,
            quiet_hours_start=datetime_time.fromisoformat(self.config.quiet_hours_start),
            quiet_hours_end=datetime_time.fromisoformat(self.config.quiet_hours_end),
            max_daily_messages=self.config.max_daily_proactive_messages,
            min_interval_minutes=self.config.min_message_interval_minutes,
            post_reply_cooldown_minutes=self.config.post_reply_cooldown_minutes,
            light_chat_after_hours=self.config.light_chat_after_hours,
        )
        self.dispatcher = ActionDispatcher(
            self.store,
            social_policy,
            HermesCLISender(
                self.config.hermes_cli,
                target_user_id=self.environment.target_user_id,
                allowlisted_user_id=self.environment.target_user_id,
            ),
            enabled=self.config.outbound_delivery_enabled,
            target_user_id=self.environment.target_user_id,
            allowlisted_user_id=self.environment.target_user_id,
            auto_approve=self.config.mode == "live",
        )
        self.dispatcher.recover_interrupted_sends()
        budgeted_model = BudgetedModel(
            self.model,
            self.store,
            daily_token_budget=self.config.daily_token_budget,
            monthly_cost_budget_usd=self.config.monthly_cost_budget_usd,
            timezone=self.config.timezone,
        )
        extractor = MemoryExtractor(budgeted_model, timezone=self.config.timezone)
        companion_model = BudgetedModel(
            self.model,
            self.store,
            daily_token_budget=self.config.daily_token_budget,
            monthly_cost_budget_usd=self.config.monthly_cost_budget_usd,
            timezone=self.config.timezone,
            purpose="companion_draft",
        )
        self.processor = EventProcessor(
            self.store, extractor, worker_id=f"{socket.gethostname()}-{os.getpid()}",
            response_recorder=self.dispatcher.record_user_response,
        )
        self.ingestor = SessionDbIngestor(
            self.reader, self.store, user_id="primary-user",
            controls=self.processor.controls,
        )
        self.shadow_decisions = ShadowDecisionRunner(
            self.store,
            social_policy,
            self.config.evaluation_interval_minutes,
            max_thought_queue_items=self.config.max_thought_queue_items,
            max_thought_tasks_per_day=self.config.max_thought_tasks_per_day,
            max_thought_attempts=self.config.max_thought_attempts,
            companion_agent=CompanionAgent(
                companion_model, persona_profile=self.config.persona_profile
            ),
        )

    def close(self) -> None:
        self.store.close()

    def doctor(self) -> dict[str, str]:
        self.reader.read_after(0, limit=1)
        return {
            "config": "ok",
            "hermes_sessiondb": "readable",
            "weixin_target": "allowlisted (redacted)",
            "mimo_key": "present (redacted)",
            "companion_db": "ready",
            "database_schema": str(self.store.schema_version),
            "mode": self.config.mode,
            "outbound_delivery": "enabled" if self.config.outbound_delivery_enabled else "disabled",
            "discovery": "enabled" if self.config.discovery_enabled else "disabled",
        }

    def status(self) -> dict[str, int | str]:
        db = self.store._db
        counts = {
            table: int(db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
            for table in ("memories", "episodes", "followup_events", "topics")
        }
        counts["pending_events"] = int(
            db.execute("SELECT COUNT(*) FROM event_outbox WHERE status='pending'").fetchone()[0]
        )
        counts["processed_events"] = int(
            db.execute("SELECT COUNT(*) FROM event_outbox WHERE status='processed'").fetchone()[0]
        )
        counts["shadow_decisions"] = int(db.execute("SELECT COUNT(*) FROM social_decisions").fetchone()[0])
        counts["shadow_selected"] = int(db.execute("SELECT COUNT(*) FROM social_decisions WHERE selected=1").fetchone()[0])
        counts["drafts_generated"] = int(db.execute("SELECT COUNT(*) FROM social_decisions WHERE draft_status='generated'").fetchone()[0])
        counts["actions_awaiting_review"] = int(
            db.execute("SELECT COUNT(*) FROM proactive_actions WHERE state='draft_ready'").fetchone()[0]
        )
        counts["actions_delivery_unknown"] = int(
            db.execute("SELECT COUNT(*) FROM proactive_actions WHERE state='delivery_unknown'").fetchone()[0]
        )
        counts["discoveries_new"] = int(
            db.execute("SELECT COUNT(*) FROM discoveries WHERE status='new'").fetchone()[0]
        )
        counts["model_tokens_today"] = self._tokens_today()
        counts["model_cost_estimate_month_usd"] = f"{self._estimated_cost_month():.6f}"
        counts["monthly_cost_budget_usd"] = f"{self.config.monthly_cost_budget_usd:.2f}"
        counts["mode"] = self.config.mode
        counts["outbound_delivery"] = "enabled" if self.config.outbound_delivery_enabled else "disabled"
        counts["discovery"] = "enabled" if self.config.discovery_enabled else "disabled"
        state = self.shadow_decisions.controls.state("primary-user")
        counts["proactive_paused"] = bool(state.get("proactive_paused", False))
        counts["busy_until"] = str(state.get("busy_until", ""))
        dynamic = self.shadow_decisions.companion_state.get("primary-user")
        interests = dynamic.get("current_interests", [])
        counts["dynamic_interest_count"] = len(interests) if isinstance(interests, list) else 0
        counts["pending_thought_count"] = int(db.execute(
            "SELECT COUNT(*) FROM thought_queue WHERE user_id='primary-user' "
            "AND status IN ('candidate','pending','ready','selected')"
        ).fetchone()[0])
        return counts

    def tick(self, *, now: datetime | None = None) -> TickReport:
        ingested = self.ingestor.poll_once(limit=100)
        processed = self.processor.process_once(limit=10, now=now)
        ignored_marked = self.dispatcher.update_feedback_state(now=now)
        discovery_result = self.discovery.run_once(now=now) if self.config.discovery_enabled else None
        shadow_decision = self.shadow_decisions.evaluate(
            now=now or datetime.now().astimezone()
        )
        delivery_result = (
            self.dispatcher.dispatch_one(now=now)
            if self.config.outbound_delivery_enabled else None
        )
        if ignored_marked and delivery_result is None:
            delivery_result = {"state": "feedback_updated", "ignored_count": ignored_marked}
        return TickReport(
            ingested, processed.processed, processed.failed, processed.deferred,
            shadow_decision, delivery_result, discovery_result,
        )

    def run_forever(self, *, interval_seconds: int = 30) -> None:
        if interval_seconds < 5:
            raise ValueError("poll interval must be at least five seconds")
        stopping = threading.Event()
        previous_handler = signal.getsignal(signal.SIGTERM)
        signal.signal(signal.SIGTERM, lambda *_: stopping.set())
        logging.info(
            "companion started in %s mode; outbound delivery %s",
            self.config.mode,
            "enabled" if self.config.outbound_delivery_enabled else "disabled",
        )
        try:
            while not stopping.is_set():
                try:
                    report = self.tick()
                    if (report.ingested or report.processed or report.failed or report.deferred
                            or report.shadow_decision or report.delivery_result or report.discovery_result):
                        logging.info(
                            "tick ingested=%d processed=%d failed=%d deferred=%d shadow=%s delivery=%s discovery=%s",
                            report.ingested, report.processed, report.failed, report.deferred,
                            "evaluated" if report.shadow_decision else "idle",
                            report.delivery_result.get("state") if report.delivery_result else "idle",
                            report.discovery_result.get("state") if report.discovery_result else "idle",
                        )
                except Exception as exc:
                    logging.error("tick failed (%s)", type(exc).__name__)
                stopping.wait(interval_seconds)
        finally:
            signal.signal(signal.SIGTERM, previous_handler)

    def _tokens_today(self) -> int:
        from datetime import time as datetime_time, timedelta
        from zoneinfo import ZoneInfo

        zone = ZoneInfo(self.config.timezone)
        now = datetime.now(zone)
        start = datetime.combine(now.date(), datetime_time.min, tzinfo=zone)
        return self.store.usage_between(start, start + timedelta(days=1))

    def _estimated_cost_month(self) -> float:
        from datetime import timedelta
        from zoneinfo import ZoneInfo

        zone = ZoneInfo(self.config.timezone)
        now = datetime.now(zone)
        start = datetime.combine(now.date().replace(day=1), datetime_time.min, tzinfo=zone)
        next_month = (start.date().replace(day=28) + timedelta(days=4)).replace(day=1)
        end = datetime.combine(next_month, datetime_time.min, tzinfo=zone)
        return self.store.usage_cost_between(
            start,
            end,
            input_usd_per_million=BudgetedModel.INPUT_USD_PER_MILLION,
            output_usd_per_million=BudgetedModel.OUTPUT_USD_PER_MILLION,
        )


def main(argv: list[str] | None = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(prog="hermes-companion")
    parser.add_argument("--config", default=str(Path.home() / ".hermes/companion/config.toml"))
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("doctor")
    subparsers.add_parser("status")
    subparsers.add_parser("once", help="ingest and process one batch")
    subparsers.add_parser("pause", help="pause proactive candidate evaluation")
    subparsers.add_parser("resume", help="resume proactive candidate evaluation")
    approve = subparsers.add_parser("approve-action", help="approve one reviewed message draft")
    approve.add_argument("action_id")
    reviews = subparsers.add_parser("reviews", help="show recent shadow drafts for human review")
    reviews.add_argument("--limit", type=int, default=10)
    memories = subparsers.add_parser("memories", help="list active structured memories")
    memories.add_argument("--limit", type=int, default=100)
    search = subparsers.add_parser("search", help="search memories, episodes, events, and topics")
    search.add_argument("query", nargs="?", help="keyword or phrase")
    search.add_argument("--since", dest="start_at", help="inclusive ISO-8601 time with offset")
    search.add_argument("--until", dest="end_at", help="exclusive ISO-8601 time with offset")
    search.add_argument("--limit", type=int, default=10, help="maximum results per record type")
    related = subparsers.add_parser("related", help="find memories related by source or explicit link")
    related.add_argument("memory_id")
    related.add_argument("--limit", type=int, default=10)
    forget = subparsers.add_parser("forget", help="soft-delete one memory by ID")
    forget.add_argument("memory_id")
    correct = subparsers.add_parser("correct", help="replace one incorrect memory")
    correct.add_argument("memory_id")
    correct.add_argument("content", nargs="+", help="corrected memory text")
    export = subparsers.add_parser("export", help="export structured companion data as JSON")
    export.add_argument("--output", default="-", help="destination file (created mode 0600) or - for stdout")
    thoughts = subparsers.add_parser("thoughts", help="inspect and manage the persistent thought queue")
    thought_commands = thoughts.add_subparsers(dest="thought_command", required=True)
    thought_list = thought_commands.add_parser("list")
    thought_list.add_argument("--status", help="comma-separated statuses")
    thought_list.add_argument("--limit", type=int, default=100)
    thought_search = thought_commands.add_parser("search")
    thought_search.add_argument("query", nargs="?", default="")
    thought_search.add_argument("--status", help="comma-separated statuses")
    thought_search.add_argument("--limit", type=int, default=20)
    thought_complete = thought_commands.add_parser("complete")
    thought_complete.add_argument("thought_id")
    thought_cancel = thought_commands.add_parser("cancel")
    thought_cancel.add_argument("thought_id")
    thought_delay = thought_commands.add_parser("delay")
    thought_delay.add_argument("thought_id")
    thought_delay.add_argument("until", help="timezone-aware ISO-8601 time")
    thought_update = thought_commands.add_parser("update")
    thought_update.add_argument("thought_id")
    thought_update.add_argument("--type", dest="thought_type")
    thought_update.add_argument("--topic")
    thought_update.add_argument("--reason")
    thought_update.add_argument("--action", dest="possible_action")
    thought_update.add_argument("--priority", type=float)
    thought_update.add_argument("--earliest-send-at")
    thought_update.add_argument("--expires-at")
    thought_merge = thought_commands.add_parser("merge")
    thought_merge.add_argument("target_id")
    thought_merge.add_argument("source_ids", nargs="+")
    run = subparsers.add_parser("run", help="run the shadow service")
    run.add_argument("--interval", type=int, default=30)
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )
    runtime = CompanionRuntime(args.config)
    try:
        if args.command == "doctor":
            for key, value in runtime.doctor().items():
                print(f"{key}: {value}")
        elif args.command == "status":
            for key, value in runtime.status().items():
                print(f"{key}: {value}")
        elif args.command == "once":
            report = runtime.tick()
            print(
                f"ingested={report.ingested} processed={report.processed} "
                f"failed={report.failed} deferred={report.deferred} "
                f"shadow_decision={report.shadow_decision} delivery={report.delivery_result}"
            )
        elif args.command in {"pause", "resume"}:
            runtime.shadow_decisions.set_paused(args.command == "pause")
            print(f"proactive_paused={args.command == 'pause'}")
        elif args.command == "approve-action":
            action = runtime.dispatcher.approve(args.action_id)
            delivery = "enabled" if runtime.config.outbound_delivery_enabled else "disabled"
            print(f"approved action_id={action.id} outbound_delivery={delivery}")
        elif args.command == "reviews":
            if args.limit < 1:
                raise ValueError("review limit must be positive")
            rows = runtime.store._db.execute(
                "SELECT d.evaluated_at, d.candidate_title, d.score, d.draft_status, d.draft_text, "
                "d.reason_codes_json, a.id AS action_id, a.state AS action_state "
                "FROM social_decisions d LEFT JOIN proactive_actions a "
                "ON a.candidate_id=d.candidate_id WHERE d.selected=1 "
                "ORDER BY d.id DESC LIMIT ?", (args.limit,),
            ).fetchall()
            for row in rows:
                print(
                    f"[{row['evaluated_at']}] {row['candidate_title']} "
                    f"score={row['score']} draft={row['draft_status']} "
                    f"action={row['action_id'] or '-'} state={row['action_state'] or '-'}"
                )
                if row["draft_text"]:
                    print(row["draft_text"])
                print(f"reasons={row['reason_codes_json']}")
        elif args.command == "memories":
            if args.limit < 1:
                raise ValueError("memory limit must be positive")
            for memory in MemoryRepository(runtime.store).list_active("primary-user", args.limit):
                print(
                    f"{memory.id}\t{memory.kind}\timportance={memory.importance:.2f}\t"
                    f"confidence={memory.confidence:.2f}\t{memory.content}"
                )
        elif args.command == "search":
            if args.limit < 1:
                raise ValueError("search limit must be positive")
            result = MemoryRepository(runtime.store).search_records(
                "primary-user", query=args.query, start_at=args.start_at,
                end_at=args.end_at, limit=args.limit,
            )
            print(json.dumps(result, ensure_ascii=False, indent=2))
        elif args.command == "related":
            if args.limit < 1:
                raise ValueError("related limit must be positive")
            result = MemoryRepository(runtime.store).related_memories(
                "primary-user", args.memory_id, args.limit
            )
            print(json.dumps(result, ensure_ascii=False, indent=2))
        elif args.command == "forget":
            deleted = MemoryRepository(runtime.store).delete("primary-user", args.memory_id)
            print("deleted" if deleted else "not_found")
        elif args.command == "correct":
            corrected = MemoryRepository(runtime.store).correct(
                "primary-user", args.memory_id, " ".join(args.content)
            )
            print(f"corrected={corrected.id}" if corrected else "not_found")
        elif args.command == "export":
            export_text = json.dumps(
                MemoryRepository(runtime.store).export_data("primary-user"),
                ensure_ascii=False,
                indent=2,
            ) + "\n"
            if args.output == "-":
                print(export_text, end="")
            else:
                destination = Path(args.output).expanduser()
                descriptor = os.open(
                    destination,
                    os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                    0o600,
                )
                with os.fdopen(descriptor, "w", encoding="utf-8") as export_file:
                    export_file.write(export_text)
                print(f"exported={destination}")
        elif args.command == "thoughts":
            if args.thought_command in {"list", "search"} and args.limit < 1:
                raise ValueError("thought limit must be positive")
            queue = ThoughtQueueRepository(
                runtime.store,
                max_queue_length=runtime.config.max_thought_queue_items,
                max_tasks_per_day=runtime.config.max_thought_tasks_per_day,
                max_attempts=runtime.config.max_thought_attempts,
                timezone_name=runtime.config.timezone,
            )
            if args.thought_command in {"list", "search"}:
                statuses = tuple(args.status.split(",")) if args.status else None
                records = (
                    queue.list("primary-user", statuses=statuses, limit=args.limit)
                    if args.thought_command == "list" else
                    queue.search("primary-user", query=args.query, statuses=statuses, limit=args.limit)
                )
                print(json.dumps(
                    [asdict(item) for item in records], ensure_ascii=False, indent=2,
                    default=lambda value: value.isoformat() if isinstance(value, datetime) else str(value),
                ))
            elif args.thought_command == "complete":
                print("completed" if queue.complete("primary-user", args.thought_id) else "not_transitionable")
            elif args.thought_command == "cancel":
                print("cancelled" if queue.cancel("primary-user", args.thought_id) else "not_transitionable")
            elif args.thought_command == "delay":
                until = datetime.fromisoformat(args.until)
                thought = queue.delay("primary-user", args.thought_id, until)
                print(f"delayed_until={thought.earliest_send_at.isoformat()}")
            elif args.thought_command == "update":
                changes = {
                    key: getattr(args, key)
                    for key in (
                        "thought_type", "topic", "reason", "possible_action", "priority"
                    )
                    if getattr(args, key) is not None
                }
                if args.earliest_send_at:
                    changes["earliest_send_at"] = datetime.fromisoformat(args.earliest_send_at)
                if args.expires_at:
                    changes["expires_at"] = datetime.fromisoformat(args.expires_at)
                if not changes:
                    raise ValueError("thought update requires at least one field")
                thought = queue.update("primary-user", args.thought_id, **changes)
                print(json.dumps(
                    asdict(thought), ensure_ascii=False, default=lambda value:
                    value.isoformat() if isinstance(value, datetime) else str(value),
                ))
            else:
                thought = queue.merge("primary-user", args.target_id, args.source_ids)
                print(json.dumps(
                    asdict(thought), ensure_ascii=False, default=lambda value:
                    value.isoformat() if isinstance(value, datetime) else str(value),
                ))
        else:
            runtime.run_forever(interval_seconds=args.interval)
    finally:
        runtime.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
