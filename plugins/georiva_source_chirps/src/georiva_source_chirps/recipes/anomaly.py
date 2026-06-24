"""
ChirpsAnomalyRecipe — the rolling per-calendar-slot anomaly (Stage 2).

Event-driven: each arriving CHIRPS slice yields one anomaly per quantity
(absolute mm and relative percent-of-normal) against the matching published
climatology slot. The slice is the ``value`` input (Staging tier); the normal is
the ``baseline`` input (Published tier), joined by the slot's sentinel-year time.

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

from georiva_source_chirps.constants import CHIRPS_BASELINE, resolution_from_slug
from georiva_source_chirps.periods import slot_time
from georiva_source_chirps.recipes.stats import array_stats

# The two quantities every arriving slice produces.
QUANTITIES = ("anomaly", "relative_anomaly")


@RecipeRegistry.register
class ChirpsAnomalyRecipe(BaseRecipe):
    type = "chirps-anomaly"
    version = "1"
    
    def candidate_units(self, trigger) -> Iterable[ProductionUnit]:
        """Staging-value arrivals only. Published-item triggers (promotion
        outputs, climatology outputs, our own outputs) are ignored so the
        anomaly never double-fires or mis-reads a non-value input."""
        trigger = trigger or {}
        if "published_item_id" in trigger:
            return []
        if "staging_item_id" in trigger:
            return list(self._units_for_staging_item(trigger["staging_item_id"]))
        return self.enumerate_units(trigger)
    
    def _units_for_staging_item(self, staging_item_id) -> Iterable[ProductionUnit]:
        from georiva.staging.models import StagingItem
        
        si = (
            StagingItem.objects
            .select_related("collection")
            .filter(pk=staging_item_id)
            .first()
        )
        if si is None or si.datetime is None:
            return
        resolution = resolution_from_slug(si.collection.slug)
        for quantity in QUANTITIES:
            yield self._make_unit(si, resolution, quantity, list(CHIRPS_BASELINE))
    
    @staticmethod
    def _make_unit(si, resolution, quantity, baseline) -> ProductionUnit:
        return {
            "staging_item_id": si.pk,
            "source_collection": si.collection.slug,
            "resolution": resolution,
            "baseline": baseline,
            "valid_time": si.datetime.isoformat(),
            "quantity": quantity,
        }
    
    def enumerate_units(self, selector) -> Iterable[ProductionUnit]:
        """Manual backfill: anomalies for every already-staged slice in range.

        Needed because the engine's staleness sweep only re-runs units with an
        existing run record; it cannot resurrect the units that were *skipped*
        for un-readiness while their normals were still being built. Documented
        order: build climatology, then run this backfill, then steady-state
        events flow through ``candidate_units``.
        """
        from georiva.staging.models import StagingItem
        
        selector = selector or {}
        source = selector["source_collection"]
        resolution = resolution_from_slug(source)
        baseline = selector.get("baseline", list(CHIRPS_BASELINE))
        year_range = selector.get("year_range")
        
        for si in (
                StagingItem.objects
                        .select_related("collection")
                        .filter(collection__slug=source)
        ):
            if si.datetime is None:
                continue
            if year_range and not (year_range[0] <= si.datetime.year <= year_range[1]):
                continue
            for quantity in QUANTITIES:
                yield self._make_unit(si, resolution, quantity, baseline)
    
    def resolve_inputs(self, unit: ProductionUnit) -> "dict[str, ResolvedInput]":
        value_items, value_assets = self._value(unit)
        base_items, base_assets = self._baseline(unit)
        return {
            "value": ResolvedInput(
                "value", required=True, items=value_items, assets=value_assets
            ),
            "baseline": ResolvedInput(
                "baseline", required=True, items=base_items, assets=base_assets
            ),
        }
    
    @staticmethod
    def _value(unit):
        """The arriving raw slice (Staging tier) and its source asset."""
        from georiva.staging.models import StagingItem
        
        si = (
            StagingItem.objects
            .prefetch_related("assets")
            .filter(pk=unit["staging_item_id"])
            .first()
        )
        if si is None:
            return [], []
        return [si], list(si.assets.all())
    
    def _baseline(self, unit):
        """The published normal (Published tier) for this slice's calendar slot,
        located by the slot's sentinel-year ``Item.time``."""
        from datetime import datetime, timezone
        
        from georiva.core.models import Item
        
        dt = datetime.fromisoformat(unit["valid_time"])
        join_time = slot_time(dt, unit["resolution"]).replace(tzinfo=timezone.utc)
        item = (
            Item.objects
            .prefetch_related("assets")
            .filter(collection__slug=self._climatology_slug(unit), time=join_time)
            .first()
        )
        if item is None:
            return [], []
        return [item], list(item.assets.all())
    
    @staticmethod
    def _climatology_slug(unit: ProductionUnit) -> str:
        b0, b1 = unit["baseline"]
        return f"{unit['source_collection']}-climatology-{b0}-{b1}"
    
    def outputs(self, unit: ProductionUnit) -> OutputItem:
        from datetime import datetime
        
        from georiva_source_chirps.periods import slot_index
        
        value_items, _ = self._value(unit)
        si = value_items[0]
        collection = self._published_collection(unit, si.collection.catalog)
        valid = datetime.fromisoformat(unit["valid_time"])
        return OutputItem(
            collection=collection,
            time=valid,
            bounds=si.bounds, crs=si.crs, width=si.width, height=si.height,
            properties={"anomaly": {
                "resolution": unit["resolution"], "quantity": unit["quantity"],
                "baseline": unit["baseline"], "valid_time": unit["valid_time"],
                "slot": slot_index(valid, unit["resolution"]),
            }},
        )
    
    def transform(self, unit: ProductionUnit, resolved) -> "list[OutputAsset]":
        from georiva.geoprocessing import anomaly
        
        value = self.read_value(resolved["value"].assets[0])
        normal = self.read_normal(resolved["baseline"].assets[0])
        relative = unit["quantity"] == "relative_anomaly"
        result = anomaly(value, normal, relative=relative)
        array = np.asarray(result, dtype="float32")
        
        si = resolved["value"].items[0]
        out_var = self._output_variable(unit, resolved)
        return [OutputAsset(
            variable=out_var, roles=["data"], format="cog",
            array=array, bounds=si.bounds, crs=si.crs,
            width=si.width, height=si.height,
            stats=array_stats(array),
        )]
    
    # ---- I/O seams (mocked in tests) ---------------------------------------
    
    def read_value(self, asset):
        """The arriving raw slice (Staging tier GeoTIFF) as a 2D array."""
        from georiva.core.storage import BucketType

        from georiva_source_chirps.recipes.io import read_band
        return read_band(asset, BucketType.STAGING)
    
    def read_normal(self, asset):
        """The published normal (Published assets-tier COG) as a 2D array."""
        from georiva.core.storage import BucketType

        from georiva_source_chirps.recipes.io import read_band
        return read_band(asset, BucketType.ASSETS)
    
    # ---- helpers ------------------------------------------------------------
    
    def _output_collection_slug(self, unit: ProductionUnit) -> str:
        b0, b1 = unit["baseline"]
        infix = "relative-anomaly" if unit["quantity"] == "relative_anomaly" else "anomaly"
        return f"{unit['source_collection']}-{infix}-{b0}-{b1}"
    
    def _published_collection(self, unit, catalog):
        from georiva.core.models import Collection
        
        slug = self._output_collection_slug(unit)
        collection, _ = Collection.objects.get_or_create(
            catalog=catalog, slug=slug, defaults={"name": slug},
        )
        return collection
    
    def _output_variable(self, unit, resolved):
        """Quantity-specific output Variable: absolute anomaly keeps the source
        unit on a symmetric range; relative anomaly is dimensionless on [-1, 1]."""
        from georiva.core.models import Variable
        
        si = resolved["value"].items[0]
        catalog = si.collection.catalog
        src = Variable.objects.filter(
            collection__catalog=catalog,
            collection__slug=unit["source_collection"],
        ).first()
        if src is None:
            raise ValueError(
                "ChirpsAnomalyRecipe: no source Variable in raw collection "
                f"'{unit['source_collection']}'"
            )
        collection = self._published_collection(unit, catalog)
        spec = self._variable_spec(src, unit["quantity"])
        out_var, _ = Variable.objects.get_or_create(
            collection=collection, slug=src.slug, defaults=spec,
        )
        return out_var
    
    def _variable_spec(self, src, quantity: str) -> dict:
        span = (src.value_max - src.value_min) / 2.0
        if quantity == "relative_anomaly":
            return {"name": f"{src.name} relative anomaly",
                    "unit": self._dimensionless_unit(),
                    "value_min": -1.0, "value_max": 1.0}
        return {"name": f"{src.name} anomaly", "unit": src.unit,
                "value_min": -span, "value_max": span}
    
    @staticmethod
    def _dimensionless_unit():
        from georiva.core.models import Unit
        
        unit, _ = Unit.objects.get_or_create(
            symbol="dimensionless", defaults={"name": "Dimensionless"},
        )
        return unit
