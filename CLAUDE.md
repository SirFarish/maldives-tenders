# Maldives Tender Collector — project context for Claude

This file is read automatically by Claude Code on any machine that clones this
repo, so work can continue seamlessly from a different laptop. Keep it up to
date when the project changes.

## What this is
A tool that collects every public tender / procurement announcement advertised
in the Maldives and combines them into one shareable, auto-updating dashboard.

- **Live site:** https://sirfarish.github.io/maldives-tenders/ (public, GitHub Pages)
- **Repo:** SirFarish/maldives-tenders
- **Auto-refresh:** GitHub Actions (`.github/workflows/publish.yml`) re-scrapes and
  redeploys daily at 11:00 UTC = 16:00 Maldives time. Manual: Actions tab → Run workflow.

## How it's built
- `sources.py` — a registry `SOURCES = {name: collect_fn(target_dates) -> list[dict]}`.
  Each adapter returns normalised records (see the module docstring for the shape).
  Add a new SOE/agency/firm by writing another `collect_*` function and registering it.
- `tender_collector.py` — orchestrator + HTML dashboard builder + CLI. Runs every
  adapter (failures isolated so one bad site can't sink the run), de-duplicates the
  same tender across sources (shared `ref` + similar title), and writes `output/`.
- Dashboard has a **Source** column + Source filter, a **Type** filter (incl.
  "Tenders only"), live search, and colour-coded deadline badges.

## Two data models (important)
- **Publish-windowed** sources (Gazette, STELCO): filtered to a rolling date window.
- **Open-tender** sources (MWSC, Finance): no reliable publish date; return all
  currently-open tenders (submission deadline >= today), `published` may be None.

## Source status
| Source | Status | Notes |
|---|---|---|
| Gazette (gazette.gov.mv/iulaan) | ✅ live | static HTML; all announcement types; Dhivehi dates mapped |
| STELCO (stelco.com.mv/tenders) | ✅ live | static table; English dates; `?cp_1=N` pages |
| MWSC (mwsc.com.mv/tenders) | ✅ live | open-tenders table |
| Finance national tender (finance.gov.mv/tenders) | ✅ live | **Playwright** (JS); govt-wide agencies; open tenders |
| STO (sto.mv/newsroom/tenders) | ✅ live | **Playwright**; current tenders; REF NO + publish date |
| MACL (macl.aero/tenders) | ⚠️ empty | renders with no tender rows — no current tenders or a deferred loader; revisit |
| MTCC (mtcc.mv/downloads) | ⏳ TODO | 640 undated PDFs; hard to date-filter |
| HDC | ⏳ TODO | JS-rendered; needs inspection |
| Fenaka (fenaka.mv/tenders) | ⏳ TODO | rendered blank; needs a different wait/approach |
| Maldives Ports (cnm.mv/notice) | ⏳ TODO | static Dhivehi card board (864 cards); no Playwright needed |

Note: Finance national tender already aggregates government-wide (agency) tenders, and
SOEs are legally required to also post in the Gazette — so Gazette + Finance already
capture the large majority. Remaining SOE adapters add mostly site-only posts + documents.

## Run locally
```
pip install -r requirements.txt
python -m playwright install chromium      # only needed for JS sources (Finance)
python tender_collector.py --days 4        # rolling window, all sources
python tender_collector.py --only Gazette,Finance
python tender_collector.py --list-sources
```
Output → `output/index.html` (the page served online).

## Gotchas
- Windows console can't print Dhivehi (cp1252) — when debugging, write results to a
  UTF-8 file instead of printing.
- The daily job installs Chromium via `playwright install --with-deps chromium`.
- GitHub Pages was enabled once via the API (`gh api -X POST repos/SirFarish/maldives-tenders/pages -f build_type=workflow`); it is on now.

## Roadmap
- **Track A (in progress):** add remaining SOE/agency adapters (STO, MTCC, MACL, HDC,
  Fenaka, Maldives Ports).
- **Track B (not started):** a **private** RFQ dashboard from Gmail/Workspace over IMAP
  (app-password, read from a local secrets file, never committed, never on the public
  site). Extract company, product, quantity, deadline, attachments.
