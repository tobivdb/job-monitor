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
It never sends mail, appends Google Sheet rows or changes `state.json`.
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

Each source runs in a separate browser worker with a 180-second wall-clock
deadline covering startup, navigation, detail verification and browser cleanup.
The supervisor stops timed-out workers and their browser descendants, reports a
source error and continues. Existing jobs for that source are retained. This
prevents a navigation/cleanup hang, such as the UCP failure, from blocking the
whole digest. LinkedIn authentication is checked inside each LinkedIn worker.

The complete scan has a 75-minute budget, leaving time before the Actions
90-minute limit for delivery and state persistence. Sources not reached within
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

CV transfers use only verified ad text. Failed transfers remain pending and are
retried only after the ad is verified again. The feed reads existing sheet URLs
before appending to avoid duplication after a partial run failure. Thin ad text is
marked `NEEDS_REVIEW`. A Google `invalid_grant` requires renewing the configured
Google authorization; code cannot repair an expired/revoked credential.

## Configuration

Each site has `name`, `url`, optional `tier`, `exclude_patterns`,
`include_job_patterns`, and `no_jobs_indicators`. Include patterns are matched
against the individual job's fields, never the entire board. A no-jobs indicator
does not override actual vacancy candidates (many boards have hidden templates).
Names must be unique. Existing priority tiers, sources and daily schedule are
preserved by the September 2026 correction.


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
including during parser migration. An HTTP error or incomplete pagination fails
the separate coverage check even if the scanner process completed successfully.

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
