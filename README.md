# NewsBot

NewsBot is an open-source Discord bot that monitors configured RSS/Atom feeds and posts new articles into channel categories on your server. It is designed to run locally (or in a container) and requires a single Discord bot token and channel configuration to get started.

The bot is intentionally small and dependency-light: a single `bot.py` script does the fetching, deduplication, and posting, while `feeds.json` and `channels.json` contain the configured sources and channel mappings.

## What this repo contains

- `bot.py` — main bot implementation (Discord client, feed fetcher, scheduler, DB, and slash commands).
- `feeds.json` — list of RSS/Atom sources with categories and channel keys.
- `channels.json` — channel key → Discord channel ID mapping (created on first-run from env vars or edited by commands).
- `requirements.txt` — Python dependencies.

## Stack
- Language: Python 3.10+ (uses zoneinfo, types, and modern libs)
- Notable libraries: discord.py, feedparser, APScheduler, python-dotenv, certifi

## Quickstart (local)

1. Clone and create a virtual environment

```bash
git clone https://github.com/WScully3000/NewsBot.git
cd NewsBot
python -m venv .venv
source .venv/bin/activate  # or .venv\Scripts\activate on Windows
pip install -r requirements.txt
```

2. Configure environment variables

Create a `.env` file in the project root (DO NOT commit this file). At minimum set:

```
DISCORD_TOKEN=your_discord_bot_token_here
# Optional first-run channel IDs to seed channels.json (integers):
TELECOM_CHANNEL_ID=123456789012345678
FIBER_CHANNEL_ID=123456789012345678
WIRELESS_CHANNEL_ID=123456789012345678
CLOUD_CHANNEL_ID=123456789012345678
AI_CHANNEL_ID=123456789012345678
CYBER_CHANNEL_ID=123456789012345678
REGULATORY_CHANNEL_ID=123456789012345678
CARRIER_CHANNEL_ID=123456789012345678
DATACENTER_CHANNEL_ID=123456789012345678
```

The bot will seed `channels.json` from the `_CHANNEL_ID` env vars on first run if `channels.json` does not exist.

3. Start the bot

```bash
python bot.py
```

On startup the bot will sync application commands and immediately perform a feed check, then run checks on a schedule (default: every 15 minutes).

## Config files
- `feeds.json`: Add, remove, or edit feed entries. Each entry should include `name`, `category`, `channel` (a key from `channels.json`), and `url`.
- `channels.json`: Maps channel keys (used in `feeds.json`) to Discord channel IDs. Managed by the bot via slash commands `/addchannel`, `/editchannel`, `/removechannel` and seeded from env vars on first run.

## Runtime details
- The bot uses an on-disk SQLite DB `news.db` to track posted article IDs and user timezone preferences. The DB is created automatically next to `bot.py`.
- The bot posts up to `ARTICLES_PER_FEED` newest items for each feed when discovered. It deduplicates by article `id` or URL.
- Key slash commands: `/listfeeds`, `/listchannels`, `/addfeed`, `/addchannel`, `/editchannel`, `/removechannel`, `/refresh`, `/cleanup`, `/purge_news`, `/stats`, `/timezone`.

## Security & Secrets
- The repository currently contains no Discord tokens or secret keys in tracked files. I inspected `bot.py`, `channels.json`, `feeds.json`, and `requirements.txt` and found no hard-coded secrets.
- `channels.json` contains numeric channel IDs (Discord snowflakes). These are not secrets but are server-specific — keep them private if your server is private.
- Never commit `.env` or any file containing `DISCORD_TOKEN` or other secrets. Add `.env` to `.gitignore` if it is not already present.

Recommended check before publishing:

```bash
# show any recent accidental commits containing 'TOKEN' or 'KEY'
git grep -n "TOKEN\|KEY\|SECRET" -- ** || true
# show any .env or .env.* accidentally tracked
git ls-files | grep -i "\.env" || true
```

If you want, I can add a `.env.example` file (non-secret template) and a GitHub Actions workflow to block secrets in PRs.

## Running in Docker (optional)
There is no Dockerfile in the repository. To run in Docker, create a simple Dockerfile that installs Python, copies the files, installs `requirements.txt`, and runs `python bot.py` while providing the `DISCORD_TOKEN` via an env file or secret.

## Tests
- `requirements.txt` includes `pytest`, but there are currently no tests in the repository. Adding tests for the feed parsing and DB logic is recommended.

## Contributing
- Fork, create a branch, and open a PR.
- Follow the project style (PEP 8) and include tests for new features.

## License
This project is licensed under the MIT License — see `LICENSE`.

## Maintainer
WScully3000 — https://github.com/WScully3000/NewsBot
