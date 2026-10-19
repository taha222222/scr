"""Per-company research pipeline (layers A-D) producing one JSON-serialisable result."""
from __future__ import annotations

import datetime as dt
import re
from dataclasses import asdict
from urllib.parse import urlsplit

import extractors as ex
import verification as vf
from crawler import REGION_NAMES, crawl_company
from http_client import PoliteSession, is_no_fetch
from providers import CreditCapReached
from search_engine import SearchBudgetExceeded, company_queries, manual_search_links, person_queries

DIRECTORY_HOSTS = ("yelp.", "yellowpages", "truelocal", "hotfrog", "cylex", "facebook.", "linkedin.", "instagram.",
                   "tripadvisor", "wikipedia.", "youtube.", "amazon.", "ebay.", "google.")


def _dom(url: str) -> str:
    h = (urlsplit(url).hostname or "").lower()
    return h[4:] if h.startswith("www.") else h


def _mentions_company(text: str, company: str, domain: str) -> bool:
    low = ex.fold(text)
    if domain and domain.lower() in low:
        return True
    if company:
        core = re.sub(r"\b(pty|ltd|limited|llc|inc|the|co)\b\.?", "", ex.fold(company)).strip(" ,.")
        if len(core) >= 5 and core in low:
            return True
    return False


def _linkedin_title(title: str) -> tuple[str, str, str] | None:
    """'Jane Doe - Managing Director - Acme Mobility | LinkedIn' -> (name, title, company)."""
    t = re.sub(r"\s*\|\s*LinkedIn.*$", "", title, flags=re.I)
    parts = [p.strip() for p in re.split(r"\s[-–—]\s", t) if p.strip()]
    if len(parts) >= 2:
        name = ex.clean_name(parts[0])
        if name:
            return name, parts[1], parts[2] if len(parts) > 2 else ""
    return None


class Researcher:
    def __init__(self, settings, http: PoliteSession, search, registry, hunter, apollo, gate):
        self.s, self.http, self.search, self.registry = settings, http, search, registry
        self.hunter, self.apollo, self.gate = hunter, apollo, gate

    # ------------------------------------------------------------------
    def research(self, key: str, url: str, domain: str, name_hint: str, row_kind: str) -> dict:
        res: dict = {
            "key": key, "input_url": url, "domain": domain, "row_kind": row_kind, "name_hint": name_hint,
            "confirmed_name": "", "legal_name": "", "country": "", "region": "", "region_basis": "",
            "product_fit": "", "identity_ok": False, "identity_note": "", "people": [], "emails": [], "phones": [],
            "primary": None, "email": None, "phone": None, "research_status": "", "confidence": "",
            "notes": [], "todo": [], "visited": [], "log": [], "sources": [], "manual_queries": [],
            "search_queries_used": 0, "paid_calls": [], "cloudflare_emails": False,
            "checked_at": dt.datetime.now().strftime("%Y-%m-%d %H:%M"),
        }
        log = lambda layer, target, detail: res["log"].append(  # noqa: E731
            {"time": dt.datetime.now().strftime("%H:%M:%S"), "layer": layer, "target": target, "detail": str(detail)[:400]})
        people: list[ex.PersonFinding] = []
        emails: list[ex.EmailFinding] = []
        phones: list[ex.PhoneFinding] = []
        q_used = 0

        # ---------------- identity for rows without a website
        if row_kind == "name_only":
            res["notes"].append(f"No website supplied; only the text '{name_hint}'.")
            if self.search.available:
                try:
                    results, used = self.search.search(f'"{name_hint}"', 10)
                    q_used += used
                    log("A-search", f'"{name_hint}"', f"{len(results)} results")
                    for r in results:
                        if not any(d in r.url for d in DIRECTORY_HOSTS):
                            url, domain = r.url, _dom(r.url)
                            res["input_url"], res["domain"] = url, domain
                            res["identity_note"] = (f"Website {domain} chosen from search result '{r.title}' - "
                                                    "confirm manually that this is the intended company.")
                            break
                except SearchBudgetExceeded as exc:
                    log("A-search", name_hint, exc)
            if not domain:
                res["research_status"] = vf.STATUS_UNRESOLVED
                res["confidence"] = "Low"
                res["todo"].append("Supply the company's website or full legal name, or configure a search API, then re-run.")
                res["manual_queries"] = [{"purpose": "Identify company", "query": f'"{name_hint}"', **manual_search_links(f'"{name_hint}"')}]
                return res

        # ---------------- Layer B: official website
        crawl = crawl_company(url, domain, self.http, self.s, log)
        res["visited"] = crawl.visited
        if crawl.final_domain != domain:
            res["notes"].append(f"Website redirects from {domain} to {crawl.final_domain}; research used {crawl.final_domain}.")
            domain = crawl.final_domain
            res["domain"] = domain
        ident = crawl.identity
        res["legal_name"] = ident.get("legal_name", "")
        res["confirmed_name"] = ex.best_company_name(ident, domain)
        res["region"], res["region_basis"] = crawl.region, crawl.region_basis
        res["country"] = REGION_NAMES.get(crawl.region, crawl.region)
        res["cloudflare_emails"] = crawl.cloudflare_emails
        all_text = "\n".join(p.text for p in crawl.pages)
        res["product_fit"] = ex.product_fit(all_text)
        if not crawl.pages:
            res["identity_note"] = res["identity_note"] or "Website could not be fetched (see Research_Log)."
        elif crawl.parked:
            res["identity_note"] = "Website looks parked / for sale / under construction."
        else:
            res["identity_ok"] = True
            if not res["product_fit"]:
                res["notes"].append("No mobility/medical product terms found on crawled pages - check product fit.")
        people += crawl.people
        emails += crawl.emails
        phones += crawl.phones
        for p in crawl.pages:
            res["sources"].append({"field": "Company website page", "value": p.title[:80], "url": p.url,
                                   "source_type": "official_website", "snippet": ""})
        if crawl.cloudflare_emails:
            res["notes"].append("Some emails on the site are hidden by Cloudflare email protection; they were not decoded. "
                                "Open the contact page in a browser to read them.")

        company = res["confirmed_name"] or name_hint or domain

        # ---------------- Official registry (UK)
        if self.registry.available and crawl.region == "GB" and res["identity_ok"]:
            reg = self.registry.lookup([res["legal_name"], ident.get("site_name", ""), res["confirmed_name"]])
            for n in reg["notes"]:
                log("registry", "Companies House", n)
            if reg["match"]:
                m = reg["match"]
                log("registry", m["url"], f"matched {m['title']} ({m['status']}, similarity {m['similarity']})")
                res["notes"].append(f"Companies House match: {m['title']} #{m['company_number']} ({m['status']}).")
                for o in reg["officers"]:
                    if "secretary" in o["role"].lower():
                        continue
                    people.append(ex.PersonFinding(o["name"], o["role"], ex.ROLE_EXEC, o["url"], "official_registry",
                                                   f"Companies House officer: {o['name']}, {o['role']}, appointed {o['appointed_on']}",
                                                   "strong"))

        # ---------------- Layer A: search discovery
        queries = company_queries(company if company != domain else "", domain, res["country"])
        budget = self.s.max_search_queries_per_company
        third_party_fetched = 0
        if self.search.available and res["identity_ok"]:
            for purpose, q in queries[: max(0, budget - 2)]:
                try:
                    results, used = self.search.search(q, 10)
                except SearchBudgetExceeded as exc:
                    log("A-search", q, exc)
                    res["todo"].append("Total search budget reached - re-run later to continue search layer.")
                    break
                q_used += used
                log("A-search", q, f"{len(results)} results ({purpose})")
                for r in results:
                    blob = f"{r.title}\n{r.snippet}"
                    rdom = _dom(r.url)
                    official = rdom == domain or rdom.endswith("." + domain)
                    li = _linkedin_title(r.title) if "linkedin." in rdom else None
                    if li and _mentions_company(blob, company, domain):
                        cls = ex.classify_title(li[1])
                        if cls:
                            people.append(ex.PersonFinding(li[0], li[1], cls[0], r.url, "search_snippet", blob[:300], "strong"))
                    elif _mentions_company(blob, company, domain) or official:
                        for pf in ex.extract_people(r.snippet, r.url, "search_snippet"):
                            people.append(pf)
                    for m in ex.EMAIL_RE.finditer(r.snippet):
                        addr = m.group(0).lower()
                        if addr.endswith("@" + domain):
                            local = addr.split("@")[0]
                            kind = "generic" if local in ex.GENERIC_LOCALPARTS else "personal"
                            emails.append(ex.EmailFinding(addr, kind, r.url, "search_snippet", r.snippet[:300]))
                    # fetch a few relevant pages (company's own uncrawled pages, or third-party pages naming the company)
                    if (official and r.url not in [v[0] for v in crawl.visited]) or (
                            not official and third_party_fetched < self.s.max_third_party_pages_per_company
                            and not is_no_fetch(r.url) and _mentions_company(blob, company, domain)
                            and ex.classify_title(blob)):
                        third_party_fetched += 0 if official else 1
                        self._fetch_extra(r.url, official, company, domain, crawl.region, people, emails, phones, res, log)

        # ---------------- aggregate & person-specific follow-up
        agg = vf.aggregate_people(people)
        top = sorted(agg, key=lambda p: (p["role_category"] != ex.ROLE_EXEC, p["role_category"] != ex.ROLE_PURCHASING))
        followups = []
        for role in (ex.ROLE_EXEC, ex.ROLE_PURCHASING):
            cand = [p for p in top if p["role_category"] == role]
            if cand:
                followups.append(cand[0])
        if self.search.available:
            for p in followups:
                for purpose, q in person_queries(p["name"], company, domain):
                    if q_used >= budget:
                        break
                    try:
                        results, used = self.search.search(q, 10)
                    except SearchBudgetExceeded as exc:
                        log("C-verify", q, exc)
                        break
                    q_used += used
                    log("C-verify", q, f"{len(results)} results ({purpose})")
                    for r in results:
                        txt = f"{r.title}\n{r.snippet}"
                        if ex.fold(p["name"]) not in ex.fold(txt):
                            continue
                        for m in ex.EMAIL_RE.finditer(r.snippet):
                            addr = m.group(0).lower()
                            if addr.split("@")[1] == domain:
                                emails.append(ex.EmailFinding(addr, "personal" if addr.split("@")[0] not in ex.GENERIC_LOCALPARTS else "generic",
                                                              r.url, "search_snippet", r.snippet[:300]))
                        for ph in ex.extract_phones(r.snippet, r.url, "search_snippet", crawl.region or None, [p["name"]]):
                            if ph.person_name:
                                phones.append(ph)
                        people.append(ex.PersonFinding(p["name"], p["title"], p["role_category"], r.url, "search_snippet", txt[:300], "weak"))

        # ---------------- Layer D: optional providers (only if approved)
        if self.gate.approved and res["identity_ok"]:
            self._paid_layer(domain, people, emails, phones, res, log)

        res["search_queries_used"] = q_used

        # ---------------- verification & selection
        agg = vf.aggregate_people(people)
        phones = vf.attribute_contacts(agg, emails, phones)
        emails = _dedupe_emails(emails)
        primary = vf.choose_primary(agg, emails, phones)
        email = vf.pick_email(primary, emails, domain)
        phone = vf.pick_phone(primary, phones)

        for p in agg:
            status, conf = vf.person_status(p)
            res["people"].append({
                "name": p["name"], "title": p["title"], "all_titles": p["titles"], "role_category": p["role_category"],
                "status": status, "confidence": conf, "conflicts": p["conflicts"],
                "sources": [{"url": s.source_url, "source_type": s.source_type, "snippet": s.snippet, "pattern": s.pattern} for s in p["sources"]],
            })
            for s in p["sources"]:
                res["sources"].append({"field": f"Person: {p['name']} ({s.title})", "value": p["name"], "url": s.source_url,
                                       "source_type": s.source_type, "snippet": s.snippet})
        for e in emails:
            d = asdict(e)
            d["evidence"] = vf.email_evidence(e, domain, self.s.dns_checks_enabled)
            res["emails"].append(d)
            res["sources"].append({"field": "Email", "value": e.email, "url": e.source_url, "source_type": e.source_type, "snippet": e.context})
        for ph in phones:
            d = asdict(ph)
            d["evidence"] = vf.phone_evidence(ph)
            res["phones"].append(d)
            res["sources"].append({"field": f"Phone ({ph.phone_type})", "value": ph.display, "url": ph.source_url,
                                   "source_type": ph.source_type, "snippet": ph.context})

        if primary:
            res["primary"] = next(i for i, p in enumerate(res["people"]) if p["name"] == primary["name"])
        if email:
            res["email"] = next(i for i, e in enumerate(res["emails"]) if e["email"] == email.email and e["source_url"] == email.source_url)
        if phone:
            res["phone"] = next(i for i, p in enumerate(res["phones"]) if p["e164"] == phone.e164 and p["extension"] == phone.extension
                                and p["source_url"] == phone.source_url)

        self._status_and_notes(res, primary, email, phone)

        # Manual search links for every company (for the human follow-up).
        mq = [{"purpose": purpose, "query": q, **manual_search_links(q)} for purpose, q in queries]
        for p in followups:
            mq += [{"purpose": purpose, "query": q, **manual_search_links(q)} for purpose, q in person_queries(p["name"], company, domain)]
        res["manual_queries"] = mq
        return res

    # ------------------------------------------------------------------
    def _fetch_extra(self, url, official, company, domain, region, people, emails, phones, res, log):
        r = self.http.fetch(url)
        res["visited"].append((url, ("ok" if r.ok else r.error or f"HTTP {r.status}") + (" (official, from search)" if official else " (third-party)")))
        if not r.ok or "html" not in r.content_type:
            return
        soup = ex.page_soup(r.text)
        text = ex.visible_text(ex.page_soup(r.text))
        st = "official_website" if official else "third_party_page"
        if not official and not _mentions_company(text, company, domain):
            log("A-thirdparty", url, "page does not mention the company; ignored")
            return
        found = ex.extract_people(text, url, st, soup)
        if not official:
            # keep only people whose line of text mentions the company, to avoid attributing unrelated names
            found = [f for f in found if _mentions_company(f.snippet, company, domain) or _near_company(text, f.name, company, domain)]
        people += found
        names = [f.name for f in found]
        es, _ = ex.extract_emails(r.text, text, url, st, soup)
        emails += [e for e in es if official or e.email.endswith("@" + domain)]
        for ph in ex.extract_phones(text, url, st, region or None, names, ex.page_soup(r.text)):
            if official or ph.person_name:
                phones.append(ph)
        log("A-thirdparty" if not official else "B-crawl", url, f"{len(found)} people, {len(es)} emails")

    def _paid_layer(self, domain, people, emails, phones, res, log):
        have_named_email = any(e.kind == "personal" for e in emails)
        try:
            if self.hunter.available and not have_named_email:
                for h in self.hunter.domain_search(domain, self.gate):
                    res["paid_calls"].append(self.gate.ledger[-1])
                    src = h["sources"][0] if h["sources"] else "Hunter.io domain search (no public URL given)"
                    cls = ex.classify_title(h["title"] or "")
                    if h["name"] and cls:
                        people.append(ex.PersonFinding(h["name"], h["title"], cls[0], src, "data_provider", f"Hunter: {h['name']} - {h['title']}", "strong"))
                    if h["email"]:
                        e = ex.EmailFinding(h["email"], "personal" if h["name"] else "generic", src, "data_provider", h["email_status"])
                        emails.append(e)
                log("D-provider", "Hunter.io", f"domain search done; credits so far {self.gate.spent}")
            if self.apollo.available:
                found = self.apollo.people_search(domain, self.gate)
                log("D-provider", "Apollo.io", f"people search returned {len(found)} people (no contact data)")
                for p in found[:1]:
                    if p.get("has_email") and p.get("apollo_id"):
                        m = self.apollo.people_match(p["apollo_id"], domain, self.gate)
                        res["paid_calls"].append(self.gate.ledger[-1])
                        cls = ex.classify_title(m["title"] or "")
                        if m["name"] and cls:
                            people.append(ex.PersonFinding(m["name"], m["title"], cls[0], m.get("linkedin_url") or "Apollo.io people/match",
                                                           "data_provider", f"Apollo: {m['name']} - {m['title']}", "strong"))
                        if m["email"]:
                            emails.append(ex.EmailFinding(m["email"], "personal", "Apollo.io people/match", "data_provider", m["email_status"]))
        except CreditCapReached as exc:
            log("D-provider", "paid", exc)
            res["notes"].append(f"Paid lookup stopped: {exc}")
        except Exception as exc:  # provider outages must not stop the run
            log("D-provider", "error", exc)

    def _status_and_notes(self, res, primary, email, phone):
        notes, todo = res["notes"], res["todo"]
        if not res["identity_ok"]:
            res["research_status"], res["confidence"] = vf.STATUS_UNRESOLVED, "Low"
            if res["identity_note"]:
                notes.insert(0, res["identity_note"])
            todo.append("Confirm the correct website / company manually (see Manual_Search_Links).")
            return
        if res["identity_note"]:
            notes.insert(0, res["identity_note"])
        if primary:
            p = res["people"][res["primary"]]
            status, conf = p["status"], p["confidence"]
            direct_email = email is not None and email.person_name == primary["name"]
            direct_phone = phone is not None and phone.person_name == primary["name"]
            if status == vf.STATUS_OFFICIAL and conf == "Medium" and (direct_email or direct_phone) \
                    and any(s["source_type"] in ("official_website", "official_registry") for s in p["sources"]):
                conf = "High"
            res["research_status"], res["confidence"] = status, conf
            if p["conflicts"]:
                notes.append(p["conflicts"])
            if any(s["pattern"] == "weak" for s in p["sources"]) and not any(s["pattern"] == "strong" for s in p["sources"]):
                notes.append("Weak evidence: name/title pairing inferred from page layout, or surname taken from a profile URL - review the snippet in Sources.")
            if not direct_email:
                todo.append(f"Named work email for {p['name']} not found publicly.")
            if not direct_phone:
                todo.append(f"Direct line / business mobile for {p['name']} not found publicly.")
            others = [x for x in res["people"] if x["name"] != p["name"]]
            purch = [x for x in others if x["role_category"] == ex.ROLE_PURCHASING]
            if purch:
                notes.append(f"Purchasing contact also found: {purch[0]['name']} ({purch[0]['title']}) - see All_Contacts.")
            elif p["role_category"] != ex.ROLE_PURCHASING:
                todo.append("No purchasing/procurement manager identified (common for small businesses where the owner buys).")
            if others and not purch:
                notes.append(f"{len(others)} other named people found - see All_Contacts.")
        elif email or phone:
            res["research_status"], res["confidence"] = vf.STATUS_GENERIC, "Low"
            notes.append("Direct decision-maker contact not found.")
            if email is not None and email.kind == "personal":
                notes.append(f"{email.email} looks like an individual's mailbox but the person's name/role is not "
                             "published - worth trying, but it is not confirmed as a decision-maker.")
            todo.append("Owner/CEO/purchasing manager not named on the public sources checked.")
        else:
            res["research_status"], res["confidence"] = vf.STATUS_NOT_FOUND, "Low"
            notes.append("No named person, email or phone found on the pages checked.")
        if not self.search.available:
            todo.append("Search-engine layer not run (no search API configured) - use Manual_Search_Links or add a key.")
        if not self.gate.approved and (self.hunter.available or self.apollo.available):
            todo.append("Paid database lookup possible but not approved for this run.")


def _near_company(text: str, name: str, company: str, domain: str) -> bool:
    i = ex.fold(text).find(ex.fold(name))
    return i >= 0 and _mentions_company(text[max(0, i - 300): i + 300], company, domain)


def _dedupe_emails(emails: list[ex.EmailFinding]) -> list[ex.EmailFinding]:
    rank = {"official_website": 0, "data_provider": 1, "third_party_page": 2, "search_snippet": 3}
    best: dict[str, ex.EmailFinding] = {}
    for e in emails:
        cur = best.get(e.email)
        if cur is None or rank.get(e.source_type, 9) < rank.get(cur.source_type, 9) or (e.person_name and not cur.person_name):
            if cur is not None and cur.person_name and not e.person_name:
                e.person_name, e.attribution = cur.person_name, cur.attribution
            best[e.email] = e
    return list(best.values())
