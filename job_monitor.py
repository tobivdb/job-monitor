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

# Patterns to always exclude (generic unsolicited application links)
DEFAULT_EXCLUDE = [
    "initiativbewerbung", "spontanbewerbung", "blindbewerbung",
    "unsolicited application", "open application",
]

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
        title = title.strip()
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
                title = heading.get_text(strip=True)
            else:
                title = el.get_text(strip=True)[:150]

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
        title = heading.get_text(strip=True)

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
                    title = heading.get_text(strip=True)
                else:
                    # Go up one more level
                    grandparent = parent.parent
                    if grandparent:
                        heading = grandparent.find(["h2", "h3", "h4", "h5", "strong"])
                        if heading:
                            title = heading.get_text(strip=True)
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
# Email notification
# ---------------------------------------------------------------------------

def _render_site_section(diff: SiteDiff) -> tuple[str, bool, Optional[str]]:
    """Render a single site section. Returns (html, has_job_changes, change_summary)."""
    section = f'<div class="site-section">'
    section += f'<h2><a href="{diff.url}">{diff.name}</a></h2>'

    if diff.error:
        section += f'<div class="error">Error checking this site: {diff.error}</div>'
        section += f'<p><a href="{diff.url}">Open career page &rarr;</a></p>'
        section += '</div>'
        return section, False, None

    if diff.is_first_run:
        section += '<p><span class="badge badge-first">FIRST SCAN</span> Baseline established.</p>'
        if diff.new_jobs:
            section += f"<p>Found {len(diff.new_jobs)} existing position(s):</p>"
            for job in diff.new_jobs:
                section += '<div class="new-job">'
                if job.url:
                    section += f'<div class="job-title"><a href="{job.url}">{job.title}</a></div>'
                else:
                    section += f'<div class="job-title">{job.title}</div>'
                if job.location:
                    section += f'<div class="job-detail">Location: {job.location}</div>'
                section += '</div>'
        elif diff.has_no_jobs_indicator:
            section += '<p class="no-change">No open positions currently listed.</p>'
        else:
            section += '<p class="no-change">No job listings found. Page will be monitored for changes.</p>'
        section += f'<p><a href="{diff.url}">Open career page &rarr;</a></p>'
        section += '</div>'
        return section, bool(diff.new_jobs), f"{diff.name}: {len(diff.new_jobs)} initial" if diff.new_jobs else None

    site_has_changes = False
    change_summary = None

    if diff.new_jobs:
        site_has_changes = True
        change_summary = f"{diff.name}: {len(diff.new_jobs)} new"
        section += f'<p><span class="badge badge-new">NEW</span> {len(diff.new_jobs)} new position(s):</p>'
        for job in diff.new_jobs:
            section += '<div class="new-job">'
            if job.url:
                section += f'<div class="job-title"><a href="{job.url}">{job.title}</a></div>'
            else:
                section += f'<div class="job-title">{job.title}</div>'
            if job.location:
                section += f'<div class="job-detail">Location: {job.location}</div>'
            if job.detail and job.detail != job.title:
                section += f'<div class="job-detail">{job.detail[:200]}</div>'
            section += '</div>'

    if diff.removed_jobs:
        site_has_changes = True
        section += f'<p><span class="badge badge-removed">REMOVED</span> {len(diff.removed_jobs)} position(s) no longer listed:</p>'
        for job in diff.removed_jobs:
            section += '<div class="removed-job">'
            section += f'<div class="job-title">{job.title}</div>'
            section += '</div>'

    if diff.page_changed and not site_has_changes:
        section += '<div class="page-changed">Page content changed (but no specific new job listings detected).</div>'

    if not site_has_changes and not diff.page_changed:
        section += '<p class="no-change">No changes detected.</p>'

    section += f'<p><a href="{diff.url}">Open career page &rarr;</a></p>'
    section += '</div>'

    return section, site_has_changes, change_summary


def build_email_html(diffs: list[SiteDiff]) -> tuple[str, bool, list[str]]:
    """Build a nicely formatted HTML email. Sites are sorted:
    1. Sites with new/removed jobs (top)
    2. Sites with page changes (middle)
    3. Sites with no changes (bottom)
    """
    timestamp = datetime.now().strftime("%d.%m.%Y %H:%M")

    html = f"""
    <html>
    <head>
        <style>
            body {{ font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif; color: #333; max-width: 700px; margin: 0 auto; padding: 20px; }}
            h1 {{ color: #1a5276; border-bottom: 2px solid #1a5276; padding-bottom: 10px; }}
            h2 {{ color: #2c3e50; margin-top: 30px; }}
            .site-section {{ background: #f8f9fa; border-left: 4px solid #3498db; padding: 15px; margin: 15px 0; border-radius: 4px; }}
            .new-job {{ background: #e8f5e9; border-left: 4px solid #27ae60; padding: 12px; margin: 10px 0; border-radius: 4px; }}
            .removed-job {{ background: #fce4ec; border-left: 4px solid #e74c3c; padding: 12px; margin: 10px 0; border-radius: 4px; }}
            .no-change {{ color: #888; font-style: italic; }}
            .page-changed {{ background: #fff3e0; border-left: 4px solid #ff9800; padding: 12px; margin: 10px 0; border-radius: 4px; }}
            .error {{ background: #ffebee; border-left: 4px solid #f44336; padding: 12px; margin: 10px 0; border-radius: 4px; }}
            a {{ color: #2980b9; }}
            .job-title {{ font-weight: bold; font-size: 1.1em; }}
            .job-detail {{ color: #666; font-size: 0.9em; margin-top: 5px; }}
            .footer {{ margin-top: 40px; padding-top: 15px; border-top: 1px solid #ddd; color: #999; font-size: 0.85em; }}
            .badge {{ display: inline-block; padding: 2px 8px; border-radius: 12px; font-size: 0.8em; font-weight: bold; }}
            .badge-new {{ background: #27ae60; color: white; }}
            .badge-removed {{ background: #e74c3c; color: white; }}
            .badge-first {{ background: #3498db; color: white; }}
        </style>
    </head>
    <body>
        <h1>Job Monitor Report</h1>
        <p>Scan completed: {timestamp}</p>
    """

    # Render all sections and categorize them
    sections_with_jobs = []     # new/removed jobs detected
    sections_page_changed = []  # page changed but no specific jobs
    sections_no_change = []     # nothing changed
    sections_error = []         # errors
    sections_first_run = []     # first scan

    has_changes = False
    changes_summary = []

    for diff in diffs:
        section_html, has_job_changes, summary = _render_site_section(diff)

        if diff.error:
            sections_error.append(section_html)
        elif diff.is_first_run:
            sections_first_run.append(section_html)
            if summary:
                changes_summary.append(summary)
        elif has_job_changes:
            has_changes = True
            sections_with_jobs.append(section_html)
            if summary:
                changes_summary.append(summary)
        elif diff.page_changed:
            has_changes = True
            sections_page_changed.append(section_html)
        else:
            sections_no_change.append(section_html)

    # Output in priority order: jobs → page changes → first runs → no changes → errors
    for section in sections_with_jobs + sections_page_changed + sections_first_run + sections_no_change + sections_error:
        html += section

    html += f"""
        <div class="footer">
            <p>Job Monitor | Monitoring {len(diffs)} career pages</p>
        </div>
    </body>
    </html>
    """

    return html, has_changes, changes_summary


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

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        context = browser.new_context(
            user_agent="Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            locale="de-CH",
        )

        for site_cfg in config["sites"]:
            name = site_cfg["name"]
            site_url = site_cfg["url"]
            log.info(f"Checking: {name} ({site_url})")

            try:
                html = fetch_page(site_url, context)
                result = extract_jobs_from_page(html, site_cfg)
                log.info(f"  [{name}] Found {len(result.jobs)} job(s), no_jobs_indicator={result.has_no_jobs_indicator}")
                for j in result.jobs:
                    log.info(f"    -> {j.title}")
            except Exception as e:
                log.error(f"  [{name}] Error: {e}")
                result = SiteResult(name=name, url=site_url, error=str(e))

            results.append(result)
            diff = compute_diff(result, state)
            diffs.append(diff)

            # Update state immediately
            if not result.error:
                update_state(state, result)

        browser.close()

    save_state(state)

    # --- Build and send report ---
    html_body, has_changes, changes_summary = build_email_html(diffs)

    # Determine if this is the first run
    is_first_run = any(d.is_first_run for d in diffs)

    if is_first_run:
        subject = "Job Monitor - Initial Scan Complete"
        log.info("First run completed. Baseline established.")
    elif has_changes:
        summary = ", ".join(changes_summary)
        subject = f"Job Monitor - Changes Detected: {summary}"
        log.info(f"Changes detected: {summary}")
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
