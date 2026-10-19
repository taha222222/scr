"""Configuration loading.

Settings come from environment variables, optionally loaded from a `.env`
file (see `config.example.env`). Every limit has a conservative default so
the tool can run with no configuration at all.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

try:  # optional dependency
    from dotenv import load_dotenv
except ImportError:  # pragma: no cover
    load_dotenv = None


def _str(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


def _int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, default))
    except ValueError:
        return default


def _float(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, default))
    except ValueError:
        return default


def _bool(name: str, default: bool) -> bool:
    val = os.environ.get(name)
    if val is None:
        return default
    return val.strip().lower() in {"1", "true", "yes", "on"}


@dataclass
class Settings:
    # Identification sent to websites. Put a real contact address in CONTACT_EMAIL.
    contact_email: str = ""
    user_agent: str = ""

    # Politeness and reliability
    request_delay_seconds: float = 2.0
    request_timeout_seconds: float = 20.0
    max_retries: int = 2
    max_retry_after_seconds: int = 120
    respect_robots_txt: bool = True

    # Bounded crawl
    max_pages_per_company: int = 15
    max_pdfs_per_company: int = 3
    max_page_bytes: int = 3_000_000
    max_pdf_bytes: int = 5_000_000
    max_third_party_pages_per_company: int = 4

    # Search budget
    search_provider: str = "auto"
    max_search_queries_per_company: int = 8
    max_total_search_queries: int = 300

    # Run control
    batch_size: int = 10
    max_runtime_minutes: int = 180
    cache_ttl_hours: int = 168
    recheck_after_days: int = 30

    # Optional API keys (never commit real keys)
    brave_api_key: str = ""
    google_cse_api_key: str = ""
    google_cse_id: str = ""
    serpapi_api_key: str = ""
    companies_house_api_key: str = ""
    hunter_api_key: str = ""
    apollo_api_key: str = ""

    # Paid / credit-consuming providers are disabled unless explicitly approved
    max_paid_credits_per_run: float = 0.0

    # MX checks
    dns_checks_enabled: bool = True

    work_dir: Path = field(default_factory=lambda: Path("ks_osint_work"))

    @property
    def cache_dir(self) -> Path:
        return self.work_dir / "cache"

    @property
    def state_dir(self) -> Path:
        return self.work_dir / "state"


def load_settings(env_file: str | None = None) -> Settings:
    if load_dotenv is not None:
        if env_file:
            load_dotenv(env_file, override=False)
        elif Path(".env").exists():
            load_dotenv(".env", override=False)

    contact = _str("CONTACT_EMAIL")
    ua = _str("USER_AGENT") or (
        "KSMedicalLeadResearch/1.0 (business-contact research; "
        + (f"contact: {contact}" if contact else "contact not configured")
        + ")"
    )
    s = Settings(
        contact_email=contact,
        user_agent=ua,
        request_delay_seconds=_float("REQUEST_DELAY_SECONDS", 2.0),
        request_timeout_seconds=_float("REQUEST_TIMEOUT_SECONDS", 20.0),
        max_retries=_int("MAX_RETRIES", 2),
        max_retry_after_seconds=_int("MAX_RETRY_AFTER_SECONDS", 120),
        respect_robots_txt=_bool("RESPECT_ROBOTS_TXT", True),
        max_pages_per_company=_int("MAX_PAGES_PER_COMPANY", 15),
        max_pdfs_per_company=_int("MAX_PDFS_PER_COMPANY", 3),
        max_page_bytes=_int("MAX_PAGE_BYTES", 3_000_000),
        max_pdf_bytes=_int("MAX_PDF_BYTES", 5_000_000),
        max_third_party_pages_per_company=_int("MAX_THIRD_PARTY_PAGES_PER_COMPANY", 4),
        search_provider=_str("SEARCH_PROVIDER", "auto").lower() or "auto",
        max_search_queries_per_company=_int("MAX_SEARCH_QUERIES_PER_COMPANY", 8),
        max_total_search_queries=_int("MAX_TOTAL_SEARCH_QUERIES", 300),
        batch_size=_int("BATCH_SIZE", 10),
        max_runtime_minutes=_int("MAX_RUNTIME_MINUTES", 180),
        cache_ttl_hours=_int("CACHE_TTL_HOURS", 168),
        recheck_after_days=_int("RECHECK_AFTER_DAYS", 30),
        brave_api_key=_str("BRAVE_API_KEY"),
        google_cse_api_key=_str("GOOGLE_CSE_API_KEY"),
        google_cse_id=_str("GOOGLE_CSE_ID"),
        serpapi_api_key=_str("SERPAPI_API_KEY"),
        companies_house_api_key=_str("COMPANIES_HOUSE_API_KEY"),
        hunter_api_key=_str("HUNTER_API_KEY"),
        apollo_api_key=_str("APOLLO_API_KEY"),
        max_paid_credits_per_run=_float("MAX_PAID_CREDITS_PER_RUN", 0.0),
        dns_checks_enabled=_bool("DNS_CHECKS_ENABLED", True),
        work_dir=Path(_str("WORK_DIR", "ks_osint_work")),
    )
    return s
