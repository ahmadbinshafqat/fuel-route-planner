"""Cheapest refuelling plan along a fixed route.

Minimises   fuel spend + stop_penalty * number_of_stops

With stop_penalty = 0 this is the textbook "gas station problem", whose optimum
tends to include silly 1-gallon top-ups to save a few cents. A small penalty
(default a few dollars, standing in for the time a stop costs) removes those
without materially changing the fuel bill.

Method: dynamic programming over (station, fuel-in-tank) with fuel discretised
to 1 mile of range (0.1 gal at 10 mpg). Stations are visited in route order and
every station transitions only to the next one, so each step is a handful of
vectorised numpy operations on a (range+1)-length array:

    V[f]          min cost to arrive here with f miles of fuel
    buy to g>f    V[f] + (g - f) * cost_per_mile + stop_penalty
                  = stop_penalty + g * c + min_{f<g}(V[f] - f * c)   <- prefix minimum

That is O(stations * range), i.e. a few milliseconds for a coast-to-coast trip.
"""

from dataclasses import dataclass, field

import numpy as np

UNIT_MILES = 1.0


class InfeasibleRouteError(ValueError):
    pass


@dataclass
class Purchase:
    station_index: int
    route_mile: float
    price: float
    miles_bought: float
    fuel_on_arrival_miles: float


@dataclass
class Plan:
    purchases: list[Purchase] = field(default_factory=list)

    def gallons(self, mpg):
        return sum(p.miles_bought for p in self.purchases) / mpg

    def cost(self, mpg):
        return sum(p.miles_bought / mpg * p.price for p in self.purchases)


def plan_fuel_stops(stations, total_miles, tank_range_miles, start_fuel_miles, mpg, stop_penalty=0.0, reserve_miles=0.0, min_stop_spacing_miles=0.0):
    """stations: sequence of (route_mile, price_per_gallon) sorted by route_mile.

    min_stop_spacing_miles drops a later stop that sits too close to the one before it and
    solves again, so the truck does not buy a few gallons and then stop a mile later.

    reserve_miles is fuel that must still be in the tank on arrival at every stop and at the
    finish. A truck that starts with no more than that reserve may pass stations without
    buying until its first purchase. That purchase refills to the reserve, and every later
    arrival keeps it.

    Returns a Plan whose purchases reference indexes into `stations`.
    Raises InfeasibleRouteError if the route cannot be completed.
    """
    if min_stop_spacing_miles > 0:
        return _plan_with_stop_spacing(
            stations, total_miles, tank_range_miles, start_fuel_miles, mpg,
            stop_penalty, reserve_miles, min_stop_spacing_miles,
        )
    if start_fuel_miles > tank_range_miles + 1e-9:
        raise ValueError("start fuel exceeds tank range")

    cap = int(round(tank_range_miles / UNIT_MILES))
    reserve_u = int(round(reserve_miles / UNIT_MILES))
    if not 0 <= reserve_u < cap:
        raise ValueError("reserve must be less than the tank range")
    levels = np.arange(cap + 1)
    V = np.full(cap + 1, np.inf)
    start_u = int(np.floor(start_fuel_miles / UNIT_MILES))
    V[start_u] = 0.0

    positions = [int(round(m / UNIT_MILES)) for m, _ in stations]
    total_u = int(np.ceil(total_miles / UNIT_MILES - 1e-9))
    # Only stations strictly before the destination are useful.
    usable = [i for i, p in enumerate(positions) if p < total_u]

    came_from = []  # per usable station: after-buy level g -> arrival level f
    prev_pos = 0
    # A short starting tank is tracked separately from fuel bought along the way. It may pass a
    # station without stopping. A purchase refills to the reserve, and that bought fuel cannot
    # later be driven below the reserve. Forcing the refill at the first station on the road
    # buys a few gallons and then stops again a mile later.
    if start_u <= reserve_u:
        start_V, kept_V = V, np.full(cap + 1, np.inf)
    else:
        start_V, kept_V = None, V
    for i in usable:
        leg = positions[i] - prev_pos
        if start_V is not None:
            start_V = _drive(start_V, leg, 0)
            kept_V = _drive(kept_V, leg, reserve_u)
            V = np.minimum(start_V, kept_V)
        else:
            V = kept_V = _drive(kept_V, leg, reserve_u)
        if not np.isfinite(V).any():
            raise _gap_error(prev_pos, positions[i], tank_range_miles, reserve_miles)
        c = stations[i][1] / mpg * UNIT_MILES  # $ per unit of range
        base = V - levels * c
        run_min = np.minimum.accumulate(base)
        run_arg = np.maximum.accumulate(np.where(base == run_min, levels, 0))
        # Best arrival level strictly below g.
        best_below = np.concatenate(([np.inf], run_min[:-1]))
        best_below_arg = np.concatenate(([0], run_arg[:-1]))
        bought = best_below + levels * c + stop_penalty
        if start_V is not None:
            bought[:reserve_u] = np.inf
        take = bought < V - 1e-12
        came_from.append(np.where(take, best_below_arg, levels).astype(np.int32))
        kept_V = np.where(take, bought, kept_V)
        prev_pos = positions[i]

    if start_V is not None:
        V = np.minimum(
            _drive(start_V, total_u - prev_pos, reserve_u),
            _drive(kept_V, total_u - prev_pos, reserve_u),
        )
    else:
        V = _drive(kept_V, total_u - prev_pos, reserve_u)
    if not np.isfinite(V).any():
        raise _gap_error(prev_pos, total_u, tank_range_miles, reserve_miles)

    # Backtrack from the cheapest arrival state.
    plan = Plan()
    level = int(np.argmin(V))
    next_pos = total_u
    for k in range(len(usable) - 1, -1, -1):
        i = usable[k]
        after_buy = level + (next_pos - positions[i])
        arrival = int(came_from[k][after_buy])
        if arrival != after_buy:
            plan.purchases.append(Purchase(
                station_index=i,
                route_mile=stations[i][0],
                price=stations[i][1],
                miles_bought=(after_buy - arrival) * UNIT_MILES,
                fuel_on_arrival_miles=arrival * UNIT_MILES,
            ))
        level, next_pos = arrival, positions[i]
    plan.purchases.reverse()
    return plan


def _plan_with_stop_spacing(stations, total_miles, tank_range_miles, start_fuel_miles, mpg, stop_penalty, reserve_miles, spacing):
    active = list(range(len(stations)))
    best = None
    while active:
        subset = [stations[i] for i in active]
        try:
            plan = plan_fuel_stops(
                subset, total_miles, tank_range_miles, start_fuel_miles, mpg,
                stop_penalty, reserve_miles, min_stop_spacing_miles=0,
            )
        except InfeasibleRouteError:
            if best is None:
                raise
            return best
        for purchase in plan.purchases:
            purchase.station_index = active[purchase.station_index]
        best = plan
        later = next(
            (b.station_index for a, b in zip(plan.purchases, plan.purchases[1:])
             if b.route_mile - a.route_mile < spacing),
            None,
        )
        if later is None:
            return plan
        active.remove(later)
    return best


def top_up_near_full(plan, tank_range_miles, within_miles):
    """Fill the tank at any stop that the plan leaves within within_miles of full.

    The optimum stops short of full when the next pump is cheaper, e.g. at 49.6 of 50 gallons.
    Later stops then arrive with the extra fuel and buy that much less, so they leave the tank
    where the plan did; a stop left with nothing to buy is dropped. No arrival has less fuel
    than in the original plan, so the reserve and the tank capacity still hold.
    """
    if within_miles <= 0:
        return plan
    return fill_tank_at(plan, tank_range_miles, lambda k, planned: tank_range_miles - planned <= within_miles + 1e-9)


def fill_tank_at(plan, tank_range_miles, should_fill):
    """Fill the tank at every stop k where should_fill(k, planned_level) is true.

    Later stops arrive with the extra fuel and buy that much less, so they leave the tank where
    the plan did; a stop left with nothing to buy is dropped.
    """
    out = Plan()
    extra = 0.0  # fuel above the original plan, carried into the next arrival
    for k, p in enumerate(plan.purchases):
        arrival = p.fuel_on_arrival_miles + extra
        planned = p.fuel_on_arrival_miles + p.miles_bought
        target = tank_range_miles if should_fill(k, planned) else planned
        if target > arrival + 1e-9:
            out.purchases.append(Purchase(
                station_index=p.station_index,
                route_mile=p.route_mile,
                price=p.price,
                miles_bought=target - arrival,
                fuel_on_arrival_miles=arrival,
            ))
        extra = max(target, arrival) - planned
    return out


def _drive(V, leg_units, min_fuel=0):
    """Fuel decreases by the leg length. Arrivals below min_fuel are unreachable."""
    if leg_units <= 0:
        out = V.copy()
    else:
        out = np.full_like(V, np.inf)
        if leg_units < len(V):
            out[: len(V) - leg_units] = V[leg_units:]
    if min_fuel > 0:
        out[:int(min_fuel)] = np.inf
    return out


def _gap_error(from_u, to_u, tank_range_miles, reserve_miles=0.0):
    where = "the start" if from_u == 0 else f"mile {from_u * UNIT_MILES:.0f}"
    if reserve_miles > 0:
        limit = f"{tank_range_miles - reserve_miles:.0f} miles it can drive while keeping {reserve_miles:.0f} miles in reserve"
    else:
        limit = f"{tank_range_miles:.0f}-mile range"
    return InfeasibleRouteError(
        f"Cannot reach mile {to_u * UNIT_MILES:.0f} from {where}: the gap between fuel stations on this route "
        f"exceeds the vehicle's {limit} (or no station is close enough to the start)."
    )
