#!/usr/bin/env python3
"""Monitor public sources and send meaningful changes to your phone."""

from __future__ import annotations

import argparse
import hashlib
import logging
import os
import re
import sqlite3
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Iterable
from urllib.parse import urljoin

import feedparser
import requests
import yaml
from bs4 import BeautifulSoup
from dotenv import load_dotenv


LOGGER = logging.getLogger("source-alert")


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def clean_html(value: str) -> str:
    text = BeautifulSoup(value or "", "html.parser").get_text(" ", strip=True)
    text = re.sub(r"\s+", " ", text).strip()
    return re.sub(r"\s+([,.;:!?])", r"\1", text)


def make_hash(title: str, body: str) -> str:
    return hashlib.sha256(f"{title.strip()}\n{body.strip()}".encode()).hexdigest()


@dataclass(frozen=True)
class Source:
    name: str
    url: str
    kind: str = "auto"
    selector: str | None = None
    enabled: bool = True


@dataclass(frozen=True)
class Item:
    source: str
    external_id: str
    title: str
    body: str
    link: str
    published_at: str
    content_hash: str


@dataclass(frozen=True)
class Change:
    kind: str  # new or updated
    item: Item


def load_sources(path: str | Path) -> list[Source]:
    with open(path, encoding="utf-8") as file:
        document = yaml.safe_load(file) or {}

    sources = []
    for raw in document.get("sources", []):
        if not raw.get("name") or not raw.get("url"):
            raise ValueError("Every source needs both name and url")
        kind = str(raw.get("type", "auto")).lower()
        if kind not in {"auto", "rss", "webpage"}:
            raise ValueError(f"Unsupported source type: {kind}")
        sources.append(
            Source(
                name=str(raw["name"]),
                url=str(raw["url"]),
                kind=kind,
                selector=raw.get("selector"),
                enabled=bool(raw.get("enabled", True)),
            )
        )
    return [source for source in sources if source.enabled]


def parse_feed(payload: bytes | str, source: Source) -> list[Item]:
    parsed = feedparser.parse(payload)
    if parsed.bozo and not parsed.entries:
        raise ValueError(f"Could not parse feed: {parsed.bozo_exception}")

    items = []
    for entry in parsed.entries:
        title = clean_html(entry.get("title", "Untitled update"))
        link = str(entry.get("link", source.url)).strip()
        external_id = str(entry.get("id") or link).strip()
        if not external_id:
            continue
        raw_body = (
            entry.content[0].get("value", "")
            if entry.get("content")
            else entry.get("summary", "")
        )
        body = clean_html(raw_body)
        published = str(entry.get("updated") or entry.get("published") or "")
        items.append(
            Item(
                source=source.name,
                external_id=external_id,
                title=title,
                body=body,
                link=link,
                published_at=published,
                content_hash=make_hash(title, body),
            )
        )
    return items


def parse_webpage(payload: bytes | str, source: Source) -> list[Item]:
    soup = BeautifulSoup(payload, "html.parser")
    for element in soup.select("script, style, noscript, svg"):
        element.decompose()

    if source.selector:
        selected = soup.select_one(source.selector)
        if selected is None:
            raise ValueError(f"CSS selector did not match: {source.selector}")
    else:
        selected = soup.find("main") or soup.find("article") or soup.body or soup

    title = clean_html(soup.title.get_text() if soup.title else source.name)
    body = clean_html(str(selected))
    return [
        Item(
            source=source.name,
            external_id=source.url,
            title=title,
            body=body,
            link=source.url,
            published_at="",
            content_hash=make_hash(title, body),
        )
    ]


def request_url(url: str, timeout: int = 25) -> requests.Response:
    response = requests.get(
        url,
        timeout=timeout,
        headers={"User-Agent": "SourceAlert/1.0 (personal public-page monitor)"},
    )
    response.raise_for_status()
    return response


def fetch_source(
    source: Source,
    requester: Callable[[str], requests.Response] = request_url,
) -> list[Item]:
    response = requester(source.url)
    payload = response.content
    content_type = response.headers.get("content-type", "").lower()

    if source.kind == "rss" or "xml" in content_type or payload.lstrip().startswith(b"<?xml"):
        return parse_feed(payload, source)

    if source.kind == "auto":
        soup = BeautifulSoup(payload, "html.parser")
        alternate = soup.select_one(
            'link[rel="alternate"][type="application/rss+xml"], '
            'link[rel="alternate"][type="application/atom+xml"]'
        )
        if alternate and alternate.get("href"):
            feed_url = urljoin(source.url, str(alternate["href"]))
            return parse_feed(requester(feed_url).content, source)

    return parse_webpage(payload, source)


class Store:
    def __init__(self, path: str | Path):
        self.path = str(path)
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(self.path)
        self.db.row_factory = sqlite3.Row
        self.db.executescript(
            """
            CREATE TABLE IF NOT EXISTS items (
                source TEXT NOT NULL,
                external_id TEXT NOT NULL,
                title TEXT NOT NULL,
                body TEXT NOT NULL,
                link TEXT NOT NULL,
                published_at TEXT NOT NULL,
                content_hash TEXT NOT NULL,
                first_seen_at TEXT NOT NULL,
                last_seen_at TEXT NOT NULL,
                PRIMARY KEY (source, external_id)
            );
            CREATE TABLE IF NOT EXISTS versions (
                source TEXT NOT NULL,
                external_id TEXT NOT NULL,
                content_hash TEXT NOT NULL,
                title TEXT NOT NULL,
                body TEXT NOT NULL,
                observed_at TEXT NOT NULL,
                UNIQUE (source, external_id, content_hash)
            );
            CREATE TABLE IF NOT EXISTS deliveries (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                source TEXT NOT NULL,
                external_id TEXT NOT NULL,
                content_hash TEXT NOT NULL,
                channel TEXT NOT NULL,
                delivered_at TEXT NOT NULL,
                success INTEGER NOT NULL,
                error TEXT
            );
            """
        )
        self.db.commit()

    def has_source(self, source: str) -> bool:
        row = self.db.execute(
            "SELECT 1 FROM items WHERE source = ? LIMIT 1", (source,)
        ).fetchone()
        return row is not None

    def observe(self, item: Item) -> Change | None:
        now = utc_now()
        current = self.db.execute(
            "SELECT content_hash FROM items WHERE source = ? AND external_id = ?",
            (item.source, item.external_id),
        ).fetchone()

        if current is None:
            kind = "new"
            self.db.execute(
                "INSERT INTO items VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    item.source,
                    item.external_id,
                    item.title,
                    item.body,
                    item.link,
                    item.published_at,
                    item.content_hash,
                    now,
                    now,
                ),
            )
        elif current["content_hash"] != item.content_hash:
            kind = "updated"
            self.db.execute(
                """
                UPDATE items SET title=?, body=?, link=?, published_at=?,
                    content_hash=?, last_seen_at=?
                WHERE source=? AND external_id=?
                """,
                (
                    item.title,
                    item.body,
                    item.link,
                    item.published_at,
                    item.content_hash,
                    now,
                    item.source,
                    item.external_id,
                ),
            )
        else:
            # Keep the database byte-for-byte stable when nothing changed. This
            # lets the free GitHub Actions deployment persist state only when a
            # source actually changes, instead of creating a commit every run.
            return None

        self.db.execute(
            "INSERT OR IGNORE INTO versions VALUES (?, ?, ?, ?, ?, ?)",
            (
                item.source,
                item.external_id,
                item.content_hash,
                item.title,
                item.body,
                now,
            ),
        )
        self.db.commit()
        return Change(kind, item)

    def record_delivery(
        self, change: Change, channel: str, success: bool, error: str | None = None
    ) -> None:
        self.db.execute(
            """
            INSERT INTO deliveries
            (source, external_id, content_hash, channel, delivered_at, success, error)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                change.item.source,
                change.item.external_id,
                change.item.content_hash,
                channel,
                utc_now(),
                int(success),
                error,
            ),
        )
        self.db.commit()

    def close(self) -> None:
        self.db.close()


def excerpt(item: Item, limit: int = 500) -> str:
    text = item.body or "Open the source for details."
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def send_ntfy(change: Change, server: str, topic: str) -> None:
    verb = "Updated" if change.kind == "updated" else "New"
    response = requests.post(
        f"{server.rstrip('/')}/{topic}",
        data=excerpt(change.item).encode(),
        headers={
            "Title": f"{verb} · {change.item.source}: {change.item.title}",
            "Priority": "high",
            "Tags": "bell",
            "Click": change.item.link,
        },
        timeout=20,
    )
    response.raise_for_status()


def send_telegram(change: Change, token: str, chat_id: str) -> None:
    verb = "UPDATED" if change.kind == "updated" else "NEW"
    text = (
        f"{verb} · {change.item.source}\n{change.item.title}\n\n"
        f"{excerpt(change.item, 2500)}\n\n{change.item.link}"
    )
    response = requests.post(
        f"https://api.telegram.org/bot{token}/sendMessage",
        json={"chat_id": chat_id, "text": text, "disable_web_page_preview": True},
        timeout=20,
    )
    response.raise_for_status()


def sms_text(change: Change, limit: int = 1450) -> str:
    verb = "UPDATED" if change.kind == "updated" else "NEW"
    text = (
        f"SourceAlert {verb} · {change.item.source}\n"
        f"{change.item.title}\n{change.item.link}"
    )
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def send_twilio_sms(
    change: Change,
    account_sid: str,
    auth_token: str,
    from_number: str,
    to_number: str,
) -> None:
    response = requests.post(
        f"https://api.twilio.com/2010-04-01/Accounts/{account_sid}/Messages.json",
        data={
            "From": from_number,
            "To": to_number,
            "Body": sms_text(change),
        },
        auth=(account_sid, auth_token),
        timeout=20,
    )
    response.raise_for_status()


def notifiers() -> list[tuple[str, Callable[[Change], None]]]:
    result = []
    topic = os.getenv("NTFY_TOPIC", "").strip()
    if topic:
        server = os.getenv("NTFY_SERVER", "https://ntfy.sh").strip()
        result.append(("ntfy", lambda change: send_ntfy(change, server, topic)))
    token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
    chat_id = os.getenv("TELEGRAM_CHAT_ID", "").strip()
    if token and chat_id:
        result.append(("telegram", lambda change: send_telegram(change, token, chat_id)))

    twilio_sid = os.getenv("TWILIO_ACCOUNT_SID", "").strip()
    twilio_token = os.getenv("TWILIO_AUTH_TOKEN", "").strip()
    from_number = os.getenv("TWILIO_FROM_NUMBER", "").strip()
    to_number = os.getenv("SMS_TO_NUMBER", "").strip()
    twilio_values = [twilio_sid, twilio_token, from_number, to_number]
    if any(twilio_values) and not all(twilio_values):
        raise ValueError(
            "Twilio SMS requires TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN, "
            "TWILIO_FROM_NUMBER, and SMS_TO_NUMBER"
        )
    if all(twilio_values):
        result.append(
            (
                "twilio-sms",
                lambda change: send_twilio_sms(
                    change,
                    twilio_sid,
                    twilio_token,
                    from_number,
                    to_number,
                ),
            )
        )
    return result


def check_source(
    source: Source,
    store: Store,
    channels: Iterable[tuple[str, Callable[[Change], None]]],
    notify_on_first_run: bool = False,
    fetcher: Callable[[Source], list[Item]] = fetch_source,
) -> int:
    first_run = not store.has_source(source.name)
    changes = []
    for item in reversed(fetcher(source)):
        change = store.observe(item)
        if change:
            changes.append(change)

    if first_run and not notify_on_first_run:
        LOGGER.info("%s: baseline saved (%d items)", source.name, len(changes))
        return 0

    sent = 0
    for change in changes:
        LOGGER.info("%s: %s · %s", source.name, change.kind, change.item.title)
        for channel, notify in channels:
            try:
                notify(change)
                store.record_delivery(change, channel, True)
                sent += 1
            except Exception as exc:
                store.record_delivery(change, channel, False, str(exc))
                LOGGER.exception("%s delivery failed", channel)
    return sent


def main() -> int:
    load_dotenv()
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--notify-on-first-run", action="store_true")
    parser.add_argument("--test-notification", action="store_true")
    parser.add_argument("--config", default=os.getenv("SOURCES_FILE", "sources.yaml"))
    args = parser.parse_args()

    logging.basicConfig(
        level=os.getenv("LOG_LEVEL", "INFO").upper(),
        format="%(asctime)s %(levelname)s %(message)s",
    )
    interval = int(os.getenv("CHECK_INTERVAL_SECONDS", "300"))
    if interval < 60:
        raise ValueError("CHECK_INTERVAL_SECONDS must be at least 60")

    channels = notifiers()
    if not channels:
        LOGGER.warning("No notification channel configured; changes will be logged only.")

    if args.test_notification:
        item = Item(
            "SourceAlert",
            "test",
            "SourceAlert is connected",
            "This is a test. Future source changes will appear here.",
            "https://github.com/sherryxia404/source-alert",
            utc_now(),
            "test",
        )
        for name, notify in channels:
            notify(Change("new", item))
            LOGGER.info("Test sent via %s", name)
        return 0

    sources = load_sources(args.config)
    if not sources:
        raise ValueError("No enabled sources found in the configuration")
    store = Store(os.getenv("DATABASE_PATH", "data/source_alert.sqlite3"))
    try:
        while True:
            for source in sources:
                try:
                    check_source(
                        source,
                        store,
                        channels,
                        notify_on_first_run=args.notify_on_first_run,
                    )
                except Exception:
                    LOGGER.exception("%s check failed", source.name)
            if args.once:
                break
            LOGGER.info("Next check in %d seconds", interval)
            time.sleep(interval)
    except KeyboardInterrupt:
        LOGGER.info("Stopped")
    finally:
        store.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
