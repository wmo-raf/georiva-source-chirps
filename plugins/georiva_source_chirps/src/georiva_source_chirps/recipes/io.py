"""Raster I/O for the CHIRPS recipes — read a single-band GeoTIFF/COG to a 2D array.

Uses ``rasterio`` directly (a core GeoRiva dependency) rather than rioxarray, and
maps the band's nodata to NaN so downstream means/anomalies skip it (CHIRPS uses
-9999). This is the recipes' real I/O; the recipes expose it behind their own
``read_series``/``read_value``/``read_normal`` seams, which tests mock.
"""
from __future__ import annotations

import numpy as np
import rasterio

from georiva.core.storage import storage


def read_band(asset, bucket_type) -> np.ndarray:
    """Read band 1 of ``asset`` (in ``bucket_type``) as a float32 2D array,
    with nodata mapped to NaN."""
    data = storage.bucket(bucket_type).read_bytes(asset.href)
    with rasterio.MemoryFile(data) as memfile, memfile.open() as ds:
        band = ds.read(1).astype("float32")
        nodata = ds.nodata
    if nodata is not None and not np.isnan(nodata):
        band = np.where(band == nodata, np.nan, band)
    return band
