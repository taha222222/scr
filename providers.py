"""Optional contact-data providers (Hunter, Apollo).

Nothing in this module runs unless:
  1. the provider's API key is configured in .env, AND
  2. the run was started with --allow-paid, AND
  3. the operator typed APPROVE at the interactive prompt that shows the
     estimated credit use and the balance reported by the provider, AND
  4. the per-run cap MAX_PAID_CREDITS_PER_RUN has not been reached.

A connection between Apollo and Claude (or any other app) does NOT give this
script access: the script only uses an API key you put in .env yourself.
Provider plan limits are never worked around; a 402/403/429 stops the provider.
"""
from __future__ import annotations

import requests

from config import Settings


class CreditCapReached(Exception):
    pass


class PaidGate:
    def __init__(self, approved: bool, max_credits: float):
        self.approved = approved
        self.max_credits = max_credits
        self.spent = 0.0
        self.ledger: list[dict] = []

    def spend(self, provider: str, credits: float, purpose: str) -> None:
        if not self.approved:
            raise CreditCapReached("Paid lookups not approved for this run")
        if self.spent + credits > self.max_credits:
            raise CreditCapReached(f"Per-run cap of {self.max_credits} credits would be exceeded")
        self.spent += credits
        self.ledger.append({"provider": provider, "estimated_credits": credits, "purpose": purpose})


class Hunter:
    name = "Hunter.io"
    BASE = "https://api.hunter.io/v2"
    COST = {"domain_search": 1.0, "email_finder": 1.0, "email_verifier": 0.5}

    def __init__(self, settings: Settings):
        self.key = settings.hunter_api_key
        self.available = bool(self.key)
        self.disabled_reason = ""

    def account(self) -> dict:
        """Free call: plan and remaining quota."""
        r = requests.get(f"{self.BASE}/account", params={"api_key": self.key}, timeout=20)
        r.raise_for_status()
        d = r.json().get("data", {})
        return {"plan": d.get("plan_name"), "requests": d.get("requests"), "reset_date": d.get("reset_date")}

    def _call(self, endpoint: str, gate: PaidGate, purpose: str, **params) -> dict:
        if self.disabled_reason:
            raise CreditCapReached(self.disabled_reason)
        gate.spend(self.name, self.COST[endpoint], purpose)
        r = requests.get(f"{self.BASE}/{endpoint.replace('_', '-')}", params={**params, "api_key": self.key}, timeout=30)
        if r.status_code in (402, 403, 429):
            self.disabled_reason = f"Hunter returned HTTP {r.status_code} (plan/quota limit); stopped using Hunter"
            raise CreditCapReached(self.disabled_reason)
        r.raise_for_status()
        return r.json().get("data", {})

    def domain_search(self, domain: str, gate: PaidGate) -> list[dict]:
        data = self._call("domain_search", gate, f"domain search {domain}", domain=domain,
                          seniority="senior,executive", limit=10)
        out = []
        for e in data.get("emails", []):
            out.append({
                "name": " ".join(x for x in (e.get("first_name"), e.get("last_name")) if x),
                "title": e.get("position") or "",
                "email": e.get("value") or "",
                "email_status": f"Hunter: confidence {e.get('confidence')}%, verification={((e.get('verification') or {}).get('status'))}",
                "phone": e.get("phone_number") or "",
                "sources": [s.get("uri") for s in e.get("sources", []) if s.get("uri")][:5],
                "provider": self.name,
            })
        return out

    def email_finder(self, domain: str, first: str, last: str, gate: PaidGate) -> dict:
        d = self._call("email_finder", gate, f"email finder {first} {last} @ {domain}", domain=domain,
                       first_name=first, last_name=last)
        return {
            "email": d.get("email") or "",
            "email_status": f"Hunter: score {d.get('score')}, verification={((d.get('verification') or {}).get('status'))}",
            "phone": d.get("phone_number") or "",
            "sources": [s.get("uri") for s in d.get("sources", []) if s.get("uri")][:5],
            "provider": self.name,
        }


class Apollo:
    name = "Apollo.io"
    BASE = "https://api.apollo.io"
    COST = {"people_search": 0.0, "people_match": 1.0}

    def __init__(self, settings: Settings):
        self.key = settings.apollo_api_key
        self.available = bool(self.key)
        self.disabled_reason = ""

    def _headers(self):
        return {"X-Api-Key": self.key, "Content-Type": "application/json", "Cache-Control": "no-cache"}

    def health(self) -> dict:
        """Free call: confirms the key works. Apollo does not expose a credit balance via this API;
        check Settings > Plans & Billing in Apollo for remaining credits."""
        r = requests.get(f"{self.BASE}/v1/auth/health", headers=self._headers(), timeout=20)
        return {"http_status": r.status_code, "body": r.json() if r.headers.get("content-type", "").startswith("application/json") else r.text[:200]}

    def _post(self, path: str, endpoint: str, gate: PaidGate, purpose: str, payload: dict) -> dict:
        if self.disabled_reason:
            raise CreditCapReached(self.disabled_reason)
        gate.spend(self.name, self.COST[endpoint], purpose)
        r = requests.post(f"{self.BASE}{path}", headers=self._headers(), json=payload, timeout=30)
        if r.status_code in (401, 402, 403, 422, 429):
            self.disabled_reason = f"Apollo returned HTTP {r.status_code}: {r.text[:150]} - stopped using Apollo"
            raise CreditCapReached(self.disabled_reason)
        r.raise_for_status()
        return r.json()

    def people_search(self, domain: str, gate: PaidGate) -> list[dict]:
        """Apollo documents this endpoint as not consuming credits; it returns names/titles only."""
        data = self._post("/api/v1/mixed_people/api_search", "people_search", gate, f"people search {domain}", {
            "q_organization_domains_list": [domain],
            "person_seniorities": ["owner", "founder", "c_suite", "partner", "vp", "head", "director"],
            "per_page": 10,
        })
        out = []
        for p in data.get("people", []):
            out.append({
                "apollo_id": p.get("id"),
                "first_name": p.get("first_name") or "",
                "last_name": p.get("last_name") or p.get("last_name_obfuscated") or "",
                "last_name_is_obfuscated": not p.get("last_name"),
                "title": p.get("title") or "",
                "has_email": p.get("has_email"),
                "has_direct_phone": p.get("has_direct_phone"),
            })
        return out

    def people_match(self, apollo_id: str, domain: str, gate: PaidGate) -> dict:
        """Credit-consuming enrichment for one person. Personal emails are never requested."""
        data = self._post("/api/v1/people/match", "people_match", gate, f"enrich person {apollo_id} @ {domain}", {
            "id": apollo_id, "domain": domain, "reveal_personal_emails": False,
        })
        p = data.get("person") or {}
        return {
            "name": p.get("name") or "",
            "title": p.get("title") or "",
            "email": p.get("email") or "",
            "email_status": f"Apollo email_status={p.get('email_status')}",
            "linkedin_url": p.get("linkedin_url") or "",
            "provider": self.name,
        }
