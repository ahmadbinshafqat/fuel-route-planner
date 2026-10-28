import itertools
import json
import random
from decimal import Decimal
from unittest import mock

from django.core.cache import cache
from django.test import SimpleTestCase, TestCase

from routing.models import FuelStation, Place
from routing.services import geocoding, station_index
from routing.services.optimizer import (
    InfeasibleRouteError, Plan, Purchase, fill_tank_at, plan_fuel_stops, top_up_near_full,
)
from routing.services.osrm import Route, decode_polyline
from routing.services.planner import _explain_infeasible, _first_gap, _start_fuel_miles, _stop_reason
from routing.services.station_index import CorridorStation

MPG = 10.0


def greedy_reference_cost(stations, total, cap, start):
    """Textbook optimal greedy for the zero-stop-cost gas station problem (used only to check the DP)."""
    pos, fuel, price, i, cost = 0.0, start, float("inf"), -1, 0.0
    if total <= start:
        return 0.0
    while True:
        reach = pos + (fuel if i < 0 else cap)
        limit = min(reach, total)
        nxt = next((j for j in range(i + 1, len(stations)) if stations[j][0] <= limit and stations[j][1] < price), None)
        if nxt is None and total <= reach:
            if i >= 0 and total - pos > fuel:
                cost += (total - pos - fuel) / MPG * price
            return cost
        if nxt is not None:
            need = stations[nxt][0] - pos
            if i >= 0 and need > fuel:
                cost += (need - fuel) / MPG * price
                fuel = need
        else:
            in_range = [j for j in range(i + 1, len(stations)) if stations[j][0] <= reach]
            if not in_range:
                raise InfeasibleRouteError
            nxt = min(in_range, key=lambda j: (stations[j][1], -stations[j][0]))
            need = stations[nxt][0] - pos
            cost += (cap - fuel) / MPG * price
            fuel = cap
        fuel -= need
        i = nxt
        pos, price = stations[i]


class OptimizerTests(SimpleTestCase):
    def test_no_stop_when_start_fuel_covers_trip(self):
        plan = plan_fuel_stops([(100, 3.0)], total_miles=450, tank_range_miles=500, start_fuel_miles=500, mpg=MPG)
        self.assertEqual(plan.purchases, [])

    def test_reserve_keeps_five_gallons_at_every_arrival(self):
        plan = plan_fuel_stops([(400, 3.0)], total_miles=700, tank_range_miles=500, start_fuel_miles=500, mpg=MPG, reserve_miles=50)
        self.assertEqual(len(plan.purchases), 1)
        p = plan.purchases[0]
        self.assertAlmostEqual(p.fuel_on_arrival_miles, 100)
        self.assertAlmostEqual(p.miles_bought, 250)  # 300 miles left, plus 50 miles of reserve, 100 already in the tank
        fuel, pos = 500.0, 0.0
        for stop in plan.purchases:
            fuel -= stop.route_mile - pos
            self.assertGreaterEqual(fuel, 50 - 1e-6)
            fuel += stop.miles_bought
            pos = stop.route_mile
        self.assertGreaterEqual(fuel - (700 - pos), 50 - 1e-6)

    def test_buys_only_what_is_needed_to_reach_destination(self):
        plan = plan_fuel_stops([(400, 3.0)], total_miles=700, tank_range_miles=500, start_fuel_miles=500, mpg=MPG)
        self.assertEqual(len(plan.purchases), 1)
        p = plan.purchases[0]
        self.assertAlmostEqual(p.fuel_on_arrival_miles, 100)
        self.assertAlmostEqual(p.miles_bought, 200)  # 300 miles left, 100 in tank
        self.assertAlmostEqual(plan.cost(MPG), 60.0)

    def test_prefers_cheap_station_and_fills_up_there(self):
        stations = [(100, 2.50), (300, 4.00), (450, 4.00), (700, 3.00)]
        plan = plan_fuel_stops(stations, total_miles=1000, tank_range_miles=500, start_fuel_miles=500, mpg=MPG)
        bought = {stations[p.station_index][0]: p.miles_bought for p in plan.purchases}
        self.assertEqual(bought[100], 100)  # tops up to full at the cheap station
        # 700 is out of reach from 100, so exactly 100 miles of $4 fuel is bought at 300 or 450.
        self.assertEqual(bought.get(300, 0) + bought.get(450, 0), 100)
        self.assertEqual(bought[700], 300)
        self.assertAlmostEqual(plan.cost(MPG), 25 + 40 + 90)

    def test_tank_never_exceeds_capacity(self):
        stations = [(i * 60.0, 2.5 + (i % 3) * 0.4) for i in range(1, 40)]
        plan = plan_fuel_stops(stations, 2400, 500, 500, MPG, stop_penalty=0)
        for p in plan.purchases:
            self.assertLessEqual(p.fuel_on_arrival_miles + p.miles_bought, 500 + 1e-6)
            self.assertGreaterEqual(p.fuel_on_arrival_miles, -1e-6)

    def test_gap_longer_than_range_is_infeasible(self):
        with self.assertRaises(InfeasibleRouteError):
            plan_fuel_stops([(300, 3.0), (900, 3.0)], 1000, 500, 500, MPG)

    def test_start_exactly_at_reserve_can_reach_the_first_pump(self):
        plan = plan_fuel_stops([(30, 3.0)], total_miles=100, tank_range_miles=500, start_fuel_miles=50, mpg=MPG, reserve_miles=50)
        self.assertEqual(len(plan.purchases), 1)
        p = plan.purchases[0]
        self.assertAlmostEqual(p.fuel_on_arrival_miles, 20)  # spent the reserve to cover the 30 miles
        self.assertGreaterEqual(p.fuel_on_arrival_miles + p.miles_bought, 120)  # 70 miles left, plus the reserve
        self.assertGreaterEqual(50 + p.miles_bought - 100, 50)

    def test_empty_start_names_the_gallons_needed_to_reach_the_first_station(self):
        station = CorridorStation(
            row={"name": "DELTA", "city": "Jersey City", "state": "NJ"},
            price=3.2, route_mile=10, offset_miles=1, route_lat=0.0, route_lon=0.0,
        )
        route = Route(distance_miles=100, duration_hours=2, polyline6="", coords=[])
        msg = _explain_infeasible([station], route, [], [], 0, 50, 500, 50, MPG)
        self.assertIn("DELTA (Jersey City, NJ) is 10 miles ahead", msg)
        self.assertIn("You entered 0, which covers 0 miles.", msg)
        self.assertIn("Enter at least 0.02.", msg)
        self.assertNotIn("gallon", msg)
        self.assertNotIn("Widening", msg)

    def test_nine_mile_station_asks_for_0_018_not_a_full_gallon(self):
        station = CorridorStation(
            row={"name": "DELTA", "city": "Jersey City", "state": "NJ"},
            price=3.2, route_mile=9, offset_miles=1, route_lat=0.0, route_lon=0.0,
        )
        route = Route(distance_miles=20, duration_hours=1, polyline6="", coords=[])
        msg = _explain_infeasible([station], route, [], [], 0.01, 50, 500, 50, MPG)
        self.assertIn("You entered 0.01, which covers 5 miles.", msg)
        self.assertIn("Enter at least 0.018.", msg)
        self.assertNotIn("gallon", msg)
        self.assertIsNone(_first_gap([station], 20, _start_fuel_miles(0.018, 500), 500, 50))
        self.assertIsNotNone(_first_gap([station], 20, _start_fuel_miles(0.017, 500), 500, 50))

    def test_shortfall_message_asks_for_more_fuel_than_the_tank_already_has(self):
        station = CorridorStation(
            row={"name": "PETES PLACE SOUTH", "city": "Bemidji", "state": "MN"},
            price=3.0, route_mile=80, offset_miles=1, route_lat=0.0, route_lon=0.0,
        )
        route = Route(distance_miles=500, duration_hours=8, polyline6="", coords=[])
        # 0.12 of a tank is 60 miles, above the 50-mile reserve, so only 10 miles are usable.
        msg = _explain_infeasible([station], route, [], [], 0.12, 50, 500, 50, MPG)
        self.assertIn("PETES PLACE SOUTH (Bemidji, MN) is 80 miles ahead", msg)
        self.assertIn("You entered 0.12", msg)
        self.assertIn("drive 10 miles", msg)
        self.assertIn("Enter at least 0.26.", msg)
        self.assertIn("keep 5 gallons", msg)

    def test_low_start_skips_a_top_up_a_mile_before_the_real_fill(self):
        stations = [(9, 3.239), (10, 3.099), (400, 3.50)]
        plan = plan_fuel_stops(stations, 500, 500, 30, MPG, stop_penalty=5, reserve_miles=50)
        self.assertEqual(plan.purchases[0].station_index, 1)
        self.assertNotIn(0, [p.station_index for p in plan.purchases])
        first = plan.purchases[0]
        self.assertAlmostEqual(first.fuel_on_arrival_miles, 20)
        self.assertGreaterEqual(first.fuel_on_arrival_miles + first.miles_bought, 50)
        fuel, pos = 30.0, 0.0
        for stop in plan.purchases:
            fuel -= stop.route_mile - pos
            if stop is not first:
                self.assertGreaterEqual(fuel, 50 - 1e-6)
            fuel += stop.miles_bought
            self.assertGreaterEqual(fuel, 50 - 1e-6)
            pos = stop.route_mile
        self.assertGreaterEqual(fuel - (500 - pos), 50 - 1e-6)

    def test_after_the_first_fill_a_hop_cannot_spend_the_reserve(self):
        with self.assertRaises(InfeasibleRouteError):
            plan_fuel_stops([(10, 3.0), (470, 2.0)], 600, 500, 30, MPG, reserve_miles=50)

    def test_just_enough_fuel_does_not_top_up_and_stop_again_a_mile_later(self):
        stations = [(9, 3.239), (10.2, 3.099), (400, 3.50)]
        plan = plan_fuel_stops(
            stations, 500, 500, 9, MPG, stop_penalty=5, reserve_miles=50, min_stop_spacing_miles=10,
        )
        self.assertEqual(plan.purchases[0].station_index, 0)
        self.assertNotIn(1, [p.station_index for p in plan.purchases])
        self.assertGreater(plan.purchases[0].miles_bought, 20)
        for a, b in zip(plan.purchases, plan.purchases[1:]):
            self.assertGreaterEqual(b.route_mile - a.route_mile, 10)

    def test_low_start_refills_when_the_next_pump_is_out_of_reach(self):
        plan = plan_fuel_stops([(9, 3.2), (200, 2.5)], 400, 500, 30, MPG, stop_penalty=5, reserve_miles=50)
        self.assertEqual(plan.purchases[0].station_index, 0)
        bought = plan.purchases[0]
        self.assertGreaterEqual(bought.fuel_on_arrival_miles + bought.miles_bought, 50)

    def test_full_tank_still_keeps_the_reserve_on_a_long_first_hop(self):
        with self.assertRaises(InfeasibleRouteError):
            plan_fuel_stops([(460, 3.0)], 700, 500, 500, MPG, reserve_miles=50)

    def test_empty_tank_without_nearby_station_is_infeasible(self):
        with self.assertRaises(InfeasibleRouteError):
            plan_fuel_stops([(50, 3.0)], 300, 500, 0, MPG)

    def test_matches_greedy_optimum_on_random_routes(self):
        rng = random.Random(42)
        for _ in range(200):
            total = rng.randint(300, 3000)
            stations = sorted((float(rng.randint(1, total - 1)), round(rng.uniform(2.7, 4.5), 3)) for _ in range(rng.randint(5, 60)))
            start = float(rng.choice([500, 250, 120]))
            try:
                expected = greedy_reference_cost(stations, total, 500.0, start)
            except InfeasibleRouteError:
                with self.assertRaises(InfeasibleRouteError):
                    plan_fuel_stops(stations, total, 500, start, MPG)
                continue
            plan = plan_fuel_stops(stations, total, 500, start, MPG, stop_penalty=0)
            self.assertAlmostEqual(plan.cost(MPG), expected, places=6)

    def test_stop_penalty_matches_brute_force_on_small_routes(self):
        """With a stop penalty, compare to exhaustive search over stop subsets (each stop fills optimally)."""
        rng = random.Random(7)
        for _ in range(60):
            total = rng.randint(600, 1400)
            stations = sorted((float(rng.randint(1, total - 1)), round(rng.uniform(2.7, 4.0), 2)) for _ in range(rng.randint(3, 8)))
            penalty = rng.choice([1.0, 5.0, 20.0])
            best = float("inf")
            for r in range(len(stations) + 1):
                for subset in itertools.combinations(range(len(stations)), r):
                    try:
                        cost = plan_fuel_stops([stations[j] for j in subset], total, 500, 500, MPG, stop_penalty=0).cost(MPG)
                    except InfeasibleRouteError:
                        continue
                    best = min(best, cost + penalty * r)
            try:
                plan = plan_fuel_stops(stations, total, 500, 500, MPG, stop_penalty=penalty)
            except InfeasibleRouteError:
                self.assertEqual(best, float("inf"))
                continue
            self.assertAlmostEqual(plan.cost(MPG) + penalty * len(plan.purchases), best, places=6)

    def test_penalty_removes_tiny_top_ups(self):
        # Arriving at mile 400 empty: the pure-cost plan buys 0.5 gal to reach a station 5 miles on
        # that is 1 cent cheaper; with a stop penalty it just fills up once.
        stations = [(400.0, 3.00), (405.0, 2.99)]
        cheap = plan_fuel_stops(stations, 900, 500, 400, MPG, stop_penalty=0)
        sensible = plan_fuel_stops(stations, 900, 500, 400, MPG, stop_penalty=5)
        self.assertEqual(len(cheap.purchases), 2)
        self.assertEqual(len(sensible.purchases), 1)
        self.assertLess(sensible.cost(MPG) - cheap.cost(MPG), 5)


class TopUpTests(SimpleTestCase):
    def test_nearly_full_stop_fills_and_the_next_stop_buys_less(self):
        plan = Plan([Purchase(0, 100, 3.1, 446, 50), Purchase(1, 546, 2.9, 300, 50)])
        out = top_up_near_full(plan, 500, 10).purchases
        self.assertEqual([p.miles_bought for p in out], [450, 296])
        self.assertEqual(out[1].fuel_on_arrival_miles, 54)
        self.assertEqual(out[1].fuel_on_arrival_miles + out[1].miles_bought, 350)  # same level as planned

    def test_stop_further_from_full_is_unchanged(self):
        plan = Plan([Purchase(0, 100, 3.2, 387, 50), Purchase(1, 487, 3.0, 450, 50)])
        out = top_up_near_full(plan, 500, 10).purchases
        self.assertEqual([p.miles_bought for p in out], [387, 450])

    def test_stop_with_nothing_left_to_buy_is_dropped(self):
        plan = Plan([Purchase(0, 100, 3.1, 445, 50), Purchase(1, 545, 2.9, 3, 50)])
        out = top_up_near_full(plan, 500, 10).purchases
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0].miles_bought, 450)

    def test_rounded_plan_keeps_reserve_on_nyc_style_route(self):
        stations = [(9, 3.239), (396, 3.059), (818, 3.149), (1264, 2.921), (1593, 3.074), (2035, 3.399), (2481, 3.332)]
        plan = plan_fuel_stops(stations, 2810, 500, 9, MPG, stop_penalty=25, reserve_miles=50)
        out = top_up_near_full(plan, 500, 10)
        fuel, pos = 9.0, 0.0
        for p in out.purchases:
            fuel -= p.route_mile - pos
            self.assertAlmostEqual(fuel, p.fuel_on_arrival_miles)
            fuel += p.miles_bought
            self.assertLessEqual(fuel, 500 + 1e-9)
            pos = p.route_mile
        self.assertGreaterEqual(fuel - (2810 - pos), 50 - 1e-6)
        self.assertTrue(all(500 - (p.fuel_on_arrival_miles + p.miles_bought) == 0
                            or 500 - (p.fuel_on_arrival_miles + p.miles_bought) > 10 for p in out.purchases))


class FillTankAtTests(SimpleTestCase):
    def test_filling_before_a_cheaper_stop_costs_the_price_gap(self):
        # 3.10 then 2.90: a full tank at the first stop moves 104 miles (10.4 gal) of buying there.
        plan = Plan([Purchase(0, 100, 3.10, 346, 50), Purchase(1, 446, 2.90, 300, 50)])
        filled = fill_tank_at(plan, 500, lambda k, _: k == 0)
        self.assertEqual([p.miles_bought for p in filled.purchases], [450, 196])
        self.assertAlmostEqual(filled.cost(MPG) - plan.cost(MPG), 10.4 * 0.20)


class StopReasonTests(SimpleTestCase):
    def stop(self, price, filled, arrival=5.0, city="A"):
        return {"price_per_gallon": price, "filled_tank": filled, "fuel_on_arrival_gallons": arrival,
                "miles_until_next": 357, "city": city, "state": "IA"}

    def test_partial_buy_names_the_cheaper_station_ahead(self):
        msg = _stop_reason(self.stop(2.982, False), self.stop(2.799, True, city="Waco"), 5, 5)
        self.assertIn("Cheaper fuel at Waco, IA ($2.799) 357 mi ahead", msg)

    def test_fill_up_when_the_next_stop_costs_more(self):
        msg = _stop_reason(self.stop(3.079, True), self.stop(3.359, True, city="Salt Lake City"), 5, 5)
        self.assertIn("costs more ($3.359): filled the tank", msg)

    def test_rounded_up_fill_says_what_waiting_would_save(self):
        msg = _stop_reason(self.stop(3.009, True), self.stop(2.982, False, arrival=7.8, city="Tipton"), 5, 5)
        self.assertIn("Within 5 gal of full", msg)
        self.assertIn("save only $0.08", msg)

    def test_full_tank_needed_to_reach_the_cheaper_station(self):
        msg = _stop_reason(self.stop(3.1, True), self.stop(2.9, False, arrival=5.0, city="B"), 5, 5)
        self.assertIn("takes a full tank to get there", msg)


class GeocodingTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        Place.objects.create(state="MO", key=geocoding.normalize_name("Saint Louis"), name="St. Louis", lat=38.63, lon=-90.24)
        Place.objects.create(state="UT", key="saltlakecity", name="Salt Lake City", lat=40.78, lon=-111.93)

    def test_normalize_name(self):
        self.assertEqual(geocoding.normalize_name("St. Louis city", gazetteer=True), "saintlouis")
        self.assertEqual(geocoding.normalize_name("Kansas City city", gazetteer=True), "kansascity")
        self.assertEqual(geocoding.normalize_name("Kansas City"), "kansascity")
        self.assertEqual(geocoding.normalize_name("Mc Calla "), "mccalla")

    def test_city_state_forms_resolve_offline(self):
        for q in ("St. Louis, MO", "saint louis mo", "St Louis, Missouri", "Salt Lake City Utah", "Salt Lake City, UT, USA"):
            with mock.patch("routing.services.geocoding._nominatim") as nom:
                loc = geocoding.geocode(q)
                nom.assert_not_called()
            self.assertEqual(loc.source, "gazetteer", q)

    def test_coordinates(self):
        loc = geocoding.geocode("39.74, -104.99")
        self.assertEqual((loc.lat, loc.lon, loc.source), (39.74, -104.99, "coordinates"))

    def test_outside_usa_rejected(self):
        with self.assertRaises(geocoding.GeocodingError):
            geocoding.geocode("51.5, -0.12")

    def test_free_text_falls_back_to_nominatim_once(self):
        cache.clear()
        fake = geocoding.Location(41.88, -87.63, "Chicago", "nominatim")
        with mock.patch("routing.services.geocoding._nominatim", return_value=fake) as nom:
            calls = []
            geocoding.geocode("233 S Wacker Dr Chicago", calls)
            geocoding.geocode("233 S Wacker Dr Chicago", calls)
        self.assertEqual(nom.call_count, 1)
        self.assertEqual(calls, ["nominatim"])


class PolylineTests(SimpleTestCase):
    def test_decode_known_polyline(self):
        # Google's documented example, precision 5.
        self.assertEqual(
            decode_polyline("_p~iF~ps|U_ulLnnqC_mqNvxq`@", precision=5),
            [(38.5, -120.2), (40.7, -120.95), (43.252, -126.453)],
        )


class StationIndexTests(TestCase):
    def setUp(self):
        station_index.invalidate()

    def tearDown(self):
        station_index.invalidate()

    def test_finds_stations_near_long_straight_segment(self):
        # A single 300-mile straight segment along latitude 40; station sits beside its middle.
        FuelStation.objects.create(opis_id=1, name="Mid", address="", city="X", state="NE", price=Decimal("3.0"), lat=40.05, lon=-100.0, geocode_source="t")
        FuelStation.objects.create(opis_id=2, name="Far", address="", city="Y", state="NE", price=Decimal("3.0"), lat=41.0, lon=-100.0, geocode_source="t")
        found = station_index.stations_along([40.0, 40.0], [-102.8, -97.2], 295.0, corridor_miles=10)
        self.assertEqual([s.row["opis_id"] for s in found], [1])
        self.assertAlmostEqual(found[0].route_mile, 147.5, delta=3)
        self.assertAlmostEqual(found[0].offset_miles, 3.5, delta=0.5)


def _fake_routes(*_args, **_kwargs):
    # Straight line Denver -> Kansas City-ish along lat 39, ~560 miles, encoded on the fly.
    coords = [(39.0, -105.0 + k * 0.1) for k in range(111)]
    return [Route(distance_miles=600.0, duration_hours=9.0, polyline6="", coords=coords)], False


def _two_routes(*_args, **_kwargs):
    coords = [(39.0, -105.0 + k * 0.1) for k in range(111)]
    return [
        Route(distance_miles=600.0, duration_hours=9.0, polyline6="fast", coords=coords),
        Route(distance_miles=640.0, duration_hours=11.0, polyline6="alt", coords=coords),
    ], False


class RouteApiTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        Place.objects.create(state="CO", key="denver", name="Denver", lat=39.0, lon=-105.0)
        Place.objects.create(state="MO", key="kansascity", name="Kansas City", lat=39.0, lon=-94.0)
        for opis, lon, price in ((9, -104.95, "3.60"), (10, -102.0, "3.50"), (11, -100.0, "2.90"), (12, -97.0, "3.20")):
            FuelStation.objects.create(opis_id=opis, name=f"S{opis}", address="I-70", city="Town", state="KS",
                                       price=Decimal(price), lat=39.02, lon=lon, geocode_source="t")

    def setUp(self):
        station_index.invalidate()
        cache.clear()

    def tearDown(self):
        station_index.invalidate()

    @mock.patch("routing.services.planner.osrm.get_routes", side_effect=_fake_routes)
    def test_plan_json(self, _route):
        resp = self.client.get("/api/route/", {"start": "Denver, CO", "finish": "Kansas City, MO"})
        self.assertEqual(resp.status_code, 200, resp.content)
        body = resp.json()
        self.assertEqual(body["meta"]["external_api_calls"], 1)
        self.assertEqual([s["opis_id"] for s in body["fuel_stops"]], [11])  # the $2.90 station
        stop = body["fuel_stops"][0]
        self.assertAlmostEqual(body["summary"]["total_fuel_cost"], stop["cost"], places=2)
        # 600 mi trip, 500 mi in the tank, plus the 5-gallon reserve that must remain at the finish.
        self.assertAlmostEqual(stop["gallons_purchased"], 15.0, delta=0.5)
        self.assertGreaterEqual(stop["fuel_on_arrival_gallons"], 5)
        self.assertGreaterEqual(body["summary"]["gallons_at_finish"], 5)
        self.assertGreater(stop["miles_since_last"], 0)
        self.assertAlmostEqual(stop["gallons_after_purchase"], stop["fuel_on_arrival_gallons"] + stop["gallons_purchased"], places=2)
        self.assertTrue(stop["next_is_finish"])
        # The no-planning driver fills at S12 ($3.20), the last pump in range; the plan picks S11 ($2.90).
        self.assertEqual(body["summary"]["baseline_stops"], 1)
        self.assertAlmostEqual(body["summary"]["savings"], 15 * 0.30, delta=0.05)
        parts = body["summary"]["savings_breakdown"]
        self.assertAlmostEqual(parts["from_cheaper_stations"] + parts["from_partial_fills"], body["summary"]["savings"], delta=0.01)
        self.assertTrue(stop["reason"].startswith("Last stop: bought just enough to reach the finish"), stop["reason"])
        self.assertEqual(body["summary"]["gallons_at_departure"], 50)
        self.assertGreaterEqual(body["summary"]["gallons_at_finish"], 0)
        self.assertIn("/api/route/map/?", body["map_url"])
        self.assertEqual(body["selected_route"], 0)
        self.assertEqual(len(body["options"]), 1)

    @mock.patch("routing.services.planner.osrm.get_routes", side_effect=_two_routes)
    def test_user_keeps_alternate_road(self, _routes):
        kept = self.client.get("/api/route/", {"start": "Denver, CO", "finish": "Kansas City, MO", "route": "1"})
        self.assertEqual(kept.status_code, 200, kept.content)
        body = kept.json()
        self.assertEqual(body["selected_route"], 1)
        self.assertEqual(body["route"]["label"], "Alternate road")
        self.assertEqual(body["route"]["distance_miles"], 640.0)
        self.assertEqual(len(body["options"]), 2)
        self.assertTrue(all(opt["feasible"] for opt in body["options"]))
        default = self.client.get("/api/route/", {"start": "Denver, CO", "finish": "Kansas City, MO"})
        self.assertEqual(default.json()["selected_route"], 0)
        self.assertEqual(default.json()["route"]["distance_miles"], 600.0)

    @mock.patch("routing.services.planner.osrm.get_routes", side_effect=_fake_routes)
    def test_post_json_body(self, _route):
        resp = self.client.post("/api/route/", json.dumps({"start": "Denver, CO", "finish": "Kansas City, MO", "start_fuel": 1}),
                                content_type="application/json")
        self.assertEqual(resp.status_code, 200, resp.content)
        self.assertGreater(resp.json()["summary"]["total_fuel_cost"], 0)

    @mock.patch("routing.services.planner.osrm.get_routes", side_effect=_fake_routes)
    def test_infeasible_empty_start_explains_needed_start_fuel(self, _route):
        resp = self.client.get("/api/route/", {"start": "Denver, CO", "finish": "Kansas City, MO",
                                                "start_fuel": "0", "corridor_miles": "2"})
        self.assertEqual(resp.status_code, 422)
        err = resp.json()["error"]
        self.assertIn("S9 (Town, KS)", err)
        self.assertIn("miles ahead", err)
        self.assertIn("Enter at least", err)
        self.assertNotIn("gallon", err)
        self.assertNotIn("Widening corridor", err)

    @mock.patch("routing.services.planner.osrm.get_routes", side_effect=_fake_routes)
    def test_infeasible_gap_names_the_stations(self, _route):
        FuelStation.objects.exclude(opis_id=9).delete()  # only the station by the start is left
        resp = self.client.get("/api/route/", {"start": "Denver, CO", "finish": "Kansas City, MO"})
        self.assertEqual(resp.status_code, 422)
        err = resp.json()["error"]
        self.assertIn("The last fuel station along this route is S9 (Town, KS)", err)
        self.assertIn("does not fix this", err)

    def test_validation_errors(self):
        self.assertEqual(self.client.get("/api/route/", {"start": "Denver, CO"}).status_code, 400)
        over = self.client.get("/api/route/", {"start": "Denver, CO", "finish": "Kansas City, MO", "start_fuel": "2"})
        self.assertEqual(over.status_code, 400)
        self.assertIn("1", over.json()["error"])
        self.assertEqual(self.client.get("/api/route/", {"start": "48.85,2.35", "finish": "Denver, CO"}).status_code, 400)
        self.assertEqual(self.client.get("/api/route/", {"start": "Denver, CO", "finish": "Kansas City, MO", "route": "2"}).status_code, 400)

    @mock.patch("routing.services.planner.osrm.get_routes", side_effect=_fake_routes)
    def test_map_page_renders(self, _route):
        resp = self.client.get("/api/route/map/", {"start": "Denver, CO", "finish": "Kansas City, MO"})
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "leaflet")
        self.assertContains(resp, "S11")
        self.assertContains(resp, "Last stop: bought just enough")
        self.assertContains(resp, "saving $")
        self.assertContains(resp, "This plan saves $4.5")
        self.assertContains(resp, "from stopping at cheaper stations")
        self.assertContains(resp, "from not filling up when a cheaper station is ahead")
        self.assertContains(resp, "how many miles from the route a fuel station can be")


class PlaceSuggestionTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        for name, state, pop, prio in (
            ("New York", "NY", 8_000_000, 0), ("Newark", "NJ", 300_000, 0), ("New Haven", "CT", 130_000, 0),
            ("Newton", "IA", 15_000, 0), ("Springfield", "IL", 110_000, 0), ("Springfield", "MO", 170_000, 0),
            ("Nashville-Davidson", "TN", 700_000, 0),
        ):
            Place.objects.create(state=state, key=geocoding.normalize_name(name), name=name, lat=40, lon=-90,
                                 population=pop, priority=prio)
        # Alias key pointing at the same place, as the loader creates for hyphenated names.
        Place.objects.create(state="TN", key="nashville", name="Nashville-Davidson", lat=40, lon=-90, population=700_000)

    def labels(self, q, **params):
        resp = self.client.get("/api/places/", {"q": q, **params})
        self.assertEqual(resp.status_code, 200)
        return [r["label"] for r in resp.json()["results"]]

    def test_prefix_ranked_by_population(self):
        self.assertEqual(self.labels("new"), ["New York, NY", "Newark, NJ", "New Haven, CT", "Newton, IA"])
        self.assertEqual(self.labels("new", limit=2), ["New York, NY", "Newark, NJ"])

    def test_state_filter_forms(self):
        self.assertEqual(self.labels("springfield, il"), ["Springfield, IL"])
        self.assertEqual(self.labels("springfield, illi"), ["Springfield, IL"])
        self.assertEqual(self.labels("springfield il"), ["Springfield, IL"])
        self.assertEqual(self.labels("springfield, zz"), [])

    def test_aliases_are_deduplicated(self):
        self.assertEqual(self.labels("nash"), ["Nashville-Davidson, TN"])

    def test_short_or_unknown_queries(self):
        self.assertEqual(self.labels("n"), [])
        self.assertEqual(self.labels("qqqq"), [])
        self.assertEqual(self.client.get("/api/places/", {"q": "x" * 101}).status_code, 400)

    def test_every_label_resolves_offline(self):
        with mock.patch("routing.services.geocoding._nominatim") as nom:
            for label in self.labels("new") + self.labels("nash") + self.labels("spring"):
                self.assertEqual(geocoding.geocode(label).source, "gazetteer", label)
            nom.assert_not_called()
