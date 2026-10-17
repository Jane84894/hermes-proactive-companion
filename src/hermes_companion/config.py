"""Strict, secret-free application configuration."""

from __future__ import annotations

import tomllib
from dataclasses import dataclass
from datetime import time
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


@dataclass(frozen=True, slots=True)
class AppConfig:
    enabled: bool
    mode: str
    outbound_delivery_enabled: bool
    timezone: str
    hermes_state_db: Path
    hermes_env_file: Path
    hermes_cli: Path
    companion_db: Path
    persona_profile: Path
    quiet_hours_start: str
    quiet_hours_end: str
    max_daily_proactive_messages: int
    min_message_interval_minutes: int
    post_reply_cooldown_minutes: int
    light_chat_after_hours: int
    evaluation_interval_minutes: int
    discovery_interval_hours: int
    discovery_enabled: bool
    github_discovery_enabled: bool
    discovery_rss_feeds: tuple[str, ...]
    max_thought_queue_items: int
    max_thought_tasks_per_day: int
    max_thought_attempts: int
    memory_enabled: bool
    memory_retention_days: int
    daily_token_budget: int
    monthly_cost_budget_usd: float
    log_level: str

    @classmethod
    def load(cls, path: str | Path) -> "AppConfig":
        config_path = Path(path).expanduser().resolve()
        try:
            raw = tomllib.loads(config_path.read_text(encoding="utf-8"))
        except FileNotFoundError as exc:
            raise FileNotFoundError("companion configuration file is missing") from exc
        except tomllib.TOMLDecodeError as exc:
            raise ValueError("companion configuration is invalid TOML") from exc
        companion = raw.get("companion", {})
        paths = raw.get("paths", {})
        social = raw.get("social", {})
        memory = raw.get("memory", {})
        budget = raw.get("budget", {})
        discovery = raw.get("discovery", {})
        thoughts = raw.get("thoughts", {})
        logging = raw.get("logging", {})

        mode = companion.get("mode", "shadow")
        if mode not in {"shadow", "live"}:
            raise ValueError("mode must be shadow or live")
        timezone_name = companion.get("timezone", "Asia/Shanghai")
        try:
            ZoneInfo(timezone_name)
        except (ZoneInfoNotFoundError, TypeError) as exc:
            raise ValueError("timezone must be a valid IANA timezone") from exc

        def clock_value(name: str, default: str) -> str:
            value = social.get(name, default)
            try:
                parsed = time.fromisoformat(value)
            except (TypeError, ValueError) as exc:
                raise ValueError(f"{name} must be HH:MM") from exc
            if parsed.second or parsed.microsecond or parsed.tzinfo:
                raise ValueError(f"{name} must be HH:MM")
            return parsed.strftime("%H:%M")

        def positive_int(mapping: dict, key: str, default: int, *, allow_zero=False) -> int:
            value = mapping.get(key, default)
            if isinstance(value, bool) or not isinstance(value, int):
                raise ValueError(f"{key} must be an integer")
            if value < 0 or (value == 0 and not allow_zero):
                raise ValueError(f"{key} must be {'non-negative' if allow_zero else 'positive'}")
            return value

        def bool_value(mapping: dict, key: str, default: bool) -> bool:
            value = mapping.get(key, default)
            if not isinstance(value, bool):
                raise ValueError(f"{key} must be boolean")
            return value

        outbound_delivery_enabled = bool_value(companion, "outbound_delivery_enabled", False)
        if mode == "live" and not outbound_delivery_enabled:
            raise ValueError("live mode requires outbound delivery to be enabled")

        daily_budget = positive_int(budget, "daily_token_budget", 600000)
        rss_feeds = discovery.get("rss_feeds", [])
        if not isinstance(rss_feeds, list) or any(not isinstance(url, str) for url in rss_feeds):
            raise ValueError("discovery.rss_feeds must be an array of URL strings")
        monthly_cost = budget.get("monthly_cost_budget_usd", 10.0)
        if isinstance(monthly_cost, bool) or not isinstance(monthly_cost, (int, float)) or monthly_cost <= 0:
            raise ValueError("monthly_cost_budget_usd must be positive")
        log_level = str(logging.get("level", "INFO")).upper()
        if log_level not in {"DEBUG", "INFO", "WARNING", "ERROR"}:
            raise ValueError("log level must be DEBUG, INFO, WARNING, or ERROR")
        persona = Path(companion.get("persona_profile", "persona.toml")).expanduser()
        if not persona.is_absolute():
            persona = config_path.parent / persona

        return cls(
            enabled=bool_value(companion, "enabled", True),
            mode=mode,
            outbound_delivery_enabled=outbound_delivery_enabled,
            timezone=timezone_name,
            hermes_state_db=config_path_value(paths, "hermes_state_db", "~/.hermes/state.db", config_path),
            hermes_env_file=config_path_value(paths, "hermes_env_file", "~/.hermes/.env", config_path),
            hermes_cli=config_path_value(
                paths, "hermes_cli", "~/.hermes/hermes-agent/.venv/bin/hermes", config_path
            ),
            companion_db=config_path_value(
                paths, "companion_db", "~/.hermes/companion/data/companion.sqlite3", config_path
            ),
            persona_profile=persona.resolve(),
            quiet_hours_start=clock_value("quiet_hours_start", "23:30"),
            quiet_hours_end=clock_value("quiet_hours_end", "08:30"),
            max_daily_proactive_messages=positive_int(
                social, "max_daily_proactive_messages", 5, allow_zero=True
            ),
            min_message_interval_minutes=positive_int(
                social, "min_message_interval_minutes", 90, allow_zero=True
            ),
            post_reply_cooldown_minutes=positive_int(
                social, "post_reply_cooldown_minutes", 60, allow_zero=True
            ),
            light_chat_after_hours=positive_int(
                social, "light_chat_after_hours", 24
            ),
            evaluation_interval_minutes=positive_int(
                social, "evaluation_interval_minutes", 30
            ),
            discovery_interval_hours=positive_int(
                social, "discovery_interval_hours", 24
            ),
            discovery_enabled=bool_value(discovery, "enabled", False),
            github_discovery_enabled=bool_value(discovery, "github_enabled", True),
            discovery_rss_feeds=tuple(rss_feeds),
            max_thought_queue_items=positive_int(thoughts, "max_queue_length", 100),
            max_thought_tasks_per_day=positive_int(thoughts, "max_tasks_per_day", 20),
            max_thought_attempts=positive_int(thoughts, "max_attempts", 3),
            memory_enabled=bool_value(memory, "enabled", True),
            memory_retention_days=positive_int(
                memory, "retention_days", 0, allow_zero=True
            ),
            daily_token_budget=daily_budget,
            monthly_cost_budget_usd=float(monthly_cost),
            log_level=log_level,
        )


def config_path_value(paths: dict, name: str, default: str, config_file: Path) -> Path:
    value = Path(paths.get(name, default)).expanduser()
    if not value.is_absolute():
        value = config_file.parent / value
    return value.resolve()
