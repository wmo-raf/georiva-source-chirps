"""Each CHIRPS collection's precipitation is a total over its period (GeoRiva ADR 0051)."""

from django.test import SimpleTestCase

from georiva.sources.collection_definitions import parse_collection_defs
from georiva_source_chirps.models import COLLECTIONS


class TimeStatisticTests(SimpleTestCase):
    def test_precipitation_is_a_sum_over_the_period_before_its_time(self):
        for definition in parse_collection_defs(COLLECTIONS):
            for variable in definition.variables:
                with self.subTest(definition.key):
                    self.assertEqual(
                        (variable.time_statistic, variable.statistic_window), ("sum", "preceding_interval")
                    )
