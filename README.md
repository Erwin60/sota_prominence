# AT-SOTA Prominence Pipeline

A divide-consistent, seamless QGIS/GRASS/SAGA workflow that computes national
topographic **prominence** directly on a raster digital terrain model (DTM) and
certifies **Summits on the Air (SOTA)** candidates for Austria at the
**150 m** prominence threshold.

The core contribution is **PixelMinimax**, a pixel-level descending Union-Find
algorithm. DEM pixels are processed from highest to lowest elevation, and the
key col of each summit is assigned at the moment its connected component first
merges with a higher-elevation component. This first merge occurs exactly at the
key-col elevation, so the method needs neither an intermediate basin–saddle
graph nor parameter tuning, and it correctly handles mountain-to-plain
transition zones. The computation is `O(n log n)` and runs at 10 m resolution,
followed by a dual 1 m refinement of summit and key-col elevations for
candidates inside a conservative ±20 m band around the threshold.

Applied to the national Austrian ALS-derived DEM (≈2.45 billion pixels), the
workflow identifies **2433 qualifying summits** and agrees with **95.4 %** of
the official SOTA Austria database in cross-comparison.

This repository accompanies the paper:

> E. Grabler, *A Divide-Consistent, Seamless QGIS Workflow for SOTA Prominence
> (150 m) in Austria.*

---

## What is in this repository

| Path | Purpose |
|------|---------|
| `run_sota_pipeline.sh` | Orchestrator. Runs Step 0 through Step 5d (+4b) with skip/force logic. |
| `scripts/` | The QGIS Processing scripts and one standalone post-processing script. |
| `docs/PARAMETERS.md` | Full reference of every command-line and per-step parameter, with examples. |
| `docs/SETUP.md` | Step-by-step environment setup and a worked first run. |
| `paper/` | The paper (PDF) documenting the method, data, and validation. |

### Pipeline scripts

The orchestrator invokes exactly the following scripts. These, and only these,
are published here (they are the ones referenced by the driver script):

| Step | Script | Role |
|------|--------|------|
| 1 | `scripts/AT_SOTA_SeamlessHydrology_TB5.py` | Seamless hydrology (peaks / basins / saddles) — auditability layers. |
| 2 | `scripts/AT_SOTA_PixelMinimax.py` | **PixelMinimax** descending Union-Find prominence. |
| 2b | `scripts/find_routing_targets.py` | Standalone post-processing: routing target peak per summit. |
| 3 | `scripts/AT_SOTA_Refine1m.py` | Dual 1 m refinement of summit and key-col elevations. |
| 4 | `scripts/AT_SOTA_Join_Geonamen.py` | BEV Geonamen + Bundesland (state) join. |
| 5 | `scripts/AT_SOTA_Match_DB.py` | Match against the SOTA Austria database. |
| 5b | `scripts/AT_SOTA_Ambiguity_Diagnosis.py` | Summit-ambiguity diagnosis. |
| 5c | `scripts/AT_SOTA_Coordinate_Validation.py` | Coordinate and summit-identity validation (1 m DEM + BEV names). |
| 5d | `scripts/AT_SOTA_Final_Assignment.py` | Coordinate-aware final assignment. |
| 4b | `scripts/AT_SOTA_Export_Keycol.py` | Key-col export (points + peak-to-col lines). |

> **Note.** Step 0 (DEM mosaicking and 40 km padding) is performed inline in
> `run_sota_pipeline.sh` with `gdalwarp`; there is no separate Step 0 script.
> Step 5e (replay / manual worklist backlog) is a conceptual downstream step and
> requires no additional compute in the batch driver.

---

## Pipeline overview

```
Step 0   DEM mosaic + 40 km padding (gdalwarp, inline)        -> AT_10m_PADDED.tif
Step 1   Seamless hydrology (audit layers)                    -> peaks/basins/saddles_10m.gpkg
Step 2   PixelMinimax prominence (Union-Find)                 -> peaks_prom_raw.gpkg
Step 2b  Routing target peaks                                 -> (in-place fields)
Step 3   1 m refinement (ambiguous 130-170 m band)            -> peaks_sota_valid.gpkg
Step 4   Geonamen + Bundesland join                           -> AT_SOTA_Final.gpkg
Step 5   SOTA-DB match                                        -> AT_SOTA_Matched.gpkg
Step 5b  Ambiguity diagnosis                                  -> AT_SOTA_Matched_Diagnosed.gpkg
Step 5c  Coordinate validation                                -> AT_SOTA_Matched_CoordValidated.gpkg
Step 5d  Final assignment (coordinate-aware)                  -> AT_SOTA_Matched_Assigned.gpkg
Step 4b  Key-col export                                       -> keycol_points.gpkg, peak_to_col_lines.gpkg
```

Each step **skips automatically** if its output already exists. Use `--force-stepN`
to recompute; a forced step also forces everything downstream of it.

---

## Requirements

* **QGIS 3.44** (tested) with the bundled Python, GDAL, GRASS **8.4**, and
  SAGA NextGen **9.11.3**. The scripts call GRASS and SAGA algorithms through
  QGIS Processing.
* Python **3.9+** (the QGIS-bundled interpreter). External Python packages used:
  `numpy`, `scipy` (both ship with a standard QGIS install).
* Developed and run on **macOS** (Apple Silicon). The environment block in
  `run_sota_pipeline.sh` sets macOS QGIS/GRASS paths; adjust for Linux/Windows.
* Substantial disk and RAM. The national run processes ≈2.45 billion pixels;
  large intermediates are memory-mapped to an external SSD (see `--tmpdir` and
  `SCRATCH_DIR`). Allow **40 GB+** free scratch space.

### Input data (not distributed here)

You must supply the following data yourself. In the paper these come from the
Austrian Federal Office of Metrology and Surveying (BEV):

* `--als-1m` — national 1 m ALS-derived DTM (GeoTIFF).
* `--copernicus-30m` — Copernicus 30 m DEM used to fill the 40 km padding ring.
* `--austria-border` — the national Austria boundary polygon (GeoPackage).
* `--names-gpkg` — BEV DLM Geonamen (for Step 4 and Step 5c). *Required for 5c.*
* `--bl-gpkg` — Bundesländer (state) polygons (optional).
* `raw/SOTA_2026.csv` — the SOTA Austria reference database, placed under
  `<workdir>/raw/`.

---

## Quick start

```bash
# 1. Install the QGIS Processing scripts (see docs/SETUP.md for the exact folder)
#    Copy scripts/*.py into your QGIS "processing/scripts" directory, or point
#    QGIS at this scripts/ folder.

# 2. Make the driver executable
chmod +x run_sota_pipeline.sh

# 3. Run the full national pipeline
./run_sota_pipeline.sh \
  --workdir      /Volumes/TB5_SSD/AT_SOTA_150m \
  --austria-border /data/austria_border.gpkg \
  --als-1m       /data/AT_ALS_1m.tif \
  --copernicus-30m /data/COP30.tif \
  --names-gpkg   /data/BEV_Geonamen.gpkg \
  --bl-gpkg      /data/BL_202504.gpkg \
  --tmpdir       /Volumes/TB5_SSD/scratch \
  --threads      8
```

See **[docs/PARAMETERS.md](docs/PARAMETERS.md)** for every parameter and more
worked examples (local test region, resuming a run, re-running a single step).

---

## Reproducibility notes

* Step 1 (hydrology) produces **audit layers only** and does not feed Step 2;
  PixelMinimax reads the DEM directly. You may `--skip-step1` for a faster run.
* The prominence computation is deterministic. Optimisation parameters
  (`--threads`, `--tmpdir`, sort-chunk size) affect performance and memory only,
  never the result.
* Outputs land in `<workdir>/results/`; intermediates in `<workdir>/intermediate/`.

---

## Code status and scope

This code grew out of the practical development of the AT-SOTA project rather
than as a polished software library. The individual scripts and the overall
workflow reflect that development history: they were shaped step by step around
the concrete needs of processing Austria, and they are provided **as is**.

There is clear room for refactoring and optimisation — tighter module
boundaries, removal of development-time scaffolding, and performance tuning
would all be possible in a revised version. This was a deliberate trade-off,
not an oversight: for any given country the full national run is normally a
**one-time** process, so the effort of hardening the code into a reusable
package was not justified relative to its benefit. The workflow is documented
and reproducible, and it does what the paper describes; it is simply not
optimised as a general-purpose tool.

Anyone adapting it to another country or dataset should expect to read and
adjust the scripts rather than treat them as a turnkey package, and is welcome
to improve on the structure.

## License

Released under the **MIT License** — see [LICENSE](LICENSE). The input geodata
(BEV ALS DEM, VGD boundaries, DLM Geonamen) and the SOTA database are **not**
covered by this license and remain subject to their respective providers' terms.

## Citation

If you use this workflow, please cite both the software and the accompanying
paper. The paper PDF is included in [`paper/`](paper/), and machine-readable
citation metadata is in [`CITATION.cff`](CITATION.cff) (GitHub shows a "Cite
this repository" button from it). An archival DOI is minted on Zenodo when a
release is published; the resulting archival DOI is then recorded here and in the paper.

## Author

Erwin Grabler — independent researcher, Vienna, Austria
(callsign OE1EKG). Contact: erwin.grabler@artcom.cc
