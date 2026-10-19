"""Polite HTTP client: robots.txt, per-host delays, retries, size limits, caching.

This client never tries to get around access controls. A 401/403, a CAPTCHA
page, or a robots.txt disallow is recorded and the URL is skipped.
"""
from __future__ import annotations

import hashlib
import json
import time
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit
from urllib.robotparser import RobotFileParser

import requests

from config import Settings

# Sites whose terms prohibit automated collection, or that sit behind logins.
# Search snippets that mention them may still be recorded, but pages are never fetched.
NO_FETCH_DOMAINS = (
    "linkedin.com", "facebook.com", "instagram.com", "twitter.com", "x.com",
    "tiktok.com", "zoominfo.com", "rocketreach.co", "apollo.io", "lusha.com",
    "signalhire.com", "contactout.com", "crunchbase.com", "glassdoor.com",
    "indeed.com", "seamless.ai", "pitchbook.com", "dnb.com",
)

BLOCK_MARKERS = ("captcha", "cf-challenge", "are you a robot", "access denied", "verify you are human")


@dataclass
class FetchResult:
    url: str
    final_url: str = ""
    status: int = 0
    content_type: str = ""
    text: str = ""
    content: bytes = b""
    from_cache: bool = False
    error: str = ""

    @property
    def ok(self) -> bool:
        return self.status == 200 and not self.error


def host_of(url: str) -> str:
    return (urlsplit(url).hostname or "").lower()


def is_no_fetch(url: str) -> bool:
    host = host_of(url)
    return any(host == d or host.endswith("." + d) for d in NO_FETCH_DOMAINS)


class PoliteSession:
    def __init__(self, settings: Settings, log=None):
        self.s = settings
        self.session = requests.Session()
        self.session.headers.update({
            "User-Agent": settings.user_agent,
            "Accept": "text/html,application/xhtml+xml,application/pdf;q=0.9,*/*;q=0.5",
            "Accept-Language": "en;q=0.9,*;q=0.5",
        })
        self._last_hit: dict[str, float] = {}
        self._robots: dict[str, RobotFileParser | None] = {}
        self._blocked_hosts: dict[str, str] = {}
        self.log = log or (lambda *a, **k: None)
        self.stats = {"requests": 0, "cache_hits": 0, "errors": 0, "robots_skips": 0}
        settings.cache_dir.mkdir(parents=True, exist_ok=True)

    # ---------------------------------------------------------------- cache
    def _cache_path(self, url: str) -> Path:
        return self.s.cache_dir / (hashlib.sha1(url.encode()).hexdigest() + ".json")

    def _cache_get(self, url: str) -> FetchResult | None:
        p = self._cache_path(url)
        if not p.exists():
            return None
        if time.time() - p.stat().st_mtime > self.s.cache_ttl_hours * 3600:
            return None
        try:
            d = json.loads(p.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        content = bytes.fromhex(d.pop("content_hex", ""))
        return FetchResult(**d, content=content, from_cache=True)

    def _cache_put(self, r: FetchResult) -> None:
        d = {k: getattr(r, k) for k in ("url", "final_url", "status", "content_type", "text", "error")}
        # Binary bodies (PDFs) are cached as hex; HTML is cached as text only.
        d["content_hex"] = r.content.hex() if r.content and not r.text else ""
        try:
            self._cache_path(r.url).write_text(json.dumps(d), encoding="utf-8")
        except OSError:
            pass

    # --------------------------------------------------------------- robots
    def allowed(self, url: str) -> bool:
        if not self.s.respect_robots_txt:
            return True
        parts = urlsplit(url)
        base = f"{parts.scheme}://{parts.netloc}"
        if base not in self._robots:
            rp = RobotFileParser()
            try:
                self._wait(parts.hostname or "")
                resp = self.session.get(base + "/robots.txt", timeout=self.s.request_timeout_seconds)
                self.stats["requests"] += 1
                if resp.status_code in (401, 403):
                    rp.disallow_all = True
                elif resp.status_code >= 400:
                    rp.allow_all = True
                else:
                    rp.parse(resp.text.splitlines())
            except requests.RequestException:
                rp.allow_all = True  # robots.txt unreachable: treat as no restrictions
            self._robots[base] = rp
        rp = self._robots[base]
        return rp.can_fetch(self.s.user_agent, url) and rp.can_fetch("*", url)

    def crawl_delay(self, url: str) -> float:
        parts = urlsplit(url)
        rp = self._robots.get(f"{parts.scheme}://{parts.netloc}")
        delay = None
        if rp is not None:
            try:
                delay = rp.crawl_delay(self.s.user_agent) or rp.crawl_delay("*")
            except AttributeError:
                delay = None
        return max(self.s.request_delay_seconds, float(delay or 0))

    def _wait(self, host: str, delay: float | None = None) -> None:
        delay = self.s.request_delay_seconds if delay is None else delay
        last = self._last_hit.get(host, 0)
        remaining = delay - (time.time() - last)
        if remaining > 0:
            time.sleep(remaining)
        self._last_hit[host] = time.time()

    # ---------------------------------------------------------------- fetch
    def fetch(self, url: str, max_bytes: int | None = None, use_cache: bool = True) -> FetchResult:
        max_bytes = max_bytes or self.s.max_page_bytes
        if is_no_fetch(url):
            return FetchResult(url, error="Skipped: site terms restrict automated access")
        host = host_of(url)
        if host in self._blocked_hosts:
            return FetchResult(url, error=f"Skipped: host previously refused access ({self._blocked_hosts[host]})")
        if use_cache:
            cached = self._cache_get(url)
            if cached is not None:
                self.stats["cache_hits"] += 1
                return cached
        if not self.allowed(url):
            self.stats["robots_skips"] += 1
            return FetchResult(url, error="Skipped: disallowed by robots.txt")

        result = FetchResult(url)
        for attempt in range(self.s.max_retries + 1):
            self._wait(host, self.crawl_delay(url))
            try:
                self.stats["requests"] += 1
                resp = self.session.get(url, timeout=self.s.request_timeout_seconds, stream=True, allow_redirects=True)
                body = b""
                for chunk in resp.iter_content(65536):
                    body += chunk
                    if len(body) > max_bytes:
                        break
                resp.close()
                result.status = resp.status_code
                result.final_url = resp.url
                result.content_type = resp.headers.get("Content-Type", "").lower()
                if len(body) > max_bytes:
                    result.error = f"Skipped: response larger than {max_bytes} bytes"
                    break
                if resp.status_code == 429 or resp.status_code == 503:
                    retry_after = resp.headers.get("Retry-After", "")
                    wait = int(retry_after) if retry_after.isdigit() else 30 * (attempt + 1)
                    if wait > self.s.max_retry_after_seconds or attempt == self.s.max_retries:
                        result.error = f"Rate limited (HTTP {resp.status_code}); stopped as requested by the site"
                        self._blocked_hosts[host] = f"HTTP {resp.status_code}"
                        break
                    time.sleep(wait)
                    continue
                if resp.status_code in (401, 403):
                    result.error = f"Access refused (HTTP {resp.status_code}); not bypassed"
                    self._blocked_hosts[host] = f"HTTP {resp.status_code}"
                    break
                if resp.status_code >= 500 and attempt < self.s.max_retries:
                    time.sleep(2 ** (attempt + 1))
                    continue
                if resp.status_code != 200:
                    result.error = f"HTTP {resp.status_code}"
                    break
                if "pdf" in result.content_type:
                    result.content = body
                else:
                    enc = resp.encoding or "utf-8"
                    if enc.lower() == "iso-8859-1" and b"charset" not in body[:2000].lower():
                        enc = "utf-8"
                    result.text = body.decode(enc, errors="replace")
                    head = result.text[:5000].lower()
                    if any(m in head for m in BLOCK_MARKERS) and len(result.text) < 20000:
                        result.error = "Bot-protection/challenge page detected; not bypassed"
                        self._blocked_hosts[host] = "challenge page"
                break
            except requests.RequestException as exc:
                result.error = f"{type(exc).__name__}: {str(exc)[:160]}"
                if attempt < self.s.max_retries:
                    time.sleep(2 ** (attempt + 1))
                    continue
        if result.error:
            self.stats["errors"] += 1
        if result.status in (200, 404, 410) or result.ok:
            self._cache_put(result)
        return result
