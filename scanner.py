"""
faredrop-data scanner
Run: python scanner.py        (live scrape via fast-flights)
     python scanner.py --demo (seed data, no internet, no fast-flights)

Model: cheapest-ever per route.
  - Each scan finds the cheapest valid INR fare for every route.
  - deals.json holds the lowest price we've ever seen for each route.
  - A route's deal is only replaced when a scan finds something strictly
    cheaper, OR when the stored fare's depart date has passed (expired),
    at which point we start tracking the next cheapest.
  - A scan that finds nothing never wipes the previously published deals.
"""

import argparse
import json
import random
import re
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlencode

BASE = Path(__file__).parent
CONFIG_FILE = BASE / "config.json"
HISTORY_FILE = BASE / "history.json"
DEALS_FILE = BASE / "deals.json"
DEALS_JS_FILE = BASE / "deals.js"
LEG_CACHE_FILE = BASE / "legs.json"


def load_config():
    with open(CONFIG_FILE) as f:
        return json.load(f)


def load_history():
    if HISTORY_FILE.exists():
        with open(HISTORY_FILE) as f:
            return json.load(f)
    return []


def save_history(records):
    with open(HISTORY_FILE, "w") as f:
        json.dump(records, f, indent=2)


def prune_history(history_records, history_days):
    cutoff = datetime.now(timezone.utc) - timedelta(days=history_days)
    return [
        r for r in history_records
        if datetime.fromisoformat(r["ts"]) >= cutoff
    ]


def load_deals():
    """Prior published deals — this IS the persistent cheapest-ever state."""
    if DEALS_FILE.exists():
        with open(DEALS_FILE) as f:
            return json.load(f)
    return {"deals": []}


def save_deals(obj):
    with open(DEALS_FILE, "w") as f:
        json.dump(obj, f, indent=2)
    with open(DEALS_JS_FILE, "w") as f:
        f.write("window.DEALS = ")
        json.dump(obj, f, indent=2)
        f.write(";\n")


def load_leg_cache():
    """Persistent cheapest-seen price per one-way leg, keyed 'O-D-YYYY-MM-DD'.

    This is what makes free/eventual-consistency converge: scraping is flaky
    (~14-30 legs land per run), but each run banks the legs it got, so multi-leg
    self-transfer combos assemble across runs even though no single run lands
    every leg of a combo at once.
    """
    if LEG_CACHE_FILE.exists():
        with open(LEG_CACHE_FILE) as f:
            return json.load(f)
    return {}


def save_leg_cache(cache):
    with open(LEG_CACHE_FILE, "w") as f:
        json.dump(cache, f, indent=2)


def prune_leg_cache(cache, now):
    """Drop legs whose travel date has passed (codes are 3 letters, so the last
    three dash-fields are the YYYY-MM-DD date)."""
    today = now.date()
    kept = {}
    for k, v in cache.items():
        parts = k.split("-")
        try:
            if datetime.strptime("-".join(parts[-3:]), "%Y-%m-%d").date() >= today:
                kept[k] = v
        except ValueError:
            kept[k] = v
    return kept


def parse_inr(price_str):
    """Return int rupees if price_str is an INR amount, else None."""
    s = price_str.strip()
    if "₹" not in s:
        return None
    digits = re.sub(r"[^\d]", "", s)
    if not digits:
        return None
    val = int(digits)
    return val if val > 0 else None


def build_google_url(origin, dest, dep_date, ret_date):
    q = f"Flights from {origin} to {dest} on {dep_date} through {ret_date}"
    return "https://www.google.com/travel/flights?q=" + urlencode({"": q})[1:]


def is_expired(deal, now):
    """A stored deal is expired once its depart date is in the past."""
    try:
        dep = datetime.strptime(deal["depart_date"], "%Y-%m-%d").date()
    except (KeyError, ValueError, TypeError):
        return True
    return dep < now.date()


def make_deal(candidate, cities, now, first_seen):
    """Build a published deal record from a scraped/seeded candidate."""
    origin = candidate["origin"]
    dest = candidate["dest"]
    return {
        "id": f"{origin}-{dest}",
        "origin": origin,
        "destination": dest,
        "origin_city": cities.get(origin, origin),
        "dest_city": cities.get(dest, dest),
        "price_inr": candidate["price_inr"],
        # deal-vs-typical fields are retired in the cheapest-ever model; kept
        # as null so the existing UI schema stays intact.
        "typical_inr": None,
        "drop_pct": None,
        "savings_inr": None,
        "family_savings_inr": None,
        "lowest_in_days": None,
        "typical_source": "cheapest_ever",
        "google_signal": candidate.get("google_signal"),
        "airline": candidate["airline"],
        "stops": candidate["stops"],
        "depart_date": candidate["depart_date"],
        "return_date": candidate["return_date"],
        "google_flights_url": candidate["google_flights_url"],
        # Separate-ticket pricing: each leg booked independently, summed.
        "fare_type": candidate.get("fare_type", "round_trip"),
        "outbound_airline": candidate.get("outbound_airline"),
        "outbound_stops": candidate.get("outbound_stops"),
        "outbound_price_inr": candidate.get("outbound_price_inr"),
        "return_airline": candidate.get("return_airline"),
        "return_stops": candidate.get("return_stops"),
        "return_price_inr": candidate.get("return_price_inr"),
        "outbound_legs": candidate.get("outbound_legs"),
        "return_legs": candidate.get("return_legs"),
        "first_seen": first_seen,
        "last_updated": now.isoformat().replace("+00:00", "Z"),
    }


def scan_routes(cfg):
    """Routes to actively scan this run (cfg['enabled_routes'] filter, if set)."""
    enabled = cfg.get("enabled_routes")
    routes = cfg["routes"]
    if enabled:
        allow = set(enabled)
        routes = [r for r in routes if f"{r['from']}-{r['to']}" in allow]
    return routes


def merge_deals(cfg, candidates, cities, now, scanned_keys):
    """Combine this scan's cheapest fares with the prior cheapest-ever board.

    Only routes we actually scanned (scanned_keys) are updated. For each:
      - keep the stored cheapest fare unless this scan found a strictly cheaper
        one, or the stored fare has expired (its depart date has passed),
      - in which case publish the freshest cheapest fare instead.
    Routes we did NOT scan keep their previously published deal untouched, so
    narrowing scope never wipes the rest of the board.
    """
    board = {d["id"]: d for d in load_deals().get("deals", [])}

    for route in cfg["routes"]:
        key = f"{route['from']}-{route['to']}"
        if key not in scanned_keys:
            continue  # not scanned this run — leave prior deal as-is

        prior = board.get(key)
        prior_valid = prior is not None and not is_expired(prior, now)
        today = candidates.get(key)

        if today and (not prior_valid or today["price_inr"] < prior["price_inr"]):
            board[key] = make_deal(today, cities, now, first_seen=now.isoformat().replace("+00:00", "Z"))
            note = "new low" if prior_valid else ("fresh" if prior is None else "replaced expired")
            print(f"  [TRACK] {key}: ₹{today['price_inr']:,} ({note})")
        elif prior_valid:
            kept = "no scan" if today is None else f"≥ stored ₹{prior['price_inr']:,}"
            print(f"  [KEEP]  {key}: ₹{prior['price_inr']:,} ({kept})")
        else:
            board.pop(key, None)
            print(f"  [DROP]  {key}: expired/absent and no fare this scan")

    return sorted(board.values(), key=lambda d: d["price_inr"])


def publish(cfg, deals, now, history_records=None):
    """Write deals.json/deals.js, preserving the last good board if empty."""
    if history_records is not None:
        save_history(prune_history(history_records, cfg["history_days"]))

    if deals:
        output = {
            "generated_at": now.isoformat().replace("+00:00", "Z"),
            "routes_watched": len(cfg["routes"]),
            "deals": deals,
        }
        save_deals(output)
        print(f"\nWrote: {DEALS_FILE}, {DEALS_JS_FILE}")
    else:
        print("\n  [KEEP] nothing to publish — preserving previous deals.json")


# ---------------------------------------------------------------------------
# DEMO MODE
# ---------------------------------------------------------------------------

DEMO_AIRLINES = ["IndiGo", "Air India", "SpiceJet", "Vistara", "GoAir", "Emirates", "flydubai"]
DEMO_STOPS = [0, 1, 1, 0, 2]


def demo_base_price(origin, dest):
    bases = {
        "DEL-JED": 28000, "BOM-JED": 31000, "LKO-JED": 33000,
        "DEL-MED": 29000, "DEL-DXB": 22000, "BOM-BKK": 19000,
        "DEL-DPS": 35000, "BLR-SIN": 17000, "DEL-ALA": 26000,
        "DEL-GYD": 24000, "DEL-TBS": 27000, "DEL-KTM": 9000,
    }
    return bases.get(f"{origin}-{dest}", 25000)


def run_demo(cfg):
    print("=== DEMO MODE (no internet) ===\n")
    now = datetime.now(timezone.utc)
    cities = cfg["cities"]
    random.seed()

    candidates = {}
    history_records = load_history()
    routes = scan_routes(cfg)
    scanned_keys = {f"{r['from']}-{r['to']}" for r in routes}
    for route in routes:
        o, d = route["from"], route["to"]
        key = f"{o}-{d}"
        price = max(1000, demo_base_price(o, d) + random.randint(-3000, 3000))

        days_out = random.choice(cfg["date_windows_days_out"])
        stay = random.choice(cfg["stay_nights"])
        dep = (now + timedelta(days=days_out)).strftime("%Y-%m-%d")
        ret = (now + timedelta(days=days_out + stay)).strftime("%Y-%m-%d")

        candidates[key] = {
            "origin": o, "dest": d, "price_inr": price,
            "airline": random.choice(DEMO_AIRLINES),
            "stops": random.choice(DEMO_STOPS),
            "depart_date": dep, "return_date": ret,
            "google_signal": random.choice(["low", "low", "typical"]),
            "google_flights_url": build_google_url(o, d, dep, ret),
        }
        history_records.append({"route": key, "price_inr": price, "ts": now.isoformat()})
        print(f"  [DEMO] {key}: ₹{price:,} dep {dep} ret {ret}")

    print()
    deals = merge_deals(cfg, candidates, cities, now, scanned_keys)
    publish(cfg, deals, now, history_records)

    print(f"\n=== DEMO COMPLETE ===  deals on board: {len(deals)}")


# ---------------------------------------------------------------------------
# LIVE MODE
# ---------------------------------------------------------------------------

def cheapest_oneway(query, origin, dest, date, currency, mode):
    """Cheapest valid INR one-way fare for origin→dest on date.

    Returns (best_dict|None, google_signal). best_dict has price_inr/airline/stops.
    """
    from fast_flights.filter import TFSData
    from fast_flights.flights_impl import FlightData, Passengers

    result = query(
        TFSData.from_interface(
            flight_data=[FlightData(date=date, from_airport=origin, to_airport=dest)],
            trip="one-way",
            seat="economy",
            passengers=Passengers(adults=1, children=0, infants_in_seat=0, infants_on_lap=0),
        ),
        currency=currency,
        mode=mode,
    )
    signal = getattr(result, "current_price", None)
    best = None
    for f in result.flights:
        price_inr = parse_inr(f.price)
        if price_inr is None:
            continue
        if best is None or price_inr < best["price_inr"]:
            best = {"price_inr": price_inr, "airline": f.name, "stops": f.stops}
    return best, signal


def _next_day(date_str):
    return (datetime.strptime(date_str, "%Y-%m-%d") + timedelta(days=1)).strftime("%Y-%m-%d")


def _leg_rec(frm, to, date, leg):
    return {"from": frm, "to": to, "date": date,
            "airline": leg["airline"], "stops": leg["stops"], "price_inr": leg["price_inr"]}


def _route_label(legs):
    return "→".join([legs[0]["from"]] + [l["to"] for l in legs])


def leg_requests(o, d, date, hubs, overnight):
    """Every (from, to, date) leg cheapest_path(o,d,date) will look up — so we
    can prefetch them in a randomized order (avoids always starving the same
    combos when Google rate-limits the runner mid-run)."""
    reqs = [(o, d, date)]
    for h in hubs:
        if h in (o, d):
            continue
        reqs.append((o, h, date))
        for cd in [date] + ([_next_day(date)] if overnight else []):
            reqs.append((h, d, cd))
    return reqs


def cheapest_path(fetch_leg, o, d, date, hubs, overnight):
    """Cheapest way to fly o→d departing `date`: a direct fare, or a separate-
    ticket self-transfer via one of `hubs` (Google 'Cheapest' / Kiwi-style
    virtual interlining). Returns {price_inr, legs[], signal} or None.

    `legs` is one record for a direct flight, or two (o→hub, hub→d) for a
    self-transfer; with `overnight` the hub→d leg is also tried the next day.
    """
    best = None
    direct, sig = fetch_leg(o, d, date)
    if direct:
        best = {"price_inr": direct["price_inr"], "legs": [_leg_rec(o, d, date, direct)], "signal": sig}

    for h in hubs:
        if h in (o, d):
            continue
        l1, s1 = fetch_leg(o, h, date)
        if not l1:
            continue
        for cd in [date] + ([_next_day(date)] if overnight else []):
            l2, _ = fetch_leg(h, d, cd)
            if not l2:
                continue
            total = l1["price_inr"] + l2["price_inr"]
            if best is None or total < best["price_inr"]:
                best = {
                    "price_inr": total,
                    "legs": [_leg_rec(o, h, date, l1), _leg_rec(h, d, cd, l2)],
                    "signal": s1,
                }
    return best


def build_multicity_url(legs, currency):
    """Deep-link the exact (possibly multi-leg, separate-ticket) itinerary."""
    from fast_flights.filter import TFSData
    from fast_flights.flights_impl import FlightData, Passengers

    fd = [FlightData(date=l["date"], from_airport=l["from"], to_airport=l["to"]) for l in legs]
    b64 = TFSData.from_interface(
        flight_data=fd,
        trip="multi-city" if len(fd) > 1 else "one-way",
        seat="economy",
        passengers=Passengers(adults=1, children=0, infants_in_seat=0, infants_on_lap=0),
    ).as_b64().decode("utf-8")
    return "https://www.google.com/travel/flights?" + urlencode({"tfs": b64, "hl": "en", "curr": currency})


def run_live(cfg):
    # Lazy-import fast-flights ONLY in the real path.
    # We call get_flights_from_filter directly (instead of the public get_flights
    # wrapper) because only the lower-level function plumbs through `currency`.
    # Forcing currency="INR" makes Google price the page in ₹ regardless of IP.
    from fast_flights.core import get_flights_from_filter as query  # noqa: lazy import

    currency = cfg.get("currency", "INR")
    # 'local' runs a real headless Chromium on the runner (reliable); 'fallback'
    # depends on the now-turnstile-gated try.playwright.tech demo and mostly 401s.
    mode = cfg.get("fetch_mode", "local")
    retries = cfg.get("fetch_retries", 2)
    now = datetime.now(timezone.utc)
    cities = cfg["cities"]
    routes = scan_routes(cfg)
    scanned_keys = {f"{r['from']}-{r['to']}" for r in routes}
    history_records = load_history()
    leg_cache = prune_leg_cache(load_leg_cache(), now)  # cheapest-seen per leg, banked across runs

    candidates = {}
    total_calls = [0]  # mutable counter shared with the throttled fetch helper

    def fetch_leg(origin, dest, date):
        """Cheapest known price for a one-way leg = min(live fetch, banked cache).

        Live success banks the price (keeping the cheaper of new vs stored).
        Live failure falls back to the banked price, so a leg scraped on an
        earlier run still contributes to today's combinations.
        """
        memo_key = (origin, dest, date)
        if memo_key in fetch_leg.memo:
            return fetch_leg.memo[memo_key]

        pkey = f"{origin}-{dest}-{date}"
        stored = leg_cache.get(pkey)
        live, signal = None, None
        for attempt in range(1, retries + 2):
            if total_calls[0] > 0:
                time.sleep(random.uniform(1.5, 3.5))
            try:
                live, signal = cheapest_oneway(query, origin, dest, date, currency, mode)
                total_calls[0] += 1
                break
            except Exception as exc:
                total_calls[0] += 1
                msg = str(exc).splitlines()[0][:60]
                print(f"  [ERROR] {origin}→{dest} {date} (try {attempt}): {msg}")
                time.sleep(random.uniform(8, 15))  # cool-off; timeouts mean rate-limiting

        if live and (stored is None or live["price_inr"] < stored["price_inr"]):
            leg_cache[pkey] = {**live, "ts": now.isoformat()}
            best = live
            print(f"  [LEG] {origin}→{dest} {date}: ₹{live['price_inr']:,} (banked)")
        elif stored:
            best = {"price_inr": stored["price_inr"], "airline": stored["airline"], "stops": stored["stops"]}
            src = "live≥cache" if live else "cache (fetch failed)"
            print(f"  [LEG] {origin}→{dest} {date}: ₹{stored['price_inr']:,} ({src})")
        else:
            best = live  # None if both failed
            if not live:
                print(f"  [MISS] {origin}→{dest} {date}: no live fare, none banked")

        fetch_leg.memo[memo_key] = (best, signal)
        return best, signal
    fetch_leg.memo = {}

    combos = [
        (days_out, stay)
        for days_out in cfg["date_windows_days_out"]
        for stay in cfg["stay_nights"]
    ]
    hubs_by_route = cfg.get("interline_hubs", {})
    overnight = cfg.get("interline_overnight", False)

    # Phase 1: enumerate every leg we'll need, dedupe, fetch in random order.
    # Randomizing means a mid-run rate-limit block starves a *different* slice
    # each day, so the cheapest-ever board fills in coverage over several runs.
    pending = set()
    for route in routes:
        o, d = route["from"], route["to"]
        hubs = hubs_by_route.get(f"{o}-{d}", [])
        for days_out, stay in combos:
            dep = (now + timedelta(days=days_out)).strftime("%Y-%m-%d")
            ret = (now + timedelta(days=days_out + stay)).strftime("%Y-%m-%d")
            pending.update(leg_requests(o, d, dep, hubs, overnight))
            pending.update(leg_requests(d, o, ret, hubs, overnight))
    pending = list(pending)
    random.shuffle(pending)
    print(f"  Prefetching {len(pending)} unique legs (randomized order)…\n")
    for a, b, dt in pending:
        fetch_leg(a, b, dt)

    # Phase 2: combine cached legs into the cheapest round trip per route.
    print()
    for route in routes:
        o, d = route["from"], route["to"]
        key = f"{o}-{d}"
        hubs = hubs_by_route.get(key, [])
        best_combo = None
        out_cache, ret_cache = {}, {}

        for days_out, stay in combos:
            dep = (now + timedelta(days=days_out)).strftime("%Y-%m-%d")
            ret = (now + timedelta(days=days_out + stay)).strftime("%Y-%m-%d")

            if dep not in out_cache:
                out_cache[dep] = cheapest_path(fetch_leg, o, d, dep, hubs, overnight)   # outbound
            if ret not in ret_cache:
                ret_cache[ret] = cheapest_path(fetch_leg, d, o, ret, hubs, overnight)   # return
            out, back = out_cache[dep], ret_cache[ret]
            if not out or not back:
                continue

            total = out["price_inr"] + back["price_inr"]
            if best_combo is None or total < best_combo["price_inr"]:
                all_legs = out["legs"] + back["legs"]
                out_label, ret_label = _route_label(out["legs"]), _route_label(back["legs"])
                interline = len(out["legs"]) > 1 or len(back["legs"]) > 1
                best_combo = {
                    "origin": o,
                    "dest": d,
                    "price_inr": total,
                    "airline": f"{out_label} / {ret_label}",
                    "stops": (len(out["legs"]) - 1) + (len(back["legs"]) - 1),
                    "depart_date": dep,
                    "return_date": ret,
                    "google_signal": out["signal"],
                    "google_flights_url": build_multicity_url(all_legs, currency),
                    "fare_type": "virtual_interline" if interline else "separate_tickets",
                    "outbound_airline": out_label,
                    "outbound_stops": len(out["legs"]) - 1,
                    "outbound_price_inr": out["price_inr"],
                    "return_airline": ret_label,
                    "return_stops": len(back["legs"]) - 1,
                    "return_price_inr": back["price_inr"],
                    "outbound_legs": out["legs"],
                    "return_legs": back["legs"],
                }

        if best_combo:
            candidates[key] = best_combo
            print(f"  [CHEAPEST] {key}: ₹{best_combo['price_inr']:,} "
                  f"[{best_combo['airline']}] "
                  f"({best_combo['outbound_price_inr']:,} + {best_combo['return_price_inr']:,}) "
                  f"dep {best_combo['depart_date']} ret {best_combo['return_date']} "
                  f"type={best_combo['fare_type']}")
            history_records.append({
                "route": key,
                "price_inr": best_combo["price_inr"],
                "ts": now.isoformat(),
            })
        else:
            print(f"  [NO CANDIDATE] {key}: no valid INR legs found across all windows")

    print()
    save_leg_cache(leg_cache)  # bank this run's legs for future combinations
    deals = merge_deals(cfg, candidates, cities, now, scanned_keys)
    publish(cfg, deals, now, history_records)

    print(f"\n=== SCAN COMPLETE ===")
    print(f"  Routes scanned : {sorted(scanned_keys)}")
    print(f"  API calls made : {total_calls[0]}")
    print(f"  Legs banked    : {len(leg_cache)}")
    print(f"  Deals on board : {len(deals)}")


# ---------------------------------------------------------------------------
# ENTRY POINT
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="faredrop-data scanner")
    parser.add_argument("--demo", action="store_true", help="Run in demo mode (no internet)")
    args = parser.parse_args()

    cfg = load_config()
    if args.demo:
        run_demo(cfg)
    else:
        run_live(cfg)


if __name__ == "__main__":
    main()
