"""In-memory spatial index of fuel stations.

All ~6.7k stations are loaded once per process into numpy arrays. Matching a
route against them is a KD-tree query over the densified route polyline, which
takes a few milliseconds even for coast-to-coast trips.
"""

import threading
from dataclasses import dataclass

import numpy as np
from scipy.spatial import cKDTree

EARTH_RADIUS_MILES = 3958.8

_lock = threading.Lock()
_index = None


@dataclass
class StationIndex:
    ids: np.ndarray
    lat: np.ndarray
    lon: np.ndarray
    price: np.ndarray
    rows: list  # dicts with display fields, aligned with the arrays

    def __len__(self):
        return len(self.ids)


@dataclass
class CorridorStation:
    row: dict
    price: float
    route_mile: float      # distance from the start, along the route, of the closest route point
    offset_miles: float    # straight-line distance from the route to the station
    route_lat: float       # closest point on the route, where the stop is drawn
    route_lon: float


def to_xyz(lat, lon):
    lat, lon = np.radians(lat), np.radians(lon)
    return np.column_stack((np.cos(lat) * np.cos(lon), np.cos(lat) * np.sin(lon), np.sin(lat)))


def haversine_miles(lat1, lon1, lat2, lon2):
    lat1, lon1, lat2, lon2 = map(np.radians, (lat1, lon1, lat2, lon2))
    a = np.sin((lat2 - lat1) / 2) ** 2 + np.cos(lat1) * np.cos(lat2) * np.sin((lon2 - lon1) / 2) ** 2
    return 2 * EARTH_RADIUS_MILES * np.arcsin(np.sqrt(np.clip(a, 0, 1)))


def get_index() -> StationIndex:
    global _index
    if _index is None:
        with _lock:
            if _index is None:
                _index = _build()
    return _index


def invalidate():
    global _index
    with _lock:
        _index = None


def _build() -> StationIndex:
    from routing.models import FuelStation

    rows = list(
        FuelStation.objects.order_by("id").values("opis_id", "name", "address", "city", "state", "price", "lat", "lon")
    )
    for r in rows:
        r["price"] = float(r["price"])
    return StationIndex(
        ids=np.array([r["opis_id"] for r in rows], dtype=np.int64),
        lat=np.array([r["lat"] for r in rows], dtype=float),
        lon=np.array([r["lon"] for r in rows], dtype=float),
        price=np.array([r["price"] for r in rows], dtype=float),
        rows=rows,
    )


def densify(lat, lon, max_step_miles=0.5):
    """Insert points so no gap exceeds max_step_miles, returning (lat, lon, cumulative_miles).

    OSRM leaves long straight interstate stretches as a single segment; without
    densifying, a station beside the middle of such a segment would look far away.
    """
    seg = haversine_miles(lat[:-1], lon[:-1], lat[1:], lon[1:])
    cum = np.concatenate(([0.0], np.cumsum(seg)))
    pieces = np.maximum(1, np.ceil(seg / max_step_miles).astype(int))
    # fractional positions within each segment: 0, 1/n, ..., (n-1)/n
    seg_idx = np.repeat(np.arange(len(seg)), pieces)
    starts = np.repeat(np.cumsum(pieces) - pieces, pieces)
    frac = (np.arange(len(seg_idx)) - starts) / pieces[seg_idx]
    d_lat = lat[seg_idx] + (lat[seg_idx + 1] - lat[seg_idx]) * frac
    d_lon = lon[seg_idx] + (lon[seg_idx + 1] - lon[seg_idx]) * frac
    d_cum = cum[seg_idx] + seg[seg_idx] * frac
    return (np.append(d_lat, lat[-1]), np.append(d_lon, lon[-1]), np.append(d_cum, cum[-1]))


def stations_along(route_lat, route_lon, route_total_miles, corridor_miles) -> list[CorridorStation]:
    """Stations within corridor_miles of the route, each projected to a mile marker on the route."""
    idx = get_index()
    if not len(idx):
        return []

    d_lat, d_lon, d_cum = densify(np.asarray(route_lat, float), np.asarray(route_lon, float))
    # Scale so mile markers agree with OSRM's road distance.
    if d_cum[-1] > 0:
        d_cum = d_cum * (route_total_miles / d_cum[-1])

    # Cheap bounding-box prefilter (1 degree latitude ~ 69 miles).
    pad = corridor_miles / 69.0 + 0.05
    lon_pad = pad / max(np.cos(np.radians(np.abs(d_lat).max())), 0.2)
    mask = (
        (idx.lat >= d_lat.min() - pad) & (idx.lat <= d_lat.max() + pad)
        & (idx.lon >= d_lon.min() - lon_pad) & (idx.lon <= d_lon.max() + lon_pad)
    )
    cand = np.flatnonzero(mask)
    if not len(cand):
        return []

    tree = cKDTree(to_xyz(d_lat, d_lon))
    max_chord = 2 * np.sin(corridor_miles / (2 * EARTH_RADIUS_MILES))
    chord, nearest = tree.query(to_xyz(idx.lat[cand], idx.lon[cand]), distance_upper_bound=max_chord)
    hit = np.isfinite(chord)

    out = []
    for i, c, n in zip(cand[hit], chord[hit], nearest[hit]):
        out.append(CorridorStation(
            row=idx.rows[i],
            price=float(idx.price[i]),
            route_mile=float(d_cum[n]),
            offset_miles=float(2 * EARTH_RADIUS_MILES * np.arcsin(min(c / 2, 1.0))),
            route_lat=float(d_lat[n]),
            route_lon=float(d_lon[n]),
        ))
    out.sort(key=lambda s: (s.route_mile, s.price))
    return out
