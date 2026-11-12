# Internship Alert Bot

Watches public internship-listing repos, filters them down to **US internships/co-ops** in **Software Engineering, Machine Learning, Embedded Systems, or Hardware Engineering**, and posts matches from a curated company list to a Discord channel. It also publishes a browsable board of everything currently open to GitHub Pages, filterable by category, company, and notify status.

Runs entirely on GitHub Actions cron — no server, no database, no hosting bill. State is a single JSON file committed back to the repo.

Built for a US-citizen CS undergrad not graduating until 2028, where sponsorship and country-of-residence aren't the question — the two that matter are *"is this in Software Engineering, Machine Learning, Embedded Systems, or Hardware Engineering?"* and *"is it a company I actually want a ping for?"* This started as a fork of a bot built for the opposite problem (EU-location-or-visa-sponsorship filtering for an international candidate); most of what follows is about how the filtering logic changed shape, not just what it excludes.

## Setup

1. Fork or clone the repo.
2. Create a Discord webhook: *Channel → Edit Channel → Integrations → Webhooks → New Webhook → Copy URL*.
3. Add it as a repo secret named `DISCORD_WEBHOOK_URL` (*Settings → Secrets and variables → Actions*).
4. Set *Settings → Actions → General → Workflow permissions* to **Read and write** so the bot can commit `state/seen.json`.
5. Trigger a manual run from the Actions tab.

The first run seeds silently and sends nothing — see [First run](#first-run).

Local run:

```bash
pip install -r requirements.txt
python src/main.py --dry-run        # log matches, send nothing, write nothing
python tests/test_core.py           # sanity checks
python scripts/render_board.py -o site/index.html    # build the board locally
```

Everything tunable lives in [config/](config/) — sources, role-category keyword lists, and the notify-list companies. Adding a source, a keyword, or a company needs no code change.

## How it decides

A posting is kept if it is **US-located** AND **looks like an internship/co-op** AND **its title matches at least one role category** (Software Engineering, Machine Learning, Embedded Systems, or Hardware Engineering).

**US-only.** US detection runs the same city-name-collision guard the original EU-vs-US bot needed: Dublin CA/OH, Berlin NH, Paris TX, Vienna VA and Naples FL are all real US tech locations, and both source boards carry Canada/EU postings too (vanshb03's board explicitly says it covers "the United States, Canada, or Remote"). Multi-location postings qualify if *any one* location is US.

**Internship-only.** Enforced two ways: primarily by only watching internship-scoped source repos (see [Sources](#sources)), with a title-based fallback (`require_internship_title_patterns` in [config/sources.yaml](config/sources.yaml)) that drops anything lacking internship/co-op wording — this is what catches a new-grad req that leaks into an internship board.

**Role category.** [config/role_categories.yaml](config/role_categories.yaml) is a positive keyword screen — the title must look like the target role, run per-category so a posting can earn more than one tag ("Embedded Software Engineer Intern" is genuinely both Software Engineering and Embedded Systems). A title matching none of the four is dropped, same as the old bot's non-CS handling, unless `enable_other_category` is turned on to audit for keyword gaps. Bare single words are avoided in the category lists on purpose — "engineer" alone would tag every Sales Engineer and Field Applications Engineer posting, so most patterns are qualified phrases ("software engineer", "asic design") instead. [config/sources.yaml](config/sources.yaml)'s `exclude_title_patterns` is now short: with the category screen doing the "is this CS/CE" work, there's much less non-CS noise left to name explicitly, and what's there is either a sales/recruiting/legal title or a specific trap (`sales engineer`, `solutions engineer` — pre-sales technical roles that would otherwise ride the word "engineer").

One deliberate reversal from the original bot: clearance/citizenship terms (`TS/SCI`, `polygraph`, `ITAR`, `US citizen required`...) are **not** excluded here. Those existed to protect an international candidate who could never clear them. A US citizen isn't blocked by a posting merely announcing a clearance requirement, and cleared defense/aerospace roles are exactly what [config/notify_companies.yaml](config/notify_companies.yaml) exists to surface.

Simplify's own `category` field (Software/Hardware/AI-ML-Data/Product/Quant) was checked before writing the keyword screen and rejected as a filter: sampled 2026-09, its "Hardware" bucket contained "Software Development Intern" and "Embedded Software Engineer Intern" alongside real hardware roles — the same kind of unreliable tag the old EU source's `role_family` field was.

## Seasons

Every posting is tagged with the internship term(s) it's for, and both Discord and the board show it. Terms are grouped into three buckets per year:

| Bucket | Covers |
|---|---|
| ❄️ Winter/Spring 2027 | Winter 2027 and Spring 2027 terms (off-cycle co-ops) |
| ☀️ Summer 2027 | Summer 2027 |
| 🍂 Fall 2027 | Fall 2027 |

Other years get the same treatment (Fall 2026, Summer 2028...). A posting can be in more than one bucket, e.g. a Spring + Summer co-op shows as `Winter/Spring 2027 + Summer 2027`. A posting nobody tagged shows as **Term not listed**. The bot never guesses one.

[src/terms.py](src/terms.py) picks the term in this order:

1. **The title**, when it names a season and a year ("SWE Intern - Fall 2026", "Winter/Spring 2027", "2027 Summer Intern"). This is what the employer wrote. Simplify's own tag sometimes contradicts it (sampled 2026-11: "Firmware Engineer Intern - Fall 2026" was tagged Summer 2026), and the title wins. If Simplify's tag is a superset of what the title says (title "Summer 2027", tag Summer + Fall 2027), the fuller tag is kept.
2. **The source's tag**: Simplify's `terms` field. `N/A` is treated as no tag.
3. **The source's `default_terms`** in [config/sources.yaml](config/sources.yaml). vanshb03's main README has no term column but only lists Summer 2027 (its off-season roles are in a separate file), so it's set to `[Summer 2027]`.

**In Discord**, each run's notifications are grouped by season, earliest first. Each message carries one season only, with a header like `## ☀️ Summer 2027 · 3 new postings`, and each embed repeats the season on its top line. Seasons are grouped *after* the per-run cap has picked the newest postings, so a big Summer batch can't push fresher Fall postings past the cap.

**On the board**, every row has a season badge, and an "All seasons" dropdown filters to a single bucket (or to *Term not listed*).

Seasons are display-only: they don't change the dedupe key or which postings are kept.

## Sources

| Source | Adapter | Format |
|---|---|---|
| [SimplifyJobs/Summer2027-Internships](https://github.com/SimplifyJobs/Summer2027-Internships) | `simplify_json` | listings.json |
| [vanshb03/Summer2027-Internships](https://github.com/vanshb03/Summer2027-Internships) | `markdown_table` | README pipe table |
| [vanshb03 off-season list](https://github.com/vanshb03/Summer2027-Internships/blob/main/OFFSEASON_README.md) | `markdown_table` | OFFSEASON_README pipe table |
| [speedyapply/2027-SWE-College-Jobs](https://github.com/speedyapply/2027-SWE-College-Jobs) | `markdown_table` | README pipe tables (FAANG+ / Quant / Other) |
| [speedyapply/2027-AI-College-Jobs](https://github.com/speedyapply/2027-AI-College-Jobs) | `markdown_table` | README pipe tables (FAANG+ / Quant / Other) |

The last three were added 2026-11 after measuring each candidate against the first two: about 35 (vanshb03 off-season), 320 (speedyapply SWE) and 100 (speedyapply AI) US internships passing the filters weren't in Simplify or vanshb03's main list. The markdown adapter matches column names loosely ("Position", "Posting", "Age"...), so a table with renamed columns still parses. speedyapply has no posting date, only an age ("34d"); it's converted to a date. Checked and **not** added: jobright-ai's lists (~40 new, but links go through jobright.ai pages rather than the employer), sndsh404/summer-2027-internships (~30), and Simplify's `README-Off-Season.md` (generated from the same `listings.json`, so it adds nothing).

Source order in `sources.yaml` matters. When two sources carry the same job, the first one listed is kept (see [Dedupe key](#dedupe-key)), so Simplify stays first for its term tags.

A source that 404s or changes shape is logged and skipped; the others still run.

**Machine Learning** was added 2026-11. Before it, ~1,600 active US internship titles that read as ML/AI ("Machine Learning Intern", "Research Scientist Intern", "AI/ML Engineering Co-op") matched no category and were silently dropped. Its patterns avoid a bare "ai" ("AI Solutions Intern - Human Resources" is a real title) and leave data science/analytics out; ML/AI-engineer titles moved out of Software Engineering into it. Simplify's own "AI/ML/Data" tag still isn't used, for the same reason as the rest of its `category` field.

**Simplify's `README-Off-Season.md` is not a separate source.** It's generated from the same `listings.json` the bot already reads (checked 2026-11: all 647 off-season rows were in it), so adding it would only duplicate Fall/Winter/Spring postings the bot already sees.

### Quirks worth knowing

- **Despite the "2027" name, both repos are the actively-maintained boards**, not a single-season list. Simplify's currently carries Summer 2026 (mostly closed now), Fall 2026, and Winter/Spring/Summer 2027 postings together — `require_active` is what keeps the closed Summer 2026 backlog out, the same mechanism the old bot used for the same reason.
- **~81% of Simplify rows are inactive** (closed postings), filtered out via `require_active`.
- **vanshb03's 🛂 marker flipped meaning between repo eras.** In the old New-Grad-2026 table it meant "sponsors internationals"; in the current Summer2027-Internships table it means "does NOT offer sponsorship," per that repo's own legend. Harmless here — `sponsorship_flag` isn't read anywhere in this pipeline — but a future feature built on that field needs to re-check the legend, not assume it.
- **Markdown-table dates have no year** (`Aug 05`). The year is inferred as the most recent one that isn't in the future, allowing one day of clock skew, for the same reason the original bot needed this: the README is a rolling list, so a date that reads as "the near future" is actually last year's posting.

## The daily board

Discord is a queue: good for "what's new in the last hour", useless for browsing. [scripts/render_board.py](scripts/render_board.py) renders every currently-open match into one self-contained HTML page, and [.github/workflows/board.yml](.github/workflows/board.yml) rebuilds it once a day and publishes it to GitHub Pages.

Search, category filter chips, a company filter, a notified/not distinction, and a reviewed-checkbox with a progress meter — all client-side, all composable (searching within a filtered category doesn't reset the category, filtering by company doesn't reset the notified toggle, etc.).

**The board shows everything; Discord shows less.** Every posting that passes the location/internship/category filters lands on the board regardless of company. Discord only fires for a company in [config/notify_companies.yaml](config/notify_companies.yaml) — that's the noise control, not the board's. A row from a notify-list company is badged **📣 Notified**, and the "Notified only" chip filters to just that slice; everything else on the board still passed every real filter, it just isn't a company you asked to be pinged for.

| Category | Meaning |
|---|---|
| Software Engineering | General SWE, backend, frontend, full-stack |
| Machine Learning | ML/AI engineering, applied & research scientist, CV/NLP/LLM |
| Embedded Systems | Firmware, RTOS, microcontrollers, IoT |
| Hardware Engineering | PCB, ASIC/VLSI, digital design |
| Other | Only appears if `enable_other_category` is on — matched no category, kept for a keyword-gap audit |

Postings older than 60 days are dropped (`--max-age-days`).

### Setting up Pages

Pages on a **private** repo requires a paid plan; on a public repo it's free, and public repos also get unlimited Actions minutes.

1. *Settings → Pages → Build and deployment → Source:* **GitHub Actions**.
2. Run the `job-board` workflow once from the Actions tab.
3. The URL appears in the `deploy` job summary — `https://<user>.github.io/<repo>/`.

The cron is `17 11 * * *` (11:17 UTC = 7:17 AM US Eastern during EDT, 6:17 AM during EST). GitHub cron has no timezone support, so it drifts by an hour when the US changes clocks — mid-morning ET is chosen so that hour of drift, plus the delay GitHub adds to scheduled runs on shared runners, can never push the build across a date boundary and stamp the page with tomorrow's date.

This workflow is the only thing that rebuilds the page. If the board looks like it updated twice in one day, check the Actions tab for a manual `workflow_dispatch` run.

## Notify list: why there's a curated company file

[config/notify_companies.yaml](config/notify_companies.yaml) replaces the old `h1b_sponsors.yaml`. There is no external data behind it and no eligibility question it's answering — it's just "companies worth a Discord ping," seeded with big tech plus the aerospace/defense/robotics names relevant to embedded and hardware work (Blue Origin, SpaceX, Honeywell, RTX, Northrop Grumman, Boston Dynamics...). Unlike the old sponsor list, cleared-defense primes are **included** here on purpose — the reason they were excluded before (an international candidate can't clear a clearance requirement) doesn't apply to a US citizen.

Matching is the same exact-or-prefix scheme the sponsor list used: normalized company names match exactly or as a prefix, so "Amazon" covers "Amazon Web Services" and "Amazon Robotics" without a separate entry, and prefix-anchoring (not substring) avoids the false positive that motivated it originally — a contains-match let "Applied Materials" hit "Johns Hopkins Applied Physics Laboratory".

This list is meant to be hand-edited. Add or remove companies freely; deleting a whole group changes nothing else, since the loader flattens whatever groups remain.

## Dedupe key

`sha1(normalized_company + normalized_title + MMDDYYYY_posting_date)`

- **Date included** so the same role reposted months later is treated as genuinely new.
- **URL and source id excluded** — the same job appears in multiple repos with different tracking URLs and different UUIDs. Keying on either notifies twice for one job.
- **Company suffixes, case, emoji and diacritics normalized**, so `Acme, Inc.` and `ACME` are one job.
- When a source publishes **no date**, the date slot is left empty (see `to_mmddyyyy`).

**Second key: the apply URL.** Added 2026-11 with the speedyapply sources. The id alone misses a duplicate whenever two repos word a title differently ("… - 2027 Start") or date it differently (speedyapply's age-derived dates can drift by a day between README rebuilds). So postings are also collapsed by a *canonical* apply URL (`normalize.canonical_url`), both within a run and against URLs stored in `seen.json`. Canonical means tracking parameters (`utm_*`, `ref`, `source`...) are removed while everything else is kept. That matters: some job sites identify the job in the query string (`?gh_jid=123`), and stripping the whole query would merge different jobs and silently lose pings. The id is unchanged; the URL is an extra check. One known gap: vanshb03 links via its own short URLs (`go.vansh.nyc`), so a vanshb03 copy of a Simplify job worded differently can still ping twice. That was true before this change too.

## First run

The first run **seeds state silently and sends nothing**. With well over ten thousand postings across the sources, notifying on the backlog would mean hundreds of Discord messages.

Every run afterward notifies only on new matches from a notify-list company. To re-seed from scratch, run the workflow with the `reseed` input checked, or `python src/main.py --reseed`.

`max_notifications_per_run` (default 60) caps a single run. If a source reformats and suddenly looks like thousands of new jobs, you get 60 and the rest defer — not a channel flood.

**Only recent postings ping** (`notify_max_age_days`, default 3). Being unseen isn't the same as being new: adding a source or a category suddenly makes months of backlog qualify. Adding Machine Learning plus the speedyapply sources did exactly that (~770 postings at once). An unseen posting dated before the window is recorded in `seen.json` silently (marked `"silent": true`) and shows up on the board, but not in Discord. Undated postings are let through. This means **adding a source or category no longer needs a re-seed**.

**If you're switching an existing seeded state over to this filtering scheme** (i.e. running this refactor against a `state/seen.json` seeded under the old EU/sponsorship rules), the same problem as a first run applies: postings that were never candidates before (US internships from notify-list companies) will look "new" all at once. Re-seed once so the existing pool counts as old news, and review it out-of-band:

```bash
python src/main.py --reseed          # record everything live, send nothing
python scripts/backlog_dump.py       # dump the pool to backlog.md instead
```

## Weekly heartbeat

This bot's healthy state is silence, which looks identical to a dead bot — exhausted Actions minutes, a source gone permanently 404, a revoked webhook. [heartbeat.yml](.github/workflows/heartbeat.yml) posts a status message every Monday 09:00 UTC so silence becomes informative:

- postings tracked, and how many were new in the last 7 days
- estimated Actions minutes used this month, with a progress bar
- last check-jobs result, and a red flag if several recent runs failed
- a warning if run durations approach 60s (crossing it doubles minute usage)

Preview without sending: `gh workflow run heartbeat.yml -f dry_run=true`.

**The minutes figure is an estimate.** GitHub's `/timing` endpoint reports `billable: 0ms` on the Free tier, so it's unusable. Instead the heartbeat counts runs this month, samples the real `run_duration_ms` of recent runs, applies GitHub's round-up-to-the-minute rule, and scales. Two caveats: minutes are billed **account-wide** across all private repos, and this counts only one; and the count includes Dependabot's runs, which are slower than the bot's own.

## Cost and cadence

**The check runs every 5 minutes** (`2-59/5 * * * *`), GitHub's minimum. It was hourly until 2026-11; the point of the bot is to hear about a posting before the first wave of applicants. GitHub delays scheduled runs under load (most at the top of the hour, hence the `:02` offset), so real gaps are 5–15 minutes. Speed is also limited by how fast the source repos add a posting, which nothing here can change.

That cadence is only free because **this repo is public**: public repos get unlimited Actions minutes. On a **private** repo every job bills as a whole minute rounded up, against the Free tier's 2,000 private-repo minutes/month:

| Cadence | Minutes/month | % of free tier |
| --- | --- | --- |
| **Every 5 min (current)** | **~8,640** | **432% — Actions stops in week one** |
| Every 30 min | ~1,460 | 73% |
| Hourly | ~730 | 37% |
| Every 2 hours | ~365 | 18% |

If the repo is ever made private, set the cron in `check_jobs.yml` back to hourly first.

`seen.json` is only rewritten (and so only committed) when the run changes something, or once an hour to refresh `last_checked`. An unconditional write at this cadence would add ~290 noise commits a day.

Exhausting the allowance doesn't charge you — the default spending limit is $0, so Actions simply stops until the next billing cycle. That's precisely the silent failure the heartbeat exists to catch.

## Privacy

Running this in public exposes what you're looking at. Worth deciding deliberately:

- **The secret is safe.** Workflows trigger only on `schedule` and `workflow_dispatch`, and fork PRs can't reach secrets.
- **`state/seen.json` is committed**, and it stores `company`, `title` and (since 2026-11) the canonical apply `url` alongside each hashed id. Dated commits then show which roles you tracked and when you started. The ids and URLs are load-bearing for dedupe; `company` and `title` can be stripped if that matters to you.
- **The Pages board is world-readable** at a guessable URL. It ships `<meta name="robots" content="noindex, nofollow">` so it stays out of search results, but that's obscurity, not access control.
- **`site/` and `backlog.md` are gitignored** so neither generated artifact lands in history.

## Failure behavior

- A source that 404s or changes shape is logged and skipped; other sources still run.
- If *every* source fails, the run aborts **without touching state**, so nothing is falsely marked seen.
- Discord 429s honor the `retry_after` the API returns; 4xx rejections don't retry.
- Only postings that actually sent are marked seen — a failed batch is retried next run rather than lost.
- An unreadable `state/seen.json` aborts the run rather than re-notifying everything.
- The board renderer refuses to overwrite a good page with an empty one when every source is down.

## Layout

```
.github/workflows/check_jobs.yml   5-minute cron + manual trigger
.github/workflows/heartbeat.yml    weekly liveness report
.github/workflows/board.yml        daily Pages build + deploy
config/sources.yaml                source repos, adapters, tunables
config/role_categories.yaml        role-category keyword screen (SWE/ML/embedded/hardware)
config/notify_companies.yaml       curated companies that page Discord
src/fetch.py                       per-source adapters
src/normalize.py                   text/date normalization, dedupe key + URL dedupe, term patterns
src/filter.py                      US-location + internship + role-category rules
src/terms.py                       season detection (title > source tag > default) + buckets
src/notify_discord.py              batched embeds, retry/backoff
src/main.py                        orchestration
src/heartbeat.py                   weekly status + minutes estimate
scripts/render_board.py            daily Pages board renderer
scripts/board_template.html        its CSS/JS shell (placeholders: __DATA__ etc)
scripts/backlog_dump.py            one-off catch-up dump to markdown
state/seen.json                    committed state
tests/test_core.py                 sanity checks
```

## Credits

This bot aggregates community-maintained internship-listing repos and adds filtering on top. All the actual listing work is theirs:

- [SimplifyJobs/Summer2027-Internships](https://github.com/SimplifyJobs/Summer2027-Internships)
- [vanshb03/Summer2027-Internships](https://github.com/vanshb03/Summer2027-Internships)
- [speedyapply/2027-SWE-College-Jobs](https://github.com/speedyapply/2027-SWE-College-Jobs) and [2027-AI-College-Jobs](https://github.com/speedyapply/2027-AI-College-Jobs)

Forked from a version of this bot built for EU-location-or-US-visa-sponsorship filtering; that lineage is why the US-location detection and dedupe-key design look the way they do.
