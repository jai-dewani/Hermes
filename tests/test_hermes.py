"""Unit tests for Hermes RSS/Atom feed checker."""

import json
import os
import sys
import time
from unittest.mock import MagicMock, patch

import pytest

import hermes


# ---------------------------------------------------------------------------
# resolve_id
# ---------------------------------------------------------------------------


def test_resolve_id_priority():
    """Verify entry.id > entry.link > entry.title priority."""
    entry1 = {"id": "  https://example.com/item/1  ", "link": "https://example.com/item-alt", "title": "Post 1"}
    assert hermes.resolve_id(entry1) == "https://example.com/item/1"

    entry2 = {"link": " https://example.com/item/2 ", "title": "Post 2"}
    assert hermes.resolve_id(entry2) == "https://example.com/item/2"

    entry3 = {"title": "  Only Title  "}
    assert hermes.resolve_id(entry3) == "Only Title"

    assert hermes.resolve_id({}) == ""


# ---------------------------------------------------------------------------
# State loading & migration
# ---------------------------------------------------------------------------


def test_load_seen_valid_new_format(tmp_path):
    """Verify loading a valid seen.json with current metadata format."""
    seen_file = tmp_path / "seen.json"
    data = {"articles": {
        "art1": {"first_seen": 1700000000, "title": "Post A", "link": "https://a.com", "blog_name": "Blog A"},
    }}
    seen_file.write_text(json.dumps(data), encoding="utf-8")

    result = hermes.load_seen(str(seen_file))
    assert result["articles"]["art1"]["first_seen"] == 1700000000
    assert result["articles"]["art1"]["title"] == "Post A"
    # sent_count must NOT be present in the migrated output
    assert "sent_count" not in result["articles"]["art1"]


def test_load_seen_migrates_legacy_timestamps(tmp_path):
    """Verify legacy timestamp-only entries are migrated — sent_count is dropped."""
    seen_file = tmp_path / "seen.json"
    data = {"articles": {"art1": 1700000000, "art2": 1700000100}}
    seen_file.write_text(json.dumps(data), encoding="utf-8")

    result = hermes.load_seen(str(seen_file))
    for art_id in ("art1", "art2"):
        meta = result["articles"][art_id]
        assert isinstance(meta, dict)
        assert "first_seen" in meta
        assert "sent_count" not in meta  # Removed with throwback
        assert meta["title"] == ""


def test_load_seen_migrates_old_format_drops_sent_count(tmp_path):
    """Verify entries with sent_count (throwback era) are migrated cleanly."""
    seen_file = tmp_path / "seen.json"
    data = {"articles": {
        "art1": {
            "first_seen": 1700000000,
            "sent_count": 2,           # Old throwback field — must be dropped
            "title": "Old Post",
            "link": "https://a.com",
            "blog_name": "Blog A",
        }
    }}
    seen_file.write_text(json.dumps(data), encoding="utf-8")

    result = hermes.load_seen(str(seen_file))
    meta = result["articles"]["art1"]
    assert meta["first_seen"] == 1700000000
    assert meta["title"] == "Old Post"
    assert "sent_count" not in meta


def test_load_seen_corrupt_and_missing(tmp_path):
    """Verify corrupted or missing seen.json falls back to empty articles structure."""
    missing_file = tmp_path / "nonexistent.json"
    assert hermes.load_seen(str(missing_file)) == {"articles": {}}

    corrupt_file = tmp_path / "corrupt.json"
    corrupt_file.write_text("{ malformed json ...", encoding="utf-8")
    assert hermes.load_seen(str(corrupt_file)) == {"articles": {}}

    invalid_structure_file = tmp_path / "invalid.json"
    invalid_structure_file.write_text(json.dumps(["not", "a", "dict"]), encoding="utf-8")
    assert hermes.load_seen(str(invalid_structure_file)) == {"articles": {}}


# ---------------------------------------------------------------------------
# State saving
# ---------------------------------------------------------------------------


def test_save_seen_atomic(tmp_path):
    """Verify atomic saving of seen.json."""
    seen_file = tmp_path / "seen.json"
    data = {"articles": {"post-1": {"first_seen": 1700000000, "title": "X", "link": "", "blog_name": "B"}}}
    hermes.save_seen(str(seen_file), data)

    loaded = json.loads(seen_file.read_text(encoding="utf-8"))
    assert loaded == data


# ---------------------------------------------------------------------------
# Pruning
# ---------------------------------------------------------------------------


def test_prune_old_entries():
    """Verify pruning entries older than max_age_days."""
    now = int(time.time())
    seen = {
        "articles": {
            "fresh":    {"first_seen": now - 86400,      "title": "", "link": "", "blog_name": ""},
            "retained": {"first_seen": now - 20 * 86400, "title": "", "link": "", "blog_name": ""},
            "expired":  {"first_seen": now - 40 * 86400, "title": "", "link": "", "blog_name": ""},
        }
    }
    hermes.prune_old_entries(seen, max_age_days=30)
    assert "fresh" in seen["articles"]
    assert "retained" in seen["articles"]
    assert "expired" not in seen["articles"]


# ---------------------------------------------------------------------------
# Config loading
# ---------------------------------------------------------------------------


def test_load_config_valid(tmp_path):
    """Verify valid YAML config loading."""
    cfg_file = tmp_path / "feeds.yaml"
    cfg_file.write_text(
        """
ntfy_topic: test-alerts
feeds:
  - name: Test Feed
    url: https://example.com/rss
""",
        encoding="utf-8",
    )
    config = hermes.load_config(str(cfg_file))
    assert config["ntfy_topic"] == "test-alerts"
    assert len(config["feeds"]) == 1
    assert config["feeds"][0]["name"] == "Test Feed"


def test_load_config_missing_required(tmp_path):
    """Verify sys.exit when required fields are missing."""
    cfg_file = tmp_path / "invalid.yaml"
    cfg_file.write_text("feeds: []\n", encoding="utf-8")
    with pytest.raises(SystemExit):
        hermes.load_config(str(cfg_file))


# ---------------------------------------------------------------------------
# Excerpt extraction
# ---------------------------------------------------------------------------


def test_extract_excerpt_from_summary():
    """Verify HTML stripping and whitespace collapse from entry.summary."""
    entry = {"summary": "<p>This is a <strong>great</strong> post about DNS.</p>"}
    result = hermes._extract_excerpt(entry)
    assert result == "This is a great post about DNS."
    assert "<" not in result


def test_extract_excerpt_truncates_at_word_boundary():
    """Verify truncation happens at a word boundary and appends ellipsis."""
    long_text = "word " * 60  # Way more than 200 chars
    entry = {"summary": long_text}
    result = hermes._extract_excerpt(entry, max_length=20)
    assert result.endswith("…")
    assert len(result) <= 25  # Some slack for word boundary


def test_extract_excerpt_from_content_fallback():
    """Verify fallback to entry.content[0].value when summary is absent."""
    class FakeEntry:
        summary = ""
        content = [{"value": "<p>Content from content field.</p>"}]

    result = hermes._extract_excerpt(FakeEntry())
    assert result == "Content from content field."


def test_extract_excerpt_empty():
    """Verify empty string returned when no summary or content exists."""
    assert hermes._extract_excerpt({}) == ""
    assert hermes._extract_excerpt({"summary": ""}) == ""


def test_extract_excerpt_decodes_html_entities():
    """Verify HTML entities like &amp; are decoded."""
    entry = {"summary": "Rocks &amp; Rails &lt;framework&gt;"}
    result = hermes._extract_excerpt(entry)
    assert "&amp;" not in result
    assert "Rocks & Rails" in result


# ---------------------------------------------------------------------------
# Notifications
# ---------------------------------------------------------------------------


@patch("requests.post")
def test_send_notification_new_article(mock_post):
    """Verify ntfy POST request formatting for new articles."""
    mock_post.return_value.status_code = 200

    config = {"ntfy_topic": "my-topic", "ntfy_server": "https://ntfy.sh"}
    entry = {"title": "Exciting News", "link": "https://example.com/post-1", "summary": ""}

    with patch.dict(os.environ, {"NTFY_TOKEN": "secret-123"}):
        success = hermes.send_notification(config, "My Blog", entry)

    assert success is True
    mock_post.assert_called_once()
    _, kwargs = mock_post.call_args
    assert kwargs["headers"]["Title"] == "My Blog"
    assert kwargs["headers"]["Tags"] == "newspaper"
    assert kwargs["headers"]["Click"] == "https://example.com/post-1"
    assert kwargs["headers"]["Authorization"] == "Bearer secret-123"
    assert kwargs["headers"]["Priority"] == "default"
    assert b"Exciting News" in kwargs["data"]


@patch("requests.post")
def test_send_notification_with_priority(mock_post):
    """Verify the Priority header is set correctly when priority is specified."""
    mock_post.return_value.status_code = 200

    config = {"ntfy_topic": "my-topic", "ntfy_server": "https://ntfy.sh"}
    entry = {"title": "Important Post", "link": "https://example.com/post"}

    with patch.dict(os.environ, {}, clear=True):
        hermes.send_notification(config, "High Pri Blog", entry, priority="high")

    _, kwargs = mock_post.call_args
    assert kwargs["headers"]["Priority"] == "high"


@patch("requests.post")
def test_send_notification_invalid_priority_falls_back_to_default(mock_post):
    """Verify an invalid priority value silently falls back to 'default'."""
    mock_post.return_value.status_code = 200

    config = {"ntfy_topic": "my-topic", "ntfy_server": "https://ntfy.sh"}
    entry = {"title": "Post", "link": "https://example.com/post"}

    with patch.dict(os.environ, {}, clear=True):
        hermes.send_notification(config, "Blog", entry, priority="INVALID_LEVEL")

    _, kwargs = mock_post.call_args
    assert kwargs["headers"]["Priority"] == "default"


@patch("requests.post")
def test_send_notification_body_includes_excerpt(mock_post):
    """Verify the notification body includes the article excerpt."""
    mock_post.return_value.status_code = 200

    config = {"ntfy_topic": "my-topic", "ntfy_server": "https://ntfy.sh"}
    entry = {
        "title": "My Great Post",
        "link": "https://example.com/post",
        "summary": "<p>This is a really interesting summary of the post.</p>",
    }

    with patch.dict(os.environ, {}, clear=True):
        hermes.send_notification(config, "Blog", entry)

    _, kwargs = mock_post.call_args
    body = kwargs["data"].decode("utf-8")
    assert "My Great Post" in body
    assert "interesting summary" in body


# ---------------------------------------------------------------------------
# Feed health status
# ---------------------------------------------------------------------------


def test_update_feed_status_success():
    """Verify a successful fetch sets last_success and resets consecutive_failures."""
    feed_status = {"last_updated": None, "feeds": {}}
    hermes.update_feed_status(feed_status, "Test Blog", "https://example.com/feed", success=True, article_count=5)

    entry = feed_status["feeds"]["Test Blog"]
    assert entry["consecutive_failures"] == 0
    assert entry["last_success"] is not None
    assert entry["last_error"] is None
    assert entry["article_count"] == 5


def test_update_feed_status_failure_increments():
    """Verify consecutive_failures increments on each failure."""
    feed_status = {"last_updated": None, "feeds": {}}
    hermes.update_feed_status(feed_status, "Test Blog", "https://example.com/feed", success=False, error="Timeout")
    hermes.update_feed_status(feed_status, "Test Blog", "https://example.com/feed", success=False, error="Timeout")

    entry = feed_status["feeds"]["Test Blog"]
    assert entry["consecutive_failures"] == 2
    assert entry["last_success"] is None
    assert entry["last_error"] == "Timeout"


def test_update_feed_status_recovery():
    """Verify recovery from failures resets consecutive_failures to 0."""
    feed_status = {"last_updated": None, "feeds": {
        "Test Blog": {
            "url": "https://example.com/feed",
            "consecutive_failures": 3,
            "last_success": None,
            "last_error": "503",
            "article_count": 0,
        }
    }}
    hermes.update_feed_status(feed_status, "Test Blog", "https://example.com/feed", success=True, article_count=10)

    entry = feed_status["feeds"]["Test Blog"]
    assert entry["consecutive_failures"] == 0
    assert entry["last_error"] is None
    assert entry["article_count"] == 10


def test_load_feed_status_missing(tmp_path):
    """Verify missing feed-status.json returns a clean default structure."""
    result = hermes.load_feed_status(str(tmp_path / "nonexistent.json"))
    assert result == {"last_updated": None, "feeds": {}}


# ---------------------------------------------------------------------------
# Main flow integration
# ---------------------------------------------------------------------------


@patch("hermes.fetch_feed")
@patch("hermes.send_notification")
def test_main_first_run_populates_without_notifications(mock_notify, mock_fetch, tmp_path, monkeypatch):
    """Verify first run populates seen.json with metadata without firing notifications."""
    monkeypatch.chdir(tmp_path)

    (tmp_path / "feeds.yaml").write_text(
        "ntfy_topic: test\nfeeds:\n  - name: Blog\n    url: https://example.com/feed\n",
        encoding="utf-8",
    )
    (tmp_path / "seen.json").write_text('{"articles": {}}', encoding="utf-8")

    mock_fetch.return_value = [
        {"id": "item-1", "title": "First Post", "link": "https://example.com/1"},
        {"id": "item-2", "title": "Second Post", "link": "https://example.com/2"},
    ]

    with pytest.raises(SystemExit) as exc_info:
        hermes.main()

    assert exc_info.value.code == 0
    mock_notify.assert_not_called()

    seen = json.loads((tmp_path / "seen.json").read_text(encoding="utf-8"))
    assert "item-1" in seen["articles"]
    assert "item-2" in seen["articles"]
    assert seen["articles"]["item-1"]["title"] == "First Post"
    assert seen["articles"]["item-1"]["blog_name"] == "Blog"
    assert "sent_count" not in seen["articles"]["item-1"]


@patch("hermes.fetch_feed")
@patch("hermes.send_notification")
def test_main_subsequent_run_notifies_only_new(mock_notify, mock_fetch, tmp_path, monkeypatch):
    """Verify subsequent runs notify only new items and store metadata correctly."""
    monkeypatch.chdir(tmp_path)

    now = int(time.time())
    existing = {"articles": {
        "item-1": {"first_seen": now, "title": "Old Post", "link": "https://example.com/1", "blog_name": "Blog"},
    }}
    (tmp_path / "feeds.yaml").write_text(
        "ntfy_topic: test\nfeeds:\n  - name: Blog\n    url: https://example.com/feed\n",
        encoding="utf-8",
    )
    (tmp_path / "seen.json").write_text(json.dumps(existing), encoding="utf-8")

    mock_fetch.return_value = [
        {"id": "item-1", "title": "Old Post", "link": "https://example.com/1"},
        {"id": "item-2", "title": "Brand New Post", "link": "https://example.com/2"},
    ]
    mock_notify.return_value = True

    with pytest.raises(SystemExit) as exc_info:
        hermes.main()

    assert exc_info.value.code == 0
    assert mock_notify.call_count == 1
    args, kwargs = mock_notify.call_args
    assert args[1] == "Blog"

    seen = json.loads((tmp_path / "seen.json").read_text(encoding="utf-8"))
    assert "item-1" in seen["articles"]
    assert "item-2" in seen["articles"]
    assert seen["articles"]["item-2"]["title"] == "Brand New Post"


@patch("hermes.fetch_feed")
@patch("hermes.send_notification")
def test_main_uses_feed_priority(mock_notify, mock_fetch, tmp_path, monkeypatch):
    """Verify per-feed priority is passed through to send_notification."""
    monkeypatch.chdir(tmp_path)

    now = int(time.time())
    existing = {"articles": {
        "item-1": {"first_seen": now, "title": "Old Post", "link": "https://example.com/1", "blog_name": "Blog"},
    }}
    (tmp_path / "feeds.yaml").write_text(
        "ntfy_topic: test\nfeeds:\n  - name: Blog\n    url: https://example.com/feed\n    priority: high\n",
        encoding="utf-8",
    )
    (tmp_path / "seen.json").write_text(json.dumps(existing), encoding="utf-8")

    mock_fetch.return_value = [
        {"id": "item-2", "title": "New Post", "link": "https://example.com/2"},
    ]
    mock_notify.return_value = True

    with pytest.raises(SystemExit):
        hermes.main()

    _, kwargs = mock_notify.call_args
    assert kwargs.get("priority") == "high"


@patch("hermes.fetch_feed")
@patch("hermes.send_notification")
def test_main_no_new_articles_no_throwback(mock_notify, mock_fetch, tmp_path, monkeypatch):
    """Verify no throwback notifications are ever sent (feature removed)."""
    monkeypatch.chdir(tmp_path)

    now = int(time.time())
    existing = {"articles": {
        "item-1": {"first_seen": now, "title": "Existing Post", "link": "https://example.com/1", "blog_name": "Blog"},
    }}
    (tmp_path / "feeds.yaml").write_text(
        "ntfy_topic: test\nfeeds:\n  - name: Blog\n    url: https://example.com/feed\n",
        encoding="utf-8",
    )
    (tmp_path / "seen.json").write_text(json.dumps(existing), encoding="utf-8")

    mock_fetch.return_value = [
        {"id": "item-1", "title": "Existing Post", "link": "https://example.com/1"},
    ]

    with pytest.raises(SystemExit) as exc_info:
        hermes.main()

    assert exc_info.value.code == 0
    mock_notify.assert_not_called()


@patch("hermes.fetch_feed")
@patch("hermes.send_notification")
def test_main_writes_run_summary(mock_notify, mock_fetch, tmp_path, monkeypatch):
    """Verify main() always writes a run-summary.md file."""
    monkeypatch.chdir(tmp_path)

    now = int(time.time())
    existing = {"articles": {
        "item-1": {"first_seen": now, "title": "Old Post", "link": "https://example.com/1", "blog_name": "Blog"},
    }}
    (tmp_path / "feeds.yaml").write_text(
        "ntfy_topic: test\nfeeds:\n  - name: Blog\n    url: https://example.com/feed\n",
        encoding="utf-8",
    )
    (tmp_path / "seen.json").write_text(json.dumps(existing), encoding="utf-8")

    mock_fetch.return_value = [
        {"id": "item-2", "title": "New Post", "link": "https://example.com/2"},
    ]
    mock_notify.return_value = True

    with pytest.raises(SystemExit):
        hermes.main()

    summary = (tmp_path / "run-summary.md").read_text(encoding="utf-8")
    assert "Hermes Run Report" in summary
    assert "New Post" in summary
    assert "Blog" in summary


@patch("hermes.fetch_feed")
@patch("hermes.send_notification")
def test_main_writes_feed_status(mock_notify, mock_fetch, tmp_path, monkeypatch):
    """Verify main() persists feed-status.json after a run."""
    monkeypatch.chdir(tmp_path)

    (tmp_path / "feeds.yaml").write_text(
        "ntfy_topic: test\nfeeds:\n  - name: Blog\n    url: https://example.com/feed\n",
        encoding="utf-8",
    )
    (tmp_path / "seen.json").write_text('{"articles": {}}', encoding="utf-8")

    mock_fetch.return_value = []

    with pytest.raises(SystemExit):
        hermes.main()

    status = json.loads((tmp_path / "feed-status.json").read_text(encoding="utf-8"))
    assert "Blog" in status["feeds"]
    assert status["feeds"]["Blog"]["consecutive_failures"] == 0
    assert status["last_updated"] is not None
