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
