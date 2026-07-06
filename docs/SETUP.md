# Setup and Installation

This guide covers installing the QGIS Processing scripts, preparing the input
data, and running the pipeline for the first time. It assumes macOS with a
standard QGIS 3.44 install; adjust paths for Linux/Windows.

---

## 1. Prerequisites

Install (or confirm you have) the following, all reachable from QGIS Processing:

* **QGIS 3.44** (bundles Python 3.9+, GDAL, and Processing).
* **GRASS GIS 8.4** — used for `r.watershed` etc. in Step 1.
* **SAGA NextGen 9.11.3** — used by the hydrology step.
* Python packages `numpy` and `scipy` (ship with QGIS; no extra install needed
  for a standard QGIS bundle).

Confirm `qgis_process` and `gdalwarp` are on your `PATH`. On macOS with the
default install they live under:

```
/Applications/QGIS.app/Contents/MacOS
```

The driver script exports the macOS QGIS/GRASS/SAGA environment at the top of
`run_sota_pipeline.sh`. **On a non-macOS system, edit that environment block**
(the `export PROJ_LIB=…`, `GISBASE=…`, `PATH=…` lines) to match your install.

---

## 2. Install the Processing scripts

QGIS Processing scripts must be discoverable by the `qgis_process` engine under
the algorithm IDs used by the driver (e.g. `script:AT_SOTA_PixelMinimax`).

1. Find your QGIS Processing scripts folder. In QGIS:
   **Settings → Options → Processing → Scripts folder**, or typically:
   * macOS: `~/Library/Application Support/QGIS/QGIS3/profiles/default/processing/scripts`
   * Linux: `~/.local/share/QGIS/QGIS3/profiles/default/processing/scripts`
   * Windows: `%APPDATA%\QGIS\QGIS3\profiles\default\processing\scripts`

2. Copy the step scripts into that folder:

   ```bash
   cp scripts/AT_SOTA_*.py "<your-processing-scripts-folder>/"
   ```

   `find_routing_targets.py` is a **standalone** script run with `python3` — it
   does **not** need to go into the Processing folder, but it must be reachable
   from the repository (`scripts/find_routing_targets.py`), which the driver
   already expects.

3. Restart QGIS (or refresh the Processing Toolbox) and confirm the algorithms
   appear under the **SOTA** / **AT SOTA Pipeline** group.

> The algorithm ID comes from each script's `name()` method. The driver calls,
> for example, `script:AT_SOTA_SeamlessHydrology` — the hydrology file is named
> `AT_SOTA_SeamlessHydrology_TB5.py` but registers the expected algorithm name.

---

## 3. Prepare input data

Create a working directory and a `raw/` subfolder for the SOTA database CSV:

```bash
mkdir -p /Volumes/TB5_SSD/AT_SOTA_150m/raw
cp SOTA_2026.csv /Volumes/TB5_SSD/AT_SOTA_150m/raw/
```

Gather the geodata (see the README "Input data" section for provenance):

* National 1 m ALS DTM GeoTIFF  → `--als-1m`
* Copernicus 30 m DEM GeoTIFF   → `--copernicus-30m`
* Austria border GeoPackage     → `--austria-border`
* BEV Geonamen GeoPackage       → `--names-gpkg`
* Bundesländer GeoPackage       → `--bl-gpkg` (optional)

Put large scratch on a fast external SSD and pass it via `--tmpdir`. Allow at
least **40 GB** free (the hydrology step warns below this threshold).

---

## 4. First run

```bash
chmod +x run_sota_pipeline.sh

./run_sota_pipeline.sh \
  --workdir        /Volumes/TB5_SSD/AT_SOTA_150m \
  --austria-border /data/austria_border.gpkg \
  --als-1m         /data/AT_ALS_1m.tif \
  --copernicus-30m /data/COP30.tif \
  --names-gpkg     /data/BEV_Geonamen.gpkg \
  --bl-gpkg        /data/BL_202504.gpkg \
  --tmpdir         /Volumes/TB5_SSD/scratch \
  --threads        8
```

The driver prints a configuration banner, then runs each step, skipping any
whose output already exists. Final layers appear in
`<workdir>/results/`, and the key deliverable is
`AT_SOTA_Matched_Assigned.gpkg` (with `keycol_points.gpkg` and
`peak_to_col_lines.gpkg` for the key cols).

For a much faster first smoke test, restrict to a small region and skip the
audit-only hydrology:

```bash
./run_sota_pipeline.sh \
  --workdir        ~/AT_SOTA_test \
  --austria-border /data/austria_border.gpkg \
  --test-region    /data/vorarlberg.gpkg \
  --als-1m         /data/AT_ALS_1m.tif \
  --copernicus-30m /data/COP30.tif \
  --names-gpkg     /data/BEV_Geonamen.gpkg \
  --skip-step1
```

---

## 5. Resuming and re-running

* Every step **auto-skips** if its output already exists.
* Recompute one step with `--force-stepN` (this also forces downstream steps).
* Recompute everything with `--force-all`.

See **[PARAMETERS.md](PARAMETERS.md)** for the full flag reference and more
examples.

---

## 6. Troubleshooting

* **`Unknown argument`** — a flag was mistyped; run without arguments to print
  usage.
* **Step 5c aborts with "requires --names-gpkg"** — provide `--names-gpkg`; the
  coordinate validation needs the BEV-NAMEN reference.
* **Scratch-space warning** — free up space on the `--tmpdir` volume or point it
  at a larger SSD (40 GB+ recommended).
* **Algorithm not found (`script:AT_SOTA_…`)** — the script is not in the
  Processing scripts folder or QGIS has not refreshed; re-copy and restart QGIS.
* **GRASS/SAGA path errors on non-macOS** — edit the environment block at the
  top of `run_sota_pipeline.sh`.
