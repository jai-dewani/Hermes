#!/usr/bin/env python3
"""Hermes: RSS/Atom feed notification service powered by ntfy.sh and GitHub Actions."""

import json
import os
import pathlib
import random
import sys
import time
from typing import Any, Dict, List, Optional

import feedparser
import requests
import yaml

USER_AGENT = "Hermes/1.0 (RSS Reader; +https://github.com/)"
DEFAULT_TIMEOUT = 15
NOTIFICATION_TIMEOUT = 10
DEFAULT_MAX_AGE_DAYS = 30
MAX_THROWBACK_SENDS = 2
THROWBACK_COUNT_MIN = 2
THROWBACK_COUNT_MAX = 3


def load_config(config_path: str = "feeds.yaml") -> Dict[str, Any]:
    """Load and validate the YAML configuration file."""
    path = pathlib.Path(config_path)
    if not path.is_file():
        print(f"Error: Configuration file '{config_path}' not found.", file=sys.stderr)
        sys.exit(1)

    try:
        with open(path, "r", encoding="utf-8") as f:
            config = yaml.safe_load(f)
    except Exception as e:
        print(f"Error: Failed to parse '{config_path}': {e}", file=sys.stderr)
        sys.exit(1)

    if not isinstance(config, dict):
        print(f"Error: '{config_path}' must be a YAML mapping/dictionary.", file=sys.stderr)
        sys.exit(1)

    if "ntfy_topic" not in config or not config["ntfy_topic"]:
        print("Error: Missing required 'ntfy_topic' in configuration.", file=sys.stderr)
        sys.exit(1)

    if "feeds" not in config or not isinstance(config["feeds"], list):
        print("Error: Missing or invalid 'feeds' list in configuration.", file=sys.stderr)
        sys.exit(1)

    return config


def _migrate_article_entry(value: Any) -> Dict[str, Any]:
    """Migrate legacy timestamp-only entries to the rich metadata format."""
    if isinstance(value, (int, float)):
        return {
            "first_seen": int(value),
            "sent_count": 1,
            "title": "",
            "link": "",
            "blog_name": "",
        }
    if isinstance(value, dict) and "first_seen" in value:
        return value
    # Unrecognized format — treat as fresh with no metadata
    return {
        "first_seen": int(time.time()),
        "sent_count": 0,
        "title": "",
        "link": "",
        "blog_name": "",
    }


def load_seen(seen_path: str = "seen.json") -> Dict[str, Any]:
    """Load the seen articles state file. Migrates legacy formats automatically."""
    path = pathlib.Path(seen_path)
    if not path.is_file():
        return {"articles": {}}

    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict) and isinstance(data.get("articles"), dict):
            # Migrate any legacy entries (plain timestamp values)
            migrated = {}
            for art_id, value in data["articles"].items():
                migrated[art_id] = _migrate_article_entry(value)
            data["articles"] = migrated
            return data
        print(f"Warning: Corrupted structure in '{seen_path}'. Resetting state.", file=sys.stderr)
    except Exception as e:
        print(f"Warning: Failed to parse '{seen_path}' ({e}). Rebuilding state.", file=sys.stderr)

    return {"articles": {}}


def save_seen(seen_path: str, seen_data: Dict[str, Any]) -> None:
    """Save the seen articles state file atomically."""
    path = pathlib.Path(seen_path)
    temp_path = path.with_suffix(".tmp")
    with open(temp_path, "w", encoding="utf-8") as f:
        json.dump(seen_data, f, indent=2, sort_keys=True)
        f.write("\n")
    temp_path.replace(path)


def resolve_id(entry: Any) -> str:
    """Determine a unique, stable identifier for a feed entry."""
    entry_id = getattr(entry, "id", None) or (entry.get("id") if isinstance(entry, dict) else None)
    if entry_id and str(entry_id).strip():
        return str(entry_id).strip()

    entry_link = getattr(entry, "link", None) or (entry.get("link") if isinstance(entry, dict) else None)
    if entry_link and str(entry_link).strip():
        return str(entry_link).strip()

    entry_title = getattr(entry, "title", None) or (entry.get("title") if isinstance(entry, dict) else None)
    if entry_title and str(entry_title).strip():
        return str(entry_title).strip()

    return ""


def _extract_entry_field(entry: Any, field: str) -> str:
    """Safely extract a string field from a feedparser entry or dict."""
    value = getattr(entry, field, None) or (entry.get(field) if isinstance(entry, dict) else None)
    return str(value).strip() if value else ""


def fetch_feed(url: str, timeout: int = DEFAULT_TIMEOUT) -> List[Any]:
    """Fetch and parse feed entries using requests and feedparser."""
    headers = {"User-Agent": USER_AGENT}
    response = requests.get(url, headers=headers, timeout=timeout)
    response.raise_for_status()

    feed = feedparser.parse(response.content)
    if getattr(feed, "bozo", False) and not getattr(feed, "entries", None):
        bozo_exc = getattr(feed, "bozo_exception", "Unknown parse error")
        raise ValueError(f"Malformed feed content: {bozo_exc}")

    return getattr(feed, "entries", [])


def send_notification(
    config: Dict[str, Any],
    blog_name: str,
    entry: Any,
    timeout: int = NOTIFICATION_TIMEOUT,
    is_throwback: bool = False,
) -> bool:
    """Send an HTTP push notification via ntfy.sh."""
    topic = config["ntfy_topic"]
    server = config.get("ntfy_server", "https://ntfy.sh").rstrip("/")
    url = f"{server}/{topic}"

    title = _extract_entry_field(entry, "title") or "New article"
    link = _extract_entry_field(entry, "link")

    if is_throwback:
        ntfy_title = f"📚 Throwback: {blog_name}"
        tags = "books"
    else:
        ntfy_title = blog_name
        tags = "newspaper"

    headers = {
        "Title": ntfy_title,
        "Tags": tags,
    }
    if link:
        headers["Click"] = link

    token = os.environ.get("NTFY_TOKEN")
    if token:
        headers["Authorization"] = f"Bearer {token}"

    try:
        resp = requests.post(url, data=str(title).encode("utf-8"), headers=headers, timeout=timeout)
        resp.raise_for_status()
        return True
    except Exception as e:
        print(f"Warning: Failed to send notification for '{title}': {e}", file=sys.stderr)
        return False


def send_throwbacks(config: Dict[str, Any], seen: Dict[str, Any]) -> int:
    """Pick 2-3 random old articles (sent_count < MAX_THROWBACK_SENDS) and notify."""
    articles = seen.get("articles", {})

    # Filter to articles eligible for throwback (have metadata and sent_count < max)
    eligible = [
        (art_id, meta)
        for art_id, meta in articles.items()
        if isinstance(meta, dict)
        and meta.get("title")  # Must have title metadata to be useful
        and meta.get("sent_count", 0) < MAX_THROWBACK_SENDS
    ]

    if not eligible:
        print("No eligible articles for throwback.")
        return 0

    count = min(random.randint(THROWBACK_COUNT_MIN, THROWBACK_COUNT_MAX), len(eligible))
    picks = random.sample(eligible, count)
    sent = 0

    for art_id, meta in picks:
        entry = {"title": meta.get("title", ""), "link": meta.get("link", "")}
        blog_name = meta.get("blog_name", "Unknown Blog")
        print(f"Throwback: {blog_name} — {meta.get('title', art_id)}")

        if send_notification(config, blog_name, entry, is_throwback=True):
            articles[art_id]["sent_count"] = meta.get("sent_count", 0) + 1
            sent += 1

    return sent


def prune_old_entries(seen: Dict[str, Any], max_age_days: int = DEFAULT_MAX_AGE_DAYS) -> None:
    """Prune entries from the seen map that are older than max_age_days."""
    articles = seen.get("articles", {})
    cutoff = int(time.time()) - (max_age_days * 86400)
    seen["articles"] = {
        art_id: meta
        for art_id, meta in articles.items()
        if isinstance(meta, dict) and isinstance(meta.get("first_seen"), (int, float)) and meta["first_seen"] >= cutoff
    }


def main() -> None:
    """Main execution flow for Hermes feed checker."""
    config = load_config("feeds.yaml")
    seen = load_seen("seen.json")

    is_first_run = len(seen.get("articles", {})) == 0
    new_count = 0
    now_ts = int(time.time())

    feeds = config.get("feeds", [])
    for feed_info in feeds:
        if not isinstance(feed_info, dict):
            continue

        name = feed_info.get("name", "Unknown Feed")
        url = feed_info.get("url")

        if not url:
            print(f"Warning: Feed '{name}' is missing a URL. Skipping.", file=sys.stderr)
            continue

        print(f"Checking feed: {name} ({url})")

        try:
            entries = fetch_feed(url)
        except Exception as e:
            print(f"Warning: Failed to fetch feed '{name}': {e}", file=sys.stderr)
            continue

        for entry in entries:
            article_id = resolve_id(entry)
            if not article_id:
                print(f"Warning: Could not determine ID for an entry in '{name}'. Skipping.", file=sys.stderr)
                continue

            if article_id not in seen["articles"]:
                if not is_first_run:
                    entry_title = _extract_entry_field(entry, "title") or article_id
                    print(f"New article: {entry_title}")
                    send_notification(config, name, entry)
                    new_count += 1

                # Store rich metadata for throwback support
                seen["articles"][article_id] = {
                    "first_seen": now_ts,
                    "sent_count": 1 if not is_first_run else 0,
                    "title": _extract_entry_field(entry, "title"),
                    "link": _extract_entry_field(entry, "link"),
                    "blog_name": name,
                }

    # Throwback: if no new articles, not first run, and feature is enabled
    throwback_count = 0
    throwback_enabled = config.get("throwback_enabled", True)
    if new_count == 0 and not is_first_run and throwback_enabled:
        print("No new articles found. Sending throwbacks...")
        throwback_count = send_throwbacks(config, seen)

    max_age_days = config.get("max_age_days", DEFAULT_MAX_AGE_DAYS)
    prune_old_entries(seen, max_age_days=max_age_days)
    save_seen("seen.json", seen)

    if is_first_run:
        print(f"Initial run completed: indexed {len(seen['articles'])} articles without sending notifications.")
    elif new_count > 0:
        print(f"Found {new_count} new articles across {len(feeds)} feeds.")
    else:
        print(f"No new articles. Sent {throwback_count} throwback notifications.")

    sys.exit(0)


if __name__ == "__main__":
    main()
