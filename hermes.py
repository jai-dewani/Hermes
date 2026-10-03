#!/usr/bin/env python3
"""Hermes: RSS/Atom feed notification service powered by ntfy.sh and GitHub Actions."""

import datetime
import html
import json
import os
import pathlib
import re
import sys
import time
from typing import Any, Dict, List, Optional, Tuple

import feedparser
import requests
import yaml

USER_AGENT = "Hermes/1.0 (RSS Reader; +https://github.com/)"
DEFAULT_TIMEOUT = 15
NOTIFICATION_TIMEOUT = 10
DEFAULT_MAX_AGE_DAYS = 30
MAX_EXCERPT_LENGTH = 200

# Valid ntfy priority levels (https://docs.ntfy.sh/publish/#message-priority)
VALID_PRIORITIES = {"min", "low", "default", "high", "urgent"}


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------


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


# ---------------------------------------------------------------------------
# State: seen.json
# ---------------------------------------------------------------------------


def _migrate_article_entry(value: Any) -> Dict[str, Any]:
    """Migrate legacy entries to the current metadata format (drops sent_count)."""
    if isinstance(value, (int, float)):
        # Legacy: plain Unix timestamp
        return {"first_seen": int(value), "title": "", "link": "", "blog_name": ""}
    if isinstance(value, dict) and "first_seen" in value:
        # Drop sent_count if present (removed with throwback feature)
        return {
            "first_seen": value["first_seen"],
            "title": value.get("title", ""),
            "link": value.get("link", ""),
            "blog_name": value.get("blog_name", ""),
        }
    # Unrecognized format — treat as fresh with no metadata
    return {"first_seen": int(time.time()), "title": "", "link": "", "blog_name": ""}


def load_seen(seen_path: str = "seen.json") -> Dict[str, Any]:
    """Load the seen articles state file. Migrates legacy formats automatically."""
    path = pathlib.Path(seen_path)
    if not path.is_file():
        return {"articles": {}}

    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict) and isinstance(data.get("articles"), dict):
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


def prune_old_entries(seen: Dict[str, Any], max_age_days: int = DEFAULT_MAX_AGE_DAYS) -> None:
    """Prune entries from the seen map that are older than max_age_days."""
    articles = seen.get("articles", {})
    cutoff = int(time.time()) - (max_age_days * 86400)
    seen["articles"] = {
        art_id: meta
        for art_id, meta in articles.items()
        if isinstance(meta, dict)
        and isinstance(meta.get("first_seen"), (int, float))
        and meta["first_seen"] >= cutoff
    }


# ---------------------------------------------------------------------------
# State: feed-status.json
# ---------------------------------------------------------------------------


def load_feed_status(status_path: str = "feed-status.json") -> Dict[str, Any]:
    """Load the persistent feed health status file."""
    path = pathlib.Path(status_path)
    if not path.is_file():
        return {"last_updated": None, "feeds": {}}
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict) and isinstance(data.get("feeds"), dict):
            return data
    except Exception as e:
        print(f"Warning: Failed to parse '{status_path}' ({e}). Rebuilding.", file=sys.stderr)
    return {"last_updated": None, "feeds": {}}


def save_feed_status(status_path: str, status_data: Dict[str, Any]) -> None:
    """Save the feed health status file atomically."""
    path = pathlib.Path(status_path)
    temp_path = path.with_suffix(".tmp")
    with open(temp_path, "w", encoding="utf-8") as f:
        json.dump(status_data, f, indent=2, sort_keys=True)
        f.write("\n")
    temp_path.replace(path)


def update_feed_status(
    feed_status: Dict[str, Any],
    name: str,
    url: str,
    success: bool,
    article_count: int = 0,
    error: Optional[str] = None,
) -> None:
    """Update health metadata for a single feed in the status dict (mutates in place)."""
    now_iso = datetime.datetime.now(datetime.UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    existing = feed_status["feeds"].get(name, {})

    feed_status["feeds"][name] = {
        "url": url,
        "article_count": article_count if success else existing.get("article_count", 0),
        "last_success": now_iso if success else existing.get("last_success"),
        "last_error": None if success else error,
        "consecutive_failures": 0 if success else existing.get("consecutive_failures", 0) + 1,
    }


# ---------------------------------------------------------------------------
# Feed fetching
# ---------------------------------------------------------------------------


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


def _extract_excerpt(entry: Any, max_length: int = MAX_EXCERPT_LENGTH) -> str:
    """Extract a plain-text excerpt from a feed entry's summary or content.

    Tries entry.summary first, then entry.content[0].value. Strips HTML tags,
    collapses whitespace, and truncates to max_length characters at a word boundary.
    """
    raw = _extract_entry_field(entry, "summary")
    if not raw:
        content = getattr(entry, "content", None)
        if content and isinstance(content, list) and len(content) > 0:
            raw = content[0].get("value", "") if isinstance(content[0], dict) else ""

    if not raw:
        return ""

    # Unescape HTML entities, then strip tags
    text = html.unescape(raw)
    text = re.sub(r"<[^>]+>", " ", text)
    # Collapse whitespace
    text = re.sub(r"\s+", " ", text).strip()

    if len(text) > max_length:
        truncated = text[:max_length].rsplit(" ", 1)[0]
        return truncated + "…"

    return text


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


# ---------------------------------------------------------------------------
# Notifications
# ---------------------------------------------------------------------------


def send_notification(
    config: Dict[str, Any],
    blog_name: str,
    entry: Any,
    timeout: int = NOTIFICATION_TIMEOUT,
    priority: str = "default",
) -> bool:
    """Send an HTTP push notification via ntfy.sh.

    Args:
        config: Loaded feeds.yaml config dict.
        blog_name: Human-readable feed name, used as the notification title.
        entry: feedparser entry (or dict with 'title', 'link', 'summary' keys).
        timeout: HTTP request timeout in seconds.
        priority: ntfy priority level — 'min', 'low', 'default', 'high', or 'urgent'.
    """
    topic = config["ntfy_topic"]
    server = config.get("ntfy_server", "https://ntfy.sh").rstrip("/")
    url = f"{server}/{topic}"

    title = _extract_entry_field(entry, "title") or "New article"
    link = _extract_entry_field(entry, "link")
    excerpt = _extract_excerpt(entry)

    body = f"{title}\n\n{excerpt}" if excerpt else title

    # Validate priority; fall back to default silently
    ntfy_priority = priority if priority in VALID_PRIORITIES else "default"

    headers = {
        "Title": blog_name,
        "Tags": "newspaper",
        "Priority": ntfy_priority,
    }
    if link:
        headers["Click"] = link

    token = os.environ.get("NTFY_TOKEN")
    if token:
        headers["Authorization"] = f"Bearer {token}"

    try:
        resp = requests.post(url, data=body.encode("utf-8"), headers=headers, timeout=timeout)
        resp.raise_for_status()
        return True
    except Exception as e:
        print(f"Warning: Failed to send notification for '{title}': {e}", file=sys.stderr)
        return False


# ---------------------------------------------------------------------------
# Run summary
# ---------------------------------------------------------------------------


def write_run_summary(
    results: Dict[str, Any],
    feed_status: Dict[str, Any],
    summary_path: str = "run-summary.md",
) -> None:
    """Write a markdown run report consumed by the GitHub Actions job summary step."""
    lines: List[str] = ["## 🕊️ Hermes Run Report\n"]

    new_articles = results.get("new_articles", [])
    if new_articles:
        lines.append(f"### 📰 {len(new_articles)} New Article{'s' if len(new_articles) != 1 else ''}\n")
        lines.append("| Blog | Article | Notified |")
        lines.append("|---|---|---|")
        for art in new_articles:
            status = "✅" if art["notified"] else "❌ Failed"
            title = art["title"] or art.get("link", "Unknown")
            link = art.get("link", "")
            article_cell = f"[{title}]({link})" if link else title
            lines.append(f"| {art['blog']} | {article_cell} | {status} |")
    else:
        lines.append("### ✅ No new articles found\n")

    # Feed health table
    feeds_checked = results.get("feeds_checked", 0)
    lines.append(f"\n### 📡 Feed Health ({feeds_checked} feeds checked)\n")
    lines.append("| Feed | Status | Consecutive Failures | Last Success |")
    lines.append("|---|---|---|---|")
    for feed_name, info in sorted(feed_status.get("feeds", {}).items()):
        failures = info.get("consecutive_failures", 0)
        if failures >= 5:
            status_icon = "🔴 Down"
        elif failures > 0:
            status_icon = "⚠️ Flaky"
        else:
            status_icon = "✅ OK"
        last_success = info.get("last_success") or "Never"
        lines.append(f"| {feed_name} | {status_icon} | {failures} | {last_success} |")

    # Errors section
    feed_errors = results.get("feed_errors", [])
    if feed_errors:
        lines.append(f"\n### ⚠️ Fetch Errors ({len(feed_errors)})\n")
        for err in feed_errors:
            lines.append(f"- **{err['blog']}**: `{err['error']}`")

    with open(summary_path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main() -> None:
    """Main execution flow for Hermes feed checker."""
    config = load_config("feeds.yaml")
    seen = load_seen("seen.json")
    feed_status = load_feed_status("feed-status.json")

    is_first_run = len(seen.get("articles", {})) == 0
    now_ts = int(time.time())

    # Tracking for run summary
    run_results: Dict[str, Any] = {
        "new_articles": [],
        "feed_errors": [],
        "feeds_checked": 0,
    }

    feeds = config.get("feeds", [])
    for feed_info in feeds:
        if not isinstance(feed_info, dict):
            continue

        name = feed_info.get("name", "Unknown Feed")
        url = feed_info.get("url")
        priority = feed_info.get("priority", "default")

        if not url:
            print(f"Warning: Feed '{name}' is missing a URL. Skipping.", file=sys.stderr)
            continue

        print(f"Checking feed: {name} ({url})")
        run_results["feeds_checked"] += 1

        try:
            entries = fetch_feed(url)
        except Exception as e:
            error_msg = str(e)
            print(f"Warning: Failed to fetch feed '{name}': {error_msg}", file=sys.stderr)
            run_results["feed_errors"].append({"blog": name, "error": error_msg})
            update_feed_status(feed_status, name, url, success=False, error=error_msg)
            continue

        update_feed_status(feed_status, name, url, success=True, article_count=len(entries))

        for entry in entries:
            article_id = resolve_id(entry)
            if not article_id:
                print(f"Warning: Could not determine ID for an entry in '{name}'. Skipping.", file=sys.stderr)
                continue

            if article_id not in seen["articles"]:
                entry_title = _extract_entry_field(entry, "title") or article_id
                entry_link = _extract_entry_field(entry, "link")

                notified = False
                if not is_first_run:
                    print(f"New article: {entry_title}")
                    notified = send_notification(config, name, entry, priority=priority)
                    run_results["new_articles"].append({
                        "blog": name,
                        "title": entry_title,
                        "link": entry_link,
                        "notified": notified,
                    })

                seen["articles"][article_id] = {
                    "first_seen": now_ts,
                    "title": entry_title,
                    "link": entry_link,
                    "blog_name": name,
                }

    max_age_days = config.get("max_age_days", DEFAULT_MAX_AGE_DAYS)
    prune_old_entries(seen, max_age_days=max_age_days)

    # Persist state
    feed_status["last_updated"] = datetime.datetime.now(datetime.UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    save_seen("seen.json", seen)
    save_feed_status("feed-status.json", feed_status)
    write_run_summary(run_results, feed_status)

    # Final log output
    new_count = len(run_results["new_articles"])
    if is_first_run:
        print(f"Initial run completed: indexed {len(seen['articles'])} articles without sending notifications.")
    elif new_count > 0:
        print(f"Found {new_count} new article{'s' if new_count != 1 else ''} across {len(feeds)} feeds.")
    else:
        print("No new articles found.")

    sys.exit(0)


if __name__ == "__main__":
    main()
