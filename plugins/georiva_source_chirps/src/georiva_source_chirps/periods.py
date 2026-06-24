"""
Shared CHIRPS calendar-slot helpers.

A *calendar slot* is the position-within-the-year a CHIRPS period occupies:
month-of-year (1-12), dekad-of-year (1-36) or pentad-of-year (1-72). It is the
grain a rolling anomaly is computed within (June vs all Junes), and the single
home for slot math shared by the fetch path (``source.py``) and the derivation
recipes. See docs/adr/0007-chirps-rolling-anomaly-product-structure.md.
"""
from __future__ import annotations

from datetime import datetime

# Slots per year for each CHIRPS resolution.
_SLOT_COUNTS = {"monthly": 12, "dekadal": 36, "pentadal": 72}


def slot_count(resolution: str) -> int:
    """How many calendar slots a year holds at ``resolution`` (12/36/72)."""
    try:
        return _SLOT_COUNTS[resolution]
    except KeyError:
        raise ValueError(f"unknown resolution: {resolution!r}")


def dekad_of_month(dt: datetime) -> int:
    """Within-month dekad (1-3): days 1-10, 11-20, 21-end."""
    return 1 if dt.day <= 10 else 2 if dt.day <= 20 else 3


def pentad_of_month(dt: datetime) -> int:
    """Within-month pentad (1-6); the trailing pentad absorbs days 26-end."""
    return min(6, (dt.day - 1) // 5 + 1)


def slot_index(dt: datetime, resolution: str) -> int:
    """The 1-based calendar slot of ``dt`` within its year, for ``resolution``."""
    if resolution == "monthly":
        return dt.month
    if resolution == "dekadal":
        return (dt.month - 1) * 3 + dekad_of_month(dt)
    if resolution == "pentadal":
        return (dt.month - 1) * 6 + pentad_of_month(dt)
    raise ValueError(f"unknown resolution: {resolution!r}")


def slot_start(resolution: str, slot: int, sentinel_year: int) -> datetime:
    """The first date of calendar ``slot`` (1-based), placed in ``sentinel_year``.

    The inverse of :func:`slot_index`: monthly slots start on day 1; dekadal on
    day 1/11/21; pentadal on day 1/6/11/16/21/26. Used to encode a slot into a
    Published ``Item.time`` and to recover it.
    """
    if resolution == "monthly":
        return datetime(sentinel_year, slot, 1)
    if resolution == "dekadal":
        month, dekad_of_month = divmod(slot - 1, 3)
        return datetime(sentinel_year, month + 1, dekad_of_month * 10 + 1)
    if resolution == "pentadal":
        month, pentad_of_month = divmod(slot - 1, 6)
        return datetime(sentinel_year, month + 1, pentad_of_month * 5 + 1)
    raise ValueError(f"unknown resolution: {resolution!r}")


def slot_time(dt: datetime, resolution: str, sentinel_year: int = 1991) -> datetime:
    """Encode ``dt``'s calendar slot as a sentinel-year ``Item.time``.

    The single join key the pipeline shares: the climatology Item for a slot is
    keyed on ``slot_time`` of any slice in that slot, and an anomaly looks up its
    baseline normal by ``slot_time`` of the arriving slice. Collapses the real
    year so every same-slot slice maps to one time.
    """
    return slot_start(resolution, slot_index(dt, resolution), sentinel_year)
