#!/usr/bin/env python3
"""
Job Monitor - Checks company career pages for new job postings.
Sends email notifications when changes are detected.

Usage:
    python job_monitor.py              # Run a check
    python job_monitor.py --dry-run    # Run without sending emails
    python job_monitor.py --reset      # Clear saved state and start fresh
    python job_monitor.py --list       # Show currently tracked jobs
"""

import json
import hashlib
import html as html_module
import logging
import argparse
import os
import smtplib
import ssl
import sys
import re
import time
import math
import subprocess
import tempfile
from pathlib import Path
from datetime import datetime
from zoneinfo import ZoneInfo

import fit_screen
from tracker_fields import clean_job_title, employer_title_key, normalize_location, posted_date
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from dataclasses import asdict, dataclass, field
from typing import Optional

from scan_process import run_bounded
from playwright.sync_api import sync_playwright
from bs4 import BeautifulSoup
from site_crawl import crawl_careers
from concurrent.futures import ThreadPoolExecutor
from job_sources import (
    EXTRACTOR_VERSION, NON_JOB_TEXT, canonical_url, candidates, is_linkedin,
    verify_detail, ROLE_WORDS, RateLimited, vacancy_identity,
)

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

BASE_DIR = Path(__file__).parent
CONFIG_FILE = BASE_DIR / "config.json"
if not CONFIG_FILE.exists():
    CONFIG_FILE = BASE_DIR / "config.github.json"
STATE_FILE = BASE_DIR / "state.json"
LOG_FILE = BASE_DIR / "job_monitor.log"

# Patterns to always exclude (generic unsolicited application links and irrelevant roles)
DEFAULT_EXCLUDE = [
    "initiativbewerbung", "spontanbewerbung", "blindbewerbung",
    "unsolicited application", "open application",
    "werkstudent", "praktikant", "praktikum", "internship", "intern ",
    "fund controller", "marketing", "stagiair", "stagiaire", "stage ",
]

# Regex to collapse runs of whitespace and strip location/seniority suffixes
# that get concatenated when HTML elements have no separator text
_TITLE_TRIM = re.compile(r"\s{2,}")


def clean_title(raw: str) -> str:
    """Collapse whitespace runs and trim trailing location fragments."""
    return _TITLE_TRIM.sub(" ", raw).strip()

# Titles that are definitely NOT job postings (navigation, section headers, etc.)
NOISE_TITLES = {
    "home", "about", "contact", "menu", "back", "top", "karriere", "career",
    "careers", "offene stellen", "open positions", "über uns", "about us",
    "team", "kontakt", "impressum", "datenschutz", "privacy", "newsletter",
    "services", "leistungen", "produkte", "products", "news", "events",
    "blog", "aktuelles", "verwaltungsrat", "advisory board", "board of directors",
    "unser team", "our team", "management", "partner", "partners",
    "mehr laden", "load more", "see more", "mehr erfahren", "weiterlesen",
    "read more", "alle anzeigen", "show all", "zurück", "weiter", "next",
    "previous", "jetzt bewerben", "apply now", "zur bewerbung",
}

# Pattern to detect date-like strings (events, not jobs)
DATE_PATTERN = re.compile(r"^\d{2}\.\d{2}\.\d{4}")


# PE-relevant keywords — only LinkedIn jobs matching these are included
PE_RELEVANT_KEYWORDS = re.compile(
    r"(private\s+equity|direct\s+investm|associate|investment\s+manager|"
    r"portfolio|fund|venture|buyout|m&a|mergers|acquisitions|"
    r"due\s+diligence|deal|transaction|lbo|leveraged|"
    r"principal|vice\s+president|managing\s+director|"
    r"investor\s+relations|fundrais|co-invest|coinvest)",
    re.IGNORECASE,
)

# Location keywords for Swiss/DACH job postings
LOCATION_PATTERN = re.compile(
    r"(Zürich|Zurich|Zug|Basel|Bern|Geneva|Genf|Genève|Lausanne|Crissier|"
    r"Luzern|Lucerne|St\.?\s*Gallen|Winterthur|Lugano|Biel|Thun|"
    r"Munich|München|Frankfurt|Berlin|Hamburg|Wien|Vienna|Schweiz|Switzerland|"
    r"Deutschland|Germany|Österreich|Austria|Amsterdam|London|New York|Dublin|Warsaw|Singapore|Singapur|Tokyo)",
    re.IGNORECASE,
)

_log_handlers = [logging.StreamHandler(sys.stdout)]
# Only write log file when running locally (not in CI)
if not os.environ.get("CI"):
    _log_handlers.append(logging.FileHandler(LOG_FILE))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=_log_handlers,
)
log = logging.getLogger("job_monitor")


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------

@dataclass
class JobEntry:
    title: str
    url: str = ""
    location: str = ""
    detail: str = ""
    date_posted: str = ""

    @property
    def key(self) -> str:
        """Stable identifier for deduplication."""
        raw = vacancy_identity(self.url) or html_module.unescape(self.title).strip().casefold()
        return hashlib.md5(raw.encode()).hexdigest()


@dataclass
class SiteResult:
    name: str
    url: str
    jobs: list[JobEntry] = field(default_factory=list)
    page_hash: str = ""
    error: Optional[str] = None
    has_no_jobs_indicator: bool = False
    warnings: list[str] = field(default_factory=list)
    unverified_urls: list[str] = field(default_factory=list)
    descriptions: dict[str, str] = field(default_factory=dict)
    coverage_complete: bool = True
    coverage: dict = field(default_factory=dict)
    evidence: list = field(default_factory=list)


# ---------------------------------------------------------------------------
# Page fetching (Playwright - handles JS-rendered pages)
# ---------------------------------------------------------------------------

def is_linkedin_url(url: str) -> bool:
    """Check if a URL is a LinkedIn page."""
    return is_linkedin(url)


def is_pe_relevant(title: str, detail: str = "") -> bool:
    """Check if a job title/detail is relevant to Private Equity roles."""
    # General company descriptions must not turn legal/IT/support roles into PE jobs.
    title = html_module.unescape(title)
    if re.search(r"\b(legal|compliance|treasury|service desk|operations|accounting|"
                 r"controller|marketing|human resources|paralegal|assistant|"
                 r"fund services|fund oversight|performance measurement)\b", title, re.I):
        return False
    return not NON_JOB_TEXT.search(title) and bool(PE_RELEVANT_KEYWORDS.search(title))


def load_linkedin_cookies(context) -> bool:
    """Load LinkedIn session cookies into the browser context.
    Cookies come from LINKEDIN_COOKIES env var (JSON string) or linkedin_cookies.json file.
    Returns True if cookies were loaded successfully."""
    cookies_json = os.environ.get("LINKEDIN_COOKIES", "")

    if not cookies_json:
        cookies_file = BASE_DIR / "linkedin_cookies.json"
        if cookies_file.exists():
            cookies_json = cookies_file.read_text(encoding="utf-8")
        else:
            log.warning("No LinkedIn cookies found. Run export_linkedin_cookies.py first.")
            return False

    try:
        cookies = json.loads(cookies_json)
        if not cookies:
            log.warning("LinkedIn cookies are empty.")
            return False
        context.add_cookies(cookies)
        log.info(f"Loaded {len(cookies)} LinkedIn cookies.")
        return True
    except Exception:
        log.warning("Failed to load LinkedIn cookies (invalid cookie data).")
        return False


def verify_linkedin_session(context) -> bool:
    """Verify that the loaded cookies give us an authenticated LinkedIn session."""
    page = context.new_page()
    try:
        page.goto("https://www.linkedin.com/feed/", wait_until="domcontentloaded", timeout=30000)
        page.wait_for_timeout(3000)
        current_url = page.url
        if not is_linkedin_url(current_url) or not re.search(r"linkedin\.com/feed/?(?:\?|$)", current_url):
            log.warning("LinkedIn cookies expired or invalid — redirected to login.")
            return False
        log.info(f"LinkedIn session verified (at: {current_url})")
        return True
    except Exception as e:
        log.warning(f"LinkedIn session verification failed: {e}")
        return False
    finally:
        page.close()


def fetch_page(url: str, playwright_context, timeout: int = 20000, *, with_url=False, job_button_selector=""):
    """Fetch rendered HTML with a bounded navigation time.

    Waiting for networkidle is a poor fit for modern career sites because
    analytics and long-polling requests may never become idle. Waiting for the
    DOM plus a short rendering window keeps each site bounded to roughly the
    configured timeout plus three seconds.
    """
    page = playwright_context.new_page()
    try:
        response = page.goto(url, wait_until="domcontentloaded", timeout=timeout)
        if response is None or response.status >= 400:
            raise ValueError(f"Career page HTTP {response.status if response else 'no response'}")
        page.wait_for_timeout(3000)  # Give client-rendered job boards time to populate.
        if "myworkdayjobs.com" in url:
            try:
                page.locator('a[data-automation-id="jobTitle"]').first.wait_for(state="attached", timeout=12000)
            except Exception:
                log.warning("Workday did not expose a job link within the rendering window.")

        # Check if LinkedIn redirected to login wall
        if is_linkedin_url(url) and any(p in page.url.lower() for p in ("/login", "/authwall", "/checkpoint", "/challenge")):
            log.warning(f"  LinkedIn login wall hit for {url}")
            raise Exception("LinkedIn login wall - not authenticated")

        html = page.content()
        if job_button_selector:
            # Some ATS boards expose navigation only through buttons. Resolve the
            # real browser URL after opening each ad; never invent an ID or path.
            supplemental = BeautifulSoup(html, "lxml")
            count = page.locator(job_button_selector).count()
            for index in range(count):
                detail_page = playwright_context.new_page()
                try:
                    detail_page.goto(page.url, wait_until="domcontentloaded", timeout=timeout)
                    button = detail_page.locator(job_button_selector).nth(index)
                    button.wait_for(state="visible", timeout=timeout)
                    title = button.locator(".job-opening-title").inner_text()
                    button.click(timeout=timeout)
                    detail_page.wait_for_url(lambda target: str(target) != page.url, timeout=timeout)
                    link = canonical_url(detail_page.url)
                    if link:
                        anchor = supplemental.new_tag("a", href=link)
                        anchor.string = title
                        (supplemental.body or supplemental).append(anchor)
                finally:
                    detail_page.close()
            html = str(supplemental)
        return (html, page.url) if with_url else html
    except Exception as e:
        log.warning(f"Error fetching {url}: {e}")
        raise
    finally:
        page.close()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def is_noise_title(title: str) -> bool:
    """Check if a title is generic noise, not a job posting."""
    t = title.strip().lower()

    # Exact matches to known noise
    if t in NOISE_TITLES:
        return True

    # Too short to be a job title
    if len(t) < 4:
        return True

    # Starts with a date (likely an event)
    if DATE_PATTERN.match(t):
        return True

    # Looks like a metadata fragment (e.g., "Zug|Werkstudent|Teilzeit")
    if "|" in t and not ROLE_WORDS.search(t):
        return True

    # Contains only a person's name pattern (First Last or First LastFirst Last)
    # Heuristic: no spaces or only 1-2 words with capital letters, no job-like words
    words = t.split()
    if len(words) <= 2 and all(w[0].isupper() if w else False for w in title.strip().split()):
        # Likely a person name, not a job
        job_indicators = ["manager", "analyst", "specialist", "director", "associate",
                          "engineer", "developer", "consultant", "controller", "accountant",
                          "assistant", "coordinator", "intern", "werkstudent", "praktikant",
                          "senior", "junior", "head", "chief", "lead", "officer",
                          "(w/m/d)", "(m/w/d)", "(m/f/d)", "(w/m)", "(m/w)"]
        if not ROLE_WORDS.search(t) and not any(ind in t for ind in job_indicators):
            return True

    return False




def extract_location(text: str) -> str:
    """Extract location from text."""
    match = LOCATION_PATTERN.search(text)
    return match.group(0) if match else ""


def clean_soup(soup: BeautifulSoup) -> BeautifulSoup:
    """Remove non-content elements from soup."""
    for tag in soup.find_all(["script", "style", "noscript", "svg", "path", "meta", "link"]):
        tag.decompose()
    return soup


def normalize_text_for_hash(text: str) -> str:
    """Remove dynamic content (timestamps, counters) before hashing to reduce
    false 'page changed' alerts."""
    # Remove timestamps like "13.02.2026 18:22" or "2026-02-13T18:22:00"
    text = re.sub(r"\d{2}\.\d{2}\.\d{4}\s*\d{2}:\d{2}", "", text)
    text = re.sub(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}", "", text)
    # Remove cookie banner IDs, session tokens, nonces
    text = re.sub(r"[a-f0-9]{32,}", "", text)
    # Collapse whitespace
    text = re.sub(r"\s+", " ", text).strip()
    return text


# ---------------------------------------------------------------------------
# Job extraction — Main function with multiple strategies
# ---------------------------------------------------------------------------

def extract_jobs_from_page(html: str, site_config: dict, final_url: str = "") -> SiteResult:
    """Extract individual vacancy candidates, never arbitrary page headings."""
    soup = BeautifulSoup(html, "lxml")
    result = SiteResult(name=site_config["name"], url=site_config["url"])
    # Collect structured data before scripts are removed.
    found = list(candidates(soup, site_config, final_url or result.url))
    clean_soup(soup)
    for tag in soup.find_all(["nav", "header", "footer"]):
        tag.decompose()
    body = soup.find("body") or soup
    body_text = body.get_text(" ", strip=True)
    result.page_hash = hashlib.sha256(normalize_text_for_hash(body_text).encode()).hexdigest()
    from job_sources import BLOCKED_TEXT
    if BLOCKED_TEXT.search(body_text[:2000]):
        result.error = "Career page blocked; previous results retained."
        return result

    exclude = [p.lower() for p in site_config.get("exclude_patterns", []) + DEFAULT_EXCLUDE]
    include = [p.lower() for p in site_config.get("include_job_patterns", [])]
    jobs = {}
    for title, link, location, detail in found:
        title = clean_title(title)
        if not title or is_noise_title(title) or NON_JOB_TEXT.search(title):
            continue
        if any(p in title.lower() for p in exclude):
            continue
        location = location or extract_location(detail)
        if include and not any(p in " ".join((title, link, location, detail)).lower() for p in include):
            continue
        job = JobEntry(title=title, url=link, location=location, detail=detail[:300])
        # One job per canonical URL, prefer concise titles over an entire row.
        if job.key not in jobs or len(title) < len(jobs[job.key].title):
            jobs[job.key] = job
    result.jobs = list(jobs.values())
    # A hidden "no results" template must not override actual vacancy links.
    result.has_no_jobs_indicator = not result.jobs and any(
        indicator.lower() in body_text.lower() for indicator in site_config.get("no_jobs_indicators", [])
    )
    if not result.jobs and not result.has_no_jobs_indicator:
        result.warnings.append("No verifiable vacancy links extracted; inspect the career page manually.")
    return result


def validate_jobs(result: SiteResult, config: dict, context):
    """Only verified individual ads may enter email, state or the CV pipeline."""
    if result.error:
        return
    verified = {}
    for job in result.jobs:
        if time.monotonic() >= config.get("_deadline", float("inf")):
            result.coverage_complete = False
            result.warnings.append("Detail verification reached the source time budget; remaining ads are unverified.")
            break
        page = context.new_page()
        try:
            if config.get("request_delay_seconds"):
                page.wait_for_timeout(min(10, float(config["request_delay_seconds"])) * 1000)
            final_url, description = verify_detail(page, job, config)
            job.url = final_url
            verified[job.key] = job
            result.descriptions[job.key] = description
        except RateLimited:
            result.coverage_complete = False
            result.warnings.append("Source rate limited; remaining details deferred and previous jobs retained.")
            break
        except Exception as exc:
            result.unverified_urls.append(canonical_url(job.url))
            result.warnings.append(f"{job.title}: link/ad not verified ({type(exc).__name__}: {str(exc)[:160]}).")
        finally:
            page.close()
    result.jobs = list(verified.values())

# ---------------------------------------------------------------------------
# State management
# ---------------------------------------------------------------------------

def load_state() -> dict:
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    return {"sites": {}, "last_run": None}


def save_state(state: dict):
    state["last_run"] = datetime.now().isoformat()
    temporary = STATE_FILE.with_suffix(".tmp")
    temporary.write_text(json.dumps(state, indent=2, ensure_ascii=False), encoding="utf-8")
    temporary.replace(STATE_FILE)


# ---------------------------------------------------------------------------
# Diff engine
# ---------------------------------------------------------------------------

@dataclass
class SiteDiff:
    name: str
    url: str
    new_jobs: list[JobEntry] = field(default_factory=list)
    removed_jobs: list[JobEntry] = field(default_factory=list)
    page_changed: bool = False
    is_first_run: bool = False
    has_no_jobs_indicator: bool = False
    error: Optional[str] = None
    tier: str = ""  # priority tier from the PE Funds Tracker (A/B/C/D/E/EU), "" if unmapped
    warnings: list[str] = field(default_factory=list)
    parser_migrated: bool = False


def tier_rank(tier: str) -> int:
    """Sort key: A first, unmapped last."""
    order = {"A": 0, "B": 1, "C": 2, "D": 3, "E": 4, "ZH": 5, "EU": 6}
    return order.get((tier or "").upper(), 9)


def compute_diff(result: SiteResult, state: dict) -> SiteDiff:
    site_state = state["sites"].get(result.name, {})
    diff = SiteDiff(name=result.name, url=result.url, warnings=list(result.warnings))

    if result.error:
        diff.error = result.error
        return diff

    diff.has_no_jobs_indicator = result.has_no_jobs_indicator

    if not site_state:
        diff.is_first_run = True
        diff.new_jobs = result.jobs
        return diff

    legacy = site_state.get("extractor_version") != EXTRACTOR_VERSION
    diff.parser_migrated = legacy
    old_jobs = {}
    for data in site_state.get("job_keys", {}).values():
        old = JobEntry(**data)
        old.title = html_module.unescape(old.title)
        old_jobs[old.key] = old
    old_keys = set(old_jobs)
    new_keys = {j.key: j for j in result.jobs}

    for key, job in new_keys.items():
        if key not in old_keys:
            diff.new_jobs.append(job)

    missing = site_state.get("missing_counts", {})
    for key in old_keys:
        old = old_jobs[key]
        # Parser cleanup and blocked detail pages are not evidence a role was filled.
        if (result.coverage_complete and not legacy and key not in new_keys
                and canonical_url(old.url) not in result.unverified_urls
                and (result.has_no_jobs_indicator or result.jobs)
                and missing.get(key, 0) >= 1):
            diff.removed_jobs.append(old)

    # Also check page-level change
    old_hash = site_state.get("page_hash", "")
    if old_hash and old_hash != result.page_hash:
        diff.page_changed = True

    return diff


def update_state(state: dict, result: SiteResult):
    old_state = state["sites"].get(result.name, {})
    current = {j.key: {"title": j.title, "url": j.url, "location": j.location, "detail": j.detail}
               for j in result.jobs}
    missing = {}
    if old_state.get("extractor_version") == EXTRACTOR_VERSION or not result.coverage_complete:
        for data in old_state.get("job_keys", {}).values():
            job = JobEntry(**data)
            if job.key in current:
                continue
            uncertain = (not result.coverage_complete or canonical_url(job.url) in result.unverified_urls
                         or not result.jobs and not result.has_no_jobs_indicator)
            count = old_state.get("missing_counts", {}).get(job.key, 0) + (0 if uncertain else 1)
            if uncertain or count < 2:
                current[job.key] = data
                missing[job.key] = count
    state["sites"][result.name] = {
        "url": result.url,
        "page_hash": result.page_hash,
        "job_keys": current,
        "extractor_version": EXTRACTOR_VERSION,
        "missing_counts": missing,
        "last_checked": datetime.now().isoformat(),
        "has_no_jobs_indicator": result.has_no_jobs_indicator,
        "coverage_complete": result.coverage_complete,
    }


# ---------------------------------------------------------------------------
# CV-pipeline feed (Job Ad Overview V2 Google Sheet)
# ---------------------------------------------------------------------------

TRACKER_SHEET_NAME = "Job Ad Overview V2"
TRACKER_COLUMNS = 10  # Website..Fit Probability, Location (I), Claude Comment (J); CV CSV_FIELDS


def google_sheets_available() -> bool:
    """Feed is active only when the CV pipeline's Google credentials are configured."""
    has_oauth = bool(os.environ.get("GOOGLE_OAUTH_CLIENT_JSON", "").strip() and os.environ.get("GOOGLE_OAUTH_REFRESH_TOKEN", "").strip())
    has_sa = bool(os.environ.get("GOOGLE_SERVICE_ACCOUNT_JSON", "").strip())
    return bool(os.environ.get("GOOGLE_DRIVE_CV_FOLDER_ID", "").strip()) and (has_oauth or has_sa)


def _google_services():
    """Build Drive + Sheets clients from the same env credentials the CV pipeline uses."""
    from google.oauth2 import service_account
    from google.oauth2.credentials import Credentials
    from googleapiclient.discovery import build

    scopes = ["https://www.googleapis.com/auth/drive"]
    # .strip() everywhere: a stray leading/trailing whitespace in a pasted GitHub
    # secret would otherwise produce an opaque invalid_grant at the first real feed.
    client_json = os.environ.get("GOOGLE_OAUTH_CLIENT_JSON", "").strip()
    refresh_token = os.environ.get("GOOGLE_OAUTH_REFRESH_TOKEN", "").strip()
    if client_json and refresh_token:
        data = json.loads(client_json)
        cfg = data.get("installed") or data.get("web") or data
        creds = Credentials(
            token=None,
            refresh_token=refresh_token,
            token_uri=cfg.get("token_uri", "https://oauth2.googleapis.com/token"),
            client_id=cfg["client_id"],
            client_secret=cfg["client_secret"],
            scopes=scopes,
        )
    else:
        info = json.loads(os.environ["GOOGLE_SERVICE_ACCOUNT_JSON"])
        creds = service_account.Credentials.from_service_account_info(info, scopes=scopes)
    drive = build("drive", "v3", credentials=creds, cache_discovery=False)
    sheets = build("sheets", "v4", credentials=creds, cache_discovery=False)
    return drive, sheets


def _find_tracker_sheet_id(drive) -> str:
    folder_id = os.environ["GOOGLE_DRIVE_CV_FOLDER_ID"]
    resp = drive.files().list(
        q=(
            f"'{folder_id}' in parents and trashed=false and "
            f"name='{TRACKER_SHEET_NAME}' and mimeType='application/vnd.google-apps.spreadsheet'"
        ),
        fields="files(id, name)",
        supportsAllDrives=True,
        includeItemsFromAllDrives=True,
    ).execute()
    files = resp.get("files", [])
    if not files:
        raise RuntimeError(f"Tracker sheet '{TRACKER_SHEET_NAME}' not found in CV folder.")
    if len(files) != 1:
        raise RuntimeError("Multiple tracker sheets found; refusing an ambiguous destination.")
    return files[0]["id"]




def site_feed_enabled(site: dict, feed_config: dict) -> bool:
    """Explicit boolean overrides the legacy tier fallback; neither means disabled."""
    if "feed" in site:
        return site["feed"] is True
    return str(site.get("tier", "")).upper() in {str(t).upper() for t in feed_config.get("tiers", [])}


def feed_tracker(candidates: list[dict], state: dict, *, errors=None, screened_out=None) -> list[dict]:
    """Deduplicate, screen at most 40 ads, then append only High/Medium fits.

    Failures remain pending. Reports contain only confirmed appends. Low decisions
    persist with normal delivered state, independently of later append failures.
    """
    if not candidates:
        return []
    errors = errors if errors is not None else []
    screened_out = screened_out if screened_out is not None else []
    fed = state.setdefault("tracker_fed", {})
    rejected = state.setdefault("tracker_screened_out", {})
    pending = state.setdefault("tracker_pending", {})
    drive, sheets = _google_services()
    sheet_id = _find_tracker_sheet_id(drive)
    # One read, across every status, including already processed and rejected rows.
    existing = sheets.spreadsheets().values().get(
        spreadsheetId=sheet_id, range="A:D"
    ).execute().get("values", [])
    known_urls = {vacancy_identity(row[0]) for row in existing if row and canonical_url(row[0])}
    known_titles = {employer_title_key(row[2], row[3]) for row in existing if len(row) >= 4}
    known_titles.discard(None)
    now = datetime.now(ZoneInfo("Europe/Zurich"))
    stamp, day = now.strftime("%Y-%m-%d %H:%M %Z"), now.strftime("%Y-%m-%d")
    rows, report, batch, aliases = [], [], [], []
    batch_urls, batch_titles = set(), set()
    attempts = 0

    def duplicate(job, company):
        fed[job.key] = {"title": job.title, "company": company, "date": "already in sheet"}
        pending.pop(job.key, None)

    for cand in candidates:
        job, site, desc = cand["job"], cand["site"], cand.get("description", "")
        if job.key in fed or job.key in rejected:
            pending.pop(job.key, None)
            continue
        pending[job.key] = {"title": job.title, "company": site.name, "url": job.url}
        url = vacancy_identity(job.url)
        identity = employer_title_key(site.name, job.title)
        if url in known_urls or identity in known_titles:
            duplicate(job, site.name)
            continue
        if url in batch_urls or identity in batch_titles:
            aliases.append((job, site.name))
            continue
        if not canonical_url(job.url) or not desc.strip():
            pending[job.key]["reason"] = "Verified ad URL/text unavailable"
            errors.append("Verified ad URL/text unavailable; candidate remains pending.")
            continue
        reason = fit_screen.prefilter_reason(job.title)
        result = None
        if not reason:
            if attempts >= fit_screen.MAX_SCREENINGS:
                pending[job.key]["reason"] = "40-screening limit reached"
                errors.append("40-screening limit reached; remaining candidates stay pending.")
                continue
            try:
                # Missing credentials do not consume a paid call, but are still fail-closed.
                if os.environ.get("OPENAI_API_KEY", "").strip():
                    attempts += 1
                result = fit_screen.screen_job(
                    title=job.title, company=site.name, tier=site.tier,
                    notes=cand.get("notes", ""), location=normalize_location(job.location), description=desc,
                )
            except fit_screen.ScreenError as exc:
                pending[job.key]["reason"] = str(exc)
                errors.append(f"{exc}; candidate remains pending for the next verified scan.")
                continue
        if reason or result["fit"] == "Low":
            entry = {"title": job.title, "company": site.name, "fit": "Low", "date": day,
                     "reason": reason or result["reason"]}
            rejected[job.key] = entry
            pending.pop(job.key, None)
            screened_out.append(entry)
            continue
        title = clean_job_title(result["clean_title"])
        company = result["employer"].strip()
        # A model may recognize an employer/title variant not present in the listing.
        identity = employer_title_key(company, title)
        if identity in known_titles:
            duplicate(job, company)
            continue
        if identity in batch_titles:
            aliases.append((job, company))
            continue
        location = normalize_location(job.location)  # verified metadata only, never inferred headquarters
        posted = posted_date(job.date_posted)
        status = "NEW" if len(desc.strip()) >= 800 else "NEEDS_REVIEW"
        description = f"{title} · {company} · {location} · Posted {posted} · Source: Career page {job.url}\n\n{desc}"
        comment = (f"Career page monitor {day} | Fit: {result['fit']} | {result['summary']} | "
                   f"Source: {site.name} {site.url} | Posted {posted}")
        # Never allow external text to emit another automation's daily-run marker.
        comment = re.sub(r"LinkedIn screening", "external screening", comment, flags=re.I)
        rows.append([job.url, description, company, title, status, stamp, "", result["fit"], location, comment])
        report.append({"company": company, "title": title, "key": job.key, "status": status,
                       "fit": result["fit"], "location": location, "reason": result["reason"]})
        batch.append(job)
        batch_urls.add(url)
        batch_titles.add(identity)
        batch_titles.add(employer_title_key(site.name, job.title))
    if rows:
        sheets.spreadsheets().values().append(
            spreadsheetId=sheet_id, range="A1", valueInputOption="RAW",
            insertDataOption="INSERT_ROWS", body={"values": rows},
        ).execute()
        # Neither aliases nor actual candidates are marked fed until append succeeds.
        for job, rep in zip(batch, report):
            fed[job.key] = {"title": rep["title"], "company": rep["company"], "date": stamp}
            pending.pop(job.key, None)
        for job, company in aliases:
            duplicate(job, company)
        log.info("Tracker feed: appended %s screened row(s).", len(rows))
    return report


# ---------------------------------------------------------------------------
# Email notification
# ---------------------------------------------------------------------------

def _html_text(value: str) -> str:
    return html_module.escape(html_module.unescape(str(value)), quote=True)


def _html_link(url: str, label: str, color: str = "#374151") -> str:
    safe = canonical_url(url)
    text = _html_text(label)
    return f'<a href="{html_module.escape(safe, quote=True)}" style="color:{color};">{text}</a>' if safe else text


def _job_card(job: JobEntry, company: str, company_url: str, accent: str = "#166534", bg: str = "#f0fdf4", tier: str = "") -> str:
    """One compact job card, fully inline-styled (Gmail-safe)."""
    title_html = _html_link(job.url, job.title, "#111827")
    tier_badge = ""
    if (tier or "").upper() in ("A", "B"):
        tier_badge = (
            f'<span style="display:inline-block;margin-left:6px;padding:1px 7px;border-radius:999px;'
            f'background:#fef3c7;color:#92400e;font-size:11px;line-height:15px;font-weight:700;'
            f'vertical-align:middle;">Tier {tier.upper()}</span>'
        )
    meta_bits = [_html_link(company_url, company, "#6b7280")]
    if job.location:
        meta_bits.append(_html_text(job.location))
    if not canonical_url(job.url):
        meta_bits.append("Direct link unavailable; use the company career page")
    meta = " &middot; ".join(meta_bits)
    return (
        f'<div style="background:{bg};border-left:4px solid {accent};border-radius:4px;padding:10px 12px;margin:0 0 8px 0;">'
        f'<div style="font-size:15px;line-height:20px;font-weight:700;">{title_html}{tier_badge}</div>'
        f'<div style="font-size:12px;line-height:17px;color:#6b7280;margin-top:2px;">{meta}</div>'
        f'</div>'
    )


def _section_header(title: str, count: int, color: str) -> str:
    return (
        f'<h2 style="font-size:15px;line-height:20px;margin:22px 0 8px 0;color:{color};'
        f'text-transform:uppercase;letter-spacing:0.04em;">{title} '
        f'<span style="color:#9ca3af;font-weight:400;">({count})</span></h2>'
    )


def _compact_line_list(items: list[str]) -> str:
    rows = "".join(
        f'<div style="font-size:13px;line-height:19px;color:#374151;padding:3px 0;'
        f'border-bottom:1px solid #f3f4f6;">{item}</div>'
        for item in items
    )
    return f'<div style="background:#ffffff;border:1px solid #e5e7eb;border-radius:6px;padding:8px 12px;">{rows}</div>'


def build_email_html(
    diffs: list[SiteDiff],
    linkedin_auth_failed: bool = False,
    tracker_fed: Optional[list[dict]] = None,
    tracker_error: str = "",
    tracker_screened_out: Optional[list[dict]] = None,
    tracker_pending: Optional[list[dict]] = None,
) -> tuple[str, bool, list[str]]:
    """Build a compact, summary-first HTML email.

    Design goals (learned from the previous format being unreadable):
    - Everything actionable is in the first screen: stat chips + investment-team roles.
    - Unchanged sites are ONE line (a count), never 150 individual sections.
    - All styling is inline so Gmail/Outlook render it correctly, and the total size
      stays far below Gmail's 102 KB clipping limit even with 160+ monitored sites.
    """
    timestamp = datetime.now().strftime("%d.%m.%Y %H:%M")

    pe_new: list[tuple[SiteDiff, list[JobEntry]]] = []
    other_new: list[tuple[SiteDiff, list[JobEntry]]] = []
    removed: list[tuple[SiteDiff, list[JobEntry]]] = []
    page_changed: list[SiteDiff] = []
    first_runs: list[SiteDiff] = []
    errors: list[SiteDiff] = []
    review = [d for d in diffs if d.warnings]
    unchanged = 0

    for diff in diffs:
        if diff.error:
            errors.append(diff)
            continue
        if diff.is_first_run:
            first_runs.append(diff)
            continue
        pe_jobs = [j for j in diff.new_jobs if is_pe_relevant(j.title, j.detail)]
        other_jobs = [j for j in diff.new_jobs if not is_pe_relevant(j.title, j.detail)]
        if pe_jobs:
            pe_new.append((diff, pe_jobs))
        if other_jobs:
            other_new.append((diff, other_jobs))
        if diff.removed_jobs:
            removed.append((diff, diff.removed_jobs))
        if not diff.new_jobs and not diff.removed_jobs:
            if diff.page_changed:
                page_changed.append(diff)
            else:
                unchanged += 1

    # Tier A/B funds first within each section
    pe_new.sort(key=lambda pair: (tier_rank(pair[0].tier), pair[0].name))
    other_new.sort(key=lambda pair: (tier_rank(pair[0].tier), pair[0].name))

    has_changes = bool(pe_new or other_new or removed or page_changed or errors or review or tracker_error or tracker_fed or tracker_screened_out)
    new_counts: dict[str, int] = {}
    for d, jobs in pe_new + other_new:
        new_counts[d.name] = new_counts.get(d.name, 0) + len(jobs)
    changes_summary = [f"{name}: {n} new" for name, n in new_counts.items()]

    n_pe = sum(len(j) for _, j in pe_new)
    n_other = sum(len(j) for _, j in other_new)
    n_removed = sum(len(j) for _, j in removed)

    def chip(value: int, label: str, fg: str) -> str:
        return (
            f'<td style="padding:10px 12px;border:1px solid #e5e7eb;border-radius:6px;background:#ffffff;text-align:center;">'
            f'<div style="font-size:22px;line-height:26px;font-weight:700;color:{fg};">{value}</div>'
            f'<div style="font-size:11px;line-height:15px;color:#6b7280;">{label}</div></td>'
        )

    body = f"""<!doctype html>
<html><body style="margin:0;padding:0;background:#f3f4f6;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Arial,sans-serif;color:#111827;">
<table role="presentation" width="100%" cellspacing="0" cellpadding="0" style="background:#f3f4f6;padding:20px 0;"><tr><td align="center">
<table role="presentation" width="640" cellspacing="0" cellpadding="0" style="width:640px;max-width:96%;">
<tr><td style="background:#111827;color:#ffffff;border-radius:8px 8px 0 0;padding:18px 22px;">
  <div style="font-size:12px;line-height:16px;color:#9ca3af;">Job Monitor &middot; {timestamp}</div>
  <div style="font-size:20px;line-height:26px;font-weight:700;margin-top:2px;">{n_pe} PE-related role(s), {n_other} other new role(s)</div>
</td></tr>
<tr><td style="background:#ffffff;border:1px solid #e5e7eb;border-top:none;border-radius:0 0 8px 8px;padding:18px 22px;">
<table role="presentation" width="100%" cellspacing="6" cellpadding="0"><tr>
{chip(n_pe, "PE-related", "#166534")}
{chip(n_other, "Other new", "#1e40af")}
{chip(n_removed, "No longer listed", "#92400e")}
{chip(len(page_changed), "Pages changed", "#6b7280")}
{chip(len(errors), "Errors", "#991b1b" if errors else "#6b7280")}
</tr></table>
"""

    if linkedin_auth_failed:
        body += (
            '<div style="margin:14px 0 0 0;padding:11px 14px;border-left:4px solid #d97706;'
            'background:#fffbeb;border-radius:4px;font-size:13px;line-height:19px;color:#92400e;">'
            '<strong>LinkedIn session expired or missing.</strong> LinkedIn boards were skipped; '
            'previous results were retained. Re-run '
            '<code>export_linkedin_cookies.py</code> locally and update the <code>LINKEDIN_COOKIES</code> '
            'GitHub secret.</div>'
        )

    if pe_new:
        body += _section_header("New verified PE-related roles", n_pe, "#166534")
        for diff, jobs in pe_new:
            for job in jobs:
                body += _job_card(job, diff.name, diff.url, accent="#166534", bg="#f0fdf4", tier=diff.tier)

    if other_new:
        body += _section_header("Other new roles", n_other, "#1e40af")
        for diff, jobs in other_new:
            for job in jobs:
                body += _job_card(job, diff.name, diff.url, accent="#1e40af", bg="#eff6ff", tier=diff.tier)

    if tracker_fed:
        body += _section_header("Screened roles added to the CV pipeline", len(tracker_fed), "#6d28d9")
        body += _compact_line_list([
            f'{_html_text(f["company"])} &mdash; {_html_text(f["title"])} '
            f'{_html_text(f["fit"])} · {_html_text(f["location"] or "Location unknown")} · '
            f'{_html_text(" ".join(f["reason"].split()))} '
            f'<span style="color:#9ca3af;">({_html_text(f["status"])})</span>'
            for f in tracker_fed
        ])

    if tracker_pending:
        body += _section_header("Pending fit screening / transfer", len(tracker_pending), "#92400e")
        body += _compact_line_list([
            f'{_html_text(f["company"])} — {_html_text(f["title"])} · '
            f'{_html_text(f.get("reason", "Awaiting current verified ad and successful screening/transfer"))}'
            for f in tracker_pending
        ])

    if tracker_screened_out:
        body += _section_header("Screened out", len(tracker_screened_out), "#6b7280")
        body += _compact_line_list([
            f'{_html_text(f["company"])} — {_html_text(f["title"])} · Low · '
            f'{_html_text(" ".join(f["reason"].split()))}' for f in tracker_screened_out
        ])

    if removed:
        body += _section_header("No longer listed in two successful scans", n_removed, "#92400e")
        body += _compact_line_list([
            f'{_html_link(d.url, d.name)} &mdash; {_html_text(j.title)}'
            for d, jobs in removed for j in jobs
        ])

    if page_changed:
        body += _section_header("Page changed, no new verified roles", len(page_changed), "#6b7280")
        body += _compact_line_list([
            _html_link(d.url, d.name) for d in page_changed
        ])

    if first_runs:
        body += _section_header("First scan (baseline established)", len(first_runs), "#6b7280")
        body += _compact_line_list([
            _html_link(d.url, d.name)
            + (f' &mdash; {len(d.new_jobs)} existing role(s) recorded' if d.new_jobs else '')
            for d in first_runs
        ])

    if errors:
        body += _section_header("Errors", len(errors), "#991b1b")
        body += _compact_line_list([
            f'{_html_link(d.url, d.name, "#991b1b")} &mdash; '
            f'<span style="color:#6b7280;">{html_module.escape((d.error or "")[:120])}</span>'
            for d in errors
        ])

    if tracker_error:
        body += _section_header("CV pipeline requires attention", 1, "#991b1b")
        body += _compact_line_list([_html_text(tracker_error)])
    if review:
        body += _section_header("Manual review — no confirmed vacancy evidence", len(review), "#92400e")
        body += _compact_line_list([
            _html_link(d.url, d.name) + " &mdash; " + _html_text(d.warnings[0][:140])
            + (f" (+{len(d.warnings)-1} more checks in scan_results.json)" if len(d.warnings) > 1 else "")
            for d in review
        ])
    if any(d.parser_migrated for d in diffs):
        body += '<p>Stellenerkennung aktualisiert: alte unbestätigte Treffer wurden bereinigt; dies bedeutet nicht, dass diese Stellen besetzt wurden.</p>'

    body += f"""
<div style="margin-top:20px;padding-top:12px;border-top:1px solid #e5e7eb;font-size:12px;line-height:17px;color:#9ca3af;">
{unchanged} site(s) unchanged &middot; {len(diffs)} career pages monitored
</div>
</td></tr></table></td></tr></table></body></html>"""

    return body, has_changes, changes_summary

def _email_settings(config: dict) -> tuple[str, int, str, str, str]:
    """Resolve SMTP settings without ever logging credential values."""
    email_cfg = config.get("email", {})
    smtp_server = os.environ.get("SMTP_SERVER", email_cfg.get("smtp_server", "smtp.gmail.com"))
    smtp_port = int(os.environ.get("SMTP_PORT", email_cfg.get("smtp_port", 587)))
    sender_email = os.environ.get("SENDER_EMAIL", email_cfg.get("sender_email", ""))
    sender_password = os.environ.get("SENDER_PASSWORD", email_cfg.get("sender_password", ""))
    recipient_email = os.environ.get("RECIPIENT_EMAIL", email_cfg.get("recipient_email", ""))
    return smtp_server, smtp_port, sender_email, sender_password, recipient_email


def validate_email_config(config: dict) -> tuple[str, int, str, str, str]:
    """Fail early when required mail settings are missing."""
    settings = _email_settings(config)
    smtp_server, smtp_port, sender_email, sender_password, recipient_email = settings
    missing = [
        name
        for name, value in (
            ("SENDER_EMAIL", sender_email),
            ("SENDER_PASSWORD", sender_password),
            ("RECIPIENT_EMAIL", recipient_email),
        )
        if not value
    ]
    if missing:
        raise ValueError(f"Email configuration missing: {', '.join(missing)}")
    log.info(
        "Email configuration validated (SMTP %s:%s; sender, password and recipient present).",
        smtp_server,
        smtp_port,
    )
    return settings


def send_email(config: dict, subject: str, html_body: str):
    """Send an email and require positive SMTP acceptance.

    Email credentials can come from environment variables (for GitHub Actions)
    or from config.json (for local runs). Env vars take priority.
    """
    smtp_server, smtp_port, sender_email, sender_password, recipient_email = validate_email_config(config)

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = sender_email
    msg["To"] = recipient_email

    plain_soup = BeautifulSoup(html_body, "html.parser")
    for anchor in plain_soup.find_all("a", href=True):
        anchor.append(f" ({anchor['href']})")
    plain = plain_soup.get_text("\n", strip=True)
    msg.attach(MIMEText(plain, "plain"))
    msg.attach(MIMEText(html_body, "html"))

    log.info("Attempting SMTP delivery via %s:%s.", smtp_server, smtp_port)
    with smtplib.SMTP(smtp_server, smtp_port, timeout=30) as server:
        server.ehlo()
        server.starttls(context=ssl.create_default_context())
        server.ehlo()
        server.login(sender_email, sender_password)
        refused = server.sendmail(sender_email, recipient_email, msg.as_string())

    if refused:
        raise RuntimeError(f"SMTP server refused {len(refused)} recipient(s)")
    log.info("Email delivery accepted by SMTP server.")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def scan_site_worker(site_cfg: dict) -> SiteResult:
    """Own all browser operations in a disposable child process."""
    name, site_url = site_cfg["name"], site_cfg["url"]
    try:
        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless=True)
            context = browser.new_context(
                user_agent="Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
                locale="de-CH",
            )
            if is_linkedin_url(site_url):
                if not load_linkedin_cookies(context) or not verify_linkedin_session(context):
                    raise ValueError("LinkedIn authentication unavailable; previous results retained")
            site_cfg = dict(site_cfg)
            site_cfg["_deadline"] = time.monotonic() + float(site_cfg.get("_soft_timeout", 160))
            crawl = crawl_careers(context, site_cfg, fetch_page, site_cfg["_deadline"])
            result = SiteResult(name=name, url=site_url, coverage_complete=not crawl.warnings)
            result.warnings.extend(crawl.warnings)
            unique, hashes = {}, []
            zero_signals = []
            for document in crawl.documents:
                parsed = extract_jobs_from_page(document["html"], site_cfg, document["url"])
                unique.update({job.key: job for job in parsed.jobs})
                hashes.append(parsed.page_hash)
                zero_signals.append(parsed.has_no_jobs_indicator)
                if parsed.error:
                    result.warnings.append(parsed.error)
                # Public career-page evidence only; never browser cookies or credentials.
                evidence_soup = BeautifulSoup(document["html"], "lxml")
                for tag in evidence_soup.select("script, style, nav, footer, header, input"):
                    tag.decompose()
                result.evidence.append({"url": document["url"],
                    "text": evidence_soup.get_text(" ", strip=True)[:18000],
                    "links": [{"title": a.get_text(" ", strip=True)[:180],
                               "url": canonical_url(a["href"], document["url"])}
                              for a in evidence_soup.select("a[href]")][:300]})
            result.jobs = list(unique.values())
            result.page_hash = hashlib.sha256("".join(hashes).encode()).hexdigest()
            result.has_no_jobs_indicator = bool(zero_signals) and all(zero_signals) and not result.jobs
            if not result.jobs and not result.has_no_jobs_indicator:
                result.warnings.append("No verifiable vacancy links extracted; inspect the career page manually.")
            result.coverage = {"career_pages": crawl.visited, "listing_pages": len(crawl.documents),
                               "candidates": len(result.jobs)}

            # Filter LinkedIn jobs to PE-relevant roles only
            if is_linkedin_url(site_url) and result.jobs:
                before = len(result.jobs)
                result.jobs = [j for j in result.jobs if is_pe_relevant(j.title, j.detail)]
                filtered = before - len(result.jobs)
                if filtered:
                    log.info(f"  [{name}] Filtered out {filtered} non-PE jobs, kept {len(result.jobs)}")

            validate_jobs(result, site_cfg, context)
            result.coverage_complete = result.coverage_complete and not result.warnings
            result.coverage.update({"status": "checked" if result.coverage_complete else "needs_review",
                                    "verified": len(result.jobs), "unverified": len(result.unverified_urls)})
            browser.close()
        return result
    except Exception as exc:
        return SiteResult(name=name, url=site_url, error=str(exc))


def scan_site(site_cfg: dict, timeout: float) -> SiteResult:
    """Include startup, extraction, verification and cleanup in one deadline."""
    try:
        with tempfile.TemporaryDirectory(prefix="job-monitor-site-") as folder:
            config_path = Path(folder) / "site.json"
            result_path = Path(folder) / "result.json"
            config_path.write_text(json.dumps(dict(site_cfg, _soft_timeout=max(1, timeout - 30))), encoding="utf-8")
            returncode = run_bounded(
                [sys.executable, str(Path(__file__).resolve()), "--scan-site-worker",
                 str(config_path), str(result_path)], timeout,
            )
            if returncode != 0:
                raise RuntimeError(f"Browser worker exited with status {returncode}")
            data = json.loads(result_path.read_text(encoding="utf-8"))
            data["jobs"] = [JobEntry(**job) for job in data["jobs"]]
            return SiteResult(**data)
    except subprocess.TimeoutExpired:
        error = f"Site scan exceeded {timeout:.0f}s; browser worker stopped; previous results retained"
    except Exception as exc:
        error = f"Site worker failed ({type(exc).__name__}); previous results retained"
    return SiteResult(name=site_cfg["name"], url=site_cfg["url"], error=error)


def scan_many(sites, default_timeout, scan_timeout, workers=1):
    """Browser processes stay isolated; results/state are handled in config order."""
    deadline = time.monotonic() + scan_timeout
    def run(site):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return SiteResult(name=site["name"], url=site["url"],
                              error="Not scanned: total scan time budget exhausted; previous results retained")
        timeout = float(site.get("site_timeout_seconds", default_timeout))
        if not math.isfinite(timeout) or timeout <= 0:
            return SiteResult(name=site["name"], url=site["url"], error="Invalid per-source timeout")
        return scan_site(site, min(timeout, remaining))
    if int(workers) == 1:
        for site in sites:
            yield site, run(site)
        return
    with ThreadPoolExecutor(max_workers=max(1, min(int(workers), 4))) as pool:
        yield from zip(sites, pool.map(run, sites))


def write_scan_audit(results):
    """Checkpoint evidence without advancing the notification/state baseline."""
    audit = [{"name": r.name, "url": r.url, "error": r.error, "warnings": r.warnings,
              "coverage_complete": r.coverage_complete and not r.error, "coverage": r.coverage,
              "verified_jobs": [{"title": j.title, "url": j.url, "location": j.location} for j in r.jobs]}
             for r in results]
    path = BASE_DIR / "scan_results.json"
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(audit, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(path)
    (BASE_DIR / "scan_evidence.json").write_text(json.dumps(
        [{"name": r.name, "documents": r.evidence} for r in results], ensure_ascii=False), encoding="utf-8")


def main():
    parser = argparse.ArgumentParser(description="Job Monitor - Track career pages for new postings")
    parser.add_argument("--dry-run", action="store_true", help="Read-only scan; no email, tracker writes or saved state changes")
    parser.add_argument("--reset", action="store_true", help="Clear saved state")
    parser.add_argument("--list", action="store_true", help="Show currently tracked jobs")
    parser.add_argument("--always-email", action="store_true", help="Send email even if no changes")
    parser.add_argument("--email-test", action="store_true", help="Send a fast SMTP test without scanning sites")
    parser.add_argument("--config", type=str, default=str(CONFIG_FILE), help="Path to config file")
    parser.add_argument("--scan-site-worker", nargs=2, help=argparse.SUPPRESS)
    args = parser.parse_args()
    if args.scan_site_worker:
        site_path, result_path = map(Path, args.scan_site_worker)
        result = scan_site_worker(json.loads(site_path.read_text(encoding="utf-8")))
        result_path.write_text(json.dumps(asdict(result)), encoding="utf-8")
        return
    if args.dry_run and (args.email_test or args.reset):
        parser.error("--dry-run cannot be combined with --email-test or --reset")

    config_path = Path(args.config)
    if not config_path.exists():
        log.error(f"Config file not found: {config_path}")
        sys.exit(1)

    config = json.loads(config_path.read_text(encoding="utf-8"))
    names = [s["name"] for s in config.get("sites", [])]
    if not names or len(set(names)) != len(names):
        parser.error("Configuration must contain sites with unique names")

    if args.email_test:
        test_timestamp = datetime.now().strftime("%d.%m.%Y %H:%M")
        test_body = (
            "<html><body><h2>Job Monitor email test</h2>"
            f"<p>SMTP delivery test completed at {test_timestamp}.</p>"
            "<p>No career sites were scanned and state.json was not changed.</p>"
            "</body></html>"
        )
        send_email(config, "Job Monitor - Email Test", test_body)
        log.info("Email test completed successfully.")
        return

    if args.reset:
        if STATE_FILE.exists():
            STATE_FILE.unlink()
            log.info("State file deleted. Next run will be treated as first scan.")
        else:
            log.info("No state file to delete.")
        return

    state = load_state()
    old_health = state.get("source_health", {})

    if args.list:
        if not state["sites"]:
            print("No sites tracked yet. Run the monitor first.")
            return
        for site_name, site_data in state["sites"].items():
            print(f"\n{'='*60}")
            print(f"  {site_name}")
            print(f"  URL: {site_data['url']}")
            print(f"  Last checked: {site_data.get('last_checked', 'never')}")
            jobs = site_data.get("job_keys", {})
            if jobs:
                print(f"  Jobs ({len(jobs)}):")
                for jdata in jobs.values():
                    print(f"    - {jdata['title']}")
                    if jdata.get("location"):
                        print(f"      Location: {jdata['location']}")
            elif site_data.get("has_no_jobs_indicator"):
                print("  No open positions listed.")
            else:
                print("  No structured jobs found (page monitored for changes).")
        print(f"\n{'='*60}")
        print(f"Last run: {state.get('last_run', 'never')}")
        return

    try:
        site_timeout = float(config.get("site_timeout_seconds", 180))
        scan_timeout = float(config.get("scan_timeout_seconds", 75 * 60))
        if not all(math.isfinite(value) and value > 0 for value in (site_timeout, scan_timeout)):
            raise ValueError
    except (TypeError, ValueError):
        parser.error("Scan timeouts must be finite positive numbers")

    if not args.dry_run:
        # Validate before spending up to 90 minutes scanning sites.
        validate_email_config(config)

    # --- Run the monitor ---
    log.info(f"Starting job monitor scan for {len(config['sites'])} sites...")

    results = []
    diffs = []
    feed_cfg = config.get("tracker_feed", {})
    feed_requested = not args.dry_run and feed_cfg.get("enabled", True)
    feed_enabled = feed_requested and google_sheets_available()
    feed_candidates: list[dict] = []
    linkedin_auth_failed = False

    for site_cfg, result in scan_many(config["sites"], site_timeout, scan_timeout, config.get("scan_workers", 1)):
        name, site_url = site_cfg["name"], site_cfg["url"]
        site_started = time.monotonic()
        log.info(f"Checking: {name} ({site_url})")
        if result.error:
            log.error(f"  [{name}] Error: {result.error}")
            if is_linkedin_url(site_url) and "LinkedIn authentication unavailable" in result.error:
                linkedin_auth_failed = True
        else:
            log.info(f"  [{name}] Verified {len(result.jobs)} job(s), no_jobs_indicator={result.has_no_jobs_indicator}")
            for warning in result.warnings:
                log.warning(f"  [{name}] {warning}")
            for job in result.jobs:
                log.info(f"    -> {job.title}")
        log.info(f"  [{name}] Scan result recorded")
        results.append(result)
        diff = compute_diff(result, state)
        diff.tier = site_cfg.get("tier", "")
        diffs.append(diff)

        # Queue new verified roles at eligible sources; the mandatory fit gate decides suitability.
        # Verified ad text comes back with the isolated worker result.
        if (
            feed_requested
            and not diff.is_first_run
            and site_feed_enabled(site_cfg, feed_cfg)
        ):
            fed_keys = state.get("tracker_fed", {})
            pending = state.setdefault("tracker_pending", {})
            new_keys = {j.key for j in diff.new_jobs}
            for job in result.jobs:
                if job.key not in new_keys and job.key not in pending:
                    continue
                if job.key in fed_keys or job.key in state.get("tracker_screened_out", {}):
                    continue
                pending[job.key] = {"company": name, "url": job.url, "title": job.title}
                description = result.descriptions.get(job.key, "")
                feed_candidates.append({"job": job, "site": diff, "description": description, "notes": site_cfg.get("notes", "")})

        # Update only the in-memory state; persist after successful delivery.
        if not result.error:
            update_state(state, result)

        write_scan_audit(results)

    tracker_fed_report: list[dict] = []
    tracker_error = ""
    tracker_errors = []
    screened_out_report = []
    if feed_candidates and feed_enabled:
        try:
            tracker_fed_report = feed_tracker(feed_candidates, state, errors=tracker_errors, screened_out=screened_out_report)
        except Exception as e:
            tracker_error = "CV pipeline transfer failed; eligible roles remain pending and will be retried after verification."
            if "invalid_grant" in str(e):
                tracker_error += " Google authorization must be renewed (invalid_grant)."
            log.error(
                f"Tracker feed failed — roles NOT queued in the sheet, but they are listed "
                f"in this email's new-roles section ({type(e).__name__})."
            )
    elif config.get("tracker_feed", {}).get("enabled", True) and not google_sheets_available():
        log.info("Tracker feed inactive: Google credentials / GOOGLE_DRIVE_CV_FOLDER_ID not configured.")
        if feed_requested and feed_candidates:
            tracker_error = "CV pipeline credentials are missing; verified eligible roles remain pending."
    elif feed_enabled:
        log.info("Tracker feed active; no new or pending verified roles at eligible sources.")

    tracker_error = " ".join(filter(None, [tracker_error, *dict.fromkeys(tracker_errors)]))
    for key in set(state.get("tracker_fed", {})) | set(state.get("tracker_screened_out", {})):
        state.get("tracker_pending", {}).pop(key, None)

    # --- Build and send report ---
    html_body, has_changes, changes_summary = build_email_html(
        diffs, linkedin_auth_failed=linkedin_auth_failed, tracker_fed=tracker_fed_report,
        tracker_error=tracker_error, tracker_screened_out=screened_out_report,
        tracker_pending=list(state.get("tracker_pending", {}).values()) if feed_requested else [],
    )

    health = {r.name: {"error": r.error, "warnings": sorted(set(r.warnings)),
                       "coverage_complete": r.coverage_complete and not bool(r.error)} for r in results}
    health_changed = health != old_health
    actionable = any(d.new_jobs or d.removed_jobs for d in diffs) or health_changed or bool(tracker_error or tracker_fed_report or screened_out_report)
    state["source_health"] = health

    # Determine if this is the first run
    is_first_run = any(d.is_first_run for d in diffs)

    if is_first_run:
        subject = "Job Monitor - Initial Scan Complete"
        log.info("First run completed. Baseline established.")
    elif has_changes:
        # Cap the subject at three companies so it stays scannable in the inbox
        summary = ", ".join(changes_summary[:3])
        if len(changes_summary) > 3:
            summary += f" +{len(changes_summary) - 3} more"
        subject = f"Job Monitor - Changes Detected: {summary}" if summary else "Job Monitor - Scan requires review"
        log.info(f"Changes detected: {summary or 'page-level changes'}")
    else:
        subject = "Job Monitor - No Changes"
        log.info("No changes detected.")

    # Keep auditable artifacts on successful runs as well as failures.
    report_path = BASE_DIR / "last_report.html"
    report_path.write_text(html_body, encoding="utf-8")
    write_scan_audit(results)
    if args.dry_run:
        # Save HTML report to file for inspection
        report_path = BASE_DIR / "last_report.html"
        report_path.write_text(html_body, encoding="utf-8")
        log.info(f"Dry run - report saved to {report_path}")
        print(f"\nDry run complete. Report saved to: {report_path}")
    else:
        if actionable or is_first_run or args.always_email:
            try:
                send_email(config, subject, html_body)
            except Exception as e:
                log.error(f"Failed to send email: {e}")
                # Preserve the report for local debugging, then fail the Actions job.
                report_path = BASE_DIR / "last_report.html"
                report_path.write_text(html_body, encoding="utf-8")
                log.info(f"Report saved locally to {report_path}")
                raise RuntimeError("Email delivery failed; state was not committed.") from e
        else:
            log.info("No changes - email not sent (use --always-email to override)")
        save_state(state)


if __name__ == "__main__":
    main()

