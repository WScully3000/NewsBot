import asyncio
import hashlib
import json
import os
import re
import sqlite3
import ssl
import time
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

FEEDS_FILE = os.getenv("FEEDS_FILE", "feeds.json")
FEEDS_EXAMPLE_FILE = "feeds.example.json"
CHANNELS_FILE = os.getenv("CHANNELS_FILE", "channels.json")
CHANNELS_EXAMPLE_FILE = "channels.example.json"
DB_FILE = os.getenv("DB_FILE", "news.db")
ARTICLES_PER_FEED = int(os.getenv("ARTICLES_PER_FEED", "5"))
FEED_CHECK_INTERVAL_MINUTES = int(os.getenv("FEED_CHECK_INTERVAL_MINUTES", "15"))


def save_channels(channels: dict) -> None:
    with open(CHANNELS_FILE, "w") as f:
        json.dump(channels, f, indent=2)
        f.write("\n")


def load_channels() -> dict:
    # First run: no channels.json yet, so seed it from the tracked example
    # template (ships empty - add your own via /addchannel or by editing
    # channels.json directly).
    if not os.path.exists(CHANNELS_FILE):
        if os.path.exists(CHANNELS_EXAMPLE_FILE):
            with open(CHANNELS_EXAMPLE_FILE, "r") as f:
                channels = json.load(f)
        else:
            channels = {}

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


def article_title(article) -> str:
    # feedparser's FeedParserDict raises AttributeError (not just missing)
    # on `.title` when a feed entry has no <title> - not rare enough to
    # skip guarding, since it previously crashed check_feeds() before the
    # dedup INSERT committed, permanently stalling that feed every cycle.
    return article.get("title") or "(untitled)"


def article_link(article) -> str:
    # Same AttributeError risk as .title above - generic-json feeds in
    # particular can't guarantee an arbitrary API supplies a usable link.
    return article.get("link") or ""


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


def truncate_text(text: str, max_chars: int) -> str:
    # Discord embed field values cap at 1024 chars, titles at 256 - an
    # oversize value raises HTTPException from channel.send() rather than
    # failing early, so anything built from unbounded upstream text needs
    # this guard.
    if len(text) <= max_chars:
        return text

    return text[:max_chars].rsplit(" ", 1)[0] + "…"


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


CVE_SEVERITY_STYLE = {
    "CRITICAL": ("🔴", 0xE74C3C),
    "HIGH": ("🟠", 0xE67E22),
    "MEDIUM": ("🟡", 0xF1C40F),
    "LOW": ("🟢", 0x2ECC71),
}


def _nvd_severity(cve: dict) -> str | None:
    # Prefer the newest CVSS version present; NVD doesn't backfill older
    # CVEs with newer metrics, so a given CVE may only have v2 data - and
    # a growing share of new CVEs (CNA-supplied) carry only a v4.0 score,
    # with no v3.x/v2 fallback at all.
    # v3.x/v4.0 carry baseSeverity inside cvssData; v2 doesn't define
    # severity itself, so NVD's API adds it as a sibling field instead.
    metrics = cve.get("metrics", {})
    for key in ("cvssMetricV40", "cvssMetricV31", "cvssMetricV30", "cvssMetricV2"):
        metric_list = metrics.get(key)
        if not metric_list:
            continue

        metric = metric_list[0]
        severity = metric.get("cvssData", {}).get("baseSeverity") or metric.get("baseSeverity")
        if severity:
            return severity.upper()

    return None


CVE_TITLE_MAX_CHARS = 100

# NVD's API has no title field for CVE records at all - these are the
# common lead-in phrases NVD/CNA descriptions open with, stripped so the
# synthesized title below leads with the actually-informative part
# (product/component) instead of restating "a vulnerability was found".
_CVE_TITLE_BOILERPLATE_RE = re.compile(
    r"^(?:"
    r"an issue (?:was|has been) discovered in|"
    r"a (?:vulnerability|flaw) (?:was|has been) (?:discovered|found|identified) in|"
    r"multiple vulnerabilities (?:were|have been) discovered in|"
    r"it was discovered that"
    r")\s+",
    re.IGNORECASE,
)
_SENTENCE_SPLIT_RE = re.compile(r"(?<=[.!?])\s")


def _cve_title_snippet(description: str) -> str | None:
    # Derives a short label from the description's first sentence so the
    # embed title isn't just a bare CVE ID. This is string trimming, not
    # summarization - it won't always read as a polished title, but it's
    # more useful at a glance than the ID alone.
    if not description:
        return None

    text = _CVE_TITLE_BOILERPLATE_RE.sub("", description.strip())
    # Stripping the lead-in above often leaves a dangling article ("...in
    # the Tempo Operator" -> "the Tempo Operator") - drop it too.
    text = re.sub(r"^(?:the|an?)\s+", "", text, flags=re.IGNORECASE)
    text = _SENTENCE_SPLIT_RE.split(text, maxsplit=1)[0].strip().rstrip(".")

    if not text:
        return None

    if len(text) > CVE_TITLE_MAX_CHARS:
        text = text[:CVE_TITLE_MAX_CHARS].rsplit(" ", 1)[0] + "…"

    return text


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
    entry["nvd_severity"] = _nvd_severity(cve)
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
            # NVD caps resultsPerPage at 2000. Always request the max here -
            # this is a periodic poll of a short window, and a truncated
            # fetch would silently drop CVEs published during a disclosure
            # spike instead of just delaying them (the next poll's window
            # won't reach back far enough to recover them).
            "resultsPerPage": 2000,
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

        # check_feeds() assumes feedparser's newest-first convention before
        # reversing to post oldest to newest; NVD's ordering isn't
        # documented, so sort explicitly.
        entries.sort(key=lambda e: e["published_parsed"], reverse=True)

        # The one-off /addfeed validation check wants a small preview
        # sample; the periodic poll must post every new CVE it finds, or
        # anything beyond a fixed cap is silently lost for good once the
        # lookback window moves past it.
        if not use_last_modified:
            entries = entries[:ARTICLES_PER_FEED]

        return FetchResult(entries=entries)
    except Exception as e:
        return FetchResult(bozo=True, bozo_exception=e)


# CISA's Known Exploited Vulnerabilities (KEV) Catalog: vulnerabilities with
# confirmed active exploitation, which federal agencies are required to
# remediate by a mandated due date. Served as a static JSON file that -
# unlike CISA's RSS/HTML advisory pages - isn't behind the Akamai bot-
# protection WAF blocking those. Two feed types share this data:
# "cisa-kev-summary" (a short announcement, like CISA's own Current
# Activity bulletins - which we can't fetch directly, same WAF issue) and
# "cisa-kev-detail" (the full per-CVE breakdown). Both poll the same URL;
# the id_prefix keeps their dedup rows from colliding with each other or
# with a bare CVE ID already posted by an nvd-cve feed.
CISA_KEV_POLL_LOOKBACK_DAYS = 2
CISA_KEV_ID_PREFIXES = {
    "cisa-kev-summary": "kev-summary",
    "cisa-kev-detail": "kev-detail",
}


def _date_only_parsed(date_str: str):
    # KEV's dateAdded is a bare date (no time component) - mirrors
    # feedparser's published_parsed convention (a UTC struct_time) so
    # article_timestamp() works unchanged for this feed type too.
    dt = datetime.strptime(date_str, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    return dt.utctimetuple()


def _nvd_severity_for_cve(cve_id: str) -> str | None:
    # KEV entries carry no CVSS data of their own - cross-reference NVD for
    # the real severity instead of guessing. Failures here (including NVD
    # rate-limiting) must degrade to "unknown" rather than fail the whole
    # KEV fetch - the KEV listing itself is the point, severity is a nice-
    # to-have on top of it.
    params = {"cveId": cve_id}
    request = urllib.request.Request(
        f"https://services.nvd.nist.gov/rest/json/cves/2.0?{urllib.parse.urlencode(params)}"
    )
    if NVD_API_KEY:
        request.add_header("apiKey", NVD_API_KEY)

    try:
        with urllib.request.urlopen(request, context=_ssl_context) as response:
            payload = json.load(response)

        vulnerabilities = payload.get("vulnerabilities", [])
        return _nvd_severity(vulnerabilities[0]["cve"]) if vulnerabilities else None
    except Exception as e:
        print(f"CISA KEV: NVD severity lookup failed for {cve_id}: {e}")
        return None


def _kev_entry_from_vulnerability(vulnerability: dict, id_prefix: str) -> FeedParserDict:
    cve_id = vulnerability["cveID"]

    entry = FeedParserDict()
    entry["id"] = f"{id_prefix}:{cve_id}"
    entry["cve_id"] = cve_id
    entry["title"] = vulnerability.get("vulnerabilityName") or cve_id
    # KEV entries have no per-CVE cisa.gov URL of their own; NVD's detail
    # page is the one link CISA itself includes in every entry's notes.
    entry["link"] = f"https://nvd.nist.gov/vuln/detail/{cve_id}"
    entry["summary"] = vulnerability.get("shortDescription", "")
    entry["published_parsed"] = _date_only_parsed(vulnerability["dateAdded"])
    entry["kev_vendor_project"] = vulnerability.get("vendorProject", "")
    entry["kev_product"] = vulnerability.get("product", "")
    entry["kev_date_added"] = vulnerability.get("dateAdded", "")
    entry["kev_due_date"] = vulnerability.get("dueDate", "")
    entry["kev_required_action"] = vulnerability.get("requiredAction", "")
    return entry


def fetch_cisa_kev_entries(url: str, id_prefix: str, *, validate: bool = False) -> FetchResult:
    request = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})

    try:
        with urllib.request.urlopen(request, context=_ssl_context) as response:
            payload = json.load(response)

        vulnerabilities = payload.get("vulnerabilities", [])

        if not validate:
            # The catalog file has no pagination/date filtering server-side
            # (it's the full ~1700-entry history every time) - without this
            # window, adding the feed for the first time would flood the
            # channel with every KEV entry ever published instead of just
            # new ones.
            cutoff = (datetime.now(timezone.utc) - timedelta(days=CISA_KEV_POLL_LOOKBACK_DAYS)).date()
            vulnerabilities = [
                v for v in vulnerabilities
                if datetime.strptime(v["dateAdded"], "%Y-%m-%d").date() >= cutoff
            ]

        entries = [_kev_entry_from_vulnerability(v, id_prefix) for v in vulnerabilities]
        entries.sort(key=lambda e: e["published_parsed"], reverse=True)

        if validate:
            entries = entries[:ARTICLES_PER_FEED]
        elif id_prefix == "kev-detail":
            # Cross-reference NVD for real severity per new entry. Sequential
            # and rate-limit-aware (NVD allows 5 req/30s unauthenticated, 50
            # req/30s with an API key) - this only runs over the handful of
            # entries newly added in the last CISA_KEV_POLL_LOOKBACK_DAYS,
            # not the whole catalog.
            delay = 0.6 if NVD_API_KEY else 6.0
            for entry in entries:
                entry["nvd_severity"] = _nvd_severity_for_cve(entry["cve_id"])
                time.sleep(delay)

        return FetchResult(entries=entries)
    except Exception as e:
        return FetchResult(bozo=True, bozo_exception=e)


# "generic-json" is a no-code adapter for future JSON API sources that
# don't need bespoke logic (unlike nvd-cve and cisa-kev-*, which cross-
# reference other data and split across channels - that kind of behavior
# always needs real code, this only covers "a flat list of items with
# simple fields"). Configured entirely through /addjsonfeed via dot-paths
# into the response - e.g. "vulnerabilities" or "cve.id" or
# "descriptions[0].value" - so adding a new simple JSON source doesn't
# require touching this file.
_JSON_PATH_SEGMENT_RE = re.compile(r"([^.\[\]]+)|\[(\d+)\]")


def resolve_json_path(data, path: str):
    if not path:
        return data

    current = data
    for key, index in _JSON_PATH_SEGMENT_RE.findall(path):
        if key:
            if not isinstance(current, dict) or key not in current:
                return None
            current = current[key]
        else:
            if not isinstance(current, list) or int(index) >= len(current):
                return None
            current = current[int(index)]

    return current


def _parse_generic_date(value):
    # Returns a UTC struct_time (feedparser's published_parsed convention)
    # or None if the value is missing/unparseable - article_timestamp()
    # already falls back to "now" when published_parsed isn't set, so
    # there's no need to duplicate that fallback here.
    if isinstance(value, (int, float)):
        try:
            return datetime.fromtimestamp(value, tz=timezone.utc).utctimetuple()
        except (ValueError, OSError, OverflowError):
            return None

    if isinstance(value, str) and value:
        try:
            dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt.astimezone(timezone.utc).utctimetuple()
        except ValueError:
            pass

        try:
            return datetime.strptime(value, "%Y-%m-%d").replace(tzinfo=timezone.utc).utctimetuple()
        except ValueError:
            pass

    return None


GENERIC_JSON_TITLE_MAX_CHARS = 256


def _generic_json_entry_from_item(item, feed: dict) -> FeedParserDict:
    config = feed.get("json_config", {})
    entry = FeedParserDict()

    raw_id = resolve_json_path(item, config.get("id_path", ""))
    # An arbitrary future API's id field could be missing or non-unique in
    # ways we can't predict - hash the whole item as a last resort so a bad
    # id_path degrades to "every item looks new" rather than colliding
    # everything onto one dedup row.
    entry["id"] = (
        str(raw_id) if raw_id is not None
        else hashlib.sha256(json.dumps(item, sort_keys=True, default=str).encode()).hexdigest()
    )

    raw_title = resolve_json_path(item, config.get("title_path", ""))
    entry["title"] = truncate_text(str(raw_title), GENERIC_JSON_TITLE_MAX_CHARS) if raw_title else entry["id"]

    # Always set link (even if link_path is blank or doesn't resolve) -
    # article_link() would otherwise fall back to "", which is safe for
    # Discord but means a dead, unclickable title for no reason when the
    # feed's own URL is a reasonable substitute.
    raw_link = resolve_json_path(item, config.get("link_path", "")) if config.get("link_path") else None
    entry["link"] = str(raw_link) if raw_link else feed.get("url", "")

    raw_summary = resolve_json_path(item, config.get("summary_path", ""))
    entry["summary"] = str(raw_summary) if raw_summary is not None else ""

    parsed_date = _parse_generic_date(resolve_json_path(item, config.get("date_path", "")))
    if parsed_date:
        entry["published_parsed"] = parsed_date

    return entry


def fetch_generic_json_entries(feed: dict, *, validate: bool = False) -> FetchResult:
    config = feed.get("json_config", {})
    request = urllib.request.Request(feed["url"], headers={"User-Agent": "Mozilla/5.0"})

    try:
        with urllib.request.urlopen(request, context=_ssl_context) as response:
            payload = json.load(response)

        items_path = config.get("items_path", "")
        items = resolve_json_path(payload, items_path)
        if items is None:
            items = payload if isinstance(payload, list) else None

        if not isinstance(items, list):
            return FetchResult(
                bozo=True,
                bozo_exception=ValueError(
                    f"items_path '{items_path}' did not resolve to a list in the response"
                ),
            )

        entries = [_generic_json_entry_from_item(item, feed) for item in items]
        entries.sort(key=lambda e: e.get("published_parsed") or time.gmtime(0), reverse=True)

        if validate:
            entries = entries[:ARTICLES_PER_FEED]

        return FetchResult(entries=entries)
    except Exception as e:
        return FetchResult(bozo=True, bozo_exception=e)


NEWS_ICON_EMOJI_NAME = "newsicon"


def news_icon() -> str:
    # :newsicon: is a custom emoji uploaded separately via Discord (Server
    # Settings -> Emoji), looked up by name here instead of hardcoding an
    # ID so it keeps working if it's ever deleted and re-uploaded.
    emoji = discord.utils.get(client.emojis, name=NEWS_ICON_EMOJI_NAME)
    return str(emoji) if emoji else "📰"


def build_article_embed(article, feed: dict) -> discord.Embed:
    timestamp = article_timestamp(article)

    if feed.get("type") == "nvd-cve":
        severity = article.get("nvd_severity")
        dot, color = CVE_SEVERITY_STYLE.get(severity, ("⚪", None))

        title = f"🛡️ {article_title(article)}"
        title_snippet = _cve_title_snippet(article.get("summary", ""))
        if title_snippet:
            title += f" — {title_snippet}"

        embed = discord.Embed(
            title=title,
            url=article_link(article),
            description=extract_summary(article) or "No description available yet.",
            timestamp=timestamp,
            color=color,
        )

        embed.add_field(name="Severity", value=f"{dot} {severity or 'Not yet scored'}", inline=True)
        embed.add_field(name="Date", value=timestamp.strftime("%Y-%m-%d"), inline=True)
        embed.set_footer(text=f"Source: {feed['name']} • CVE Auto-Poster")
        return embed

    if feed.get("type") == "cisa-kev-summary":
        cve_id = article.get("cve_id", "")

        embed = discord.Embed(
            title="📢 CISA Adds One Known Exploited Vulnerability to Catalog",
            url=article_link(article),
            description=(
                "CISA has added one new vulnerability to its Known Exploited "
                "Vulnerabilities (KEV) Catalog, based on evidence of active "
                f"exploitation. {cve_id} {article_title(article)}\n"
                f"{extract_summary(article)}"
            ),
            timestamp=timestamp,
        )
        embed.set_footer(text="Source: CISA KEV Catalog")
        return embed

    if feed.get("type") == "cisa-kev-detail":
        cve_id = article.get("cve_id", "")
        severity = article.get("nvd_severity")
        dot, color = CVE_SEVERITY_STYLE.get(severity, ("⚪", None))

        title = truncate_text(f"🛡️ {cve_id}: {article_title(article)}", 256)

        embed = discord.Embed(
            title=title,
            url=article_link(article),
            description=extract_summary(article) or "No description available yet.",
            timestamp=timestamp,
            color=color,
        )

        severity_label = f"{severity or 'Not yet scored'} (Actively Exploited)"
        embed.add_field(name="Severity", value=f"{dot} {severity_label}", inline=True)
        embed.add_field(
            name="Vendor/Product",
            value=f"{article.get('kev_vendor_project', '?')} - {article.get('kev_product', '?')}",
            inline=True,
        )
        embed.add_field(name="Date Added", value=article.get("kev_date_added", "?"), inline=True)

        due_date = article.get("kev_due_date")
        if due_date:
            embed.add_field(name="⚠️ Due Date", value=due_date, inline=False)

        required_action = article.get("kev_required_action")
        if required_action:
            embed.add_field(
                name="Required Action", value=truncate_text(required_action, 1024), inline=False
            )

        embed.set_footer(text="Source: CISA KEV")
        return embed

    embed = discord.Embed(
        title=f"{news_icon()} {article_title(article)}",
        url=article_link(article),
        description=extract_summary(article) or "No summary available.",
        timestamp=timestamp,
    )

    image_url = extract_image_url(article)
    if image_url:
        embed.set_image(url=image_url)

    embed.add_field(name="Source", value=feed["name"], inline=True)
    embed.add_field(name="Category", value=feed["category"], inline=True)
    embed.add_field(name="Time/Date", value=timestamp.strftime("%Y-%m-%d %H:%M UTC"), inline=True)
    return embed


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

    if feed_type in CISA_KEV_ID_PREFIXES:
        return fetch_cisa_kev_entries(feed["url"], CISA_KEV_ID_PREFIXES[feed_type], validate=validate)

    if feed_type == "generic-json":
        return fetch_generic_json_entries(feed, validate=validate)

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
                print(f"Skipping {feed['name']}: channel key '{feed.get('channel')}' not in channels.json")
                continue

            channel = client.get_channel(channel_id)
            if channel is None:
                print(f"Skipping {feed['name']}: bot can't see channel ID {channel_id} (wrong ID, or bot not in that server/channel)")
                continue

            if feed.get("type") == "generic-json" and data.entries:
                # Unlike nvd-cve/cisa-kev (researched ahead of time to size
                # a safe lookback window), an arbitrary future JSON API's
                # total history size is unknown - it could be 10 items or
                # 10,000. On this feed's first-ever check, silently seed
                # dedup state for everything currently returned instead of
                # flooding the channel; only genuinely new items post from
                # the next check onward.
                already_seen = db.execute(
                    "SELECT COUNT(*) FROM articles WHERE source=?", (feed["name"],)
                ).fetchone()[0]

                if already_seen == 0:
                    for article in data.entries:
                        article_id = article.get("id", article_link(article))
                        db.execute(
                            """
                            INSERT OR IGNORE INTO articles (id, title, created_at, source, channel)
                            VALUES (?, ?, ?, ?, ?)
                            """,
                            (article_id, article_title(article), utcnow_iso(), feed["name"], feed["channel"]),
                        )
                    db.commit()
                    print(f"Seeded {len(data.entries)} existing items for {feed['name']} (first check, nothing posted)")
                    continue

            # fetch_feed_entries() already sizes these result sets correctly
            # (every new item found, not just the first N) - re-slicing here
            # would reintroduce the same silent-drop bug.
            uncapped_feed_types = {"nvd-cve", *CISA_KEV_ID_PREFIXES}
            entries = (
                data.entries
                if feed.get("type") in uncapped_feed_types
                else data.entries[:ARTICLES_PER_FEED]
            )

            for article in reversed(entries):
                article_id = article.get("id", article_link(article))

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
                    (article_id, article_title(article), utcnow_iso(), feed["name"], feed["channel"]),
                )
                db.commit()

                embed = build_article_embed(article, feed)

                await channel.send(embed=embed)
                await asyncio.sleep(1)

                print(f"Posted: {article_title(article)}")

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
# - Major US metro areas
# - Common international locations

TIMEZONE_PRESETS = {
    # United States - East
    "New York / Eastern": "America/New_York",
    "Washington DC / Eastern": "America/New_York",
    "Atlanta / Eastern": "America/New_York",
    "Miami / Eastern": "America/New_York",
    "Boston / Eastern": "America/New_York",
    "Northern Virginia": "America/New_York",

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
    "Silicon Valley": "America/Los_Angeles",

    # Alaska / Hawaii
    "Alaska": "America/Anchorage",
    "Hawaii": "Pacific/Honolulu",

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
        app_commands.Choice(name="CISA KEV — Summary", value="cisa-kev-summary"),
        app_commands.Choice(name="CISA KEV — Detailed", value="cisa-kev-detail"),
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

    if any(f["url"] == url and f["channel"] == channel for f in feeds):
        await interaction.followup.send("⚠️ That feed URL is already configured for this channel.")
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
        f"Most recent: {article_title(data.entries[0])}"
    )


@tree.command(name="addjsonfeed", description="Add a generic JSON API feed source (no code required)")
@app_commands.describe(
    name="Display name for the source",
    url="JSON API endpoint URL",
    items_path="Dot-path to the list of items in the response (e.g. 'vulnerabilities'; blank if the response IS the list)",
    id_path="Dot-path to each item's unique id (e.g. 'id' or 'cve.id')",
    title_path="Dot-path to each item's title",
    date_path="Dot-path to each item's publish/added date (ISO timestamp or YYYY-MM-DD)",
    channel="Channel to post this feed's articles to",
    category="Category label shown in posts (e.g. 'Advisories')",
    link_path="Dot-path to each item's URL (optional - blank links to the feed's own URL instead)",
    summary_path="Dot-path to each item's description/summary (optional)",
)
@app_commands.autocomplete(channel=channel_key_autocomplete)
async def addjsonfeed(
    interaction: discord.Interaction,
    name: str,
    url: str,
    items_path: str,
    id_path: str,
    title_path: str,
    date_path: str,
    channel: str,
    category: str,
    link_path: str = "",
    summary_path: str = "",
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

    if any(f["url"] == url and f["channel"] == channel for f in feeds):
        await interaction.followup.send("⚠️ That feed URL is already configured for this channel.")
        return

    new_feed = {
        "name": name,
        "category": category,
        "channel": channel,
        "url": url,
        "type": "generic-json",
        "json_config": {
            "items_path": items_path,
            "id_path": id_path,
            "title_path": title_path,
            "link_path": link_path,
            "summary_path": summary_path,
            "date_path": date_path,
        },
    }

    data = await asyncio.to_thread(fetch_feed_entries, new_feed, validate=True)

    if data.bozo and not data.entries:
        await interaction.followup.send(f"❌ Couldn't parse that feed: {data.bozo_exception}")
        return

    if not data.entries:
        await interaction.followup.send(
            "❌ Feed parsed but returned zero items — double check items_path and the field paths."
        )
        return

    feeds.append(new_feed)
    save_feeds(feeds)

    await interaction.followup.send(
        f"✅ Added **{name}** → #{channel} ({len(data.entries)} items found)\n"
        f"Most recent: {article_title(data.entries[0])}\n"
        f"⏳ The first scheduled check will silently seed dedup state for everything the API currently "
        f"returns — nothing posts until a genuinely new item shows up after that."
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
