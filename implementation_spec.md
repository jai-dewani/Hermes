# Hermes — Implementation Specification

> Hermes: the Greek messenger god. A system that monitors RSS/Atom/JSON feeds and delivers push notifications to your phone via ntfy.sh, running entirely on GitHub Actions.

---

## Table of Contents

1. [Overview](#overview)
2. [Architecture](#architecture)
3. [Repo Structure](#repo-structure)
4. [Component Specifications](#component-specifications)
   - [feeds.yaml — Feed Configuration](#1-feedsyaml--feed-configuration)
   - [seen.json — State File](#2-seenjson--state-file)
   - [hermes.py — Core Script](#3-hermespy--core-script)
   - [check-feeds.yml — GitHub Actions Workflow](#4-check-feedsyml--github-actions-workflow)
   - [requirements.txt — Dependencies](#5-requirementstxt--dependencies)
5. [Detailed Behavior & Edge Cases](#detailed-behavior--edge-cases)
6. [ntfy.sh Integration Details](#ntfysh-integration-details)
7. [Git Hygiene for State Commits](#git-hygiene-for-state-commits)
8. [Setup Instructions for the End User](#setup-instructions-for-the-end-user)
9. [Constraints & Limitations](#constraints--limitations)

---

## Overview

**Goal**: Monitor a user-configured list of blogs (via their RSS, Atom, or JSON feeds) on a cron schedule, and send a push notification to the user's phone for every new article detected.

**Key Design Decisions** (already agreed upon):

| Decision | Choice |
|---|---|
| Language | Python 3 |
| Feed parsing | `feedparser` library |
| Notification delivery | ntfy.sh (free, HTTP-based push notification service) |
| Scheduling & runtime | GitHub Actions with `schedule` (cron) trigger |
| State persistence | JSON file committed to the repository |
| Configuration format | YAML file in the repository root |

---

## Architecture

```
┌──────────────────────────────────────────────────────────────┐
│  GitHub Actions Runner (triggered every 15 minutes via cron) │
│                                                              │
│  Workflow: check-feeds.yml                                   │
│  ┌────────────────────────────────────────────────────────┐  │
│  │ Step 1: Checkout repo (includes seen.json)             │  │
│  │ Step 2: Install Python dependencies                    │  │
│  │ Step 3: Run hermes.py                                  │  │
│  │   ├── Load feeds.yaml (list of blogs)                  │  │
│  │   ├── Load seen.json (previously seen article IDs)     │  │
│  │   ├── For each feed:                                   │  │
│  │   │   ├── Fetch & parse feed via feedparser             │  │
│  │   │   ├── Identify new entries (not in seen set)       │  │
│  │   │   ├── For each new entry:                          │  │
│  │   │   │   └── POST notification to ntfy.sh             │  │
│  │   │   └── Add all entry IDs to seen set                │  │
│  │   └── Write updated seen.json                          │  │
│  │ Step 4: Commit & push seen.json (only if changed)      │  │
│  └────────────────────────────────────────────────────────┘  │
└──────────────────────────────────────────────────────────────┘
         │                              │
         ▼                              ▼
  ┌─────────────┐              ┌────────────────┐
  │  Blog Feeds │              │    ntfy.sh     │
  │ (RSS/Atom/  │              │  ───► Phone    │
  │  JSON Feed) │              │   Notification │
  └─────────────┘              └────────────────┘
```

---

## Repo Structure

```
hermes/
├── .github/
│   └── workflows/
│       └── check-feeds.yml      # GitHub Actions cron workflow
├── hermes.py                    # Core Python script (~80-100 lines)
├── feeds.yaml                   # User-configured list of feeds
├── seen.json                    # State file (auto-managed, committed by bot)
├── requirements.txt             # Python dependencies
├── README.md                    # Project README with setup instructions
└── .gitignore                   # Standard Python gitignore
```

---

## Component Specifications

### 1. `feeds.yaml` — Feed Configuration

The user's blog list. This is the only file the user manually edits.

**Schema:**

```yaml
# ntfy.sh topic to publish notifications to.
# The user subscribes to this topic in the ntfy app on their phone.
ntfy_topic: "my-hermes-alerts"

# Optional: ntfy server URL. Defaults to https://ntfy.sh if omitted.
# ntfy_server: "https://ntfy.sh"

# List of feeds to monitor.
feeds:
  - name: "Julia Evans"
    url: "https://jvns.ca/atom.xml"

  - name: "Simon Willison"
    url: "https://simonwillison.net/atom/everything/"

  - name: "Dan Luu"
    url: "https://danluu.com/atom.xml"
```

**Fields per feed entry:**

| Field | Required | Description |
|---|---|---|
| `name` | Yes | Human-readable blog name. Used in the notification title. |
| `url` | Yes | The RSS, Atom, or JSON Feed URL. |

**Top-level fields:**

| Field | Required | Default | Description |
|---|---|---|---|
| `ntfy_topic` | Yes | — | The ntfy.sh topic string to POST to. |
| `ntfy_server` | No | `https://ntfy.sh` | Base URL of the ntfy server. |

---

### 2. `seen.json` — State File

Tracks which articles have already triggered a notification. Committed to the repo automatically by the workflow.

**Schema:**

```json
{
  "articles": {
    "https://jvns.ca/blog/2024/some-post/": 1691234567,
    "https://simonwillison.net/2024/some-post/": 1691234890
  }
}
```

- **Key**: The article's unique identifier (see [Article Identity Resolution](#article-identity-resolution) below).
- **Value**: Unix timestamp of when the article was first seen. Used for pruning old entries.

**Pruning rule**: On every run, discard entries older than **30 days**. This keeps the file small and bounded. No blog feed contains entries older than 30 days anyway.

**Initial state**: The file should ship as `{"articles": {}}` in the repo. On the very first run, `hermes.py` must treat all current feed entries as "already seen" and add them to `seen.json` *without* sending notifications. This prevents a flood of notifications for old articles on first setup.

> [!IMPORTANT]
> **First-run behavior**: Detect first run by checking if `seen.json` contains an empty `articles` object. If empty, populate it with all current feed entry IDs but **do not send any notifications**. Only send notifications on subsequent runs for entries that are truly new.

---

### 3. `hermes.py` — Core Script

The main Python script. Should be a single file, no classes needed — just clean procedural code with well-named functions.

#### Dependencies

- `feedparser` — Parses RSS, Atom, and JSON Feed formats with a single `feedparser.parse(url)` call.
- `requests` — For sending HTTP POST requests to ntfy.sh.
- `pyyaml` — For reading `feeds.yaml`.
- Standard library: `json`, `time`, `sys`, `pathlib`.

#### Main Flow (pseudocode)

```
function main():
    config = load_yaml("feeds.yaml")
    seen = load_json("seen.json")
    is_first_run = (seen.articles is empty)

    new_count = 0

    for feed in config.feeds:
        entries = feedparser.parse(feed.url).entries

        for entry in entries:
            article_id = resolve_id(entry)

            if article_id not in seen.articles:
                if not is_first_run:
                    send_notification(config, feed.name, entry)
                    new_count += 1
                seen.articles[article_id] = current_unix_timestamp()

    prune_old_entries(seen, max_age_days=30)
    save_json("seen.json", seen)

    print(f"Found {new_count} new articles.")
    exit(0)
```

#### Article Identity Resolution

`feedparser` normalizes entries from all feed formats. Use the following priority to determine a unique ID for each entry:

```
1. entry.id        (the <guid> in RSS, <id> in Atom)  — preferred
2. entry.link      (the permalink URL)                 — fallback
3. entry.title     (the article title)                 — last resort
```

Always strip whitespace and normalize the ID. Implement this as a `resolve_id(entry)` function.

#### Error Handling

- **Feed fetch failure** (network error, 404, timeout): Log a warning to stderr and **skip that feed**. Do not crash the entire run because one blog is down. Use a try/except around each feed's processing.
- **ntfy.sh POST failure**: Log a warning to stderr and **continue**. The article will still be marked as seen (to avoid duplicate notifications on retry). This is a deliberate trade-off — missing one notification is better than getting duplicates.
- **Malformed feed** (feedparser returns bozo=True with no entries): Log a warning and skip.
- **Missing `feeds.yaml`**: Exit with a clear error message and non-zero exit code.
- **Missing `seen.json`**: Treat as first run (create it with empty articles).

#### Logging

Use `print()` to stdout for normal operation messages:
- `"Checking feed: {name} ({url})"`
- `"New article: {title}"`
- `"Found {n} new articles across {m} feeds."`

Use `print(..., file=sys.stderr)` for warnings/errors:
- `"Warning: Failed to fetch feed '{name}': {error}"`
- `"Warning: Failed to send notification for '{title}': {error}"`

Do **not** use the `logging` module — it's overkill for a script this small.

---

### 4. `check-feeds.yml` — GitHub Actions Workflow

```yaml
name: Hermes Feed Checker

on:
  schedule:
    # Run every 15 minutes
    - cron: '*/15 * * * *'
  workflow_dispatch:  # Allow manual trigger for testing

permissions:
  contents: write  # Needed to push seen.json updates

jobs:
  check-feeds:
    runs-on: ubuntu-latest

    steps:
      - name: Checkout repository
        uses: actions/checkout@v4

      - name: Set up Python
        uses: actions/setup-python@v5
        with:
          python-version: '3.12'

      - name: Install dependencies
        run: pip install -r requirements.txt

      - name: Run Hermes
        run: python hermes.py

      - name: Commit updated seen.json
        run: |
          git config user.name "hermes-bot"
          git config user.email "hermes-bot@users.noreply.github.com"
          git add seen.json
          git diff --cached --quiet || git commit -m "chore: update seen articles"
          git push
```

**Key details:**

- `workflow_dispatch` allows the user to manually trigger the workflow from the GitHub UI for testing.
- `permissions: contents: write` is required for the bot to push commits.
- The `git diff --cached --quiet || git commit` pattern ensures **no commit is created if `seen.json` didn't change** (i.e., no new articles were found).
- `actions/checkout@v4` checks out the repo including `seen.json` with the latest state.

> [!WARNING]
> **Race condition**: If two workflow runs overlap (rare but possible), the second `git push` could fail. This is harmless — the next run will pick up the missed articles. If you want to be defensive, add `concurrency: { group: hermes, cancel-in-progress: false }` to the workflow to queue runs instead of running them in parallel.

Add this to the workflow YAML at the job level:

```yaml
concurrency:
  group: hermes
  cancel-in-progress: false
```

---

### 5. `requirements.txt` — Dependencies

```
feedparser>=6.0,<7.0
requests>=2.28,<3.0
pyyaml>=6.0,<7.0
```

Pin to major versions for stability, allow minor/patch updates.

---

## Detailed Behavior & Edge Cases

### Article Identity Resolution — expanded

| Feed Type | `entry.id` value | `entry.link` value |
|---|---|---|
| RSS with `<guid>` | The GUID string (often a URL) | Permalink URL |
| RSS without `<guid>` | `None` | Permalink URL |
| Atom | The `<id>` element (usually a URN or URL) | Permalink URL |
| JSON Feed | The `id` field | The `url` field |

`feedparser` normalizes all of these into `entry.id` and `entry.link`.

### What if a blog changes its feed URL?

The user updates `feeds.yaml`. Old seen entries (keyed by article ID, not feed URL) are still in `seen.json`, so no duplicate notifications are sent — article IDs are typically stable across feed URL changes.

### What if `seen.json` gets corrupted?

If `seen.json` is not valid JSON, treat it as a first run (log a warning, rebuild from scratch, don't notify). This is the safest recovery path.

### What if feedparser can't determine any ID?

If `entry.id`, `entry.link`, and `entry.title` are all missing/empty, skip that entry and log a warning. This should essentially never happen with real-world feeds.

---

## ntfy.sh Integration Details

ntfy.sh is a simple HTTP-based pub/sub notification service. No API key is required for basic usage.

### Sending a Notification

```
POST https://ntfy.sh/{topic}
Headers:
  Title: {blog_name}: {article_title}
  Click: {article_url}
  Tags: newspaper

Body: {article_title}
```

**Implementation as a function:**

```python
def send_notification(config, blog_name, entry):
    topic = config["ntfy_topic"]
    server = config.get("ntfy_server", "https://ntfy.sh")
    url = f"{server}/{topic}"

    title = entry.get("title", "New article")
    link = entry.get("link", "")

    headers = {
        "Title": f"{blog_name}: {title}",
        "Tags": "newspaper",
    }
    if link:
        headers["Click"] = link

    requests.post(url, data=title.encode("utf-8"), headers=headers, timeout=10)
```

**What the user sees on their phone:**

```
┌─────────────────────────────────────┐
│ 📰 Julia Evans: How DNS Works      │
│                                     │
│ How DNS Works                       │
│                                     │
│ Tap to open article                 │
└─────────────────────────────────────┘
```

- The `Title` header sets the notification title.
- The `Click` header makes tapping the notification open the article URL in the browser.
- The `Tags: newspaper` header adds a 📰 emoji icon to the notification.

### ntfy.sh Authentication (optional)

If the user wants to use a private/authenticated ntfy topic (to prevent others from posting to it), they can:

1. Set up access control on ntfy.sh.
2. Store the ntfy token as a GitHub Actions secret named `NTFY_TOKEN`.
3. Pass it to the script via an environment variable.
4. Add an `Authorization: Bearer {token}` header to the POST request.

This is optional. For most personal use, an obscure topic name (e.g., `hermes-a7f3b2c9`) is sufficient.

If implementing, add this to the workflow:

```yaml
- name: Run Hermes
  run: python hermes.py
  env:
    NTFY_TOKEN: ${{ secrets.NTFY_TOKEN }}
```

And in `hermes.py`, check `os.environ.get("NTFY_TOKEN")` and add the auth header if present.

---

## Git Hygiene for State Commits

The bot commits to `seen.json` will appear in the repo's git history. Here's how to keep things clean:

### Commit identity

```
Author: hermes-bot <hermes-bot@users.noreply.github.com>
Message: chore: update seen articles
```

### Filtering bot commits from git log

```bash
# Show only human commits
git log --oneline --invert-grep --grep="chore: update seen articles"

# Show only bot commits
git log --oneline --grep="chore: update seen articles"
```

### No-commit runs

If no new articles are found, `seen.json` might still change due to pruning. The `git diff --cached --quiet` check handles this — only commits if the file actually changed.

---

## Setup Instructions for the End User

Include these in the `README.md`:

### 1. Create the repository

Create a new GitHub repository (public or private — both work with Actions).

### 2. Install the ntfy app

- **Android**: [ntfy on Google Play](https://play.google.com/store/apps/details?id=io.heckel.ntfy)
- **iOS**: [ntfy on App Store](https://apps.apple.com/app/ntfy/id1625396347)

Subscribe to the topic you chose in `feeds.yaml` (e.g., `my-hermes-alerts`).

### 3. Configure your feeds

Edit `feeds.yaml` to add your favorite blogs. Find a blog's feed URL by:
- Looking for an RSS/Atom icon on the blog.
- Checking `{blog_url}/feed`, `{blog_url}/rss`, `{blog_url}/atom.xml`.
- Using a browser extension like "Get RSS Feed URL".

### 4. Push and activate

Push all files to GitHub. The workflow will start running on the cron schedule. You can also trigger it manually from the **Actions** tab → **Hermes Feed Checker** → **Run workflow**.

### 5. Verify

Trigger the workflow manually. Check the Actions log for output. You should see your feeds being checked. On the first run, no notifications are sent (all existing articles are marked as seen). On subsequent runs, new articles will trigger phone notifications.

---

## Constraints & Limitations

| Constraint | Impact |
|---|---|
| GitHub Actions cron can be delayed up to 15-60 min under high load | Notifications may arrive slightly late; acceptable for a blog monitor |
| ntfy.sh free tier has no SLA | If ntfy.sh is down, notifications are silently lost for that run |
| `seen.json` committed to repo | Adds bot commits to git history (mitigated by filtering, see above) |
| Feed fetch timeout | Set a 15-second timeout on `feedparser.parse()` calls via `requests` or `urllib` |
| GitHub Actions free tier limits | 2,000 min/month for private repos; at ~15s/run × 96 runs/day ≈ 720 min/month — well within limits |

---

*Hermes v1.0 — designed for simplicity, reliability, and zero infrastructure costs.*
