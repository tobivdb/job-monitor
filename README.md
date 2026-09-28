# Job Monitor

Daily monitoring of configured PE/investment firm career pages, with an email
digest and an optional feed into the CV pipeline.

## Run

```bash
pip install -r requirements.txt
playwright install chromium
python job_monitor.py --config config.github.json --dry-run
python -m unittest discover -s tests -v
```

`--dry-run` writes only `last_report.html`, `scan_results.json` and a local log.
It never calls the fit API, sends mail, appends Google Sheet rows or changes `state.json`.
The workflow also skips the optional AI audit in dry-run mode.
The default config is local `config.json` if present, otherwise `config.github.json`.
SMTP and Google credentials belong in environment variables / GitHub Secrets.

## What counts as a job

* JobPosting JSON-LD or an individual vacancy link with a job title is a candidate.
* Team biographies, testimonials, general headings and search suggestions are not.
* LinkedIn requires a concrete `/jobs/view/<id>/` URL and matching employer
  evidence. Unauthenticated LinkedIn scans are skipped and reported.
* Before a candidate enters the digest or CV pipeline, its detail page is loaded:
  HTTP status, redirects, title, expiry/closure and substantive vacancy evidence
  are checked. A reachable overview or login page is not a verified job.
* Links resolve against the final page URL and HTML `<base>` tag. Canonical URLs
  remove session/tracking parameters while preserving actual job IDs.
* The same canonical URL is one job, even if a title or tracking parameter changes.
* PE relevance is a title heuristic, not a guarantee of investment-team fit.
  Company boilerplate does not upgrade a support role into a PE role.

## Coverage and uncertainty

Sites with no extractable links, inaccessible detail pages, unreadable PDFs or
unconfirmed employer/title evidence are listed for manual review. This does not
mean the company has no openings. Full warning details are in `scan_results.json`.
Workday gets a bounded extra rendering wait. Unigestion uses a configured
`job_button_selector` to open job buttons and capture their real destination URLs.
Other custom button-only boards need a tested adapter; no URLs are guessed.
Text PDFs are checked for the title and vacancy evidence (bounded to 10 MB and
20 pages); scans without extractable text require manual review.

Each source runs in a separate browser worker with a configurable wall-clock
deadline covering startup, navigation, detail verification and browser cleanup.
The production default is 240 seconds; larger boards have explicit longer budgets.
The supervisor stops timed-out workers and their browser descendants, reports a
source error and continues. Existing jobs for that source are retained. This
prevents a navigation/cleanup hang, such as the UCP failure, from blocking the
whole digest. LinkedIn authentication is checked inside each LinkedIn worker.

The complete scan has a 75-minute budget, leaving time before the Actions
100-minute limit for delivery and state persistence. Sources not reached within
that budget are explicitly reported as not scanned. JSON evidence is saved
atomically after every source; the notification baseline is still committed only
after SMTP acceptance. Optional top-level `site_timeout_seconds` and
`scan_timeout_seconds` configure these positive, finite budgets.

## State and delivery

`state.json` stores canonical job identities, page hashes, per-site parser version,
missing counters, and CV feed deduplication/pending state. On parser migration,
unconfirmed legacy entries are cleaned without being reported as filled jobs.
Known canonical URLs do not become new just because the parser changed.

A job is reported as no longer listed only after two usable scans omit it. Empty
unconfirmed scans and blocked detail pages preserve prior verified jobs. This is
an absence signal, not proof a position was filled.

State is written atomically after SMTP acceptance (or a run requiring no mail).
Failed delivery leaves the prior baseline intact. Successful and failed scans
upload HTML/JSON artifacts from GitHub Actions for seven days.

CV transfers use only verified ad text and require a High or Medium fit from
`fit_screen.py`. The case-insensitive title pre-filter runs before any API call;
it uses word boundaries (so `intern` does not reject `International`) plus the
specified German stems and role-family suffixes. These hard exclusions override
model judgement. The screening prompt applies Low exclusions first and specific
Medium lanes (including platform M&A Manager) before overlapping High rules.
Unknown facts remain unknown; site headquarters are not evidence of job location.

Newly verified roles and previously pending roles at eligible sources are screened;
a site's first successful scan still establishes its baseline without bulk feeding
historical jobs. At most **40 API attempts per run**, including failed attempts,
are allowed, within a ten-minute screening-stage budget (a full 60-second call
is reserved before starting another). This keeps slow screenings from using the
time needed for email/state delivery after the bounded scan. Missing `OPENAI_API_KEY`, API failure/incomplete output, exhausted
budget, unavailable ad text, or failed Sheet writes leave candidates in
`tracker_pending`. They are retried only after fresh ad verification. No unscreened
row is appended. `tracker_screened_out` stores each Low/prefilter decision keyed
by job identity (title, company, fit, date, reason); these are never automatically
screened again. The email lists fed fits/location/reason, newly screened-out roles,
and pending candidates with transfer/screening errors. State still commits only
after successful delivery. A Google `invalid_grant` requires renewing the configured
Google authorization; code cannot repair an expired/revoked credential.

The feed reads **A:D once per run**, across all statuses, to deduplicate canonical
URLs and employer/title pairs. Title matching ignores case, punctuation, gender
markers and known city suffixes; Investment Associate equals Associate Investment
Team, but seniority is retained. Duplicates are recorded in `tracker_fed` with date
`already in sheet`. Same-batch duplicates become fed only after a successful append.

The ten-column contract matches the CV pipeline's `CSV_FIELDS`:
A Website, B Description, C Company, D Job Position, E Status, F Date, G My Comment,
H Fit Probability, **I Location, J Claude Comment**. Status is `NEW`, or
`NEEDS_REVIEW` when verified text is under 800 characters. Date uses Europe/Zurich
(`YYYY-MM-DD HH:MM CEST`, `CET` in winter). Location uses verified ad metadata,
normalized to City, Country or Remote (Country), empty when unresolved. A single
JSON-LD job location takes precedence; multiple locations are not guessed.
Description contains title, employer, location, JSON-LD datePosted (or `unknown`),
source URL, a blank line and the full extracted verified text with real newlines.
The comment starts `Career page monitor <date> | Fit: <High|Medium> | ...` and
contains summary/source/posted date; it never emits another automation's daily marker.

Merge the companion Job-Tracker-Update schema/status PR before enabling this feed:
https://github.com/tobivdb/Job-Tracker-Update/pull/42. The pipeline must retain
Location in I, write Claude Comment in J, and process both empty and `NEW` statuses.

## Configuration

Each site has `name`, `url`, optional boolean `feed`, `tier`, `exclude_patterns`,
`include_job_patterns`, and `no_jobs_indicators`. Include patterns are matched
against the individual job's fields, never the entire board. A no-jobs indicator
does not override actual vacancy candidates (many boards have hidden templates).
Names must be unique. Existing priority tiers, sources and daily schedule are
preserved by the September 2026 correction.


Feed precedence: `tracker_feed.enabled: false` disables all transfers. Otherwise,
an explicit per-site `feed: true/false` takes precedence over the legacy
`tracker_feed.tiers` list. Only when `feed` is absent is that list consulted;
without either opt-in the default is false. Tier labels still control digest
priority and are independent of feed eligibility. Existing role filters apply.

The mandatory gate uses Responses with strict JSON schema, `store=false`, no tools,
600 output tokens, a 60-second timeout and no retries within a call. Set
`OPENAI_API_KEY` only as an environment variable/repository secret. The Run job
monitor step receives it; `OPENAI_SCREEN_MODEL` defaults to `gpt-5-mini` and may be
overridden by its repository variable. The 40-call fit budget is separate from the
optional audit budget. An incomplete response fails closed (the 600-token bound
includes reasoning); the next verified run retries, not an automatic API loop.

## September 2026 coverage audit

The daily schedule runs on GitHub-hosted Ubuntu runners. No laptop is required.
`OPENAI_API_KEY` must be a secret in **this** repository. A secret in
`Job-Tracker-Update` is not inherited. Never put a key in a config file or report.
The optional Responses API review defaults to `gpt-5-mini`; override using the
repository variable `OPENAI_AUDIT_MODEL`. Missing keys are explicitly reported as
`skipped_missing_key`. The scanner itself does not require an OpenAI key.

`site_crawl.py` traverses observed career links and linked ATS portals (two levels,
12 career pages by default), preserves configured filters, and reads subsequent
rendered listing pages (60 by default). Workday numbered pages, visible Next/Load
More controls, Pictet page size and Oracle scrolling have bounded handling. A
stuck or capped traversal is incomplete. Known changed URLs and official portals
are configured explicitly. Four isolated browser workers may scan sources in
parallel; state processing remains in configured order.

Coverage is recorded per source in `scan_results.json`. `checked` means the
observed documents and extracted candidates were processed successfully, not
that all jobs on every possible subsite have been proven discovered. Unexplained
zero results require review. Incomplete scans preserve previously verified jobs,
including during parser migration. The default `python verify_scan.py` command fails on unreadable pages or
incomplete pagination, even if the scanner process completed successfully.
Scheduled and manual full runs use `--operational`: broken source inventories,
invalid output, unexpected worker failures and a scan with no verified adverts
still fail the workflow. Individual site outages and coverage gaps remain explicit
GitHub warnings and `coverage_status: needs_review` in the quality report. A green
execution status is not a complete-coverage claim. The strict live regression
sample is unchanged; `--live --operational` is rejected.

`python verify_scan.py --live` runs a read-only regression sample against Pinova,
Afinum, Egeria and ICG. Pull requests run unit tests and this live sample in
separate jobs without repository secrets. A full read-only scan can be dispatched
on a branch using `dry_run`; production scans and state commits require main.
The fixed source sample can legitimately fail if all a company's adverts close;
inspect the evidence rather than weakening the gate without review.

The optional AI review samples up to 12 sources daily and 40 on Sundays, including
a rotating sample of apparently healthy sources. Each request is bounded to three
page excerpts, 180 observed links, 1,800 output tokens and a 60-second timeout;
there are no automatic retries. This is a cost-bounded second opinion, not an
independent full crawl or a completeness guarantee. Suggestions must reference
observed link IDs and remain review-only in `ai_audit.json`. They never enter the
CV queue or modify source health. Requests contain only public career-page data,
use `store=false`, and have no tools. Authentication/rate-limit failures are
reported without response bodies or credentials. See the official
[Structured Outputs guide](https://developers.openai.com/api/docs/guides/structured-outputs).

Daily emails are sent for job changes or changed source health. Repeated unchanged
warnings no longer force an email. All sources and current warnings remain in the
HTML/JSON artifacts (seven-day retention). API findings appear in the artifact and
Actions summary; they do not send an additional email.


## Feed source configuration (September 27, 2026)

The separate source PR sets an explicit boolean on all **203 retained sources**:
**140 feed-enabled, 63 digest-only**. Existing priority tiers, retained source names,
URLs, role filters, timeout settings and the daily schedule are preserved. Eligibility
is a source-level opt-in, not a finding that every role is suitable. All High/Medium
requirements and geographic exclusions still apply to individual verified ads.

All A–E/ZH sites and the explicitly requested German/Swiss sources are included.
That explicit inclusion instruction takes precedence over the narrower generic
source categories: banks/allocators, VC, advisory and other mixed sources in that
list remain eligible for screening, while excluded roles cannot enter the sheet.
Examples requiring review are Banque Pictet, EKBQ, Jacobs Foundation, Muzinich,
Petiole, LGT Capital Partners, HQ Capital, MPEP, Alpha Associates, Flexstone,
Montana, HarbourVest, HBM, IBB Ventures, coinIX, Aravis, Apricum and Bridgemaker.
No investment-business-model reclassification of those firms is implied.

Foreign-headquartered sources may be included when they recruit in DACH; a foreign
URL or head office alone does not exclude them. Evidence includes
[Waterland's German roles](https://www.waterlandpe.com/careers/),
[Main's Düsseldorf recruiting](https://main.nl/de/stellenangebote/),
[Egeria's German/Swiss careers](https://egeriagroup.com/career/),
[Avedon's Düsseldorf recruiting](https://avedoncapital.com/our-people/),
[Gilde's Frankfurt recruiting](https://gildehealthcare.com/de/career-opportunities/),
[Cinven's 2026 Frankfurt hire](https://www.cinven.com/team/pia-borjans/),
[Rivean's careers](https://riveancapital.com/working-with-us/) and
[Frankfurt/Zug offices](https://riveancapital.com/contact/).
Nordic Capital is enabled based on its official
[Frankfurt office](https://www.nordiccapital.com/contact/frankfurt/) and
[recruitment program featuring Frankfurt hires](https://career.nordiccapital.com/pages/internship-program).
This establishes a DACH hiring footprint, not a current suitable vacancy: the
[current board](https://career.nordiccapital.com/jobs) showed only a Stockholm
internship on review. The title/fit gate still rejects internships.

Non-DACH-only sources remain in the digest with `feed: false`. Where DACH hiring
was not established, sources stay digest-only pending review rather than assuming
eligibility from an investment in a German company. The PR's complete source table
identifies these conservative classifications; examples are Permira, BC Partners,
Montagu, IK, Apax, Bridgepoint, CVC DIF, Columna and Bamboo. This does not
assert that they lack DACH offices or never hire there.
[Müller-Möhl](https://mm-grp.com/en/) remains digest-only: the official description
establishes multi-asset family-office management, but not a direct-investment
hiring mandate. Revisit if that mandate is confirmed.

Only TMF Group and Kantar were removed as investment-source exclusions. Duplicate
Invision AG was merged into Invision (identical URL; retained source-history note).
Ufenau Capital Partners was merged into UCP, retaining UCP's 600-second timeout:
the URLs differ by `language=en&display=undefined`, but both returned the same six
job IDs (641217, 675192, 2264527, 2361358, 2700171, 2799873) on review. No other
sources were deleted, and historic state was not edited. Future removals require
review of the proposed list in the PR description.
