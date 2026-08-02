import asyncio
import json
import os
import re
import sqlite3
import ssl
import urllib.parse
import urllib.request
from calendar import timegm
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import certifi
import discord
import feedparser
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from discord import app_commands
from dotenv import load_dotenv
from feedparser.util import FeedParserDict

load_dotenv()

TOKEN = os.getenv("DISCORD_TOKEN")

# The Windows certificate store can hold stale cached intermediate CAs,
# which makes some (but not all) feeds fail TLS verification even though
# their certs are genuinely valid. certifi's independently maintained CA
# bundle sidesteps that instead of trusting the OS store.
_ssl_context = ssl.create_default_context(cafile=certifi.where())
FEED_FETCH_HANDLERS = [urllib.request.HTTPSHandler(context=_ssl_context)]

FEEDS_FILE = "feeds.json"
FEEDS_EXAMPLE_FILE = "feeds.example.json"
CHANNELS_FILE = "channels.json"
DB_FILE = "news.db"
ARTICLES_PER_FEED = 5
FEED_CHECK_INTERVAL_MINUTES = 15


def _seed_channels_from_env() -> dict:
    return {
        "telecom-news": int(os.getenv("TELECOM_CHANNEL_ID")),
        "fiber-news": int(os.getenv("FIBER_CHANNEL_ID")),
        "wireless-news": int(os.getenv("WIRELESS_CHANNEL_ID")),
        "cloud-news": int(os.getenv("CLOUD_CHANNEL_ID")),
        "ai-news": int(os.getenv("AI_CHANNEL_ID")),
        "cybersecurity-news": int(os.getenv("CYBER_CHANNEL_ID")),
        "regulatory-news": int(os.getenv("REGULATORY_CHANNEL_ID")),
        "carrier-alerts": int(os.getenv("CARRIER_CHANNEL_ID")),
        "datacenter-news": int(os.getenv("DATACENTER_CHANNEL_ID")),
    }


def save_channels(channels: dict) -> None:
    with open(CHANNELS_FILE, "w") as f:
        json.dump(channels, f, indent=2)
        f.write("\n")


def load_channels() -> dict:
    # First run: no channels.json yet, so seed it from the existing
    # .env-based channel IDs (one-time migration). After this, .env's
    # *_CHANNEL_ID vars are no longer read - channels.json is the sole
    # source of truth, editable live via /addchannel /editchannel /removechannel.
    if not os.path.exists(CHANNELS_FILE):
        channels = _seed_channels_from_env()
        save_channels(channels)
        return channels

    with open(CHANNELS_FILE, "r") as f:
        return json.load(f)


CHANNELS = load_channels()


# -------------------------
# DATABASE
# -------------------------

db = sqlite3.connect(DB_FILE)

db.execute("""
    CREATE TABLE IF NOT EXISTS articles (
        id TEXT PRIMARY KEY,
        title TEXT,
        created_at TEXT,
        source TEXT,
        channel TEXT
    )
""")

db.execute("""
    CREATE TABLE IF NOT EXISTS users (
        discord_id TEXT PRIMARY KEY,
        timezone TEXT DEFAULT 'UTC'
    )
""")

db.commit()


def utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def article_timestamp(article) -> datetime:
    # feedparser normalizes published_parsed/updated_parsed to UTC struct_time.
    parsed = article.get("published_parsed") or article.get("updated_parsed")

    if parsed:
        return datetime.fromtimestamp(timegm(parsed), tz=timezone.utc)

    return datetime.now(timezone.utc)


IMG_TAG_RE = re.compile(r'<img[^>]+src="([^"]+)"', re.IGNORECASE)
HTML_TAG_RE = re.compile(r"<[^>]+>")
SUMMARY_MAX_CHARS = 300


def extract_image_url(article) -> str | None:
    for media in article.get("media_content", []):
        if media.get("url"):
            return media["url"]

    for thumb in article.get("media_thumbnail", []):
        if thumb.get("url"):
            return thumb["url"]

    for link in article.get("links", []):
        if link.get("rel") == "enclosure" and link.get("type", "").startswith("image/"):
            return link.get("href")

    html = article.get("summary", "")
    match = IMG_TAG_RE.search(html)
    return match.group(1) if match else None


def extract_summary(article) -> str:
    text = HTML_TAG_RE.sub("", article.get("summary", ""))
    text = " ".join(text.split())

    if len(text) > SUMMARY_MAX_CHARS:
        text = text[:SUMMARY_MAX_CHARS].rsplit(" ", 1)[0] + "…"

    return text


def get_user_timezone(discord_id: int) -> ZoneInfo:
    row = db.execute(
        "SELECT timezone FROM users WHERE discord_id=?",
        (str(discord_id),),
    ).fetchone()

    tz_name = row[0] if row else "UTC"

    try:
        return ZoneInfo(tz_name)
    except Exception:
        return ZoneInfo("UTC")


def format_in_timezone(iso_timestamp: str, tz: ZoneInfo) -> str:
    # Old rows were written with naive local-time isoformat();
    # astimezone() treats naive datetimes as system local time, which
    # matches how they were originally recorded, so this works for both.
    dt = datetime.fromisoformat(iso_timestamp).astimezone(tz)
    return dt.strftime("%Y-%m-%d %H:%M %Z")


def is_admin(interaction: discord.Interaction) -> bool:
    return bool(interaction.user.guild_permissions.administrator)


# -------------------------
# DISCORD
# -------------------------

intents = discord.Intents.default()
client = discord.Client(intents=intents)
tree = app_commands.CommandTree(client)
scheduler = AsyncIOScheduler()


# -------------------------
# FEEDS
# -------------------------

def load_feeds() -> list:
    # feeds.json is gitignored (it's live, per-deployment state, edited via
    # /addfeed). First run: seed it from the tracked example list so a fresh
    # clone has a working starting point instead of crashing on a missing file.
    if not os.path.exists(FEEDS_FILE):
        if os.path.exists(FEEDS_EXAMPLE_FILE):
            with open(FEEDS_EXAMPLE_FILE, "r") as f:
                feeds = json.load(f)
        else:
            feeds = []

        save_feeds(feeds)
        return feeds

    with open(FEEDS_FILE, "r") as f:
        return json.load(f)


def save_feeds(feeds: list) -> None:
    with open(FEEDS_FILE, "w") as f:
        json.dump(feeds, f, indent=2)
        f.write("\n")


# Feeds default to type "rss" (feedparser handles RSS/Atom XML). type
# "nvd-cve" instead queries the NVD CVE API (JSON) and normalizes results
# into the same FeedParserDict shape feedparser produces, so check_feeds()
# and addfeed() don't need to know which kind of feed they're looking at.

NVD_API_KEY = os.getenv("NVD_API_KEY")
NVD_POLL_LOOKBACK_HOURS = 6
NVD_VALIDATE_LOOKBACK_DAYS = 7


@dataclass
class FetchResult:
    entries: list = field(default_factory=list)
    bozo: bool = False
    bozo_exception: Exception | None = None


def _nvd_published_parsed(iso_timestamp: str):
    # NVD publishes timestamps without a UTC offset (e.g. "2026-08-01T00:00:00.000")
    # but they are UTC. Mirrors feedparser's published_parsed convention (a UTC
    # struct_time) so article_timestamp() works unchanged for both feed types.
    dt = datetime.fromisoformat(iso_timestamp).replace(tzinfo=timezone.utc)
    return dt.utctimetuple()


def _nvd_entry_from_vulnerability(vulnerability: dict) -> FeedParserDict:
    cve = vulnerability["cve"]
    cve_id = cve["id"]

    description = next(
        (d["value"] for d in cve.get("descriptions", []) if d.get("lang") == "en"),
        "",
    )

    entry = FeedParserDict()
    entry["id"] = cve_id
    entry["title"] = cve_id
    entry["link"] = f"https://nvd.nist.gov/vuln/detail/{cve_id}"
    entry["summary"] = description
    entry["published_parsed"] = _nvd_published_parsed(cve["published"])
    return entry


def fetch_nvd_cve_entries(url: str, lookback_hours: int, *, use_last_modified: bool) -> FetchResult:
    now = datetime.now(timezone.utc)
    start = now - timedelta(hours=lookback_hours)
    start_struct = start.utctimetuple()

    if use_last_modified:
        # NVD's documented best practice ("The best, most efficient, practice
        # for keeping up to date with the NVD is to use the date range
        # parameters to request only the CVEs that have been modified since
        # your last request") filters by last-modified date, not published
        # date - a CVE can become queryable after its nominal published
        # timestamp, so filtering on published alone can silently miss it.
        # Most lastModified hits in a short window are edits to older CVEs
        # (filtered out below), not new publications, so fetch generously
        # more than we'll actually post.
        params = {
            "lastModStartDate": start.strftime("%Y-%m-%dT%H:%M:%S.000"),
            "lastModEndDate": now.strftime("%Y-%m-%dT%H:%M:%S.000"),
            "resultsPerPage": max(ARTICLES_PER_FEED * 20, 100),
        }
    else:
        # Used only to validate a feed URL at /addfeed time (a wide, e.g.
        # 7-day, window). A plain published-date query is simpler and
        # guaranteed non-empty for any real NVD endpoint; the lastModified
        # completeness concern above only matters for gap-free periodic
        # polling of a narrow window, not a one-off reachability check -
        # and over a wide window, the top resultsPerPage lastModified hits
        # are dominated by old-CVE edits, so the same approach could return
        # zero newly-published entries here and misreport a healthy feed as
        # broken.
        params = {
            "pubStartDate": start.strftime("%Y-%m-%dT%H:%M:%S.000"),
            "pubEndDate": now.strftime("%Y-%m-%dT%H:%M:%S.000"),
            "resultsPerPage": ARTICLES_PER_FEED,
        }

    request = urllib.request.Request(f"{url}?{urllib.parse.urlencode(params)}")
    if NVD_API_KEY:
        request.add_header("apiKey", NVD_API_KEY)

    try:
        with urllib.request.urlopen(request, context=_ssl_context) as response:
            payload = json.load(response)

        entries = [
            _nvd_entry_from_vulnerability(v) for v in payload.get("vulnerabilities", [])
        ]

        if use_last_modified:
            # Only alert on CVEs newly published in this window - the
            # lastModified query above also surfaces metadata edits to old
            # CVEs, which shouldn't be posted as news.
            entries = [e for e in entries if e["published_parsed"] >= start_struct]

        # check_feeds() assumes feedparser's newest-first convention
        # (it takes entries[:ARTICLES_PER_FEED] then reverses to post oldest
        # to newest); NVD's ordering isn't documented, so sort explicitly.
        entries.sort(key=lambda e: e["published_parsed"], reverse=True)
        return FetchResult(entries=entries[:ARTICLES_PER_FEED])
    except Exception as e:
        return FetchResult(bozo=True, bozo_exception=e)


def fetch_feed_entries(feed: dict, *, validate: bool = False) -> FetchResult:
    feed_type = feed.get("type", "rss")

    if feed_type == "nvd-cve":
        # /addfeed validates against a wide window so a quiet few hours on
        # NVD's side doesn't look like a broken feed; periodic checks use a
        # tight window sized to the poll interval to limit result volume.
        lookback_hours = (
            NVD_VALIDATE_LOOKBACK_DAYS * 24 if validate else NVD_POLL_LOOKBACK_HOURS
        )
        return fetch_nvd_cve_entries(feed["url"], lookback_hours, use_last_modified=not validate)

    data = feedparser.parse(feed["url"], handlers=FEED_FETCH_HANDLERS)
    return FetchResult(
        entries=data.entries,
        bozo=data.bozo,
        bozo_exception=getattr(data, "bozo_exception", None),
    )


async def check_feeds():
    print("Checking RSS feeds...")

    feeds = load_feeds()
    print(f"Loaded {len(feeds)} feeds")

    for feed in feeds:
        try:
            print(f"Checking {feed['name']}")

            # Fetching is a blocking network call either way (feedparser or
            # urllib); run off the event loop so a slow feed can't stall the
            # Discord heartbeat.
            data = await asyncio.to_thread(fetch_feed_entries, feed)

            # Fetch failures (TLS errors, malformed XML/JSON, HTTP errors)
            # are captured into bozo/bozo_exception instead of raising, so
            # without this check a feed would silently produce zero entries.
            if data.bozo and not data.entries:
                print(f"Feed warning {feed['name']}: {data.bozo_exception}")
                continue

            channel_id = CHANNELS.get(feed.get("channel"))
            if channel_id is None:
                continue

            channel = client.get_channel(channel_id)
            if channel is None:
                continue

            for article in reversed(data.entries[:ARTICLES_PER_FEED]):
                article_id = article.get("id", article.link)

                exists = db.execute(
                    "SELECT id FROM articles WHERE id=?",
                    (article_id,),
                ).fetchone()

                if exists:
                    continue

                db.execute(
                    """
                    INSERT INTO articles (id, title, created_at, source, channel)
                    VALUES (?, ?, ?, ?, ?)
                    """,
                    (article_id, article.title, utcnow_iso(), feed["name"], feed["channel"]),
                )
                db.commit()

                description = f"**Category:** {feed['category']}\n**Source:** {feed['name']}"

                summary = extract_summary(article)
                if summary:
                    description += f"\n\n{summary}"

                embed = discord.Embed(
                    title=article.title,
                    url=article.link,
                    description=description,
                    timestamp=article_timestamp(article),
                )

                image_url = extract_image_url(article)
                if image_url:
                    embed.set_image(url=image_url)

                embed.set_footer(text="TELECOM_RSS_BOT")

                await channel.send(embed=embed)
                await asyncio.sleep(1)

                print(f"Posted: {article.title}")

        except Exception as e:
            print(f"Feed error {feed['name']}: {e}")


# -------------------------
# TIMEZONE PRESETS
# -------------------------
# Suggested timezones shown through Discord autocomplete.
#
# Values use IANA timezone names.
# Example:
# "New York": "America/New_York"
#
# Users can still manually enter any valid IANA timezone.
# Validation is handled through Python's zoneinfo module.
#
# These presets focus on:
# - US business markets
# - Telecom/carrier hubs
# - Data center markets
# - Common international locations

TIMEZONE_PRESETS = {
    # United States - East
    "New York / Eastern": "America/New_York",
    "Washington DC / Eastern": "America/New_York",
    "Atlanta / Eastern": "America/New_York",
    "Miami / Eastern": "America/New_York",
    "Boston / Eastern": "America/New_York",

    # United States - Central
    "Chicago / Central": "America/Chicago",
    "Dallas / Central": "America/Chicago",
    "Houston / Central": "America/Chicago",
    "Nashville / Central": "America/Chicago",
    "Minneapolis / Central": "America/Chicago",

    # United States - Mountain
    "Denver / Mountain": "America/Denver",
    "Phoenix / Arizona": "America/Phoenix",
    "Salt Lake City / Mountain": "America/Denver",

    # United States - Pacific
    "Los Angeles / Pacific": "America/Los_Angeles",
    "San Francisco / Pacific": "America/Los_Angeles",
    "Seattle / Pacific": "America/Los_Angeles",
    "Las Vegas / Pacific": "America/Los_Angeles",

    # Alaska / Hawaii
    "Alaska": "America/Anchorage",
    "Hawaii": "Pacific/Honolulu",

    # Telecom / Data Center Hubs
    "Ashburn VA (Data Centers)": "America/New_York",
    "Northern Virginia": "America/New_York",
    "Silicon Valley": "America/Los_Angeles",
    "Phoenix AZ (Data Centers)": "America/Phoenix",
    "Dallas TX (Data Centers)": "America/Chicago",
    "Chicago IL (Data Centers)": "America/Chicago",

    # Canada
    "Toronto": "America/Toronto",
    "Vancouver": "America/Vancouver",
    "Montreal": "America/Toronto",

    # Europe
    "London": "Europe/London",
    "Paris": "Europe/Paris",
    "Frankfurt": "Europe/Berlin",
    "Amsterdam": "Europe/Amsterdam",
    "Madrid": "Europe/Madrid",
    "Stockholm": "Europe/Stockholm",

    # Asia Pacific
    "Tokyo": "Asia/Tokyo",
    "Seoul": "Asia/Seoul",
    "Singapore": "Asia/Singapore",
    "Hong Kong": "Asia/Hong_Kong",
    "Sydney": "Australia/Sydney",
    "Melbourne": "Australia/Melbourne",
    "Auckland": "Pacific/Auckland",

    # Middle East / India
    "Dubai": "Asia/Dubai",
    "India": "Asia/Kolkata",

    # Universal
    "UTC": "UTC",
}


# -------------------------
# COMMANDS
# -------------------------

async def channel_key_autocomplete(interaction: discord.Interaction, current: str):
    return [
        app_commands.Choice(name=key, value=key)
        for key in CHANNELS
        if current.lower() in key.lower()
    ][:25]


@tree.command(name="stats", description="Show RSS statistics")
@app_commands.describe(channel="Optional channel filter")
@app_commands.autocomplete(channel=channel_key_autocomplete)
async def stats(interaction: discord.Interaction, channel: str = None):
    if channel and channel not in CHANNELS:
        await interaction.response.send_message(
            f"❌ Unknown channel category: `{channel}`. Use /listchannels to see valid options.",
            ephemeral=True,
        )
        return

    if channel:
        article_count = db.execute(
            "SELECT COUNT(*) FROM articles WHERE channel=?", (channel,)
        ).fetchone()[0]

        source_count = db.execute(
            "SELECT COUNT(DISTINCT source) FROM articles WHERE channel=?", (channel,)
        ).fetchone()[0]

        latest = db.execute(
            """
            SELECT title, source, created_at FROM articles
            WHERE channel=? ORDER BY created_at DESC LIMIT 1
            """,
            (channel,),
        ).fetchone()
    else:
        article_count = db.execute("SELECT COUNT(*) FROM articles").fetchone()[0]
        source_count = db.execute("SELECT COUNT(DISTINCT source) FROM articles").fetchone()[0]
        latest = db.execute(
            "SELECT title, source, created_at FROM articles ORDER BY created_at DESC LIMIT 1"
        ).fetchone()

    embed = discord.Embed(title="📊 RSS Statistics")
    embed.add_field(name="Articles Tracked", value=str(article_count), inline=True)
    embed.add_field(name="Sources", value=str(source_count), inline=True)

    if latest:
        user_tz = get_user_timezone(interaction.user.id)
        embed.add_field(
            name="Latest Article",
            value=(
                f"**{latest[0]}**\n"
                f"Source: {latest[1]}\n"
                f"Time: {format_in_timezone(latest[2], user_tz)}"
            ),
            inline=False,
        )

    if channel:
        embed.set_footer(text=f"Filtered: {channel}")

    await interaction.response.send_message(embed=embed)


@tree.command(name="listfeeds", description="List configured RSS feed sources")
async def listfeeds(interaction: discord.Interaction):
    feeds = load_feeds()

    embed = discord.Embed(title="📡 Configured Feeds")

    by_channel = {}
    for feed in feeds:
        by_channel.setdefault(feed["channel"], []).append(feed)

    for channel_name in CHANNELS:
        channel_feeds = by_channel.get(channel_name)
        if not channel_feeds:
            continue

        lines = [f"• **{f['name']}** ({f['category']})" for f in channel_feeds]
        embed.add_field(name=f"#{channel_name}", value="\n".join(lines), inline=False)

    embed.set_footer(text=f"{len(feeds)} feeds total")

    await interaction.response.send_message(embed=embed)


@tree.command(name="addfeed", description="Add a new feed source")
@app_commands.describe(
    name="Display name for the source",
    url="Feed URL (RSS/Atom, or the NVD CVE API endpoint for JSON)",
    category="Category label shown in posts (e.g. 'Cybersecurity')",
    channel="Channel to post this feed's articles to",
    feed_type="Feed format (default: RSS/Atom)",
)
@app_commands.choices(
    feed_type=[
        app_commands.Choice(name="RSS/Atom", value="rss"),
        app_commands.Choice(name="NVD CVE API (JSON)", value="nvd-cve"),
    ]
)
@app_commands.autocomplete(channel=channel_key_autocomplete)
async def addfeed(
    interaction: discord.Interaction,
    name: str,
    url: str,
    category: str,
    channel: str,
    feed_type: str = "rss",
):
    if not is_admin(interaction):
        await interaction.response.send_message("❌ Administrator required.", ephemeral=True)
        return

    if channel not in CHANNELS:
        await interaction.response.send_message(
            f"❌ Unknown channel category: `{channel}`. Use /listchannels to see valid options.",
            ephemeral=True,
        )
        return

    await interaction.response.defer(ephemeral=True)

    feeds = load_feeds()

    if any(f["url"] == url for f in feeds):
        await interaction.followup.send("⚠️ That feed URL is already configured.")
        return

    data = await asyncio.to_thread(
        fetch_feed_entries, {"url": url, "type": feed_type}, validate=True
    )

    if data.bozo and not data.entries:
        await interaction.followup.send(f"❌ Couldn't parse that feed: {data.bozo_exception}")
        return

    if not data.entries:
        await interaction.followup.send("❌ Feed parsed but returned zero articles — check the URL.")
        return

    new_feed = {"name": name, "category": category, "channel": channel, "url": url}
    if feed_type != "rss":
        new_feed["type"] = feed_type

    feeds.append(new_feed)
    save_feeds(feeds)

    await interaction.followup.send(
        f"✅ Added **{name}** → #{channel} ({len(data.entries)} articles found)\n"
        f"Most recent: {data.entries[0].title}"
    )


CHANNEL_KEY_RE = re.compile(r"[a-z0-9-]+")


@tree.command(name="listchannels", description="List configured channel categories")
async def listchannels(interaction: discord.Interaction):
    lines = []

    for key, channel_id in CHANNELS.items():
        channel = client.get_channel(channel_id)
        lines.append(f"**{key}** → {channel.mention if channel else f'`{channel_id}` (not found)'}")

    embed = discord.Embed(
        title="📺 Configured Channel Categories",
        description="\n".join(lines) if lines else "No channel categories configured.",
    )

    await interaction.response.send_message(embed=embed)


@tree.command(name="addchannel", description="Add a new channel category")
@app_commands.describe(
    key="Category key used in feeds.json (e.g. 'gaming-news')",
    discord_channel="The Discord channel to post this category's articles to",
)
async def addchannel(
    interaction: discord.Interaction,
    key: str,
    discord_channel: discord.TextChannel,
):
    if not is_admin(interaction):
        await interaction.response.send_message("❌ Administrator required.", ephemeral=True)
        return

    key = key.strip().lower()

    if not CHANNEL_KEY_RE.fullmatch(key):
        await interaction.response.send_message(
            "❌ Key must be lowercase letters, numbers, and hyphens only.", ephemeral=True
        )
        return

    if key in CHANNELS:
        await interaction.response.send_message(
            f"⚠️ `{key}` already exists — use /editchannel to repoint it.", ephemeral=True
        )
        return

    CHANNELS[key] = discord_channel.id
    save_channels(CHANNELS)

    await interaction.response.send_message(
        f"✅ Added category `{key}` → {discord_channel.mention}", ephemeral=True
    )


@tree.command(name="editchannel", description="Repoint an existing channel category to a different channel")
@app_commands.describe(
    key="Existing category key to update",
    discord_channel="The new Discord channel for this category",
)
@app_commands.autocomplete(key=channel_key_autocomplete)
async def editchannel(
    interaction: discord.Interaction,
    key: str,
    discord_channel: discord.TextChannel,
):
    if not is_admin(interaction):
        await interaction.response.send_message("❌ Administrator required.", ephemeral=True)
        return

    if key not in CHANNELS:
        await interaction.response.send_message(
            f"❌ `{key}` isn't a configured category. Use /listchannels to see valid options.",
            ephemeral=True,
        )
        return

    CHANNELS[key] = discord_channel.id
    save_channels(CHANNELS)

    await interaction.response.send_message(
        f"✅ `{key}` now points to {discord_channel.mention}", ephemeral=True
    )


@tree.command(name="removechannel", description="Remove a channel category")
@app_commands.describe(key="Category key to remove")
@app_commands.autocomplete(key=channel_key_autocomplete)
async def removechannel(interaction: discord.Interaction, key: str):
    if not is_admin(interaction):
        await interaction.response.send_message("❌ Administrator required.", ephemeral=True)
        return

    if key not in CHANNELS:
        await interaction.response.send_message(
            f"❌ `{key}` isn't a configured category. Use /listchannels to see valid options.",
            ephemeral=True,
        )
        return

    del CHANNELS[key]
    save_channels(CHANNELS)

    affected = [f["name"] for f in load_feeds() if f["channel"] == key]

    message = f"🗑️ Removed category `{key}`."
    if affected:
        message += f"\n⚠️ These feeds still reference it and will stop posting: {', '.join(affected)}"

    await interaction.response.send_message(message, ephemeral=True)


async def timezone_autocomplete(interaction: discord.Interaction, current: str):
    choices = []

    for name, tz in TIMEZONE_PRESETS.items():
        if current.lower() in name.lower() or current.lower() in tz.lower():
            choices.append(app_commands.Choice(name=f"{name} ({tz})", value=tz))

    return choices[:25]


@tree.command(name="timezone", description="Set your preferred timezone")
@app_commands.describe(timezone="Choose your timezone")
@app_commands.autocomplete(timezone=timezone_autocomplete)
async def set_timezone(interaction: discord.Interaction, timezone: str):
    try:
        ZoneInfo(timezone)
    except Exception:
        await interaction.response.send_message(
            "❌ Invalid timezone. Please select a timezone from the suggestions.",
            ephemeral=True,
        )
        return

    db.execute(
        """
        INSERT INTO users (discord_id, timezone) VALUES (?, ?)
        ON CONFLICT(discord_id) DO UPDATE SET timezone=excluded.timezone
        """,
        (str(interaction.user.id), timezone),
    )
    db.commit()

    await interaction.response.send_message(f"🌎 Timezone set to `{timezone}`", ephemeral=True)


@tree.command(name="refresh", description="Manually check RSS feeds")
async def refresh(interaction: discord.Interaction):
    if not is_admin(interaction):
        await interaction.response.send_message("❌ Administrator required.", ephemeral=True)
        return

    await interaction.response.defer()
    await check_feeds()
    await interaction.followup.send("✅ RSS refresh complete")


@tree.command(name="cleanup", description="Remove old article records")
@app_commands.describe(days="Remove articles older than this many days")
async def cleanup(interaction: discord.Interaction, days: int = 30):
    if not is_admin(interaction):
        await interaction.response.send_message("❌ Administrator required.", ephemeral=True)
        return

    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()

    result = db.execute("DELETE FROM articles WHERE created_at < ?", (cutoff,))
    removed = result.rowcount
    db.commit()

    await interaction.response.send_message(f"🧹 Removed {removed} records.")


@tree.command(name="clear", description="Clear RSS cache")
async def clear(interaction: discord.Interaction):
    if not is_admin(interaction):
        await interaction.response.send_message("❌ Administrator required.", ephemeral=True)
        return

    db.execute("DELETE FROM articles")
    db.commit()

    await interaction.response.send_message("⚠️ RSS cache cleared.")


@tree.command(name="purge_news", description="Delete bot posts and reset RSS")
async def purge_news(interaction: discord.Interaction):
    if not is_admin(interaction):
        await interaction.response.send_message("❌ Administrator required.", ephemeral=True)
        return

    await interaction.response.defer(ephemeral=True)

    deleted = 0

    for channel_id in CHANNELS.values():
        channel = client.get_channel(channel_id)

        if not isinstance(channel, discord.TextChannel):
            continue

        async for message in channel.history(limit=None):
            if message.author == client.user:
                try:
                    await message.delete()
                    deleted += 1
                    await asyncio.sleep(1)
                except Exception as e:
                    print(e)

    db.execute("DELETE FROM articles")
    db.commit()

    await interaction.followup.send(f"🧹 Deleted {deleted} messages and cleared cache.")


# -------------------------
# STARTUP
# -------------------------

@client.event
async def on_ready():
    print(f"Logged in as {client.user}")

    # on_ready can fire again after a gateway reconnect; only initialize
    # the scheduler and sync commands once per process.
    if scheduler.running:
        return

    synced = await tree.sync()
    print(f"Synced {len(synced)} commands")

    await check_feeds()

    scheduler.add_job(check_feeds, "interval", minutes=FEED_CHECK_INTERVAL_MINUTES)
    scheduler.start()

    print("RSS Scheduler Started")


client.run(TOKEN)
