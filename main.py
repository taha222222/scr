"""KS Medical business OSINT lead research tool - command-line entry point.

Examples:
  python main.py inspect  "My Companies.xlsx"
  python main.py providers
  python main.py run      "My Companies.xlsx" --output-dir output
  python main.py export   "My Companies.xlsx" --output-dir output     (rebuild workbook from saved progress)
"""
from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from config import load_settings  # noqa: E402
from exporter import export  # noqa: E402
from http_client import PoliteSession  # noqa: E402
from input_loader import load_companies  # noqa: E402
from providers import Apollo, Hunter, PaidGate  # noqa: E402
from registries import CompaniesHouse  # noqa: E402
from research import Researcher  # noqa: E402
from search_engine import get_provider  # noqa: E402
from state import StateStore  # noqa: E402
import verification as vf  # noqa: E402


def cmd_inspect(args, settings):
    rows, info = load_companies(args.input, args.sheet)
    print(f"File: {info['file']}")
    print(f"Header row: {info['header_row']}  Headers: {[h for h in info['headers'] if h]}")
    print(f"Columns detected: {info['columns_detected']}")
    print(f"Non-empty rows: {info['total_rows']}  Unique research targets: {info['unique_research_targets']}  "
          f"Duplicates: {info['duplicate_rows']}  Name-only rows: {info['name_only_rows']}  Section labels: {info['section_labels']}")
    print()
    for r in rows:
        flag = f"duplicate of row {r.duplicate_of_row}" if r.duplicate_of_row else ""
        fix = "; ".join(r.corrections)
        print(f"  row {r.row_number:>3}  [{r.row_kind:<13}] {r.original_website or r.original_company!r:<50} -> {r.domain or '-':<32} {flag} {fix}")


def cmd_providers(args, settings):
    print("Provider availability (keys read from environment/.env; nothing is spent by this check)\n")
    sp = get_provider(settings)
    print(f"  Search API          : {sp.name if sp.available else 'none configured (manual search links will be exported)'}")
    print(f"  Companies House (UK): {'configured' if settings.companies_house_api_key else 'not configured (free key available)'}")
    print(f"  MX/DNS checks       : {'available' if vf.dns_available() else 'unavailable on this network or dnspython missing'}")
    h = Hunter(settings)
    if h.available:
        try:
            print(f"  Hunter.io           : key OK - {h.account()}")
        except Exception as exc:
            print(f"  Hunter.io           : key configured but account check failed: {exc}")
    else:
        print("  Hunter.io           : not configured")
    a = Apollo(settings)
    if a.available:
        try:
            print(f"  Apollo.io           : {a.health()}  (credit balance is not exposed by the API - check Apollo billing page)")
        except Exception as exc:
            print(f"  Apollo.io           : key configured but health check failed: {exc}")
    else:
        print("  Apollo.io           : not configured (a Claude/other-app connection does NOT give this script access)")


def _approve_paid(args, settings, hunter, apollo, n_targets) -> PaidGate:
    if not args.allow_paid:
        return PaidGate(False, 0)
    if not (hunter.available or apollo.available):
        print("--allow-paid given but no HUNTER_API_KEY / APOLLO_API_KEY configured; paid layer disabled.")
        return PaidGate(False, 0)
    cap = settings.max_paid_credits_per_run
    if cap <= 0:
        print("MAX_PAID_CREDITS_PER_RUN is 0 - set a cap in .env to allow paid lookups. Paid layer disabled.")
        return PaidGate(False, 0)
    est = n_targets * ((1 if hunter.available else 0) + (1 if apollo.available else 0))
    print("\n=== PAID LOOKUP APPROVAL ===")
    if hunter.available:
        try:
            print(f"Hunter account: {hunter.account()}")
        except Exception as exc:
            print(f"Hunter account check failed: {exc}")
    if apollo.available:
        try:
            print(f"Apollo health: {apollo.health()} (check remaining credits in Apollo's billing page)")
        except Exception as exc:
            print(f"Apollo health check failed: {exc}")
    print(f"Up to ~{est} credits could be used for {n_targets} companies; hard cap this run: {cap} credits.")
    if not sys.stdin.isatty():
        print("Not an interactive terminal - refusing to spend credits without a typed approval.")
        return PaidGate(False, 0)
    answer = input("Type APPROVE to allow credit-consuming lookups (anything else = free sources only): ").strip()
    return PaidGate(answer == "APPROVE", cap)


def cmd_run(args, settings):
    start = time.time()
    rows, info = load_companies(args.input, args.sheet)
    out_dir = Path(args.output_dir)
    if args.work_dir:
        settings.work_dir = Path(args.work_dir)
    store = StateStore(settings.state_dir)
    if args.fresh:
        store.clear()

    targets: dict[str, object] = {}
    for r in rows:
        if r.research_key and r.research_key not in targets:
            if args.start_row and r.row_number < args.start_row:
                continue
            targets[r.research_key] = r
    keys = list(targets)
    if args.limit:
        keys = keys[: args.limit]

    results = {}
    for r in rows:
        if r.research_key:
            prev = store.load(r.research_key)
            if prev:
                results[r.research_key] = prev
    todo = [k for k in keys if args.recheck or not store.is_fresh(k, settings.recheck_after_days)]

    search = get_provider(settings)
    hunter, apollo = Hunter(settings), Apollo(settings)
    print(f"Input rows: {info['total_rows']} | unique targets: {info['unique_research_targets']} | "
          f"selected: {len(keys)} | already done: {len(keys) - len(todo)} | to research now: {len(todo)}")
    print(f"Search API: {search.name if search.available else 'none (manual search links will be exported)'} | "
          f"Companies House: {'on' if settings.companies_house_api_key else 'off'} | "
          f"MX checks: {'on' if vf.dns_available() else 'unavailable'}")
    if search.available and todo and not args.yes:
        maxq = min(len(todo) * settings.max_search_queries_per_company, settings.max_total_search_queries)
        print(f"The search layer may use up to {maxq} {search.name} queries (counts against your API quota).")
        if not sys.stdin.isatty() or input("Continue? [y/N] ").strip().lower() != "y":
            print("Stopped before using search quota. Re-run with --yes to skip this prompt.")
            return
    gate = _approve_paid(args, settings, hunter, apollo, len(todo))
    if gate.approved:
        print(f"Paid lookups APPROVED, cap {gate.max_credits} credits.")

    http = PoliteSession(settings)
    researcher = Researcher(settings, http, search, CompaniesHouse(settings), hunter, apollo, gate)
    deadline = start + settings.max_runtime_minutes * 60
    batch = args.batch_size or settings.batch_size
    errors = 0
    meta = {}

    def run_meta():
        return {"Search provider": search.name if search.available else "none", "Search queries used (this run)": search.total_queries,
                "HTTP requests (this run)": http.stats["requests"], "Cache hits": http.stats["cache_hits"],
                "Fetch errors": http.stats["errors"], "robots.txt skips": http.stats["robots_skips"],
                "Paid credits used (estimated)": gate.spent, "Paid lookups approved": gate.approved,
                "MX checks": "available" if vf.dns_available() else "unavailable on this network"}

    for i, key in enumerate(todo, 1):
        if time.time() > deadline:
            print(f"\nMAX_RUNTIME_MINUTES reached - stopping. Re-run the same command to resume.")
            break
        r = targets[key]
        print(f"\n[{i}/{len(todo)}] row {r.row_number}: {r.domain or r.original_website}")
        try:
            res = researcher.research(key, r.url, r.domain, r.original_company or r.original_website, r.row_kind)
        except KeyboardInterrupt:
            print("\nInterrupted - progress so far is saved. Re-run the same command to resume.")
            break
        except Exception as exc:  # one bad site must not stop the list
            errors += 1
            print(f"    ERROR: {type(exc).__name__}: {exc}")
            continue
        store.save(key, res)
        results[key] = res
        p = res["people"][res["primary"]] if res.get("primary") is not None else None
        print(f"    {res['research_status']} | {p['name'] + ' - ' + p['title'] if p else 'no named person'} | "
              f"email: {res['emails'][res['email']]['email'] if res.get('email') is not None else '-'} | "
              f"phone: {res['phones'][res['phone']]['display'] if res.get('phone') is not None else '-'}")
        done = sum(1 for k in keys if k in results)
        print(f"    progress: {done}/{len(keys)} done, {len(keys) - done} remaining | pages fetched {http.stats['requests']} | "
              f"errors {http.stats['errors'] + errors} | search queries {search.total_queries} | paid credits {gate.spent}")
        if i % batch == 0 or i == len(todo):
            xlsx, _ = export(rows, results, info, out_dir, run_meta())
            print(f"    batch saved -> {xlsx}")

    xlsx, csvp = export(rows, results, info, out_dir, run_meta())
    print(f"\nDone. Workbook: {xlsx}\n      CSV:      {csvp}")
    print(f"Elapsed {int(time.time() - start)}s. Paid credits used: {gate.spent}.")


def cmd_export(args, settings):
    rows, info = load_companies(args.input, args.sheet)
    if args.work_dir:
        settings.work_dir = Path(args.work_dir)
    store = StateStore(settings.state_dir)
    results = {r.research_key: store.load(r.research_key) for r in rows if r.research_key and store.load(r.research_key)}
    xlsx, csvp = export(rows, results, info, Path(args.output_dir), {"Note": "Rebuilt from saved progress"})
    print(f"Workbook: {xlsx}\nCSV: {csvp}")


def main(argv=None):
    try:
        sys.stdout.reconfigure(line_buffering=True)  # live progress even when output is redirected
    except AttributeError:
        pass
    ap = argparse.ArgumentParser(description="KS Medical business-contact OSINT research tool (public sources first).")
    ap.add_argument("--env-file", help="path to .env file (default: ./.env if present)")
    sub = ap.add_subparsers(dest="cmd", required=True)

    def common(p):
        p.add_argument("input", help=".xlsx or .csv company list (never modified)")
        p.add_argument("--sheet", help="worksheet name (default: first sheet)")
        p.add_argument("--output-dir", default="output")
        p.add_argument("--work-dir", help="cache/progress folder (default: ks_osint_work)")

    common(sub.add_parser("inspect", help="show rows, columns, duplicates and URL corrections"))
    sub.add_parser("providers", help="show which search/data providers are configured (no credits used)")
    run = sub.add_parser("run", help="research companies and write the enriched workbook")
    common(run)
    run.add_argument("--batch-size", type=int, help="export the workbook every N companies (default 10)")
    run.add_argument("--limit", type=int, help="only research the first N unique companies")
    run.add_argument("--start-row", type=int, help="skip companies whose first row is before this sheet row")
    run.add_argument("--fresh", action="store_true", help="discard saved progress and start over")
    run.add_argument("--recheck", action="store_true", help="re-research companies even if recently done")
    run.add_argument("--yes", action="store_true", help="do not ask before using free search-API quota")
    run.add_argument("--allow-paid", action="store_true", help="offer credit-consuming lookups (asks for typed approval)")
    common(sub.add_parser("export", help="rebuild the workbook from saved progress"))
    args = ap.parse_args(argv)

    settings = load_settings(args.env_file)
    {"inspect": cmd_inspect, "providers": cmd_providers, "run": cmd_run, "export": cmd_export}[args.cmd](args, settings)


if __name__ == "__main__":
    main()
