# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

A GitHub Actions-based job monitor that scrapes ~207 PE/investment firm career pages daily and sends a Gmail summary email. Everything is fully device-free — config, state, credentials, and schedule all live in GitHub. The site list is kept in sync with the "PE Funds Tracker" tab of `20260212_PE_Job_Search_Tracker.xlsx` (Drive, CVs root folder); last reconciliation 2026-07-03.

## Running the monitor

```bash
# Dry run — scrapes all sites, saves HTML report to last_report.html, no email sent
python job_monitor.py --config config.github.json --dry-run

# Show currently tracked jobs from state.json
python job_monitor.py --config config.github.json --list

# Reset state (next run treats all sites as first scan)
python job_monitor.py --config config.github.json --reset
```

Dependencies: `pip install -r requirements.txt && playwright install chromium`

Email credentials are **only** available as GitHub Secrets (`SENDER_EMAIL`, `SENDER_PASSWORD`, `RECIPIENT_EMAIL`). To send a real email, trigger the workflow manually: GitHub → Actions → Check Job Postings → Run workflow.

## Architecture

The pipeline runs in a single pass per execution:

```
config.github.json → fetch_page() → extract_jobs_from_page() → compute_diff() → build_email_html() → send_email()
                                                                      ↕
                                                                 state.json
```

**`state.json`** is committed back to `main` after every GitHub Actions run. It stores per-site `job_keys` (MD5 of `title|url`) and a `page_hash` (SHA-256 of normalized body text). This is the change-detection mechanism — no database needed.

**Job extraction** uses four cascading strategies on the rendered HTML (via Playwright/Chromium): CSS class matching (`job`, `career`, `stelle`, etc.), heading-based (`h2`/`h3`/`h4`), link-based (`bewerben`, `/job/`, etc.), and finally a page hash fallback for raw change detection.

**LinkedIn pages** are filtered more aggressively — only roles matching `PE_RELEVANT_KEYWORDS` (associate, investment manager, buyout, LBO, etc.) are kept, because LinkedIn shows many unrelated roles.

## Email structure

The daily email always sends (`--always-email`). Subject is either:
- `Job Monitor - Changes Detected: <up to 3 companies, then "+N more">`
- `Job Monitor - No Changes`

The email is compact and summary-first (all styles inline for Gmail; total size stays
far below Gmail's 102 KB clipping limit):
1. **Header** — headline counts of investment-team and other new roles
2. **Stat chips** — investment team / other new / removed / pages changed / errors
3. **New investment-team roles** (`is_pe_relevant`) — one green card per job with company and location
4. **Other new roles** — blue cards
5. **Removed or filled since last scan** — one-line list (signal that a posting closed)
6. **Page changed, no structured roles** — one-line list of site links
7. **First scan** / **Errors** — one-line lists
8. **Footer** — count of unchanged sites (unchanged sites are never listed individually)

## Two keyword sets

- **`EMAIL_SUMMARY_KEYWORDS`** (narrow): `private equity | associate | investment manager` — controls the top summary box and the "Investment Team" label within site sections
- **`PE_RELEVANT_KEYWORDS`** (broad): ~20 patterns including portfolio, buyout, LBO, fundraising, etc. — controls LinkedIn filtering and site-level sorting in the email

## config.github.json site fields

| Field | Purpose |
|---|---|
| `name` | Display name (also used as key in `state.json`) |
| `url` | Career page URL |
| `no_jobs_indicators` | Strings that, if found on page, mean no open roles (skips extraction) |
| `exclude_patterns` | Job titles containing these strings are ignored (e.g. `Initiativbewerbung`) |
| `include_job_patterns` | Optional allowlist matched case-insensitively across title, URL, location, and detail. When set, only matching jobs are retained; useful for location segments in global Workday URLs. |
| `notes` | Informational only, not used by the scraper |
| `tier` | Priority tier from the PE Funds Tracker (A/B/C/D/E/ZH/EU). A/B get a badge in the email and sort first; they also gate the CV-pipeline feed. |

## CV-pipeline feed

Top-level `tracker_feed` config block (`enabled`, `tiers`, default `["A","B"]`). When a
non-first-run scan finds a new investment-team role at a fund whose `tier` is in
`tiers`, the monitor scrapes the job ad text and appends a row to the
**Job Ad Overview V2** Google Sheet (the daily CV pipeline's input in the CVs Drive
folder): Website = job URL, Description = scraped ad text, Status empty → the next
06:00 CV run generates the application automatically. Scrapes under 800 chars are
parked as `Status=NEEDS_REVIEW` instead so no application is ever generated from a
garbage description. Fed job keys are remembered in `state.json` (`tracker_fed`) to
prevent duplicates; the dedup key is only written after a successful sheet append.

Requires GitHub secrets (same values as the Job-Tracker-Update repo):
`GOOGLE_OAUTH_CLIENT_JSON` + `GOOGLE_DRIVE_OAUTH_REFRESH_TOKEN` (or
`GOOGLE_SERVICE_ACCOUNT_JSON`) and `GOOGLE_DRIVE_CV_FOLDER_ID`. Without them the feed
logs one info line and is skipped. Queued roles appear in the email under
"Queued for the CV pipeline". A LinkedIn cookie-expiry warning banner is shown in the
email whenever LinkedIn authentication fails.

## Git / branch requirements

Claude Code pushes must go to a branch named `claude/<task>-<session-id>` (e.g. `claude/access-job-monitor-repo-9ue4f`). Pushing to any other branch returns HTTP 403. Always open a PR and merge to `main` — the GitHub Actions workflow runs from `main`.
