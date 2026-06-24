"""Behavioural tests for the ChirpsAnomalyRecipe (Stage 2).

Run via ``make dev-test TEST_ARGS=georiva_source_chirps``.
"""
from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

import numpy as np
from django.test import TestCase as DjangoTestCase

from georiva.core.models import Asset, Catalog, Collection, Item, Unit, Variable
from georiva.processing.engine import run_unit
from georiva.staging.models import (
    DerivationLink,
    StagingAsset,
    StagingCollection,
    StagingItem,
)
from georiva_source_chirps.recipes.anomaly import ChirpsAnomalyRecipe

UTC = timezone.utc

CLIMO_SLUG = "chirps-monthly-climatology-1991-2020"


def _mock_writer():
    w = MagicMock()
    w.write_cog.side_effect = lambda arr, path, *a, **k: path
    w.bucket.save.side_effect = lambda path, data: path
    return w


class _AnomalyFixture(DjangoTestCase):
    SOURCE = "chirps-monthly"

    def setUp(self):
        self.catalog = Catalog.objects.create(
            name="CHIRPS", slug="chirps", file_format="geotiff"
        )
        self.scol = StagingCollection.objects.create(
            catalog=self.catalog, slug=self.SOURCE, name=self.SOURCE
        )
        self.unit_mm, _ = Unit.objects.get_or_create(
            symbol="mm", defaults={"name": "millimetre"}
        )
        self.src_col = Collection.objects.create(
            catalog=self.catalog, slug="chirps-monthly-src", name="src"
        )
        self.src_var = Variable.objects.create(
            collection=self.src_col, slug="precip", name="Precipitation",
            unit=self.unit_mm, value_min=0, value_max=1000,
        )

    def _stage(self, dt):
        item = StagingItem.objects.create(
            collection=self.scol, datetime=dt,
            bounds=[0, 0, 1, 1], crs="EPSG:4326", width=2, height=3,
        )
        StagingAsset.objects.create(
            item=item, href=f"chirps/{dt:%Y%m}.tif", roles=["source"],
            format="geotiff", checksum=f"sum-{dt:%Y%m}", variable=self.src_var,
        )
        return item

    def _publish_normal(self, time, checksum="normal-jun"):
        """A published climatology Item (one calendar slot's normal) + its COG."""
        col, _ = Collection.objects.get_or_create(
            catalog=self.catalog, slug=CLIMO_SLUG, defaults={"name": CLIMO_SLUG}
        )
        var, _ = Variable.objects.get_or_create(
            collection=col, slug="precip",
            defaults={"name": "Precipitation", "unit": self.unit_mm,
                      "value_min": 0, "value_max": 1000},
        )
        item = Item.objects.create(
            collection=col, time=time, bounds=[0, 0, 1, 1], crs="EPSG:4326",
            width=2, height=3,
        )
        Asset.objects.create(
            item=item, variable=var, format="cog", href=f"{CLIMO_SLUG}.tif",
            roles=["data"], checksum=checksum,
        )
        return item

    def _unit(self, si, quantity="anomaly", valid=datetime(2024, 6, 15, tzinfo=UTC)):
        return {
            "staging_item_id": si.pk, "source_collection": self.SOURCE,
            "resolution": "monthly", "baseline": [1991, 2020],
            "valid_time": valid.isoformat(), "quantity": quantity,
        }


class CandidateUnitsTests(_AnomalyFixture):
    def test_staging_arrival_yields_one_unit_per_quantity(self):
        si = self._stage(datetime(2024, 6, 15, tzinfo=UTC))
        trigger = {"staging_item_id": si.pk, "collection_slug": self.SOURCE}

        units = list(ChirpsAnomalyRecipe().candidate_units(trigger))

        self.assertEqual(
            {u["quantity"] for u in units}, {"anomaly", "relative_anomaly"}
        )
        for u in units:
            self.assertEqual(u["staging_item_id"], si.pk)
            self.assertEqual(u["resolution"], "monthly")
            self.assertEqual(u["baseline"], [1991, 2020])

    def test_published_trigger_is_ignored(self):
        # Promotion outputs, climatology outputs, and its own outputs all arrive
        # as published triggers; the anomaly consumes staging values only.
        trigger = {
            "published_item_id": 7,
            "collection_slug": "chirps-monthly-climatology-1991-2020",
        }
        self.assertEqual(list(ChirpsAnomalyRecipe().candidate_units(trigger)), [])


class EnumerateUnitsTests(_AnomalyFixture):
    def test_backfill_enumerates_every_staged_slice_times_quantities(self):
        self._stage(datetime(2022, 6, 15, tzinfo=UTC))
        self._stage(datetime(2023, 7, 10, tzinfo=UTC))

        units = list(
            ChirpsAnomalyRecipe().enumerate_units({"source_collection": self.SOURCE})
        )

        # 2 staged slices × 2 quantities.
        self.assertEqual(len(units), 4)
        self.assertEqual({u["quantity"] for u in units}, {"anomaly", "relative_anomaly"})
        self.assertTrue(all(u["resolution"] == "monthly" for u in units))

    def test_backfill_respects_a_year_range(self):
        self._stage(datetime(2022, 6, 15, tzinfo=UTC))
        self._stage(datetime(2030, 6, 15, tzinfo=UTC))

        units = list(ChirpsAnomalyRecipe().enumerate_units(
            {"source_collection": self.SOURCE, "year_range": [2000, 2025]}
        ))

        # Only the 2022 slice is in range → × 2 quantities.
        self.assertEqual(len(units), 2)


class ResolveInputsTests(_AnomalyFixture):
    def test_resolves_value_slice_and_the_matching_published_normal(self):
        si = self._stage(datetime(2024, 6, 15, tzinfo=UTC))
        # The June normal is keyed on the sentinel-year slot time 1991-06-01.
        normal = self._publish_normal(datetime(1991, 6, 1, tzinfo=UTC))

        resolved = ChirpsAnomalyRecipe().resolve_inputs(self._unit(si))

        self.assertEqual([i.pk for i in resolved["value"].items], [si.pk])
        self.assertEqual([i.pk for i in resolved["baseline"].items], [normal.pk])

    def test_baseline_absent_when_the_normal_is_not_built_yet(self):
        si = self._stage(datetime(2024, 6, 15, tzinfo=UTC))  # no normal published

        resolved = ChirpsAnomalyRecipe().resolve_inputs(self._unit(si))

        # Required-but-absent → engine readiness skips the unit cleanly.
        self.assertFalse(resolved["baseline"].present)
        self.assertFalse(
            ChirpsAnomalyRecipe().readiness(self._unit(si), resolved)
        )


class RunUnitTests(_AnomalyFixture):
    def _run(self, unit, value_arr, normal_arr):
        recipe = ChirpsAnomalyRecipe()
        self.writer = _mock_writer()
        with patch.object(ChirpsAnomalyRecipe, "read_value", return_value=value_arr), \
                patch.object(ChirpsAnomalyRecipe, "read_normal", return_value=normal_arr):
            return run_unit(recipe, unit, writer=self.writer)

    def test_absolute_anomaly_is_value_minus_normal_with_dual_lineage(self):
        si = self._stage(datetime(2024, 6, 15, tzinfo=UTC))
        normal = self._publish_normal(datetime(1991, 6, 1, tzinfo=UTC))
        value_arr = np.full((3, 2), 30.0, dtype="float32")
        normal_arr = np.full((3, 2), 20.0, dtype="float32")

        result = self._run(self._unit(si, "anomaly"), value_arr, normal_arr)

        self.assertEqual(result.status, "completed")
        item = Item.objects.get(pk=result.item_id)
        self.assertEqual(item.collection.slug, "chirps-monthly-anomaly-1991-2020")
        # Anomaly Items are keyed on the real valid time, not a sentinel.
        self.assertEqual(item.time, datetime(2024, 6, 15, tzinfo=UTC))

        asset = item.assets.get()
        self.assertAlmostEqual(asset.stats_mean, 10.0)  # 30 - 20

        links = DerivationLink.objects.filter(derived_item=item)
        self.assertEqual(links.filter(source_staging_item=si).count(), 1)
        self.assertEqual(links.filter(source_published_item=normal).count(), 1)

    def test_relative_anomaly_uses_a_distinct_collection_and_is_a_ratio(self):
        si = self._stage(datetime(2024, 6, 15, tzinfo=UTC))
        self._publish_normal(datetime(1991, 6, 1, tzinfo=UTC))
        value_arr = np.full((3, 2), 30.0, dtype="float32")
        normal_arr = np.full((3, 2), 20.0, dtype="float32")

        result = self._run(self._unit(si, "relative_anomaly"), value_arr, normal_arr)

        item = Item.objects.get(pk=result.item_id)
        self.assertEqual(
            item.collection.slug, "chirps-monthly-relative-anomaly-1991-2020"
        )
        asset = item.assets.get()
        self.assertAlmostEqual(asset.stats_mean, 0.5)  # (30-20)/20

    def test_relative_anomaly_is_nodata_where_the_normal_is_zero(self):
        si = self._stage(datetime(2024, 6, 15, tzinfo=UTC))
        self._publish_normal(datetime(1991, 6, 1, tzinfo=UTC))
        value_arr = np.full((3, 2), 5.0, dtype="float32")
        normal_arr = np.zeros((3, 2), dtype="float32")  # arid / dry-season slot

        result = self._run(self._unit(si, "relative_anomaly"), value_arr, normal_arr)

        # safe_divide maps divide-by-zero to NaN, so the whole raster is nodata
        # and carries no finite stats.
        asset = Item.objects.get(pk=result.item_id).assets.get()
        self.assertIsNone(asset.stats_mean)
