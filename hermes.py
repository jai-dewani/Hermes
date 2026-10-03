#!/usr/bin/env python3
"""
Hermes - A simple, zero-infrastructure RSS/Atom feed checker.
"""

import sys
import time
import datetime
from typing import Dict, Any

from lib.config import load_config, DEFAULT_MAX_AGE_DAYS
from lib.state import (
    _migrate_article_entry,
    load_seen, save_seen, prune_old_entries,
    load_feed_status, save_feed_status, update_feed_status
)
from lib.feed import resolve_id, _extract_entry_field, _extract_excerpt, fetch_feed
from lib.notify import send_notification
from lib.summary import write_run_summary

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
