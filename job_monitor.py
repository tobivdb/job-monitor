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
import sys
import re
from pathlib import Path
from datetime import datetime
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from urllib.parse import urljoin
from dataclasses import dataclass, field
from typing import Optional

from playwright.sync_api import sync_playwright
from bs4 import BeautifulSoup

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

BASE_DIR = Path(__file__).parent
CONFIG_FILE = BASE_DIR / "config.json"
STATE_FILE = BASE_DIR / "state.json"
LOG_FILE = BASE_DIR / "job_monitor.log"

# Patterns to always exclude (generic unsolicited application links and irrelevant roles)
DEFAULT_EXCLUDE = [
    "initiativbewerbung", "spontanbewerbung", "blindbewerbung",
    "unsolicited application", "open application",
    "werkstudent", "praktikant", "praktikum", "internship", "intern ",
    "fund controller", "marketing",
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

# Keywords that trigger the per-company summary in email notifications
EMAIL_SUMMARY_KEYWORDS = re.compile(
    r"(private\s+equity|associate|investment\s+manager)",
    re.IGNORECASE,
)

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
    r"Deutschland|Germany|Österreich|Austria)",
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

    @property
    def key(self) -> str:
        """Stable identifier for deduplication."""
        raw = f"{self.title.strip().lower()}|{self.url.strip().lower()}"
        return hashlib.md5(raw.encode()).hexdigest()


@dataclass
class SiteResult:
    name: str
    url: str
    jobs: list[JobEntry] = field(default_factory=list)
    page_hash: str = ""
    error: Optional[str] = None
    has_no_jobs_indicator: bool = False


# ---------------------------------------------------------------------------
# Page fetching (Playwright - handles JS-rendered pages)
# ---------------------------------------------------------------------------

def is_linkedin_url(url: str) -> bool:
    """Check if a URL is a LinkedIn page."""
    return "linkedin.com" in url.lower()


def is_pe_relevant(title: str, detail: str = "") -> bool:
    """Check if a job title/detail is relevant to Private Equity roles."""
    text = f"{title} {detail}"
    return bool(PE_RELEVANT_KEYWORDS.search(text))


def load_linkedin_cookies(context) -> bool:
    """Load LinkedIn session cookies into the browser context.
    Cookies come from LINKEDIN_COOKIES env var (JSON string) or linkedin_cookies.json file.
    Returns True if cookies were loaded successfully."""
    cookies_json = os.environ.get("LINKEDIN_COOKIES", "")

    if not cookies_json:
        cookies_file = BASE_DIR / "linkedin_cookies.json"
        if cookies_file.exists():
            cookies_json = cookies_file.read_text()
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
    except (json.JSONDecodeError, Exception) as e:
        log.warning(f"Failed to load LinkedIn cookies: {e}")
        return False


def verify_linkedin_session(context) -> bool:
    """Verify that the loaded cookies give us an authenticated LinkedIn session."""
    page = context.new_page()
    try:
        page.goto("https://www.linkedin.com/feed/", wait_until="domcontentloaded", timeout=30000)
        page.wait_for_timeout(3000)
        current_url = page.url
        if "login" in current_url or "authwall" in current_url:
            log.warning("LinkedIn cookies expired or invalid — redirected to login.")
            return False
        log.info(f"LinkedIn session verified (at: {current_url})")
        return True
    except Exception as e:
        log.warning(f"LinkedIn session verification failed: {e}")
        return False
    finally:
        page.close()


def fetch_page(url: str, playwright_context, timeout: int = 30000) -> str:
    """Fetch a page using Playwright (headless Chromium). Returns rendered HTML."""
    page = playwright_context.new_page()
    try:
        # Try networkidle first, fall back to domcontentloaded on timeout
        try:
            page.goto(url, wait_until="networkidle", timeout=timeout)
        except Exception:
            log.info(f"  networkidle timed out for {url}, retrying with domcontentloaded...")
            page.goto(url, wait_until="domcontentloaded", timeout=timeout)
            page.wait_for_timeout(5000)  # Give JS time to render

        # Extra wait for lazy-loaded content
        page.wait_for_timeout(2000)

        # Check if LinkedIn redirected to login wall
        if is_linkedin_url(url) and "login" in page.url.lower():
            log.warning(f"  LinkedIn login wall hit for {url}")
            raise Exception("LinkedIn login wall - not authenticated")

        html = page.content()
        return html
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
    if "|" in t and len(t.split("|")) >= 2:
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
        if not any(ind in t for ind in job_indicators):
            return True

    return False


def looks_like_job_title(title: str) -> bool:
    """Heuristic: does this text look like a job title?"""
    t = title.strip().lower()

    # Must have reasonable length
    if len(t) < 5 or len(t) > 200:
        return False

    # Strong positive signals
    job_indicators = [
        r"manager", r"analyst", r"specialist", r"director", r"associate",
        r"engineer", r"developer", r"consultant", r"controller", r"accountant",
        r"assistant", r"coordinator", r"intern\b", r"werkstudent", r"praktikant",
        r"senior", r"junior", r"head\s+of", r"chief", r"lead\b", r"officer",
        r"\(w/m/d\)", r"\(m/w/d\)", r"\(m/f/d\)", r"\(w/m\)", r"\(m/w\)",
        r"\d+\s*%",  # percentage (e.g., 80%)
        r"vollzeit", r"teilzeit", r"full[\s-]?time", r"part[\s-]?time",
        r"investment", r"portfolio", r"finance", r"financial", r"accounting",
        r"marketing", r"sales", r"operations", r"hr\b", r"human\s+resources",
        r"it\b", r"software", r"data", r"legal", r"compliance",
        r"real\s+estate", r"immobilien", r"hypothek",
    ]

    for pattern in job_indicators:
        if re.search(pattern, t, re.IGNORECASE):
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

def extract_jobs_from_page(html: str, site_config: dict) -> SiteResult:
    """
    Extract jobs using multiple strategies:
    1. Check for 'no jobs' indicators → early return
    2. CSS-class based extraction (elements with job/career/stelle in class)
    3. Heading-based extraction (h2/h3/h4 that look like job titles)
    4. Link-based extraction (links containing "bewerben", "apply", job-like paths)
    5. Fallback: page content hash for raw change detection
    """
    soup = BeautifulSoup(html, "lxml")
    name = site_config["name"]
    url = site_config["url"]

    clean_soup(soup)

    # Get body text for hashing and indicator checks
    body = soup.find("body")
    if not body:
        body = soup
    body_text = body.get_text(separator="\n", strip=True)
    normalized = normalize_text_for_hash(body_text)
    page_hash = hashlib.sha256(normalized.encode()).hexdigest()

    result = SiteResult(name=name, url=url, page_hash=page_hash)

    # --- Strategy 1: Check for "no jobs" indicators ---
    no_jobs = site_config.get("no_jobs_indicators", [])
    for indicator in no_jobs:
        if indicator.lower() in body_text.lower():
            result.has_no_jobs_indicator = True
            log.info(f"  [{name}] No-jobs indicator found: '{indicator}'")
            return result

    exclude_patterns = [p.lower() for p in site_config.get("exclude_patterns", [])] + [
        p.lower() for p in DEFAULT_EXCLUDE
    ]

    def is_excluded(text: str) -> bool:
        return any(excl in text.lower() for excl in exclude_patterns)

    jobs_found: dict[str, JobEntry] = {}  # key -> JobEntry

    def add_job(title: str, link: str = "", location: str = "", detail: str = ""):
        title = clean_title(title)
        if not title or is_noise_title(title) or is_excluded(title):
            return
        if not looks_like_job_title(title):
            return
        # Sanitize all text fields to prevent HTML injection in emails
        title = html_module.escape(title)
        location = html_module.escape(location)
        detail = html_module.escape(detail[:300])
        job = JobEntry(title=title, url=link, location=location, detail=detail)
        if job.key not in jobs_found:
            jobs_found[job.key] = job

    # --- Strategy 2: CSS-class based extraction ---
    job_class_selectors = [
        "[class*='job']", "[class*='Job']",
        "[class*='career']", "[class*='Career']",
        "[class*='stelle']", "[class*='Stelle']",
        "[class*='vacancy']", "[class*='Vacancy']",
        "[class*='position']", "[class*='Position']",
        "[class*='opening']", "[class*='Opening']",
    ]
    for sel in job_class_selectors:
        try:
            elements = body.select(sel)
        except Exception:
            continue
        for el in elements:
            # Look for a heading or strong text inside
            heading = el.find(["h2", "h3", "h4", "h5", "strong"])
            if heading:
                title = heading.get_text(separator=" ", strip=True)
            else:
                title = el.get_text(separator=" ", strip=True)[:150]

            link = ""
            link_el = el.find("a", href=True)
            if link_el:
                href = link_el["href"]
                link = urljoin(url, href) if not href.startswith("http") else href

            el_text = el.get_text(separator=" ", strip=True)
            location = extract_location(el_text)

            add_job(title, link, location, el_text)

    # --- Strategy 3: Heading-based extraction ---
    # Look for h2/h3/h4 that look like job titles
    for heading in body.find_all(["h2", "h3", "h4"]):
        title = heading.get_text(separator=" ", strip=True)

        # Check the parent or sibling for more context
        parent = heading.parent
        if parent:
            parent_text = parent.get_text(separator=" ", strip=True)
            location = extract_location(parent_text)
        else:
            parent_text = ""
            location = ""

        # Try to find a link nearby
        link = ""
        link_el = heading.find("a", href=True)
        if not link_el and parent:
            link_el = parent.find("a", href=True)
        if link_el:
            href = link_el["href"]
            link = urljoin(url, href) if not href.startswith("http") else href

        add_job(title, link, location, parent_text)

    # --- Strategy 4: Link-based extraction ---
    # Links with "bewerben", "apply", or paths containing "job", "stelle", "career"
    apply_links = body.find_all("a", href=True)
    for link_el in apply_links:
        href = link_el["href"]
        link_text = link_el.get_text(strip=True)
        full_url = urljoin(url, href) if not href.startswith("http") else href

        # Check if the link itself or its href suggests a job posting
        is_apply_link = any(kw in link_text.lower() for kw in
                           ["bewerben", "apply", "mehr erfahren", "details", "zur stelle"])
        is_job_url = any(kw in href.lower() for kw in
                         ["/job", "/stelle", "/career", "/position", "/vacancy"])

        if is_apply_link or is_job_url:
            # The job title is likely in a sibling or parent element
            parent = link_el.parent
            if parent:
                # Look for a heading in the same container
                heading = parent.find(["h2", "h3", "h4", "h5", "strong"])
                if heading and heading != link_el:
                    title = heading.get_text(separator=" ", strip=True)
                else:
                    # Go up one more level
                    grandparent = parent.parent
                    if grandparent:
                        heading = grandparent.find(["h2", "h3", "h4", "h5", "strong"])
                        if heading:
                            title = heading.get_text(separator=" ", strip=True)
                        else:
                            title = link_text
                    else:
                        title = link_text
            else:
                title = link_text

            parent_text = parent.get_text(separator=" ", strip=True) if parent else ""
            location = extract_location(parent_text)

            add_job(title, full_url, location, parent_text)

    result.jobs = list(jobs_found.values())
    return result


# ---------------------------------------------------------------------------
# State management
# ---------------------------------------------------------------------------

def load_state() -> dict:
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text())
    return {"sites": {}, "last_run": None}


def save_state(state: dict):
    state["last_run"] = datetime.now().isoformat()
    STATE_FILE.write_text(json.dumps(state, indent=2, ensure_ascii=False))


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


def tier_rank(tier: str) -> int:
    """Sort key: A first, unmapped last."""
    order = {"A": 0, "B": 1, "C": 2, "D": 3, "E": 4, "ZH": 5, "EU": 6}
    return order.get((tier or "").upper(), 9)


def compute_diff(result: SiteResult, state: dict) -> SiteDiff:
    site_state = state["sites"].get(result.name, {})
    diff = SiteDiff(name=result.name, url=result.url)

    if result.error:
        diff.error = result.error
        return diff

    diff.has_no_jobs_indicator = result.has_no_jobs_indicator

    if not site_state:
        diff.is_first_run = True
        diff.new_jobs = result.jobs
        return diff

    old_keys = set(site_state.get("job_keys", {}).keys())
    new_keys = {j.key: j for j in result.jobs}

    for key, job in new_keys.items():
        if key not in old_keys:
            diff.new_jobs.append(job)

    old_jobs_data = site_state.get("job_keys", {})
    for key in old_keys:
        if key not in new_keys:
            data = old_jobs_data[key]
            diff.removed_jobs.append(JobEntry(**data))

    # Also check page-level change
    old_hash = site_state.get("page_hash", "")
    if old_hash and old_hash != result.page_hash:
        diff.page_changed = True

    return diff


def update_state(state: dict, result: SiteResult):
    state["sites"][result.name] = {
        "url": result.url,
        "page_hash": result.page_hash,
        "job_keys": {
            j.key: {"title": j.title, "url": j.url, "location": j.location, "detail": j.detail}
            for j in result.jobs
        },
        "last_checked": datetime.now().isoformat(),
        "has_no_jobs_indicator": result.has_no_jobs_indicator,
    }


# ---------------------------------------------------------------------------
# CV-pipeline feed (Job Ad Overview V2 Google Sheet)
# ---------------------------------------------------------------------------

TRACKER_SHEET_NAME = "Job Ad Overview V2"
TRACKER_COLUMNS = 9  # Website..Claude Comment, must match the CV pipeline's CSV_FIELDS


def google_sheets_available() -> bool:
    """Feed is active only when the CV pipeline's Google credentials are configured."""
    has_oauth = bool(os.environ.get("GOOGLE_OAUTH_CLIENT_JSON") and os.environ.get("GOOGLE_OAUTH_REFRESH_TOKEN"))
    has_sa = bool(os.environ.get("GOOGLE_SERVICE_ACCOUNT_JSON"))
    return bool(os.environ.get("GOOGLE_DRIVE_CV_FOLDER_ID")) and (has_oauth or has_sa)


def _google_services():
    """Build Drive + Sheets clients from the same env credentials the CV pipeline uses."""
    from google.oauth2 import service_account
    from google.oauth2.credentials import Credentials
    from googleapiclient.discovery import build

    scopes = ["https://www.googleapis.com/auth/drive"]
    client_json = os.environ.get("GOOGLE_OAUTH_CLIENT_JSON")
    refresh_token = os.environ.get("GOOGLE_OAUTH_REFRESH_TOKEN")
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
    return files[0]["id"]


def scrape_job_description(job: JobEntry, site_url: str, context) -> str:
    """Fetch the job posting page and return its readable text (the CV pipeline's Description)."""
    target = job.url or site_url
    try:
        html = fetch_page(target, context)
    except Exception as e:
        log.warning(f"Tracker feed: could not fetch job page {target}: {e}")
        return ""
    soup = BeautifulSoup(html, "html.parser")
    for tag in soup(["script", "style", "nav", "header", "footer", "noscript"]):
        tag.decompose()
    text = re.sub(r"\n{3,}", "\n\n", soup.get_text(separator="\n")).strip()
    return text[:12000]


def feed_tracker(candidates: list[dict], state: dict) -> list[dict]:
    """Append newly found investment-team roles to the Job Ad Overview V2 sheet.

    Each candidate: {"job": JobEntry, "site": SiteDiff-like, "description": str}.
    Rows with a substantive scraped ad get an empty Status so the nightly CV run picks
    them up; thin scrapes are parked as NEEDS_REVIEW so the pipeline never generates an
    application from a garbage description. Fed job keys are remembered in state.json.
    Returns a report list for the email: {"company", "title", "status"}.
    """
    if not candidates:
        return []
    min_chars = 800
    fed_state = state.setdefault("tracker_fed", {})
    drive, sheets = _google_services()
    sheet_id = _find_tracker_sheet_id(drive)

    stamp = datetime.now().strftime("%Y-%m-%d %H:%M")
    report = []
    rows = []
    for cand in candidates:
        job, site, desc = cand["job"], cand["site"], cand["description"]
        substantive = len(desc) >= min_chars
        description = desc if desc else (
            f"{job.title} at {site.name}. (Job ad text could not be scraped automatically - "
            f"open {job.url or site.url} and paste the ad here.)"
        )
        status = "" if substantive else "NEEDS_REVIEW"
        rows.append([
            job.url or site.url,            # Website
            description,                    # Description
            site.name,                      # Company (pipeline refines)
            job.title,                      # Job Position (pipeline refines)
            status,                         # Status
            "",                             # Date (pipeline stamps on processing)
            "",                             # My Comment (operator-only)
            "",                             # Fit Probability
            f"Auto-added by job-monitor on {stamp} from {site.name} ({site.url}).",
        ])
        report.append({"company": site.name, "title": job.title, "key": job.key,
                       "status": "queued" if substantive else "needs_review"})

    sheets.spreadsheets().values().append(
        spreadsheetId=sheet_id,
        range="A1",
        valueInputOption="RAW",
        insertDataOption="INSERT_ROWS",
        body={"values": rows},
    ).execute()
    # Mark as fed only after the append succeeded, so a failed append never
    # poisons the dedup state.
    for cand, rep in zip(candidates, report):
        fed_state[cand["job"].key] = {"title": rep["title"], "company": rep["company"], "date": stamp}
    log.info(f"Tracker feed: appended {len(rows)} row(s) to '{TRACKER_SHEET_NAME}'.")
    return report


# ---------------------------------------------------------------------------
# Email notification
# ---------------------------------------------------------------------------

def _job_card(job: JobEntry, company: str, company_url: str, accent: str = "#166534", bg: str = "#f0fdf4", tier: str = "") -> str:
    """One compact job card, fully inline-styled (Gmail-safe)."""
    title_html = f'<a href="{job.url}" style="color:#111827;text-decoration:none;">{job.title}</a>' if job.url else job.title
    tier_badge = ""
    if (tier or "").upper() in ("A", "B"):
        tier_badge = (
            f'<span style="display:inline-block;margin-left:6px;padding:1px 7px;border-radius:999px;'
            f'background:#fef3c7;color:#92400e;font-size:11px;line-height:15px;font-weight:700;'
            f'vertical-align:middle;">Tier {tier.upper()}</span>'
        )
    meta_bits = [f'<a href="{company_url}" style="color:#6b7280;text-decoration:none;">{company}</a>']
    if job.location:
        meta_bits.append(job.location)
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

    has_changes = bool(pe_new or other_new or removed or page_changed)
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
  <div style="font-size:20px;line-height:26px;font-weight:700;margin-top:2px;">{n_pe} investment-team role(s), {n_other} other new role(s)</div>
</td></tr>
<tr><td style="background:#ffffff;border:1px solid #e5e7eb;border-top:none;border-radius:0 0 8px 8px;padding:18px 22px;">
<table role="presentation" width="100%" cellspacing="6" cellpadding="0"><tr>
{chip(n_pe, "Investment team", "#166534")}
{chip(n_other, "Other new", "#1e40af")}
{chip(n_removed, "Removed / filled", "#92400e")}
{chip(len(page_changed), "Pages changed", "#6b7280")}
{chip(len(errors), "Errors", "#991b1b" if errors else "#6b7280")}
</tr></table>
"""

    if linkedin_auth_failed:
        body += (
            '<div style="margin:14px 0 0 0;padding:11px 14px;border-left:4px solid #d97706;'
            'background:#fffbeb;border-radius:4px;font-size:13px;line-height:19px;color:#92400e;">'
            '<strong>LinkedIn session expired or missing.</strong> LinkedIn job boards were scanned '
            'without authentication and may show limited or no content. Re-run '
            '<code>export_linkedin_cookies.py</code> locally and update the <code>LINKEDIN_COOKIES</code> '
            'GitHub secret.</div>'
        )

    if pe_new:
        body += _section_header("New investment-team roles", n_pe, "#166534")
        for diff, jobs in pe_new:
            for job in jobs:
                body += _job_card(job, diff.name, diff.url, accent="#166534", bg="#f0fdf4", tier=diff.tier)

    if other_new:
        body += _section_header("Other new roles", n_other, "#1e40af")
        for diff, jobs in other_new:
            for job in jobs:
                body += _job_card(job, diff.name, diff.url, accent="#1e40af", bg="#eff6ff", tier=diff.tier)

    if tracker_fed:
        body += _section_header("Queued for the CV pipeline", len(tracker_fed), "#6d28d9")
        body += _compact_line_list([
            f'{f["company"]} &mdash; {f["title"]} '
            f'<span style="color:#9ca3af;">({"ready for tonight&#39;s run" if f["status"] == "queued" else "added as NEEDS_REVIEW - ad text too thin"})</span>'
            for f in tracker_fed
        ])

    if removed:
        body += _section_header("Removed or filled since last scan", n_removed, "#92400e")
        body += _compact_line_list([
            f'<a href="{d.url}" style="color:#374151;">{d.name}</a> &mdash; {j.title}'
            for d, jobs in removed for j in jobs
        ])

    if page_changed:
        body += _section_header("Page changed, no structured roles detected", len(page_changed), "#6b7280")
        body += _compact_line_list([
            f'<a href="{d.url}" style="color:#374151;">{d.name}</a>' for d in page_changed
        ])

    if first_runs:
        body += _section_header("First scan (baseline established)", len(first_runs), "#6b7280")
        body += _compact_line_list([
            f'<a href="{d.url}" style="color:#374151;">{d.name}</a>'
            + (f' &mdash; {len(d.new_jobs)} existing role(s) recorded' if d.new_jobs else '')
            for d in first_runs
        ])

    if errors:
        body += _section_header("Errors", len(errors), "#991b1b")
        body += _compact_line_list([
            f'<a href="{d.url}" style="color:#991b1b;">{d.name}</a> &mdash; '
            f'<span style="color:#6b7280;">{html_module.escape((d.error or "")[:120])}</span>'
            for d in errors
        ])

    body += f"""
<div style="margin-top:20px;padding-top:12px;border-top:1px solid #e5e7eb;font-size:12px;line-height:17px;color:#9ca3af;">
{unchanged} site(s) unchanged &middot; {len(diffs)} career pages monitored
</div>
</td></tr></table></td></tr></table></body></html>"""

    return body, has_changes, changes_summary

def send_email(config: dict, subject: str, html_body: str):
    """Send email notification via SMTP.

    Email credentials can come from environment variables (for GitHub Actions)
    or from config.json (for local runs). Env vars take priority.
    """
    email_cfg = config.get("email", {})

    smtp_server = os.environ.get("SMTP_SERVER", email_cfg.get("smtp_server", "smtp.gmail.com"))
    smtp_port = int(os.environ.get("SMTP_PORT", email_cfg.get("smtp_port", 587)))
    sender_email = os.environ.get("SENDER_EMAIL", email_cfg.get("sender_email", ""))
    sender_password = os.environ.get("SENDER_PASSWORD", email_cfg.get("sender_password", ""))
    recipient_email = os.environ.get("RECIPIENT_EMAIL", email_cfg.get("recipient_email", ""))

    if not sender_email or not sender_password or not recipient_email:
        raise ValueError("Email credentials not configured. Set env vars or config.json.")

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = sender_email
    msg["To"] = recipient_email

    # Plain text fallback
    plain = "Job Monitor has detected changes. View this email in HTML for details."
    msg.attach(MIMEText(plain, "plain"))
    msg.attach(MIMEText(html_body, "html"))

    with smtplib.SMTP(smtp_server, smtp_port) as server:
        server.starttls()
        server.login(sender_email, sender_password)
        server.sendmail(sender_email, recipient_email, msg.as_string())
    log.info(f"Email sent to {recipient_email}")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="Job Monitor - Track career pages for new postings")
    parser.add_argument("--dry-run", action="store_true", help="Run without sending emails")
    parser.add_argument("--reset", action="store_true", help="Clear saved state")
    parser.add_argument("--list", action="store_true", help="Show currently tracked jobs")
    parser.add_argument("--always-email", action="store_true", help="Send email even if no changes")
    parser.add_argument("--config", type=str, default=str(CONFIG_FILE), help="Path to config file")
    args = parser.parse_args()

    config_path = Path(args.config)
    if not config_path.exists():
        log.error(f"Config file not found: {config_path}")
        sys.exit(1)

    config = json.loads(config_path.read_text())

    if args.reset:
        if STATE_FILE.exists():
            STATE_FILE.unlink()
            log.info("State file deleted. Next run will be treated as first scan.")
        else:
            log.info("No state file to delete.")
        return

    state = load_state()

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

    # --- Run the monitor ---
    log.info(f"Starting job monitor scan for {len(config['sites'])} sites...")

    results = []
    diffs = []
    feed_cfg = config.get("tracker_feed", {})
    feed_enabled = feed_cfg.get("enabled", True) and google_sheets_available()
    feed_tiers = {t.upper() for t in feed_cfg.get("tiers", ["A", "B"])}
    feed_candidates: list[dict] = []
    linkedin_auth_failed = False

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        context = browser.new_context(
            user_agent="Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            locale="de-CH",
        )

        # Load LinkedIn cookies if any sites use LinkedIn URLs
        has_linkedin_sites = any(is_linkedin_url(s["url"]) for s in config["sites"])
        linkedin_ok = False
        if has_linkedin_sites:
            if load_linkedin_cookies(context):
                linkedin_ok = verify_linkedin_session(context)
            if not linkedin_ok:
                linkedin_auth_failed = True
                log.warning("LinkedIn auth failed — LinkedIn pages may show limited content.")

        for site_cfg in config["sites"]:
            name = site_cfg["name"]
            site_url = site_cfg["url"]
            log.info(f"Checking: {name} ({site_url})")

            try:
                html = fetch_page(site_url, context)
                result = extract_jobs_from_page(html, site_cfg)

                # Filter LinkedIn jobs to PE-relevant roles only
                if is_linkedin_url(site_url) and result.jobs:
                    before = len(result.jobs)
                    result.jobs = [j for j in result.jobs if is_pe_relevant(j.title, j.detail)]
                    filtered = before - len(result.jobs)
                    if filtered:
                        log.info(f"  [{name}] Filtered out {filtered} non-PE jobs, kept {len(result.jobs)}")

                log.info(f"  [{name}] Found {len(result.jobs)} job(s), no_jobs_indicator={result.has_no_jobs_indicator}")
                for j in result.jobs:
                    log.info(f"    -> {j.title}")
            except Exception as e:
                log.error(f"  [{name}] Error: {e}")
                result = SiteResult(name=name, url=site_url, error=str(e))

            results.append(result)
            diff = compute_diff(result, state)
            diff.tier = site_cfg.get("tier", "")
            diffs.append(diff)

            # Queue new investment-team roles at priority-tier funds for the CV pipeline.
            # Scraping the ad text must happen here, while the browser context is alive.
            if (
                feed_enabled
                and not diff.is_first_run
                and diff.tier.upper() in feed_tiers
            ):
                fed_keys = state.get("tracker_fed", {})
                for job in diff.new_jobs:
                    if job.key in fed_keys or not is_pe_relevant(job.title, job.detail):
                        continue
                    description = scrape_job_description(job, diff.url, context)
                    feed_candidates.append({"job": job, "site": diff, "description": description})

            # Update state immediately
            if not result.error:
                update_state(state, result)

        browser.close()

    tracker_fed_report: list[dict] = []
    if feed_candidates:
        try:
            tracker_fed_report = feed_tracker(feed_candidates, state)
        except Exception as e:
            log.error(
                f"Tracker feed failed — roles NOT queued in the sheet, but they are listed "
                f"in this email's new-roles section; add them manually if wanted: {e}"
            )
    elif config.get("tracker_feed", {}).get("enabled", True) and not google_sheets_available():
        log.info("Tracker feed inactive: Google credentials / GOOGLE_DRIVE_CV_FOLDER_ID not configured.")

    save_state(state)

    # --- Build and send report ---
    html_body, has_changes, changes_summary = build_email_html(
        diffs, linkedin_auth_failed=linkedin_auth_failed, tracker_fed=tracker_fed_report
    )

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
        subject = f"Job Monitor - Changes Detected: {summary}" if summary else "Job Monitor - Changes Detected"
        log.info(f"Changes detected: {summary or 'page-level changes'}")
    else:
        subject = "Job Monitor - No Changes"
        log.info("No changes detected.")

    if args.dry_run:
        # Save HTML report to file for inspection
        report_path = BASE_DIR / "last_report.html"
        report_path.write_text(html_body)
        log.info(f"Dry run - report saved to {report_path}")
        print(f"\nDry run complete. Report saved to: {report_path}")
    else:
        if has_changes or is_first_run or args.always_email:
            try:
                send_email(config, subject, html_body)
            except Exception as e:
                log.error(f"Failed to send email: {e}")
                # Save report locally as fallback
                report_path = BASE_DIR / "last_report.html"
                report_path.write_text(html_body)
                log.info(f"Report saved locally to {report_path}")
        else:
            log.info("No changes - email not sent (use --always-email to override)")


if __name__ == "__main__":
    main()
