# GeoRiva CHIRPS

A [GeoRiva](https://github.com/wmo-raf/georiva) source plugin for
**CHIRPS v2.0** rainfall estimates (UCSB Climate Hazards Center).

It ships:

- **`CHIRPSDataSource`** — generates download requests against the public CHC
  file server (`https://data.chc.ucsb.edu/products/CHIRPS-2.0`) for the Africa
  domain, over plain HTTPS (`HTTPFetchStrategy`). It probes the server to find
  the latest available period and gunzips each `*.tif.gz` into a GeoTIFF in
  `post_process_fetched_file`, patching the CHIRPS `-9999` nodata value.
- **`CHIRPSDataFeed`** — a DataFeed exposing three collections: monthly,
  dekadal (10-day) and pentadal (5-day) precipitation. The period is baked into
  each collection's definition key; the operator picks which to ingest in the
  setup wizard.

## Data model

| Concept | Maps to |
| --- | --- |
| Collection | one *period* — CHIRPS Monthly / Dekadal / Pentadal |
| DataFeed | the acquisition (HTTP HEAD timeout + per-collection start date) |
| Variable | `precip` — precipitation (mm), read from band 1 of the GeoTIFF |

The period is derived from the collection definition key, so it is never shown
as an editable field; each collection link carries its own backfill start date
(default 1981-01-01, the start of the CHIRPS record).

## No credentials required

CHIRPS is served from an open HTTP file server — there is nothing to authenticate.
The only feed-level knob is the HTTP HEAD timeout used for URL existence checks.

## Install

This plugin installs into a running GeoRiva instance — it is a Python package,
not a standalone service. It needs no environment variables (`requires_env` is
empty).

- **Production:** declare it in the operator's `plugins.toml`
  (`git = "https://github.com/wmo-raf/georiva-source-chirps.git"`, with a release
  `tag`), rebuild, and run migrations.
- **Development:** bind-mount the package into the core GeoRiva dev stack — add
  `../plugins/georiva-source-chirps/plugins/georiva_source_chirps:/georiva/dev-plugins/georiva_source_chirps`
  to the core repo's `docker-compose.override.yml` (see its
  `docker-compose.override.sample.yml`), then `make dev-up OV=1` and
  `make dev-makemigrations && make dev-migrate`.

Then in the GeoRiva admin, open **Automated Sources → Set up wizard**, choose
**CHIRPS Data Feed**, and select the collections (monthly / dekadal / pentadal)
to provision. The feed advances from the latest stored period on each run, so it
can be scheduled or run once as a backfill.
