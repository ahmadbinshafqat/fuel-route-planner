"""Single-request routing via the public OSRM server (OpenStreetMap data, no API key)."""

from dataclasses import dataclass

import requests
from django.conf import settings
from django.core.cache import cache

METERS_PER_MILE = 1609.344


class RoutingError(RuntimeError):
    pass


class NoRouteError(RoutingError):
    pass


@dataclass
class Route:
    distance_miles: float
    duration_hours: float
    polyline6: str           # OSRM encoded polyline, precision 6 (what we return to clients)
    coords: list             # decoded [(lat, lon), ...]


def decode_polyline(encoded: str, precision: int = 6) -> list[tuple[float, float]]:
    """Google encoded-polyline algorithm."""
    coords, index, lat, lon = [], 0, 0, 0
    factor = 10 ** precision
    length = len(encoded)
    while index < length:
        for which in (0, 1):
            shift = result = 0
            while True:
                b = ord(encoded[index]) - 63
                index += 1
                result |= (b & 0x1F) << shift
                shift += 5
                if b < 0x20:
                    break
            delta = ~(result >> 1) if result & 1 else result >> 1
            if which == 0:
                lat += delta
            else:
                lon += delta
        coords.append((lat / factor, lon / factor))
    return coords


def _to_route(raw: dict) -> Route:
    return Route(
        distance_miles=raw["distance"] / METERS_PER_MILE,
        duration_hours=raw["duration"] / 3600,
        polyline6=raw["geometry"],
        coords=decode_polyline(raw["geometry"]),
    )


def get_routes(start_lat, start_lon, end_lat, end_lon) -> tuple[list[Route], bool]:
    """Fastest road plus one alternative, from a single OSRM call. Returns (routes, served_from_cache)."""
    key = f"osrm:v2:{start_lat:.5f},{start_lon:.5f};{end_lat:.5f},{end_lon:.5f}"
    cached = cache.get(key)
    if cached is not None:
        return cached, True

    cfg = settings.FUEL_ROUTE
    url = f"{cfg['OSRM_BASE_URL']}/route/v1/driving/{start_lon:.6f},{start_lat:.6f};{end_lon:.6f},{end_lat:.6f}"
    try:
        resp = requests.get(
            url,
            # alternatives=1 asks for one extra road besides the fastest.
            params={"overview": "full", "geometries": "polyline6", "steps": "false", "alternatives": "1"},
            headers={"User-Agent": cfg["USER_AGENT"]},
            timeout=cfg["HTTP_TIMEOUT_SECONDS"],
        )
        data = resp.json()
    except (requests.RequestException, ValueError) as exc:
        raise RoutingError(f"Routing service unavailable: {exc}") from exc

    if data.get("code") in ("NoRoute", "NoSegment"):
        raise NoRouteError(data.get("message") or "No drivable route between these locations.")
    if resp.status_code != 200 or data.get("code") != "Ok" or not data.get("routes"):
        raise RoutingError(f"Routing service error: {data.get('code')} {data.get('message', '')}".strip())

    routes = [_to_route(raw) for raw in data["routes"][:2]]
    cache.set(key, routes)
    return routes, False


def get_route(start_lat, start_lon, end_lat, end_lon) -> tuple[Route, bool]:
    """Returns the fastest route and whether it was served from cache."""
    routes, cached = get_routes(start_lat, start_lon, end_lat, end_lon)
    return routes[0], cached
