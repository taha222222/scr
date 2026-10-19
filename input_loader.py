"""Load and validate the company list from .xlsx or .csv.

The loader is deliberately forgiving: it finds the header row by scanning the
first rows for a "website"/"company"/"url" column, keeps the original sheet
row number for every entry, and normalises URLs cautiously while recording
every correction it makes. The original file is only ever read.
"""
from __future__ import annotations

import csv
import re
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

TRACKING_PARAMS = re.compile(r"^(utm_|gclid$|fbclid$|msclkid$|mc_|_hs|ref$)", re.I)

WEBSITE_HEADERS = ("website", "url", "domain", "site", "web")
COMPANY_HEADERS = ("company", "business", "organisation", "organization", "name")
REMARK_HEADERS = ("remark", "note", "comment")


@dataclass
class InputRow:
    row_number: int                  # row number in the original sheet (1-based)
    original_website: str            # cell value exactly as supplied
    original_company: str            # company-name cell, if the sheet has one
    original_remark: str
    url: str = ""                    # normalised URL used for research ("" if none)
    domain: str = ""                 # host without leading www.
    corrections: list[str] = field(default_factory=list)
    row_kind: str = "website"        # website | name_only | section_label | empty
    research_key: str = ""           # rows sharing a key are researched once
    duplicate_of_row: int | None = None


def _clean_cell(value) -> str:
    if value is None:
        return ""
    text = str(value).replace("\xa0", " ").replace("​", "")
    return re.sub(r"\s+", " ", text).strip()


def normalize_url(raw: str) -> tuple[str, str, list[str]]:
    """Return (url, domain, corrections). Empty url when the value is not a URL."""
    corrections: list[str] = []
    value = raw.strip()
    if not value:
        return "", "", corrections

    stripped = value.strip(" /\\\"'<>")
    if stripped != value:
        # e.g. "/https://eroutemobility.com/" -> "https://eroutemobility.com"
        if value.lstrip().startswith("/"):
            corrections.append(f"Removed stray leading characters from '{value}'")
        value = stripped

    if " " in value or "." not in value:
        return "", "", corrections

    if not re.match(r"^https?://", value, re.I):
        fixed = re.sub(r"^(https?):?/*", r"\1://", value, flags=re.I) if re.match(r"^https?", value, re.I) else "https://" + value
        corrections.append(f"Added/repaired scheme: '{value}' -> '{fixed}'")
        value = fixed

    parts = urlsplit(value)
    host = (parts.hostname or "").lower().rstrip(".")
    if not host or "." not in host or not re.match(r"^[a-z0-9.-]+$", host):
        return "", "", corrections + [f"Could not parse a valid host from '{raw}'"]

    query = [(k, v) for k, v in parse_qsl(parts.query) if not TRACKING_PARAMS.match(k)]
    if len(query) != len(parse_qsl(parts.query)):
        corrections.append("Removed tracking parameters from URL")
    netloc = host + (f":{parts.port}" if parts.port else "")
    url = urlunsplit((parts.scheme.lower(), netloc, parts.path or "/", urlencode(query), ""))
    domain = host[4:] if host.startswith("www.") else host
    return url, domain, corrections


def _find_header(rows: list[list[str]]) -> tuple[int, dict[str, int]]:
    """Return (header_index, column map) by scanning the first 15 rows."""
    for idx, row in enumerate(rows[:15]):
        cols: dict[str, int] = {}
        for c, cell in enumerate(row):
            low = cell.lower()
            if not low:
                continue
            if "website" not in cols and any(low.startswith(h) or low == h for h in WEBSITE_HEADERS):
                cols["website"] = c
            elif "company" not in cols and any(h in low for h in COMPANY_HEADERS) and "email" not in low:
                cols["company"] = c
            elif "remark" not in cols and any(h in low for h in REMARK_HEADERS):
                cols["remark"] = c
        if "website" in cols or "company" in cols:
            return idx, cols
    # No header found: assume first column holds websites, no header row.
    return -1, {"website": 0}


def _read_rows(path: Path, sheet: str | None) -> tuple[list[list[str]], int]:
    """Return (rows, first_row_number)."""
    if path.suffix.lower() == ".csv":
        with path.open(newline="", encoding="utf-8-sig") as fh:
            return [[_clean_cell(c) for c in row] for row in csv.reader(fh)], 1
    if path.suffix.lower() in {".xlsx", ".xlsm"}:
        import openpyxl

        wb = openpyxl.load_workbook(path, read_only=True, data_only=True)
        ws = wb[sheet] if sheet else wb.worksheets[0]
        rows = [[_clean_cell(c) for c in row] for row in ws.iter_rows(values_only=True)]
        wb.close()
        return rows, 1
    raise ValueError(f"Unsupported input type '{path.suffix}'. Use .xlsx or .csv")


def _is_section_label(text: str) -> bool:
    # All-lowercase free text with no dot, e.g. "beach wheel chair": a grouping
    # label typed by the user rather than a company name.
    return text == text.lower() and "." not in text and len(text.split()) <= 5


def load_companies(path: str | Path, sheet: str | None = None) -> tuple[list[InputRow], dict]:
    path = Path(path)
    rows, first = _read_rows(path, sheet)
    header_idx, cols = _find_header(rows)
    info = {
        "file": str(path),
        "header_row": header_idx + first if header_idx >= 0 else None,
        "headers": rows[header_idx] if header_idx >= 0 else [],
        "columns_detected": cols,
    }

    out: list[InputRow] = []
    seen: dict[str, int] = {}
    for i in range(header_idx + 1, len(rows)):
        row = rows[i]

        def cell(key: str) -> str:
            c = cols.get(key)
            return row[c] if c is not None and c < len(row) else ""

        website, company, remark = cell("website"), cell("company"), cell("remark")
        if not website and not company and not remark:
            continue
        item = InputRow(i + first, website, company, remark)
        url, domain, corrections = normalize_url(website)
        item.corrections = corrections
        if url:
            item.url, item.domain, item.row_kind = url, domain, "website"
            item.research_key = domain
        elif company or website:
            name = company or website
            if not company and _is_section_label(website):
                item.row_kind = "section_label"
            else:
                item.row_kind = "name_only"
                item.research_key = "name:" + re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")
        else:
            item.row_kind = "empty"
        if item.research_key:
            if item.research_key in seen:
                item.duplicate_of_row = seen[item.research_key]
            else:
                seen[item.research_key] = item.row_number
        out.append(item)

    info["total_rows"] = len(out)
    info["unique_research_targets"] = len(seen)
    info["duplicate_rows"] = sum(1 for r in out if r.duplicate_of_row)
    info["section_labels"] = sum(1 for r in out if r.row_kind == "section_label")
    info["name_only_rows"] = sum(1 for r in out if r.row_kind == "name_only")
    return out, info
