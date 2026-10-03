import feedparser
import requests
import html
import re
from typing import Any, Dict, List, Optional

DEFAULT_TIMEOUT = 15
MAX_EXCERPT_LENGTH = 200
USER_AGENT = "Hermes/1.0 (RSS Reader; +https://github.com/)"

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