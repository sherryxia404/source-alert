from pathlib import Path

import pytest

from source_alert import (
    Change,
    Item,
    Source,
    Store,
    check_source,
    load_sources,
    parse_feed,
    parse_webpage,
    send_ntfy,
    sms_text,
)


def feed(body: str, guid: str = "one") -> bytes:
    return f"""<?xml version="1.0"?><rss version="2.0"
    xmlns:content="http://purl.org/rss/1.0/modules/content/"><channel><item>
    <guid>{guid}</guid><title>Test alert</title><link>https://example.com/{guid}</link>
    <content:encoded><![CDATA[<p>{body}</p>]]></content:encoded>
    </item></channel></rss>""".encode()


def test_load_sources(tmp_path: Path):
    config = tmp_path / "sources.yaml"
    config.write_text("sources:\n  - name: Example\n    url: https://example.com\n")
    assert load_sources(config) == [Source("Example", "https://example.com")]


def test_rss_update_detection(tmp_path: Path):
    source = Source("Example", "https://example.com/feed", "rss")
    store = Store(tmp_path / "state.sqlite3")
    sent = []

    first = lambda _: parse_feed(feed("Original"), source)
    assert check_source(source, store, [("fake", sent.append)], fetcher=first) == 0
    assert not sent

    changed = lambda _: parse_feed(feed("UPDATE: reopened"), source)
    assert check_source(source, store, [("fake", sent.append)], fetcher=changed) == 1
    assert sent[0].kind == "updated"
    store.close()


def test_new_rss_item_notifies_after_baseline(tmp_path: Path):
    source = Source("Example", "https://example.com/feed", "rss")
    store = Store(tmp_path / "state.sqlite3")
    sent = []
    check_source(source, store, [("fake", sent.append)], fetcher=lambda _: parse_feed(feed("One"), source))
    check_source(source, store, [("fake", sent.append)], fetcher=lambda _: parse_feed(feed("Two", "two"), source))
    assert sent[0].kind == "new"
    store.close()


def test_unchanged_item_does_not_write_to_database(tmp_path: Path):
    source = Source("Example", "https://example.com/feed", "rss")
    store = Store(tmp_path / "state.sqlite3")
    item = parse_feed(feed("Same content"), source)[0]
    store.observe(item)
    changes_before = store.db.total_changes

    assert store.observe(item) is None
    assert store.db.total_changes == changes_before
    store.close()


def test_failed_delivery_remains_pending_and_only_failed_channel_retries(tmp_path: Path):
    source = Source("Example", "https://example.com/feed", "rss")
    store = Store(tmp_path / "state.sqlite3")
    check_source(
        source,
        store,
        [],
        fetcher=lambda _: parse_feed(feed("Original"), source),
    )

    successful_attempts = []
    flaky_attempts = []

    def successful(change):
        successful_attempts.append(change)

    def flaky(change):
        flaky_attempts.append(change)
        if len(flaky_attempts) == 1:
            raise RuntimeError("temporary delivery failure")

    changed = lambda _: parse_feed(feed("UPDATE: reopened"), source)
    channels = [("successful", successful), ("flaky", flaky)]

    with pytest.raises(RuntimeError, match="remains pending for retry"):
        check_source(source, store, channels, fetcher=changed)

    assert store.detect(changed(source)[0]).kind == "updated"
    assert check_source(source, store, channels, fetcher=changed) == 1
    assert len(successful_attempts) == 1
    assert len(flaky_attempts) == 2
    assert store.detect(changed(source)[0]) is None
    store.close()


def test_ntfy_json_publish_supports_unicode_title(monkeypatch):
    captured = {}

    class Response:
        def raise_for_status(self):
            return None

    def fake_post(url, **kwargs):
        captured["url"] = url
        captured.update(kwargs)
        return Response()

    monkeypatch.setattr("source_alert.requests.post", fake_post)
    item = Item(
        "UW Seattle Alert",
        "one",
        "UW Advisory – Emergency response",
        "Update includes 中文 and an em dash — safely.",
        "https://example.com/one",
        "",
        "hash",
    )

    send_ntfy(Change("new", item), "https://ntfy.sh/", "private-topic")

    assert captured["url"] == "https://ntfy.sh"
    assert captured["json"]["topic"] == "private-topic"
    assert captured["json"]["title"] == (
        "New · UW Seattle Alert: UW Advisory – Emergency response"
    )
    assert "中文" in captured["json"]["message"]
    assert "headers" not in captured


def test_webpage_selector():
    source = Source("Status", "https://example.com", "webpage", "#status")
    item = parse_webpage(b"<title>Site</title><div id='status'>All good</div>", source)[0]
    assert item.title == "Site"
    assert item.body == "All good"


def test_sms_contains_source_title_and_link():
    item = Item(
        "UW Seattle Alert",
        "one",
        "Test alert",
        "Details",
        "https://example.com/one",
        "",
        "hash",
    )
    message = sms_text(Change("updated", item))
    assert "UPDATED" in message
    assert "UW Seattle Alert" in message
    assert "Test alert" in message
    assert "https://example.com/one" in message
