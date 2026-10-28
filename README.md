# Fuel Route Planner (Django)

Given a start and finish in the USA, returns the driving route, the cheapest places to refuel along it (500-mile range, 10 mpg), and the total fuel cost. It also serves an interactive map of the result.

- **One external call per request.** That call goes to [OSRM](https://project-osrm.org/) for the route (free, no key, OpenStreetMap data). Geocoding of `City, ST` inputs and of all fuel stations happens offline against US Census data. Repeat queries are served from cache with **zero** external calls.
- **Fast.** A cold coast-to-coast request takes about 1 s, almost all of it OSRM. A cached request takes about 20–50 ms. The stop optimizer itself runs in a few ms.
- **Cost-optimal stop selection** with an exact dynamic program, not a heuristic. It is checked in tests against the textbook greedy optimum and against brute force.

## Quick start

Requires Python ≥ 3.12, because Django 6.1 does.

```bash
python3.13 -m venv venv && source venv/bin/activate
pip install -r requirements.txt
python manage.py migrate
python manage.py load_stations --offline   # ~5 s; downloads Census place + population files (~10 MB) on first run
python manage.py runserver
```

Then open http://127.0.0.1:8000/ to use the map UI, or call the API:

```bash
curl "http://127.0.0.1:8000/api/route/?start=New%20York,%20NY&finish=Los%20Angeles,%20CA"
```

Run the tests with `python manage.py test routing` (47 tests, no network needed).

## API

### `GET /api/route/` or `POST /api/route/`

The endpoint accepts query parameters, a form body, or a JSON body.

| param | required | default | meaning |
|---|---|---|---|
| `start`, `finish` | yes | | `"City, ST"`, `"City, State"`, `"lat,lon"`, or any US address. Addresses fall back to Nominatim. |
| `start_fuel` | no | `1` | Fraction of the tank. `1` is full, `0.5` is half. |
| `stop_penalty` | no | `25` | Dollars charged per stop *in the optimizer only*. It stands in for the time a stop costs. `0` gives the pure lowest-fuel-cost plan. See below. |
| `corridor_miles` | no | `10` | Maximum distance from the route for a station to be considered. Widen it if a route has too few stations. |

Response (trimmed):

```json
{
  "start":  {"query_resolved_to": "New York, NY", "lat": 40.662712, "lon": -73.938677, "source": "gazetteer"},
  "finish": {"query_resolved_to": "Los Angeles, CA", "lat": 34.019394, "lon": -118.410825, "source": "gazetteer"},
  "route": {"distance_miles": 2810.4, "duration_hours": 50.3,
            "geometry": {"format": "polyline6", "encoded": "...."}},
  "fuel_stops": [
    {"stop_number": 1, "opis_id": 72445, "name": "SHEETZ #639", "address": "I-80 Exit 223",
     "city": "Youngstown", "state": "OH", "lat": 41.148784, "lon": -80.676182,
     "price_per_gallon": 3.059, "route_mile": 396.0, "distance_off_route_miles": 3.8,
     "fuel_on_arrival_gallons": 10.4, "gallons_purchased": 39.6, "cost": 121.14,
     "reason": "Fuel at the next stop, Minooka, IL, costs more ($3.149): filled the tank."}
  ],
  "summary": {"total_fuel_cost": 744.29, "gallons_purchased": 236.1, "total_gallons_used": 281.04,
              "number_of_stops": 6, "average_price_paid": 3.152, "stations_considered": 460,
              "baseline_fuel_cost": 843.85, "baseline_stops": 6, "savings": 99.56},
  "assumptions": {"vehicle_range_miles": 500, "mpg": 10, "start_fuel_fraction": 1.0, "stop_penalty_dollars": 25, "...": "..."},
  "meta": {"external_api_calls": 1, "external_api_call_detail": ["osrm"], "route_cache_hit": false, "elapsed_ms": 1027.1},
  "map_url": "http://127.0.0.1:8000/api/route/map/?start=New+York%2C+NY&finish=Los+Angeles%2C+CA&start_fuel=1.0"
}
```

`route.geometry` uses [Google's encoded polyline format](https://developers.google.com/maps/documentation/utilities/polylinealgorithm) at precision 6. Any map library can decode it, and so can `@mapbox/polyline` in JS or `polyline` in Python. It is about 20× smaller than GeoJSON for a cross-country route.

Errors return `{"error": "..."}` with these statuses:

- `400`: bad input, location not found, or location outside the USA.
- `422`: no drivable route, or a gap between stations longer than the range.
- `502`: the routing service failed.

### `GET /api/route/map/?start=...&finish=...`

An interactive Leaflet map. It shows the route, numbered fuel-stop pins with price and gallons, the summary, and a form to plan other trips. `/` redirects here.

Start and Finish are autocomplete fields backed by `/api/places/`. Typing "new" lists New York, New Orleans, Newark and so on. Arrow keys and Enter pick an option, and the top match is preselected. The form only submits once each field holds a chosen place, or coordinates typed as `lat, lon`. Unknown names never reach the planner, so the UI doesn't produce "location not found" errors.

### `GET /api/places/?q=new&limit=8`

Place suggestions for the start and finish fields, drawn from the local Census data with no external calls (under 10 ms). Prefix matching ignores case and punctuation, so "st lo" matches St. Louis. Accepts `"springfield, il"`, `"springfield, illinois"` or `"austin tx"`, and returns up to 20 results ranked by 2024 population:

```json
{"query": "new", "results": [{"label": "New York, NY", "state_name": "New York", "lat": 40.66, "lon": -73.94}, "..."]}
```

Every `label` resolves exactly when passed back as `start` or `finish`.

### `GET /api/health/`

Returns liveness and the number of stations loaded.

## How it works

```
start/finish ──► geocode (local Census table; Nominatim only for free-text addresses)
             ──► OSRM /route (1 call, cached by coordinates)
             ──► stations within the corridor (KD-tree over the densified route, ~5 ms)
             ──► DP optimizer (~5 ms) ──► JSON / map
```

**Station data (`manage.py load_stations`).** The CSV has 8,151 rows but only 6,626 distinct US stations. Duplicates of the same OPIS id are collapsed to their lowest listed price, and the Canadian rows are dropped. The CSV has no coordinates, so each station is placed at its town's centroid from the [US Census Gazetteer](https://www.census.gov/geographies/reference-files/time-series/geo/gazetteer-files.html) (places plus county subdivisions, public domain), ranked for autocomplete by the [Census 2024 population estimates](https://www.census.gov/data/tables/time-series/demo/popest/2020s-total-cities-and-towns.html). Names are normalized so that, for example, "St. Johns CDP" matches "SAINT JOHNS". This resolves 6,414 stations. The remaining 212 are in unincorporated communities. They were geocoded once via Nominatim, and 4 by hand, and the results are committed in `data/station_geocode_cache.json`, so `--offline` builds are reproducible with no geocoding traffic. The same Gazetteer table answers `"City, ST"` request inputs, so typical requests never need a geocoding API.

**Matching stations to the route.** The OSRM polyline is densified to points at most 0.5 mi apart, because straight interstate stretches can be a single 30-mile segment. Then a KD-tree on 3-D unit vectors finds every station within `corridor_miles` of the route. Each match gets a mile marker along the route and its off-route distance.

**Choosing stops (`routing/services/optimizer.py`).** The objective is `fuel $ + stop_penalty × stops`. The optimizer is a dynamic program over stations in route order, with fuel in the tank discretized to 1 mile (0.1 gal). The "buy up to level g" step is a prefix minimum, so each station costs a few vectorized numpy operations: O(stations × 500).

- With `stop_penalty=0` the result is exactly the classic min-cost "gas station problem" optimum. The tests check this against the textbook greedy algorithm on 200 random routes.
- That pure optimum is impractical. For NY→LA on a full tank it makes **17 stops** for $716.50, several of them 1–2 gallon top-ups to save a few cents per gallon.
- The default $25 penalty is roughly what a 15–30 minute truck stop costs in driver time. It gives **6 stops for $744.29**, the fewest this trip can be done in, and every purchase is 33–45 gal. A $5 penalty still makes 7 stops, including an 11-gallon top-up, to save $21. Pass a lower `stop_penalty` to trade more stops for cheaper fuel.
- Even with the penalty, a stop does not always fill the tank. If a cheaper station lies within reach, the plan buys just enough to arrive there with the reserve, for example 43.7 of 50 gal. On the last stop it buys only enough to finish with the reserve.
- A stop that would leave the tank within `FILL_UP_WITHIN_GALLONS` (1 gal) of full fills it instead. Later stops buy correspondingly less, so no arrival has less fuel than before. For NY→LA this turns two 49.6-gal stops into fill-ups for 12 cents. It is skipped when `stop_penalty=0`, so that setting stays the pure optimum.
- Each stop carries a one-line `reason`, shown on the map. Examples: "Cheaper fuel at Waco, NE ($2.799) 357 mi ahead: bought just enough to get there", "Fuel at the next stop costs more: filled the tank", or "Within 1 gal of full, so it filled the tank. Leaving that for the cheaper fuel at Gretna, NE ($2.921) would save only $0.09."
- `summary.savings` compares the plan with a driver who doesn't compare prices. That driver fills up at the last station in range before the tank would drop below the reserve, and the last stop buys just enough to finish. It uses the same road, stations and reserve, and makes the fewest stops possible, so the saving does not come from making extra stops. NY→LA: $744.29 against $843.85, both 6 stops, **saving $99.56**. The map shows it as the "saving $" tile.
- `summary.savings_breakdown` splits that saving into two parts:
  - `from_cheaper_stations`: the other driver's cost minus the cost of filling the tank at each of the plan's stops (the last buys just enough to finish).
  - `from_partial_fills`: what buying just enough before a cheaper station saves over filling the tank at those same stops.
  NY→Los Banos: $107.69 from stations plus $3.20 from partial fills. Each partial stop also gives `fill_up_here_would_cost_more`, and its reason ends with "Filling the tank here would cost $1.70 more."
- With a penalty, the tests check the result against brute force over every subset of stops.

**Speed and the API budget.**

- Geocoding is local, so there is one OSRM call per uncached request.
- Free-text addresses add at most two Nominatim calls, for 3 in total, which is within the "two or three" allowance.
- Routes and geocodes are cached for 24 h, keyed on coordinates, so changing `start_fuel` or `stop_penalty` re-plans without any external call.
- The station index loads into memory once per process.
- Responses are gzip-compressed: NY→LA is about 120 KB on the wire.

## Assumptions and limitations

- **Starting fuel.** The vehicle departs with a full tank by default (`start_fuel=1`). `0.5` is half a tank. `total_fuel_cost` is what is paid at the stops, so fuel already in the tank is not charged. If that fraction cannot reach the first station, the error says the number to enter. `0` is an empty tank.
- **Station positions are town centroids.** That is the best the price list allows. Most stations are at interstate exits within a few miles of the centroid, and the default 10-mile corridor absorbs this. The detour to a station is reported, but it is not added to fuel consumed.
- **Coverage follows the price list.** The CSV has no stations in Alaska or Hawaii, and only 8 in California, all near the Mexican border. Routes leaving Northern or Central California, for example Union City, CA → Tulsa, OK, have no station for their first 500+ miles and return `422`. The error names the blocking gap and says whether a wider `corridor_miles` or a higher `start_fuel` would fix it.
- **Endpoints must be in the 50 states or DC.** A route may still pass through Canada (Detroit→Buffalo, for example), but Canadian stations are never used.
- **Shared infrastructure.** The public OSRM and Nominatim servers are shared demo services. For production, point `OSRM_BASE_URL` and `NOMINATIM_BASE_URL` at self-hosted or paid instances and swap the in-process cache for Redis. Both base URLs are environment variables.

## Layout

```
fuelroute/                  Django project (settings, urls)
routing/
  models.py                 Place (Census Gazetteer), FuelStation
  management/commands/load_stations.py
  services/geocoding.py     offline "City, ST" / lat,lon / Nominatim fallback
  services/osrm.py          single routing call, polyline decoding, cache
  services/station_index.py in-memory KD-tree corridor search
  services/optimizer.py     DP stop planner
  services/planner.py       orchestration → response payload
  views.py, urls.py, templates/routing/map.html
  tests.py
data/
  fuel-prices-for-be-assessment.csv
  station_geocode_cache.json
```
