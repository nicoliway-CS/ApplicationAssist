"""Title, location, internship, and role-category filtering.

A posting passes if:
    (0) its title is not excluded by exclude_title_patterns, AND
    (1) it is US-located, AND
    (2) its title looks like an internship/co-op (require_internship_title_patterns), AND
    (3) its title matches at least one role category in config/role_categories.yaml
        (software_engineering / machine_learning / embedded_systems), unless
        enable_other_category is on, in which case a no-match posting is kept
        and tagged "other" instead of dropped.

This replaces the old EU-vs-US-sponsorship branch entirely: there is no
sponsorship question for a US-citizen applicant, so US location is a plain
requirement rather than one arm of an OR, and there is no known-sponsor list
here. See config/notify_companies.yaml for the (unrelated) company-level
signal that now controls Discord noise instead.
"""

from __future__ import annotations

import logging
import re

from normalize import (
    compile_term_patterns, normalize_company, normalize_text, strip_company_suffix,
)

log = logging.getLogger(__name__)

# Trailing ", TX" style state codes, or a bare "US"/"USA"/"United States"
# mention -- the shape both sources use for US roles, including "Remote (US)".
_US_STATES = (
    "AL AK AZ AR CA CO CT DE FL GA HI ID IL IN IA KS KY LA ME MD MA MI MN MS "
    "MO MT NE NV NH NJ NM NY NC ND OH OK OR PA RI SC SD TN TX UT VT VA WA WV "
    "WI WY DC PR"
).split()
_US_STATE_RE = re.compile(r",\s*(" + "|".join(_US_STATES) + r")\b", re.IGNORECASE)
_US_COUNTRY_RE = re.compile(r"\b(united states|u\.?s\.?a?\.?)\b", re.IGNORECASE)


def is_us_location(location: str) -> bool:
    """US-only check. Deliberately just a location string test.

    Reused verbatim from the old EU-vs-US bot: many EU/Canadian city names
    are also US cities (Dublin CA/OH, Berlin NH, Paris TX, Vienna VA, Naples
    FL), so a plain "contains a US state or country marker" check still
    matters even with the EU branch gone -- it's what keeps a Toronto or
    Waterloo posting (vanshb03's board explicitly also carries Canada) from
    being miscounted just because its company or role also has a US office
    listed elsewhere in a multi-location string.
    """
    return bool(_US_STATE_RE.search(location) or _US_COUNTRY_RE.search(location))


def compile_title_exclusions(patterns) -> re.Pattern | None:
    return compile_term_patterns(patterns)


class CompanyMatcher:
    """Resolve a posting's company field to a notify-list company, or None.

    Used for config/notify_companies.yaml (which companies page Discord). Only
    the company field is ever looked at, never the title.

    An entry matches when the company name STARTS with it as whole words, so
    "Amazon" covers "Amazon Web Services" but "Arm" can't cover "Armadin" and
    "Square" can't cover "Squarespace". The old rule compared space-stripped
    strings, which had no word boundary: live data (2026-11) showed "HP" hitting
    "HPR (Hyannis Port Research)", "Arm" hitting "Armadin", "Meta" hitting
    "Metaphor". Start-anchored on purpose rather than anywhere-in-the-name: a
    contains match let "Applied Materials" hit "Johns Hopkins Applied Physics
    Laboratory", and would let "Sierra" hit "Sierra Nevada Corporation".

    Names listed under `exact:` must equal the whole company field instead --
    for names short or common enough that even a whole-word prefix misfires
    ("TI", "Ring", "Lambda"). Equality is checked on the space-stripped form, so
    "D.E. Shaw" / "DE Shaw" and "U.S. Bank" / "US Bank" still meet.

    `aliases:` map other names to a parent ("Raytheon" -> RTX). A hit on an
    alias reports the parent, so Discord shows one name per company and
    main.py can collapse the same job posted under two names.
    """

    def __init__(self, config: dict | None, key: str = "notify"):
        config = config or {}
        groups = config.get(key) or {}
        # Accept either a flat list or the grouped dict the config ships
        # with, so deleting a group never changes the file's shape.
        if isinstance(groups, dict):
            names = [n for group in groups.values() for n in (group or [])]
        else:
            names = list(groups)

        pairs = [(str(n), str(n)) for n in names if n]  # (name to match, parent to report)
        for parent, alias_list in (config.get("aliases") or {}).items():
            pairs += [(str(a), str(parent)) for a in (alias_list or []) if a]
        exact = {normalize_company(n) for n in (config.get("exact") or []) if n}

        self._entries: list[tuple[tuple[str, ...], str, str, bool]] = []
        for name, parent in pairs:
            tokens = self._tokens(name)
            if tokens:
                squashed = "".join(tokens)
                self._entries.append((tokens, squashed, parent, squashed in exact))
        # Most words first, then longest, so "Citadel Securities" beats "Citadel"
        # and "Hewlett Packard Enterprise" (-> HPE) beats any shorter "Hewlett" entry.
        self._entries.sort(key=lambda e: (len(e[0]), len(e[1])), reverse=True)
        self.names = {parent for _, _, parent, _ in self._entries}

    @staticmethod
    def _tokens(name: str) -> tuple[str, ...]:
        """Lowercased words, legal suffix and a leading "the" dropped.

        Sources are inconsistent about the article -- "The Home Depot" appears
        with it, most names without -- so it's stripped on both sides.
        """
        cleaned = strip_company_suffix(name)
        words = re.sub(r"[^a-z0-9]+", " ", cleaned).split()
        if len(words) > 1 and words[0] == "the":
            words = words[1:]
        return tuple(words)

    def __bool__(self) -> bool:
        return bool(self._entries)

    def resolve(self, company: str) -> str | None:
        """The parent company name to report, or None if not on the list."""
        tokens = self._tokens(company)
        if not tokens:
            return None
        squashed = "".join(tokens)
        for entry_tokens, entry_squashed, parent, exact_only in self._entries:
            if squashed == entry_squashed:
                return parent
            if not exact_only and tokens[: len(entry_tokens)] == entry_tokens:
                return parent
        return None

    def matches(self, company: str) -> bool:
        return self.resolve(company) is not None


class RoleCategoryTagger:
    """Positive-keyword screen assigning zero or more role categories.

    Mirrors the require_title_patterns screen the old eu-tech-jobs source
    used (see config/role_categories.yaml for the measurements behind that
    choice): a title must look like the target role, since sources' own
    category tags can't be trusted. A title can earn more than one category.
    """

    def __init__(self, config: dict | None):
        config = config or {}
        options = config.get("options") or {}
        fpga_category = str(options.get("fpga_category") or "embedded").lower()
        self.enable_other = bool(options.get("enable_other_category", False))
        fpga_patterns = config.get("fpga_patterns") or []

        categories = config.get("categories") or {}
        self.labels = {key: (spec or {}).get("label", key) for key, spec in categories.items()}
        self._compiled: list[tuple[str, re.Pattern]] = []
        for key, spec in categories.items():
            patterns = list((spec or {}).get("patterns") or [])
            if key == "embedded_systems" and fpga_category in ("embedded", "both"):
                patterns += fpga_patterns
            if key == "hardware_engineering" and fpga_category in ("hardware", "both"):
                patterns += fpga_patterns
            compiled = compile_term_patterns(patterns)
            if compiled:
                self._compiled.append((key, compiled))

    def categorize(self, title: str) -> list[str]:
        normalized = normalize_text(title)
        hits = [key for key, pattern in self._compiled if pattern.search(normalized)]
        if not hits and self.enable_other:
            return ["other"]
        return hits


def filter_postings(
    postings: list[dict],
    settings: dict,
    role_categories_config: dict | None = None,
) -> list[dict]:
    tagger = RoleCategoryTagger(role_categories_config)
    require_active = settings.get("require_active", True)
    title_exclusions = compile_title_exclusions(settings.get("exclude_title_patterns"))
    require_internship = compile_term_patterns(settings.get("require_internship_title_patterns"))

    kept, stats = [], {
        "inactive": 0, "title": 0, "not_us": 0, "not_internship": 0, "no_category": 0,
    }
    category_counts: dict[str, int] = {}

    for posting in postings:
        if require_active and not posting.get("active", True):
            stats["inactive"] += 1
            continue

        title = normalize_text(posting.get("title", ""))
        if title_exclusions and title_exclusions.search(title):
            stats["title"] += 1
            continue

        if require_internship and not require_internship.search(title):
            stats["not_internship"] += 1
            continue

        # Multi-location postings: any one US location is enough.
        parts = [p.strip() for p in str(posting.get("location", "")).split("|") if p.strip()]
        if not parts:
            parts = [str(posting.get("location", ""))]
        if not any(is_us_location(part) for part in parts):
            stats["not_us"] += 1
            continue

        categories = tagger.categorize(posting.get("title", ""))
        if not categories:
            stats["no_category"] += 1
            continue

        posting["role_categories"] = categories
        for key in categories:
            category_counts[key] = category_counts.get(key, 0) + 1
        kept.append(posting)

    log.info(
        "filter: kept %d (%s), dropped %d (inactive=%d, title=%d, not_us=%d, "
        "not_internship=%d, no_category=%d)",
        len(kept),
        ", ".join(f"{k}={v}" for k, v in sorted(category_counts.items())) or "none",
        stats["inactive"] + stats["title"] + stats["not_us"] + stats["not_internship"] + stats["no_category"],
        stats["inactive"], stats["title"], stats["not_us"],
        stats["not_internship"], stats["no_category"],
    )
    return kept
