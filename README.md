# faredrop-data

Data engine for the FareDrop flight deals tracker. This repo has no website —
its only job is to scrape cheap round-trip fares every 6 hours, compute real
savings figures from a rolling price history, and commit `deals.json` so a
separate static frontend can fetch it via the raw GitHub URL.

## How it works

Every run of `scanner.py`:
1. Reads `config.json` for routes, date windows, and thresholds.
2. Queries Google Flights (via `fast-flights`) for every route × date-window × stay-night combination.
3. Appends every valid INR fare to `history.json` (90-day rolling window).
4. Computes "% below typical" using the **median** of that route's price history.
5. Filters to deals ≥ 20 % below typical (or Google-signalled "low" for cold-start routes).
6. Overwrites `deals.json` and `deals.js` with the ranked deal list.

## Quick start

```bash
pip install fast-flights
python scanner.py          # live scrape (requires network, best from India region)
python scanner.py --demo   # seed demo data, no internet needed
```

## Editing routes and thresholds

All tunable settings live in **`config.json`**:

| Key | Default | Purpose |
|-----|---------|---------|
| `routes` | 12 routes | List of `{"from":"XXX","to":"YYY"}` pairs |
| `date_windows_days_out` | `[25, 50, 80]` | How far ahead to search (days) |
| `stay_nights` | `[10, 14]` | Return-trip durations to check |
| `min_drop_to_show` | `0.20` | Minimum % below typical to publish (0.20 = 20%) |
| `min_samples_for_typical` | `8` | History samples needed before using median |
| `family_size_for_savings` | `4` | Multiplier for family savings figure |
| `history_days` | `90` | How many days of price history to keep |

## Changing the scan cadence

Open `.github/workflows/scan.yml` and edit this one line:

```yaml
- cron: "0 */6 * * *"   # ← change to e.g. "0 */3 * * *" for every 3 hours
```

## Frontend URL

A separate frontend fetches data from:

```
https://raw.githubusercontent.com/<your-username>/faredrop-data/main/deals.json
```

Or as a JS module (sets `window.DEALS`):

```
https://raw.githubusercontent.com/<your-username>/faredrop-data/main/deals.js
```

## Generated files

| File | Committed? | Purpose |
|------|-----------|---------|
| `deals.json` | Yes | Machine-readable deal list (frontend source of truth) |
| `deals.js` | Yes | Same data as `window.DEALS = {...};` for direct `<script>` use |
| `history.json` | Yes | Rolling 90-day price log; drives the "% below typical" math |

## Caveats

1. **Non-INR results**: `fast-flights` may return prices in a non-INR currency
   depending on where the request resolves. The scanner skips any fare without
   a `₹` symbol — no guessing. If you see many skips, run from an India-region
   machine or VPN.

2. **ToS gray area**: Scraping Google Flights is against Google's Terms of
   Service. Keep call volume modest (the built-in 2.5–5.5 s throttle helps).

3. **Library updates**: `fast-flights` parses undocumented internal APIs and
   can break on Google changes. Run `pip install -U fast-flights` if you see
   errors.
