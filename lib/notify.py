import os
import sys
import requests
from typing import Dict, Any

NOTIFICATION_TIMEOUT = 10
VALID_PRIORITIES = {"min", "low", "default", "high", "urgent"}

# We need to import these internally for the excerpt logic
from .feed import _extract_entry_field, _extract_excerpt

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