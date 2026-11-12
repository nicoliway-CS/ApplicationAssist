"""Pipeline entrypoint: fetch -> filter -> dedupe -> notify -> persist state.

First run seeds state silently (no Discord messages) so you don't get
thousands of historical postings dumped into the channel. Every run after
that notifies only on genuinely new matches.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).parent))

from fetch import fetch_source  # noqa: E402
from filter import CompanyMatcher, filter_postings  # noqa: E402
from normalize import canonical_url, dedupe_postings  # noqa: E402
from notify_discord import notify, order_for_notify  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
SOURCES_PATH = ROOT / "config" / "sources.yaml"
ROLE_CATEGORIES_PATH = ROOT / "config" / "role_categories.yaml"
NOTIFY_PATH = ROOT / "config" / "notify_companies.yaml"
STATE_PATH = ROOT / "state" / "seen.json"

log = logging.getLogger("job-alert-bot")

# last_checked is a liveness marker, but writing it changes seen.json, and every
# change is a commit. At a 5-minute cadence an unconditional write is ~290
# commits a day of pure noise, so it's refreshed at most this often unless the
# run changed state anyway.
LAST_CHECKED_EVERY = timedelta(hours=1)


def load_state() -> dict:
    """State shape: {"version", "bootstrapped", "postings": {id: {first_seen}}}.

    `first_seen` is stored per id (not a bare id list) because pruning needs a
    timestamp, and because the board uses it to decide which rows are NEW.
    It is NOT a stand-in posting date: a source that publishes no date leaves
    `date_posted` empty and the posting renders as "Undated". See to_mmddyyyy.
    """
    if not STATE_PATH.exists():
        return {"version": 1, "bootstrapped": False, "postings": {}}
    try:
        state = json.loads(STATE_PATH.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        log.error("state file unreadable (%s) -- refusing to run rather than re-notify everything", exc)
        raise SystemExit(1)

    # Tolerate a hand-written legacy list of bare ids.
    if isinstance(state, list):
        now = datetime.now(timezone.utc).isoformat()
        return {
            "version": 1,
            "bootstrapped": True,
            "postings": {pid: {"first_seen": now} for pid in state},
        }
    state.setdefault("postings", {})
    state.setdefault("bootstrapped", bool(state["postings"]))
    return state


def save_state(state: dict) -> None:
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    STATE_PATH.write_text(
        json.dumps(state, indent=2, sort_keys=True, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )


def prune(state: dict, days: int) -> int:
    if not days or days <= 0:
        return 0
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    stale = []
    for pid, meta in state["postings"].items():
        try:
            first_seen = datetime.fromisoformat(str(meta.get("first_seen", "")))
            if first_seen.tzinfo is None:
                first_seen = first_seen.replace(tzinfo=timezone.utc)
        except ValueError:
            continue  # unparseable timestamp: keep it, safer than re-notifying
        if first_seen < cutoff:
            stale.append(pid)
    for pid in stale:
        del state["postings"][pid]
    return len(stale)


def main() -> int:
    parser = argparse.ArgumentParser(description="Job alert bot")
    parser.add_argument("--dry-run", action="store_true",
                        help="log what would be sent; never POST, never write state")
    parser.add_argument("--reseed", action="store_true",
                        help="force a silent re-seed of state (no notifications)")
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(levelname)s %(message)s",
        stream=sys.stdout,
    )

    sources_config = yaml.safe_load(SOURCES_PATH.read_text(encoding="utf-8"))
    role_categories_config = yaml.safe_load(ROLE_CATEGORIES_PATH.read_text(encoding="utf-8"))
    settings = sources_config.get("settings", {}) or {}

    # Optional: a missing notify list degrades to "notify on nothing" rather
    # than killing the run -- the board still renders everything either way.
    if NOTIFY_PATH.exists():
        notify_config = yaml.safe_load(NOTIFY_PATH.read_text(encoding="utf-8")) or {}
    else:
        log.warning("%s not found -- Discord will stay silent this run", NOTIFY_PATH.name)
        notify_config = {}
    notify_matcher = CompanyMatcher(notify_config)

    state = load_state()
    bootstrapping = args.reseed or not state.get("bootstrapped", False)

    # --- fetch ---
    all_postings = []
    for source in sources_config.get("sources", []):
        all_postings.extend(fetch_source(source))

    if not all_postings:
        log.error("no postings from any source -- aborting without touching state")
        return 1
    log.info("fetched %d postings from %d sources",
             len(all_postings), len(sources_config.get("sources", [])))

    # --- filter ---
    # Everything here (US, internship, categorized) is what the board shows.
    # Discord is narrower: only postings from a config/notify_companies.yaml
    # company are even candidates for a notification. state/seen.json only
    # ever needs to track that narrower pool -- the board re-derives its full
    # picture from the live sources on every render rather than from state.
    matches = filter_postings(all_postings, settings, role_categories_config)
    notify_candidates = [p for p in matches if notify_matcher.matches(p.get("company", ""))]
    log.info("%d matches, %d from notify-list companies", len(matches), len(notify_candidates))

    # --- dedupe ---
    # Same job twice (two repos, or a drifted speedyapply date) collapses by id OR
    # canonical apply URL before the seen check, so a copy can't sneak past it
    # under a new id. Stored URLs catch the cross-run version of the same thing.
    seen_ids = set(state["postings"])
    seen_urls = {meta.get("url") for meta in state["postings"].values()
                 if isinstance(meta, dict) and meta.get("url")}
    new_postings = sorted(
        (p for p in dedupe_postings(notify_candidates)
         if p["id"] not in seen_ids and canonical_url(p.get("url")) not in seen_urls),
        key=lambda p: _date_key(p), reverse=True,
    )
    log.info("%d notify-list matches, %d new after dedupe", len(notify_candidates), len(new_postings))

    now = datetime.now(timezone.utc)
    now_iso = now.isoformat()

    # --- bootstrap: record everything, notify about nothing ---
    if bootstrapping:
        log.info("BOOTSTRAP: seeding %d ids silently (no Discord messages sent)",
                 len(new_postings))
        if args.dry_run:
            log.info("[dry-run] state not written")
            return 0
        for posting in new_postings:
            _record(state, posting, now_iso)
        state["bootstrapped"] = True
        _finish(state, settings, now, changed=True)
        log.info("seeded. Future runs will notify only on new postings.")
        return 0

    # --- freshness: only recent postings are worth a ping ---
    # A posting can be unseen without being new: a source added to sources.yaml,
    # or a category added to role_categories.yaml, suddenly qualifies months of
    # backlog at once (adding machine_learning + speedyapply was ~600). Anything
    # older than this window is recorded silently instead -- it's still on the
    # board, it just isn't news. Undated postings are let through: no date is
    # not evidence of age.
    max_age = settings.get("notify_max_age_days", 3)
    fresh, stale = new_postings, []
    if max_age and max_age > 0:
        cutoff = (now - timedelta(days=max_age)).strftime("%Y%m%d")
        fresh = [p for p in new_postings if not _date_key(p) or _date_key(p) >= cutoff]
        stale = [p for p in new_postings if _date_key(p) and _date_key(p) < cutoff]
    if stale:
        log.info("%d unseen postings older than %dd -- recording silently", len(stale), max_age)

    if not fresh:
        log.info("nothing new this run")
        if args.dry_run:
            log.info("[dry-run] state not written")
            return 0
        for posting in stale:
            _record(state, posting, now_iso, silent=True)
        _finish(state, settings, now, changed=bool(stale))
        return 0

    cap = settings.get("max_notifications_per_run", 60)
    to_send = fresh
    overflow = []
    if cap and len(fresh) > cap:
        log.warning("%d new postings exceeds cap of %d -- sending %d, deferring the rest",
                    len(fresh), cap, cap)
        to_send, overflow = fresh[:cap], fresh[cap:]

    # The cap above picks the newest postings; only then regroup by season, so a
    # big Summer batch can't crowd fresher Fall postings past the cap.
    to_send = order_for_notify(to_send)

    webhook_url = os.environ.get("DISCORD_WEBHOOK_URL", "")
    sent = notify(webhook_url, to_send, dry_run=args.dry_run)
    log.info("notified on %d/%d postings", sent, len(to_send))

    if args.dry_run:
        log.info("[dry-run] state not written")
        return 0

    # Only mark what actually went out. A failed batch stays unseen and is
    # retried next run rather than being silently lost.
    for posting in to_send[:sent]:
        _record(state, posting, now_iso)
    for posting in stale:
        _record(state, posting, now_iso, silent=True)
    if overflow:
        log.info("%d postings deferred to the next run", len(overflow))

    _finish(state, settings, now, changed=bool(sent or stale))
    log.info("state now tracks %d ids", len(state["postings"]))
    return 0


def _date_key(posting: dict) -> str:
    """MMDDYYYY -> YYYYMMDD so dates sort and compare as strings. "" if undated."""
    raw = str(posting.get("date_posted", ""))
    return raw[4:] + raw[:4] if len(raw) == 8 and raw.isdigit() else ""


def _record(state: dict, posting: dict, now_iso: str, *, silent: bool = False) -> None:
    entry = {
        "first_seen": now_iso,
        "company": posting["company"],
        "title": posting["title"],
    }
    url = canonical_url(posting.get("url"))
    if url:
        entry["url"] = url  # lets a later copy under a different id match (see dedupe)
    if silent:
        entry["silent"] = True  # recorded as backlog, never pinged
    state["postings"][posting["id"]] = entry


def _finish(state: dict, settings: dict, now: datetime, *, changed: bool) -> None:
    """Prune, refresh last_checked if due, and write state only if anything moved."""
    removed = prune(state, settings.get("prune_after_days", 90))
    if removed:
        log.info("pruned %d stale ids", removed)
    try:
        last = datetime.fromisoformat(str(state.get("last_checked", "")))
        due = now - last >= LAST_CHECKED_EVERY
    except (ValueError, TypeError):  # missing, garbled, or naive timestamp
        due = True
    if changed or removed or due:
        state["last_checked"] = now.isoformat()
        save_state(state)


if __name__ == "__main__":
    raise SystemExit(main())
