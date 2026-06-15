"""
faredrop-data scanner
Run: python scanner.py        (live scrape via fast-flights)
     python scanner.py --demo (seed data, no internet, no fast-flights)
"""

import argparse
import json
import math
import os
import random
import re
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from statistics import median
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


def compute_typical(route_key, history_records, cfg):
    cutoff = datetime.now(timezone.utc) - timedelta(days=cfg["history_days"])
    prices = [
        r["price_inr"]
        for r in history_records
        if r["route"] == route_key
        and datetime.fromisoformat(r["ts"]) >= cutoff
    ]
    return prices


def prune_history(history_records, history_days):
    cutoff = datetime.now(timezone.utc) - timedelta(days=history_days)
    return [
        r for r in history_records
        if datetime.fromisoformat(r["ts"]) >= cutoff
    ]


def build_deal(candidate, history_records, cfg, cities):
    origin = candidate["origin"]
    dest = candidate["dest"]
    route_key = f"{origin}-{dest}"
    price_inr = candidate["price_inr"]

    prices = compute_typical(route_key, history_records, cfg)
    now = datetime.now(timezone.utc)

    if len(prices) >= cfg["min_samples_for_typical"]:
        typ = int(median(prices))
        drop = typ - price_inr
        if drop <= 0:
            return None
        drop_pct = round(drop / typ * 100)
        if drop_pct < int(cfg["min_drop_to_show"] * 100):
            return None

        # lowest_in_days: is this <= historical min AND history spans >= 14 days?
        lowest_in_days = None
        route_records = [
            r for r in history_records
            if r["route"] == route_key
            and datetime.fromisoformat(r["ts"]) >= now - timedelta(days=cfg["history_days"])
        ]
        if route_records and price_inr <= min(r["price_inr"] for r in route_records):
            oldest = min(datetime.fromisoformat(r["ts"]) for r in route_records)
            span = (now - oldest).days
            if span >= 14:
                lowest_in_days = min(span, cfg["history_days"])

        deal = {
            "id": route_key,
            "origin": origin,
            "destination": dest,
            "origin_city": cities.get(origin, origin),
            "dest_city": cities.get(dest, dest),
            "price_inr": price_inr,
            "typical_inr": typ,
            "drop_pct": drop_pct,
            "savings_inr": drop,
            "family_savings_inr": drop * cfg["family_size_for_savings"],
            "lowest_in_days": lowest_in_days,
            "typical_source": "history",
            "google_signal": candidate.get("google_signal"),
            "airline": candidate["airline"],
            "stops": candidate["stops"],
            "depart_date": candidate["depart_date"],
            "return_date": candidate["return_date"],
            "google_flights_url": candidate["google_flights_url"],
        }
        return deal
    else:
        # cold-start: only include if google says "low"
        if candidate.get("google_signal") != "low":
            return None
        deal = {
            "id": route_key,
            "origin": origin,
            "destination": dest,
            "origin_city": cities.get(origin, origin),
            "dest_city": cities.get(dest, dest),
            "price_inr": price_inr,
            "typical_inr": None,
            "drop_pct": None,
            "savings_inr": None,
            "family_savings_inr": None,
            "lowest_in_days": None,
            "typical_source": "google",
            "google_signal": candidate.get("google_signal"),
            "airline": candidate["airline"],
            "stops": candidate["stops"],
            "depart_date": candidate["depart_date"],
            "return_date": candidate["return_date"],
            "google_flights_url": candidate["google_flights_url"],
        }
        return deal


def sort_deals(deals):
    def key(d):
        dp = d["drop_pct"] if d["drop_pct"] is not None else -1
        return (-dp, d["price_inr"])
    return sorted(deals, key=key)


# ---------------------------------------------------------------------------
# DEMO MODE
# ---------------------------------------------------------------------------

DEMO_AIRLINES = ["IndiGo", "Air India", "SpiceJet", "Vistara", "GoAir", "Emirates", "flydubai"]
DEMO_STOPS = [0, 1, 1, 0, 2]


def demo_base_price(origin, dest):
    """Deterministic-ish base price per route for demo seeding."""
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
    routes = cfg["routes"]

    # Build rich history so most routes clear 20% floor
    history_records = []
    random.seed(42)

    # Routes that will be "cold start" (< min_samples_for_typical)
    cold_routes = {"DEL-KTM", "BLR-SIN"}

    for route in routes:
        o, d = route["from"], route["to"]
        key = f"{o}-{d}"
        base = demo_base_price(o, d)

        if key in cold_routes:
            # Only seed 3 samples — won't reach threshold
            n_samples = 3
        else:
            n_samples = random.randint(12, 22)

        for i in range(n_samples):
            # Prices 15–45% above today's cheap price (so today looks like a deal)
            multiplier = 1.0 + random.uniform(0.15, 0.45)
            price = int(base * multiplier)
            # Spread over last 60 days
            days_ago = random.uniform(1, 60)
            ts = (now - timedelta(days=days_ago)).isoformat()
            history_records.append({"route": key, "price_inr": price, "ts": ts})

    # Build candidates — today's "scraped" prices (30–40% below typical)
    candidates = {}
    for route in routes:
        o, d = route["from"], route["to"]
        key = f"{o}-{d}"
        base = demo_base_price(o, d)

        # Today's deal price = base (which is well below seeded history)
        price = base + random.randint(-1000, 1000)
        if price < 1000:
            price = 1000

        days_out = random.choice(cfg["date_windows_days_out"])
        stay = random.choice(cfg["stay_nights"])
        dep = (now + timedelta(days=days_out)).strftime("%Y-%m-%d")
        ret = (now + timedelta(days=days_out + stay)).strftime("%Y-%m-%d")

        airline = random.choice(DEMO_AIRLINES)
        stops = random.choice(DEMO_STOPS)

        # Cold-start routes get google_signal="low"; warm routes get "low" too for realism
        google_signal = "low" if key in cold_routes else random.choice(["low", "low", "typical"])

        # Add today's price to history too
        history_records.append({
            "route": key,
            "price_inr": price,
            "ts": now.isoformat(),
        })

        candidates[key] = {
            "origin": o,
            "dest": d,
            "price_inr": price,
            "airline": airline,
            "stops": stops,
            "depart_date": dep,
            "return_date": ret,
            "google_signal": google_signal,
            "google_flights_url": build_google_url(o, d, dep, ret),
        }

        print(f"  [DEMO] {key}: ₹{price:,} ({airline}, {stops} stop(s)) dep {dep} ret {ret} signal={google_signal}")

    # Seed a couple routes with lowest_in_days by making today's price the all-time low
    # and ensuring oldest record is >= 14 days ago (already done by spreading 1–60 days)

    # Compute deals
    deals = []
    for key, candidate in candidates.items():
        deal = build_deal(candidate, history_records, cfg, cities)
        if deal:
            deals.append(deal)
        else:
            o, d = candidate["origin"], candidate["dest"]
            prices = compute_typical(key, history_records, cfg)
            reason = (
                f"cold-start signal={candidate.get('google_signal')}"
                if len(prices) < cfg["min_samples_for_typical"]
                else f"drop insufficient (samples={len(prices)})"
            )
            print(f"  [SKIP] {key}: {reason}")

    deals = sort_deals(deals)

    output = {
        "generated_at": now.isoformat().replace("+00:00", "Z"),
        "routes_watched": len(routes),
        "deals": deals,
    }

    history_records = prune_history(history_records, cfg["history_days"])
    save_history(history_records)
    save_deals(output)

    print(f"\n=== DEMO COMPLETE ===")
    print(f"  History records : {len(history_records)}")
    print(f"  Deals found     : {len(deals)}")
    for d in deals:
        src = d["typical_source"]
        if src == "history":
            print(f"  {d['id']}: ₹{d['price_inr']:,} | -{d['drop_pct']}% | save ₹{d['savings_inr']:,} | family ₹{d['family_savings_inr']:,} | lowest_in_days={d['lowest_in_days']}")
        else:
            print(f"  {d['id']}: ₹{d['price_inr']:,} | google={d['google_signal']} (cold-start)")
    print(f"\nWrote: {DEALS_FILE}, {DEALS_JS_FILE}, {HISTORY_FILE}")


# ---------------------------------------------------------------------------
# LIVE MODE
# ---------------------------------------------------------------------------

def run_live(cfg):
    # Lazy-import fast-flights ONLY in the real path.
    # We call get_flights_from_filter directly (instead of the public get_flights
    # wrapper) because only the lower-level function plumbs through `currency`.
    # The public wrapper sends curr="" so Google serves the runner's geo currency
    # (USD on US-hosted CI), which the INR currency guard then rejects wholesale.
    # Forcing currency="INR" makes Google price the page in ₹ regardless of IP.
    from fast_flights.core import get_flights_from_filter  # noqa: lazy import
    from fast_flights.filter import TFSData  # noqa: lazy import
    from fast_flights.flights_impl import FlightData, Passengers  # noqa: lazy import

    currency = cfg.get("currency", "INR")

    now = datetime.now(timezone.utc)
    cities = cfg["cities"]
    routes = cfg["routes"]
    history_records = load_history()

    candidates = {}
    total_calls = 0

    for route in routes:
        o, d = route["from"], route["to"]
        key = f"{o}-{d}"
        best = None

        combos = [
            (days_out, stay)
            for days_out in cfg["date_windows_days_out"]
            for stay in cfg["stay_nights"]
        ]

        for days_out, stay in combos:
            dep_dt = now + timedelta(days=days_out)
            ret_dt = dep_dt + timedelta(days=stay)
            dep = dep_dt.strftime("%Y-%m-%d")
            ret = ret_dt.strftime("%Y-%m-%d")

            if total_calls > 0:
                sleep_s = random.uniform(2.5, 5.5)
                print(f"  [throttle] sleeping {sleep_s:.1f}s …")
                time.sleep(sleep_s)

            try:
                result = get_flights_from_filter(
                    TFSData.from_interface(
                        flight_data=[
                            FlightData(date=dep, from_airport=o, to_airport=d),
                            FlightData(date=ret, from_airport=d, to_airport=o),
                        ],
                        trip="round-trip",
                        seat="economy",
                        passengers=Passengers(
                            adults=1, children=0,
                            infants_in_seat=0, infants_on_lap=0,
                        ),
                    ),
                    currency=currency,
                    mode="fallback",
                )
                total_calls += 1
                google_signal = getattr(result, "current_price", None)

                for f in result.flights:
                    price_inr = parse_inr(f.price)
                    if price_inr is None:
                        print(f"  [SKIP non-INR] {key} {dep}: {f.price!r}")
                        continue

                    if best is None or price_inr < best["price_inr"]:
                        best = {
                            "origin": o,
                            "dest": d,
                            "price_inr": price_inr,
                            "airline": f.name,
                            "stops": f.stops,
                            "depart_date": dep,
                            "return_date": ret,
                            "google_signal": google_signal,
                            "google_flights_url": build_google_url(o, d, dep, ret),
                        }

                print(f"  [OK] {key} {dep}→{ret}: best so far ₹{best['price_inr']:,} | signal={google_signal}" if best else f"  [OK] {key} {dep}: no INR fares")

            except Exception as exc:
                print(f"  [ERROR] {key} {dep}: {exc}")
                continue

        if best:
            candidates[key] = best
            # Record ONLY the cheapest valid INR fare for this route this scan.
            # 'typical' is then the median of the usual cheapest price over time,
            # so drop% means "today's cheapest vs the typical cheapest" — a real
            # deal signal, not noise from premium/multi-stop fares in the pool.
            history_records.append({
                "route": key,
                "price_inr": best["price_inr"],
                "ts": now.isoformat(),
            })
        else:
            print(f"  [NO CANDIDATE] {key}: no valid INR fares found across all windows")

    # Compute deals
    deals = []
    for key, candidate in candidates.items():
        deal = build_deal(candidate, history_records, cfg, cities)
        if deal:
            deals.append(deal)
            print(f"  [DEAL] {key}: ₹{candidate['price_inr']:,} → included")
        else:
            prices = compute_typical(key, history_records, cfg)
            reason = (
                f"cold-start signal={candidate.get('google_signal')}"
                if len(prices) < cfg["min_samples_for_typical"]
                else "drop < threshold or not positive"
            )
            print(f"  [FILTERED] {key}: {reason}")

    deals = sort_deals(deals)

    # Always keep price history fresh (it feeds the 'typical' median).
    history_records = prune_history(history_records, cfg["history_days"])
    save_history(history_records)

    # Never blow away the last good deals.json. If this scan found nothing
    # (network failure, all routes errored, or everything filtered out),
    # leave the previously published cheapest deals in place rather than
    # overwriting them with an empty list.
    if deals:
        output = {
            "generated_at": now.isoformat().replace("+00:00", "Z"),
            "routes_watched": len(routes),
            "deals": deals,
        }
        save_deals(output)
        wrote = f"{DEALS_FILE}, {DEALS_JS_FILE}, {HISTORY_FILE}"
    else:
        print("  [KEEP] no deals this scan — preserving previous deals.json")
        wrote = str(HISTORY_FILE)

    print(f"\n=== SCAN COMPLETE ===")
    print(f"  API calls made  : {total_calls}")
    print(f"  History records : {len(history_records)}")
    print(f"  Deals published : {len(deals)}")
    print(f"\nWrote: {wrote}")


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
