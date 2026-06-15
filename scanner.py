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
        "first_seen": first_seen,
        "last_updated": now.isoformat().replace("+00:00", "Z"),
    }


def merge_deals(cfg, candidates, cities, now):
    """Combine this scan's cheapest fares with the prior cheapest-ever board.

    For each route, keep the stored cheapest fare unless:
      - this scan found a strictly cheaper fare, or
      - the stored fare has expired (its depart date has passed),
    in which case we publish the freshest cheapest fare instead.
    """
    prev_by_route = {d["id"]: d for d in load_deals().get("deals", [])}
    deals = []

    for route in cfg["routes"]:
        key = f"{route['from']}-{route['to']}"
        prior = prev_by_route.get(key)
        prior_valid = prior is not None and not is_expired(prior, now)
        today = candidates.get(key)

        if today and (not prior_valid or today["price_inr"] < prior["price_inr"]):
            # New all-time low (or first/expired sighting) — start its clock.
            deals.append(make_deal(today, cities, now, first_seen=now.isoformat().replace("+00:00", "Z")))
            note = "new low" if prior_valid else ("fresh" if prior is None else "replaced expired")
            print(f"  [TRACK] {key}: ₹{today['price_inr']:,} ({note})")
        elif prior_valid:
            # Keep the standing cheapest-ever; nothing beat it this scan.
            deals.append(prior)
            kept = "no scan" if today is None else f"≥ stored ₹{prior['price_inr']:,}"
            print(f"  [KEEP]  {key}: ₹{prior['price_inr']:,} ({kept})")
        else:
            # No valid stored fare and nothing found this scan — drop it.
            print(f"  [DROP]  {key}: expired/absent and no fare this scan")

    deals.sort(key=lambda d: d["price_inr"])
    return deals


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
    for route in cfg["routes"]:
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
    deals = merge_deals(cfg, candidates, cities, now)
    publish(cfg, deals, now, history_records)

    print(f"\n=== DEMO COMPLETE ===  deals on board: {len(deals)}")


# ---------------------------------------------------------------------------
# LIVE MODE
# ---------------------------------------------------------------------------

def run_live(cfg):
    # Lazy-import fast-flights ONLY in the real path.
    # We call get_flights_from_filter directly (instead of the public get_flights
    # wrapper) because only the lower-level function plumbs through `currency`.
    # Forcing currency="INR" makes Google price the page in ₹ regardless of IP.
    from fast_flights.core import get_flights_from_filter  # noqa: lazy import
    from fast_flights.filter import TFSData  # noqa: lazy import
    from fast_flights.flights_impl import FlightData, Passengers  # noqa: lazy import

    currency = cfg.get("currency", "INR")
    now = datetime.now(timezone.utc)
    cities = cfg["cities"]
    history_records = load_history()

    candidates = {}
    total_calls = 0

    for route in cfg["routes"]:
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
            # Raw log of this scan's cheapest fare (data trail; not used for deals).
            history_records.append({
                "route": key,
                "price_inr": best["price_inr"],
                "ts": now.isoformat(),
            })
        else:
            print(f"  [NO CANDIDATE] {key}: no valid INR fares found across all windows")

    print()
    deals = merge_deals(cfg, candidates, cities, now)
    publish(cfg, deals, now, history_records)

    print(f"\n=== SCAN COMPLETE ===")
    print(f"  API calls made : {total_calls}")
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
