"""Evidence checks, de-duplication, contact selection and confidence scoring.

Status labels (research status column):
  Verified from official source        - person + title stated on the company's own site or an official registry
  Supported by multiple public sources - person found on 2+ independent sources (none official)
  Single-source evidence               - person found on one public page
  Unverified candidate                 - person only seen in a search snippet / weakly parsed text
  Generic company contact only         - no named person, only generic mailbox / main line
  Not found                            - nothing usable found
  Company identity unresolved          - website missing, unreachable, parked or ambiguous
"""
from __future__ import annotations

import re
from functools import lru_cache
from urllib.parse import urlsplit

import extractors as ex

STATUS_OFFICIAL = "Verified from official source"
STATUS_MULTI = "Supported by multiple public sources"
STATUS_SINGLE = "Single-source evidence"
STATUS_CANDIDATE = "Unverified candidate"
STATUS_GENERIC = "Generic company contact only"
STATUS_NOT_FOUND = "Not found"
STATUS_UNRESOLVED = "Company identity unresolved"

SPECIFIC_EXEC = re.compile(r"ceo|chief executive|founder|owner|proprietor|managing director|president|general manager|"
                           r"managing partner|gerente general|fundador|propietari|due[ñn]o|g[ée]rant|gesch|inhaber|"
                           r"amministratore|titolare|director general|directeur", re.I)


# ------------------------------------------------------------------ emails
try:
    from email_validator import EmailNotValidError, validate_email
except ImportError:  # optional
    validate_email = None

try:
    import dns.resolver
except ImportError:  # optional
    dns = None


@lru_cache(maxsize=1)
def dns_available() -> bool:
    """Self-test: can this machine resolve MX records at all? (Some networks block DNS.)"""
    if dns is None:
        return False
    try:
        dns.resolver.resolve("gmail.com", "MX", lifetime=6)
        return True
    except Exception:
        return False


@lru_cache(maxsize=512)
def mx_status(domain: str, enabled: bool = True) -> str:
    if not enabled:
        return "MX check disabled"
    if dns is None:
        return "MX check unavailable (dnspython not installed)"
    if not dns_available():
        return "MX check unavailable (DNS lookups blocked on this network)"
    try:
        answers = dns.resolver.resolve(domain, "MX", lifetime=8)
        return f"domain has MX records ({len(answers)})"
    except dns.resolver.NXDOMAIN:
        return "domain does not exist (NXDOMAIN)"
    except dns.resolver.NoAnswer:
        return "no MX records found for domain"
    except Exception as exc:
        return f"MX check failed ({type(exc).__name__})"


def syntax_ok(email: str) -> bool:
    if validate_email is not None:
        try:
            validate_email(email, check_deliverability=False)
            return True
        except EmailNotValidError:
            return False
    return bool(ex.EMAIL_RE.fullmatch(email))


def email_evidence(f: ex.EmailFinding, company_domain: str, dns_enabled: bool) -> str:
    dom = f.email.split("@", 1)[1]
    where = {
        "official_website": "Published on official website",
        "third_party_page": f"Published on third-party page ({urlsplit(f.source_url).hostname})",
        "search_snippet": "Seen in search-result snippet only",
        "data_provider": "Returned by data provider",
    }.get(f.source_type, f.source_type)
    parts = [where]
    if f.kind == "generic":
        parts.append("generic/department mailbox - not personal")
    elif f.person_name:
        parts.append(f"attributed to {f.person_name} ({f.attribution})")
    else:
        parts.append("personal-looking mailbox; owner not identified")
    parts.append("syntax valid" if syntax_ok(f.email) else "SYNTAX INVALID")
    on_domain = dom == company_domain or dom.endswith("." + company_domain)
    parts.append("on company domain" if on_domain else f"different domain ({dom})")
    parts.append(mx_status(dom, dns_enabled))
    parts.append("deliverability NOT verified (no SMTP probing)")
    return "; ".join(parts)


# ------------------------------------------------------------------ people
def _domain_of(url: str) -> str:
    h = (urlsplit(url).hostname or "").lower()
    return h[4:] if h.startswith("www.") else h


def aggregate_people(findings: list[ex.PersonFinding]) -> list[dict]:
    people: dict[str, dict] = {}
    for f in findings:
        key = ex.fold(f.name)
        p = people.setdefault(key, {"name": f.name, "titles": [], "role_categories": set(), "sources": [],
                                    "domains": set(), "official": False, "official_strong": False,
                                    "registry": False})
        if f.title and f.title.lower() not in [t.lower() for t in p["titles"]]:
            p["titles"].append(f.title)
        p["role_categories"].add(f.role_category)
        p["sources"].append(f)
        p["domains"].add("registry" if f.source_type == "official_registry" else _domain_of(f.source_url))
        if f.source_type == "official_website":
            p["official"] = True
            p["official_strong"] |= f.pattern == "strong"
        if f.source_type == "official_registry":
            p["registry"] = True
    for p in people.values():
        p["role_category"] = (ex.ROLE_EXEC if ex.ROLE_EXEC in p["role_categories"]
                              else ex.ROLE_PURCHASING if ex.ROLE_PURCHASING in p["role_categories"]
                              else ex.ROLE_OTHER)
        p["title"] = _best_title(p["titles"])
        p["conflicts"] = _title_conflicts(p["titles"])
    return list(people.values())


def _best_title(titles: list[str]) -> str:
    for t in titles:
        if SPECIFIC_EXEC.search(t):
            return t
    return titles[0] if titles else ""


def _title_conflicts(titles: list[str]) -> str:
    cats = {ex.classify_title(t)[0] for t in titles if ex.classify_title(t)}
    if len(cats) > 1:
        return "Sources give different roles: " + " / ".join(titles)
    return ""


def person_status(p: dict) -> tuple[str, str]:
    """Return (status, confidence) for a person's identity + role."""
    if p["registry"] or (p["official"] and p["official_strong"]):
        conf = "High" if (p["official"] and p["registry"]) or len(p["domains"]) >= 2 else "Medium"
        return STATUS_OFFICIAL, conf
    if len(p["domains"]) >= 2:
        return STATUS_MULTI, "Medium"
    if p["official"]:
        return STATUS_SINGLE, "Low"   # weak parse on the official site: needs manual review
    if all(s.source_type == "search_snippet" for s in p["sources"]):
        return STATUS_CANDIDATE, "Low"
    return STATUS_SINGLE, "Low"


def attribute_contacts(people: list[dict], emails: list[ex.EmailFinding], phones: list[ex.PhoneFinding]) -> list[ex.PhoneFinding]:
    """Attach emails to people only on name evidence; downgrade phones that are also company lines.

    Returns the phone list de-duplicated to one finding per number."""
    for e in emails:
        if e.kind != "personal" or e.person_name:
            continue
        best = ("", "")
        for p in people:
            why = ex.email_matches_person(e.email, p["name"])
            if why and (not best[0] or "first name only" in best[1]):
                best = (p["name"], why)
        e.person_name, e.attribution = best
        if best[0] and e.source_type == "official_website" and "first name only" not in best[1]:
            for p in people:
                if p["name"] == best[0]:
                    # a work email built from the person's name on the official site corroborates the layout parse
                    p["official_strong"] = p["official_strong"] or p["official"]

    by_number: dict[str, list[ex.PhoneFinding]] = {}
    for ph in phones:
        by_number.setdefault(ph.e164 + ph.extension, []).append(ph)
    for group in by_number.values():
        unattributed = [g for g in group if not g.person_name]
        attributed = [g for g in group if g.person_name]
        switchboard = [g for g in unattributed if g.phone_type.startswith("Switchboard")]
        department = [g for g in unattributed if g.phone_type.startswith("Department")]
        if attributed and department and not switchboard:
            # conflicting labels: keep the person link but say so
            for g in attributed:
                g.phone_type += " - conflict: also printed as a department line"
                g.context += "  || elsewhere: " + department[0].context[-160:]
            continue
        if attributed and (switchboard or any(g.number_kind == "toll_free" for g in group)):
            for g in attributed:
                g.phone_type = f"Switchboard / main line (also printed next to {g.person_name})"
                g.person_name = ""
    src = {"official_website": 0, "data_provider": 1, "third_party_page": 2, "search_snippet": 3}
    return [sorted(g, key=lambda p: (not p.person_name, src.get(p.source_type, 9), phone_rank(p)))[0]
            for g in by_number.values()]


ROLE_SCORE = {ex.ROLE_EXEC: 50, ex.ROLE_PURCHASING: 45, ex.ROLE_OTHER: 20}
PHONE_RANK = ["Direct dial", "Business mobile", "Extension", "Direct dial (person not stated)",
              "Extension (person not stated)", "Department line", "Company-listed mobile", "Switchboard"]


def phone_rank(ph: ex.PhoneFinding) -> int:
    for i, prefix in enumerate(PHONE_RANK):
        if ph.phone_type.startswith(prefix):
            return i
    return len(PHONE_RANK)


def choose_primary(people: list[dict], emails: list[ex.EmailFinding], phones: list[ex.PhoneFinding]) -> dict | None:
    def score(p):
        s = ROLE_SCORE[p["role_category"]]
        if p["role_category"] == ex.ROLE_EXEC and not SPECIFIC_EXEC.search(p["title"] or ""):
            s -= 15  # bare 'Director'
        if any(e.person_name == p["name"] for e in emails):
            s += 30
        if any(ph.person_name == p["name"] for ph in phones):
            s += 30
        status, _ = person_status(p)
        s += {STATUS_OFFICIAL: 20, STATUS_MULTI: 15, STATUS_SINGLE: 8, STATUS_CANDIDATE: 0}.get(status, 0)
        return s
    if not people:
        return None
    return max(people, key=score)


def pick_email(person: dict | None, emails: list[ex.EmailFinding], company_domain: str) -> ex.EmailFinding | None:
    def on_domain(e):
        d = e.email.split("@", 1)[1]
        return d == company_domain or d.endswith("." + company_domain)

    def src_rank(e):
        return {"official_website": 0, "data_provider": 1, "third_party_page": 2, "search_snippet": 3}.get(e.source_type, 4)

    if person:
        mine = [e for e in emails if e.person_name == person["name"]]
        if mine:
            return sorted(mine, key=lambda e: ("first name only" in e.attribution, src_rank(e)))[0]
    personal = [e for e in emails if e.kind == "personal" and not e.person_name and on_domain(e)]
    if personal and not person:
        return sorted(personal, key=src_rank)[0]
    generic = [e for e in emails if e.kind == "generic" and on_domain(e)]
    order = ["purchasing", "procurement", "buying", "compras", "info", "sales", "enquiries", "contact", "hello", "office"]

    def g_rank(e):
        local = e.email.split("@")[0]
        return (order.index(local) if local in order else len(order), src_rank(e))
    if generic:
        return sorted(generic, key=g_rank)[0]
    return sorted(personal, key=src_rank)[0] if personal else None


def pick_phone(person: dict | None, phones: list[ex.PhoneFinding]) -> ex.PhoneFinding | None:
    if not phones:
        return None
    if person:
        mine = [p for p in phones if p.person_name == person["name"]]
        if mine:
            return sorted(mine, key=phone_rank)[0]
    candidates = [p for p in phones if not p.person_name]
    src = {"official_website": 0, "data_provider": 1, "third_party_page": 2}
    return sorted(candidates or phones, key=lambda p: (phone_rank(p), src.get(p.source_type, 3)))[0]


def phone_evidence(ph: ex.PhoneFinding) -> str:
    where = {"official_website": "Printed on official website",
             "third_party_page": f"Printed on third-party page ({urlsplit(ph.source_url).hostname})",
             "data_provider": "Returned by data provider"}.get(ph.source_type, ph.source_type)
    parts = [where]
    if ph.person_name:
        parts.append(f"printed next to {ph.person_name}")
    parts.append(f"numbering plan type: {ph.number_kind}")
    parts.append("number not dialled/verified")
    return "; ".join(parts)
