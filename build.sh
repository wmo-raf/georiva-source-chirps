#!/bin/bash
set -euo pipefail
# Run by georiva when the plugin is built. No extra build steps required:
# this plugin's runtime dependencies (rasterio, requests) are already provided
# by GeoRiva core.
