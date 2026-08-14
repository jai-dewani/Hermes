"""Unit tests for Hermes RSS/Atom feed checker."""

import json
import os
import sys
import time
from unittest.mock import MagicMock, patch

import pytest

import hermes


def test_resolve_id_priority():
    """Verify entry.id > entry.link > entry.title priority."""
    # 1. Has id, link, and title -> returns id
    entry1 = {"id": "  https://example.com/item/1  ", "link": "https://example.com/item-alt", "title": "Post 1"}
    assert hermes.resolve_id(entry1) == "https://example.com/item/1"

    # 2. Missing id -> returns link
    entry2 = {"link": " https://example.com/item/2 ", "title": "Post 2"}
    assert hermes.resolve_id(entry2) == "https://example.com/item/2"

    # 3. Missing id and link -> returns title
    entry3 = {"title": "  Only Title  "}
    assert hermes.resolve_id(entry3) == "Only Title"

    # 4. Completely empty -> returns empty string
    assert hermes.resolve_id({}) == ""


def test_load_seen_valid(tmp_path):
    """Verify loading a valid seen.json state file."""
    seen_file = tmp_path / "seen.json"
    data = {"articles": {"art1": 1700000000, "art2": 1700000100}}
    seen_file.write_text(json.dumps(data), encoding="utf-8")

    result = hermes.load_seen(str(seen_file))
    assert result == data


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


def test_save_seen_atomic(tmp_path):
    """Verify atomic saving of seen.json."""
    seen_file = tmp_path / "seen.json"
    data = {"articles": {"post-1": 1700000000}}
    hermes.save_seen(str(seen_file), data)

    loaded = json.loads(seen_file.read_text(encoding="utf-8"))
    assert loaded == data


def test_prune_old_entries():
    """Verify pruning entries older than max_age_days."""
    now = int(time.time())
    one_day_ago = now - 86400
    twenty_days_ago = now - (20 * 86400)
    forty_days_ago = now - (40 * 86400)

    seen = {
        "articles": {
            "fresh": one_day_ago,
            "retained": twenty_days_ago,
            "expired": forty_days_ago,
            "invalid_ts": "not-a-timestamp",
        }
    }

    hermes.prune_old_entries(seen, max_age_days=30)
    assert "fresh" in seen["articles"]
    assert "retained" in seen["articles"]
    assert "expired" not in seen["articles"]
    assert "invalid_ts" not in seen["articles"]


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


@patch("requests.post")
def test_send_notification(mock_post):
    """Verify ntfy POST request formatting."""
    mock_post.return_value.status_code = 200

    config = {
        "ntfy_topic": "my-topic",
        "ntfy_server": "https://ntfy.sh",
    }
    entry = {
        "title": "Exciting News",
        "link": "https://example.com/post-1",
    }

    with patch.dict(os.environ, {"NTFY_TOKEN": "secret-123"}):
        success = hermes.send_notification(config, "My Blog", entry)

    assert success is True
    mock_post.assert_called_once()
    args, kwargs = mock_post.call_args
    assert args[0] == "https://ntfy.sh/my-topic"
    assert kwargs["headers"]["Title"] == "My Blog"
    assert kwargs["headers"]["Click"] == "https://example.com/post-1"
    assert kwargs["headers"]["Tags"] == "newspaper"
    assert kwargs["headers"]["Authorization"] == "Bearer secret-123"
    assert kwargs["data"] == "Exciting News".encode("utf-8")


@patch("hermes.fetch_feed")
@patch("hermes.send_notification")
def test_main_first_run_populates_without_notifications(mock_notify, mock_fetch, tmp_path, monkeypatch):
    """Verify first run populates seen.json without firing notifications."""
    monkeypatch.chdir(tmp_path)

    # Setup feeds.yaml and empty seen.json
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


@patch("hermes.fetch_feed")
@patch("hermes.send_notification")
def test_main_subsequent_run_notifies_only_new(mock_notify, mock_fetch, tmp_path, monkeypatch):
    """Verify subsequent runs notify only new items."""
    monkeypatch.chdir(tmp_path)

    now = int(time.time())
    (tmp_path / "feeds.yaml").write_text(
        "ntfy_topic: test\nfeeds:\n  - name: Blog\n    url: https://example.com/feed\n",
        encoding="utf-8",
    )
    (tmp_path / "seen.json").write_text(
        json.dumps({"articles": {"item-1": now}}),
        encoding="utf-8",
    )

    mock_fetch.return_value = [
        {"id": "item-1", "title": "Old Post", "link": "https://example.com/1"},
        {"id": "item-2", "title": "Brand New Post", "link": "https://example.com/2"},
    ]

    with pytest.raises(SystemExit) as exc_info:
        hermes.main()

    assert exc_info.value.code == 0
    assert mock_notify.call_count == 1
    # Check that notification was sent for item-2
    args, _ = mock_notify.call_args
    assert args[1] == "Blog"
    assert args[2]["id"] == "item-2"

    seen = json.loads((tmp_path / "seen.json").read_text(encoding="utf-8"))
    assert "item-1" in seen["articles"]
    assert "item-2" in seen["articles"]
