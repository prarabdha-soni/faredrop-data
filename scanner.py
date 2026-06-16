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

def cheapest_oneway(query, origin, dest, date, currency):
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
        mode="fallback",
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


def run_live(cfg):
    # Lazy-import fast-flights ONLY in the real path.
    # We call get_flights_from_filter directly (instead of the public get_flights
    # wrapper) because only the lower-level function plumbs through `currency`.
    # Forcing currency="INR" makes Google price the page in ₹ regardless of IP.
    from fast_flights.core import get_flights_from_filter as query  # noqa: lazy import

    currency = cfg.get("currency", "INR")
    now = datetime.now(timezone.utc)
    cities = cfg["cities"]
    routes = scan_routes(cfg)
    scanned_keys = {f"{r['from']}-{r['to']}" for r in routes}
    history_records = load_history()

    candidates = {}
    total_calls = [0]  # mutable counter shared with the throttled fetch helper

    def fetch_leg(origin, dest, date):
        """One throttled, error-tolerant one-way lookup; caches by (o,d,date)."""
        cache_key = (origin, dest, date)
        if cache_key in fetch_leg.cache:
            return fetch_leg.cache[cache_key]
        if total_calls[0] > 0:
            sleep_s = random.uniform(2.5, 5.5)
            print(f"  [throttle] sleeping {sleep_s:.1f}s …")
            time.sleep(sleep_s)
        try:
            best, signal = cheapest_oneway(query, origin, dest, date, currency)
            total_calls[0] += 1
            tag = f"₹{best['price_inr']:,}" if best else "no INR fare"
            print(f"  [LEG] {origin}→{dest} {date}: {tag}")
        except Exception as exc:
            best, signal = None, None
            print(f"  [ERROR] {origin}→{dest} {date}: {exc}")
        fetch_leg.cache[cache_key] = (best, signal)
        return best, signal
    fetch_leg.cache = {}

    combos = [
        (days_out, stay)
        for days_out in cfg["date_windows_days_out"]
        for stay in cfg["stay_nights"]
    ]

    for route in routes:
        o, d = route["from"], route["to"]
        key = f"{o}-{d}"
        best_combo = None

        for days_out, stay in combos:
            dep_dt = now + timedelta(days=days_out)
            ret_dt = dep_dt + timedelta(days=stay)
            dep = dep_dt.strftime("%Y-%m-%d")
            ret = ret_dt.strftime("%Y-%m-%d")

            out_best, out_sig = fetch_leg(o, d, dep)   # outbound leg
            ret_best, _ = fetch_leg(d, o, ret)         # return leg
            if not out_best or not ret_best:
                continue

            total = out_best["price_inr"] + ret_best["price_inr"]
            if best_combo is None or total < best_combo["price_inr"]:
                best_combo = {
                    "origin": o,
                    "dest": d,
                    "price_inr": total,
                    "airline": f"{out_best['airline']} + {ret_best['airline']}",
                    "stops": out_best["stops"],
                    "depart_date": dep,
                    "return_date": ret,
                    "google_signal": out_sig,
                    "google_flights_url": build_google_url(o, d, dep, ret),
                    "fare_type": "separate_tickets",
                    "outbound_airline": out_best["airline"],
                    "outbound_stops": out_best["stops"],
                    "outbound_price_inr": out_best["price_inr"],
                    "return_airline": ret_best["airline"],
                    "return_stops": ret_best["stops"],
                    "return_price_inr": ret_best["price_inr"],
                }

        if best_combo:
            candidates[key] = best_combo
            print(f"  [CHEAPEST] {key}: ₹{best_combo['price_inr']:,} "
                  f"({best_combo['outbound_price_inr']:,} + {best_combo['return_price_inr']:,}) "
                  f"dep {best_combo['depart_date']} ret {best_combo['return_date']}")
            history_records.append({
                "route": key,
                "price_inr": best_combo["price_inr"],
                "ts": now.isoformat(),
            })
        else:
            print(f"  [NO CANDIDATE] {key}: no valid INR legs found across all windows")

    print()
    deals = merge_deals(cfg, candidates, cities, now, scanned_keys)
    publish(cfg, deals, now, history_records)

    print(f"\n=== SCAN COMPLETE ===")
    print(f"  Routes scanned : {sorted(scanned_keys)}")
    print(f"  API calls made : {total_calls[0]}")
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
