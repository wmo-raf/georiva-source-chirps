"""Shared derived-raster statistics for the CHIRPS recipes."""
from __future__ import annotations

import numpy as np


def array_stats(array) -> dict:
    """NaN-aware min/max/mean/std for a derived raster (empty if all-NaN)."""
    if not np.isfinite(array).any():
        return {}
    return {
        "min": float(np.nanmin(array)),
        "max": float(np.nanmax(array)),
        "mean": float(np.nanmean(array)),
        "std": float(np.nanstd(array)),
    }
