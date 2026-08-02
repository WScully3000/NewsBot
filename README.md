# NewsBot

NewsBot is an open-source Discord bot that monitors feeds you configure and posts new items into channels on your server. It supports both RSS/Atom (XML) and JSON feed sources, and ships with zero preconfigured feeds or channels — you decide what it follows and where it posts, whether that's telecom news, gaming, sports, security advisories, or anything else. It is designed to run locally (or in a container) and requires a single Discord bot token to get started.

The bot is intentionally small and dependency-light: a single `bot.py` script does the fetching, deduplication, and posting, while `feeds.json` and `channels.json` hold your configured sources and channel mappings. Both are gitignored (they're live, per-deployment state, editable via slash commands) — `feeds.example.json` and `channels.example.json` are the tracked (empty) templates the bot seeds from on first run.

## What this repo contains

- `bot.py` — main bot implementation (Discord client, feed fetcher, scheduler, DB, and slash commands).
- `feeds.example.json` / `channels.example.json` — tracked templates (ship empty). The bot copies these into `feeds.json` / `channels.json` on first run if those don't exist yet; both are gitignored from then on.
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
```

See `.env.example` for optional overrides (NVD API key, file paths, poll interval).

3. Start the bot

```bash
python bot.py
```

`feeds.json` and `channels.json` don't exist yet on a fresh clone — the bot creates both automatically on first run (empty, from `feeds.example.json` / `channels.example.json`). Nothing will post until you add at least one channel and one feed:

- In Discord: `/addchannel <key> <#channel>` to map a category key to a channel, then `/addfeed <name> <url> <category> <channel>` to add a feed pointed at it.
- Or by hand: edit `channels.json` / `feeds.json` directly (see [Config files](#config-files) below for the shape). `feeds.json` is re-read on every check, so `/refresh` (or the next scheduled poll) picks up changes immediately. `channels.json` is only loaded once at startup, so a hand edit to it needs a bot **restart** to take effect — `/addchannel`/`/editchannel`/`/removechannel` update it live instead, without a restart.

On startup the bot syncs application commands and immediately performs a feed check, then runs checks on a schedule (default: every 15 minutes).

## Config files
- `feeds.json` (gitignored; template: `feeds.example.json`): a list of feed entries. Add, remove, or edit directly, or add new ones via `/addfeed` (there's no remove-feed command yet; delete the entry from the file). Each entry needs `name`, `category`, `channel` (a key from `channels.json`), and `url`. An optional `type` field selects the feed format:
  - `"rss"` (default, can be omitted) — any RSS/Atom XML feed, parsed with `feedparser`.
  - `"nvd-cve"` — the [NVD CVE API](https://nvd.nist.gov/developers/vulnerabilities) (JSON), useful if you're running a security-news channel. The bot queries recently modified CVEs on each check (NVD's documented best practice for gap-free polling) and posts one message per CVE newly published in that window. Optionally set `NVD_API_KEY` in `.env` to raise the API rate limit.
  - New feed formats can be added via `/addfeed`'s `feed_type` option, which offers the same choices.
- `channels.json` (gitignored; template: `channels.example.json`): maps channel keys of your choosing (referenced by `channel` in `feeds.json`) to Discord channel IDs. There's nothing special about the key names — pick whatever categories fit your use case. Managed via `/addchannel`, `/editchannel`, `/removechannel`, or edited directly.

## Runtime details
- The bot uses an on-disk SQLite DB `news.db` to track posted article IDs and user timezone preferences. The DB is created automatically next to `bot.py`.
- The bot posts up to `ARTICLES_PER_FEED` newest items for each feed when discovered. It deduplicates by article `id` or URL.
- Key slash commands: `/listfeeds`, `/listchannels`, `/addfeed`, `/addchannel`, `/editchannel`, `/removechannel`, `/refresh`, `/cleanup`, `/purge_news`, `/stats`, `/timezone`.

## Security & Secrets
- The repository contains no Discord tokens or secret keys in tracked files.
- `channels.json` contains numeric channel IDs (Discord snowflakes) and `feeds.json` reflects your actual configured sources — both are server-specific rather than secret, and both are gitignored so they never end up in a fork's history.
- Never commit `.env` or any file containing `DISCORD_TOKEN` or other secrets. Add `.env` to `.gitignore` if it is not already present.
- A `secret-check` GitHub Actions workflow (`.github/workflows/secret-check.yml`) runs on every push/PR and fails the build on likely hardcoded secrets. Before opening a PR, you can run the same check locally:

```bash
git grep -I -E "(TOKEN|SECRET|API_KEY|PASSWORD|PRIVATE_KEY|AWS_ACCESS_KEY_ID|AWS_SECRET_ACCESS_KEY)[[:space:]]*[:=][[:space:]]*[\"'][A-Za-z0-9_/+.-]{8,}[\"']" -- . ':!*.md' ':!.env.example' ':!.github/workflows/*'
```

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
