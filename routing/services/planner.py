"""Orchestrates geocode -> route -> corridor stations -> optimal stops -> response payload."""

import math
import time
from dataclasses import dataclass

from django.conf import settings

from . import geocoding, osrm, station_index
from .optimizer import InfeasibleRouteError, fill_tank_at, plan_fuel_stops, top_up_near_full

# Corridor widths tried when explaining why a route is infeasible.
WIDER_CORRIDORS_MILES = (25.0, 50.0)


ROUTE_LABELS = ("Fastest road", "Alternate road")


@dataclass(frozen=True)
class PlanRequest:
    start: str
    finish: str
    start_fuel: float = 1.0          # 1 is a full tank, 0.5 is half a tank
    corridor_miles: float | None = None
    stop_penalty: float | None = None
    route_index: int = 0             # 0 fastest, 1 the alternate road; the user picks


def _price_route(route, index, corridor, start_fuel_miles, tank_range, mpg, stop_penalty,
                 reserve_miles, reserve_gallons, start_fuel, fill_up_within_miles=0.0):
    lats = [c[0] for c in route.coords]
    lons = [c[1] for c in route.coords]
    corridor_stations = station_index.stations_along(lats, lons, route.distance_miles, corridor)
    try:
        plan = plan_fuel_stops(
            [(s.route_mile, s.price) for s in corridor_stations],
            total_miles=route.distance_miles,
            tank_range_miles=tank_range,
            start_fuel_miles=start_fuel_miles,
            mpg=mpg,
            stop_penalty=stop_penalty,
            reserve_miles=reserve_miles,
            min_stop_spacing_miles=10,
        )
    except InfeasibleRouteError as exc:
        raise InfeasibleRouteError(
            _explain_infeasible(
                corridor_stations, route, lats, lons, start_fuel, corridor, tank_range, reserve_miles, mpg,
            )
        ) from exc
    # stop_penalty=0 asks for the pure cheapest plan, so it is left exactly as solved.
    if stop_penalty > 0:
        plan = top_up_near_full(plan, tank_range, fill_up_within_miles)

    stops = []
    for n, p in enumerate(plan.purchases, start=1):
        s = corridor_stations[p.station_index]
        gallons = p.miles_bought / mpg
        stops.append({
            "stop_number": n,
            "opis_id": s.row["opis_id"],
            "name": s.row["name"],
            "address": s.row["address"],
            "city": s.row["city"],
            "state": s.row["state"],
            "lat": round(s.route_lat, 6),
            "lon": round(s.route_lon, 6),
            "town_lat": round(s.row["lat"], 6),
            "town_lon": round(s.row["lon"], 6),
            "price_per_gallon": round(s.price, 3),
            "route_mile": round(s.route_mile, 1),
            "distance_off_route_miles": round(s.offset_miles, 1),
            "fuel_on_arrival_gallons": round(p.fuel_on_arrival_miles / mpg, 2),
            "gallons_purchased": round(gallons, 2),
            "cost": round(gallons * s.price, 2),
        })

    depart_gallons = start_fuel_miles / mpg
    tank_gallons = tank_range / mpg
    prev_mile = 0.0
    fuel_gal = depart_gallons
    for i, stop in enumerate(stops):
        nxt = stops[i + 1]["route_mile"] if i + 1 < len(stops) else route.distance_miles
        after = stop["fuel_on_arrival_gallons"] + stop["gallons_purchased"]
        stop["miles_since_last"] = round(stop["route_mile"] - prev_mile, 1)
        stop["gallons_after_purchase"] = round(after, 2)
        stop["range_miles_on_arrival"] = round(stop["fuel_on_arrival_gallons"] * mpg, 1)
        stop["range_miles_after_purchase"] = round(after * mpg, 1)
        stop["miles_until_next"] = round(nxt - stop["route_mile"], 1)
        stop["next_is_finish"] = i + 1 == len(stops)
        stop["filled_tank"] = after >= tank_gallons - 0.15
        prev_mile = stop["route_mile"]
        fuel_gal = after
    last = len(plan.purchases) - 1
    for i, stop in enumerate(stops):
        nxt = stops[i + 1] if i + 1 < len(stops) else None
        stop["reason"] = _stop_reason(stop, nxt, reserve_gallons, fill_up_within_miles / mpg)
        # What a full tank here would add: the next stops then buy less of their cheaper fuel.
        stop["fill_up_here_would_cost_more"] = None
        if i < last and not stop["filled_tank"]:
            extra = fill_tank_at(plan, tank_range, lambda k, _, i=i: k == i).cost(mpg) - plan.cost(mpg)
            stop["fill_up_here_would_cost_more"] = round(extra, 2)
            if extra >= 0.005:
                stop["reason"] += f" Filling the tank here would cost ${extra:.2f} more."
    gallons_at_finish = max(0.0, fuel_gal - (route.distance_miles - prev_mile) / mpg)
    if reserve_gallons - 0.1 <= gallons_at_finish < reserve_gallons:
        gallons_at_finish = reserve_gallons

    purchased_gallons = plan.gallons(mpg)
    purchased_cost = plan.cost(mpg)
    baseline = _baseline_cost(corridor_stations, route.distance_miles, start_fuel_miles, tank_range, reserve_miles, mpg)
    # The same stops, but filling the tank at each one except the last, which buys just enough to finish.
    fill_every_stop = fill_tank_at(plan, tank_range, lambda k, _: k < last).cost(mpg)
    corridor_prices = [s.price for s in corridor_stations]
    return {
        "index": index,
        "distance_miles": route.distance_miles,
        "duration_hours": route.duration_hours,
        "geometry": route.polyline6,
        "stops": stops,
        "cost": purchased_cost,
        "summary": {
            "total_fuel_cost": round(purchased_cost, 2),
            "gallons_purchased": round(purchased_gallons, 2),
            "total_gallons_used": round(route.distance_miles / mpg, 2),
            "gallons_at_departure": round(depart_gallons, 2),
            "range_miles_at_departure": round(start_fuel_miles, 1),
            "gallons_at_finish": round(gallons_at_finish, 2),
            "range_miles_at_finish": round(gallons_at_finish * mpg, 1),
            "number_of_stops": len(stops),
            "average_price_paid": round(purchased_cost / purchased_gallons, 3) if purchased_gallons else None,
            "stations_considered": len(corridor_stations),
            # What a driver who does not compare prices would pay on the same road; see _baseline_cost.
            "baseline_fuel_cost": round(baseline[0], 2) if baseline else None,
            "baseline_stops": baseline[1] if baseline else None,
            "savings": round(max(0.0, baseline[0] - purchased_cost), 2) if baseline else None,
            # savings = from_cheaper_stations + from_partial_fills
            "savings_breakdown": {
                "from_cheaper_stations": round(baseline[0] - fill_every_stop, 2),
                "from_partial_fills": round(fill_every_stop - purchased_cost, 2),
            } if baseline else None,
            "corridor_price_range": (
                [round(min(corridor_prices), 3), round(max(corridor_prices), 3)] if corridor_prices else None
            ),
        },
    }


def _baseline_cost(stations, total_miles, start_fuel_miles, tank_range, reserve_miles, mpg):
    """(cost, stops) for a driver who does not compare prices, or None if that driver cannot finish.

    The driver keeps going and fills the tank at the last station in range before the fuel would
    drop below the reserve. The last stop buys just enough to finish with the reserve, as the plan
    does. This makes the fewest stops possible, so the plan's saving comes from choosing pumps.
    """
    pos, fuel, cost, stops, i = 0.0, start_fuel_miles, 0.0, 0, 0
    while fuel - (total_miles - pos) < reserve_miles - 1e-9:
        # A start at or below the reserve may use it to reach the first pump, as in the optimizer.
        floor = 0.0 if stops == 0 and start_fuel_miles <= reserve_miles else reserve_miles
        reach = pos + fuel - floor
        last = None
        while i < len(stations) and stations[i].route_mile <= reach + 1e-9:
            if stations[i].route_mile > pos:
                last = stations[i]
            i += 1
        if last is None:
            return None
        fuel -= last.route_mile - pos
        pos = last.route_mile
        buy = max(0.0, min(tank_range, total_miles - pos + reserve_miles) - fuel)
        cost += buy / mpg * last.price
        fuel += buy
        stops += 1
    return cost, stops


def _stop_reason(stop, nxt, reserve_gallons, fill_up_within_gallons):
    """One line on why this stop bought what it did, for the map and the API."""
    miles = f"{stop['miles_until_next']:.0f} mi"
    if nxt is None:
        if stop["filled_tank"]:
            return f"Last stop: filled the tank for the {miles} to the finish."
        return f"Last stop: bought just enough to reach the finish, {miles} ahead, with {reserve_gallons:g} gal left."
    where = f"{nxt['city']}, {nxt['state']}"
    cheaper = nxt["price_per_gallon"] < stop["price_per_gallon"]
    if not stop["filled_tank"]:
        if cheaper:
            return (f"Cheaper fuel at {where} (${nxt['price_per_gallon']:.3f}) {miles} ahead: "
                    f"bought just enough to get there with the {reserve_gallons:g} gal reserve.")
        return f"Bought just enough to reach the next stop, {where}, {miles} ahead, with the reserve."
    if not cheaper:
        return f"Fuel at the next stop, {where}, costs more (${nxt['price_per_gallon']:.3f}): filled the tank."
    extra = nxt["fuel_on_arrival_gallons"] - reserve_gallons
    if extra <= 0.15:
        return (f"Cheaper fuel at {where} (${nxt['price_per_gallon']:.3f}) {miles} ahead, "
                f"and it takes a full tank to get there with the reserve.")
    saved = extra * (stop["price_per_gallon"] - nxt["price_per_gallon"])
    return (f"Within {fill_up_within_gallons:g} gal of full, so it filled the tank. Leaving that for the cheaper "
            f"fuel at {where} (${nxt['price_per_gallon']:.3f}) would save only ${saved:.2f}.")


def _route_options(routes, priced, errors):
    by_index = {p["index"]: p for p in priced}
    other_cost = {p["index"]: next((q["cost"] for q in priced if q["index"] != p["index"]), None) for p in priced}
    options = []
    for index in range(len(routes)):
        label = ROUTE_LABELS[index] if index < len(ROUTE_LABELS) else f"Road {index + 1}"
        match = by_index.get(index)
        if match is None:
            options.append({"index": index, "label": label, "feasible": False, "error": errors.get(index, "")})
            continue
        delta = None if other_cost[index] is None else round(match["cost"] - other_cost[index], 2)
        if delta is None:
            compared = ""
        elif abs(delta) < 0.01:
            compared = "Same fuel cost as the other road"
        elif delta < 0:
            compared = f"Saves ${-delta:.2f} compared with the other road"
        else:
            compared = f"Costs ${delta:.2f} more than the other road"
        options.append({
            "index": index,
            "label": label,
            "feasible": True,
            "distance_miles": round(match["distance_miles"], 1),
            "duration_hours": round(match["duration_hours"], 2),
            "total_fuel_cost": match["summary"]["total_fuel_cost"],
            "number_of_stops": match["summary"]["number_of_stops"],
            "compared": compared,
        })
    return options


def build_plan(req: PlanRequest) -> dict:
    t0 = time.perf_counter()
    cfg = settings.FUEL_ROUTE
    tank_range = cfg["VEHICLE_RANGE_MILES"]
    mpg = cfg["VEHICLE_MPG"]
    reserve_gallons = cfg["RESERVE_GALLONS"]
    reserve_miles = reserve_gallons * mpg
    corridor = req.corridor_miles if req.corridor_miles is not None else cfg["CORRIDOR_MILES"]
    stop_penalty = req.stop_penalty if req.stop_penalty is not None else cfg["STOP_PENALTY_DOLLARS"]
    fill_up_within_gallons = cfg.get("FILL_UP_WITHIN_GALLONS", 0.0)

    calls: list[str] = []
    start = geocoding.geocode(req.start, calls)
    finish = geocoding.geocode(req.finish, calls)

    routes, cached = osrm.get_routes(start.lat, start.lon, finish.lat, finish.lon)
    if not cached:
        calls.append("osrm")

    start_fuel_miles = _start_fuel_miles(req.start_fuel, tank_range)
    priced, errors = [], {}
    for index, route in enumerate(routes):
        try:
            priced.append(_price_route(
                route, index, corridor, start_fuel_miles, tank_range, mpg,
                stop_penalty, reserve_miles, reserve_gallons, req.start_fuel,
                fill_up_within_miles=fill_up_within_gallons * mpg,
            ))
        except InfeasibleRouteError as exc:
            errors[index] = str(exc)
    if not priced:
        raise InfeasibleRouteError(errors.get(0) or next(iter(errors.values())))

    chosen = req.route_index if any(p["index"] == req.route_index for p in priced) else priced[0]["index"]
    selected = next(p for p in priced if p["index"] == chosen)
    other = next((p for p in priced if p["index"] != chosen), None)
    options = _route_options(routes, priced, errors)

    return {
        "start": start.as_dict(),
        "finish": finish.as_dict(),
        "selected_route": chosen,
        "options": options,
        "route": {
            "index": chosen,
            "label": ROUTE_LABELS[chosen] if chosen < len(ROUTE_LABELS) else f"Road {chosen + 1}",
            "distance_miles": round(selected["distance_miles"], 1),
            "duration_hours": round(selected["duration_hours"], 2),
            "geometry": {"format": "polyline6", "encoded": selected["geometry"]},
        },
        "other_geometry": (
            {"format": "polyline6", "encoded": other["geometry"]} if other and other["geometry"] else None
        ),
        "fuel_stops": selected["stops"],
        "summary": selected["summary"],
        "assumptions": {
            "vehicle_range_miles": tank_range,
            "mpg": mpg,
            "tank_gallons": tank_range / mpg,
            "start_fuel_fraction": req.start_fuel,
            "corridor_miles": corridor,
            "stop_penalty_dollars": stop_penalty,
            "reserve_gallons": reserve_gallons,
            "fill_up_within_gallons": fill_up_within_gallons,
            "note": (
                "total_fuel_cost is what is paid at the listed stops. The vehicle departs with "
                f"{req.start_fuel:.0%} of a tank, so fuel already in the tank is not charged. "
                "start_fuel is a fraction of the tank: 1 is full and 0.5 is half. "
                "If that cannot reach the first station, the plan says what to enter. Stops are chosen to minimise "
                "fuel cost + stop_penalty_dollars per stop (stop_penalty=0 gives the pure cheapest plan). "
                f"After the first fill, the plan keeps at least {reserve_gallons:.0f} gallons at every stop and at the finish. "
                "The first pump can be reached with less when the tank started with only that much. "
                f"A stop that would leave the tank within {fill_up_within_gallons:g} gallon(s) of full fills it "
                "(not when stop_penalty is 0)."
            ),
        },
        "meta": {
            "external_api_calls": len(calls),
            "external_api_call_detail": calls,
            "route_cache_hit": cached,
            "elapsed_ms": round((time.perf_counter() - t0) * 1000, 1),
        },
    }


def _start_fuel_miles(start_fuel, tank_range):
    """start_fuel is a fraction of the tank: 1 is full, 0.5 is half."""
    return min(max(start_fuel, 0.0) * tank_range, tank_range)


def _minimum_start_fraction(needed_miles, tank_range):
    """Smallest 0.001 of a tank that the planner's whole-mile math can drive needed_miles."""
    if needed_miles <= 0:
        return 0.0
    steps = math.ceil(needed_miles / tank_range * 1000 - 1e-9)
    fraction = min(1.0, steps / 1000)
    while math.floor(_start_fuel_miles(fraction, tank_range)) < needed_miles and fraction < 1:
        steps += 1
        fraction = min(1.0, steps / 1000)
    return fraction


def _fraction_text(fraction):
    return f"{fraction:.3f}".rstrip("0").rstrip(".") or "0"


def _reach(fuel_u, cap_u, reserve_u, from_start):
    """Miles that can be driven and still arrive with the reserve in the tank.

    A start that has no more than the reserve may use all of it to reach the first pump.
    """
    if from_start and fuel_u <= reserve_u:
        return fuel_u
    return max(0, (fuel_u if from_start else cap_u) - reserve_u)


def _first_gap(stations, total_miles, start_fuel_miles, tank_range, reserve_miles=0.0):
    """First leg the vehicle cannot drive: (from_station | None, from_mile, to_station | None, to_mile), or None.

    Uses the optimizer's whole-mile arithmetic (rounded positions, floored start fuel) so the two agree.
    """
    start_u, cap_u = math.floor(start_fuel_miles), round(tank_range)
    reserve_u = round(reserve_miles)
    prev, prev_u = None, 0
    for s in stations:
        pos_u = round(s.route_mile)
        reach = _reach(start_u, cap_u, reserve_u, prev is None)
        if pos_u - prev_u > reach:
            return prev, (prev.route_mile if prev else 0.0), s, s.route_mile
        prev, prev_u = s, pos_u
    reach = _reach(start_u, cap_u, reserve_u, prev is None)
    if math.ceil(total_miles - 1e-9) - prev_u > reach:
        return prev, (prev.route_mile if prev else 0.0), None, total_miles
    return None


def _label(s):
    return f"{s.row['name']} ({s.row['city']}, {s.row['state']})"


def _range_clause(tank_range, reserve_miles, mpg):
    if reserve_miles <= 0:
        return f"the vehicle's {tank_range:.0f}-mile range"
    return (f"the {tank_range - reserve_miles:.0f} miles a full tank can cover while keeping "
            f"{reserve_miles / mpg:.0f} gallons in reserve")


def _explain_infeasible(stations, route, lats, lons, start_fuel, corridor, tank_range, reserve_miles=0.0, mpg=10.0):
    """Human-readable reason a route has no feasible plan, plus what (if anything) would fix it."""
    total = route.distance_miles
    start_miles = _start_fuel_miles(start_fuel, tank_range)
    gap = _first_gap(stations, total, start_miles, tank_range, reserve_miles)
    rng = _range_clause(tank_range, reserve_miles, mpg)

    if gap is None:  # discretisation edge case; should not happen
        return f"No refuelling plan fits {rng} on this route."
    prev, from_mile, nxt, to_mile = gap
    if prev is None and nxt is None:
        reason = (f"The price list has no fuel stations within {corridor:g} miles of this "
                  f"{total:.0f}-mile route, which is longer than {rng}.")
    elif prev is None and to_mile > tank_range:
        reason = (f"The first fuel station in the price list along this route is {_label(nxt)} at mile "
                  f"{to_mile:.0f}, beyond {rng} even on a full tank.")
    elif prev is None:
        start_u = math.floor(start_miles)
        reserve_u = round(reserve_miles)
        reach = _reach(start_u, round(tank_range), reserve_u, True)
        to_u = round(to_mile)
        # At or below the reserve, the first pump can be reached on the miles in the tank.
        # Above it, the arrival must still have the reserve, so the tank needs the gap plus that reserve.
        needed_miles = to_u if to_u <= reserve_u else to_u + reserve_u
        needed = _minimum_start_fraction(needed_miles, tank_range)
        have_miles = math.floor(start_miles)
        if reserve_u and start_u > reserve_u:
            reason = (f"{_label(nxt)} is {to_u} miles ahead. "
                      f"You entered {_fraction_text(start_fuel)}, so you can drive {reach} miles "
                      f"and still keep {reserve_miles / mpg:.0f} gallons. "
                      f"Enter at least {_fraction_text(needed)}.")
        else:
            reason = (f"{_label(nxt)} is {to_u} miles ahead. "
                      f"You entered {_fraction_text(start_fuel)}, which covers {have_miles} miles. "
                      f"Enter at least {_fraction_text(needed)}.")
    elif nxt is None:
        reason = (f"The last fuel station along this route is {_label(prev)} at mile {from_mile:.0f}, "
                  f"{total - from_mile:.0f} miles from the destination, which is more than {rng}.")
    else:
        reason = (f"There is no fuel station in the price list between {_label(prev)} at mile {from_mile:.0f} "
                  f"and {_label(nxt)} at mile {to_mile:.0f}. That {to_mile - from_mile:.0f}-mile gap is longer "
                  f"than {rng}.")

    # Not enough fuel to reach the first station, and a fuller tank can finish this same road.
    # Say how many gallons to add. A wider corridor is a different fix and hides that number.
    if prev is None and nxt is not None and to_mile <= tank_range:
        if _first_gap(stations, total, tank_range, tank_range, reserve_miles) is None:
            return reason

    for wider in (w for w in WIDER_CORRIDORS_MILES if w > corridor):
        wide = station_index.stations_along(lats, lons, total, wider)
        if _first_gap(wide, total, start_miles, tank_range, reserve_miles) is None:
            return f"{reason} Widening corridor_miles to {wider:g} would make the trip possible."
    if start_fuel < 1 and _first_gap(stations, total, tank_range, tank_range, reserve_miles) is None:
        return reason  # the start_fuel advice above already covers it
    widest = max(WIDER_CORRIDORS_MILES + (corridor,))
    return (f"{reason} Widening the corridor to {widest:g} miles does not fix this, so the trip cannot be "
            f"planned with the stations in the price list.")
