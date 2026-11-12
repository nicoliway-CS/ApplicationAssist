"""Sanity checks for the dedupe key, US-location detection, and the
role-category positive screen.

Run: python tests/test_core.py
"""

import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import yaml

from filter import (
    CompanyMatcher,
    RoleCategoryTagger,
    compile_title_exclusions,
    filter_postings,
    is_us_location,
)
from normalize import compile_term_patterns, make_id, normalize_text, to_mmddyyyy

ROOT = Path(__file__).resolve().parent.parent
failures = []


def check(label, actual, expected):
    ok = actual == expected
    print(f"{'PASS' if ok else 'FAIL'}  {label}")
    if not ok:
        print(f"        expected {expected!r}, got {actual!r}")
        failures.append(label)


# --- dedupe key -------------------------------------------------------------
same_day_a = make_id("Acme, Inc.", "Software Engineer", "08052026")
same_day_b = make_id("ACME", "software engineer", "08052026")
check("company suffix + case collapse to one id", same_day_a, same_day_b)

repost = make_id("Acme", "Software Engineer", "11052026")
check("same role reposted months later is a NEW id", repost != same_day_a, True)

emoji = make_id("Acme", "Software Engineer \U0001F6C2", "08052026")
check("sponsorship emoji in title does not change id", emoji, same_day_a)

accented = make_id("Zalando SE", "Ingénieur Logiciel", "08052026")
plain = make_id("Zalando SE", "Ingenieur Logiciel", "08052026")
check("diacritics normalize", accented, plain)

# --- date coercion ----------------------------------------------------------
check("unix epoch -> MMDDYYYY", to_mmddyyyy(1767841111), "01082026")
check("ISO string -> MMDDYYYY", to_mmddyyyy("2026-08-05"), "08052026")
check("millisecond epoch", to_mmddyyyy(1767841111000), "01082026")

now = datetime(2026, 8, 13, tzinfo=timezone.utc)
check("yearless 'Aug 05' takes current year", to_mmddyyyy("Aug 05", now=now), "08052026")
check("yearless 'Dec 20' rolls back a year", to_mmddyyyy("Dec 20", now=now), "12202025")

check("yearless date one day ahead is still this year (clock skew)",
      to_mmddyyyy("Aug 14", now=now), "08142026")
check("yearless date two days ahead rolls back a year",
      to_mmddyyyy("Aug 15", now=now), "08152025")

check("missing date is empty, not today", to_mmddyyyy(None, now=now), "")
check("empty-string date is empty", to_mmddyyyy("", now=now), "")
check("garbage date is empty", to_mmddyyyy("TBD", now=now), "")
check("an undated posting still gets a stable id",
      make_id("Acme", "Software Engineer", ""),
      make_id("Acme", "Software Engineer", ""))

# --- compile_term_patterns whole-word/phrase matching -----------------------
p = compile_term_patterns(["lead", "sr", "c++", ".net", "system administrator"])
check("whole-word: 'Leader' does not match 'lead'", bool(p.search(normalize_text("Team Leader"))), False)
check("whole-word: 'Ambassador' does not match 'sr'", bool(p.search(normalize_text("Developer Ambassador"))), False)
check("bare 'lead' still matches", bool(p.search(normalize_text("Tech Lead"))), True)
check("stack names with trailing punctuation match ('c++')",
      bool(p.search(normalize_text("C++ Developer"))), True)
check("stack names with leading punctuation match ('.net')",
      bool(p.search(normalize_text("Consultant .NET"))), True)
check("multi-word phrases match as phrases",
      bool(p.search(normalize_text("System Administrator (w/m/d)"))), True)
check("compile_term_patterns(None) is None", compile_term_patterns(None), None)
check("compile_term_patterns([]) is None", compile_term_patterns([]), None)

# --- US location detection ---------------------------------------------------
check("Seattle, WA is US", is_us_location("Seattle, WA"), True)
check("Remote (US) is US", is_us_location("Remote (US)"), True)
check("United States is US", is_us_location("United States"), True)

check("London, UK is NOT US", is_us_location("London, UK"), False)
check("Toronto, ON is NOT US", is_us_location("Toronto, ON"), False)
check("plain 'Remote' is not assumed US", is_us_location("Remote"), False)
check("empty location is not US", is_us_location(""), False)

# US precedence over identically-named EU/other cities -- still needed even
# with the EU branch gone, since these are real US tech-hub locations.
for us_city in ["Dublin, CA", "Dublin, OH", "Berlin, NH", "Paris, TX", "Vienna, VA", "Naples, FL"]:
    check(f"{us_city} detected as US", is_us_location(us_city), True)

# Multi-location postings: any one US location is enough (exercised via
# filter_postings below, since is_us_location itself takes one location).

# --- title exclusion (sources.yaml exclude_title_patterns) -------------------
sources = yaml.safe_load((ROOT / "config" / "sources.yaml").read_text(encoding="utf-8"))
settings = sources.get("settings", {})
excl = compile_title_exclusions(settings.get("exclude_title_patterns"))


def excluded(title):
    normalized = normalize_text(title)
    return bool(excl and excl.search(normalized))


for title in ["Sales Engineer", "Solutions Engineer", "Technical Account Manager",
              "Business Development Representative", "Field Service Technician",
              "Talent Acquisition Partner", "Payroll Coordinator"]:
    check(f"drops non-CS role {title!r}", excluded(title), True)

# Roles that must survive the exclude list even though the old EU-era config
# would have caught some of these -- they're now in scope (US citizen, no
# sponsorship question, hardware/aerospace of direct interest).
for title in ["Software Engineer Intern", "Embedded Software Engineer Intern",
              "Hardware Engineer Intern", "Avionics Hardware Engineer Intern",
              "Electrical Engineer Intern", "Site Reliability Engineer Intern",
              "Software Engineer, New Grad", "Backend Engineer (Internationalization)",
              "Software Engineer TS/SCI Poly"]:
    check(f"keeps {title!r}", excluded(title), False)

# --- role category tagging ---------------------------------------------------
role_categories_config = yaml.safe_load(
    (ROOT / "config" / "role_categories.yaml").read_text(encoding="utf-8")
)
tagger = RoleCategoryTagger(role_categories_config)

check("plain software title tags Software Engineering",
      tagger.categorize("Software Engineer Intern"), ["software_engineering"])
check("firmware title tags Embedded Systems",
      tagger.categorize("Firmware Engineer Intern"), ["embedded_systems"])
check("ASIC title tags Hardware Engineering",
      tagger.categorize("ASIC Design Engineer Intern"), ["hardware_engineering"])

# A title can earn more than one category.
multi = tagger.categorize("Embedded Software Engineer Intern")
check("'Embedded Software Engineer' earns both categories",
      sorted(multi), ["embedded_systems", "software_engineering"])

# A title matching none of the three is dropped (empty list) by default.
check("a non-CS title matches no category",
      tagger.categorize("Business Development Representative"), [])

# FPGA defaults to embedded per role_categories.yaml's fpga_category option.
check("FPGA title defaults to Embedded Systems",
      tagger.categorize("FPGA Engineer Intern"), ["embedded_systems"])

# Deliberate non-match: a role that looks like neither software, embedded,
# nor hardware engineering, to guard against an over-broad retune.
check("'Data Analyst Intern' matches no category",
      tagger.categorize("Data Analyst Intern"), [])

# Machine Learning category (added 2026-11). Titles below are real, from Simplify.
for title in ["Machine Learning Intern/Co-op", "AI/ML Engineering Co-op",
              "Research Scientist Intern - Language and Agents",
              "Applied Science Intern - Natural Language Processing and Speech Technologies",
              "Student Researcher - AI Foundation Model Infrastructure",
              "PhD Research Intern - Generative AI for Physical AI",
              "Deep Learning Compiler Intern", "AI Engineering Intern"]:
    check(f"ML: {title!r} tags Machine Learning",
          "machine_learning" in tagger.categorize(title), True)
check("'Machine Learning Engineer Intern' is ML only (moved out of SWE)",
      tagger.categorize("Machine Learning Engineer Intern"), ["machine_learning"])
check("'Machine Learning Software Engineer Intern' earns ML and SWE",
      sorted(tagger.categorize("Machine Learning Software Engineer Intern")),
      ["machine_learning", "software_engineering"])
# Bare "ai" is deliberately not a pattern -- these are real false positives it would cause.
for title in ["AI Solutions Intern - Human Resources", "Global Marketing AI & Data Intern",
              "Business Analytics AI Co-op", "Data Science Intern", "Email Marketing Intern"]:
    check(f"ML: {title!r} does NOT tag Machine Learning",
          "machine_learning" in tagger.categorize(title), False)

# enable_other_category: off by default (matches config/role_categories.yaml).
check("enable_other_category is off by default", tagger.enable_other, False)

other_config = {
    "options": {"enable_other_category": True},
    "categories": role_categories_config["categories"],
}
other_tagger = RoleCategoryTagger(other_config)
check("with enable_other_category on, a non-match is tagged 'other'",
      other_tagger.categorize("Business Development Representative"), ["other"])

# fpga_category: hardware / both, exercised directly against a minimal config
# so this test doesn't depend on role_categories.yaml's chosen default.
fpga_hw_config = {
    "options": {"fpga_category": "hardware"},
    "fpga_patterns": ["fpga"],
    "categories": {
        "embedded_systems": {"patterns": ["firmware"]},
        "hardware_engineering": {"patterns": ["asic"]},
    },
}
check("fpga_category: hardware tags FPGA as hardware only",
      RoleCategoryTagger(fpga_hw_config).categorize("FPGA Engineer"),
      ["hardware_engineering"])

fpga_both_config = {
    "options": {"fpga_category": "both"},
    "fpga_patterns": ["fpga"],
    "categories": {
        "embedded_systems": {"patterns": ["firmware"]},
        "hardware_engineering": {"patterns": ["asic"]},
    },
}
check("fpga_category: both tags FPGA as both",
      sorted(RoleCategoryTagger(fpga_both_config).categorize("FPGA Engineer")),
      ["embedded_systems", "hardware_engineering"])

# Whole-word guard on the category screen itself: "engineer" alone is
# deliberately NOT a Software Engineering pattern (see role_categories.yaml),
# so a Sales Engineer role must not slip in on the word "engineer".
check("'Sales Engineer' does not earn Software Engineering",
      "software_engineering" in tagger.categorize("Sales Engineer"), False)

# --- CompanyMatcher (config/notify_companies.yaml) ---------------------------
notify_config = yaml.safe_load((ROOT / "config" / "notify_companies.yaml").read_text(encoding="utf-8"))
notify = CompanyMatcher(notify_config, key="notify")

check("notify list is non-empty", bool(notify), True)

for name in ["Google", "Amazon", "NVIDIA", "SpaceX", "Blue Origin", "Anduril"]:
    check(f"{name} is on the notify list", notify.matches(name), True)

# Prefix matching: one entry should cover a company's subsidiaries.
check("prefix: Amazon Web Services", notify.matches("Amazon Web Services"), True)
check("prefix: Amazon Robotics", notify.matches("Amazon Robotics"), True)
check("legal suffix tolerated", notify.matches("NVIDIA, Inc."), True)

# The whole point of prefix-anchoring rather than substring matching --
# carried over from the old sponsor list's own guard case.
check("'Applied Materials' does NOT match Johns Hopkins APL",
      notify.matches("Johns Hopkins Applied Physics Laboratory"), False)

# Unlike the old h1b_sponsors.yaml, cleared-defense primes are IN this list on
# purpose -- there's no sponsorship-eligibility reason to exclude them for a
# US citizen, and they're exactly the aerospace/defense employers this list
# was seeded to surface.
for name in ["Northrop Grumman", "RTX", "Lockheed Martin"]:
    check(f"{name} IS on the notify list (unlike the old sponsor list)", notify.matches(name), True)

check("empty company is not a match", notify.matches(""), False)
check("missing notify config degrades safely", bool(CompanyMatcher(None)), False)

# --- filter_postings end-to-end ----------------------------------------------
internship_settings = dict(
    settings,
    require_internship_title_patterns=["intern", "internship", "co-op", "coop"],
)

sample = [
    {"id": "us-sw", "title": "Software Engineer Intern", "location": "Seattle, WA",
     "company": "Amazon", "active": True},
    {"id": "us-embedded", "title": "Firmware Engineer Co-op", "location": "Austin, TX",
     "company": "Texas Instruments", "active": True},
    {"id": "non-us", "title": "Software Engineer Intern", "location": "Berlin, Germany",
     "company": "Zalando", "active": True},
    {"id": "new-grad-leak", "title": "Software Engineer, New Grad", "location": "Seattle, WA",
     "company": "Amazon", "active": True},
    {"id": "no-category", "title": "Business Development Intern", "location": "New York, NY",
     "company": "Acme Corp", "active": True},
    {"id": "excluded-title", "title": "Sales Engineer Internship", "location": "Chicago, IL",
     "company": "Acme Corp", "active": True},
    {"id": "inactive", "title": "Software Engineer Intern", "location": "Seattle, WA",
     "company": "Amazon", "active": False},
]
result = {p["id"]: p for p in filter_postings(sample, internship_settings, role_categories_config)}

check("US internship in a real category is kept", "us-sw" in result, True)
check("kept posting is tagged with its role category",
      result["us-sw"]["role_categories"], ["software_engineering"])
check("US embedded co-op is kept", "us-embedded" in result, True)
check("non-US location is dropped", "non-us" in result, False)
check("new-grad title leaking into an internship-only run is dropped",
      "new-grad-leak" in result, False)
check("internship title matching no category is dropped", "no-category" in result, False)
check("excluded title (sales engineer) is dropped even with 'internship' in it",
      "excluded-title" in result, False)
check("inactive posting is dropped", "inactive" in result, False)

# A posting kept by filter_postings should carry every category it matches.
multi_sample = [{"id": "m", "title": "Embedded Software Engineer Intern",
                 "location": "San Jose, CA", "company": "Qualcomm", "active": True}]
multi_result = filter_postings(multi_sample, internship_settings, role_categories_config)
check("filter_postings preserves multi-category tagging",
      sorted(multi_result[0]["role_categories"]),
      ["embedded_systems", "software_engineering"])

# --- cross-source dedupe (canonical_url / dedupe_postings) --------------------
from normalize import canonical_url, dedupe_postings

check("tracking params stripped",
      canonical_url("https://www.Meta.com/jobs/123/?utm_source=Simplify&ref=Simplify"),
      "https://meta.com/jobs/123")
check("job-identifying query params KEPT (else distinct jobs would merge)",
      canonical_url("https://boards.greenhouse.io/acme?gh_jid=42&utm_medium=x"),
      "https://boards.greenhouse.io/acme?gh_jid=42")
check("different gh_jid -> different canonical URL",
      canonical_url("https://x.io/a?gh_jid=1") != canonical_url("https://x.io/a?gh_jid=2"), True)
check("non-http URL -> empty", canonical_url("mailto:jobs@acme.com"), "")

dupes = [
    {"id": "simplify-id", "url": "https://acme.com/jobs/9?utm_source=Simplify", "src": "simplify"},
    {"id": "speedy-id", "url": "https://acme.com/jobs/9", "src": "speedyapply"},   # same URL
    {"id": "simplify-id", "url": "", "src": "vansh"},                             # same id
    {"id": "other", "url": "https://acme.com/jobs/10", "src": "speedyapply"},
]
check("dedupe collapses by URL and by id, first source wins",
      [p["src"] for p in dedupe_postings(dupes)], ["simplify", "speedyapply"])
check("two URL-less postings with different ids both survive",
      len(dedupe_postings([{"id": "a", "url": ""}, {"id": "b", "url": ""}])), 2)

# --- markdown adapter: other repos' column names ------------------------------
from fetch import _parse_markdown_table

speedy_md = """
| Company | Position | Location | Salary | Posting | Age |
|---|---|---|---|---|---|
| <a href="https://meta.com"><strong>Meta</strong></a> | Research Scientist Intern | Menlo Park, CA | $50/hr | <a href="https://metacareers.com/jobs/1"><img src="x"></a> | 3d |
"""
rows = _parse_markdown_table(speedy_md, "speedyapply")
check("speedyapply table parses", len(rows), 1)
check("speedyapply company cleaned of HTML", rows[0]["company"], "Meta")
check("'Posting' column read as the apply link", rows[0]["url"], "https://metacareers.com/jobs/1")
expected_age_date = (datetime.now(timezone.utc) - __import__("datetime").timedelta(days=3)).strftime("%m%d%Y")
check("'Age' 3d becomes a date 3 days back", rows[0]["date_posted"], expected_age_date)

sndsh_md = """
| Company | Role | Location | Apply | Added |
| --- | --- | --- | --- | --- |
| Google | Software Engineering Intern | Mountain View, CA | [apply](https://google.com/j/1) | 2026-07-20 |
"""
rows = _parse_markdown_table(sndsh_md, "sndsh404")
check("'Apply' markdown link + 'Added' ISO date", (rows[0]["url"], rows[0]["date_posted"]),
      ("https://google.com/j/1", "07202026"))

# --- internship terms / seasons (src/terms.py) --------------------------------
from terms import (
    normalize_source_terms,
    parse_title_terms,
    resolve_terms,
    season_label,
    season_labels,
)
from notify_discord import _batches, build_embed, order_for_notify

check("title 'Intern - Summer 2027'", parse_title_terms("SWE Intern - Summer 2027"), ["Summer 2027"])
check("title 'Winter/Spring 2027' yields both",
      parse_title_terms("Co-op - Winter/Spring 2027"), ["Winter 2027", "Spring 2027"])
check("title year-first '2027 Summer Intern'",
      parse_title_terms("2027 Summer Software Intern"), ["Summer 2027"])
check("title range 'Fall 2026 - Spring 2027'",
      parse_title_terms("Co-op (Fall 2026 - Spring 2027)"), ["Fall 2026", "Spring 2027"])
check("title 2-digit year \"Summer '27\"", parse_title_terms("Intern Summer '27"), ["Summer 2027"])
check("autumn is fall", parse_title_terms("Intern - Autumn 2027"), ["Fall 2027"])
check("yearless season is not guessed", parse_title_terms("Summer Software Intern"), [])
check("duration is not a year", parse_title_terms("Summer 12 weeks Intern"), [])
check("'Summer Analyst' is not a term", parse_title_terms("Technology Summer Analyst Intern"), [])

check("source terms: N/A dropped", normalize_source_terms(["N/A"]), [])
check("source terms canonicalized",
      normalize_source_terms(["summer 2027", "Winter 2027"]), ["Summer 2027", "Winter 2027"])

# Title beats a contradicting source tag (real case seen in Simplify, 2026-11).
check("title overrides a wrong source tag",
      resolve_terms("Firmware Engineer Intern - Fall 2026", ["Summer 2026"]), ["Fall 2026"])
check("richer source tag kept when title is a subset",
      resolve_terms("SWE Intern - Summer 2027", ["Summer 2027", "Fall 2027"]),
      ["Summer 2027", "Fall 2027"])
check("source tag used when title is silent",
      resolve_terms("SWE Intern", ["Spring 2027", "Winter 2027"]), ["Winter 2027", "Spring 2027"])
check("default_terms used when title and source are silent",
      resolve_terms("SWE Intern", [], ["Summer 2027"]), ["Summer 2027"])
check("nothing anywhere -> no terms", resolve_terms("SWE Intern", [], None), [])

# Buckets: Winter + Spring share one; Summer and Fall separate.
check("winter and spring collapse to one bucket",
      season_labels(["Winter 2027", "Spring 2027"]), ["Winter/Spring 2027"])
check("summer 27", season_labels(["Summer 2027"]), ["Summer 2027"])
check("fall 27", season_labels(["Fall 2027"]), ["Fall 2027"])
check("multi-season label is chronological",
      season_label(["Fall 2027", "Spring 2027"]), "Winter/Spring 2027 + Fall 2027")
check("same season, different year are distinct",
      season_labels(["Winter 2026", "Winter 2027"]), ["Winter/Spring 2026", "Winter/Spring 2027"])
check("unlisted label", season_label([]), "Term not listed")

# Notifications: grouped by season, never mixed in one message, order preserved.
def _p(pid, terms, date):
    return {"id": pid, "company": "Acme", "title": "SWE Intern", "location": "Austin, TX",
            "terms": terms, "date_posted": date, "role_categories": ["software_engineering"]}

batch_in = [_p("f", ["Fall 2027"], "11102026"), _p("s1", ["Summer 2027"], "11092026"),
            _p("u", [], "11112026"), _p("w", ["Spring 2027"], "11082026"),
            _p("s2", ["Summer 2027"], "11112026")]
ordered = order_for_notify(batch_in)
check("order_for_notify: earliest season first, unlisted last, newest first within",
      [p["id"] for p in ordered], ["w", "s2", "s1", "f", "u"])
batches = _batches(ordered)
check("one batch per season group", len(batches), 4)
check("batch header names the season", batches[1][0].splitlines()[0], "## ☀️ Summer 2027")
check("batches preserve order (main.py relies on sent being a prefix)",
      [p["id"] for _, b in batches for p in b], [p["id"] for p in ordered])
big = _batches([_p(str(i), ["Summer 2027"], "11112026") for i in range(13)])
check("a >10 season group splits into parts", [len(b) for _, b in big], [10, 3])
check("part numbering in header", big[1][0].endswith("(part 2/2)"), True)
check("embed author carries the season",
      build_embed(_p("x", ["Winter 2027"], "11112026"))["author"]["name"],
      "❄️ Winter/Spring 2027")

print()
if failures:
    print(f"{len(failures)} FAILED")
    raise SystemExit(1)
print("all checks passed")
