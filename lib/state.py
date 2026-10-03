import pathlib
import sys
import json
import os
import tempfile
import time
import datetime
from typing import Dict, Any, Optional
from .config import DEFAULT_MAX_AGE_DAYS

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