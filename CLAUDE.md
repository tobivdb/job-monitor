# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

A GitHub Actions-based job monitor that scrapes 167 PE/investment firm career pages daily and sends a Gmail summary email. Everything is fully device-free — config, state, credentials, and schedule all live in GitHub.

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
- `Job Monitor - Changes Detected: <summary>` 
- `Job Monitor - No Changes`

Email sections in order:
1. **Yellow summary box** — new jobs matching `EMAIL_SUMMARY_KEYWORDS` (`private equity`, `associate`, `investment manager`) only, grouped by company. First-run scans excluded.
2. **PE sites with changes** — sites where new jobs include investment team roles (`is_pe_relevant`)
3. **Other sites with changes** — sites with new/removed jobs but no PE-relevant roles
4. **Page changed** — hash changed but no structured jobs detected
5. **First-run scans** — baseline establishment
6. **No changes**
7. **Errors**

Within each site section, PE/investment team jobs appear first with a purple "Investment Team" badge, separated by a dashed line from other positions.

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
| `notes` | Informational only, not used by the scraper |

## Git / branch requirements

Claude Code pushes must go to a branch named `claude/<task>-<session-id>` (e.g. `claude/access-job-monitor-repo-9ue4f`). Pushing to any other branch returns HTTP 403. Always open a PR and merge to `main` — the GitHub Actions workflow runs from `main`.
