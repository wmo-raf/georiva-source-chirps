"""Shared CHIRPS derivation constants.

``CHIRPS_BASELINE`` is the single source of truth for the climatology window —
the WMO standard normal — used as the default by the scheduled climatology
recipe and read directly by the event-driven anomaly recipe to locate the
normal it joins. Keeping it in one place stops the two stages drifting to a slug
that was never built. See docs/adr/0007.
"""
from __future__ import annotations

# The WMO standard normal. (year_start, year_end), inclusive.
CHIRPS_BASELINE = (1991, 2020)

# A normal built from too few years is a weak reference everything downstream
# subtracts against, so a slot below this many contributing slices is skipped.
# The single source for the climatology product's min_count ConfigField default.
DEFAULT_MIN_COUNT = 20

# CHIRPS resolutions, as the trailing token of a source collection slug.
RESOLUTIONS = ("monthly", "dekadal", "pentadal")


def resolution_from_slug(slug: str) -> str:
    """Derive the CHIRPS resolution from a ``chirps-{resolution}`` slug."""
    res = slug.rsplit("-", 1)[-1]
    if res not in RESOLUTIONS:
        raise ValueError(
            f"cannot derive CHIRPS resolution from collection slug {slug!r}"
        )
    return res


# ---------------------------------------------------------------------------
# Collection-slug scheme — the single source of truth for every CHIRPS slug.
#
# get_derived_products() builds its InputRef/OutputRef declarations from these,
# and (because the invocation layer injects the declaration into the selector,
# ADR-0008 / georiva#161) the recipes read their collection slugs back from that
# declaration rather than reconstructing them — so the declaration and the
# recipes can never disagree.
# ---------------------------------------------------------------------------

def source_slug(resolution: str) -> str:
    """The raw CHIRPS collection slug for a resolution, e.g. ``chirps-monthly``."""
    return f"chirps-{resolution}"


def climatology_slug(resolution: str) -> str:
    """The per-slot normal (internal) collection slug, e.g.
    ``chirps-monthly-climatology``. Used by the climatology/anomaly products."""
    return f"chirps-{resolution}-climatology"


def anomaly_slug(resolution: str) -> str:
    """The absolute-anomaly collection slug, e.g. ``chirps-monthly-anomaly``."""
    return f"chirps-{resolution}-anomaly"


def relative_anomaly_slug(resolution: str) -> str:
    """The relative-anomaly collection slug, e.g.
    ``chirps-monthly-relative-anomaly``."""
    return f"chirps-{resolution}-relative-anomaly"
