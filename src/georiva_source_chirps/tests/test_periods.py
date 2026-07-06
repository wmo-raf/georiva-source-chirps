"""Unit tests for the shared CHIRPS calendar-slot helpers (pure, no DB).

Written as a ``unittest.TestCase`` so the project's Django test runner
(``make dev-test`` / ``georiva test``) discovers them alongside everything else;
they also run under bare ``pytest`` since they touch no database.
"""
from datetime import datetime
from unittest import TestCase

from georiva_source_chirps.periods import (
    dekad_of_month,
    pentad_of_month,
    slot_count,
    slot_index,
    slot_start,
    slot_time,
)


class SlotIndexTests(TestCase):
    def test_monthly_slot_index_is_the_calendar_month(self):
        # June is the 6th monthly slot of the year.
        self.assertEqual(slot_index(datetime(2024, 6, 15), "monthly"), 6)

    def test_dekadal_slot_index_counts_three_dekads_per_month(self):
        # Three dekads per month: days 1-10, 11-20, 21-end. February's three
        # dekads are slots 4, 5, 6 of the year; boundaries are days 1 / 11 / 21.
        self.assertEqual(slot_index(datetime(2024, 2, 1), "dekadal"), 4)
        self.assertEqual(slot_index(datetime(2024, 2, 11), "dekadal"), 5)
        self.assertEqual(slot_index(datetime(2024, 2, 21), "dekadal"), 6)
        self.assertEqual(slot_index(datetime(2024, 2, 28), "dekadal"), 6)

    def test_pentadal_slot_index_counts_six_pentads_per_month(self):
        # Six pentads per month: days 1-5, 6-10, 11-15, 16-20, 21-25, 26-end.
        # March's six pentads are slots 13-18; the trailing pentad absorbs the
        # short remainder of the month (days 26-31 stay slot 18).
        self.assertEqual(slot_index(datetime(2024, 3, 1), "pentadal"), 13)
        self.assertEqual(slot_index(datetime(2024, 3, 6), "pentadal"), 14)
        self.assertEqual(slot_index(datetime(2024, 3, 26), "pentadal"), 18)
        self.assertEqual(slot_index(datetime(2024, 3, 31), "pentadal"), 18)


class SlotCountTests(TestCase):
    def test_slot_count_is_twelve_thirtysix_seventytwo(self):
        self.assertEqual(slot_count("monthly"), 12)
        self.assertEqual(slot_count("dekadal"), 36)
        self.assertEqual(slot_count("pentadal"), 72)


class SlotEncodingTests(TestCase):
    def test_slot_start_maps_a_slot_to_its_starting_date_in_the_sentinel_year(self):
        # Slot start days are deterministic: monthly -> day 1; dekadal -> 1/11/21;
        # pentadal -> 1/6/11/16/21/26. The sentinel year carries the encoding.
        self.assertEqual(slot_start("monthly", 6, 1991), datetime(1991, 6, 1))
        self.assertEqual(slot_start("dekadal", 5, 1991), datetime(1991, 2, 11))
        self.assertEqual(slot_start("pentadal", 14, 1991), datetime(1991, 3, 6))

    def test_slot_time_encodes_a_slices_slot_into_the_sentinel_year(self):
        # Different real years collapse to the same sentinel-year slot time —
        # the join key shared by the climatology Item.time and the anomaly.
        self.assertEqual(slot_time(datetime(2024, 6, 15), "monthly"), datetime(1991, 6, 1))
        self.assertEqual(slot_time(datetime(2024, 2, 15), "dekadal"), datetime(1991, 2, 11))
        self.assertEqual(slot_time(datetime(2024, 3, 8), "pentadal"), datetime(1991, 3, 6))

    def test_slot_time_round_trips_through_slot_index_for_every_slot(self):
        # Reversibility: encoding a slot to a time and reading the slot back must
        # be the identity, for every slot of every resolution. The whole anomaly
        # join rests on this.
        for resolution in ("monthly", "dekadal", "pentadal"):
            for slot in range(1, slot_count(resolution) + 1):
                encoded = slot_start(resolution, slot, 1991)
                self.assertEqual(slot_index(encoded, resolution), slot)


class WithinMonthTests(TestCase):
    def test_dekad_of_month_numbers_the_three_within_month_dekads(self):
        # Within-month dekad (1-3), as used to build the CHIRPS download URL.
        self.assertEqual(dekad_of_month(datetime(2024, 2, 1)), 1)
        self.assertEqual(dekad_of_month(datetime(2024, 2, 11)), 2)
        self.assertEqual(dekad_of_month(datetime(2024, 2, 21)), 3)
        self.assertEqual(dekad_of_month(datetime(2024, 2, 28)), 3)

    def test_pentad_of_month_numbers_the_six_within_month_pentads(self):
        # Within-month pentad (1-6); the trailing pentad absorbs the remainder.
        self.assertEqual(pentad_of_month(datetime(2024, 3, 1)), 1)
        self.assertEqual(pentad_of_month(datetime(2024, 3, 6)), 2)
        self.assertEqual(pentad_of_month(datetime(2024, 3, 26)), 6)
        self.assertEqual(pentad_of_month(datetime(2024, 3, 31)), 6)
