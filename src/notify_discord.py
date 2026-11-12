"""Discord webhook delivery: batched embeds with rate-limit-aware retries."""

from __future__ import annotations

import logging
import re
import time

import requests

from terms import season_label, season_sort_key

log = logging.getLogger(__name__)

EMBEDS_PER_MESSAGE = 10  # Discord's hard cap
MAX_RETRIES = 4
TIMEOUT = 20

CATEGORY_LABELS = {
    "software_engineering": "Software Engineering",
    "machine_learning": "Machine Learning",
    "embedded_systems": "Embedded Systems",
    "hardware_engineering": "Hardware Engineering",
    "other": "Other",
}
CATEGORY_COLORS = {
    "software_engineering": 0x2ECC71,
    "machine_learning": 0xE91E63,
    "embedded_systems": 0x3498DB,
    "hardware_engineering": 0xE67E22,
    "other": 0x95A5A6,
}
DEFAULT_COLOR = 0x95A5A6


def order_for_notify(postings: list[dict]) -> list[dict]:
    """Season groups together (earliest season first, unlisted last), newest first within.

    main.py marks `to_send[:sent]` as seen, so it must call this BEFORE notify() and
    pass the reordered list -- notify() preserves order, it never re-sorts.
    """
    by_date = sorted(postings, key=lambda p: p.get("date_posted", ""), reverse=True)
    return sorted(by_date, key=lambda p: season_sort_key(p.get("terms") or []))


def _batches(postings: list[dict]) -> list[tuple[str, list[dict]]]:
    """Split into (season header, <=10 postings) batches that never mix seasons.

    One season per message is what makes the message-level header honest: a batch
    that straddled Summer and Fall couldn't carry a single "Summer 2027" banner.
    Costs at most a few extra webhook posts per run, against a 60-posting cap.
    """
    groups: list[tuple[str, list[dict]]] = []
    for posting in postings:
        label = season_label(posting.get("terms") or [], emoji=True)
        if groups and groups[-1][0] == label:
            groups[-1][1].append(posting)
        else:
            groups.append((label, [posting]))

    batches = []
    for label, group in groups:
        chunks = [group[i : i + EMBEDS_PER_MESSAGE]
                  for i in range(0, len(group), EMBEDS_PER_MESSAGE)]
        for part, chunk in enumerate(chunks, start=1):
            noun = "posting" if len(group) == 1 else "postings"
            header = f"## {label}\n{len(group)} new {noun}"
            if len(chunks) > 1:
                header += f" (part {part}/{len(chunks)})"
            batches.append((header, chunk))
    return batches


def build_embed(posting: dict) -> dict:
    company = posting.get("company", "Unknown")
    title = posting.get("title", "Unknown role")
    location = posting.get("location", "Unspecified")
    categories = posting.get("role_categories") or []
    cat_labels = " / ".join(CATEGORY_LABELS.get(c, c) for c in categories) or "Uncategorized"
    season = season_label(posting.get("terms") or [], emoji=True)

    bits = [
        f"\U0001F4CD {location}",
        f"\U0001F3F7️ {cat_labels}",
        f"_{posting.get('source_repo', '')}_",
    ]

    embed = {
        # Author renders as a bold line ABOVE the title -- the most prominent slot an
        # embed has short of the title itself, so the season is the first thing read
        # even when an embed is seen on its own (e.g. in a mobile push preview).
        "author": {"name": season[:256]},
        "title": f"{company} — {title}"[:256],
        "description": " · ".join(bits)[:4096],
        "color": CATEGORY_COLORS.get(categories[0] if categories else "other", DEFAULT_COLOR),
        "footer": {"text": f"Posted {_pretty_date(posting.get('date_posted', ''))}"},
    }
    # Discord rejects the whole payload if `url` is present but not a valid URL.
    url = posting.get("url") or ""
    if url.startswith("http"):
        embed["url"] = url
    return embed


def _pretty_date(mmddyyyy: str) -> str:
    if len(mmddyyyy) == 8 and mmddyyyy.isdigit():
        return f"{mmddyyyy[:2]}/{mmddyyyy[2:4]}/{mmddyyyy[4:]}"
    return "unknown date"


def notify(webhook_url: str, postings: list[dict], *, dry_run: bool = False) -> int:
    """Send postings as season-grouped batched embeds, in the order given.

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

    for index, (header, batch) in enumerate(batches, start=1):
        payload = {"content": header, "embeds": [build_embed(p) for p in batch]}
        if dry_run:
            log.info("[dry-run] batch %d/%d: %d embeds -- %s",
                     index, len(batches), len(batch), header.replace("\n", " · "))
            for posting in batch:
                log.info("[dry-run]   %s — %s (%s) [%s]",
                         posting.get("company"), posting.get("title"), posting.get("location"),
                         season_label(posting.get("terms") or []))
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
