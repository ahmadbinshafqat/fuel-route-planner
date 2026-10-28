"""Turn user input ("Chicago, IL", "41.88,-87.63", or a free-form address) into coordinates.

Resolution order, cheapest first:
  1. "lat,lon" literal                      -> no lookup
  2. "City, ST" / "City, State"             -> local Census Gazetteer table (no network)
  3. anything else                          -> one Nominatim request (cached)
"""

import hashlib
import re
from dataclasses import dataclass

import requests
from django.conf import settings
from django.core.cache import cache

from routing.models import Place

STATES = {
    "AL": "Alabama", "AK": "Alaska", "AZ": "Arizona", "AR": "Arkansas", "CA": "California",
    "CO": "Colorado", "CT": "Connecticut", "DE": "Delaware", "DC": "District of Columbia",
    "FL": "Florida", "GA": "Georgia", "HI": "Hawaii", "ID": "Idaho", "IL": "Illinois",
    "IN": "Indiana", "IA": "Iowa", "KS": "Kansas", "KY": "Kentucky", "LA": "Louisiana",
    "ME": "Maine", "MD": "Maryland", "MA": "Massachusetts", "MI": "Michigan", "MN": "Minnesota",
    "MS": "Mississippi", "MO": "Missouri", "MT": "Montana", "NE": "Nebraska", "NV": "Nevada",
    "NH": "New Hampshire", "NJ": "New Jersey", "NM": "New Mexico", "NY": "New York",
    "NC": "North Carolina", "ND": "North Dakota", "OH": "Ohio", "OK": "Oklahoma", "OR": "Oregon",
    "PA": "Pennsylvania", "RI": "Rhode Island", "SC": "South Carolina", "SD": "South Dakota",
    "TN": "Tennessee", "TX": "Texas", "UT": "Utah", "VT": "Vermont", "VA": "Virginia",
    "WA": "Washington", "WV": "West Virginia", "WI": "Wisconsin", "WY": "Wyoming",
}
STATE_BY_NAME = {name.lower(): code for code, name in STATES.items()}

# Census LSAD suffixes ("Abbeville city", "Abanda CDP", "Nashville-Davidson metropolitan government (balance)").
_GAZ_SUFFIX = re.compile(
    r"\s+(city and borough|consolidated government|metropolitan government|unified government|urban county"
    r"|city|town|village|borough|cdp|municipality|charter township|township|plantation|gore|grant|location"
    r"|purchase|comunidad|zona urbana|ccd|precinct|district|census subarea|unorganized territory|ut)(\s*\(.*\))?$"
)
_COORDS = re.compile(r"^\s*(-?\d+(?:\.\d+)?)\s*,\s*(-?\d+(?:\.\d+)?)\s*$")

# Rough boxes for the 50 states + DC: contiguous US, Alaska, Hawaii.
_US_BOXES = ((24.3, 49.5, -125.0, -66.8), (51.0, 71.6, -180.0, -129.9), (18.8, 22.4, -160.5, -154.7))


class GeocodingError(ValueError):
    pass


@dataclass(frozen=True)
class Location:
    lat: float
    lon: float
    label: str
    source: str  # "coordinates" | "gazetteer" | "nominatim"

    def as_dict(self):
        return {"query_resolved_to": self.label, "lat": round(self.lat, 6), "lon": round(self.lon, 6), "source": self.source}


def normalize_name(name: str, *, gazetteer: bool = False) -> str:
    """Canonical key for matching place names: "St. Johns" == "Saint Johns" == "SAINT JOHNS "."""
    s = name.strip().lower()
    if gazetteer:
        s = _GAZ_SUFFIX.sub("", s)
    s = re.sub(r"\(.*?\)", "", s)
    s = re.sub(r"\bst\.?\s", "saint ", s)
    s = re.sub(r"\bste\.?\s", "sainte ", s)
    s = re.sub(r"\bmt\.?\s", "mount ", s)
    s = re.sub(r"\bft\.?\s", "fort ", s)
    return re.sub(r"[^a-z]", "", s)


def in_usa(lat: float, lon: float) -> bool:
    return any(lo_lat <= lat <= hi_lat and lo_lon <= lon <= hi_lon for lo_lat, hi_lat, lo_lon, hi_lon in _US_BOXES)


def parse_state(token: str) -> str | None:
    t = token.strip().rstrip(".").strip()
    if t.upper() in STATES:
        return t.upper()
    return STATE_BY_NAME.get(t.lower())


def lookup_place(city: str, state: str) -> Place | None:
    return Place.objects.filter(state=state, key=normalize_name(city)).order_by("priority").first()


def _states_matching(prefix: str) -> list[str]:
    p = prefix.strip().rstrip(".").lower()
    return [code for code, name in STATES.items() if code.lower().startswith(p) or name.lower().startswith(p)]


def suggest_places(query: str, limit: int = 8) -> list[dict]:
    """Autocomplete over the local Census places, most populous first. No external calls.

    Accepts "new", "new york", "springfield, il", "springfield, ill" and "austin tx".
    Every returned label is "Name, ST", which geocode() resolves exactly.
    """
    q = (query or "").strip()
    city, states = q, None
    if "," in q:
        city, _, state_part = q.partition(",")
        if state_part.strip():
            states = _states_matching(state_part)
    key = normalize_name(city)
    if len(key) < 2 or states == []:
        return []

    def run(key, states):
        qs = Place.objects.filter(key__startswith=key)
        if states:
            qs = qs.filter(state__in=states)
        # Over-fetch: alias keys can repeat a place and are de-duplicated below.
        return list(qs.order_by("-population", "priority", "name").values("name", "state", "lat", "lon")[: limit * 3])

    rows = run(key, states)
    if not rows and states is None:
        split = _split_city_state(q)  # "austin tx" without a comma
        if split:
            rows = run(normalize_name(split[0]), [split[1]])

    out, seen = [], set()
    for r in rows:
        ident = (r["name"], r["state"])
        if ident in seen:  # alias keys ("nashville" -> "Nashville-Davidson") point at the same place
            continue
        seen.add(ident)
        out.append({"label": f"{r['name']}, {r['state']}", "state_name": STATES[r["state"]],
                    "lat": round(r["lat"], 5), "lon": round(r["lon"], 5)})
        if len(out) == limit:
            break
    return out


def _split_city_state(query: str) -> tuple[str, str] | None:
    """'Austin, TX' / 'Austin TX' / 'Austin, Texas' / 'Austin, TX, USA' -> ('Austin', 'TX')."""
    q = re.sub(r",?\s*(usa|us|united states( of america)?)\s*$", "", query.strip(), flags=re.I)
    if "," in q:
        city, _, rest = q.rpartition(",")
        state = parse_state(rest)
        if state and city.strip() and "," not in city:
            return city.strip(), state
        return None
    parts = q.split()
    for n in (2, 1):  # "New York NY", "Salt Lake City Utah", "Sioux Falls South Dakota"
        if len(parts) > n:
            state = parse_state(" ".join(parts[-n:]))
            if state:
                return " ".join(parts[:-n]), state
    return None


def _nominatim(query: str) -> Location:
    cfg = settings.FUEL_ROUTE
    try:
        resp = requests.get(
            f"{cfg['NOMINATIM_BASE_URL']}/search",
            params={"q": query, "format": "jsonv2", "countrycodes": "us", "limit": 1},
            headers={"User-Agent": cfg["USER_AGENT"]},
            timeout=cfg["HTTP_TIMEOUT_SECONDS"],
        )
        resp.raise_for_status()
        hits = resp.json()
    except (requests.RequestException, ValueError) as exc:
        raise GeocodingError(f"Geocoding service unavailable for {query!r}: {exc}") from exc
    if not hits:
        raise GeocodingError(f"Could not find a US location matching {query!r}.")
    hit = hits[0]
    return Location(float(hit["lat"]), float(hit["lon"]), hit.get("display_name", query), "nominatim")


def geocode(query: str, calls: list | None = None) -> Location:
    """Resolve a location. Appends "nominatim" to `calls` if an external request was made."""
    query = (query or "").strip()
    if not query:
        raise GeocodingError("Location is required.")
    if len(query) > 200:
        raise GeocodingError("Location is too long.")

    m = _COORDS.match(query)
    if m:
        loc = Location(float(m.group(1)), float(m.group(2)), query, "coordinates")
    else:
        loc = None
        split = _split_city_state(query)
        if split:
            place = lookup_place(*split)
            if place:
                loc = Location(place.lat, place.lon, f"{place.name}, {place.state}", "gazetteer")
        if loc is None:
            cache_key = "geocode:" + hashlib.sha1(" ".join(query.lower().split()).encode()).hexdigest()
            loc = cache.get(cache_key)
            if loc is None:
                loc = _nominatim(query)
                if calls is not None:
                    calls.append("nominatim")
                cache.set(cache_key, loc)

    if not in_usa(loc.lat, loc.lon):
        raise GeocodingError(f"{query!r} resolves to ({loc.lat:.4f}, {loc.lon:.4f}), which is outside the USA.")
    return loc
