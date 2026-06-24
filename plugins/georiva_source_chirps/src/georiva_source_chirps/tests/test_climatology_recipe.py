"""Behavioural tests for the ChirpsClimatologyRecipe.

These import the derivation engine contract from georiva, so they run under the
project's Django test runner (``make dev-test TEST_ARGS=georiva_source_chirps``),
mirroring core's processing/tests/test_climatology.py.
"""
from datetime import datetime, timezone
from unittest import TestCase
from unittest.mock import MagicMock, patch

import numpy as np
import pandas as pd
import xarray as xr
from django.test import TestCase as DjangoTestCase

from georiva.core.models import Catalog, Item, Unit, Variable
from georiva.processing.engine import run_unit
from georiva.staging.models import (
    DerivationLink,
    StagingAsset,
    StagingCollection,
    StagingItem,
)
from georiva_source_chirps.recipes.climatology import ChirpsClimatologyRecipe


def _mock_writer():
    w = MagicMock()
    w.write_cog.side_effect = lambda arr, path, *a, **k: path
    w.bucket.save.side_effect = lambda path, data: path
    return w


def _cube(values_by_time, ny=3, nx=2):
    """(time, y, x) cube; every pixel at time t equals values_by_time[t]."""
    time = pd.date_range("2001-06-01", periods=len(values_by_time), freq="YS")
    data = np.broadcast_to(
        np.asarray(values_by_time, dtype="float32")[:, None, None],
        (len(time), ny, nx),
    )
    return xr.DataArray(data, coords={"time": time}, dims=["time", "y", "x"])


class EnumerateUnitsTests(TestCase):
    def test_enumerates_one_unit_per_calendar_slot(self):
        selector = {
            "source_collection": "chirps-monthly",
            "resolution": "monthly",
            "baseline": [1991, 2020],
        }
        units = list(ChirpsClimatologyRecipe().enumerate_units(selector))

        # Monthly has 12 calendar slots; each unit carries its own slot.
        self.assertEqual(len(units), 12)
        self.assertEqual({u["slot"] for u in units}, set(range(1, 13)))

    def test_candidate_units_ignores_bare_arrival_triggers(self):
        # Climatology is scheduled/manual: a plain staging arrival (no period
        # config) must not trigger it.
        recipe = ChirpsClimatologyRecipe()
        trigger = {"staging_item_id": 5, "collection_slug": "chirps-monthly"}
        self.assertEqual(list(recipe.candidate_units(trigger)), [])

    def test_candidate_units_enumerates_an_explicit_selector(self):
        recipe = ChirpsClimatologyRecipe()
        selector = {
            "source_collection": "chirps-monthly",
            "resolution": "monthly",
            "baseline": [1991, 2020],
        }
        self.assertEqual(len(list(recipe.candidate_units(selector))), 12)


class _ClimoFixture(DjangoTestCase):
    """A monthly CHIRPS staging collection with one source asset per item."""

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
        # The source Variable lives on the raw *published* collection (same slug
        # as the staging collection) — what promotion serves — not on the
        # staging asset, which carries no Variable in production.
        from georiva.core.models import Collection

        self.src_col = Collection.objects.create(
            catalog=self.catalog, slug=self.SOURCE, name=self.SOURCE
        )
        self.src_var = Variable.objects.create(
            collection=self.src_col, slug="precip", name="Precipitation",
            unit=self.unit_mm, value_min=0, value_max=1000,
        )

    def _stage(self, dt):
        """Register one monthly CHIRPS slice valid at ``dt`` + its source asset
        (no Variable on the asset, matching the real staging consumer)."""
        item = StagingItem.objects.create(
            collection=self.scol,
            datetime=dt,
            bounds=[0, 0, 1, 1], crs="EPSG:4326", width=2, height=3,
        )
        StagingAsset.objects.create(
            item=item, href=f"chirps/{dt:%Y%m}.tif", roles=["source"],
            format="geotiff", checksum=f"sum-{dt:%Y%m}",
        )
        return item

    def _unit(self, **over):
        unit = {
            "source_collection": self.SOURCE, "resolution": "monthly",
            "baseline": [1991, 2020], "slot": 6,
        }
        unit.update(over)
        return unit


class ResolveInputsTests(_ClimoFixture):
    def test_value_resolves_only_the_slots_slices_within_the_baseline(self):
        utc = timezone.utc
        in_a = self._stage(datetime(2011, 6, 15, tzinfo=utc))   # June, in baseline
        in_b = self._stage(datetime(2012, 6, 10, tzinfo=utc))   # June, in baseline
        self._stage(datetime(2011, 1, 15, tzinfo=utc))          # wrong slot (Jan)
        self._stage(datetime(2025, 6, 15, tzinfo=utc))          # after baseline
        self._stage(datetime(1985, 6, 15, tzinfo=utc))          # before baseline

        resolved = ChirpsClimatologyRecipe().resolve_inputs(self._unit())

        got = {si.pk for si in resolved["value"].items}
        self.assertEqual(got, {in_a.pk, in_b.pk})


class MinCountGuardTests(_ClimoFixture):
    def _resolved_with(self, n):
        for y in range(2001, 2001 + n):
            self._stage(datetime(y, 6, 15, tzinfo=timezone.utc))
        recipe = ChirpsClimatologyRecipe()
        return recipe, recipe.resolve_inputs(self._unit())

    def test_thin_slot_is_not_ready_under_the_default_minimum(self):
        # Three years of June is well under the 20-year default → not ready,
        # so the engine produces no normal Item for this slot.
        recipe, resolved = self._resolved_with(3)
        self.assertFalse(recipe.readiness(self._unit(), resolved))

    def test_slot_is_ready_once_the_minimum_is_met(self):
        # The minimum is overridable per unit (for short records / tests).
        recipe, resolved = self._resolved_with(3)
        self.assertTrue(recipe.readiness(self._unit(min_count=2), resolved))


class RunUnitTests(_ClimoFixture):
    def _run(self, unit, cube):
        recipe = ChirpsClimatologyRecipe()
        self.writer = _mock_writer()
        with patch.object(ChirpsClimatologyRecipe, "read_series", return_value=cube):
            return run_unit(recipe, unit, writer=self.writer)

    def test_run_produces_the_normal_item_asset_and_lineage(self):
        utc = timezone.utc
        a = self._stage(datetime(2011, 6, 15, tzinfo=utc))
        b = self._stage(datetime(2012, 6, 10, tzinfo=utc))
        # Two June slices carrying 10 and 20 -> climatological mean 15.
        result = self._run(self._unit(min_count=2), _cube([10.0, 20.0]))

        self.assertEqual(result.status, "completed")
        item = Item.objects.get(pk=result.item_id)
        # Resolution + baseline live in the slug; the slot lives in Item.time.
        self.assertEqual(item.collection.slug, "chirps-monthly-climatology-1991-2020")
        self.assertEqual(item.time, datetime(1991, 6, 1, tzinfo=utc))
        # The normals are a derivation intermediate, not a served product.
        self.assertEqual(item.collection.visibility, "internal")

        # The true slot/baseline/count are recorded on the item.
        climo = item.properties["climatology"]
        self.assertEqual(climo["slot"], 6)
        self.assertEqual(climo["baseline"], [1991, 2020])
        self.assertEqual(climo["count"], 2)

        # The written mean (15) shows up in the derived asset's stats.
        asset = item.assets.get()
        self.assertIn("data", asset.roles)
        self.assertAlmostEqual(asset.stats_mean, 15.0)

        links = DerivationLink.objects.filter(derived_item=item)
        self.assertEqual({l.source_staging_item_id for l in links}, {a.pk, b.pk})
        self.assertEqual(links.first().recipe_id, "chirps-climatology")
