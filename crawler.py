"""Bounded, respectful crawl of a company's own public website.

Starts at the supplied URL and the homepage, then follows internal links whose
URL or anchor text suggests About / Team / Contact / Management / Purchasing
pages (in several languages), plus a small sitemap scan. Stops at
MAX_PAGES_PER_COMPANY pages and MAX_PDFS_PER_COMPANY PDFs.
"""
from __future__ import annotations

import io
import re
from dataclasses import dataclass, field
from urllib.parse import urljoin, urldefrag, urlsplit

from http_client import PoliteSession, host_of
import extractors as ex

try:
    from pypdf import PdfReader
except ImportError:  # optional
    PdfReader = None

LINK_KEYWORDS = {
    # weight 3: pages most likely to name people
    "team": 3, "our-team": 3, "meet": 3, "staff": 3, "people": 3, "management": 3, "leadership": 3,
    "directors": 3, "board": 2, "founder": 3, "owner": 3, "who-we-are": 3, "quienes-somos": 3,
    "nosotros": 3, "equipo": 3, "chi-siamo": 3, "equipe": 3, "impressum": 3, "imprint": 3,
    # weight 2: about / contact / purchasing
    "about": 2, "about-us": 2, "our-story": 2, "story": 2, "history": 2, "company": 2, "contact": 2,
    "contact-us": 2, "contacto": 2, "contatti": 2, "kontakt": 2, "hubungi": 2, "tentang": 2,
    "purchasing": 2, "procurement": 2, "supplier": 2, "suppliers": 2, "vendor": 2, "partners": 1,
    "press": 1, "news": 1, "media": 1, "locations": 1, "stores": 1, "showroom": 1, "dealer": 1,
    "legal": 1, "terms": 1, "privacy": 1,
}
SKIP_EXT = re.compile(r"\.(jpg|jpeg|png|gif|webp|svg|ico|mp4|mov|avi|mp3|zip|rar|exe|dmg|css|js|xml|json|woff2?|ttf)$", re.I)
SKIP_PATH = re.compile(r"/(cart|checkout|my-account|account|login|wp-login|wp-admin|admin|register|basket|wishlist|cdn-cgi|feed)\b|[?&](add-to-cart|replytocom|orderby|filter_)", re.I)


@dataclass
class Page:
    url: str
    title: str
    text: str
    kind: str = "html"  # html | pdf


@dataclass
class CrawlResult:
    start_url: str
    final_domain: str = ""
    pages: list[Page] = field(default_factory=list)
    visited: list[tuple[str, str]] = field(default_factory=list)   # (url, outcome)
    people: list = field(default_factory=list)
    emails: list = field(default_factory=list)
    phones: list = field(default_factory=list)
    identity: dict = field(default_factory=dict)
    cloudflare_emails: bool = False
    region: str = ""
    region_basis: str = ""
    parked: bool = False


def _same_site(url: str, domain: str) -> bool:
    h = host_of(url)
    h = h[4:] if h.startswith("www.") else h
    return h == domain or h.endswith("." + domain)


def _link_score(url: str, anchor: str) -> int:
    """Score by whole-word keyword matches, so 'press' does not match 'pressure-care'."""
    path = " " + re.sub(r"[/_.\-?=&]+", " ", urlsplit(url).path.lower()) + " "
    text = " " + re.sub(r"[^\w]+", " ", anchor.lower()) + " "
    if re.search(r"/(products?|collections?|shop|category|product-category|tag)/", urlsplit(url).path.lower()):
        return 0
    score = 0
    for kw, w in LINK_KEYWORDS.items():
        word = " " + kw.replace("-", " ") + " "
        if word in path or word in text:
            score = max(score, w)
    return score


# --------------------------------------------------------- country / region
TLD_REGION = {
    "au": "AU", "uk": "GB", "sg": "SG", "my": "MY", "mx": "MX", "pe": "PE", "it": "IT", "de": "DE", "fr": "FR",
    "es": "ES", "nz": "NZ", "ca": "CA", "ie": "IE", "nl": "NL", "be": "BE", "za": "ZA", "in": "IN", "ae": "AE",
    "br": "BR", "ar": "AR", "cl": "CL", "co": "CO", "jp": "JP", "kr": "KR", "cn": "CN", "hk": "HK", "ph": "PH",
    "th": "TH", "id": "ID", "vn": "VN", "pt": "PT", "ch": "CH", "at": "AT", "se": "SE", "no": "NO", "dk": "DK",
    "fi": "FI", "pl": "PL", "gr": "GR", "tr": "TR", "il": "IL", "sa": "SA", "us": "US",
}
REGION_NAMES = {
    "AU": "Australia", "GB": "United Kingdom", "SG": "Singapore", "MY": "Malaysia", "MX": "Mexico", "PE": "Peru",
    "IT": "Italy", "US": "United States", "CA": "Canada", "NZ": "New Zealand", "IE": "Ireland", "DE": "Germany",
    "FR": "France", "ES": "Spain", "ZA": "South Africa", "IN": "India", "AE": "United Arab Emirates",
}
US_ADDR = re.compile(r"\b(?:AL|AK|AZ|AR|CA|CO|CT|DE|FL|GA|HI|ID|IL|IN|IA|KS|KY|LA|ME|MD|MA|MI|MN|MS|MO|MT|NE|NV|NH|NJ|NM|NY|NC|ND|OH|OK|OR|PA|RI|SC|SD|TN|TX|UT|VT|VA|WA|WV|WI|WY)\s+\d{5}(?:-\d{4})?\b")
AU_ADDR = re.compile(r"\b(?:NSW|VIC|QLD|WA|SA|TAS|ACT|NT)\s+\d{4}\b")
UK_ADDR = re.compile(r"\b[A-Z]{1,2}\d[A-Z\d]?\s+\d[A-Z]{2}\b")
COUNTRY_WORDS = {"US": ("united states", "usa"), "GB": ("united kingdom", "england", "scotland", "wales"),
                 "AU": ("australia",), "CA": ("canada",), "IT": ("italy", "italia"), "MX": ("méxico", "mexico"),
                 "SG": ("singapore",), "MY": ("malaysia",), "PE": ("perú", "peru")}


def detect_region(domain: str, html_pages: list[str], texts: list[str], schema_country: str = "") -> tuple[str, str]:
    tld = domain.rsplit(".", 1)[-1]
    if tld in TLD_REGION:
        return TLD_REGION[tld], f"country-code domain .{tld}"
    if schema_country:
        sc = schema_country.strip()
        if len(sc) == 2:
            return sc.upper(), "schema.org addressCountry"
        for code, name in REGION_NAMES.items():
            if name.lower() == sc.lower():
                return code, "schema.org addressCountry"
    if ex.phonenumbers is not None:
        counts: dict[str, int] = {}
        for html in html_pages:
            for m in re.finditer(r'href="tel:\s*(\+[\d\s().-]{7,})"', html):
                try:
                    num = ex.phonenumbers.parse(m.group(1), None)
                    reg = ex.phonenumbers.region_code_for_number(num)
                    if reg:
                        counts[reg] = counts.get(reg, 0) + 1
                except ex.phonenumbers.NumberParseException:
                    continue
        if counts:
            reg = max(counts, key=counts.get)
            return reg, "international tel: links on website"
    joined = "\n".join(texts)
    for rx, reg, basis in ((AU_ADDR, "AU", "Australian state+postcode in address"),
                           (US_ADDR, "US", "US state+ZIP in address"),
                           (UK_ADDR, "GB", "UK postcode in address")):
        if len(rx.findall(joined)) >= 1:
            return reg, basis
    low = joined.lower()
    scores = {reg: sum(low.count(w) for w in words) for reg, words in COUNTRY_WORDS.items()}
    if scores and max(scores.values()) >= 2:
        reg = max(scores, key=scores.get)
        return reg, "country named repeatedly on website"
    return "", "not determined"


# ------------------------------------------------------------------ crawl
def crawl_company(start_url: str, domain: str, http: PoliteSession, settings, log) -> CrawlResult:
    res = CrawlResult(start_url=start_url, final_domain=domain)
    homepage = f"{urlsplit(start_url).scheme}://{urlsplit(start_url).netloc}/"
    queue: list[tuple[int, str, str]] = [(10, start_url, "start"), (9, homepage, "homepage")]
    seen: set[str] = set()
    pdf_count = 0
    raw_html: list[tuple[str, str]] = []

    sitemap_done = False
    common_paths_added = False
    while queue and len(res.pages) < settings.max_pages_per_company:
        queue.sort(key=lambda x: -x[0])
        score, url, why = queue.pop(0)
        url = urldefrag(url)[0]
        if url in seen:
            continue
        seen.add(url)
        is_pdf = url.lower().endswith(".pdf")
        if is_pdf and pdf_count >= settings.max_pdfs_per_company:
            continue
        r = http.fetch(url, max_bytes=settings.max_pdf_bytes if is_pdf else None)
        if re.search(r"(?:host='|//)ww\d+\.", (r.error or "") + " " + (r.final_url or "")):
            res.parked = True  # parking services redirect to ww<N>.<domain>
        if not r.ok:
            res.visited.append((url, r.error or f"HTTP {r.status}"))
            log("crawl", url, r.error or f"HTTP {r.status}")
            continue
        final = r.final_url or url
        if why in ("start", "homepage") and not _same_site(final, domain):
            new_host = host_of(final)
            new_dom = new_host[4:] if new_host.startswith("www.") else new_host
            log("crawl", url, f"Redirected to different domain {new_dom}")
            res.visited.append((url, f"redirected to {final}"))
            if why == "start":
                res.final_domain = new_dom
                domain = new_dom
        if final in seen and final != url:
            res.visited.append((url, f"duplicate of {final}"))
            continue
        seen.add(final)
        if not _same_site(final, domain):
            res.visited.append((url, f"left site ({final})"))
            continue

        if "pdf" in r.content_type or is_pdf:
            pdf_count += 1
            text = _pdf_text(r.content)
            if text:
                res.pages.append(Page(final, final.rsplit("/", 1)[-1], text, "pdf"))
                res.visited.append((final, "ok (pdf)"))
            else:
                res.visited.append((final, "pdf: no extractable text" + ("" if PdfReader else " (pypdf not installed)")))
            continue
        if "html" not in r.content_type and r.text[:200].lstrip()[:1] != "<":
            res.visited.append((final, f"skipped content-type {r.content_type}"))
            continue

        soup = ex.page_soup(r.text)
        links = []
        for a in soup.find_all("a", href=True):
            href = urljoin(final, a["href"].strip())
            if href.startswith(("http://", "https://")) and _same_site(href, domain):
                links.append((href, a.get_text(" ", strip=True)[:80]))
        if why in ("start", "homepage") and not res.identity:
            res.identity = ex.company_identity(soup, "", domain)
        text = ex.visible_text(soup)
        res.pages.append(Page(final, (soup.title.string or "").strip() if soup.title and soup.title.string else "", text))
        raw_html.append((final, r.text))
        res.visited.append((final, "ok" + (" (cache)" if r.from_cache else "")))
        if (any(m in text.lower()[:3000] for m in ex.PARKED_MARKERS) and len(text) < 3000) or (
                why in ("start", "homepage") and len(text) < 120 and "click here to enter" in text.lower()):
            res.parked = True

        for href, anchor in links:
            if href in seen or SKIP_EXT.search(urlsplit(href).path) or SKIP_PATH.search(href):
                continue
            s = _link_score(href, anchor)
            if href.lower().endswith(".pdf"):
                s = s + 1 if s else 0
            if s > 0:
                queue.append((s, href, f"link: {anchor or href}"))

        if not common_paths_added and why in ("start", "homepage"):
            common_paths_added = True
            if sum(1 for q in queue if q[0] >= 2) < 2:
                # Navigation may be rendered by JavaScript: try a few common paths on the final host.
                base = f"{urlsplit(final).scheme}://{urlsplit(final).netloc}/"
                for path in ("about-us", "about", "contact-us", "contact", "our-team", "team"):
                    queue.append((1, urljoin(base, path), "common path"))

        if not sitemap_done and why in ("start", "homepage"):
            sitemap_done = True
            for href in _sitemap_urls(homepage, http, settings):
                if _same_site(href, domain) and href not in seen:
                    s = _link_score(href, "")
                    if s >= 2:
                        queue.append((s, href, "sitemap"))

    # ---- extraction across all collected pages
    identity_text = "\n".join(p.text for p in res.pages[:3])
    if raw_html:
        soup0 = ex.page_soup(raw_html[0][1])
        ident = ex.company_identity(soup0, identity_text, domain)
        res.identity = {**ident, **{k: v for k, v in res.identity.items() if v}}
    res.region, res.region_basis = detect_region(domain, [h for _, h in raw_html], [p.text for p in res.pages],
                                                 res.identity.get("schema_country", ""))
    html_by_url = dict(raw_html)
    for p in res.pages:
        soup = ex.page_soup(html_by_url[p.url]) if p.url in html_by_url else None
        res.people += ex.extract_people(p.text, p.url, "official_website", soup)
    names = sorted({pp.name for pp in res.people}, key=len, reverse=True)
    for p in res.pages:
        html = html_by_url.get(p.url, "")
        soup = ex.page_soup(html) if html else None
        emails, cf = ex.extract_emails(html, p.text, p.url, "official_website", soup)
        res.emails += emails
        res.cloudflare_emails |= cf
        soup = ex.page_soup(html) if html else None
        res.phones += ex.extract_phones(p.text, p.url, "official_website", res.region, names, soup)
    return res


def _sitemap_urls(homepage: str, http: PoliteSession, settings) -> list[str]:
    urls: list[str] = []
    for path in ("sitemap.xml", "sitemap_index.xml", "page-sitemap.xml"):
        r = http.fetch(urljoin(homepage, path))
        if not r.ok or "<" not in r.text[:100]:
            continue
        locs = re.findall(r"<loc>\s*([^<]+?)\s*</loc>", r.text)[:500]
        for loc in locs:
            if loc.endswith(".xml") and ("page" in loc or "post" not in loc) and len(urls) < 500:
                sub = http.fetch(loc)
                if sub.ok:
                    urls += re.findall(r"<loc>\s*([^<]+?)\s*</loc>", sub.text)[:500]
                break
            urls.append(loc)
        if urls:
            break
    return urls


def _pdf_text(content: bytes) -> str:
    if not content or PdfReader is None:
        return ""
    try:
        reader = PdfReader(io.BytesIO(content))
        return "\n".join((pg.extract_text() or "") for pg in reader.pages[:20])
    except Exception:  # malformed PDFs are common; skip quietly
        return ""
