"""Discord webhook delivery: batched embeds with rate-limit-aware retries."""

from __future__ import annotations

import logging
import re
import time
from datetime import date, datetime
from zoneinfo import ZoneInfo

import requests

from normalize import normalize_company
from terms import season_label, season_sort_key

log = logging.getLogger(__name__)

EMBEDS_PER_MESSAGE = 10  # Discord's hard cap
MAX_RETRIES = 4
TIMEOUT = 20

CATEGORY_LABELS = {
    "software_engineering": "Software Engineering",
    "machine_learning": "Machine Learning",
    "embedded_systems": "Embedded Systems",
    "other": "Other",
}
CATEGORY_COLORS = {
    "software_engineering": 0x2ECC71,
    "machine_learning": 0xE91E63,
    "embedded_systems": 0x3498DB,
    "other": 0x95A5A6,
}
# Discord shows ONE tag per posting; a title matching several categories takes the
# first here ("Embedded Software Intern" -> EMBEDDED, "Software Engineer, AI
# Intern" -> AI/ML). The board still shows every category a posting earned.
TAG_PRIORITY = [
    ("embedded_systems", "EMBEDDED"),
    ("machine_learning", "AI/ML"),
    ("software_engineering", "SWE"),
]


def primary_tag(categories: list[str]) -> tuple[str, str]:
    """(category key, short tag) by TAG_PRIORITY; ("other", "OTHER") if none."""
    for key, tag in TAG_PRIORITY:
        if key in categories:
            return key, tag
    return "other", "OTHER"


def display_company(posting: dict) -> str:
    """The notify-list name, with the posting's own name after it if different.

    "ByteDance (TikTok)": reported as the parent, as asked, but the name you'd
    search on the careers site stays visible. A mere spelling variant
    ("NVIDIA, Inc." vs "NVIDIA") isn't worth the parentheses.
    """
    raw = posting.get("company") or "Unknown"
    parent = posting.get("notify_company")
    if not parent or normalize_company(parent) == normalize_company(raw):
        return raw
    return f"{parent} ({raw})"
DEFAULT_COLOR = 0x95A5A6

# "Today" for day separators and "posted today/yesterday" means the reader's day,
# not UTC's -- a UTC date would flip at 7-8 PM Eastern and stamp evening postings
# with tomorrow. Same zone the board uses (scripts/render_board.py).
NOTIFY_TZ = ZoneInfo("America/New_York")


def today_local() -> date:
    return datetime.now(NOTIFY_TZ).date()


def day_separator(day: date) -> str:
    """Content-only message that starts each day's notifications in the channel.

    Discord's own date dividers are small and only show between users' messages;
    with one webhook posting all day they're easy to miss, so this draws its own.
    """
    return f"━━━━━━━━━━━━━━━━━━━━━━━━\n## \U0001F4C5 {day:%A, %B} {day.day}"


def order_for_notify(postings: list[dict]) -> list[dict]:
    """Season groups together (earliest season first, unlisted last), newest first within.

    main.py marks `to_send[:sent]` as seen, so it must call this BEFORE notify() and
    pass the reordered list -- notify() preserves order, it never re-sorts.
    """
    by_date = sorted(postings, key=lambda p: p.get("date_posted", ""), reverse=True)
    return sorted(by_date, key=lambda p: season_sort_key(p.get("terms") or []))


def _batches(postings: list[dict]) -> list[tuple[str, list[dict]]]:
    """Split into (summary line, <=10 postings) batches, preserving order.

    The summary line is the message's text content. It's kept short because it's
    what a phone push notification shows -- embeds alone push as a blank alert.
    """
    batches = []
    for i in range(0, len(postings), EMBEDS_PER_MESSAGE):
        chunk = postings[i : i + EMBEDS_PER_MESSAGE]
        companies = list(dict.fromkeys(p.get("notify_company") or p.get("company", "Unknown")
                                       for p in chunk))
        shown = ", ".join(companies[:4])
        if len(companies) > 4:
            shown += f" +{len(companies) - 4} more"
        noun = "posting" if len(chunk) == 1 else "postings"
        batches.append((f"\U0001F514 {len(chunk)} new {noun}: {shown}", chunk))
    return batches


def build_embed(posting: dict, today: date | None = None) -> dict:
    company = display_company(posting)
    title = posting.get("title", "Unknown role")
    location = posting.get("location", "Unspecified")
    categories = posting.get("role_categories") or []
    cat_labels = " / ".join(CATEGORY_LABELS.get(c, c) for c in categories) or "Uncategorized"
    season = season_label(posting.get("terms") or [], emoji=True)
    primary, tag = primary_tag(categories)

    # One fact per line: the old single " · "-joined line buried the posted date in
    # the footer, the smallest and greyest text an embed has.
    lines = [
        f"\U0001F5D3\uFE0F {posted_phrase(posting.get('date_posted', ''), today or today_local())}",
        f"\U0001F4CD {location}",
        f"\U0001F3F7\uFE0F {cat_labels}",
    ]

    embed = {
        # Author renders as a bold line ABOVE the title -- the most prominent slot an
        # embed has short of the title itself, so the season is the first thing read.
        "author": {"name": season[:256]},
        "title": f"[{tag}] {company} — {title}"[:256],
        "description": "\n".join(lines)[:4096],
        "color": CATEGORY_COLORS.get(primary, DEFAULT_COLOR),
        "footer": {"text": f"via {posting.get('source_repo', '') or 'unknown source'}"},
    }
    # Discord rejects the whole payload if `url` is present but not a valid URL.
    url = posting.get("url") or ""
    if url.startswith("http"):
        embed["url"] = url
    return embed


def posted_phrase(mmddyyyy: str, today: date) -> str:
    """'Posted **today** (Thu, Nov 26)' / '**yesterday**' / '**3 days ago**'.

    Relative first because that's the question being asked ("how fresh is this?");
    the weekday + date stays alongside so it can be matched to a day separator.
    """
    try:
        posted = datetime.strptime(mmddyyyy, "%m%d%Y").date()
    except (TypeError, ValueError):
        return "Posted date not listed"
    age = (today - posted).days
    stamp = f"{posted:%a, %b} {posted.day}"
    if age < 0:  # a source dated it ahead of our clock; don't print "-1 days ago"
        return f"Posted {stamp}"
    rel = "today" if age == 0 else "yesterday" if age == 1 else f"{age} days ago"
    return f"Posted **{rel}** ({stamp})"


def send_day_separator(webhook_url: str, day: date, *, dry_run: bool = False) -> bool:
    """Post the day divider. main.py remembers the day so it's sent once, not per run."""
    content = day_separator(day)
    if dry_run:
        log.info("[dry-run] day separator: %s", content.splitlines()[-1])
        return True
    if not webhook_url:
        return False
    return _post_with_retry(webhook_url, {"content": content})


def notify(webhook_url: str, postings: list[dict], *, dry_run: bool = False) -> int:
    """Send postings as batched embeds, in the order given.

    Returns the count successfully sent; those are always a prefix of `postings`.
    Pass the list through order_for_notify() first so seasons arrive grouped.
    """
    if not postings:
        return 0
    if not webhook_url and not dry_run:
        log.error("DISCORD_WEBHOOK_URL is not set -- cannot notify")
        return 0

    sent = 0
    batches = _batches(postings)
    today = today_local()

    for index, (header, batch) in enumerate(batches, start=1):
        payload = {"content": header, "embeds": [build_embed(p, today) for p in batch]}
        if dry_run:
            log.info("[dry-run] batch %d/%d: %d embeds -- %s",
                     index, len(batches), len(batch), header)
            for posting in batch:
                log.info("[dry-run]   [%s] %s — %s (%s) [%s] %s",
                         primary_tag(posting.get("role_categories") or [])[1],
                         display_company(posting), posting.get("title"), posting.get("location"),
                         season_label(posting.get("terms") or []),
                         posted_phrase(posting.get("date_posted", ""), today).replace("**", ""))
            sent += len(batch)
            continue

        if _post_with_retry(webhook_url, payload):
            sent += len(batch)
        else:
            log.error("batch %d/%d failed after retries -- not marking as sent", index, len(batches))
            break  # stop early; unsent ids stay unseen and retry next run

        if index < len(batches):
            time.sleep(1.0)  # stay well under the webhook rate limit

    return sent


def _redact(value, webhook_url: str) -> str:
    """Strip the webhook URL (and any Discord webhook URL) from a log message."""
    text = str(value)
    if webhook_url:
        text = text.replace(webhook_url, "<webhook redacted>")
        # Also catch the token alone, in case only the tail was interpolated.
        tail = webhook_url.rstrip("/").rsplit("/", 1)[-1]
        if len(tail) > 8:
            text = text.replace(tail, "<redacted>")
    return re.sub(
        r"https?://\S*?discord(?:app)?\.com/api/webhooks/\S*",
        "<webhook redacted>",
        text,
    )


def _post_with_retry(webhook_url: str, payload: dict) -> bool:
    delay = 1.0
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            response = requests.post(webhook_url, json=payload, timeout=TIMEOUT)
        except requests.RequestException as exc:
            # requests embeds the full URL in connection errors. Never let the
            # webhook URL reach the logs -- GitHub's secret masking is exact-
            # string and best-effort, and Actions logs are world-readable on a
            # public repo. Anyone with this URL can post to the channel.
            log.warning("webhook attempt %d: network error (%s)",
                        attempt, _redact(exc, webhook_url))
        else:
            if response.status_code in (200, 204):
                return True
            if response.status_code == 429:
                # Discord tells us exactly how long to wait.
                try:
                    wait = float(response.json().get("retry_after", delay))
                except (ValueError, KeyError, AttributeError):
                    wait = delay
                log.warning("webhook rate-limited; sleeping %.1fs", wait)
                time.sleep(min(wait, 60.0))
                continue
            if 400 <= response.status_code < 500:
                # Malformed payload — retrying identical content won't help.
                log.error("webhook rejected (%d): %s", response.status_code, response.text[:400])
                return False
            log.warning("webhook attempt %d: HTTP %d", attempt, response.status_code)

        time.sleep(delay)
        delay *= 2
    return False
