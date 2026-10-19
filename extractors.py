"""Extract published names, job titles, professional emails and phone numbers.

Everything here is heuristic text analysis. Each finding keeps the exact
snippet it came from so a human can check it. Nothing is guessed: an email
is only returned if it is literally published, a phone number only if it is
literally printed on the page.
"""
from __future__ import annotations

import json
import re
import unicodedata
from dataclasses import dataclass, field

from bs4 import BeautifulSoup

try:
    import phonenumbers
    from phonenumbers import PhoneNumberFormat, PhoneNumberType
except ImportError:  # pragma: no cover - documented as required
    phonenumbers = None

# --------------------------------------------------------------------- titles
EXEC_TITLES = [
    r"chief executive(?: officer)?", r"ceo", r"co-?founder", r"founder", r"co-?owner", r"owner",
    r"proprietor", r"managing director", r"president", r"general manager", r"managing partner",
    r"director general", r"gerente general", r"director ejecutivo", r"fundador(?:a)?",
    r"propietari[oa]", r"due[ñn]o", r"directeur g[ée]n[ée]ral", r"g[ée]rant", r"pr[ée]sident",
    r"gesch[äa]ftsf[üu]hrer(?:in)?", r"inhaber(?:in)?", r"amministratore delegato", r"titolare",
    r"pengarah urusan", r"director",
]
PURCHASING_TITLES = [
    r"head of (?:purchasing|procurement|sourcing|buying)", r"(?:senior )?purchasing (?:manager|director|officer|lead)",
    r"(?:senior )?procurement (?:manager|director|officer|lead|specialist)", r"(?:senior )?sourcing (?:manager|director)",
    r"(?:senior |chief |lead )?buyer", r"category manager", r"supply chain (?:manager|director)",
    r"(?:jefe|gerente|director) de compras", r"responsable (?:des )?achats", r"acheteur", r"einkaufsleiter(?:in)?",
    r"eink[äa]ufer(?:in)?", r"responsabile acquisti", r"pengurus pembelian",
]
OTHER_SENIOR_TITLES = [
    r"operations (?:manager|director)", r"commercial (?:manager|director)", r"sales (?:manager|director)",
    r"business development (?:manager|director)", r"general sales manager", r"store manager", r"branch manager",
    r"chief operating officer", r"coo", r"cfo", r"chief financial officer",
]

ROLE_EXEC = "Owner / Executive"
ROLE_PURCHASING = "Purchasing / Procurement"
ROLE_OTHER = "Other senior (fallback)"

_TITLE_RES = [
    (ROLE_PURCHASING, re.compile(r"\b(" + "|".join(PURCHASING_TITLES) + r")\b", re.I)),
    (ROLE_EXEC, re.compile(r"\b(" + "|".join(EXEC_TITLES) + r")\b", re.I)),
    (ROLE_OTHER, re.compile(r"\b(" + "|".join(OTHER_SENIOR_TITLES) + r")\b", re.I)),
]

# Words that show a "name" candidate is not a person's name.
NON_NAME_WORDS = {
    "mobility", "medical", "solutions", "services", "service", "group", "pty", "ltd", "limited", "llc", "inc",
    "company", "team", "our", "the", "about", "contact", "us", "home", "shop", "cart", "wheelchair", "wheelchairs",
    "scooter", "scooters", "equipment", "hire", "rental", "rentals", "sales", "australia", "uk", "usa", "products",
    "product", "aids", "care", "health", "healthcare", "store", "online", "direct", "news", "blog", "privacy",
    "policy", "terms", "conditions", "read", "more", "view", "meet", "welcome", "office", "head", "director",
    "manager", "owner", "founder", "ceo", "president", "general", "managing", "purchasing", "procurement",
    "chief", "executive", "officer", "customer", "support", "sdn", "bhd", "pte", "beach", "sand", "electric",
    "power", "chair", "chairs", "travel", "holiday", "holidays", "accessible", "accessibility", "tours", "special",
    "needs", "sea", "cruise", "cruises", "street", "road", "avenue", "suite", "unit", "level", "monday", "friday",
    "saturday", "sunday", "opening", "hours", "email", "phone", "call", "mobile", "tel", "fax", "address",
    "copyright", "rights", "reserved", "all", "my", "account", "login", "register", "search", "menu", "sign",
    "free", "delivery", "shipping", "returns", "warranty", "faq", "gallery", "testimonials", "reviews", "click",
    "here", "and", "for", "with", "of", "in", "on", "at", "to", "by", "from", "new", "best", "quality", "solution",
    "rehab", "rehabilitation", "clinic", "hospital", "national", "international", "global", "south", "north",
    "east", "west", "gold", "coast", "queensland", "brisbane", "sydney", "melbourne", "perth", "adelaide",
    "london", "cornwall", "singapore", "malaysia", "mexico", "peru", "italy", "florida", "california", "texas",
    "board", "directors", "management", "house", "building", "buildings", "park", "way", "close", "centre",
    "center", "court", "lane", "drive", "place", "square", "estate", "hall", "tower", "plaza", "business", "industrial",
    "location", "locations", "department", "clinic", "workshop", "assessors", "technicians",
    "leadership", "staff", "people", "story", "mission", "vision", "values", "history", "why", "choose",
    "how", "what", "who", "we", "are", "is", "you", "your", "get", "touch", "quote", "request", "book", "now",
}
NAME_PARTICLES = {"de", "da", "del", "della", "der", "van", "von", "la", "le", "di", "du", "bin", "binti", "al", "el", "dos", "das"}
HONORIFICS = re.compile(r"^(?:mr|mrs|ms|miss|dr|prof|sir|dame|sr|sra|ing|lic)\.?\s+", re.I)

_NAME_TOKEN = r"[A-ZÀ-ÖØ-Þ][a-zà-öø-ÿ'’]+(?:-[A-ZÀ-ÖØ-Þ][a-zà-öø-ÿ'’]+)?"
NAME_RE = re.compile(
    rf"(?:(?:Mr|Mrs|Ms|Dr)\.?\s+)?({_NAME_TOKEN}(?:\s+(?:(?:{'|'.join(NAME_PARTICLES)})\s+)*(?:{_NAME_TOKEN}|[A-Z]\.)){{1,3}})"
)

# ---------------------------------------------------------------- emails
EMAIL_RE = re.compile(r"\b[A-Za-z0-9][A-Za-z0-9._%+\-]{0,63}@[A-Za-z0-9.\-]+\.[A-Za-z]{2,24}\b")
OBFUSCATED_EMAIL_RE = re.compile(
    r"\b([A-Za-z0-9][A-Za-z0-9._%+\-]{0,63})\s*[\[\(\{]\s*at\s*[\]\)\}]\s*([A-Za-z0-9\-]+(?:\s*[\[\(\{]\s*dot\s*[\]\)\}]\s*[A-Za-z0-9\-]+)+)",
    re.I,
)
JUNK_EMAIL_DOMAINS = (
    "example.com", "domain.com", "email.com", "yourdomain.com", "sentry.io", "wixpress.com", "sentry-next.wixpress.com",
    "mysite.com", "company.com", "test.com", "godaddy.com", "yoursite.com", "website.com", "my-domain.com",
    "mydomain.com", "yourcompany.com", "yourname.com", "sample.com", "address.com", "wix.com", "squarespace.com",
)
JUNK_EMAIL_SUFFIXES = (".png", ".jpg", ".jpeg", ".gif", ".webp", ".svg", ".css", ".js")
GENERIC_LOCALPARTS = {
    "info", "information", "sales", "admin", "administration", "contact", "contacts", "contactus", "enquiries",
    "enquiry", "inquiries", "inquiry", "office", "hello", "hi", "support", "service", "services", "customerservice",
    "customerservices", "customercare", "care", "orders", "order", "accounts", "accounting", "billing", "reception",
    "mail", "email", "team", "bookings", "booking", "reservations", "hire", "rentals", "rental", "marketing", "web",
    "webmaster", "noreply", "no-reply", "donotreply", "ventas", "contacto", "informacion", "atencion", "clientes",
    "purchasing", "procurement", "buying", "purchase", "compras", "careers", "jobs", "hr", "privacy", "help",
    "shop", "store", "general", "admin1", "media", "press", "pr", "news", "newsletter", "feedback", "service-desk",
    "warranty", "returns", "parts", "repairs", "workshop", "dispatch", "logistics", "export", "import", "trade",
    "wholesale", "dealer", "dealers", "partners", "vendas", "kontakt", "vendite", "commerciale",
}

# ----------------------------------------------------------------- phones
PHONE_BLOCK_PREFIX = re.compile(
    r"(ABN|ACN|A\.B\.N|VAT|GST|UEN|SSM|RUC|NIF|CIF|RFC|EIN|Reg(?:istration)?\.?\s*No|Company\s*(?:No|Number)|"
    r"Licen[cs]e|Lic\.?\s*No|NDIS|Order|Invoice|SKU|Item|Model|Part|Serial|Postcode|Zip|BSB|Account|IBAN|SWIFT)\W{0,6}$",
    re.I,
)
MOBILE_HINT = re.compile(r"\b(mobile|mob|cell|cellular|m\s*[:.]|móvil|movil|celular|handphone|hp\s*[:.]|whats\s*app)\b", re.I)
EXT_HINT = re.compile(r"\b(ext|extension|x|anexo|interno|ramal|durchwahl)\.?\s*[:#]?\s*(\d{1,6})\b", re.I)
DIRECT_HINT = re.compile(r"\b(direct|dd|ddi|direct dial|direct line)\b", re.I)
DEPT_HINT = re.compile(r"\b(purchasing|procurement|accounts|service|workshop|parts|hire|rentals?|bookings?|sales|customer|warehouse|dispatch|support)\b", re.I)
FAX_HINT = re.compile(r"\bfax\b", re.I)


@dataclass
class PersonFinding:
    name: str
    title: str
    role_category: str
    source_url: str
    source_type: str          # official_website | official_registry | third_party_page | search_snippet | data_provider
    snippet: str
    pattern: str              # strong | weak
    extra: dict = field(default_factory=dict)


@dataclass
class EmailFinding:
    email: str
    kind: str                 # personal | generic
    source_url: str
    source_type: str
    context: str
    person_name: str = ""     # person the email is attributed to (only if evidence supports it)
    attribution: str = ""     # how attribution was made


@dataclass
class PhoneFinding:
    display: str
    e164: str
    extension: str
    phone_type: str
    number_kind: str          # mobile / fixed_line / toll_free / unknown (from numbering plan)
    source_url: str
    source_type: str
    context: str
    person_name: str = ""


# ================================================================== helpers
def fold(text: str) -> str:
    return "".join(c for c in unicodedata.normalize("NFKD", text) if not unicodedata.combining(c)).lower()


def page_soup(html: str) -> BeautifulSoup:
    return BeautifulSoup(html, "html.parser")


def visible_text(soup: BeautifulSoup) -> str:
    for tag in soup(["script", "style", "noscript", "template", "svg"]):
        tag.decompose()
    text = soup.get_text("\n")
    lines = [re.sub(r"[ \t\xa0]+", " ", ln).strip() for ln in text.splitlines()]
    return "\n".join(ln for ln in lines if ln)


def classify_title(text: str) -> tuple[str, str] | None:
    """Return (role_category, matched title) for the highest-priority title in text."""
    for role, rx in _TITLE_RES:
        m = rx.search(text)
        if m:
            title = m.group(1)
            # bare 'director' is only an exec title when not part of e.g. 'directory'
            return role, title
    return None


def clean_name(candidate: str) -> str | None:
    if "\n" in candidate:
        return None
    cand = HONORIFICS.sub("", candidate.strip(" ,.-–—:|"))
    tokens = cand.split()
    if not 2 <= len(tokens) <= 4 or len(cand) > 40:
        return None
    real = [t for t in tokens if t.lower() not in NAME_PARTICLES]
    if len(real) < 2:
        return None
    for t in real:
        base = t.strip(".").lower().replace("’", "'")
        if base in NON_NAME_WORDS or len(base) < 2 and not t.endswith("."):
            return None
        if t.isupper() and len(t) > 2:  # acronyms such as 'NDIS', 'CEO'
            return None
    return cand


def _names_in(text: str) -> list[str]:
    out = []
    for m in NAME_RE.finditer(text):
        n = clean_name(m.group(1))
        if n:
            out.append(n)
    return out


# ================================================================== people
def _people_from_jsonld(soup: BeautifulSoup, url: str, source_type: str) -> list[PersonFinding]:
    found: list[PersonFinding] = []

    def walk(node, parent_key=""):
        if isinstance(node, list):
            for n in node:
                walk(n, parent_key)
        elif isinstance(node, dict):
            typ = node.get("@type")
            typ = " ".join(typ) if isinstance(typ, list) else str(typ or "")
            if "Person" in typ and isinstance(node.get("name"), str):
                title = str(node.get("jobTitle") or parent_key or "")
                name = clean_name(node["name"])
                cls = classify_title(title) if title else None
                if name and cls:
                    found.append(PersonFinding(name, title, cls[0], url, source_type,
                                               f"schema.org Person: {node['name']} / {title}", "strong"))
            for k, v in node.items():
                walk(v, k if k in ("founder", "employee", "member", "ceo", "owns") else parent_key)

    for tag in soup.find_all("script", type="application/ld+json"):
        try:
            walk(json.loads(tag.string or ""))
        except (ValueError, TypeError):
            continue
    return found


_FOUNDED_BY = re.compile(
    r"\b(?:founded|established|started|owned|run|created|led|managed)\s+(?:in\s+\d{4}\s+)?by\s+"
    r"((?:[A-ZÀ-Þ][\w'’-]+\s?){2,4})", re.U)
_NAME_FOUNDED = re.compile(r"\b([A-ZÀ-Þ][a-zà-ÿ'’-]+(?:[ ][A-ZÀ-Þ][a-zà-ÿ'’-]+){0,2})[ ]+(?i:founded|established|started|set up)\s+[A-Z]")
_PROFILE_SLUG = re.compile(r"/(?:team|people|staff|our-team|about-us/team|meet-the-team)/([a-z]+(?:-[a-z]+){1,2})/?$")
_I_AM = re.compile(
    r"\b(?:my name is|i am|i'm)\s+((?:[A-ZÀ-Þ][\w'’-]+\s?){2,3})[, ]+(?:and\s+i(?:'m| am)\s+)?(?:the\s+)?"
    r"((?:co-?)?(?:founder|owner|managing director|director|ceo))", re.I | re.U)


def extract_people(text: str, url: str, source_type: str, soup: BeautifulSoup | None = None) -> list[PersonFinding]:
    """Find (name, title) pairs printed on a page or snippet."""
    found: list[PersonFinding] = []
    if soup is not None:
        found += _people_from_jsonld(soup, url, source_type)

    lines = [ln.strip() for ln in text.split("\n") if ln.strip()]
    for i, line in enumerate(lines):
        if len(line) > 300:
            # long paragraphs: only use narrative patterns below
            pass
        cls = classify_title(line)
        if not cls:
            continue
        role, title = cls
        # Strong: name and title on the same short line ("Jane Doe - Managing Director").
        if len(line) <= 160:
            names = [n for n in _names_in(line) if fold(title) not in fold(n)]
            if names:
                found.append(PersonFinding(names[0], _title_phrase(line, title), role, url, source_type, line[:300], "strong"))
                continue
        # Weak: a line that is only a title, with a name-only line just above/below it.
        if len(line) <= 60:
            for j in (i - 1, i + 1):
                if 0 <= j < len(lines) and len(lines[j]) <= 40:
                    n = clean_name(lines[j])
                    if n and not classify_title(lines[j]):
                        found.append(PersonFinding(n, _title_phrase(line, title), role, url, source_type,
                                                   f"{lines[j]} | {line}"[:300], "weak"))
                        break

    for m in _FOUNDED_BY.finditer(text):
        n = clean_name(m.group(1))
        if n:
            verb = m.group(0).split()[0].lower()
            title = "Founder" if verb in ("founded", "established", "started", "created") else "Owner"
            ctx = text[max(0, m.start() - 60): m.end() + 60].replace("\n", " ")
            found.append(PersonFinding(n, title, ROLE_EXEC, url, source_type, ctx, "strong"))
    slug = _PROFILE_SLUG.search(url.lower())
    for m in _NAME_FOUNDED.finditer(text):
        cand = m.group(1)
        ctx = text[max(0, m.start() - 40): m.end() + 60].replace("\n", " ")
        n = clean_name(cand)
        pattern = "strong"
        if not n and slug and len(cand.split()) == 1 and slug.group(1).split("-")[0] == fold(cand):
            # first name only on the page; surname taken from the profile URL (/team/phil-francis/)
            n = " ".join(t.capitalize() for t in slug.group(1).split("-"))
            pattern = "weak"
            ctx += f"  [surname from page URL {url}]"
        if n:
            found.append(PersonFinding(n, "Founder", ROLE_EXEC, url, source_type, ctx, pattern))
    for m in _I_AM.finditer(text):
        n = clean_name(m.group(1))
        if n:
            ctx = text[max(0, m.start() - 40): m.end() + 40].replace("\n", " ")
            found.append(PersonFinding(n, m.group(2).title(), ROLE_EXEC, url, source_type, ctx, "strong"))
    return _dedupe_people(found)


def _title_phrase(line: str, title: str) -> str:
    """Expand a matched keyword to the surrounding title phrase, e.g. 'Director' -> 'Sales & Marketing Director'."""
    m = re.search(r"((?:[A-Z][\w&/]*\s+){0,3}" + re.escape(title) + r"(?:\s+(?:of|for|&|and)\s+[A-Z][\w&/]*(?:\s+[A-Z][\w&/]*){0,2})?)", line, re.I)
    phrase = (m.group(1) if m else title).strip()
    phrase = re.sub(r"^(?:(?:meet|about|our|the|with|from|by|and|is|as)\s+)+", "", phrase, flags=re.I)
    # drop a leading person name that may have been captured
    for n in _names_in(phrase):
        phrase = phrase.replace(n, "").strip(" ,-–—|:")
    return phrase or title


def _dedupe_people(items: list[PersonFinding]) -> list[PersonFinding]:
    seen: dict[tuple, PersonFinding] = {}
    for p in items:
        key = (fold(p.name), p.role_category)
        if key not in seen or (p.pattern == "strong" and seen[key].pattern != "strong"):
            seen[key] = p
    return list(seen.values())


# ================================================================== emails
def extract_emails(html: str, text: str, url: str, source_type: str, soup: BeautifulSoup | None = None) -> tuple[list[EmailFinding], bool]:
    """Return (emails, cloudflare_obfuscation_present)."""
    raw: dict[str, str] = {}
    if soup is not None:
        for a in soup.select('a[href^="mailto:"], a[href^="MAILTO:"]'):
            addr = a.get("href", "")[7:].split("?")[0].strip()
            ctx = a.find_parent(["p", "li", "div", "td", "section"]) or a
            raw.setdefault(addr, ctx.get_text(" ", strip=True)[:300])
    for m in EMAIL_RE.finditer(text):
        raw.setdefault(m.group(0), text[max(0, m.start() - 150): m.end() + 60].replace("\n", " | "))
    for m in OBFUSCATED_EMAIL_RE.finditer(text):
        domain = re.sub(r"\s*[\[\(\{]\s*dot\s*[\]\)\}]\s*", ".", m.group(2), flags=re.I)
        raw.setdefault(f"{m.group(1)}@{domain}", text[max(0, m.start() - 150): m.end() + 60].replace("\n", " | "))

    out = []
    for addr, ctx in raw.items():
        addr = addr.strip().strip(".").lower()
        if not EMAIL_RE.fullmatch(addr):
            continue
        dom = addr.split("@", 1)[1]
        if dom in JUNK_EMAIL_DOMAINS or addr.endswith(JUNK_EMAIL_SUFFIXES) or re.search(r"@\d+x\.", addr):
            continue
        local = addr.split("@", 1)[0]
        kind = "generic" if is_generic_local(local) else "personal"
        out.append(EmailFinding(addr, kind, url, source_type, ctx))
    cf = "data-cfemail" in html or "/cdn-cgi/l/email-protection" in html
    return out, cf


GENERIC_FRAGMENTS = re.compile(r"(sales|info|enquir|inquir|service|support|order|account|admin|contact|office|booking|"
                               r"rental|reception|dealer|retail|wholesale|purchas|procure|marketing|hello|warrant|return)")


def is_generic_local(local: str) -> bool:
    local = local.lower()
    return (local in GENERIC_LOCALPARTS or re.sub(r"[\d._-]+$", "", local) in GENERIC_LOCALPARTS
            or bool(GENERIC_FRAGMENTS.search(local)))


def email_matches_person(email: str, name: str) -> str:
    """Return a description when the local part plausibly belongs to `name`, else ''."""
    local = fold(email.split("@", 1)[0])
    parts = [fold(p) for p in re.split(r"[\s-]+", name) if p.lower() not in NAME_PARTICLES and len(p.strip(".")) > 1]
    if len(parts) < 2:
        return ""
    first, last = re.sub(r"[^a-z]", "", parts[0]), re.sub(r"[^a-z]", "", parts[-1])
    letters = re.sub(r"[^a-z]", "", local)
    if not first or not last:
        return ""
    if first in letters and last in letters:
        return "local part contains first and last name"
    if letters in (first[0] + last, last + first[0], first + last[0], last):
        return "local part matches initial + surname pattern"
    if letters == first:
        return "local part equals first name only (weaker)"
    return ""


# ================================================================== phones
def _phone_type(ctx_before: str, ctx_after: str, ext: str, kind: str, person: str) -> str:
    # Only the label right before the number counts ("Mobile 07.. | Office 0800.." must not make the office line a mobile).
    ctx = ctx_before[-30:] + " " + ctx_after[:12]
    if FAX_HINT.search(ctx_before[-25:]):
        return "Fax"
    if kind == "toll_free":
        return "Switchboard / main line (toll-free)"
    if person:
        if ext:
            return "Extension"
        if kind == "mobile" or MOBILE_HINT.search(ctx):
            return "Business mobile"
        return "Direct dial"
    if ext:
        return "Extension (person not stated)"
    if DIRECT_HINT.search(ctx):
        return "Direct dial (person not stated)"
    if DEPT_HINT.search(ctx_before[-30:]):
        return "Department line"
    if kind == "mobile" or MOBILE_HINT.search(ctx):
        return "Company-listed mobile (not tied to a named person)"
    return "Switchboard / main line"


def extract_phones(text: str, url: str, source_type: str, region: str | None,
                   person_names: list[str], soup: BeautifulSoup | None = None) -> list[PhoneFinding]:
    if phonenumbers is None:
        return []
    found: dict[str, PhoneFinding] = {}
    region = (region or "ZZ").upper()

    def add(num, start: int, end: int, src_text: str):
        before = src_text[max(0, start - 200): start]
        after = src_text[end: end + 60]
        if PHONE_BLOCK_PREFIX.search(before[-30:]):
            return
        e164 = phonenumbers.format_number(num, PhoneNumberFormat.E164)
        ext = num.extension or ""
        if not ext:
            m = EXT_HINT.match(after.strip())
            if m:
                ext = m.group(2)
        ntype = phonenumbers.number_type(num)
        kind = {PhoneNumberType.MOBILE: "mobile", PhoneNumberType.FIXED_LINE: "fixed_line",
                PhoneNumberType.FIXED_LINE_OR_MOBILE: "fixed_or_mobile", PhoneNumberType.TOLL_FREE: "toll_free"}.get(ntype, "unknown")
        # attribute to the nearest preceding person name within the same block
        person = ""
        best = -1
        window = before[-160:]
        for n in person_names:
            pos = fold(window).rfind(fold(n))
            if pos > best:
                best, person = pos, n
        if not person:
            firsts = [n.split()[0] for n in person_names]
            for n in person_names:
                first = n.split()[0]
                if len(first) >= 3 and firsts.count(first) == 1:
                    m = list(re.finditer(rf"\b{re.escape(first)}\b", window[-60:]))
                    if m:
                        person = n
        if person and "\n\n" in window[best:]:
            person = ""
        display = phonenumbers.format_number(num, PhoneNumberFormat.INTERNATIONAL) + (f" ext. {ext}" if ext else "")
        key = e164 + ext + "|" + person
        ptype = _phone_type(before, after, ext, kind, person)
        if ptype == "Fax":
            return
        if key not in found:
            found[key] = PhoneFinding(display, e164, ext, ptype, kind, url, source_type,
                                      (before[-120:] + "【" + src_text[start:end] + "】" + after[:40]).replace("\n", " | "), person)

    for m in phonenumbers.PhoneNumberMatcher(text, region, leniency=phonenumbers.Leniency.VALID):
        add(m.number, m.start, m.end, text)
    if soup is not None:
        for a in soup.select('a[href^="tel:"]'):
            raw_num = a.get("href", "")[4:].strip()
            try:
                num = phonenumbers.parse(raw_num, region if not raw_num.startswith("+") else None)
            except phonenumbers.NumberParseException:
                continue
            if not phonenumbers.is_valid_number(num):
                continue
            label = a.get_text(" ", strip=True)
            parent = a.find_parent(["p", "li", "div", "td"])
            ptxt = parent.get_text(" ", strip=True) if parent else label
            idx = ptxt.find(label) if label else -1
            idx = idx if idx >= 0 else len(ptxt)
            add(num, idx, idx + len(label), ptxt)
    return list(found.values())


# ============================================================ company identity
LEGAL_SUFFIX = r"(?:Pty\.?\s*Ltd\.?|Ltd\.?|Limited|LLC|L\.L\.C\.|Inc\.?|Corp\.?|GmbH|S\.?A\.?C\.?|S\.?A\.? de C\.?V\.?|Sdn\.?\s*Bhd\.?|Pte\.?\s*Ltd\.?|S\.?r\.?l\.?|S\.?L\.?|B\.?V\.?|PLC)"
LEGAL_NAME_RE = re.compile(r"((?:(?:[A-Z0-9][\w'’.-]*|&|and|of|de|y|e)\s){1,7}" + LEGAL_SUFFIX + r")(?![\w])")
LEGAL_LEADIN = re.compile(r"^(?:(?:copyright|©|all|rights|reserved|\d{4}|[-–])\s*)+", re.I)


def company_identity(soup: BeautifulSoup, text: str, domain: str) -> dict:
    ident = {"site_name": "", "legal_name": "", "title": ""}
    og = soup.find("meta", property="og:site_name")
    if og and og.get("content"):
        ident["site_name"] = og["content"].strip()
    for tag in soup.find_all("script", type="application/ld+json"):
        try:
            data = json.loads(tag.string or "")
        except (ValueError, TypeError):
            continue
        for node in data if isinstance(data, list) else data.get("@graph", [data]) if isinstance(data, dict) else []:
            if isinstance(node, dict) and str(node.get("@type", "")) in ("Organization", "LocalBusiness", "MedicalBusiness", "Store", "Corporation"):
                if isinstance(node.get("legalName"), str):
                    ident["legal_name"] = ident["legal_name"] or node["legalName"].strip()
                if isinstance(node.get("name"), str):
                    ident["site_name"] = ident["site_name"] or node["name"].strip()
                addr = node.get("address")
                if isinstance(addr, dict) and addr.get("addressCountry"):
                    c = addr["addressCountry"]
                    ident["schema_country"] = c.get("name") if isinstance(c, dict) else str(c)
    if soup.title and soup.title.string:
        ident["title"] = soup.title.string.strip()
    if not ident["legal_name"]:
        for m in re.finditer(r"(?:©|&copy;|copyright)\s*(?:\d{4}\s*[-–]?\s*\d{0,4})?\s*(.{3,80})", text, re.I):
            lm = LEGAL_NAME_RE.search(m.group(1))
            if lm:
                name = LEGAL_LEADIN.sub("", lm.group(1)).strip()
                if len(name.split()) >= 2:
                    ident["legal_name"] = name
                    break
    return ident


def _shares_token(name: str, domain: str) -> bool:
    stem = re.sub(r"[^a-z]", "", fold(domain.split(".")[0]))
    return any(len(t) >= 4 and t in stem for t in re.findall(r"[a-z]+", fold(name)))


def best_company_name(ident: dict, domain: str) -> str:
    """Prefer a legal name that plausibly matches the domain; otherwise the site's own name."""
    legal = ident.get("legal_name", "")
    site = ident.get("site_name", "")
    if legal and (_shares_token(legal, domain) or (site and _shares_token(legal, site))):
        return legal
    if site:
        return site
    if legal and not ident.get("title"):
        return legal
    title = ident.get("title", "")
    stem = re.sub(r"[^a-z]", "", domain.split(".")[0])
    parts = [p.strip() for p in re.split(r"\s[|\-–—:]\s", title) if p.strip()]
    best, score = "", 0.0
    for p in parts:
        letters = re.sub(r"[^a-z]", "", p.lower())
        if not letters:
            continue
        common = sum(1 for ch in set(letters) if ch in stem) / max(len(set(letters)), 1)
        if stem and stem in letters:
            common += 1
        if common > score:
            best, score = p, common
    best = best.strip(" '\"-–—|:")
    return best or domain


PARKED_MARKERS = ("domain is for sale", "buy this domain", "this domain may be for sale", "parked free",
                  "domain has expired", "website coming soon", "account suspended", "under construction")

PRODUCT_TERMS = ("wheelchair", "electric wheelchair", "power chair", "powerchair", "mobility scooter", "scooter",
                 "rollator", "walker", "beach wheelchair", "rehabilitation", "hoist", "lift chair", "mobility aid",
                 "patient lift", "commode", "hospital bed", "silla de ruedas", "kerusi roda", "sedia a rotelle")


def product_fit(text: str) -> str:
    low = text.lower()
    hits = [(t, low.count(t)) for t in PRODUCT_TERMS if t in low]
    hits.sort(key=lambda x: -x[1])
    return ", ".join(f"{t} ({c})" for t, c in hits[:6])
