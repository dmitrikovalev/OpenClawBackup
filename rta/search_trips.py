"""Search RTA intercity bus trips via the public B2C API used by bus.rta.ae.

Read-only: only searches, never books.

Examples:
  python search_trips.py --from abu-dhabi --to dubai --date 2026-10-30 --time 06:00 --until 12:00 --pax 3
  python search_trips.py --from dubai --to abu-dhabi --date 2026-10-30 --json
"""
import argparse
import datetime as dt
import json
import sys
import urllib.error
import urllib.parse
import urllib.request

API = "https://bus-api.rta.ae/api/v1/b2c/booking"
PROVIDER_ID = 1
DUBAI_TZ = dt.timezone(dt.timedelta(hours=4))  # Asia/Dubai, no DST

# Location IDs found during reconnaissance (bus-api.rta.ae locations/by-name)
STATIONS = {
    "abu-dhabi": 4,   # Abu Dhabi, Central Bus Station
    "dubai": 6,       # Dubai, Ibn Battuta Bus Station
}

HEADERS = {
    "Accept": "application/json",
    "Accept-Language": "en",
    "Origin": "https://bus.rta.ae",
    "Referer": "https://bus.rta.ae/",
    "User-Agent": "Mozilla/5.0 (X11; Linux aarch64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/156.0 Safari/537.36",
}


def get_json(path, params):
    params = {**params, "providerId": PROVIDER_ID}
    url = f"{API}/{path}?{urllib.parse.urlencode(params)}"
    req = urllib.request.Request(url, headers=HEADERS)
    with urllib.request.urlopen(req, timeout=20) as resp:
        return json.load(resp)


def find_route(dep_location, dest_location):
    data = get_json("route-patterns/by-locations", {
        "DepartureLocationId": dep_location,
        "DestinationLocationId": dest_location,
    })
    items = data.get("data", {}).get("items", [])
    if not items:
        raise SystemExit("No route found for this direction")
    route = items[0]
    boarding = next(s for s in route["stops"]
                    if s["locationId"] == dep_location and s["isBoardingAllowed"])
    alighting = next(s for s in route["stops"]
                     if s["locationId"] == dest_location and s["isAlightingAllowed"])
    return route, boarding, alighting


def search(dep, dest, date, time_from, time_until, pax, max_pages=5):
    route, boarding, alighting = find_route(STATIONS[dep], STATIONS[dest])

    local_start = dt.datetime.combine(date, time_from, tzinfo=DUBAI_TZ)
    utc_start = local_start.astimezone(dt.timezone.utc)
    local_until = dt.datetime.combine(date, time_until, tzinfo=DUBAI_TZ)

    trips = []
    for page in range(1, max_pages + 1):
        data = get_json(f"route-patterns/{route['id']}/scheduled-trips", {
            "Page": page,
            "PageSize": 20,
            "DateUtc": utc_start.strftime("%Y-%m-%d"),
            "TimeUtc": utc_start.strftime("%H:%M:%S"),
            "RequiredStandardCount": pax,
            "RequiredWheelchairCount": 0,
            "RequiredPODCount": 0,
            "BoardingPatternStopId": boarding["id"],
            "AlightingPatternStopId": alighting["id"],
        })
        items = data.get("data", {}).get("items", [])
        if not items:
            break
        stop = False
        for t in items:
            start = dt.datetime.combine(
                dt.date.fromisoformat(t["date"]),
                dt.time.fromisoformat(t["plannedStartTime"]),
                tzinfo=DUBAI_TZ,
            )
            if start > local_until:
                stop = True
                break
            standard = t.get("availableSeatCounts", {}).get("Standard", 0)
            if t.get("status") != "Planned" or standard < pax:
                continue
            trips.append({
                "trip_id": t["id"],
                "date": t["date"],
                "departure": t["plannedStartTime"][:5],
                "arrival": t["plannedEndTime"][:5],
                "seats_standard": standard,
                "price_per_ticket": route["costAmount"],
                "total": round(route["costAmount"] * pax, 2),
                "currency": route["costCurrency"],
            })
        if stop:
            break

    return {
        "route": route["code"],
        "title": route["title"],
        "route_pattern_id": route["id"],
        "boarding_stop_id": boarding["id"],
        "alighting_stop_id": alighting["id"],
        "passengers": pax,
        "trips": trips,
    }


def main():
    p = argparse.ArgumentParser(description="Search RTA intercity bus trips")
    p.add_argument("--from", dest="dep", choices=STATIONS, required=True)
    p.add_argument("--to", dest="dest", choices=STATIONS, required=True)
    p.add_argument("--date", type=dt.date.fromisoformat,
                   default=dt.datetime.now(DUBAI_TZ).date())
    p.add_argument("--time", type=dt.time.fromisoformat, default=dt.time(0, 0))
    p.add_argument("--until", type=dt.time.fromisoformat, default=dt.time(23, 59))
    p.add_argument("--pax", type=int, default=1)
    p.add_argument("--json", action="store_true", help="machine-readable output")
    a = p.parse_args()

    if a.dep == a.dest:
        sys.exit("From and to must differ")
    if a.pax < 1:
        sys.exit("--pax must be at least 1")

    # Do not search in the past for today
    now = dt.datetime.now(DUBAI_TZ)
    time_from = a.time
    if a.date == now.date() and time_from < now.time():
        time_from = now.time().replace(second=0, microsecond=0)

    try:
        result = search(a.dep, a.dest, a.date, time_from, a.until, a.pax)
    except (urllib.error.URLError, TimeoutError) as e:
        detail = e.read().decode(errors="replace")[:500] if isinstance(e, urllib.error.HTTPError) else ""
        msg = f"API request failed: {e} {detail}".strip()
        if a.json:
            print(json.dumps({"error": msg}, ensure_ascii=False))
            sys.exit(1)
        sys.exit(msg)

    if a.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return

    print(f"{result['title']} | {a.date} | пассажиров: {a.pax}")
    if not result["trips"]:
        print("Подходящих рейсов нет")
    for t in result["trips"]:
        print(f"  {t['departure']} → {t['arrival']}  мест: {t['seats_standard']:>2}  "
              f"{t['total']:.2f} {t['currency']}  (trip_id {t['trip_id']})")


if __name__ == "__main__":
    main()