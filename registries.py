"""Official company registries (free, key-based).

Currently: UK Companies House public data API (free key from
https://developer.company-information.service.gov.uk/). Only the officer's
name, role and appointment date are kept. Addresses, dates of birth and
nationality are deliberately discarded.
"""
from __future__ import annotations

import difflib
import re
import time

import requests

from config import Settings

CH_BASE = "https://api.company-information.service.gov.uk"


def _norm(name: str) -> str:
    name = name.lower()
    name = re.sub(r"\b(ltd|limited|plc|llp|the|uk|co)\b\.?", "", name)
    return re.sub(r"[^a-z0-9]+", " ", name).strip()


def _officer_name(raw: str) -> str:
    # "SMITH, John Andrew" -> "John Andrew Smith"
    if "," in raw:
        last, first = raw.split(",", 1)
        return f"{first.strip().title()} {last.strip().title()}".strip()
    return raw.title()


class CompaniesHouse:
    def __init__(self, settings: Settings):
        self.key = settings.companies_house_api_key
        self.available = bool(self.key)

    def _get(self, path: str, **params):
        time.sleep(0.6)  # limit is 600 requests / 5 minutes
        r = requests.get(CH_BASE + path, params=params, auth=(self.key, ""), timeout=20)
        if r.status_code == 429:
            raise RuntimeError("Companies House rate limit reached; stopping registry lookups")
        r.raise_for_status()
        return r.json()

    def lookup(self, company_names: list[str]) -> dict:
        """Return {'match': {...} | None, 'officers': [...], 'notes': [...]}."""
        out = {"match": None, "officers": [], "notes": []}
        if not self.available:
            out["notes"].append("Companies House key not configured")
            return out
        best = None
        for name in [n for n in company_names if n]:
            try:
                data = self._get("/search/companies", q=name, items_per_page=10)
            except (requests.RequestException, RuntimeError) as exc:
                out["notes"].append(f"Companies House search failed: {exc}")
                return out
            for item in data.get("items", []):
                ratio = difflib.SequenceMatcher(None, _norm(name), _norm(item.get("title", ""))).ratio()
                if best is None or ratio > best[0]:
                    best = (ratio, item, name)
        if not best or best[0] < 0.85:
            out["notes"].append("No Companies House entity matched the website's company name closely enough (>= 0.85)")
            return out
        ratio, item, searched = best
        out["match"] = {
            "company_number": item.get("company_number"),
            "title": item.get("title"),
            "status": item.get("company_status"),
            "url": f"https://find-and-update.company-information.service.gov.uk/company/{item.get('company_number')}",
            "similarity": round(ratio, 2),
            "searched_name": searched,
        }
        if item.get("company_status") != "active":
            out["notes"].append(f"Matched entity status is '{item.get('company_status')}' - check whether the business still trades")
        try:
            officers = self._get(f"/company/{item['company_number']}/officers", items_per_page=50)
        except (requests.RequestException, RuntimeError) as exc:
            out["notes"].append(f"Officer lookup failed: {exc}")
            return out
        for o in officers.get("items", []):
            if o.get("resigned_on"):
                continue
            out["officers"].append({
                "name": _officer_name(o.get("name", "")),
                "role": (o.get("officer_role") or "").replace("-", " ").title(),
                "appointed_on": o.get("appointed_on", ""),
                "url": out["match"]["url"] + "/officers",
            })
        return out
