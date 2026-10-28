from django.db import models


class Place(models.Model):
    """A US place (city/town/CDP/township) from the Census Gazetteer.

    Used to geocode both the fuel stations (which only have city + state) and
    "City, ST" request inputs without calling an external geocoder.
    """

    state = models.CharField(max_length=2)
    key = models.CharField(max_length=80, help_text="Normalised name, see services.geocoding.normalize_name")
    name = models.CharField(max_length=120)
    lat = models.FloatField()
    lon = models.FloatField()
    # Lower is better: 0 = incorporated place / CDP, 1 = county subdivision.
    priority = models.PositiveSmallIntegerField(default=0)
    # Census 2024 estimate; 0 for CDPs, which the estimates file does not cover. Ranks autocomplete.
    population = models.PositiveIntegerField(default=0)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["state", "key"], name="uniq_place_state_key")]
        indexes = [models.Index(fields=["key"], name="place_key_idx")]

    def __str__(self):
        return f"{self.name}, {self.state}"


class FuelStation(models.Model):
    opis_id = models.PositiveIntegerField(unique=True)
    name = models.CharField(max_length=120)
    address = models.CharField(max_length=200)
    city = models.CharField(max_length=80)
    state = models.CharField(max_length=2)
    rack_id = models.PositiveIntegerField(null=True)
    # Cheapest retail price listed for this OPIS id (the CSV has duplicates).
    price = models.DecimalField(max_digits=7, decimal_places=4)
    lat = models.FloatField()
    lon = models.FloatField()
    geocode_source = models.CharField(max_length=20)

    class Meta:
        indexes = [models.Index(fields=["state", "city"])]

    def __str__(self):
        return f"{self.name} ({self.city}, {self.state}) ${self.price}"
