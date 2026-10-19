"""Offline tests: run with `python -m pytest tests` from the project folder."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import extractors as ex  # noqa: E402
import verification as vf  # noqa: E402
from input_loader import load_companies, normalize_url  # noqa: E402

FIXTURE = Path(__file__).parent / "fixtures" / "team_page.html"


def test_normalize_url_repairs():
    assert normalize_url("/https://eroutemobility.com/")[1] == "eroutemobility.com"
    url, dom, fixes = normalize_url("www.mobot.sg")
    assert url == "https://www.mobot.sg/" and dom == "mobot.sg" and fixes
    assert normalize_url("https://x.com/?utm_source=a&id=3")[0] == "https://x.com/?id=3"
    assert normalize_url("beach wheel chair")[0] == ""


def test_loader_sample():
    rows, info = load_companies(Path(__file__).parents[1] / "sample_data" / "sample_companies.csv")
    kinds = [r.row_kind for r in rows]
    assert "section_label" in kinds and "name_only" in kinds
    assert info["duplicate_rows"] == 1
    assert rows[0].row_number == 2


def _page():
    html = FIXTURE.read_text()
    soup = ex.page_soup(html)
    text = ex.visible_text(ex.page_soup(html))
    return html, soup, text


def test_people_extraction():
    html, soup, text = _page()
    people = ex.extract_people(text, "https://acme.com.au/team", "official_website", ex.page_soup(html))
    names = {p.name: p for p in people}
    assert "Jane Citizen" in names and names["Jane Citizen"].role_category == ex.ROLE_EXEC
    assert "Tom Baker" in names and names["Tom Baker"].role_category == ex.ROLE_PURCHASING
    assert "Maria de la Cruz" in names
    assert not any("Mobility" in n for n in names)


def test_email_and_phone_attribution():
    html, soup, text = _page()
    emails, cf = ex.extract_emails(html, text, "https://acme.com.au/team", "official_website", ex.page_soup(html))
    got = {e.email: e for e in emails}
    assert got["info@acme.com.au"].kind == "generic"
    assert got["jane.citizen@acme.com.au"].kind == "personal"
    assert "logo@2x.png" not in " ".join(got)
    assert cf is True
    assert ex.email_matches_person("jane.citizen@acme.com.au", "Jane Citizen")
    assert ex.email_matches_person("tbaker@acme.com.au", "Tom Baker")
    assert not ex.email_matches_person("info@acme.com.au", "Tom Baker")

    people = vf.aggregate_people(ex.extract_people(text, "https://acme.com.au/team", "official_website"))
    phones = ex.extract_phones(text, "https://acme.com.au/team", "official_website", "AU", [p["name"] for p in people])
    by = {p.e164: p for p in phones}
    assert by["+61412345678"].person_name == "Jane Citizen" and by["+61412345678"].phone_type == "Business mobile"
    assert by["+61755551234"].extension == "205"
    # ABN must not be read as a phone number
    assert all(not p.e164.startswith("+6151") for p in phones)
    phones = vf.attribute_contacts(people, emails, phones)
    by = {p.e164: p for p in phones}
    assert len(by) == len(phones)
    # main line printed both next to a person and in the header -> switchboard, not direct
    assert by["+61755550000"].person_name == ""
    assert by["+61755550000"].phone_type.startswith("Switchboard")
    assert by["+61412345678"].person_name == "Jane Citizen"
    primary = vf.choose_primary(people, emails, phones)
    assert primary["name"] == "Jane Citizen"
    assert vf.pick_email(primary, emails, "acme.com.au").email == "jane.citizen@acme.com.au"
    status, conf = vf.person_status(primary)
    assert status == vf.STATUS_OFFICIAL


def test_no_person_gives_generic():
    emails = [ex.EmailFinding("sales@x.com", "generic", "u", "official_website", "")]
    assert vf.choose_primary([], emails, []) is None
    assert vf.pick_email(None, emails, "x.com").email == "sales@x.com"


def test_identity():
    html, soup, text = _page()
    ident = ex.company_identity(ex.page_soup(html), text, "acme.com.au")
    assert ex.best_company_name(ident, "acme.com.au") == "Acme Mobility Pty Ltd"


def test_linkedin_title_and_company_match():
    from research import _linkedin_title, _mentions_company
    assert _linkedin_title("Jane Doe - Managing Director - Acme Mobility | LinkedIn")[:2] == ("Jane Doe", "Managing Director")
    assert _linkedin_title("Acme Mobility Solutions - Overview | LinkedIn") is None
    assert _mentions_company("works at Acme Mobility Pty Ltd", "Acme Mobility Pty Ltd", "acme.com.au")
    assert not _mentions_company("works at Other Co", "Acme Mobility Pty Ltd", "acme.com.au")


def test_paid_gate_blocks_without_approval():
    import pytest
    from providers import CreditCapReached, PaidGate
    with pytest.raises(CreditCapReached):
        PaidGate(False, 10).spend("x", 1, "test")
    g = PaidGate(True, 1)
    g.spend("x", 1, "ok")
    with pytest.raises(CreditCapReached):
        g.spend("x", 0.5, "over cap")


def test_pipeline_with_fake_search(monkeypatch, tmp_path):
    """Exercise layers A-C end to end without network access."""
    import research
    from config import Settings
    from crawler import CrawlResult, Page
    from http_client import FetchResult
    from providers import PaidGate
    from search_engine import SearchResult

    html, soup, text = _page()

    def fake_crawl(url, domain, http, settings, log):
        r = CrawlResult(start_url=url, final_domain=domain)
        r.pages = [Page("https://acme.com.au/team", "Our Team", text)]
        r.identity = {"legal_name": "Acme Mobility Pty Ltd", "site_name": "Acme Mobility"}
        r.region, r.region_basis = "AU", "test"
        r.people = ex.extract_people(text, "https://acme.com.au/team", "official_website", ex.page_soup(html))
        r.emails, _ = ex.extract_emails(html, text, "https://acme.com.au/team", "official_website", ex.page_soup(html))
        r.phones = ex.extract_phones(text, "https://acme.com.au/team", "official_website", "AU", [p.name for p in r.people])
        return r

    class FakeSearch:
        name, available, total_queries = "fake", True, 0

        def search(self, q, n):
            self.total_queries += 1
            return [SearchResult("Sam Buyer - Senior Buyer - Acme Mobility | LinkedIn", "https://au.linkedin.com/in/sam",
                                 "Senior Buyer at Acme Mobility Pty Ltd", "fake", q)], True

    class FakeHttp:
        def fetch(self, url, **k):
            return FetchResult(url, error="offline test")

    class Off:
        available = False

    monkeypatch.setattr(research, "crawl_company", fake_crawl)
    s = Settings(work_dir=tmp_path)
    res = research.Researcher(s, FakeHttp(), FakeSearch(), Off(), Off(), Off(), PaidGate(False, 0)).research(
        "acme.com.au", "https://acme.com.au/", "acme.com.au", "", "website")
    names = {p["name"]: p for p in res["people"]}
    assert names["Sam Buyer"]["status"] == vf.STATUS_CANDIDATE
    assert res["people"][res["primary"]]["name"] == "Jane Citizen"
    assert res["research_status"] == vf.STATUS_OFFICIAL and res["confidence"] == "High"
    assert res["emails"][res["email"]]["email"] == "jane.citizen@acme.com.au"
    assert res["phones"][res["phone"]]["phone_type"] == "Business mobile"
    assert res["search_queries_used"] <= s.max_search_queries_per_company
    assert res["manual_queries"]
