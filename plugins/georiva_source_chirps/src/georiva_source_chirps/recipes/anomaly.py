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

from georiva_source_chirps.constants import resolution_from_slug
from georiva_source_chirps.periods import slot_time
from georiva_source_chirps.recipes.stats import array_stats

# The two quantities every arriving slice produces, mapped to the output role
# each is published under in the product declaration.
QUANTITY_OUTPUT_ROLE = {
    "anomaly": "anomaly",
    "relative_anomaly": "relative-anomaly",
}
QUANTITIES = tuple(QUANTITY_OUTPUT_ROLE)


def _value_collection(selector: dict) -> str:
    """The raw staging collection slug from the product's declared inputs."""
    for ref in selector.get("inputs", []):
        if ref.get("tier") == "staging":
            return ref["collection"]
    raise ValueError(
        "ChirpsAnomalyRecipe: selector has no staging (value) input collection "
        "(expected the injected product declaration, ADR-0008)"
    )


def _baseline_collection(selector: dict) -> str:
    """The published climatology collection slug from the declared inputs."""
    for ref in selector.get("inputs", []):
        if ref.get("tier") == "published":
            return ref["collection"]
    raise ValueError(
        "ChirpsAnomalyRecipe: selector has no published (baseline) input "
        "collection (expected the injected product declaration, ADR-0008)"
    )


def _output_for_quantity(selector: dict, quantity: str) -> str:
    """The output collection slug for a quantity, by its declared output role."""
    role = QUANTITY_OUTPUT_ROLE[quantity]
    for ref in selector.get("outputs", []):
        if ref.get("role") == role:
            return ref["collection"]
    raise ValueError(
        f"ChirpsAnomalyRecipe: selector has no output for role '{role}' "
        "(expected the injected product declaration, ADR-0008)"
    )


@RecipeRegistry.register
class ChirpsAnomalyRecipe(BaseRecipe):
    type = "chirps-anomaly"
    version = "1"
    
    def candidate_units(self, trigger) -> Iterable[ProductionUnit]:
        """Staging-value arrivals only. Published-item triggers (promotion
        outputs, climatology outputs, our own outputs) are ignored so the
        anomaly never double-fires or mis-reads a non-value input. Source,
        baseline and output collections are read from the injected product
        declaration in the selector (ADR-0008), never reconstructed."""
        selector = trigger or {}
        if "published_item_id" in selector:
            return []
        if "staging_item_id" in selector:
            return list(
                self._units_for_staging_item(selector, selector["staging_item_id"])
            )
        return self.enumerate_units(selector)

    def _units_for_staging_item(self, selector, staging_item_id) -> Iterable[ProductionUnit]:
        from georiva.staging.models import StagingItem

        si = (
            StagingItem.objects
            .select_related("collection")
            .filter(pk=staging_item_id)
            .first()
        )
        if si is None or si.datetime is None:
            return
        for unit in self._units_for_slice(selector, si):
            yield unit

    def _units_for_slice(self, selector, si) -> Iterable[ProductionUnit]:
        source = _value_collection(selector)
        baseline_collection = _baseline_collection(selector)
        resolution = resolution_from_slug(source)
        for quantity in QUANTITIES:
            yield {
                "staging_item_id": si.pk,
                "source_collection": source,
                "baseline_collection": baseline_collection,
                "output_collection": _output_for_quantity(selector, quantity),
                "resolution": resolution,
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
        source = _value_collection(selector)
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
            for unit in self._units_for_slice(selector, si):
                yield unit
    
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
        located in the declared climatology collection by the slot's
        sentinel-year ``Item.time``."""
        from datetime import datetime, timezone

        from georiva.core.models import Item

        dt = datetime.fromisoformat(unit["valid_time"])
        join_time = slot_time(dt, unit["resolution"]).replace(tzinfo=timezone.utc)
        item = (
            Item.objects
            .prefetch_related("assets")
            .filter(collection__slug=unit["baseline_collection"], time=join_time)
            .first()
        )
        if item is None:
            return [], []
        return [item], list(item.assets.all())

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
                "valid_time": unit["valid_time"],
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
    
    def _published_collection(self, unit, catalog):
        # The output collection comes from the injected product declaration (one
        # per quantity; no baseline years in the slug). The recipe builds no slug
        # of its own (ADR-0008).
        from georiva.core.models import Collection

        slug = unit["output_collection"]
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
