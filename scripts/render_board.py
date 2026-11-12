"""Render the live job board: one self-contained HTML page for GitHub Pages.

Runs the same fetch -> filter pipeline the bot uses, then writes every
current US internship/co-op match into a static page grouped by posting
date, with client-side filters for role category, company, and notify-list
status. Rebuilt once a day; the page is a snapshot of what is open *now*,
not an append-only log.

Two deliberate differences from what Discord receives:

  * Every match is included, regardless of company. Discord only fires for
    config/notify_companies.yaml companies -- see README -- but the board is
    meant to be a browsable archive of everything that passed the location/
    internship/category filters, not just the curated slice.
  * Nothing is filtered on `seen.json`. The page includes postings already
    notified about, because it is meant to be a browsable archive rather than
    a queue. `seen.json` is still read, but only to mark rows NEW.

READ-ONLY. Never writes state, never posts to Discord.

    python scripts/render_board.py -o site/index.html
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import yaml

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from fetch import fetch_source  # noqa: E402
from filter import CompanyMatcher, filter_postings  # noqa: E402
from normalize import dedupe_postings  # noqa: E402
from terms import UNLISTED_LABEL, label_sort_key, season_labels  # noqa: E402

log = logging.getLogger("board")

TEMPLATE = Path(__file__).resolve().parent / "board_template.html"

# Every date the page prints about *itself* is rendered here, not in UTC. The
# workflow runs on a UTC cron, and a build that lands late in the UTC evening is
# still the previous day locally -- the page used to say "rebuilt August 26" on
# the evening of the 25th. The cron is scheduled mid-morning ET so both clocks
# agree on the calendar day even if GitHub delays the run by an hour, but the
# page states the timezone regardless rather than relying on that.
#
# Posting dates are deliberately NOT converted: those are the dates the sources
# published, and the day headings should say what the source said.
BOARD_TZ = ZoneInfo("America/New_York")

# Order matters: it is both the category-card order and the precedence used to
# color a multi-category posting's row (its FIRST matching category wins the
# left-border tint; all its categories still get a badge). (key, heading,
# hint, chip label) -- the heading/hint are display copy, kept here rather
# than in role_categories.yaml since that file's `label` is reused as-is for
# the badge text.
CATEGORY_HINTS = {
    "software_engineering": "General SWE, backend, frontend, full-stack",
    "machine_learning": "ML/AI engineering, applied & research science",
    "embedded_systems": "Firmware, RTOS, microcontrollers, IoT",
    "hardware_engineering": "PCB, ASIC/VLSI, digital design",
    "other": "Matched no category -- audit for keyword gaps",
}


def build_categories(role_categories_config: dict) -> list[tuple[str, str, str, str]]:
    labels = {
        key: (spec or {}).get("label", key)
        for key, spec in (role_categories_config.get("categories") or {}).items()
    }
    labels.setdefault("other", "Other")
    order = ["software_engineering", "machine_learning", "embedded_systems",
             "hardware_engineering", "other"]
    return [
        (key, labels[key], CATEGORY_HINTS.get(key, ""), labels[key])
        for key in order
        if key in labels
    ]


def primary_category(posting: dict, order: list[str]) -> str:
    categories = posting.get("role_categories") or []
    for key in order:
        if key in categories:
            return key
    return categories[0] if categories else "other"


def iso_date(posting: dict) -> str:
    """MMDDYYYY -> YYYY-MM-DD. The template groups and sorts on this.

    "" for a posting the source published no date for; the template renders
    that group as "Undated" and build_rows sorts it to the bottom.
    """
    raw = str(posting.get("date_posted", ""))
    try:
        return datetime.strptime(raw, "%m%d%Y").strftime("%Y-%m-%d")
    except ValueError:
        return ""


def esc(text: str) -> str:
    return (str(text).replace("&", "&amp;").replace("<", "&lt;")
            .replace(">", "&gt;").replace('"', "&quot;"))


def build_rows(
    postings: list[dict],
    seen: dict,
    new_since: datetime | None,
    notify_matcher: CompanyMatcher,
    category_order: list[str],
) -> list[dict]:
    """Compact row dicts. Short keys because this ships inline in the page."""
    rows = []
    for posting in postings:
        date = iso_date(posting)
        # NEW = first seen by the bot within the window, i.e. it showed up in
        # Discord since the last build. Non-notify-list postings are never in
        # seen.json at all (see main.py), so they're never marked NEW here --
        # that's expected, not a bug: the board's NEW badge tracks what got
        # pinged, not what's freshly scraped.
        is_new = False
        if new_since is not None:
            meta = seen.get(posting["id"]) if isinstance(seen, dict) else None
            if meta:
                try:
                    first = datetime.fromisoformat(str(meta.get("first_seen", "")))
                    if first.tzinfo is None:
                        first = first.replace(tzinfo=timezone.utc)
                    is_new = first >= new_since
                except ValueError:
                    pass
        categories = posting.get("role_categories") or ["other"]
        row = {
            "i": posting["id"],
            "c": str(posting.get("company", "")).strip(),
            "t": str(posting.get("title", "")).strip(),
            "l": str(posting.get("location", "")).strip() or "Unspecified",
            "u": posting.get("url") or "",
            "d": date,
            "rc": categories,
            "g": primary_category(posting, category_order),
            "s": str(posting.get("source_repo", "")).split("/")[-1],
            # Season buckets ("Winter/Spring 2027"...); [] = term not listed.
            "se": season_labels(posting.get("terms") or []),
        }
        if is_new:
            row["n"] = 1
        if notify_matcher.matches(posting.get("company", "")):
            row["p"] = 1
        rows.append(row)

    # Newest first, undated last. The template relies on this order to emit
    # day headings in a single pass.
    rows.sort(key=lambda r: (r["d"] or "0000-00-00"), reverse=True)
    return rows


def render(rows: list[dict], categories: list[tuple[str, str, str, str]], generated: datetime, title: str) -> str:
    local = generated.astimezone(BOARD_TZ)
    counts = {key: sum(1 for r in rows if r["g"] == key) for key, *_ in categories}
    notified_count = sum(1 for r in rows if r.get("p"))

    cards_html, chips_html = [], []
    for key, label, hint, chip in categories:
        if not counts[key]:
            continue  # an empty category is noise, not information
        cards_html.append(
            f'<div class="tier t-{key}"><div class="n">{counts[key]}</div>'
            f'<div class="k">{esc(label)}</div><div class="h">{esc(hint)}</div></div>'
        )
        chips_html.append(
            f'<button class="chip t-{key}" data-g="{key}" aria-pressed="true">'
            f'<span class="dot"></span>{esc(chip)}'
            f'<span class="ct">{counts[key]}</span></button>'
        )

    # Season dropdown: every bucket present, chronological, with counts. A row in two
    # buckets counts toward both, matching how the filter shows it under either.
    season_counts: dict[str, int] = {}
    unlisted = 0
    for r in rows:
        for label in r["se"]:
            season_counts[label] = season_counts.get(label, 0) + 1
        if not r["se"]:
            unlisted += 1
    season_counts = dict(sorted(season_counts.items(), key=lambda kv: label_sort_key(kv[0])))
    season_options = [f'<option value="">All seasons ({len(rows)})</option>']
    season_options += [f'<option value="{esc(label)}">{esc(label)} ({n})</option>'
                       for label, n in season_counts.items()]
    if unlisted:
        season_options.append(f'<option value="-">{esc(UNLISTED_LABEL)} ({unlisted})</option>')

    companies = sorted({r["c"] for r in rows if r["c"]})
    companies_html = "\n      ".join(f'<option value="{esc(c)}">' for c in companies)

    newest = next((r["d"] for r in rows if r["d"]), "")
    undated = sum(1 for r in rows if not r["d"])
    # %-I is glibc-only and %I zero-pads, so build the 12-hour clock by hand --
    # this script has to render identically on a dev Windows box and on the runner.
    clock = f"{local.hour % 12 or 12}:{local:%M %p %Z}"
    standfirst = (
        f'<strong>{len(rows)}</strong> US internship/co-op postings currently open in '
        "Software Engineering, Machine Learning, Embedded Systems, or Hardware Engineering. "
        f'<strong>{notified_count}</strong> are from a company on your notify list '
        "and already pinged Discord. "
        + (f'<strong>{undated}</strong> carry no date from the source and are grouped '
           "under <em>Undated</em> at the foot of the page. " if undated else "")
        + f'Rebuilt once a day; last run {local:%B %d, %Y} at {clock}.'
    )

    html = TEMPLATE.read_text(encoding="utf-8")
    replacements = {
        "__TITLE__": esc(title),
        "__EYEBROW__": esc(
            f"Live board · rebuilt {local:%B %d, %Y}"
            + (f" · newest posting {newest}" if newest else "")
        ),
        "__STANDFIRST__": standfirst,
        "__TIERS__": "\n      ".join(cards_html),
        "__CHIPS__": "\n      " + "\n      ".join(chips_html),
        "__COMPANIES__": companies_html,
        "__SEASONS__": "".join(season_options),
        "__ACTIVE__": json.dumps({key: True for key, *_ in categories}),
        # Inline JSON inside a <script> block: the only sequence that can break
        # out is a literal "</script>", so neutralise the slash.
        "__DATA__": json.dumps(rows, ensure_ascii=False, separators=(",", ":"))
                        .replace("</", "<\\/"),
    }
    for placeholder, value in replacements.items():
        html = html.replace(placeholder, value)
    return html


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("-o", "--output", default="site/index.html")
    parser.add_argument("--title", default="Internship Board")
    parser.add_argument("--max-age-days", type=int, default=60,
                        help="drop postings whose date is older than this (0 = keep all)")
    parser.add_argument("--new-window-hours", type=int, default=24,
                        help="mark rows NEW if the bot first saw them this recently")
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args()

    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(levelname)s %(message)s", stream=sys.stdout)

    sources_config = yaml.safe_load((ROOT / "config" / "sources.yaml").read_text(encoding="utf-8"))
    role_categories_config = yaml.safe_load(
        (ROOT / "config" / "role_categories.yaml").read_text(encoding="utf-8")
    )
    notify_path = ROOT / "config" / "notify_companies.yaml"
    notify_config = (yaml.safe_load(notify_path.read_text(encoding="utf-8")) or {}) \
        if notify_path.exists() else {}
    notify_matcher = CompanyMatcher(notify_config)

    settings = sources_config.get("settings", {}) or {}
    categories = build_categories(role_categories_config)
    category_order = [key for key, *_ in categories]

    state_path = ROOT / "state" / "seen.json"
    seen: dict = {}
    if state_path.exists():
        try:
            seen = json.loads(state_path.read_text(encoding="utf-8")).get("postings", {})
        except (json.JSONDecodeError, OSError) as exc:
            log.warning("seen.json unreadable (%s) -- no NEW badges this build", exc)

    postings = []
    for source in sources_config.get("sources", []):
        postings.extend(fetch_source(source))
    if not postings:
        # Refuse to overwrite a good page with an empty one just because every
        # source happened to be down.
        log.error("no postings fetched -- leaving the existing page untouched")
        return 1

    matches = filter_postings(postings, settings, role_categories_config)

    # Same job from two repos collapses here (by id or apply URL), so it's one row.
    selected = dedupe_postings(matches)
    if args.max_age_days > 0:
        cutoff = (datetime.now(timezone.utc) - timedelta(days=args.max_age_days)).strftime("%Y-%m-%d")
        before = len(selected)
        # An undated posting is kept. The alternative -- treating "" as older
        # than any cutoff -- would silently delete every live posting the
        # source simply never dated, which is a real cost to pay over a
        # field the source never filled in.
        selected = [p for p in selected if not iso_date(p) or iso_date(p) >= cutoff]
        log.info("age filter (%dd): kept %d of %d", args.max_age_days, len(selected), before)

    generated = datetime.now(timezone.utc)
    new_since = generated - timedelta(hours=args.new_window_hours) \
        if args.new_window_hours > 0 else None
    rows = build_rows(selected, seen, new_since, notify_matcher, category_order)

    out = Path(args.output)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(render(rows, categories, generated, args.title), encoding="utf-8")

    tiers = {key: sum(1 for r in rows if r["g"] == key) for key, *_ in categories}
    log.info("wrote %s -- %d postings (%s), %d notified, %d undated, %d marked NEW, %.0fKB",
             out, len(rows), ", ".join(f"{k}={v}" for k, v in tiers.items()),
             sum(1 for r in rows if r.get("p")),
             sum(1 for r in rows if not r["d"]),
             sum(1 for r in rows if r.get("n")), out.stat().st_size / 1024)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
