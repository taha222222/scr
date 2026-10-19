"""Write the enriched workbook (.xlsx) and a CSV copy of the main sheet.

One row per original input row, in the original order (duplicates kept).
Extra sheets: All_Contacts, Research_Log, Sources, Manual_Search_Links, Summary.
"""
from __future__ import annotations

import csv
import datetime as dt
import os
from pathlib import Path

from openpyxl import Workbook
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.utils import get_column_letter

MAIN_COLUMNS = [
    "Original row number", "Original company name", "Confirmed company name", "Website", "Country",
    "Decision-maker full name", "Job title", "Contact role category", "Professional email",
    "Email evidence/status", "Direct business phone", "Phone type", "Phone evidence/status",
    "Source URL for the person", "Source URL for the email", "Source URL for the phone",
    "Confidence level", "Research notes", "Research status", "Last checked date",
]
WIDTHS = {"Research notes": 70, "Email evidence/status": 45, "Phone evidence/status": 40, "Website": 34,
          "Source URL for the person": 40, "Source URL for the email": 40, "Source URL for the phone": 40}
HEADER_FILL = PatternFill("solid", fgColor="1F4E78")
STATUS_FILL = {
    "Verified from official source": "C6EFCE", "Supported by multiple public sources": "DDEBF7",
    "Single-source evidence": "FFF2CC", "Unverified candidate": "FCE4D6", "Generic company contact only": "EDEDED",
    "Not found": "F8CBAD", "Company identity unresolved": "F4B084", "Not researched": "FFFFFF",
}


def _sanitize(v):
    """Prevent spreadsheet formula injection from scraped text."""
    if not isinstance(v, str):
        return v
    risky = v[:1] in ("=", "-", "@") or (v[:1] == "+" and not v[1:2].isdigit())
    return ("'" + v if risky else v)[:32000]


def build_rows(input_rows, results: dict) -> list[dict]:
    rows = []
    for r in input_rows:
        res = results.get(r.research_key) if r.research_key else None
        base = {
            "Original row number": r.row_number,
            "Original company name": r.original_company or r.original_website,
            "Website": r.url or r.original_website,
        }
        notes = []
        if r.original_remark:
            notes.append(f"Original remark: {r.original_remark}")
        if r.corrections:
            notes.append("URL correction: " + "; ".join(r.corrections))
        if r.duplicate_of_row:
            notes.append(f"Duplicate of row {r.duplicate_of_row} - same research reused.")
        if r.row_kind == "section_label":
            notes.append("Looks like a section/category label, not a company - not researched.")
            rows.append({**base, "Research status": "Not researched", "Research notes": " | ".join(notes)})
            continue
        if res is None:
            rows.append({**base, "Research status": "Pending (not yet researched)", "Research notes": " | ".join(notes)})
            continue
        p = res["people"][res["primary"]] if res.get("primary") is not None else None
        e = res["emails"][res["email"]] if res.get("email") is not None else None
        ph = res["phones"][res["phone"]] if res.get("phone") is not None else None
        best_src = ""
        if p:
            order = {"official_website": 0, "official_registry": 1, "data_provider": 2, "third_party_page": 3, "search_snippet": 4}
            best_src = sorted(p["sources"], key=lambda s: order.get(s["source_type"], 9))[0]["url"]
        notes += res.get("notes", [])
        if res.get("todo"):
            notes.append("Still to research: " + "; ".join(dict.fromkeys(res["todo"])))
        if res.get("product_fit"):
            notes.append(f"Product fit: {res['product_fit']}")
        if res.get("region_basis") and res.get("country"):
            notes.append(f"Country basis: {res['region_basis']}")
        rows.append({
            **base,
            "Confirmed company name": res.get("confirmed_name", ""),
            "Website": f"https://{res['domain']}" if res.get("domain") and res.get("identity_ok") else base["Website"],
            "Country": res.get("country", ""),
            "Decision-maker full name": p["name"] if p else "",
            "Job title": p["title"] if p else "",
            "Contact role category": p["role_category"] if p else "",
            "Professional email": e["email"] if e else "",
            "Email evidence/status": e["evidence"] if e else "Not found publicly",
            "Direct business phone": ph["display"] if ph else "",
            "Phone type": ph["phone_type"] if ph else "",
            "Phone evidence/status": ph["evidence"] if ph else "Not found publicly",
            "Source URL for the person": best_src,
            "Source URL for the email": e["source_url"] if e else "",
            "Source URL for the phone": ph["source_url"] if ph else "",
            "Confidence level": res.get("confidence", ""),
            "Research notes": " | ".join(notes),
            "Research status": res.get("research_status", ""),
            "Last checked date": res.get("checked_at", "")[:10],
        })
    return rows


def _write_sheet(ws, columns, rows, widths=None, link_cols=()):
    ws.append(columns)
    for c in ws[1]:
        c.font = Font(bold=True, color="FFFFFF")
        c.fill = HEADER_FILL
        c.alignment = Alignment(wrap_text=True, vertical="center")
    for row in rows:
        ws.append([_sanitize(row.get(c, "")) for c in columns])
    for i, col in enumerate(columns, 1):
        ws.column_dimensions[get_column_letter(i)].width = (widths or {}).get(col, max(12, min(30, len(col) + 4)))
        if col in link_cols:
            for cell in ws[get_column_letter(i)][1:]:
                if isinstance(cell.value, str) and cell.value.startswith("http"):
                    cell.hyperlink = cell.value
                    cell.font = Font(color="0563C1", underline="single")
    ws.freeze_panes = "B2"
    ws.auto_filter.ref = ws.dimensions
    for row in ws.iter_rows(min_row=2):
        for cell in row:
            cell.alignment = Alignment(wrap_text=True, vertical="top")


def export(input_rows, results: dict, info: dict, out_dir: Path, run_meta: dict) -> tuple[Path, Path]:
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = dt.date.today().isoformat()
    xlsx = out_dir / f"KS_Medical_enriched_{stamp}.xlsx"
    csv_path = out_dir / f"KS_Medical_enriched_{stamp}.csv"
    if Path(info["file"]).resolve() in (xlsx.resolve(), csv_path.resolve()):
        raise RuntimeError("Refusing to overwrite the original input file")

    main_rows = build_rows(input_rows, results)
    wb = Workbook()
    ws = wb.active
    ws.title = "Enriched_Contacts"
    _write_sheet(ws, MAIN_COLUMNS, main_rows, WIDTHS,
                 link_cols=("Website", "Source URL for the person", "Source URL for the email", "Source URL for the phone"))
    status_col = MAIN_COLUMNS.index("Research status") + 1
    for row in ws.iter_rows(min_row=2):
        st = row[status_col - 1].value
        if st in STATUS_FILL:
            row[status_col - 1].fill = PatternFill("solid", fgColor=STATUS_FILL[st])

    # All contacts
    keys_rows: dict[str, list[int]] = {}
    for r in input_rows:
        if r.research_key:
            keys_rows.setdefault(r.research_key, []).append(r.row_number)
    contacts = []
    for key, res in results.items():
        for i, p in enumerate(res.get("people", [])):
            mine_e = [e for e in res["emails"] if e.get("person_name") == p["name"]]
            mine_p = [x for x in res["phones"] if x.get("person_name") == p["name"]]
            contacts.append({
                "Original row numbers": ", ".join(map(str, keys_rows.get(key, []))), "Company": res.get("confirmed_name") or res.get("domain"),
                "Website": res.get("domain"), "Name": p["name"], "Job title": p["title"], "All titles seen": " / ".join(p["all_titles"]),
                "Role category": p["role_category"], "Selected as primary": "Yes" if res.get("primary") == i else "",
                "Status": p["status"], "Confidence": p["confidence"], "Named email": ", ".join(e["email"] for e in mine_e),
                "Direct phone": ", ".join(f"{x['display']} ({x['phone_type']})" for x in mine_p),
                "Number of sources": len(p["sources"]), "Source URLs": "\n".join(dict.fromkeys(s["url"] for s in p["sources"])),
                "Evidence snippet": p["sources"][0]["snippet"] if p["sources"] else "", "Conflicts": p.get("conflicts", ""),
            })
    _write_sheet(wb.create_sheet("All_Contacts"), list(contacts[0].keys()) if contacts else ["Company", "Name"], contacts,
                 {"Source URLs": 50, "Evidence snippet": 60, "All titles seen": 30})

    # All emails & phones found (including generic ones not selected)
    contact_points = []
    for key, res in results.items():
        for e in res.get("emails", []):
            contact_points.append({"Company": res.get("confirmed_name") or res.get("domain"), "Website": res.get("domain"),
                                   "Type": "Email", "Value": e["email"], "Category": e["kind"], "Attributed to": e.get("person_name", ""),
                                   "Evidence/status": e["evidence"], "Source URL": e["source_url"], "Context": e["context"]})
        for x in res.get("phones", []):
            contact_points.append({"Company": res.get("confirmed_name") or res.get("domain"), "Website": res.get("domain"),
                                   "Type": "Phone", "Value": x["display"], "Category": x["phone_type"], "Attributed to": x.get("person_name", ""),
                                   "Evidence/status": x["evidence"], "Source URL": x["source_url"], "Context": x["context"]})
    _write_sheet(wb.create_sheet("All_Emails_Phones"),
                 ["Company", "Website", "Type", "Value", "Category", "Attributed to", "Evidence/status", "Source URL", "Context"],
                 contact_points, {"Evidence/status": 50, "Source URL": 45, "Context": 70}, link_cols=("Source URL",))

    # Research log
    log_rows = []
    for key, res in results.items():
        log_rows.append({"Company key": key, "Time": res.get("checked_at", ""), "Layer": "summary", "Target": res.get("domain", ""),
                         "Detail": f"status={res.get('research_status')}; pages visited={len(res.get('visited', []))}; "
                                   f"search queries used={res.get('search_queries_used', 0)}; paid calls={len(res.get('paid_calls', []))}"})
        for v in res.get("visited", []):
            log_rows.append({"Company key": key, "Time": res.get("checked_at", ""), "Layer": "visited URL", "Target": v[0], "Detail": v[1]})
        for entry in res.get("log", []):
            log_rows.append({"Company key": key, "Time": entry["time"], "Layer": entry["layer"], "Target": entry["target"], "Detail": entry["detail"]})
    _write_sheet(wb.create_sheet("Research_Log"), ["Company key", "Time", "Layer", "Target", "Detail"], log_rows,
                 {"Target": 60, "Detail": 70}, link_cols=("Target",))

    # Sources
    src_rows = []
    for key, res in results.items():
        for s in res.get("sources", []):
            src_rows.append({"Company": res.get("confirmed_name") or key, "Website": res.get("domain"), "Field supported": s["field"],
                             "Value": s["value"], "Source URL": s["url"], "Source type": s["source_type"],
                             "Accessed": res.get("checked_at", "")[:10], "Evidence snippet": s.get("snippet", "")})
    _write_sheet(wb.create_sheet("Sources"),
                 ["Company", "Website", "Field supported", "Value", "Source URL", "Source type", "Accessed", "Evidence snippet"],
                 src_rows, {"Source URL": 50, "Evidence snippet": 70, "Field supported": 35}, link_cols=("Source URL",))

    # Manual search links
    mq_rows = []
    for key, res in results.items():
        for q in res.get("manual_queries", []):
            mq_rows.append({"Company": res.get("confirmed_name") or key, "Purpose": q["purpose"], "Query": q["query"],
                            "Google": q["Google"], "Bing": q["Bing"], "DuckDuckGo": q["DuckDuckGo"]})
    _write_sheet(wb.create_sheet("Manual_Search_Links"), ["Company", "Purpose", "Query", "Google", "Bing", "DuckDuckGo"], mq_rows,
                 {"Query": 60, "Google": 30, "Bing": 30, "DuckDuckGo": 30}, link_cols=("Google", "Bing", "DuckDuckGo"))

    # Summary
    ws = wb.create_sheet("Summary", 0)
    counts: dict[str, int] = {}
    for r in main_rows:
        counts[r.get("Research status", "")] = counts.get(r.get("Research status", ""), 0) + 1
    lines = [("Generated", dt.datetime.now().strftime("%Y-%m-%d %H:%M")), ("Input file", info["file"]),
             ("Input rows (non-empty)", len(input_rows)), ("Unique research targets", info["unique_research_targets"]),
             ("Duplicate rows (research reused)", info["duplicate_rows"]), ("Targets researched so far", len(results)),
             ("Named decision-makers found (rows)", sum(1 for r in main_rows if r.get("Decision-maker full name"))),
             ("Rows with named personal email", sum(1 for r in main_rows if "attributed to" in r.get("Email evidence/status", ""))),
             ("Rows with person-specific phone", sum(1 for r in main_rows if r.get("Phone type", "").startswith(("Direct dial", "Business mobile", "Extension")) and "not stated" not in r.get("Phone type", ""))),
             ("", "")]
    lines += [(f"Status: {k}", v) for k, v in sorted(counts.items())]
    lines += [("", "")] + [(k, str(v)) for k, v in run_meta.items()]
    for k, v in lines:
        ws.append([k, _sanitize(v)])
    ws.column_dimensions["A"].width = 42
    ws.column_dimensions["B"].width = 90
    for c in ws["A"]:
        c.font = Font(bold=True)

    tmp = xlsx.with_suffix(".tmp.xlsx")
    wb.save(tmp)
    os.replace(tmp, xlsx)
    with csv_path.open("w", newline="", encoding="utf-8-sig") as fh:
        w = csv.DictWriter(fh, fieldnames=MAIN_COLUMNS)
        w.writeheader()
        for r in main_rows:
            w.writerow({c: _sanitize(r.get(c, "")) for c in MAIN_COLUMNS})
    return xlsx, csv_path
