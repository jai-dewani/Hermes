"""Unit tests for Hermes RSS/Atom feed checker."""

import json
import os
import sys
import time
from unittest.mock import MagicMock, patch

import pytest

import hermes


# --- resolve_id ---

def test_resolve_id_priority():
    """Verify entry.id > entry.link > entry.title priority."""
    entry1 = {"id": "  https://example.com/item/1  ", "link": "https://example.com/item-alt", "title": "Post 1"}
    assert hermes.resolve_id(entry1) == "https://example.com/item/1"

    entry2 = {"link": " https://example.com/item/2 ", "title": "Post 2"}
    assert hermes.resolve_id(entry2) == "https://example.com/item/2"

    entry3 = {"title": "  Only Title  "}
    assert hermes.resolve_id(entry3) == "Only Title"

    assert hermes.resolve_id({}) == ""


# --- State loading & migration ---

def test_load_seen_valid_new_format(tmp_path):
    """Verify loading a valid seen.json with rich metadata format."""
    seen_file = tmp_path / "seen.json"
    data = {"articles": {
        "art1": {"first_seen": 1700000000, "sent_count": 1, "title": "Post A", "link": "https://a.com", "blog_name": "Blog A"},
    }}
    seen_file.write_text(json.dumps(data), encoding="utf-8")

    result = hermes.load_seen(str(seen_file))
    assert result["articles"]["art1"]["first_seen"] == 1700000000
    assert result["articles"]["art1"]["sent_count"] == 1
    assert result["articles"]["art1"]["title"] == "Post A"


def test_load_seen_migrates_legacy_timestamps(tmp_path):
    """Verify legacy timestamp-only entries are migrated to rich metadata."""
    seen_file = tmp_path / "seen.json"
    data = {"articles": {"art1": 1700000000, "art2": 1700000100}}
    seen_file.write_text(json.dumps(data), encoding="utf-8")

    result = hermes.load_seen(str(seen_file))
    for art_id in ("art1", "art2"):
        meta = result["articles"][art_id]
        assert isinstance(meta, dict)
        assert "first_seen" in meta
        assert meta["sent_count"] == 1  # Legacy entries assumed already sent once
        assert meta["title"] == ""      # No metadata available from legacy format


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


# --- State saving ---

def test_save_seen_atomic(tmp_path):
    """Verify atomic saving of seen.json."""
    seen_file = tmp_path / "seen.json"
    data = {"articles": {"post-1": {"first_seen": 1700000000, "sent_count": 1, "title": "X", "link": "", "blog_name": "B"}}}
    hermes.save_seen(str(seen_file), data)

    loaded = json.loads(seen_file.read_text(encoding="utf-8"))
    assert loaded == data


# --- Pruning ---

def test_prune_old_entries():
    """Verify pruning entries older than max_age_days using rich metadata."""
    now = int(time.time())
    one_day_ago = now - 86400
    twenty_days_ago = now - (20 * 86400)
    forty_days_ago = now - (40 * 86400)

    seen = {
        "articles": {
            "fresh": {"first_seen": one_day_ago, "sent_count": 1, "title": "", "link": "", "blog_name": ""},
            "retained": {"first_seen": twenty_days_ago, "sent_count": 0, "title": "", "link": "", "blog_name": ""},
            "expired": {"first_seen": forty_days_ago, "sent_count": 2, "title": "", "link": "", "blog_name": ""},
        }
    }

    hermes.prune_old_entries(seen, max_age_days=30)
    assert "fresh" in seen["articles"]
    assert "retained" in seen["articles"]
    assert "expired" not in seen["articles"]


# --- Config loading ---

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


# --- Notifications ---

@patch("requests.post")
def test_send_notification_new_article(mock_post):
    """Verify ntfy POST request formatting for new articles."""
    mock_post.return_value.status_code = 200

    config = {"ntfy_topic": "my-topic", "ntfy_server": "https://ntfy.sh"}
    entry = {"title": "Exciting News", "link": "https://example.com/post-1"}

    with patch.dict(os.environ, {"NTFY_TOKEN": "secret-123"}):
        success = hermes.send_notification(config, "My Blog", entry)

    assert success is True
    mock_post.assert_called_once()
    _, kwargs = mock_post.call_args
    assert kwargs["headers"]["Title"] == "My Blog"
    assert kwargs["headers"]["Tags"] == "newspaper"
    assert kwargs["headers"]["Click"] == "https://example.com/post-1"
    assert kwargs["headers"]["Authorization"] == "Bearer secret-123"
    assert kwargs["data"] == "Exciting News".encode("utf-8")


@patch("requests.post")
def test_send_notification_throwback(mock_post):
    """Verify throwback notifications use different title prefix and tag."""
    mock_post.return_value.status_code = 200

    config = {"ntfy_topic": "my-topic", "ntfy_server": "https://ntfy.sh"}
    entry = {"title": "Old Classic Post", "link": "https://example.com/old"}

    with patch.dict(os.environ, {}, clear=True):
        success = hermes.send_notification(config, "Cool Blog", entry, is_throwback=True)

    assert success is True
    _, kwargs = mock_post.call_args
    assert kwargs["headers"]["Title"] == "📚 Throwback: Cool Blog"
    assert kwargs["headers"]["Tags"] == "books"


# --- Throwback logic ---

@patch("requests.post")
def test_send_throwbacks_picks_eligible(mock_post):
    """Verify throwbacks only pick articles with sent_count < MAX and valid title."""
    mock_post.return_value.status_code = 200

    config = {"ntfy_topic": "test-topic", "ntfy_server": "https://ntfy.sh"}
    now = int(time.time())

    seen = {"articles": {
        "eligible-1": {"first_seen": now, "sent_count": 0, "title": "Great Post", "link": "https://a.com/1", "blog_name": "Blog A"},
        "eligible-2": {"first_seen": now, "sent_count": 1, "title": "Another Post", "link": "https://a.com/2", "blog_name": "Blog A"},
        "eligible-3": {"first_seen": now, "sent_count": 0, "title": "Third Post", "link": "https://a.com/3", "blog_name": "Blog B"},
        "maxed-out":  {"first_seen": now, "sent_count": 2, "title": "Old Post", "link": "https://a.com/4", "blog_name": "Blog C"},
        "no-title":   {"first_seen": now, "sent_count": 0, "title": "", "link": "https://a.com/5", "blog_name": "Blog D"},
    }}

    sent = hermes.send_throwbacks(config, seen)
    assert 2 <= sent <= 3

    # maxed-out should still be at 2
    assert seen["articles"]["maxed-out"]["sent_count"] == 2
    # no-title should still be at 0
    assert seen["articles"]["no-title"]["sent_count"] == 0


def test_send_throwbacks_no_eligible():
    """Verify no throwbacks when all articles are maxed out or have no titles."""
    config = {"ntfy_topic": "test-topic"}
    now = int(time.time())

    seen = {"articles": {
        "maxed": {"first_seen": now, "sent_count": 2, "title": "Post", "link": "", "blog_name": "Blog"},
        "empty": {"first_seen": now, "sent_count": 0, "title": "", "link": "", "blog_name": "Blog"},
    }}

    sent = hermes.send_throwbacks(config, seen)
    assert sent == 0


# --- Main flow integration ---

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
    # First-run articles should have sent_count=0 (never notified)
    assert seen["articles"]["item-1"]["sent_count"] == 0
    assert seen["articles"]["item-1"]["title"] == "First Post"
    assert seen["articles"]["item-1"]["blog_name"] == "Blog"


@patch("hermes.fetch_feed")
@patch("hermes.send_notification")
def test_main_subsequent_run_notifies_only_new(mock_notify, mock_fetch, tmp_path, monkeypatch):
    """Verify subsequent runs notify only new items and store rich metadata."""
    monkeypatch.chdir(tmp_path)

    now = int(time.time())
    existing = {"articles": {
        "item-1": {"first_seen": now, "sent_count": 1, "title": "Old Post", "link": "https://example.com/1", "blog_name": "Blog"},
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

    with pytest.raises(SystemExit) as exc_info:
        hermes.main()

    assert exc_info.value.code == 0
    assert mock_notify.call_count == 1
    args, kwargs = mock_notify.call_args
    assert args[1] == "Blog"

    seen = json.loads((tmp_path / "seen.json").read_text(encoding="utf-8"))
    assert "item-1" in seen["articles"]
    assert "item-2" in seen["articles"]
    assert seen["articles"]["item-2"]["sent_count"] == 1
    assert seen["articles"]["item-2"]["title"] == "Brand New Post"


@patch("hermes.send_throwbacks", return_value=3)
@patch("hermes.fetch_feed")
@patch("hermes.send_notification")
def test_main_no_new_articles_triggers_throwbacks(mock_notify, mock_fetch, mock_throwback, tmp_path, monkeypatch):
    """Verify throwbacks are triggered when no new articles are found."""
    monkeypatch.chdir(tmp_path)

    now = int(time.time())
    existing = {"articles": {
        "item-1": {"first_seen": now, "sent_count": 1, "title": "Existing Post", "link": "https://example.com/1", "blog_name": "Blog"},
    }}
    (tmp_path / "feeds.yaml").write_text(
        "ntfy_topic: test\nfeeds:\n  - name: Blog\n    url: https://example.com/feed\n",
        encoding="utf-8",
    )
    (tmp_path / "seen.json").write_text(json.dumps(existing), encoding="utf-8")

    # Return only already-seen articles
    mock_fetch.return_value = [
        {"id": "item-1", "title": "Existing Post", "link": "https://example.com/1"},
    ]

    with pytest.raises(SystemExit) as exc_info:
        hermes.main()

    assert exc_info.value.code == 0
    mock_notify.assert_not_called()  # No new article notifications
    mock_throwback.assert_called_once()  # Throwbacks triggered
