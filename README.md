# KS Medical – Business Contact Research Tool

A Python command-line tool that researches every company in your spreadsheet. It looks for:

- the owner, founder, CEO, managing director or general manager,
- the purchasing or procurement manager or buyer,
- published professional emails and business phone numbers (with extensions),
- the source URL and an evidence snippet for every finding.

It uses **free public sources first**. Paid databases are only used if you configure them **and** type `APPROVE` at a prompt.

> **Honesty rules built into the tool.** It never guesses an email pattern, never invents a name or number, and never
> calls a contact "verified" without evidence. Fields it cannot support are left blank. The *Research notes* column says
> what is still missing.

---

## 1. Installation (beginner friendly)

1. Install **Python 3.10 or newer** from <https://www.python.org/downloads/>. On Windows, tick **"Add Python to PATH"**.
2. Open a terminal (Windows: *Command Prompt*; Mac: *Terminal*) and go to this folder:
   ```bash
   cd path/to/ks_medical_osint
   ```
3. (Recommended) create a virtual environment:
   ```bash
   python -m venv .venv
   # Windows:
   .venv\Scripts\activate
   # Mac/Linux:
   source .venv/bin/activate
   ```
4. Install the dependencies:
   ```bash
   pip install -r requirements.txt
   ```
5. (Optional) copy the configuration template and fill in what you want:
   ```bash
   copy config.example.env .env      # Windows
   cp config.example.env .env        # Mac/Linux
   ```
   At least set `CONTACT_EMAIL=` to your business email. It is sent in the crawler's identification string so website owners can contact you.

## 2. Commands

| Step | Command |
|---|---|
| Check the spreadsheet (rows, columns, duplicates, URL fixes) | `python main.py inspect "For_Taha_data_.xlsx"` |
| See which search/data providers are configured (spends nothing) | `python main.py providers` |
| **Research the entire list** | `python main.py run "For_Taha_data_.xlsx" --output-dir output` |
| Try only the first 5 companies | `python main.py run "For_Taha_data_.xlsx" --limit 5` |
| Rebuild the workbook from saved progress | `python main.py export "For_Taha_data_.xlsx" --output-dir output` |
| Start over and ignore saved progress | add `--fresh` |
| Re-research companies already done | add `--recheck` |
| Offer paid lookups (asks for typed approval) | add `--allow-paid` |

**Resume:** progress is saved after every company in `ks_osint_work/state/`. If the run stops (internet drop,
Ctrl+C, or `MAX_RUNTIME_MINUTES` reached), run the **same command** again. Finished companies are skipped.
The workbook is re-written after every batch (`BATCH_SIZE`, default 10) and at the end.

**Live progress:** the terminal shows each company's result, plus counts of completed and remaining companies, pages fetched, errors, search queries used and paid credits used.

## 3. Input format

- `.xlsx` or `.csv`. The header row is detected automatically. It looks for a column whose name starts with
  `website`, `url` or `domain`, and optionally a `company` column and a `remark` column. Title rows above the header are fine.
- **Every row is kept** in the output in the original order, with its original row number.
- **Duplicates** (e.g. `balancemobility.com.au` and `www.balancemobility.com.au`) are researched once. Both rows stay in the output, and the second is marked "Duplicate of row N".
- **Malformed URLs** are repaired cautiously and the repair is written in *Research notes*. For example, `/https://eroutemobility.com/` becomes
  `eroutemobility.com`, and `www.mobot.sg` becomes `https://www.mobot.sg/`.
- **All-lowercase text with no dot** (e.g. `beach wheel chair`) is treated as a section label and not researched.
  Other text without a URL (e.g. `Accessibility Tourism Directory`) is treated as a company name. If a search API is configured, the tool tries to resolve it. Otherwise it is marked *Company identity unresolved*.
- Your original file is only read, never modified.

## 4. Output workbook

`output/KS_Medical_enriched_YYYY-MM-DD.xlsx` (plus a `.csv` copy of the main sheet).

| Sheet | Contents |
|---|---|
| **Summary** | Counts by status, providers used, requests, errors, paid credits used |
| **Enriched_Contacts** | One row per original row. Columns: original row number, original company name, confirmed company name, website, country, decision-maker full name, job title, contact role category, professional email, email evidence/status, direct business phone, phone type, phone evidence/status, the three source URLs, confidence level, research notes, research status, last checked date |
| **All_Contacts** | Every named person found, e.g. both the owner *and* the purchasing manager, with sources and conflicts |
| **All_Emails_Phones** | Every email and phone number found, including generic ones, with context |
| **Research_Log** | Every URL visited (or skipped, and why), every search query, every registry or provider call |
| **Sources** | One row per piece of evidence: field supported, value, URL, source type, access date, snippet |
| **Manual_Search_Links** | Ready-made Google, Bing and DuckDuckGo links for the owner, purchasing, email and phone searches for each company. Use these if no search API is configured |

### Research status labels

| Status | Meaning |
|---|---|
| Verified from official source | Name **and** title printed on the company's own website (clear pattern) or listed in an official registry (Companies House) |
| Supported by multiple public sources | Person found on 2+ independent sources, none official |
| Single-source evidence | Person found on one page only, or the name/title pairing was inferred from page layout. Needs a manual look |
| Unverified candidate | Person seen only in search-result snippets (e.g. a LinkedIn title) |
| Generic company contact only | No named person, only info@/sales@ or the main line |
| Not found | Nothing usable on the pages checked |
| Company identity unresolved | No website, website unreachable, parked, or ambiguous |

### Email evidence/status

The email evidence column states:

- where the email was published,
- whether it is a generic or department mailbox,
- whether it is attributed to the named person, and how (e.g. *local part contains first and last name*),
- the syntax check result,
- whether it is on the company's own domain,
- the MX-record check result.

It always ends with **"deliverability NOT verified (no SMTP probing)"**. The tool deliberately does not "ping" mail servers.

### Phone types

| Type | Meaning |
|---|---|
| `Direct dial` / `Business mobile` / `Extension` | Printed next to the named person and not used elsewhere on the site as the main line |
| `Direct dial (person not stated)` / `Extension (person not stated)` | Labelled as direct, but no name attached |
| `Department line` | e.g. "Sales:", "Purchasing:", "Service:" |
| `Company-listed mobile (not tied to a named person)` | A mobile number on the site with no name next to it |
| `Switchboard / main line` | The general number. If a number appears next to a person **and** in the header or footer, it is classed as the switchboard, not a direct line |

## 5. Which sources work without paid APIs

| Layer | Source | Needs a key? | Cost |
|---|---|---|---|
| B – Company website | About/Team/Contact/Management/Imprint pages in EN/ES/IT/DE/FR/MS, sitemap, linked PDFs, schema.org data | **No** | Free |
| C – Email checks | Syntax (email-validator), MX records (dnspython) | **No** | Free (MX needs normal DNS access) |
| C – Phone checks | `phonenumbers` validation, country formatting, mobile/fixed type, extension parsing | **No** | Free |
| A – Manual search | Clickable search links exported for you to open | **No** | Free |
| A – Search API | **Brave Search API** (`BRAVE_API_KEY`) | Yes | Free monthly allowance, then paid; check Brave's current pricing |
| A – Search API | **Google Programmable Search JSON API** (`GOOGLE_CSE_API_KEY` + `GOOGLE_CSE_ID`) | Yes | ~100 free queries/day. Google has announced this API is closed to new customers, so it may not be available to you |
| A – Search API | **SerpApi** (`SERPAPI_API_KEY`) | Yes | Small free allowance, then paid |
| Registry | **UK Companies House** (`COMPANIES_HOUSE_API_KEY`). Only director names, roles and appointment dates are kept | Yes (free) | Free |
| D – Data provider | **Hunter.io** (`HUNTER_API_KEY`): domain search, email finder | Yes | **Uses credits** |
| D – Data provider | **Apollo.io** (`APOLLO_API_KEY`): people search, people match | Yes (API access depends on your Apollo plan) | People match **uses credits** |

Search APIs use quota, so before a run that would use them, the tool shows the maximum number of queries and asks
`Continue? [y/N]` (skip with `--yes`). It never scrapes Google, Bing or DuckDuckGo result pages.

### Paid providers – how approval works

Paid lookups only run when **all** of these are true:

1. `HUNTER_API_KEY` and/or `APOLLO_API_KEY` is set in `.env`.
2. `MAX_PAID_CREDITS_PER_RUN` is set above 0. This is a hard cap for the run.
3. You run with `--allow-paid`.
4. The tool shows the balance it can read from the provider, plus its credit estimate, and you type `APPROVE`.
   In a non-interactive window it refuses.

If a provider answers with a plan, quota or rate-limit error, the tool stops using that provider. It does not retry around the limit.

**Apollo connected to Claude is not the same as Apollo connected to this script.** The script only uses an API key
that you put in `.env`. Apollo's API does not report your credit balance, so check it in Apollo's billing page.

## 6. What the tool will not do

- Fetch LinkedIn, Facebook, Instagram, X, ZoomInfo, RocketReach, Lusha, Crunchbase and similar sites. Their terms
  restrict automated access. LinkedIn titles may still appear as *search-result snippets* (labelled *Unverified candidate*).
- Ignore robots.txt, solve CAPTCHAs, bypass logins or paywalls, or retry around rate limits. A 401/403/429 or
  challenge page is logged and that site is skipped.
- Decode Cloudflare-protected email addresses. These are flagged in the notes so you can read them in a browser.
- Collect home addresses, private phone numbers, personal email accounts, dates of birth or family details.
  Companies House officer addresses and birth dates are discarded.
- Guess email patterns (e.g. firstname.lastname@) and present them as real.

## 7. Configuration reference

See `config.example.env`. Key limits:

| Setting | Default |
|---|---|
| `REQUEST_DELAY_SECONDS` | 2 |
| `MAX_PAGES_PER_COMPANY` | 15 |
| `MAX_PDFS_PER_COMPANY` | 3 |
| `MAX_SEARCH_QUERIES_PER_COMPANY` | 8 |
| `MAX_TOTAL_SEARCH_QUERIES` | 300 |
| `MAX_RUNTIME_MINUTES` | 180 |
| `CACHE_TTL_HOURS` | 168 |
| `BATCH_SIZE` | 10 |

## 8. Project layout

```
main.py           command-line entry point (inspect / providers / run / export)
input_loader.py   Excel/CSV loading, header detection, URL repair, duplicate detection
http_client.py    polite HTTP: robots.txt, per-site delay, retries, size limits, disk cache
crawler.py        bounded crawl of the company website, country detection
extractors.py     names + job titles, emails, phone numbers, company identity
search_engine.py  Brave / Google CSE / SerpApi integrations + manual search links
registries.py     UK Companies House officers (free key)
providers.py      Hunter / Apollo (gated behind typed approval and a credit cap)
verification.py   evidence aggregation, attribution, status + confidence scoring
research.py       the per-company pipeline (layers A–D)
exporter.py       Excel + CSV output
state.py          resume/progress files
tests/            offline tests (python -m pytest tests)
sample_data/      a tiny CSV for trying the loader
```

## 9. Known limitations

- Name and title extraction is heuristic. Always read the evidence snippet (Sources / All_Contacts) before you call anyone.
  Pairings inferred from page layout are labelled *Single-source evidence* with Low confidence.
- Sites built entirely with JavaScript (content loaded after page load) may show little text to this tool. No browser automation is used.
- Most small mobility retailers do not publish their owner's name, a direct line or a named email. Without a search API,
  registry key or paid provider, many rows will honestly end up as *Generic company contact only*.
- MX checks need normal DNS access. Some corporate or cloud networks block it, and the tool reports "MX check unavailable" rather than guessing.
- Some sites publish an email for a person without naming them anywhere (e.g. `angel@`). These are shown as
  *personal-looking mailbox; owner not identified*.
