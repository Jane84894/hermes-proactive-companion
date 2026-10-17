"""Low-frequency GitHub and configured RSS discovery using public metadata only."""

from __future__ import annotations

import json
import re
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from urllib.parse import quote_plus, urlparse
from urllib.request import Request, urlopen
from uuid import uuid4
from xml.etree import ElementTree

from .store import EventStore


class DiscoveryWorker:
    def __init__(
        self,
        store: EventStore,
        *,
        interval_hours: int = 24,
        github_enabled: bool = True,
        rss_feeds: tuple[str, ...] = (),
        timeout_seconds: int = 8,
        max_items_per_run: int = 10,
    ):
        if interval_hours < 1 or timeout_seconds < 1 or max_items_per_run < 1:
            raise ValueError("discovery limits must be positive")
        self.store = store
        self.db = store._db
        self.interval_hours = interval_hours
        self.github_enabled = github_enabled
        self.rss_feeds = tuple(self._validate_feed_url(url) for url in rss_feeds)
        self.timeout_seconds = timeout_seconds
        self.max_items_per_run = max_items_per_run

    def run_once(self, *, now: datetime | None = None) -> dict[str, object]:
        current = now or datetime.now(timezone.utc)
        if current.tzinfo is None or current.utcoffset() is None:
            raise ValueError("now must be timezone-aware")
        last = self.db.execute(
            "SELECT last_run_at FROM worker_runs WHERE worker_name='discovery'"
        ).fetchone()
        if last and self._timestamp(last[0]) + timedelta(hours=self.interval_hours) > current:
            return {"state": "not_due", "created": 0}
        topics = self.db.execute(
            "SELECT id, title, interest_score FROM topics WHERE status='open' "
            "AND followup_allowed=1 ORDER BY interest_score DESC, last_active_at DESC LIMIT 5"
        ).fetchall()
        feeds = [(str(row["id"]), str(row["title"]), float(row["interest_score"])) for row in topics]
        if not feeds or not (self.github_enabled or self.rss_feeds):
            self._finish(current, None)
            return {"state": "no_sources", "created": 0}
        created = 0
        errors: list[str] = []
        rss_items: list[dict[str, str | None]] = []
        for feed_url in self.rss_feeds:
            try:
                rss_items.extend(self._rss_items(feed_url))
            except (OSError, ValueError, ElementTree.ParseError) as exc:
                errors.append(type(exc).__name__)
        remaining = self.max_items_per_run
        for topic_id, title, score in feeds:
            if remaining <= 0:
                break
            try:
                items = []
                if self.github_enabled:
                    items.extend(self._github_items(title))
                topic_words = self._words(title)
                items.extend(
                    item for item in rss_items
                    if topic_words.intersection(self._words(str(item["title"])))
                )
                for item in items[:remaining]:
                    if self._save_item(topic_id, score, item, current):
                        created += 1
                        remaining -= 1
            except (OSError, ValueError, ElementTree.ParseError, json.JSONDecodeError) as exc:
                errors.append(type(exc).__name__)
        error_code = ",".join(sorted(set(errors)))[:160] if errors else None
        self._finish(current, error_code)
        return {"state": "completed" if not errors else "completed_with_errors", "created": created}

    def _github_items(self, interest: str) -> list[dict[str, str | None]]:
        query = quote_plus(f'"{interest}" in:name,description')
        request = Request(
            f"https://api.github.com/search/repositories?q={query}&sort=updated&order=desc&per_page=5",
            headers={"Accept": "application/vnd.github+json", "User-Agent": "Hermes-Proactive-Companion"},
        )
        with urlopen(request, timeout=self.timeout_seconds) as response:
            body = response.read(1_000_001)
        if len(body) > 1_000_000:
            raise ValueError("GitHub response exceeded size limit")
        payload = json.loads(body)
        items: list[dict[str, str | None]] = []
        for repo in payload.get("items", []):
            html_url = repo.get("html_url")
            full_name = repo.get("full_name")
            if not isinstance(html_url, str) or not isinstance(full_name, str):
                continue
            if urlparse(html_url).scheme != "https" or urlparse(html_url).hostname != "github.com":
                continue
            items.append({
                "source": "github",
                "external_id": str(repo.get("id", html_url)),
                "title": self._clean(full_name, 120),
                "url": html_url,
                "summary": "",  # Repository descriptions are untrusted and are not sent to the model.
                "published_at": repo.get("updated_at") if isinstance(repo.get("updated_at"), str) else None,
            })
        return items

    def _rss_items(self, feed_url: str) -> list[dict[str, str | None]]:
        request = Request(feed_url, headers={"User-Agent": "Hermes-Proactive-Companion/1.0"})
        with urlopen(request, timeout=self.timeout_seconds) as response:
            body = response.read(1_000_001)
        if len(body) > 1_000_000:
            raise ValueError("RSS response exceeded size limit")
        root = ElementTree.fromstring(body)
        items: list[dict[str, str | None]] = []
        for node in root.iter():
            if node.tag.rsplit("}", 1)[-1] not in {"item", "entry"}:
                continue
            values = {child.tag.rsplit("}", 1)[-1]: child for child in list(node)}
            title = self._clean(values.get("title").text if values.get("title") is not None else "", 160)
            link_node = values.get("link")
            url = (link_node.get("href") if link_node is not None else None) or (
                link_node.text if link_node is not None else None
            )
            if not title or not isinstance(url, str) or urlparse(url).scheme != "https":
                continue
            identity = (values.get("guid") or values.get("id"))
            external_id = identity.text.strip() if identity is not None and identity.text else url
            date_node = values.get("pubDate") or values.get("published") or values.get("updated")
            published = date_node.text.strip() if date_node is not None and date_node.text else None
            if published:
                try:
                    parsed = parsedate_to_datetime(published)
                except (TypeError, ValueError, OverflowError):
                    try:
                        parsed = datetime.fromisoformat(published.replace("Z", "+00:00"))
                    except ValueError:
                        parsed = None
                published = parsed.astimezone(timezone.utc).isoformat() if parsed else None
            items.append({
                "source": "rss:" + str(urlparse(feed_url).hostname),
                "external_id": external_id[:500],
                "title": title,
                "url": url,
                "summary": "",  # Feed bodies are deliberately excluded from prompts.
                "published_at": published,
            })
        return items

    def _save_item(
        self, topic_id: str, score: float, item: dict[str, str | None], now: datetime
    ) -> bool:
        source = str(item["source"])
        url = str(item["url"])
        if not self._trusted_url(source, url):
            return False
        cursor = self.db.execute(
            "INSERT OR IGNORE INTO discoveries "
            "(id, user_id, topic_id, source, external_id, title, url, summary, published_at, "
            "interest_score, expires_at) VALUES (?, 'primary-user', ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                str(uuid4()), topic_id, source, str(item["external_id"]),
                self._clean(str(item["title"]), 160), url, "", item["published_at"],
                max(0.0, min(1.0, score)),
                (now + timedelta(days=14)).astimezone(timezone.utc).isoformat(),
            ),
        )
        self.db.commit()
        return cursor.rowcount == 1

    def _finish(self, now: datetime, error_code: str | None) -> None:
        self.db.execute(
            "INSERT INTO worker_runs(worker_name, last_run_at, last_error_code) "
            "VALUES ('discovery', ?, ?) ON CONFLICT(worker_name) DO UPDATE SET "
            "last_run_at=excluded.last_run_at, last_error_code=excluded.last_error_code",
            (now.astimezone(timezone.utc).isoformat(), error_code),
        )
        self.db.commit()

    def _trusted_url(self, source: str, url: str) -> bool:
        parsed = urlparse(url)
        if parsed.scheme != "https" or not parsed.hostname:
            return False
        if source == "github":
            return parsed.hostname == "github.com"
        return parsed.hostname in {urlparse(feed).hostname for feed in self.rss_feeds}

    @staticmethod
    def _validate_feed_url(url: str) -> str:
        parsed = urlparse(url)
        if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
            raise ValueError("RSS feeds must use HTTPS and must not include credentials")
        return url

    @staticmethod
    def _clean(value: str, limit: int) -> str:
        return re.sub(r"[\x00-\x1f\x7f]", " ", value).strip()[:limit]

    @staticmethod
    def _words(value: str) -> set[str]:
        return {
            word for word in re.findall(r"[a-z0-9]{3,}|[\u4e00-\u9fff]{2,}", value.casefold())
            if word not in {"and", "the", "for", "with", "from"}
        }

    @staticmethod
    def _timestamp(value: str) -> datetime:
        parsed = datetime.fromisoformat(value)
        return parsed.replace(tzinfo=timezone.utc) if parsed.tzinfo is None else parsed.astimezone(timezone.utc)
