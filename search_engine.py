"""Search-engine discovery through official APIs only.

Supported providers (each needs its own key; none is required to run):
  * brave      - Brave Search API          (BRAVE_API_KEY)
  * google_cse - Google Programmable Search JSON API (GOOGLE_CSE_API_KEY + GOOGLE_CSE_ID)
  * serpapi    - SerpApi (SERPAPI_API_KEY)  - a paid service with a small free tier
  * none       - no automated search. Clickable manual search links are exported instead.

Search-engine result pages are never scraped directly.
"""
from __future__ import annotations

import hashlib
import json
import time
from dataclasses import dataclass
from urllib.parse import quote_plus

import requests

from config import Settings


@dataclass
class SearchResult:
    title: str
    url: str
    snippet: str
    provider: str
    query: str


class SearchBudgetExceeded(Exception):
    pass


class BaseProvider:
    name = "none"
    available = False

    def __init__(self, settings: Settings):
        self.s = settings
        self.total_queries = 0
        self.errors: list[str] = []
        self._cache_dir = settings.cache_dir / "search"
        self._cache_dir.mkdir(parents=True, exist_ok=True)

    def _cached(self, query: str):
        p = self._cache_dir / (hashlib.sha1(f"{self.name}|{query}".encode()).hexdigest() + ".json")
        if p.exists() and time.time() - p.stat().st_mtime < self.s.cache_ttl_hours * 3600:
            try:
                return [SearchResult(**d) for d in json.loads(p.read_text(encoding="utf-8"))], p
            except (ValueError, TypeError, OSError):
                pass
        return None, p

    def search(self, query: str, count: int = 10) -> tuple[list[SearchResult], bool]:
        """Return (results, used_quota). Cached queries do not use quota."""
        cached, path = self._cached(query)
        if cached is not None:
            return cached, False
        if self.total_queries >= self.s.max_total_search_queries:
            raise SearchBudgetExceeded(f"MAX_TOTAL_SEARCH_QUERIES={self.s.max_total_search_queries} reached")
        time.sleep(1.1)  # stay well inside free-tier rate limits (~1 query/second)
        self.total_queries += 1
        try:
            results = self._search(query, count)
        except requests.RequestException as exc:
            self.errors.append(f"{query}: {exc}")
            return [], True
        except ValueError as exc:
            self.errors.append(f"{query}: {exc}")
            return [], True
        path.write_text(json.dumps([r.__dict__ for r in results]), encoding="utf-8")
        return results, True

    def _search(self, query: str, count: int) -> list[SearchResult]:
        return []


class BraveProvider(BaseProvider):
    name = "brave"

    def __init__(self, settings):
        super().__init__(settings)
        self.available = bool(settings.brave_api_key)

    def _search(self, query, count):
        r = requests.get("https://api.search.brave.com/res/v1/web/search",
                         headers={"X-Subscription-Token": self.s.brave_api_key, "Accept": "application/json"},
                         params={"q": query, "count": min(count, 20)}, timeout=20)
        if r.status_code == 429:
            raise ValueError("Brave rate/quota limit reached (HTTP 429); not retried")
        r.raise_for_status()
        items = r.json().get("web", {}).get("results", [])
        return [SearchResult(i.get("title", ""), i.get("url", ""), _strip_tags(i.get("description", "")), self.name, query) for i in items]


class GoogleCSEProvider(BaseProvider):
    name = "google_cse"

    def __init__(self, settings):
        super().__init__(settings)
        self.available = bool(settings.google_cse_api_key and settings.google_cse_id)

    def _search(self, query, count):
        r = requests.get("https://www.googleapis.com/customsearch/v1",
                         params={"key": self.s.google_cse_api_key, "cx": self.s.google_cse_id, "q": query, "num": min(count, 10)},
                         timeout=20)
        if r.status_code == 429:
            raise ValueError("Google CSE daily quota reached (HTTP 429); not retried")
        r.raise_for_status()
        return [SearchResult(i.get("title", ""), i.get("link", ""), i.get("snippet", ""), self.name, query)
                for i in r.json().get("items", [])]


class SerpApiProvider(BaseProvider):
    name = "serpapi"

    def __init__(self, settings):
        super().__init__(settings)
        self.available = bool(settings.serpapi_api_key)

    def _search(self, query, count):
        r = requests.get("https://serpapi.com/search.json",
                         params={"engine": "google", "q": query, "num": min(count, 10), "api_key": self.s.serpapi_api_key},
                         timeout=30)
        r.raise_for_status()
        data = r.json()
        if data.get("error"):
            raise ValueError(f"SerpApi: {data['error']}")
        return [SearchResult(i.get("title", ""), i.get("link", ""), i.get("snippet", ""), self.name, query)
                for i in data.get("organic_results", [])]


def _strip_tags(s: str) -> str:
    import re
    return re.sub(r"<[^>]+>", "", s)


def get_provider(settings: Settings) -> BaseProvider:
    providers = {"brave": BraveProvider, "google_cse": GoogleCSEProvider, "serpapi": SerpApiProvider}
    choice = settings.search_provider
    if choice in providers:
        p = providers[choice](settings)
        return p if p.available else BaseProvider(settings)
    if choice == "auto":
        for cls in (BraveProvider, GoogleCSEProvider, SerpApiProvider):
            p = cls(settings)
            if p.available:
                return p
    return BaseProvider(settings)


# ------------------------------------------------------------- query plans
def company_queries(company: str, domain: str, country: str) -> list[tuple[str, str]]:
    """(purpose, query) pairs in priority order."""
    c = f'"{company}"' if company else f'"{domain}"'
    loc = f" {country}" if country else ""
    return [
        ("Executive", f'{c} (owner OR founder OR CEO OR "managing director" OR "general manager"){loc}'),
        ("Purchasing", f'{c} ("purchasing manager" OR "procurement manager" OR buyer OR "head of purchasing")'),
        ("Executive", f'"{domain}" (director OR owner OR founder OR president)'),
        ("Profiles", f'site:linkedin.com/in {c}'),
        ("Registry/press", f'{c}{loc} (director OR interview OR "press release" OR exhibitor)'),
        ("Contact", f'"@{domain}" -info -sales'),
    ]


def person_queries(name: str, company: str, domain: str) -> list[tuple[str, str]]:
    return [
        ("Person email", f'"{name}" "@{domain}"'),
        ("Person phone", f'"{name}" "{company or domain}" (phone OR "direct line" OR mobile OR tel)'),
    ]


def manual_search_links(query: str) -> dict[str, str]:
    q = quote_plus(query)
    return {
        "Google": f"https://www.google.com/search?q={q}",
        "Bing": f"https://www.bing.com/search?q={q}",
        "DuckDuckGo": f"https://duckduckgo.com/?q={q}",
    }
