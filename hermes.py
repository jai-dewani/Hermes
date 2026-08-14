#!/usr/bin/env python3
"""Hermes: RSS/Atom feed notification service powered by ntfy.sh and GitHub Actions."""

import json
import os
import pathlib
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


def load_seen(seen_path: str = "seen.json") -> Dict[str, Any]:
    """Load the seen articles state file. Returns an empty structure on failure."""
    path = pathlib.Path(seen_path)
    if not path.is_file():
        return {"articles": {}}

    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict) and isinstance(data.get("articles"), dict):
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
) -> bool:
    """Send an HTTP push notification via ntfy.sh."""
    topic = config["ntfy_topic"]
    server = config.get("ntfy_server", "https://ntfy.sh").rstrip("/")
    url = f"{server}/{topic}"

    title = getattr(entry, "title", None) or (entry.get("title") if isinstance(entry, dict) else None) or "New article"
    link = getattr(entry, "link", None) or (entry.get("link") if isinstance(entry, dict) else None) or ""

    headers = {
        "Title": blog_name,
        "Tags": "newspaper",
    }
    if link:
        headers["Click"] = str(link).strip()

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


def prune_old_entries(seen: Dict[str, Any], max_age_days: int = DEFAULT_MAX_AGE_DAYS) -> None:
    """Prune entries from the seen map that are older than max_age_days."""
    articles = seen.get("articles", {})
    cutoff = int(time.time()) - (max_age_days * 86400)
    seen["articles"] = {
        art_id: ts
        for art_id, ts in articles.items()
        if isinstance(ts, (int, float)) and ts >= cutoff
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
                    entry_title = getattr(entry, "title", None) or (entry.get("title") if isinstance(entry, dict) else None) or article_id
                    print(f"New article: {entry_title}")
                    send_notification(config, name, entry)
                    new_count += 1
                seen["articles"][article_id] = now_ts

    max_age_days = config.get("max_age_days", DEFAULT_MAX_AGE_DAYS)
    prune_old_entries(seen, max_age_days=max_age_days)
    save_seen("seen.json", seen)

    if is_first_run:
        print(f"Initial run completed: indexed {len(seen['articles'])} articles without sending notifications.")
    else:
        print(f"Found {new_count} new articles across {len(feeds)} feeds.")

    sys.exit(0)


if __name__ == "__main__":
    main()
