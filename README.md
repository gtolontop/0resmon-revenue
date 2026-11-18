# 0RESMON Revenue

Archive of a revenue tracker that scraped the public "recent payments" block of the 0RESMON Tebex store (`0resmon.tebex.io`) every 15 minutes, from **Feb 24 to Nov 18, 2026**. The tracker is retired. This repo keeps its code, the raw data, the cleaned data, and the analysis.

Open [`dashboard.html`](dashboard.html) in a browser for the visual summary.

## Key numbers

All amounts are gross (what customers paid), before Tebex fees, VAT and refunds. Times are Paris time.

| | |
|---|---|
| Estimated revenue per day | **€1,615** (33.9 sales/day) |
| Estimated revenue per month / year | **€49,158** / **€589,438** |
| Estimated total, Feb 24 – Nov 18 | **€474,791** |
| Actually captured by the tracker | €299,922 in 6,281 payments (63.2%) |
| November vs October | €962/day vs €1,599/day (**−39.8%**) |
| Average / median order | €47.75 / €36.00 |
| Distinct customers (reliable names) | 2,589, of whom 659 came back |
| Revenue from repeat customers | 45.7% |
| Best hour of the week | Friday 19:00, €153/h |
| Evening (18–24h) vs morning (7–12h) | 2.9× more revenue per hour |
| Orders ≥ €100 | 8.5% of orders, 28.3% of revenue |

## How the estimate works

The store page lists only the 5 latest payments. A stretch between two successful scrapes is **verified** when the later scrape still shows a payment already known: no payment could have slipped through. 3,107 h of the 6,410 h tracked (48.5%) are verified.

Revenue is measured on verified stretches only, per hour of the week (168 buckets), then summed to a week. That avoids over-weighting nights or weekends.

## Data problems found and fixed

- **Shifted usernames.** The username regex `(\w+)` skipped names containing `.` or `-`, so every name after one was paired with the next payment. 577 duplicate rows created that way were removed. Amounts and times stay correct. 2,583 payments seen during such scrapes keep a `username_reliable = 0` flag.
- **Silent Cloudflare blocks.** 8,315 of 17,289 scrapes got the "Just a moment..." challenge page (HTTP 403) and were logged as "0 payments" with no error.
- **UTC times.** The store shows UTC. Stored times were 1 h (winter) or 2 h (summer) off Paris time. Converted.
- **GBP summed as EUR.** 341 payments, converted at £1 = €1.16.
- **Uncertain dates.** 90 payments were first seen after more than 20 h without a scrape. The store only prints `Today HH:MM`, so their day may be wrong.
- **Stale catalogue.** Package matching relies on a catalogue last refreshed on 2026-09-27 and mostly guesses from prices. Treat `package_guess` as a hint only.

## Layout

```
tracker/
  tracker.py            the tracker as it last ran (SQLite, 15 min loop)
  scraper_v1.py         the first one-shot scraper (Feb 24)
data/
  raw/
    payments.db         tracker database: payments, scrape_sessions, catalog
    tracker.log         every scrape, error and new payment (24.5k lines)
    payments_v1.*, scraper_v1.log, payments_export_march.csv   early exports
  clean/
    payments_clean.db   cleaned payments + coverage windows + dropped duplicates
    payments_clean.csv  same payments as CSV
    audit_report.txt    full audit output
  dashboard/*.json      aggregates used by the dashboard
analysis/
  audit_clean.py        raw db + log -> clean db, coverage and audit report
  export.py             clean db -> dashboard aggregates
  facts.py              clean db -> "numbers worth knowing"
  build_dashboard.py    aggregates + template -> dashboard.html
  dashboard_template.html
dashboard.html          standalone summary page
```

## Rebuild everything

Python 3.10+ standard library only.

```sh
python analysis/audit_clean.py
python analysis/export.py
python analysis/facts.py
python analysis/build_dashboard.py
```

The raw files in `data/raw/` are never modified.
