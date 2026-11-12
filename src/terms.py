"""Internship term (season) detection and the season buckets notifications group by.

A posting's terms are canonical "Season YYYY" strings ("Summer 2027", "Winter 2027").
They come from three places, in order of trust:

  1. The title, when it names a season AND a year ("... Intern - Fall 2026"). This is
     what the employer wrote. Simplify's own `terms` tag disagrees with the title often
     enough to matter -- sampled 2026-11: "Firmware Engineer Intern - Fall 2026" tagged
     Summer 2026, "... - Spring 2027" tagged Winter/Fall 2027, "Data Science Intern -
     Summer 2027" tagged Summer 2026.
  2. The source's own tag (Simplify's `terms` list), when the title says nothing.
  3. A per-source `default_terms` from config/sources.yaml, for a board whose scope
     *is* the season (vanshb03's main README is Summer 2027 only; off-season roles
     live in a separate file there).

Anything left over is "term not listed" -- never guessed. ~7% of active Simplify rows
carry `terms: ["N/A"]` and most are open-ended student-researcher reqs with no season.

Terms are bucketed for display: Winter + Spring of the same year share one bucket
("Winter/Spring 2027"), because that's how off-cycle co-op recruiting is actually run
and the user asked for exactly that split; Summer and Fall get their own.
"""

from __future__ import annotations

import re

_SEASONS = ("winter", "spring", "summer", "fall")
_SEASON_ALIASES = {"autumn": "fall"}

# Bucket index within a year: winter/spring -> 0, summer -> 1, fall -> 2.
_BUCKET_OF = {"winter": 0, "spring": 0, "summer": 1, "fall": 2}
_BUCKET_NAMES = ("Winter/Spring", "Summer", "Fall")
_BUCKET_EMOJI = ("❄️", "☀️", "\U0001F342")  # snowflake, sun, leaf
UNLISTED_LABEL = "Term not listed"
UNLISTED_EMOJI = "❔"  # grey question mark

_SEASON = r"(?:winter|spring|summer|fall|autumn)"
_SEP = r"\s*(?:/|&|,|\+|-|–|—|\band\b|\bor\b)\s*"
_SEQ = rf"{_SEASON}(?:{_SEP}{_SEASON})*"
# 4-digit year, or a 2-digit one only when it can't be a duration ("Summer 12 weeks").
_YEAR = r"(?:20(\d{2})|'?(\d{2})(?!\d|\s*-?\s*(?:weeks?|wks?|months?|mos?|days?|hours?|hrs?)\b))"
_SEASON_THEN_YEAR = re.compile(rf"(?<![a-z])({_SEQ})\s*[,(\-–]?\s*{_YEAR}(?!\d)", re.IGNORECASE)
_YEAR_THEN_SEASON = re.compile(rf"(?<!\d)20(\d{{2}})\s+({_SEQ})(?![a-z])", re.IGNORECASE)
_SEASON_WORD = re.compile(rf"(?<![a-z]){_SEASON}(?![a-z])", re.IGNORECASE)
_CANONICAL = re.compile(r"^\s*(winter|spring|summer|fall|autumn)\s+(20\d{2})\s*$", re.IGNORECASE)


def _canon(season: str, year: int) -> str:
    season = season.lower()
    season = _SEASON_ALIASES.get(season, season)
    return f"{season.title()} {year}"


def _seasons_in(seq: str) -> list[str]:
    return [m.group(0) for m in _SEASON_WORD.finditer(seq)]


def _year(two_digit: str) -> int | None:
    value = int(two_digit)
    # Plausible internship years only -- keeps "Summer 99" or a stray number out.
    return 2000 + value if 20 <= value <= 39 else None


def parse_title_terms(title: str) -> list[str]:
    """Terms a title states explicitly with a year. A yearless "Summer Intern" is [].

    Handles "Summer 2027", "Winter/Spring 2027", "Fall 2026 - Spring 2027",
    "2027 Summer Intern", and "Summer '27". Order of first appearance is kept.
    """
    text = str(title or "")
    found: list[str] = []
    masked = text
    for match in _SEASON_THEN_YEAR.finditer(text):
        year = _year(match.group(2) or match.group(3))
        if year is None:
            continue
        found += [_canon(s, year) for s in _seasons_in(match.group(1))]
        start, end = match.span()
        masked = masked[:start] + " " * (end - start) + masked[end:]
    for match in _YEAR_THEN_SEASON.finditer(masked):
        year = _year(match.group(1))
        if year is None:
            continue
        found += [_canon(s, year) for s in _seasons_in(match.group(2))]
    return list(dict.fromkeys(found))


def normalize_source_terms(raw) -> list[str]:
    """Canonicalize a source's term tag; drop "N/A" and anything unparseable."""
    if not raw:
        return []
    if isinstance(raw, str):
        raw = [raw]
    terms = []
    for item in raw:
        match = _CANONICAL.match(str(item))
        if match:
            terms.append(_canon(match.group(1), int(match.group(2))))
    return list(dict.fromkeys(terms))


def resolve_terms(title: str, source_terms: list[str] | None, default_terms=None) -> list[str]:
    """Title > source tag > configured default. See the module docstring for why.

    One refinement: when the source's tag is a superset of what the title says
    ("Summer 2027" title, tagged Summer + Fall 2027), the richer source tag wins --
    the title isn't contradicting it, just naming the headline term.
    """
    from_title = parse_title_terms(title)
    from_source = list(source_terms or [])
    if from_title:
        if from_source and set(from_title) <= set(from_source):
            return sort_terms(from_source)
        return sort_terms(from_title)
    if from_source:
        return sort_terms(from_source)
    return sort_terms(normalize_source_terms(default_terms))


def _term_key(term: str) -> tuple[int, int]:
    season, year = term.split()
    return int(year), _SEASONS.index(season.lower())


def sort_terms(terms: list[str]) -> list[str]:
    return sorted(dict.fromkeys(terms), key=_term_key)


def season_buckets(terms: list[str]) -> list[tuple[int, int]]:
    """(year, bucket index) pairs, chronological, one per distinct bucket."""
    buckets = set()
    for term in terms or []:
        season, year = term.split()
        buckets.add((int(year), _BUCKET_OF[season.lower()]))
    return sorted(buckets)


def bucket_label(bucket: tuple[int, int], *, emoji: bool = False) -> str:
    year, index = bucket
    label = f"{_BUCKET_NAMES[index]} {year}"
    return f"{_BUCKET_EMOJI[index]} {label}" if emoji else label


def label_sort_key(label: str) -> tuple[int, int]:
    """Chronological key for a plain bucket label ("Winter/Spring 2027")."""
    name, year = label.rsplit(" ", 1)
    return int(year), _BUCKET_NAMES.index(name)


def season_labels(terms: list[str]) -> list[str]:
    """Plain bucket labels, e.g. ["Winter/Spring 2027", "Summer 2027"]. [] if unlisted."""
    return [bucket_label(b) for b in season_buckets(terms)]


def season_label(terms: list[str], *, emoji: bool = False) -> str:
    """One display string for a posting: "Winter/Spring 2027 + Summer 2027"."""
    buckets = season_buckets(terms)
    if not buckets:
        return f"{UNLISTED_EMOJI} {UNLISTED_LABEL}" if emoji else UNLISTED_LABEL
    return " + ".join(bucket_label(b, emoji=emoji) for b in buckets)


def season_sort_key(terms: list[str]) -> tuple:
    """Groups identical season combos together, earliest first, unlisted last."""
    buckets = season_buckets(terms)
    return (0, tuple(buckets)) if buckets else (1, ())
