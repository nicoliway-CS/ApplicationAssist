"""Title, location, internship, and role-category filtering.

A posting passes if:
    (0) its title is not excluded by exclude_title_patterns, AND
    (1) it is US-located, AND
    (2) its title looks like an internship/co-op (require_internship_title_patterns), AND
    (3) its title matches at least one role category in config/role_categories.yaml
        (software_engineering / machine_learning / embedded_systems /
        hardware_engineering), unless
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

from normalize import compile_term_patterns, normalize_company, normalize_text

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
    """Exact-or-prefix membership test against a curated company list.

    Used for config/notify_companies.yaml (which companies page Discord).
    Matching is exact-or-prefix on the normalized company name, so one entry
    ("Amazon") covers "Amazon Web Services" and "Amazon Robotics". A contains
    match was tried first and rejected in the list this was carried over
    from: it let "Applied Materials" match "Johns Hopkins Applied Physics
    Laboratory", which is exactly the kind of false positive prefix-anchoring
    avoids.
    """

    def __init__(self, config: dict | None, key: str = "notify"):
        groups = (config or {}).get(key) or {}
        # Accept either a flat list or the grouped dict the config ships
        # with, so deleting a group never changes the file's shape.
        if isinstance(groups, dict):
            names = [n for group in groups.values() for n in (group or [])]
        else:
            names = list(groups)

        self.names = {self._key(n) for n in names if n}
        self.names.discard("")
        # Longest first so the most specific entry wins when reporting a hit.
        self._ordered = sorted(self.names, key=len, reverse=True)

    @staticmethod
    def _key(name: str) -> str:
        """Normalized form, with a leading article dropped.

        Sources are inconsistent about it -- "The Home Depot" appears with
        the article, most names without. Stripping it on both sides means
        the list doesn't need duplicate entries.
        """
        normalized = normalize_company(name)
        if normalized.startswith("the") and len(normalized) > 5:
            return normalized[3:]
        return normalized

    def __bool__(self) -> bool:
        return bool(self.names)

    def matches(self, company: str) -> bool:
        normalized = self._key(company)
        if not normalized:
            return False
        if normalized in self.names:
            return True
        return any(normalized.startswith(name) for name in self._ordered)


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
