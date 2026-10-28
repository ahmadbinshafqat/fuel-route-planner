"""One-off data build: Census Gazetteer -> Place table, fuel CSV -> geocoded FuelStation table.

The CSV only has city + state, so stations are placed at their town's Census
centroid. Towns missing from the Gazetteer (unincorporated communities) are
geocoded once via Nominatim and memoised in data/station_geocode_cache.json,
which is committed so later builds need no network.
"""

import csv
import io
import json
import time
import zipfile
from collections import defaultdict
from decimal import Decimal

import requests
from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from routing.models import FuelStation, Place
from routing.services import station_index
from routing.services.geocoding import STATES, _GAZ_SUFFIX, normalize_name

GAZETTEER_URL = "https://www2.census.gov/geo/docs/maps-data/data/gazetteer/2024_Gazetteer/{name}.zip"
GAZETTEER_FILES = (("2024_Gaz_place_national", 0), ("2024_Gaz_cousubs_national", 1))
POPULATION_URL = "https://www2.census.gov/programs-surveys/popest/datasets/2020-2024/cities/totals/sub-est2024.csv"
NOMINATIM_DELAY_SECONDS = 1.1  # Nominatim usage policy: max 1 request/second.


class Command(BaseCommand):
    help = "Load US places from the Census Gazetteer and geocoded fuel stations from the price CSV."

    def add_arguments(self, parser):
        parser.add_argument("--csv", default=str(settings.FUEL_PRICES_CSV), help="Path to the fuel price CSV.")
        parser.add_argument(
            "--offline", action="store_true",
            help="Never call Nominatim; stations whose town is not in the Gazetteer or cache are skipped.",
        )

    def handle(self, *args, **opts):
        raw_dir = settings.DATA_DIR / "raw"
        raw_dir.mkdir(parents=True, exist_ok=True)

        places = self._read_gazetteer(raw_dir)
        self.stdout.write(f"Gazetteer: {len(places)} place keys")

        stations = self._read_csv(opts["csv"])
        self.stdout.write(f"CSV: {len(stations)} unique US stations (by OPIS id)")

        cache_path = settings.DATA_DIR / "station_geocode_cache.json"
        geo_cache = json.loads(cache_path.read_text()) if cache_path.exists() else {}

        rows, skipped, sources = [], [], defaultdict(int)
        for st in stations:
            hit = places.get((st["state"], normalize_name(st["city"])))
            ck = f"{st['city'].upper()}|{st['state']}"
            # A county-subdivision centroid (e.g. a large Texas CCD) is coarser than a town point we
            # already geocoded, so a cached point wins over a priority-1 Gazetteer match.
            if hit and (hit["priority"] == 0 or not geo_cache.get(ck)):
                lat, lon, source = hit["lat"], hit["lon"], "gazetteer"
            else:
                if ck not in geo_cache and not opts["offline"]:
                    geo_cache[ck] = self._nominatim(st["city"], st["state"])
                    cache_path.write_text(json.dumps(geo_cache, indent=1, sort_keys=True))
                found = geo_cache.get(ck)
                if not found:
                    skipped.append(f"{st['city']}, {st['state']}")
                    continue
                lat, lon, source = found[0], found[1], "nominatim"
            sources[source] += 1
            rows.append(FuelStation(lat=lat, lon=lon, geocode_source=source, **st))

        with transaction.atomic():
            Place.objects.all().delete()
            Place.objects.bulk_create(
                [Place(state=s, key=k, name=v["name"], lat=v["lat"], lon=v["lon"],
                       priority=v["priority"], population=v["population"])
                 for (s, k), v in places.items()],
                batch_size=5000,
            )
            FuelStation.objects.all().delete()
            FuelStation.objects.bulk_create(rows, batch_size=2000)
        station_index.invalidate()

        self.stdout.write(self.style.SUCCESS(
            f"Loaded {len(rows)} stations ({dict(sources)}); {Place.objects.count()} places."
        ))
        if skipped:
            self.stdout.write(self.style.WARNING(f"Skipped {len(skipped)} stations with unknown towns: {sorted(set(skipped))}"))

    # -- inputs ------------------------------------------------------------------------------

    def _read_population(self, raw_dir):
        """Census 2024 estimates keyed by Gazetteer GEOID (places: state+place, MCDs: state+county+cousub)."""
        path = raw_dir / "sub-est2024.csv"
        if not path.exists():
            self.stdout.write("Downloading population estimates ...")
            resp = requests.get(POPULATION_URL, timeout=120)
            resp.raise_for_status()
            path.write_bytes(resp.content)
        pop = {}
        with path.open(encoding="latin-1", newline="") as fh:
            for rec in csv.DictReader(fh):
                if rec["SUMLEV"] == "162":
                    pop[rec["STATE"] + rec["PLACE"]] = int(rec["POPESTIMATE2024"])
                elif rec["SUMLEV"] == "061":
                    pop[rec["STATE"] + rec["COUNTY"] + rec["COUSUB"]] = int(rec["POPESTIMATE2024"])
        return pop

    def _read_gazetteer(self, raw_dir):
        population = self._read_population(raw_dir)
        places = {}
        for name, priority in GAZETTEER_FILES:
            path = raw_dir / f"{name}.txt"
            if not path.exists():
                self.stdout.write(f"Downloading {name} ...")
                resp = requests.get(GAZETTEER_URL.format(name=name), timeout=120)
                resp.raise_for_status()
                zipfile.ZipFile(io.BytesIO(resp.content)).extractall(raw_dir)
            with path.open(encoding="latin-1") as fh:
                reader = csv.reader(fh, delimiter="\t")
                header = [c.strip() for c in next(reader)]
                for raw in reader:
                    rec = dict(zip(header, raw))
                    state, full = rec["USPS"], rec["NAME"]
                    if state not in STATES:
                        continue
                    display = _strip_suffix(full)
                    # Numbered magisterial districts ("District 2") and "County subdivisions not defined"
                    # are not places anyone would search for.
                    if not display or any(ch.isdigit() for ch in display) or "not defined" in display.lower():
                        continue
                    pop = population.get(rec["GEOID"], 0)
                    keys = {normalize_name(full, gazetteer=True)}
                    if "-" in full:  # "Nashville-Davidson metropolitan government" -> also "Nashville"
                        keys.add(normalize_name(full.split("-")[0]))
                    for key in keys:
                        if not key:
                            continue
                        prev = places.get((state, key))
                        # Same name twice in a state (e.g. many "Washington" townships): keep the place
                        # over the county subdivision, then the more populous one.
                        if prev is None or (priority, -pop) < (prev["priority"], -prev["population"]):
                            places[(state, key)] = {
                                "name": display, "lat": float(rec["INTPTLAT"]),
                                "lon": float(rec["INTPTLONG"].strip()), "priority": priority, "population": pop,
                            }
        return places

    def _read_csv(self, path):
        best = {}
        try:
            fh = open(path, newline="", encoding="utf-8-sig")
        except OSError as exc:
            raise CommandError(f"Cannot open {path}: {exc}") from exc
        with fh:
            for rec in csv.DictReader(fh):
                state = rec["State"].strip().upper()
                if state not in STATES:  # drops Canadian provinces
                    continue
                opis_id = int(rec["OPIS Truckstop ID"])
                price = Decimal(rec["Retail Price"].strip())
                if opis_id in best and best[opis_id]["price"] <= price:
                    continue
                best[opis_id] = {
                    "opis_id": opis_id,
                    "name": rec["Truckstop Name"].strip(),
                    "address": rec["Address"].strip(),
                    "city": rec["City"].strip().title() if rec["City"].isupper() else rec["City"].strip(),
                    "state": state,
                    "rack_id": int(rec["Rack ID"]) if rec["Rack ID"].strip() else None,
                    "price": price,
                }
        return list(best.values())

    def _nominatim(self, city, state):
        cfg = settings.FUEL_ROUTE
        time.sleep(NOMINATIM_DELAY_SECONDS)
        try:
            resp = requests.get(
                f"{cfg['NOMINATIM_BASE_URL']}/search",
                params={"city": city, "state": STATES[state], "country": "us", "format": "jsonv2", "limit": 1},
                headers={"User-Agent": cfg["USER_AGENT"]},
                timeout=cfg["HTTP_TIMEOUT_SECONDS"],
            )
            resp.raise_for_status()
            hits = resp.json()
        except (requests.RequestException, ValueError) as exc:
            self.stderr.write(f"  nominatim failed for {city}, {state}: {exc}")
            return None
        self.stdout.write(f"  nominatim {city}, {state}: {'ok' if hits else 'not found'}")
        return [float(hits[0]["lat"]), float(hits[0]["lon"])] if hits else None


def _strip_suffix(name: str) -> str:
    """'Abbeville city' -> 'Abbeville', keeping original capitalisation."""
    lowered = name.strip().lower()
    stripped = _GAZ_SUFFIX.sub("", lowered)
    return name.strip()[: len(stripped)]
