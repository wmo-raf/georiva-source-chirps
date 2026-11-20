"""
ChirpsClimatologyRecipe — the per-calendar-slot "normal" (Stage 1).

Manual: builds one climatological mean Item per calendar slot of a CHIRPS
resolution over an operator-configured baseline window (the WMO normal by
default). The anomaly recipe (Stage 2) subtracts against these. The engine owns
the run loop; this recipe is a pure ``(selector) -> units`` transform that reads
its source/output collections from the injected product declaration and its
baseline/min_count from config (ADR-0008) — it bakes no slugs or constants.

See docs/adr/0007-chirps-rolling-anomaly-product-structure.md and issue #2.
"""
from __future__ import annotations

import logging
from typing import Iterable

import numpy as np

from georiva.processing.recipe import (
    SOURCE_UNIT,
    BaseRecipe,
    OutputAsset,
    OutputItem,
    ProductionUnit,
    ResolvedInput,
)
from georiva.processing.registry import RecipeRegistry

from georiva_source_chirps.constants import DEFAULT_MIN_COUNT, resolution_of
from georiva_source_chirps.periods import slot_count, slot_index, slot_start
from georiva_source_chirps.recipes.stats import array_stats

logger = logging.getLogger(__name__)

# The sentinel year a calendar slot is encoded into on the normal's Item.time.
SENTINEL_YEAR = 1991


def _source_collection(selector: dict) -> str:
    """The raw staging collection slug from the product's declared inputs."""
    for ref in selector.get("inputs", []):
        if ref.get("tier") == "staging":
            return ref["collection"]
    raise ValueError(
        "ChirpsClimatologyRecipe: selector has no staging input collection "
        "(expected the injected product declaration, ADR-0008)"
    )


def _output_collection(selector: dict) -> str:
    """The normals collection slug from the product's declared outputs."""
    outputs = selector.get("outputs", [])
    if not outputs:
        raise ValueError(
            "ChirpsClimatologyRecipe: selector has no output collection "
            "(expected the injected product declaration, ADR-0008)"
        )
    return outputs[0]["collection"]




@RecipeRegistry.register
class ChirpsClimatologyRecipe(BaseRecipe):
    type = "chirps-climatology"
    version = "1"
    #: Read as held (ADR 0046 §13): a mean holds in any one unit, and the normal
    #: is written down in the unit it read in.
    input_units = {"value": SOURCE_UNIT}

    def candidate_units(self, trigger) -> Iterable[ProductionUnit]:
        """Manual only. ``dispatch_for_input`` still fans this product out on a
        raw staging arrival, so an event-marker trigger must no-op; the product
        space (slot × baseline) is enumerated from a deliberate, trigger-less
        manual/backfill selector instead."""
        trigger = trigger or {}
        if "staging_item_id" in trigger or "published_item_id" in trigger:
            return []
        return self.enumerate_units(trigger)

    def enumerate_units(self, selector) -> Iterable[ProductionUnit]:
        from georiva.processing.recipe import (
            binding_input_collection_id,
            binding_output_collection_id,
        )

        selector = selector or {}
        source = _source_collection(selector)
        output = _output_collection(selector)
        source_id = binding_input_collection_id(selector, "staging")
        output_id = binding_output_collection_id(selector, "climatology")
        resolution = resolution_of(source_id, source)
        baseline = [selector["baseline_start"], selector["baseline_end"]]
        min_count = selector.get("min_count", DEFAULT_MIN_COUNT)
        for slot in range(1, slot_count(resolution) + 1):
            yield {
                "source_collection": source,
                "source_collection_id": source_id,
                "output_collection": output,
                "output_collection_id": output_id,
                "resolution": resolution,
                "baseline": baseline,
                "min_count": min_count,
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
        return unit.get("min_count", DEFAULT_MIN_COUNT)

    @staticmethod
    def _slot_items(unit: ProductionUnit) -> list:
        """Staging slices in the source collection whose own ``datetime`` falls
        in the baseline years **and** in this unit's calendar slot."""
        from georiva.staging.models import StagingItem

        start, end = unit["baseline"]
        resolution = unit["resolution"]
        slot = unit["slot"]
        # Prefer the pinned collection FK (catalog-scoped, rename-safe); a legacy
        # selector with no id falls back to the declared slug.
        source_id = unit.get("source_collection_id")
        if source_id is not None:
            qs = StagingItem.objects.filter(collection__collection_id=source_id)
        else:
            qs = StagingItem.objects.filter(collection__slug=unit["source_collection"])
        items = []
        for si in (
            qs.select_related("collection__catalog").prefetch_related("assets")
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

        series = self.read_series(resolved["value"])
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

    def read_series(self, value: ResolvedInput):
        """Stack the slot's single-band CHIRPS slices into a (time, y, x) array,
        each read in the input's declared unit through core's read module.

        The only real I/O in the recipe and its single test seam: unit tests
        patch this to return an in-memory cube. Exact time coordinates are
        irrelevant here — the transform means the whole stack — so slices are
        stacked along a synthetic time index. Nodata arrives as NaN, so the mean
        skips it.
        """
        import xarray as xr

        assets = value.assets
        if not assets:
            raise ValueError("ChirpsClimatologyRecipe: no source assets to read")

        n = len(assets)
        logger.info("climatology: reading %d source slice(s) for the slot mean…", n)
        bands = []
        for i, a in enumerate(assets, 1):
            logger.info("climatology: reading source file %d/%d — %s",
                        i, n, getattr(a, "href", "?"))
            bands.append(value.field(a).data)
        logger.info("climatology: read all %d source file(s); stacking + averaging", n)
        return xr.DataArray(np.stack(bands, axis=0), dims=["time", "y", "x"])

    def output_unit(self, unit: ProductionUnit, resolved) -> str:
        """The normal is a mean of the rainfall, in the rainfall's source unit."""
        return self._source_variable(unit, resolved).source_unit.symbol

    # ---- helpers ------------------------------------------------------------

    @staticmethod
    def _collection_slug(unit: ProductionUnit) -> str:
        # The normals collection comes from the injected product declaration
        # (one collection per resolution; the baseline is config, not in the
        # slug). The recipe builds no slug of its own (ADR-0008).
        return unit["output_collection"]

    def _published_collection(self, unit, catalog):
        """The normals collection. Owned by the product declaration
        (OutputRef visibility="internal"), materialised at enable-time; this is an
        inert fallback that finds the pre-materialised row. Resolved by the pinned
        ``output_collection_id`` (ADR-0010 §5) when present — rename-safe — else by
        catalog+slug get_or_create for a legacy selector."""
        from georiva.core.models import Collection

        output_id = unit.get("output_collection_id")
        if output_id is not None:
            existing = Collection.objects.filter(id=output_id).first()
            if existing is not None:
                return existing
        slug = self._collection_slug(unit)
        collection, _ = Collection.objects.get_or_create(
            catalog=catalog, slug=slug,
            defaults={"name": slug, "derived_by_recipe": self.type},
        )
        return collection

    def _source_variable(self, unit, resolved):
        """The raw *published* collection's Variable (same slug as the staging
        collection) — the one promotion serves and provisioning gave the precip
        Variable. Staging assets carry no Variable."""
        from georiva.core.models import Variable

        si = resolved["value"].items[0]
        catalog = si.collection.catalog
        source_id = unit.get("source_collection_id")
        if source_id is not None:
            src = Variable.objects.filter(collection_id=source_id).first()
        else:
            src = Variable.objects.filter(
                collection__catalog=catalog,
                collection__slug=unit["source_collection"],
            ).first()
        if src is None:
            raise ValueError(
                "ChirpsClimatologyRecipe: no source Variable in raw collection "
                f"'{unit['source_collection']}'"
            )
        return src

    def _output_variable(self, unit, resolved):
        """The output ``precip`` Variable, mirroring the source: the normal is in
        the same unit and range as the input rainfall. Its source unit — and
        initial served unit — is the recipe's declared output unit (ADR 0046 §13)."""
        from georiva.core.models import Variable

        src = self._source_variable(unit, resolved)
        collection = self._published_collection(unit, resolved["value"].items[0].collection.catalog)
        out_unit = self.output_source_unit(unit, resolved)
        out_var, _ = Variable.objects.get_or_create(
            collection=collection, slug=src.slug,
            defaults={"name": src.name, "source_unit": out_unit, "unit": out_unit,
                      "value_min": src.value_min, "value_max": src.value_max},
        )
        return out_var
