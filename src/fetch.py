"""Per-source adapters. Each returns a list of normalized posting dicts.

Normalized shape:
    id, company, title, location, url, sponsorship_flag, source_repo,
    date_posted (MMDDYYYY), active, terms (["Summer 2027", ...], see terms.py)

Adapters never raise past `fetch_source`: a source that changes shape is
logged and skipped so one broken repo can't take down the run.
"""

from __future__ import annotations

import json
import logging
import re
from datetime import datetime, timedelta, timezone

import requests

from normalize import make_id, normalize_text, to_mmddyyyy
from terms import normalize_source_terms, resolve_terms

log = logging.getLogger(__name__)

TIMEOUT = 30
HEADERS = {"User-Agent": "job-alert-bot (+github actions)"}

# Simplify's sponsorship enum. Not read anywhere in the filter/notify
# pipeline -- there is no sponsorship question for a US-citizen applicant --
# but kept on the normalized posting since it costs nothing and a future
# feature might want it.
_SPONSORSHIP_MAP = {
    "offers sponsorship": "yes",
    "does not offer sponsorship": "no",
    "u.s. citizenship is required": "no",
    "other": "unknown",
}

_CLOSED_MARKERS = ("\U0001F512", "🔒")  # padlock = closed application
_SPONSOR_MARKER = "\U0001F6C2"  # 🛂 in the role cell
_HREF = re.compile(r'href=["\'](https?://[^"\']+)["\']', re.IGNORECASE)
_MD_LINK = re.compile(r"\[([^\]]*)\]\((https?://[^)]+)\)")
_HTML_TAG = re.compile(r"<[^>]+>")
_BR = re.compile(r"</?br\s*/?>", re.IGNORECASE)
# speedyapply publishes an "Age" column ("34d") instead of a posting date.
_AGE = re.compile(r"^(\d+)\s*([hdwm])$", re.IGNORECASE)
_AGE_UNIT_DAYS = {"h": 0, "d": 1, "w": 7, "m": 30}
_EMOJI = re.compile(r"[\U0001F000-\U0001FAFF☀-➿️]")


def fetch_source(source: dict) -> list[dict]:
    """Download and parse one configured source. Returns [] on any failure."""
    name = source.get("name", "<unnamed>")
    adapter = source.get("adapter")

    try:
        if adapter in _TEXT_ADAPTERS:
            response = requests.get(source.get("url"), timeout=TIMEOUT, headers=HEADERS)
            response.raise_for_status()
            postings = _TEXT_ADAPTERS[adapter](response.text, name)
        else:
            log.warning("source %s: unknown adapter %r -- skipping", name, adapter)
            return []
    except requests.RequestException as exc:
        log.warning("source %s: fetch failed (%s) -- skipping", name, exc)
        return []
    except Exception as exc:  # noqa: BLE001 - one bad source must not kill the run
        log.warning("source %s: parse failed (%s) -- skipping", name, exc)
        return []

    # Adapters only report what the source itself tagged; the title and the
    # source's configured default_terms are folded in here, once, for every adapter.
    default_terms = source.get("default_terms")
    for posting in postings:
        posting["terms"] = resolve_terms(posting["title"], posting.get("terms"), default_terms)

    if not postings:
        log.warning("source %s: parsed 0 postings (format may have changed)", name)
    else:
        log.info("source %s: parsed %d postings", name, len(postings))
    return postings


def _parse_simplify_json(text: str, source_name: str) -> list[dict]:
    """Simplify-style listings.json: a flat list of posting objects.

    Real fields (verified against the live Summer2027-Internships file):
    company_name, title, locations (a LIST, not a string), url, sponsorship
    (4-value enum), date_posted (unix epoch int), active, is_visible, terms (a
    list like ["Winter 2027", "Spring 2027"], or ["N/A"]). Also
    ships a `category` field (Software/Hardware/AI-ML-Data/Product/Quant)
    that is NOT used here -- see config/role_categories.yaml for why.
    """
    data = json.loads(text)
    if not isinstance(data, list):
        raise ValueError("expected a JSON list")

    postings = []
    for row in data:
        if not isinstance(row, dict):
            continue
        company = row.get("company_name") or ""
        title = row.get("title") or ""
        if not company or not title:
            continue
        if row.get("is_visible") is False:
            continue

        locations = row.get("locations") or []
        if isinstance(locations, str):
            locations = [locations]
        location = " | ".join(str(loc) for loc in locations) or "Unspecified"

        date_posted = to_mmddyyyy(row.get("date_posted"))
        sponsorship = _SPONSORSHIP_MAP.get(
            normalize_text(row.get("sponsorship")), "unknown"
        )

        postings.append(
            {
                "id": make_id(company, title, date_posted),
                "company": str(company).strip(),
                "title": str(title).strip(),
                "location": location,
                "url": row.get("url") or row.get("company_url") or "",
                "sponsorship_flag": sponsorship,
                "source_repo": source_name,
                "date_posted": date_posted,
                "active": bool(row.get("active", True)),
                "terms": normalize_source_terms(row.get("terms")),
            }
        )
    return postings


def _parse_markdown_table(text: str, source_name: str) -> list[dict]:
    """README table: | Company | Role | Location | Application/Link | Date |

    Column names vary by repo and are matched loosely (see _map_columns):
    speedyapply says "Position" / "Posting" / "Age" instead. A README may hold
    several tables (speedyapply splits FAANG+ / Quant / Other); each header row
    re-learns the layout.

    Verified quirks in the live file: company wrapped in **bold**; multiple
    locations joined by </br>; the link cell is an <a href> around an image,
    or a bare 🔒 when the posting is closed; the date is 'Aug 05' with no
    year. The 🛂 marker in the role cell means different things in different
    eras of this table (older repos: sponsorship offered; the current
    vanshb03/Summer2027-Internships legend: sponsorship NOT offered) -- it is
    captured into sponsorship_flag for completeness but not read by the
    filter/notify pipeline, so the flip doesn't affect behavior here.
    """
    rows = [line.strip() for line in text.splitlines() if line.strip().startswith("|")]
    if not rows:
        raise ValueError("no table rows found")

    columns = None
    postings = []
    now = datetime.now(timezone.utc)

    for line in rows:
        cells = [c.strip() for c in line.strip().strip("|").split("|")]

        # Header row: learn the column order instead of assuming it.
        lowered = [normalize_text(c) for c in cells]
        if "company" in lowered and any(
            "role" in c or "position" in c or "title" in c for c in lowered
        ):
            columns = _map_columns(lowered)
            continue
        if columns is None:
            continue
        if all(set(c) <= set("-: ") for c in cells):  # separator row
            continue
        if len(cells) <= max(columns.values()):
            continue

        company = _clean(cells[columns["company"]])
        title_raw = cells[columns["role"]]
        title = _clean(title_raw)
        if not company or not title:
            continue

        link_cell = cells[columns["link"]] if "link" in columns else ""
        # A padlock (and no link) means the application is closed.
        if any(marker in link_cell for marker in _CLOSED_MARKERS) and not _HREF.search(link_cell):
            continue

        url = ""
        href = _HREF.search(link_cell) or _HREF.search(title_raw)
        if href:
            url = href.group(1)
        else:
            md = _MD_LINK.search(link_cell) or _MD_LINK.search(title_raw)
            if md:
                url = md.group(2)

        location = _clean(_BR.sub(" | ", cells[columns["location"]])) if "location" in columns else ""
        if "date" in columns:
            date_posted = to_mmddyyyy(_clean(cells[columns["date"]]), now=now)
        elif "age" in columns:
            date_posted = _age_to_mmddyyyy(_clean(cells[columns["age"]]), now)
        else:
            date_posted = ""

        sponsorship = "yes" if _SPONSOR_MARKER in title_raw else "unknown"

        postings.append(
            {
                "id": make_id(company, title, date_posted),
                "company": company,
                "title": title,
                "location": location or "Unspecified",
                "url": url,
                "sponsorship_flag": sponsorship,
                "source_repo": source_name,
                "date_posted": date_posted,
                "active": True,
                "terms": [],  # no term column; title / default_terms fill it in
            }
        )
    return postings


def _age_to_mmddyyyy(age: str, now: datetime) -> str:
    """'34d' -> the date 34 days before `now`. "" if unparseable.

    The age is relative to when the README was last regenerated, not to now,
    so the derived date can drift by a day between rebuilds -- and the date is
    part of the dedupe key. That is why main.py also dedupes on the apply URL
    (normalize.canonical_url): a drifted id still matches its stored URL.
    """
    match = _AGE.match(age.strip())
    if not match:
        return ""
    days = int(match.group(1)) * _AGE_UNIT_DAYS[match.group(2).lower()]
    return (now - timedelta(days=days)).strftime("%m%d%Y")


def _map_columns(lowered_header: list[str]) -> dict[str, int]:
    """Locate columns by name so a reordered/renamed table still parses."""
    columns: dict[str, int] = {}
    for index, name in enumerate(lowered_header):
        if "company" in name and "company" not in columns:
            columns["company"] = index
        elif ("role" in name or "position" in name or "title" in name) and "role" not in columns:
            columns["role"] = index
        elif "location" in name and "location" not in columns:
            columns["location"] = index
        elif (
            "link" in name or "application" in name or name in ("apply", "posting")
        ) and "link" not in columns:
            columns["link"] = index
        elif ("date" in name or name == "added") and "date" not in columns:
            columns["date"] = index
        elif name == "age" and "age" not in columns:
            columns["age"] = index
    if "company" not in columns or "role" not in columns:
        raise ValueError(f"could not locate company/role columns in {lowered_header}")
    return columns


def _clean(cell: str) -> str:
    """Strip markdown bold, HTML tags, emoji, and the ↳ continuation arrow."""
    text = _BR.sub(" | ", cell)
    text = _HTML_TAG.sub("", text)
    text = text.replace("**", "").replace("↳", " ")
    text = _MD_LINK.sub(r"\1", text)
    text = _EMOJI.sub("", text)
    return re.sub(r"\s+", " ", text).strip(" |")


_TEXT_ADAPTERS = {
    "simplify_json": _parse_simplify_json,
    "markdown_table": _parse_markdown_table,
}
