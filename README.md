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
