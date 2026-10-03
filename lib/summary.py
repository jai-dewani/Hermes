from typing import Dict, Any, List

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