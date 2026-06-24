"""
ChirpsClimatologyRecipe — the per-calendar-slot "normal" (Stage 1).

Scheduled/manual: builds one climatological mean Item per calendar slot of a
CHIRPS resolution over a fixed baseline window (the WMO normal). The anomaly
recipe (Stage 2) subtracts against these. The engine owns the run loop; this
recipe only declares the product.

See docs/adr/0007-chirps-rolling-anomaly-product-structure.md and issue #2.
"""
from __future__ import annotations

from typing import Iterable

import numpy as np

from georiva.processing.recipe import (
    BaseRecipe,
    OutputAsset,
    OutputItem,
    ProductionUnit,
    ResolvedInput,
)
from georiva.processing.registry import RecipeRegistry

from georiva_source_chirps.periods import slot_count, slot_index, slot_start
from georiva_source_chirps.recipes.stats import array_stats

# The sentinel year a calendar slot is encoded into on the normal's Item.time.
SENTINEL_YEAR = 1991


@RecipeRegistry.register
class ChirpsClimatologyRecipe(BaseRecipe):
    type = "chirps-climatology"
    version = "1"

    # A normal built from too few years is a weak reference everything
    # downstream subtracts against, so a slot below this many contributing
    # slices is skipped rather than published. Overridable per unit.
    MIN_COUNT = 20

    def candidate_units(self, trigger) -> Iterable[ProductionUnit]:
        """Scheduled/manual only: the product space (slot × baseline) isn't
        derivable from a bare arriving input, so a trigger lacking the explicit
        period config yields nothing. Invoked over an explicit selector instead."""
        trigger = trigger or {}
        if not (
            trigger.get("source_collection")
            and trigger.get("resolution")
            and trigger.get("baseline")
        ):
            return []
        return self.enumerate_units(trigger)

    def enumerate_units(self, selector) -> Iterable[ProductionUnit]:
        selector = selector or {}
        source = selector["source_collection"]
        resolution = selector["resolution"]
        baseline = selector["baseline"]
        for slot in range(1, slot_count(resolution) + 1):
            yield {
                "source_collection": source,
                "resolution": resolution,
                "baseline": baseline,
                "slot": slot,
            }

    def resolve_inputs(self, unit: ProductionUnit) -> "dict[str, ResolvedInput]":
        items = self._slot_items(unit)
        assets = [a for si in items for a in si.assets.all()]
        return {
            "value": ResolvedInput("value", required=True, items=items, assets=assets),
        }

    def readiness(self, unit: ProductionUnit, resolved) -> bool:
        """Ready only when the required inputs are present *and* the slot has at
        least ``min_count`` contributing slices (else the normal is skipped)."""
        if not super().readiness(unit, resolved):
            return False
        value = resolved.get("value")
        return value is not None and len(value.items) >= self._min_count(unit)

    def _min_count(self, unit: ProductionUnit) -> int:
        return unit.get("min_count", self.MIN_COUNT)

    @staticmethod
    def _slot_items(unit: ProductionUnit) -> list:
        """Staging slices in the source collection whose own ``datetime`` falls
        in the baseline years **and** in this unit's calendar slot."""
        from georiva.staging.models import StagingItem

        start, end = unit["baseline"]
        resolution = unit["resolution"]
        slot = unit["slot"]
        items = []
        for si in (
            StagingItem.objects
            .filter(collection__slug=unit["source_collection"])
            .select_related("collection__catalog")
            .prefetch_related("assets")
        ):
            dt = si.datetime
            if dt is None:
                continue
            if start <= dt.year <= end and slot_index(dt, resolution) == slot:
                items.append(si)
        return items

    def outputs(self, unit: ProductionUnit) -> OutputItem:
        from datetime import timezone

        items = self._slot_items(unit)
        si = items[0]
        collection = self._published_collection(unit, si.collection.catalog)
        time = slot_start(unit["resolution"], unit["slot"], SENTINEL_YEAR)
        return OutputItem(
            collection=collection,
            time=time.replace(tzinfo=timezone.utc),
            bounds=si.bounds, crs=si.crs, width=si.width, height=si.height,
            properties={"climatology": {
                "resolution": unit["resolution"], "slot": unit["slot"],
                "baseline": unit["baseline"], "count": len(items),
            }},
        )

    def transform(self, unit: ProductionUnit, resolved) -> "list[OutputAsset]":
        from georiva.geoprocessing import temporal_aggregate

        series = self.read_series(resolved["value"].assets)
        mean = temporal_aggregate(series, freq=None)
        array = np.asarray(mean, dtype="float32")

        si = resolved["value"].items[0]
        out_var = self._output_variable(unit, resolved)
        return [OutputAsset(
            variable=out_var, roles=["data"], format="cog",
            array=array, bounds=si.bounds, crs=si.crs,
            width=si.width, height=si.height,
            stats=array_stats(array),
        )]

    # ---- I/O seam (mocked in tests) ----------------------------------------

    def read_series(self, assets):
        """Stack the slot's single-band CHIRPS GeoTIFFs into a (time, y, x) array.

        The only real I/O in the recipe and its single test seam: unit tests
        patch this to return an in-memory cube. Exact time coordinates are
        irrelevant here — the transform means the whole stack — so slices are
        concatenated along a synthetic time index.
        """
        import tempfile

        import rioxarray  # noqa: F401  (registers the rasterio engine)
        import xarray as xr

        from georiva.core.storage import BucketType, storage

        if not assets:
            raise ValueError("ChirpsClimatologyRecipe: no source assets to read")

        arrays = []
        for asset in assets:
            data = storage.bucket(BucketType.STAGING).read_bytes(asset.href)
            with tempfile.NamedTemporaryFile(suffix=".tif") as fh:
                fh.write(data)
                fh.flush()
                da = rioxarray.open_rasterio(fh.name).squeeze(drop=True)
            arrays.append(da)
        return xr.concat(arrays, dim="time")

    # ---- helpers ------------------------------------------------------------

    @staticmethod
    def _collection_slug(unit: ProductionUnit) -> str:
        b0, b1 = unit["baseline"]
        return f"{unit['source_collection']}-climatology-{b0}-{b1}"

    def _published_collection(self, unit, catalog):
        from georiva.core.models import Collection

        slug = self._collection_slug(unit)
        collection, _ = Collection.objects.get_or_create(
            catalog=catalog, slug=slug, defaults={"name": slug},
        )
        return collection

    def _output_variable(self, unit, resolved):
        """The output ``precip`` Variable, mirroring the source (the normal is in
        the same unit and range as the input rainfall)."""
        from georiva.core.models import Variable

        src = next(
            (a.variable for a in resolved["value"].assets if a.variable), None
        )
        if src is None:
            raise ValueError(
                "ChirpsClimatologyRecipe: source asset has no Variable to mirror"
            )
        si = resolved["value"].items[0]
        collection = self._published_collection(unit, si.collection.catalog)
        out_var, _ = Variable.objects.get_or_create(
            collection=collection, slug=src.slug,
            defaults={"name": src.name, "unit": src.unit,
                      "value_min": src.value_min, "value_max": src.value_max},
        )
        return out_var
