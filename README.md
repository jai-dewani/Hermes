# Hermes 🕊️

> **Hermes** (the Greek messenger god): A lightweight blog and RSS/Atom/JSON feed monitor that delivers instant push notifications to your phone via [ntfy.sh](https://ntfy.sh), running entirely on GitHub Actions with zero server infrastructure.

---

## Architecture

```
┌──────────────────────────────────────────────────────────────┐
│  GitHub Actions Runner (triggered every 15 minutes via cron) │
│                                                              │
│  Workflow: check-feeds.yml                                   │
│  ┌────────────────────────────────────────────────────────┐  │
│  │ 1. Checkout repo (includes seen.json state)            │  │
│  │ 2. Install dependencies (feedparser, requests, pyyaml) │  │
│  │ 3. Run hermes.py                                       │  │
│  │    ├── Load feeds.yaml & seen.json                     │  │
│  │    ├── Check feeds & detect new articles               │  │
│  │    ├── Send ntfy.sh push notification for new articles │  │
│  │    └── Prune articles older than 30 days               │  │
│  │ 4. Commit & push updated seen.json (only if changed)   │  │
│  └────────────────────────────────────────────────────────┘  │
└──────────────────────────────────────────────────────────────┘
         │                              │
         ▼                              ▼
  ┌─────────────┐              ┌────────────────┐
  │  Blog Feeds │              │    ntfy.sh     │
  │ (RSS / Atom │              │  ───► Phone    │
  │  JSON Feed) │              │   Notification │
  └─────────────┘              └────────────────┘
```

---

## Setup Guide

### 1. Install the ntfy Mobile App

Download the free, open-source ntfy app for your device:
- **Android**: [ntfy on Google Play](https://play.google.com/store/apps/details?id=io.heckel.ntfy) or [F-Droid](https://f-droid.org/en/packages/io.heckel.ntfy/)
- **iOS**: [ntfy on App Store](https://apps.apple.com/app/ntfy/id1625396347)
- **Web**: Open [ntfy.sh](https://ntfy.sh) in any browser.

In the app, subscribe to a unique topic name of your choice (e.g. `hermes-myname-7f3b`).

### 2. Configure `feeds.yaml`

Edit `feeds.yaml` in the root of the repository:

```yaml
# Your private/unique ntfy topic
ntfy_topic: "hermes-myname-7f3b"

# Optional: self-hosted ntfy server (defaults to https://ntfy.sh)
# ntfy_server: "https://ntfy.sh"

# Optional: state retention window in days (defaults to 30)
# max_age_days: 30

# List of feeds to monitor
feeds:
  - name: "Julia Evans"
    url: "https://jvns.ca/atom.xml"

  - name: "Simon Willison"
    url: "https://simonwillison.net/atom/everything/"

  - name: "Dan Luu"
    url: "https://danluu.com/atom.xml"
```

### 3. Configure GitHub Workflow Permissions

Because Hermes commits state back to `seen.json`, your GitHub repository needs write permissions for workflows:
1. Go to your GitHub repository **Settings** → **Actions** → **General**.
2. Scroll to **Workflow permissions**.
3. Select **Read and write permissions**.
4. Click **Save**.

### 4. Optional: Private/Authenticated ntfy Topics

If using an authenticated ntfy topic or self-hosted instance with access control:
1. Go to repository **Settings** → **Secrets and variables** → **Actions**.
2. Create a new repository secret named `NTFY_TOKEN`.
3. Paste your token.

### 5. Seed Initial State & Test

1. Push your changes to GitHub.
2. In your repo, go to the **Actions** tab.
3. Select **Hermes Feed Checker** → **Run workflow**.
4. On the **first run**, Hermes marks all existing articles in your feeds as "seen" without spamming your phone.
5. On subsequent cron runs (every 15 minutes), any newly published articles will trigger a push notification.

---

## Notification Appearance

When a new article is detected, you will receive a notification formatted like:

```
┌──────────────────────────────────────┐
│ 📰 Julia Evans                       │
│                                      │
│ How DNS Works                        │
│                                      │
│ Tap to open article                  │
└──────────────────────────────────────┘
```

- **Title**: Blog name
- **Body**: Article title
- **Click**: Tapping opens the direct article URL in your browser.
- **Icon**: 📰 (`newspaper` tag)

---

## Git Log Hygiene

To filter out bot commits when reviewing repository history:

```bash
# Show only human commits
git log --oneline --invert-grep --grep="chore: update seen articles"

# Show only Hermes bot state update commits
git log --oneline --grep="chore: update seen articles"
```

---

## Local Development & Testing

```bash
# Create virtual environment
python3 -m venv .venv
source .venv/bin/activate

# Install dependencies
pip install -r requirements.txt

# Run tests
pytest tests/ -v

# Run Hermes locally
python hermes.py
```
