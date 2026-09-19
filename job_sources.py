"""Conservative job evidence, URL resolution and detail-page verification.

Headings on team/career pages and search suggestions are not job postings.
Only structured JobPosting data or links to individual vacancies are candidates.
"""

import html
import json
import io
import re
from datetime import datetime, timezone
from urllib.parse import parse_qsl, urlencode, urljoin, urlsplit, urlunsplit

from bs4 import BeautifulSoup
from pypdf import PdfReader

EXTRACTOR_VERSION = 3
TRACKING_PARAMS = {
    "trk", "trackingid", "refid", "origin", "originalsubdomain", "source",
    "_s.crb", "browsertimezone", "selected_lang", "rcm_site_locale",
    "jobalertcontroller_jobalertid", "jobalertcontroller_jobalertname",
}
NON_JOB_TEXT = re.compile(
    r"(?:\b(?:talent|associate)\s+(?:pool|community)|speculative|unsolicited|"
    r"initiativbewerbung|spontanbewerbung|working\s+student|"
    r"\b(?:our|meet the|unser)\s+team\b|\b(?:career as|what our|was unsere)\b)", re.I
)
ROLE_WORDS = re.compile(
    r"\b(?:manager|analyst|specialist|director|associate|engineer|developer|"
    r"consultant|controller|accountant|assistant|coordinator|officer|principal|"
    r"partner|head|lead|professional|executive|investment|investments|"
    r"vice\s+president|vp|avp|counsel|paralegal|receptionist|architect|dealer|"
    r"chief|ceo|cfo|credit|finanzbuchhalter|"
    r"referent|buchhalter|assistenz|mitarbeiter|jurist|banquier|affaires)\b|"
    r"\([mfw][/\w -]+\)", re.I
)
CLOSED_TEXT = re.compile(
    r"(?:this (?:job|position|vacancy) (?:is no longer|has been)|"
    r"job (?:is no longer (?:available|open)|not found)|position has been filled|"
    r"no longer accepting applications|vacancy is (?:now )?closed|job is (?:now )?closed|stelle (?:ist nicht mehr|wurde besetzt)|"
    r"stellenangebot (?:ist nicht mehr|nicht gefunden)|vacancy has expired)", re.I
)
BLOCKED_TEXT = re.compile(
    r"(?:verify (?:that )?you are human|access denied|just a moment|"
    r"checking your browser|unusual traffic)", re.I
)


def is_linkedin(url):
    host = (urlsplit(url).hostname or "").lower()
    return host == "linkedin.com" or host.endswith(".linkedin.com")


def canonical_url(value, base=""):
    """Resolve against the final document URL; retain functional query fields."""
    value = html.unescape(str(value or "")).strip()
    if not value or value.startswith(("#", "[", "{")):
        return ""
    try:
        parts = urlsplit(urljoin(base, value))
        if parts.scheme not in {"http", "https"} or not parts.hostname or parts.username:
            return ""
        if is_linkedin(urlunsplit(parts)):
            match = re.search(r"/jobs/view/(?:[^/?]*-)?(\d+)/?$", parts.path)
            if match:
                return f"https://www.linkedin.com/jobs/view/{match.group(1)}/"
        query = [(k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True)
                 if k.lower() not in TRACKING_PARAMS and not k.lower().startswith("utm_")]
        return urlunsplit((parts.scheme, parts.netloc.lower(), parts.path,
                           urlencode(sorted(query)), ""))
    except ValueError:
        return ""


def vacancy_identity(url):
    value = canonical_url(url)
    parts = urlsplit(value)
    if re.search(r"\.jobs\.personio\.(de|com)$", parts.hostname or ""):
        host = re.sub(r"\.com$", ".de", parts.netloc)
        query = [(k,v) for k,v in parse_qsl(parts.query) if k != "language"]
        return urlunsplit((parts.scheme, host, parts.path.rstrip('/'), urlencode(query), ''))
    return value


def is_detail_url(url):
    if not canonical_url(url):
        return False
    parts = urlsplit(url)
    path = parts.path.lower().rstrip("/")
    query = dict(parse_qsl(parts.query))
    if is_linkedin(url):
        return bool(re.fullmatch(r"/jobs/view/\d+", path))
    if re.search(r"/(?:searchjobs|register)/?$", path):
        return False
    if any(query.get(k) for k in ("career_job_req_id", "gh_jid", "jobId", "jobid", "requisitionId", "vacancyNo")):
        return True
    if re.search(r"-j\d+\.html$", path):
        return True
    if path.endswith(".pdf"):
        return True
    return bool(re.search(
        r"/(?:jobs?|job-details|jobdetail|stellen?|stellenangebote?|karriere|careers?|vacanc(?:y|ies)|"
        r"positions?|o|p)/(?!(?:search|search-results|categories|locations|teams|"
        r"departments|page|all|open-positions)(?:/|$))[^/]+", path
    ))


def job_postings(soup):
    """Read JSON-LD before removing scripts, including nested @graph objects."""
    def walk(value):
        if isinstance(value, list):
            for item in value:
                yield from walk(item)
        elif isinstance(value, dict):
            types = value.get("@type", [])
            if types == "JobPosting" or isinstance(types, list) and "JobPosting" in types:
                yield value
            for key, item in value.items():
                if key != "@context" and isinstance(item, (list, dict)):
                    yield from walk(item)
    for script in soup.find_all("script", type="application/ld+json"):
        try:
            yield from walk(json.loads(script.string or script.get_text()))
        except (ValueError, TypeError):
            continue


def expired(posting):
    value = posting.get("validThrough")
    if not value:
        return False
    try:
        deadline = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if deadline.tzinfo is None:
            deadline = deadline.replace(tzinfo=timezone.utc)
        return deadline < datetime.now(timezone.utc)
    except ValueError:
        return False


def plain(value):
    return re.sub(r"\s+", " ", BeautifulSoup(str(value or ""), "html.parser").get_text(" ", strip=True)).strip()


def title_matches(title, text, location=""):
    if location:
        title = re.sub(r"[\s,|()\-]+" + re.escape(location) + r"\s*$", "", title, flags=re.I)
    # Some ATS listings append employment terms that the ad heading omits.
    title = re.sub(r"\s+-\s+(?:Permanent Contract|Fixed.term Contract)\b.*$", "", title, flags=re.I)
    def words(value):
        return re.findall(r"\w+", html.unescape(value).casefold())
    needle, haystack = words(title), " " + " ".join(words(text)) + " "
    return bool(needle) and " " + " ".join(needle) + " " in haystack


def company_slug(url):
    match = re.search(r"/company/([^/]+)", urlsplit(url).path)
    return match.group(1).casefold() if match else ""


def employer_matches(soup, config, posting=None):
    """LinkedIn recommendations are not scoped to the page's company."""
    if not is_linkedin(config["url"]):
        return True
    slug = company_slug(config["url"])
    if slug and any(company_slug(a.get("href", "")) == slug for a in soup.find_all("a", href=True)):
        return True
    org = (posting or {}).get("hiringOrganization", {})
    if isinstance(org, dict):
        if slug and company_slug(org.get("sameAs", "") or org.get("url", "")) == slug:
            return True
        expected = re.sub(r"\s*\(LinkedIn\)\s*", "", config["name"], flags=re.I)
        normalize = lambda s: re.sub(r"[^\w]", "", s.casefold())
        return bool(org.get("name")) and normalize(org["name"]) == normalize(expected)
    return False


def candidates(soup, config, final_url):
    """Yield (title, URL, location, context) with individual-vacancy evidence."""
    base_tag = soup.find("base", href=True)
    base = canonical_url(base_tag["href"], final_url) if base_tag else final_url
    for posting in job_postings(soup):
        if expired(posting) or not employer_matches(BeautifulSoup("", "html.parser"), config, posting):
            continue
        link = canonical_url(posting.get("url"), base)
        # A JobPosting can use a nonstandard URL, but it must identify a page.
        if not link or link == canonical_url(final_url) and not is_detail_url(link):
            continue
        location = posting.get("jobLocation", [])
        if isinstance(location, dict):
            location = [location]
        locations = []
        for loc in location if isinstance(location, list) else []:
            address = loc.get("address", {}) if isinstance(loc, dict) else {}
            if isinstance(address, dict):
                locations.extend(str(address.get(k, "")) for k in ("addressLocality", "addressCountry"))
        yield plain(posting.get("title")), link, " ".join(locations).strip(), plain(posting.get("description"))

    if urlsplit(final_url).path in config.get("self_posting_paths", []) or config.get("inline_vacancies"):
        headings = soup.select("h1")
        if config.get("inline_vacancies"):
            headings = soup.select(config["inline_vacancies"])
        for heading in headings:
            title = heading.get_text(" ", strip=True)
            if ROLE_WORDS.search(title) and not NON_JOB_TEXT.search(title):
                yield title, canonical_url(final_url), "", heading.parent.get_text(" ", strip=True)[:2000]

    for anchor in soup.find_all("a", href=True):
        if anchor.find_parent(["nav", "header", "footer"]):
            continue
        link = canonical_url(anchor["href"], base)
        if not is_detail_url(link) or link == canonical_url(final_url):
            continue
        text = anchor.get_text(" ", strip=True)
        if not text and anchor.get("aria-labelledby"):
            labels = [soup.find(id=identifier) for identifier in anchor["aria-labelledby"].split()]
            pieces = []
            for label in labels:
                if label is not None:
                    title_label = label.select_one(".job-tile__title") or label
                    pieces.append(title_label.get_text(" ", strip=True))
            text = " ".join(pieces)
        text = text or anchor.get("aria-label", "")
        # Prefer a title inside the actual link, never an unrelated sibling heading.
        heading = anchor.find(["h2", "h3", "h4", "h5", "strong"])
        title = heading.get_text(" ", strip=True) if heading else text
        container = anchor
        if not ROLE_WORDS.search(title) or title.lower() in {"details", "apply", "apply now", "mehr erfahren", "read more"}:
            # An apply link may inherit a heading only from a small, single-job card.
            for parent in list(anchor.parents)[:6]:
                if parent.name in {"body", "html"}:
                    break
                titles = parent.find_all(["h2", "h3", "h4", "h5", "strong"])
                links = {canonical_url(a["href"], base) for a in parent.find_all("a", href=True)
                         if is_detail_url(canonical_url(a["href"], base))}
                if len(titles) == 1 and links == {link} and len(parent.get_text(" ", strip=True)) <= 2000:
                    title, container = titles[0].get_text(" ", strip=True), parent
                    break
        elif anchor.parent and anchor.parent.name in {"li", "td", "div", "article"}:
            # Only include context from a single-job container (allowlists need location).
            parent = anchor.parent
            links = {canonical_url(a["href"], base) for a in parent.find_all("a", href=True)
                     if is_detail_url(canonical_url(a["href"], base))}
            if links == {link} and len(parent.get_text(" ", strip=True)) <= 2000:
                container = parent
        if is_linkedin(config["url"]) and not employer_matches(container, config):
            continue
        if 5 <= len(title) <= 160 and ROLE_WORDS.search(title) and not NON_JOB_TEXT.search(title):
            yield title, link, "", container.get_text(" ", strip=True)


class RateLimited(ValueError):
    """The origin asked us to slow down; retain prior evidence and stop this source."""


def verify_detail(page, job, config):
    """Fail closed on blocked, closed, redirected-to-listing or mismatched pages.

    Returns (canonical URL, extracted ad text). Raises ValueError on weak evidence.
    """
    if urlsplit(job.url).path.lower().endswith(".pdf"):
        response = page.request.get(job.url, timeout=20000)
        try:
            if response.status >= 400 or "pdf" not in response.headers.get("content-type", ""):
                raise ValueError("PDF vacancy could not be retrieved")
            data = response.body()
            if len(data) > 10_000_000:
                raise ValueError("PDF vacancy exceeds the verification size limit")
            reader = PdfReader(io.BytesIO(data))
            text = " ".join(p.extract_text() or "" for p in reader.pages[:20])
            if CLOSED_TEXT.search(text) or not title_matches(job.title, text, job.location) or len(text) < 300:
                raise ValueError("PDF title/content not verified")
            if not re.search(r"\b(apply|application|responsibilities|requirements|qualifications|bewerben|bewerbung|aufgaben|profil)\b", text, re.I):
                raise ValueError("PDF has no vacancy/application evidence")
            return canonical_url(response.url), text[:12000]
        finally:
            response.dispose()
    response = page.goto(job.url, wait_until="domcontentloaded", timeout=20000)
    if response is not None and response.status == 429:
        # Respect Retry-After with a bounded single retry; never hammer a blocked board.
        delay = response.headers.get("retry-after", "5")
        delay = max(5, int(delay)) if str(delay).isdigit() else 5
        if delay > 30:
            raise RateLimited("Retry-After exceeds the bounded wait; defer this source")
        page.wait_for_timeout(delay * 1000)
        response = page.goto(job.url, wait_until="domcontentloaded", timeout=20000)
        if response is not None and response.status == 429:
            raise RateLimited("Source rate limit persists after Retry-After; remaining details deferred")
    if response is None or response.status >= 400:
        raise ValueError(f"Job detail HTTP {response.status if response else 'no response'}")
    final = canonical_url(page.url)
    if not final or final == canonical_url(config["url"]) and not is_detail_url(final) and not config.get("inline_vacancies"):
        raise ValueError("Job link redirects to the career overview")
    if is_linkedin(job.url) and not is_detail_url(final):
        raise ValueError("LinkedIn job link redirects to search or login")
    if "application/pdf" in response.headers.get("content-type", ""):
        raise ValueError("PDF vacancy requires manual verification")
    try:
        expected_title = re.sub(r"\s+-\s+(?:Permanent Contract|Fixed.term Contract)\b.*$", "", job.title, flags=re.I)
        if job.location:
            expected_title = re.sub(r"[\s,|()\-]+" + re.escape(job.location) + r"\s*$", "", expected_title, flags=re.I)
        page.locator("h1, h2, h3").filter(has_text=re.compile(re.escape(expected_title), re.I)).first.wait_for(state="attached", timeout=8000)
    except Exception:
        pass  # JSON-LD can supply evidence even without a rendered heading.
    soup = BeautifulSoup(page.content(), "lxml")
    for tag in soup.find_all(["nav", "footer"]):
        tag.decompose()
    visible_text = soup.get_text(" ", strip=True)
    if CLOSED_TEXT.search(visible_text) or BLOCKED_TEXT.search(visible_text[:2000]):
        raise ValueError("Job detail is closed or blocked")
    postings = list(job_postings(soup))
    for posting in postings:
        if title_matches(job.title, plain(posting.get("title")), job.location):
            if expired(posting):
                raise ValueError("JobPosting has expired")
            if not employer_matches(soup, config, posting):
                raise ValueError("LinkedIn employer does not match monitored company")
            description = plain(posting.get("description"))
            if len(description) >= 200:
                return final, description[:12000]
    for tag in soup.find_all(["script", "style", "noscript"]):
        tag.decompose()
    content = soup.find("main") or soup.find("article")
    if content is None or len(content.get_text(" ", strip=True)) < 300:
        content = soup.find("body") or soup
    text = content.get_text(" ", strip=True)
    if CLOSED_TEXT.search(text):
        raise ValueError("Job detail says the vacancy is closed or unavailable")
    if BLOCKED_TEXT.search(text[:2000]):
        raise ValueError("Job detail blocked by access protection")
    headings = (soup if is_detail_url(final) else content).find_all(["h1", "h2", "h3"])
    if not any(title_matches(job.title, h.get_text(" ", strip=True), job.location) for h in headings):
        raise ValueError("Job title not confirmed on the detail page")
    if not employer_matches(content, config):
        raise ValueError("LinkedIn employer does not match monitored company")
    if len(text) < 300 or not re.search(
        r"\b(?:apply|application|responsibilities|requirements|qualifications|"
        r"bewerben|bewerbung|aufgaben|profil|candidature|postuler|solliciteer)\b", text, re.I
    ):
        raise ValueError("No substantive vacancy or application evidence")
    # Avoid picking the first role from a board that lists many job headings.
    if not is_detail_url(final) and len(postings) > 1:
        raise ValueError("Link resolves to a multi-job overview")
    return final, text[:12000]

