# Parameter Reference

This document lists every parameter of the orchestrator `run_sota_pipeline.sh`
and of each pipeline step, followed by practical examples.

Distances are in **metres** and elevations/differences in **metres** unless
noted otherwise.

---

## 1. Orchestrator: `run_sota_pipeline.sh`

### Required

| Flag | Description |
|------|-------------|
| `--workdir DIR` | Working directory. Holds `intermediate/`, `results/`, `tmp/`, and `raw/`. |
| `--austria-border GPKG` | The **true** national Austria boundary polygon. Always the real border. |
| `--als-1m TIF` | National 1 m ALS-derived DTM (GeoTIFF). Used in Step 0, 3, 5c. |
| `--copernicus-30m TIF` | Copernicus 30 m DEM used to fill the 40 km padding ring. |

### Optional — data & region

| Flag | Default | Description |
|------|---------|-------------|
| `--tmpdir DIR` | `<workdir>/tmp` | Explicit temp/scratch path (put on a fast external SSD). |
| `--names-gpkg GPKG` | *(none)* | BEV DLM Geonamen. Needed for names in Step 4 and **required** for Step 5c. |
| `--bl-gpkg GPKG` | *(none)* | Bundesländer (state) polygons for the state join. |
| `--test-region GPKG` | *(national run)* | Restrict the analysis to a smaller polygon (e.g. one state) for testing. The national border is still used for border logic. |

### Optional — performance

| Flag | Default | Description |
|------|---------|-------------|
| `--threads N` | `8` | Threads passed to `gdalwarp`. Performance only. |
| `--buffer-dist M` | `40000` | Padding buffer around the analysis polygon (40 km). Also used as neighbour radius in Step 5. |

### Optional — matching & diagnostics tuning

| Flag | Default | Consumed by | Description |
|------|---------|-------------|-------------|
| `--raw-peak-radius M` | `600` | 5, 4b | Search radius for raw-peak diagnostics / DB_NO_PEAK. |
| `--raw-peak-candidates N` | `20` | 5 | Max raw-peak candidates at that radius. |
| `--near-calc-radius M` | `1000` | 5 | Radius for near-calculated-peak diagnostics. |
| `--near-calc-candidates N` | `30` | 5 | Max candidates for the near-calc search. |
| `--db-no-peak-candidates N` | `20` | 4b | Max candidates checked for DB entries with no computed peak. |
| `--ambiguity-pair-radius M` (alias `--ambiguity-radius`) | `800` | 5b | Pairing radius for summit-ambiguity detection. |
| `--ambiguity-zdiff M` | `5` | 5b, 5d | Hard elevation-difference threshold for an ambiguous pair. |
| `--ambiguity-soft-zdiff M` | `10` | 5b | Soft (review-flag) elevation-difference threshold. |
| `--ambiguity-keycol-dist M` | `40` | 5b | Max key-col distance for a shared-keycol pairing. |
| `--match-elev-link-radius M` | `250` | 5b, 5d | Link radius between MATCH_ELEV points and official points. |
| `--coord-bev-radius M` | `100` | 5c | Search radius around BEV-NAMEN reference points. |
| `--coord-local-max-radius M` | `50` | 5c | Radius for the 1 m local-maximum check. |
| `--coord-fcodes LIST` | `7301,7302,7303` | 5c | BEV F_CODE feature classes used as name references. |

### Optional — control flow

| Flag | Description |
|------|-------------|
| `--skip-step1` | Skip hydrology; create empty dummy `peaks/basins/saddles_10m.gpkg`. Step 1 does not feed Step 2, so this is safe and faster. |
| `--only-step1` | Run **only** Step 0 (DEM) + Step 1 (hydrology), then stop. |
| `--force-step1 … --force-step5d`, `--force-step4b` | Recompute a specific step (deletes its output first). |
| `--force-all` | Recompute everything. |

**Force cascade.** Forcing a step also forces all steps downstream of it:
`step2 → 3,4,…`; `step4 → 5,5b,5c,5d,4b`; and so on. Forcing Step 1 does **not**
force downstream steps unless combined with `--force-step2` (because Step 1 is
audit-only), except when not using `--only-step1`.

> **Developer-only:** `--test-prefix STR` writes Step-2 outputs into parallel
> `intermediate_STR/ results_STR/ tmp_STR/` directories and runs a comparison
> after Step 2. Production data is never touched. Not needed for normal runs.

---

## 2. Per-step script parameters

The QGIS Processing scripts are invoked by the orchestrator via
`qgis_process run script:<Name> --<param>=<value>`. If you run a script
directly from the QGIS Toolbox, these are the field names you will see.

### Step 1 — `AT_SOTA_SeamlessHydrology_TB5.py` (`script:AT_SOTA_SeamlessHydrology`)

| Parameter | Description |
|-----------|-------------|
| `INPUT_DEM` | Padded 10 m DEM (raw, not yet filled). |
| `BORDER_POLY` | Austria / test-region border polygon. |
| `SCRATCH_DIR` | *(optional)* Scratch folder for large intermediates (e.g. TB5 SSD). Falls back to `<workdir>/tmp`. Warns if < 40 GB free. |
| `OUTPUT_PEAKS` / `OUTPUT_BASINS` / `OUTPUT_SADDLES` | Output audit layers. |

### Step 2 — `AT_SOTA_PixelMinimax.py` (`script:AT_SOTA_PixelMinimax`)

| Parameter | Orchestrator value | Description |
|-----------|--------------------|-------------|
| `input_dem` | `AT_10m_PADDED.tif` | Padded 10 m DEM. |
| `border_poly` | analysis polygon | Clip polygon. |
| `resolution` | `10` | Working resolution (m). |
| `min_prominence` | `130` | Minimum prominence to keep (m). 130 gives headroom below the 150 m certification threshold for the 1 m refinement band. |
| `output` | `peaks_prom_raw.gpkg` | Raw prominence peaks. |

Environment variables (set by the driver): `SOTA_TMPDIR`, `SOTA_PIXMEM_DIR`
(memmap scratch), `SOTA_SORT_CHUNK` (pixels per external-sort chunk, default
20 000 000; driver raises to 100 000 000). These control memory/performance
only — never the result.

### Step 2b — `find_routing_targets.py` (standalone, `python3`)

| Argument | Default | Description |
|----------|---------|-------------|
| `--input` | *(required)* | Input GeoPackage (`peaks_prom_raw.gpkg`). |
| `--output` | *(none)* | Output path; omit with `--inplace`. |
| `--layer` | `peaks_prom_raw` | Layer name. |
| `--search-km` | `500` | Search radius (km) for the higher routing target. |
| `--inplace` | off | Write target fields back into the input layer. |

### Step 3 — `AT_SOTA_Refine1m.py` (`script:AT_SOTA_Refine1m`)

| Parameter | Description |
|-----------|-------------|
| `INPUT_PEAKS` | Raw prominence peaks from Step 2. |
| `DEM_1M` | 1 m DTM for the dual refinement. |
| `OUTPUT_SOTA` | Refined SOTA-valid peaks. Only the ambiguous 130–170 m band is refined at 1 m. |

### Step 4 — `AT_SOTA_Join_Geonamen.py` (`script:AT_SOTA_Join_Geonamen`)

| Parameter | Orchestrator value | Description |
|-----------|--------------------|-------------|
| `peaks_layer` | refined peaks | Input. |
| `geonamen_gpkg_path` | `--names-gpkg` | BEV Geonamen GeoPackage. |
| `geonamen_layer_name` | BEV default | Layer name; defaults to the BEV DLM 2025 name layer. |
| `search_radius` | `30` | Name-match radius (m). |
| `bl_gpkg_path` | `--bl-gpkg` | *(optional)* Bundesländer polygons for `land`/`land_id`. |
| `border_gpkg_path` | national border | National border for border logic. |
| `output` | `AT_SOTA_Final.gpkg` | Named + state-joined peaks. |

Filters BEV F_CODE 7302 (Berggipfel) and 7303 (Hochpunkt).

### Step 5 — `AT_SOTA_Match_DB.py` (`script:AT_SOTA_Match_DB`)

| Parameter | Orchestrator value | Description |
|-----------|--------------------|-------------|
| `peaks_layer` | `AT_SOTA_Final.gpkg` | Input. |
| `sota_csv_path` | `<workdir>/raw/SOTA_2026.csv` | SOTA Austria reference DB (CSV). |
| `match_radius` | `500` | Nearest-DB-entry match radius (m). |
| `neighbor_radius` | `--buffer-dist` (40000) | Border-buffer neighbour radius. |
| `border_gpkg` | national border | For FOREIGN_PEAK / border zone. |
| `bl_gpkg_path` | `--bl-gpkg` | *(optional)* State attribution for DB_NO_PEAK. |
| `raw_peaks_layer` | `peaks_prom_raw.gpkg` | For raw-peak diagnostics. |
| `raw_peak_radius` / `raw_peak_candidates` | `600` / `20` | DB_NO_PEAK raw-peak search. |
| `near_calc_radius` / `near_calc_candidates` | `1000` / `30` | Near-calculated diagnostics. |
| `output` | `AT_SOTA_Matched.gpkg` | Matched result. Status: MATCH_OK / MATCH_ELEV / NEW_CALC / DB_NO_PEAK / FOREIGN_PEAK. |

### Step 5b — `AT_SOTA_Ambiguity_Diagnosis.py` (`script:AT_SOTA_Ambiguity_Diagnosis`)

| Parameter | Orchestrator value |
|-----------|--------------------|
| `matched_layer` | `AT_SOTA_Matched.gpkg` |
| `ambiguity_pair_radius` | `800` |
| `ambiguity_zdiff` | `5` |
| `ambiguity_soft_zdiff` | `10` |
| `ambiguity_keycol_dist` | `40` |
| `match_elev_link_radius` | `250` |
| `output` / `output_official_points` / `output_links` | diagnosed layer + official points + links |

### Step 5c — `AT_SOTA_Coordinate_Validation.py` (`script:AT_SOTA_Coordinate_Validation`)

| Parameter | Orchestrator value | Description |
|-----------|--------------------|-------------|
| `assigned_layer` | `AT_SOTA_Matched_Diagnosed.gpkg` | Input. |
| `dem_1m` | `--als-1m` | 1 m DEM evidence. |
| `names_gpkg_path` | `--names-gpkg` | **Required.** BEV-NAMEN reference. |
| `names_layer_name` | `NAM_7300_GELAENDEFORM_P_20250325` | BEV name layer. |
| `f_codes` | `7301,7302,7303` | Feature classes used as references. |
| `bev_search_radius` | `100` | BEV-NAMEN search radius (m). |
| `local_max_radius` | `50` | 1 m local-maximum check radius (m). |
| `output` / `output_issues` | validated layer + issues layer |

> Step 5c aborts if `--names-gpkg` is not provided.

### Step 5d — `AT_SOTA_Final_Assignment.py` (`script:AT_SOTA_Final_Assignment`)

| Parameter | Orchestrator value | Description |
|-----------|--------------------|-------------|
| `matched_layer` | `AT_SOTA_Matched_CoordValidated.gpkg` | Input. |
| `pair_zdiff` | `--ambiguity-zdiff` (5) | Pair elevation-difference threshold. |
| `exact_official_dist` | `15` | Exact-on-official distance (m). |
| `near_official_dist` | `100` | Near-official distance (m). |
| `replay_official_dist` | `--match-elev-link-radius` (250) | Replay link distance (m). |
| `output` | `AT_SOTA_Matched_Assigned.gpkg` | Coordinate-aware final assignment. |

### Step 4b — `AT_SOTA_Export_Keycol.py` (`script:AT_SOTA_Export_Keycol`)

| Parameter | Orchestrator value | Description |
|-----------|--------------------|-------------|
| `matched_layer` | `AT_SOTA_Matched_Assigned.gpkg` | Assigned peaks. |
| `peaks_layer` | `AT_SOTA_Final.gpkg` | Named peaks. |
| `peaks_raw` | `peaks_prom_raw.gpkg` | Raw peaks. |
| `db_no_peak_radius` | `--raw-peak-radius` (600) | DB_NO_PEAK search radius (m). |
| `db_no_peak_candidates` | `--db-no-peak-candidates` (20) | Max candidates. |
| `output_points` / `output_lines` | `keycol_points.gpkg` + `peak_to_col_lines.gpkg` |

---

## 3. Practical examples

### A. Full national run

```bash
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

### B. Fast local test on one state (e.g. Vorarlberg)

Restrict the heavy Step 1/2 compute to a test region while keeping national
border logic. Skip the audit-only hydrology to save time.

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

### C. Only build the DEM and hydrology audit layers

```bash
./run_sota_pipeline.sh \
  --workdir        ~/AT_SOTA \
  --austria-border /data/austria_border.gpkg \
  --als-1m         /data/AT_ALS_1m.tif \
  --copernicus-30m /data/COP30.tif \
  --only-step1
```

### D. Re-run just the matching step after updating the SOTA CSV

Steps 0–4 are cached; recompute Step 5 and everything after it.

```bash
./run_sota_pipeline.sh \
  --workdir        ~/AT_SOTA \
  --austria-border /data/austria_border.gpkg \
  --als-1m         /data/AT_ALS_1m.tif \
  --copernicus-30m /data/COP30.tif \
  --names-gpkg     /data/BEV_Geonamen.gpkg \
  --force-step5
```

### E. Tighten ambiguity detection

Use a smaller pairing radius and a stricter elevation-difference threshold, then
recompute the diagnosis and downstream assignment.

```bash
./run_sota_pipeline.sh \
  --workdir        ~/AT_SOTA \
  --austria-border /data/austria_border.gpkg \
  --als-1m         /data/AT_ALS_1m.tif \
  --copernicus-30m /data/COP30.tif \
  --names-gpkg     /data/BEV_Geonamen.gpkg \
  --ambiguity-pair-radius 500 \
  --ambiguity-zdiff 3 \
  --force-step5b
```

### F. Run the routing post-processing standalone

```bash
python3 scripts/find_routing_targets.py \
  --input ~/AT_SOTA/intermediate/peaks_prom_raw.gpkg \
  --search-km 500 \
  --inplace
```
