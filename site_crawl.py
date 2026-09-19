"""Bounded traversal of observed career links and rendered job-board pagination."""
from dataclasses import dataclass, field
import hashlib
import ipaddress
import re
import time
from urllib.parse import urlsplit, parse_qsl

from bs4 import BeautifulSoup
from job_sources import canonical_url, is_detail_url, BLOCKED_TEXT, ROLE_WORDS

CAREER = re.compile(r'career|karriere|vacanc|offene.stellen|stellenangebote|current.opportunit|open.positions|jobs|join.us', re.I)
ATS = re.compile(r'(?:myworkdayjobs\.com|jobs\.personio\.(?:de|com)|recruitee\.com|workable\.com|avature\.net|successfactors\.(?:eu|com)|oraclecloud\.(?:com|eu)|salesforce-sites\.com)$', re.I)
NEXT = re.compile(r'^(?:Go to Next Page, Number \d+|next(?: page(?: url)?)?|next\s*[>»]|nächste(?: seite)?|weiter|suivant|volgende|load more(?: jobs)?|show more(?: jobs)?|mehr laden)$', re.I)

@dataclass
class Crawl:
    documents: list = field(default_factory=list)
    warnings: list = field(default_factory=list)
    visited: list = field(default_factory=list)


def public_url(url):
    parsed = urlsplit(url)
    host = parsed.hostname or ''
    if parsed.scheme not in ('https', 'http') or parsed.username or host.lower() in ('localhost',) or '.' not in host:
        return False
    try:
        return ipaddress.ip_address(host).is_global
    except ValueError:
        return not host.endswith(('.local', '.internal'))


def career_links(html, base):
    """Follow only links actually published by a career source, never guessed paths."""
    soup = BeautifulSoup(html, 'lxml')
    for tag in soup.select('nav, header, footer'):
        tag.decompose()
    links = []
    origin = urlsplit(base).hostname
    for anchor in soup.select('a[href], iframe[src]'):
        url = canonical_url(anchor.get('href') or anchor.get('src'), base)
        if not url or not public_url(url) or url == canonical_url(base):
            continue
        parts, base_parts = urlsplit(url), urlsplit(base)
        if parts.path.rstrip('/') == base_parts.path.rstrip('/') and parts.query != base_parts.query:
            continue  # pagination has its own adapter; do not enumerate filter/language combinations
        if host_is_same := (parts.hostname == base_parts.hostname):
            source_language = re.search(r'/((?:en|de|fr|it|nl))(?:/|$)', base_parts.path)
            target_language = re.search(r'/((?:en|de|fr|it|nl))(?:/|$)', parts.path)
            if source_language and target_language and source_language[1] != target_language[1]:
                continue
            if source_language and source_language[1] == 'en' and '/karriere' in parts.path:
                continue
        host = parts.hostname or ''
        label = anchor.get_text(' ', strip=True)
        if is_detail_url(url) and not re.search(r'/(?:careers?|karriere|vacancies|jobs|opportunities|current-opportunities|open-positions)/?$', parts.path, re.I):
            continue
        path = urlsplit(url).path
        if (host == origin and CAREER.search(label + ' ' + path)) or (ATS.search(host) and (CAREER.search(label + ' ' + url) or anchor.name == 'iframe')):
            if not re.search(r'privacy|datenschutz|login|sign.in|register|jobalert|job.alert|linkedin|facebook', url, re.I):
                links.append(url)
    return list(dict.fromkeys(links))


def vacancy_urls(html, base):
    soup = BeautifulSoup(html, 'lxml')
    return sorted({canonical_url(a['href'], base) for a in soup.select('a[href]')
                   if is_detail_url(canonical_url(a['href'], base))})


def fingerprint(html, base):
    return hashlib.sha256('\n'.join(vacancy_urls(html, base)).encode()).hexdigest()


def next_control(page, page_number):
    # Workday has numbered buttons; do not use the active-page counter as evidence
    # of finished rendering (it changes before the new vacancy cards arrive).
    if 'myworkdayjobs.com' in page.url:
        numbered = page.get_by_role('button', name=f'page {page_number + 1}', exact=True)
        if numbered.count() and numbered.first.is_visible() and numbered.first.is_enabled():
            return numbered.first
    for role in ('button', 'link'):
        for control in page.get_by_role(role, name=NEXT).all():
            if control.is_visible() and control.is_enabled() and control.get_attribute('aria-disabled') != 'true':
                if (control.get_attribute('type') == 'submit' or
                        control.evaluate("el => Boolean(el.closest('[class*=swiper], [class*=carousel], [class*=slick], form'))")):
                    continue
                return control
    return None


def wait_changed(page, previous, timeout=12):
    deadline = time.monotonic() + timeout
    stable, stable_since = '', 0
    while time.monotonic() < deadline:
        html = page.content()
        current = fingerprint(html, page.url)
        if vacancy_urls(html, page.url) and current != previous:
            if current != stable:
                stable, stable_since = current, time.monotonic()
            elif time.monotonic() - stable_since >= 0.5:
                return html
        else:
            stable = ''
        page.wait_for_timeout(250)
    raise ValueError('Pagination did not expose different vacancy links')


def declared_job_count(html):
    text = BeautifulSoup(html, 'lxml').get_text(' ', strip=True)
    for pattern in (r"\bof\s+(\d+)\s+(?:jobs|results)", r"\b(\d+)\s+(?:JOBS FOUND|jobs found|Jobs Found)"):
        match = re.search(pattern, text)
        if match:
            return int(match.group(1))
    return None


def check_declared_count(documents):
    counts = [declared_job_count(d['html']) for d in documents]
    expected = max((n for n in counts if n is not None), default=None)
    if expected is None:
        return ''
    urls = {u for d in documents for u in vacancy_urls(d['html'], d['url'])}
    if len(urls) < expected:
        return f"Pagination coverage mismatch: board reports {expected} jobs but only {len(urls)} unique vacancy links were read."
    return ''


def collect_board(page, crawl, max_pages, deadline, request_delay=0):
    """Capture each rendered page once; unknown or stuck pagination is incomplete."""
    try:
        selector = page.get_by_role('combobox', name='Items per page').first
        if selector.count() and selector.is_visible():
            old = fingerprint(page.content(), page.url)
            options = selector.locator('option').all_text_contents()
            if '100' in [v.strip() for v in options]:
                selector.select_option(label='100', timeout=5000)
                # Small boards need not change their links when page size changes.
                try:
                    wait_changed(page, old, 5)
                except ValueError:
                    pass
    except Exception:
        crawl.warnings.append('Could not expand page size; pagination still required.')
    seen = set()
    for number in range(1, max_pages + 1):
        html, url = page.content(), page.url
        signature = fingerprint(html, url)
        if signature in seen:
            crawl.warnings.append('Pagination repeated a previously visited vacancy page.')
            return
        seen.add(signature)
        crawl.documents.append({'url': url, 'html': html})
        if time.monotonic() >= deadline:
            crawl.warnings.append('Career traversal reached the source time budget.')
            return
        control = next_control(page, number)
        if control is not None:
            if number == max_pages:
                crawl.warnings.append('Pagination reached the configured page limit before the last page.')
                return
            try:
                if request_delay:
                    page.wait_for_timeout(request_delay * 1000)
                control.click(timeout=8000)
                wait_changed(page, signature)
            except Exception:
                crawl.warnings.append('Next page could not be read; coverage is incomplete.')
                return
        elif 'oraclecloud.' in url:
            # Oracle candidate boards can append cards when their scroll sentinel
            # enters view. Compare vacancy links, not a loading spinner/counter.
            page.evaluate('window.scrollTo(0, document.body.scrollHeight)')
            try:
                wait_changed(page, signature, 4)
            except ValueError:
                return
            if number == max_pages:
                crawl.warnings.append('Infinite-scroll board reached the configured page limit.')
        else:
            return


def crawl_careers(context, config, fetch_page, deadline):
    crawl = Crawl()
    max_boards = min(int(config.get('max_career_pages', 12)), 40)
    max_pages = min(int(config.get('max_listing_pages', 60)), 100)
    queue = [(config['url'], 0)] + [(u, 0) for u in config.get('career_urls', [])]
    queued = {canonical_url(u) for u, _ in queue}
    while queue:
        if len(crawl.visited) >= max_boards or time.monotonic() >= deadline:
            crawl.warnings.append('Career traversal stopped with unvisited pages remaining.')
            break
        url, depth = queue.pop(0)
        if not public_url(url):
            crawl.warnings.append('Career URL is not a public HTTP(S) destination.')
            continue
        page = context.new_page()
        start = len(crawl.documents)
        try:
            response = page.goto(url, wait_until=config.get('navigation_wait_until', 'domcontentloaded'), timeout=20000)
            if response is None or response.status >= 400:
                raise ValueError(f'HTTP {response.status if response else "no response"}')
            page.wait_for_timeout(1500)
            if any(host in page.url for host in ('myworkdayjobs.com', 'oraclecloud.', 'successfactors.')):
                try:
                    page.locator('a[data-automation-id="jobTitle"], a[href*="/job/"], a[href*="career_job_req_id"]').first.wait_for(state='attached', timeout=12000)
                except Exception:
                    crawl.warnings.append('Dynamic board did not expose vacancy links in time.')
            if BLOCKED_TEXT.search(BeautifulSoup(page.content(), 'lxml').get_text(' ', strip=True)[:2000]):
                raise ValueError('Access protection blocked the career page')
            crawl.visited.append(canonical_url(page.url))
            if config.get('job_button_selector') and depth == 0:
                html, final = fetch_page(url, context, with_url=True, job_button_selector=config['job_button_selector'])
                crawl.documents.append({'url': final, 'html': html})
            else:
                collect_board(page, crawl, max_pages, deadline, min(10, float(config.get("request_delay_seconds", 0))))
            mismatch = check_declared_count(crawl.documents[start:])
            if mismatch:
                crawl.warnings.append(mismatch)
            for document in crawl.documents[start:]:
                for linked in career_links(document['html'], document['url']):
                    if linked in queued or linked in crawl.visited:
                        continue
                    queued.add(linked)
                    if depth < 2:
                        queue.append((linked, depth + 1))
                    else:
                        crawl.warnings.append('Additional career subpage exceeds traversal depth: ' + linked)
        except Exception as exc:
            crawl.warnings.append(f'Career page could not be read: {url} ({type(exc).__name__}).')
        finally:
            page.close()
    return crawl
