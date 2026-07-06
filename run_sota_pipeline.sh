#!/usr/bin/env bash
# Filename: run_sota_pipeline_seamless.sh
# Version:  5.0b
# CHANGES vs 4.0:
#   - nutzt explizit WORK_DIR/tmp als Scratch-Bereich
#   - setzt TMPDIR / SOTA_TMPDIR / SOTA_PIXMEM_DIR für PixelMinimax
#   - gdalwarp nutzt den gewünschten Thread-Wert statt ALL_CPUS
#   - Speicherlayout für Step 2 optimiert, Rechenlogik unverändert
#   - QGIS macOS Pfadinitialisierung korrigiert (qgis_process / Python-Lib)
#   - --skip-step1: Step 1 bewusst überspringen und Dummy-Dateien anlegen
#   - --tmpdir: expliziter Temp-/Scratch-Pfad
#   - Rasterstabile Step-0-Variante mit -tap
#   - Saubere Trennung zwischen nationaler Grenze und lokaler Testregion:
#       * NATIONAL_BORDER_GPKG = echte Österreich-Grenze
#       * ANALYSIS_POLY        = Testregion oder nationale Grenze
#   - Step 1/2 arbeiten auf ANALYSIS_POLY
#   - Step 4/5 referenzieren weiterhin NATIONAL_BORDER_GPKG
#   - Step 4 schreibt jetzt zusätzlich Bundesland-Grenzkontext (BL-Grenze)
#   - Step 5 erhält optional BL_GPKG für amtlichen Bundesland-Join
#   - Step 5 schreibt Roh-Peak- und Grenzdiagnostik direkt in AT_SOTA_Matched
#   - Step 4b übernimmt diese Diagnosefelder in keycol_points / peak_to_col_lines
#   - Step 4b prüft konfigurierbar mehr Roh-Peak-Kandidaten bei gleichem Radius
#   - Step 5c ergänzt 1-m-DEM-basierte Koordinaten- und Gipfelidentitätsprüfung
#   - Step 5d ist nun das coordinate-aware Final Assignment
#   - Der frühere Replay-/Worklist-Backlog wird konzeptionell Step 5e

set -Eeuo pipefail

# ============================================================
# macOS ENVIRONMENT
# ============================================================
export PROJ_LIB="/Applications/QGIS.app/Contents/Resources/qgis/proj"
export PROJ_DATA="/Applications/QGIS.app/Contents/Resources/qgis/proj"
export GDAL_DATA="/Applications/QGIS.app/Contents/Resources/qgis/gdal"
export GRASS_PREFIX="/Applications/GRASS-8.4.app/Contents/Resources"
export GISBASE="/Applications/GRASS-8.4.app/Contents/Resources"
export PATH="/Applications/QGIS.app/Contents/MacOS:/Applications/SAGA.app/Contents/MacOS:$PATH"

# ============================================================
# DEFAULTS
# ============================================================
WORK_DIR=""
TMP_DIR=""
BORDER_GPKG=""
ALS_1M=""
COPERNICUS_30M=""
NAMES_GPKG=""
BL_GPKG=""
TEST_REGION=""
THREADS=8
TGT_EPSG="25833"
BUFFER_DIST=40000
SORT_CHUNK=100000000  # O2: 20M→100M — weniger Sort-Runs (122→24), schnellerer Merge-Heap
RAW_PEAK_RADIUS=600
RAW_PEAK_CANDIDATES=20
NEAR_CALC_RADIUS=1000
NEAR_CALC_CANDIDATES=30
DB_NO_PEAK_CANDIDATES=20
AMBIGUITY_PAIR_RADIUS=800
AMBIGUITY_ZDIFF=5
AMBIGUITY_SOFT_ZDIFF=10
AMBIGUITY_KEYCOL_DIST=40
MATCH_ELEV_LINK_RADIUS=250
COORD_BEV_RADIUS=100
COORD_LOCAL_MAX_RADIUS=50
COORD_FCODES="7301,7302,7303"

FORCE_S1=0; FORCE_S2=0; FORCE_S3=0; FORCE_S4=0; FORCE_S4B=0; FORCE_S5=0; FORCE_S5B=0; FORCE_S5C=0; FORCE_S5D=0
SKIP_S1=0
ONLY_S1=0
TEST_PREFIX=""   # Undokumentierter Entwicklungsparameter — parallel dirs für Step-2-Vergleichstest

# ============================================================
# ARGUMENT PARSING
# ============================================================
usage() {
    echo "Usage: $0 --workdir DIR --austria-border GPKG --als-1m TIF --copernicus-30m TIF"
    echo "          [--tmpdir DIR] [--names-gpkg GPKG] [--bl-gpkg GPKG]"
    echo "          [--test-region GPKG] [--threads N] [--buffer-dist M]"
    echo "          [--raw-peak-radius M] [--raw-peak-candidates N] [--near-calc-radius M] [--near-calc-candidates N]"
echo "          [--db-no-peak-candidates N] [--ambiguity-pair-radius M] [--ambiguity-zdiff M] [--ambiguity-soft-zdiff M]"
echo "          [--ambiguity-keycol-dist M] [--match-elev-link-radius M]"
    echo "          [--coord-bev-radius M] [--coord-local-max-radius M] [--coord-fcodes LIST]"
    echo "          [--skip-step1]"
    echo "          [--only-step1]"
    echo "          [--force-step1] [--force-step2] [--force-step3] [--force-step4] [--force-step4b] [--force-step5] [--force-step5b] [--force-step5c] [--force-step5d]"
    echo "          Bedeutungen: --force-step5c = Coordinate Validation; --force-step5d = Final Assignment"
    echo "          [--force-all]"
    echo ""
    echo "--austria-border muss immer die echte Österreich-Grenze sein."
    echo "--test-region ist optional für lokale Läufe (z.B. Vorarlberg)."
    echo "--skip-step1 legt Dummy-Dateien für peaks_10m/basins_10m/saddles_10m an.
--only-step1  führt NUR Step 0 (DEM) + Step 1 (Hydrologie) aus und beendet dann die Pipeline.
--test-prefix STR  [Entwicklung] Schreibt Step-2-Outputs in Parallelverzeichnisse
                   intermediate_STR / results_STR / tmp_STR. Produktionsdaten
                   (intermediate/, results/) werden nie berührt.
              Nützlich für isolierten Review-Lauf der Auditability-Zwischenergebnisse.
              Step 1 beeinflusst Steps 2–5d/4b nicht (Step 2 nutzt direkt das DEM)."
    exit 1
}

while [[ $# -gt 0 ]]; do
    case "$1" in
        --workdir)        WORK_DIR="$2";        shift 2 ;;
        --tmpdir)         TMP_DIR="$2";         shift 2 ;;
        --austria-border) BORDER_GPKG="$2";     shift 2 ;;
        --als-1m)         ALS_1M="$2";          shift 2 ;;
        --copernicus-30m) COPERNICUS_30M="$2";  shift 2 ;;
        --names-gpkg)     NAMES_GPKG="$2";      shift 2 ;;
        --bl-gpkg)        BL_GPKG="$2";         shift 2 ;;
        --test-region)    TEST_REGION="$2";     shift 2 ;;
        --threads)        THREADS="$2";         shift 2 ;;
        --buffer-dist)    BUFFER_DIST="$2";     shift 2 ;;
        --raw-peak-radius) RAW_PEAK_RADIUS="$2"; shift 2 ;;
        --raw-peak-candidates) RAW_PEAK_CANDIDATES="$2"; shift 2 ;;
        --near-calc-radius) NEAR_CALC_RADIUS="$2"; shift 2 ;;
        --near-calc-candidates) NEAR_CALC_CANDIDATES="$2"; shift 2 ;;
        --db-no-peak-candidates) DB_NO_PEAK_CANDIDATES="$2"; shift 2 ;;
        --ambiguity-pair-radius|--ambiguity-radius) AMBIGUITY_PAIR_RADIUS="$2"; shift 2 ;;
        --ambiguity-zdiff) AMBIGUITY_ZDIFF="$2"; shift 2 ;;
        --ambiguity-soft-zdiff) AMBIGUITY_SOFT_ZDIFF="$2"; shift 2 ;;
        --ambiguity-keycol-dist) AMBIGUITY_KEYCOL_DIST="$2"; shift 2 ;;
        --match-elev-link-radius) MATCH_ELEV_LINK_RADIUS="$2"; shift 2 ;;
        --coord-bev-radius) COORD_BEV_RADIUS="$2"; shift 2 ;;
        --coord-local-max-radius) COORD_LOCAL_MAX_RADIUS="$2"; shift 2 ;;
        --coord-fcodes) COORD_FCODES="$2"; shift 2 ;;
        --skip-step1)     SKIP_S1=1;            shift 1 ;;
        --only-step1)     ONLY_S1=1;            shift 1 ;;
        --test-prefix)    TEST_PREFIX="$2";     shift 2 ;;
        --force-step1)    FORCE_S1=1;           shift 1 ;;
        --force-step2)    FORCE_S2=1;           shift 1 ;;
        --force-step3)    FORCE_S3=1;           shift 1 ;;
        --force-step4)    FORCE_S4=1;           shift 1 ;;
        --force-step4b)   FORCE_S4B=1;          shift 1 ;;
        --force-step5)    FORCE_S5=1;           shift 1 ;;
        --force-step5b)   FORCE_S5B=1;          shift 1 ;;
        --force-step5c)   FORCE_S5C=1;          shift 1 ;;
        --force-step5d)   FORCE_S5D=1;          shift 1 ;;
        --force-all)      FORCE_S1=1; FORCE_S2=1; FORCE_S3=1; FORCE_S4=1; FORCE_S4B=1; FORCE_S5=1; FORCE_S5B=1; FORCE_S5C=1; FORCE_S5D=1; shift 1 ;;
        *) echo "Unknown argument: $1"; usage ;;
    esac
done

[[ -z "$WORK_DIR"       ]] && { echo "ERROR: --workdir is required.";        usage; }
[[ -z "$BORDER_GPKG"    ]] && { echo "ERROR: --austria-border is required."; usage; }
[[ -z "$ALS_1M"         ]] && { echo "ERROR: --als-1m is required.";         usage; }
[[ -z "$COPERNICUS_30M" ]] && { echo "ERROR: --copernicus-30m is required."; usage; }

# Abhängigkeitskette
[[ "$FORCE_S1" -eq 1 && "$ONLY_S1" -eq 0 ]] && { FORCE_S2=1; FORCE_S3=1; FORCE_S4=1; }  # Kein Downstream-Force wenn --only-step1
[[ "$FORCE_S2" -eq 1 ]] && { FORCE_S3=1; FORCE_S4=1; }
[[ "$FORCE_S3" -eq 1 ]] && { FORCE_S4=1; }
[[ "$FORCE_S4" -eq 1 ]] && { FORCE_S5=1; FORCE_S5B=1; FORCE_S5C=1; FORCE_S5D=1; FORCE_S4B=1; }
[[ "$FORCE_S5" -eq 1 ]] && { FORCE_S5B=1; FORCE_S5C=1; FORCE_S5D=1; FORCE_S4B=1; }
[[ "$FORCE_S5B" -eq 1 ]] && { FORCE_S5C=1; FORCE_S5D=1; FORCE_S4B=1; }
[[ "$FORCE_S5C" -eq 1 ]] && { FORCE_S5D=1; FORCE_S4B=1; }
[[ "$FORCE_S5D" -eq 1 ]] && { FORCE_S4B=1; }

INT_DIR="$WORK_DIR/intermediate"
RES_DIR="$WORK_DIR/results"
[[ -z "$TMP_DIR" ]] && TMP_DIR="$WORK_DIR/tmp"

QGIS_TMP="$TMP_DIR/qgis_tmp"
PM2_MEMMAP="$TMP_DIR/pixelminimax_memmap"

mkdir -p "$INT_DIR" "$RES_DIR" "$TMP_DIR" "$QGIS_TMP" "$PM2_MEMMAP"

# ------------------------------------------------------------------
# TEST_PREFIX — Parallelverzeichnisse für Step-2-Vergleichstest
# Wenn gesetzt: INT_DIR, RES_DIR, TMP_DIR → *_PREFIX-Varianten.
# DEM_10M bleibt im ORIGINAL INT_DIR (read-only, kein Neuaufbau).
# ------------------------------------------------------------------
if [[ -n "$TEST_PREFIX" ]]; then
    _ORIG_INT_DIR="$INT_DIR"
    _ORIG_DEM_10M="$INT_DIR/AT_10m_PADDED.tif"
    INT_DIR="${WORK_DIR}/intermediate_${TEST_PREFIX}"
    RES_DIR="${WORK_DIR}/results_${TEST_PREFIX}"
    TMP_DIR="${WORK_DIR}/tmp_${TEST_PREFIX}"
    QGIS_TMP="$TMP_DIR/qgis_tmp"
    PM2_MEMMAP="$TMP_DIR/pixelminimax_memmap"
    mkdir -p "$INT_DIR" "$RES_DIR" "$TMP_DIR" "$QGIS_TMP" "$PM2_MEMMAP"
    # Sicherheitscheck: Produktions-DEM muss existieren
    if [[ ! -f "$_ORIG_DEM_10M" ]]; then
        echo "FEHLER: --test-prefix gesetzt aber Produktions-DEM fehlt:"
        echo "  $_ORIG_DEM_10M"
        echo "Bitte zuerst Step 0 im normalen Lauf ausführen."
        exit 1
    fi
    # Produktions-GPKG Freeze-Check
    _PROD_PROM_RAW="${_ORIG_INT_DIR}/peaks_prom_raw.gpkg"
    if [[ ! -f "$_PROD_PROM_RAW" ]]; then
        echo "WARNUNG: Produktions-peaks_prom_raw.gpkg nicht gefunden — Vergleich nach Testlauf nicht möglich."
    fi
fi

export TMPDIR="$QGIS_TMP"
export TEMP="$QGIS_TMP"
export TMP="$QGIS_TMP"
export SOTA_TMPDIR="$QGIS_TMP"
export SOTA_PIXMEM_DIR="$PM2_MEMMAP"
export SOTA_SORT_CHUNK="$SORT_CHUNK"

# Saubere Trennung:
NATIONAL_BORDER_GPKG="$BORDER_GPKG"
ANALYSIS_POLY="$NATIONAL_BORDER_GPKG"
if [[ -n "$TEST_REGION" ]]; then
    ANALYSIS_POLY="$TEST_REGION"
fi

echo "==================================================================="
echo " AT SOTA Seamless Pipeline v5.0c (Step 5c coordinate validation sanitizer + Step 5d final assignment)"
echo "==================================================================="
echo " Work dir     : $WORK_DIR"
echo " Temp dir     : $TMP_DIR"
echo " QGIS tmp     : $QGIS_TMP"
echo " PM2 memmap   : $PM2_MEMMAP"
echo " Austria border: $NATIONAL_BORDER_GPKG"
echo " Analysis poly : ${ANALYSIS_POLY}"
echo " ALS 1m       : $ALS_1M"
echo " Copernicus   : $COPERNICUS_30M"
echo " Names GPKG   : ${NAMES_GPKG:-<nicht angegeben>}"
echo " BL GPKG      : ${BL_GPKG:-<nicht angegeben>}"
echo " Test region  : ${TEST_REGION:-<nationaler Lauf>}"
echo " Buffer dist  : ${BUFFER_DIST} m"
echo " Threads      : ${THREADS}"
echo " Sort chunk   : ${SORT_CHUNK}"
echo " Raw peak rad.: ${RAW_PEAK_RADIUS} m"
echo " Raw peak cand: ${RAW_PEAK_CANDIDATES}"
echo " DBNP cand    : ${DB_NO_PEAK_CANDIDATES}"
echo " Ambig rad.   : ${AMBIGUITY_PAIR_RADIUS} m"
echo " Ambig zdiff  : ${AMBIGUITY_ZDIFF} m"
echo " Ambig kcol   : ${AMBIGUITY_KEYCOL_DIST} m"
echo " Coord BEV rad: ${COORD_BEV_RADIUS} m"
echo " Coord max rad: ${COORD_LOCAL_MAX_RADIUS} m"
echo " Coord F_CODE : ${COORD_FCODES}"
echo " Skip Step 1  : ${SKIP_S1}"
echo " Only Step 1  : ${ONLY_S1}"
if [[ -n "$TEST_PREFIX" ]]; then
  echo " TEST PREFIX  : ${TEST_PREFIX}  (Parallelverzeichnisse aktiv — Produktionsdaten werden nicht berührt)"
fi
echo "==================================================================="

skip_or_run() {
    local step="$1"
    local output="$2"
    local label="$3"
    local force_var="FORCE_S${step}"

    echo ""
    if [[ -f "$output" && "${!force_var}" -eq 0 ]]; then
        echo "--- STEP ${step}: ${label} ---"
        echo "    ✓ SKIP — Output existiert: $(basename "$output")"
        echo "    (--force-step${step} um neu zu berechnen)"
        return 1
    else
        if [[ "${!force_var}" -eq 1 ]]; then
            echo "--- STEP ${step}: ${label} (--force: neu berechnen) ---"
            rm -f "$output"
        else
            echo "--- STEP ${step}: ${label} ---"
        fi
        return 0
    fi
}

# -------------------------------------------------------------------
# STEP 0 — DEM Mosaicking & 40 km Padding
# Rasterstabil: -tap erzwingt gemeinsames 10m-GRID in EPSG:25833
# -------------------------------------------------------------------
# DEM: bei TEST_PREFIX aus Produktions-INT_DIR lesen, sonst normal
if [[ -n "$TEST_PREFIX" ]]; then
    DEM_10M="$_ORIG_DEM_10M"   # Produktions-DEM — read-only, wird nicht überschrieben
else
    DEM_10M="$INT_DIR/AT_10m_PADDED.tif"
fi

if [[ -z "$TEST_PREFIX" ]] && [[ ! -f "$DEM_10M" || "$FORCE_S1" -eq 1 ]]; then
    echo ""
    echo "--- STEP 0: 10 m Padded DEM erstellen ---"
    [[ "$FORCE_S1" -eq 1 ]] && rm -f "$DEM_10M"

    CUTLINE_SRC="$ANALYSIS_POLY"
    if [[ -n "$TEST_REGION" ]]; then
        echo "  Test-Region: $TEST_REGION (40 km Buffer, grid-aligned to Austria 10 m lattice)"
    else
        echo "  Nationaler Lauf: $NATIONAL_BORDER_GPKG (40 km Buffer)"
    fi

    qgis_process run native:buffer \
        --INPUT="$CUTLINE_SRC" \
        --DISTANCE="$BUFFER_DIST" \
        --SEGMENTS=5 \
        --OUTPUT="$INT_DIR/Cutline_Buffer.gpkg"

    echo "  Mosaicking & Resampling auf 10 m..."
    gdalwarp \
        -t_srs EPSG:"${TGT_EPSG}" \
        -tr 10 10 \
        -tap \
        -r cubic \
        -cutline "$INT_DIR/Cutline_Buffer.gpkg" \
        -crop_to_cutline \
        -co COMPRESS=LZW \
        -co BIGTIFF=YES \
        -co NUM_THREADS=ALL_CPUS \
        "$COPERNICUS_30M" "$ALS_1M" \
        "$DEM_10M"
else
    echo ""
    echo "--- STEP 0: ✓ SKIP — Padded DEM vorhanden: AT_10m_PADDED.tif ---"
    echo "    (Löschen Sie $DEM_10M für Neuberechnung)"
fi

# -------------------------------------------------------------------
# STEP 1 — Seamless Hydrology
# Optional überspringbar mit Dummy-Dateien
# -------------------------------------------------------------------
PEAKS_10M="$INT_DIR/peaks_10m.gpkg"
BASINS_10M="$INT_DIR/basins_10m.gpkg"
SADDLES_10M="$INT_DIR/saddles_10m.gpkg"

if [[ -n "$TEST_PREFIX" || "$SKIP_S1" -eq 1 ]]; then
    echo ""
    echo "--- STEP 1: Seamless Hydrology ---"
    echo "    ✓ SKIP per --skip-step1"
    echo "    Lege Dummy-Dateien an:"
    touch "$PEAKS_10M"
    touch "$BASINS_10M"
    touch "$SADDLES_10M"
elif [[ -f "$PEAKS_10M" && -f "$BASINS_10M" && -f "$SADDLES_10M" && "$FORCE_S1" -eq 0 ]]; then
    echo ""
    echo "--- STEP 1: Seamless Hydrology ---"
    echo "    ✓ SKIP — peaks/basins/saddles_10m.gpkg vorhanden"
    echo "    (--force-step1 um neu zu berechnen)"
else
    echo ""
    if [[ "$FORCE_S1" -eq 1 ]]; then
        echo "--- STEP 1: Seamless Hydrology (--force: neu berechnen) ---"
        rm -f "$PEAKS_10M" "$BASINS_10M" "$SADDLES_10M"
    else
        echo "--- STEP 1: Seamless Hydrology ---"
    fi
    qgis_process run script:AT_SOTA_SeamlessHydrology \
        --INPUT_DEM="$DEM_10M" \
        --BORDER_POLY="$ANALYSIS_POLY" \
        --OUTPUT_PEAKS="$PEAKS_10M" \
        --OUTPUT_BASINS="$BASINS_10M" \
        --OUTPUT_SADDLES="$SADDLES_10M"
fi

# --only-step1 early exit
if [[ "$ONLY_S1" -eq 1 ]]; then
    echo ""
    echo "==================================================================="
    echo " --only-step1: Step 1 (Seamless Hydrology) abgeschlossen."
    echo " Pipeline wird wie gewünscht nach Step 1 beendet."
    echo ""
    echo " Auditability-Outputs:"
    echo "   peaks_10m.gpkg   → ${PEAKS_10M}"
    echo "   basins_10m.gpkg  → ${BASINS_10M}"
    echo "   saddles_10m.gpkg → ${SADDLES_10M}"
    echo ""
    echo " Diese Layer dienen nur der Auditability und beeinflussen"
    echo " Step 2 (PixelMinimax) nicht — Step 2 nutzt direkt das DEM."
    echo "==================================================================="
    exit 0
fi

# -------------------------------------------------------------------
# STEP 2 — Pixel-Minimax Prominenz
# Lokaler oder nationaler Ausschnitt, aber auf gemeinsamem Österreich-GRID
# -------------------------------------------------------------------
PROM_RAW="$INT_DIR/peaks_prom_raw.gpkg"

if skip_or_run 2 "$PROM_RAW" "Pixel Minimax Prominenz (Union-Find v2.1 memmap)"; then
    qgis_process run script:AT_SOTA_PixelMinimax \
        --input_dem="$DEM_10M" \
        --border_poly="$ANALYSIS_POLY" \
        --resolution=10 \
        --min_prominence=130 \
        --output="$PROM_RAW"
fi

# -------------------------------------------------------------------
# STEP 2b — Routing-Zielpeak (Post-Processing auf peaks_prom_raw.gpkg)
# Ergänzt target_pk_x/y/elev/dist Felder — kein UF-Loop-Overhead
# -------------------------------------------------------------------
ROUTING_SCRIPT="$(dirname "$0")/scripts/find_routing_targets.py"
[[ ! -f "$ROUTING_SCRIPT" ]] && \
    ROUTING_SCRIPT="$(dirname "$0")/find_routing_targets.py"
if [[ -f "$ROUTING_SCRIPT" && -f "$PROM_RAW" ]]; then
    echo ""
    echo "--- STEP 2b: Routing-Zielpeak ---"
    python3 "$ROUTING_SCRIPT" --input "$PROM_RAW" --inplace
else
    echo "--- STEP 2b: SKIP — Routing-Skript nicht gefunden ---"
fi

# --test-prefix early exit: nach Step 2 Vergleich starten und beenden
if [[ -n "$TEST_PREFIX" ]]; then
    _PROD_PROM="${_ORIG_INT_DIR}/peaks_prom_raw.gpkg"
    _TEST_PROM="${INT_DIR}/peaks_prom_raw.gpkg"
    echo ""
    echo "==================================================================="
    echo " --test-prefix: Step 2 abgeschlossen."
    echo " Test-Output : $_TEST_PROM"
    echo " Produktion  : $_PROD_PROM"
    echo "==================================================================="
    if [[ -f "$_PROD_PROM" && -f "$_TEST_PROM" ]]; then
        echo " Starte Vergleichsskript..."
        # Vergleichsskript suchen: gleicher Ordner oder scripts/-Unterordner
        COMPARE_SCRIPT="$(dirname "$0")/compare_step2_results.py"
        [[ ! -f "$COMPARE_SCRIPT" ]] && \
            COMPARE_SCRIPT="$(dirname "$0")/scripts/compare_step2_results.py"
        if [[ -f "$COMPARE_SCRIPT" ]]; then
            python3 "$COMPARE_SCRIPT" \
                --production "$_PROD_PROM" \
                --test        "$_TEST_PROM" \
                --outdir      "${WORK_DIR}/compare_${TEST_PREFIX}" \
                --label-prod  "v2.6_production" \
                --label-test  "v3.0_${TEST_PREFIX}"
        else
            echo " Vergleichsskript nicht gefunden: $COMPARE_SCRIPT"
            echo " Manuell ausführen:"
            echo "   python3 compare_step2_results.py --production \"$_PROD_PROM\" --test \"$_TEST_PROM\" --outdir ${WORK_DIR}/compare_${TEST_PREFIX}"
        fi
    else
        echo " Vergleich nicht möglich (eine Datei fehlt)"
    fi
    exit 0
fi

# -------------------------------------------------------------------
# STEP 3 — 1 m Refinement
# -------------------------------------------------------------------
SOTA_VALID="$INT_DIR/peaks_sota_valid.gpkg"

if skip_or_run 3 "$SOTA_VALID" "1 m Refinement (Ambiguous Band 130–170 m)"; then
    qgis_process run script:AT_SOTA_Refine1m \
        --INPUT_PEAKS="$PROM_RAW" \
        --DEM_1M="$ALS_1M" \
        --OUTPUT_SOTA="$SOTA_VALID"
fi

# -------------------------------------------------------------------
# STEP 4 — Namen + Bundesländer Join
# Border-Logik bleibt national
# -------------------------------------------------------------------
FINAL="$RES_DIR/AT_SOTA_Final.gpkg"

if skip_or_run 4 "$FINAL" "Namen + Bundesländer Join"; then
    if [[ -n "$NAMES_GPKG" ]]; then
        BL_ARG=""
        [[ -n "$BL_GPKG" ]] && BL_ARG="--bl_gpkg_path=$BL_GPKG"
        qgis_process run script:AT_SOTA_Join_Geonamen \
            --peaks_layer="$SOTA_VALID" \
            --geonamen_gpkg_path="$NAMES_GPKG" \
            --search_radius=30 \
            ${BL_ARG} \
            --border_gpkg_path="$NATIONAL_BORDER_GPKG" \
            --output="$FINAL"
    else
        echo "  Kein Geonamen-GPKG — Ergebnis ohne Namen kopieren"
        cp "$SOTA_VALID" "$FINAL"
    fi
fi

# -------------------------------------------------------------------
# STEP 5 — SOTA-Datenbank Abgleich
# FOREIGN_PEAK / border logic bleibt national
# -------------------------------------------------------------------
MATCHED="$RES_DIR/AT_SOTA_Matched.gpkg"
SOTA_CSV="$WORK_DIR/raw/SOTA_2026.csv"

if skip_or_run 5 "$MATCHED" "SOTA-DB Abgleich (einheitliche Tabelle)"; then
    BL_STEP5_ARG=""
    [[ -n "$BL_GPKG" ]] && BL_STEP5_ARG="--bl_gpkg_path=$BL_GPKG"
    qgis_process run script:AT_SOTA_Match_DB \
        --peaks_layer="$FINAL" \
        --sota_csv_path="$SOTA_CSV" \
        --match_radius=500 \
        --neighbor_radius="$BUFFER_DIST" \
        --border_gpkg="$NATIONAL_BORDER_GPKG" \
        ${BL_STEP5_ARG} \
        --raw_peaks_layer="$PROM_RAW" \
        --raw_peak_radius="$RAW_PEAK_RADIUS" \
        --raw_peak_candidates="$RAW_PEAK_CANDIDATES" \
        --near_calc_radius="$NEAR_CALC_RADIUS" \
        --near_calc_candidates="$NEAR_CALC_CANDIDATES" \
        --output="$MATCHED"
fi

# -------------------------------------------------------------------
# STEP 5b — Summit-Ambiguity Diagnose
# -------------------------------------------------------------------
MATCHED_DIAG="$RES_DIR/AT_SOTA_Matched_Diagnosed.gpkg"
AMBIG_OFFICIAL="$RES_DIR/AT_SOTA_Ambiguity_OfficialPoints.gpkg"
AMBIG_LINKS="$RES_DIR/AT_SOTA_Ambiguity_Links.gpkg"

if [[ -f "$MATCHED_DIAG" && -f "$AMBIG_OFFICIAL" && -f "$AMBIG_LINKS" && "$FORCE_S5B" -eq 0 ]]; then
    echo ""
    echo "--- STEP 5b: Summit-Ambiguity Diagnose ---"
    echo "    ✓ SKIP — Diagnosed matched / official points / links vorhanden"
    echo "    (--force-step5b um neu zu berechnen)"
else
    echo ""
    if [[ "$FORCE_S5B" -eq 1 ]]; then
        echo "--- STEP 5b: Summit-Ambiguity Diagnose (--force: neu berechnen) ---"
        rm -f "$MATCHED_DIAG" "$AMBIG_OFFICIAL" "$AMBIG_LINKS"
    else
        echo "--- STEP 5b: Summit-Ambiguity Diagnose ---"
    fi
    qgis_process run script:AT_SOTA_Ambiguity_Diagnosis \
        --matched_layer="$MATCHED" \
        --ambiguity_pair_radius="$AMBIGUITY_PAIR_RADIUS" \
        --ambiguity_zdiff="$AMBIGUITY_ZDIFF" \
        --ambiguity_soft_zdiff="$AMBIGUITY_SOFT_ZDIFF" \
        --ambiguity_keycol_dist="$AMBIGUITY_KEYCOL_DIST" \
        --match_elev_link_radius="$MATCH_ELEV_LINK_RADIUS" \
        --output="$MATCHED_DIAG" \
        --output_official_points="$AMBIG_OFFICIAL" \
        --output_links="$AMBIG_LINKS"
fi

# -------------------------------------------------------------------
# STEP 5c — Coordinate and summit-identity validation
# -------------------------------------------------------------------
MATCHED_COORD="$RES_DIR/AT_SOTA_Matched_CoordValidated.gpkg"
COORD_ISSUES="$RES_DIR/AT_SOTA_Coordinate_Issues.gpkg"

if [[ -f "$MATCHED_COORD" && -f "$COORD_ISSUES" && "$FORCE_S5C" -eq 0 ]]; then
    echo ""
    echo "--- STEP 5c: Coordinate + summit-identity validation ---"
    echo "    ✓ SKIP — Coordinate validation outputs vorhanden"
    echo "    (--force-step5c um neu zu berechnen)"
else
    echo ""
    if [[ "$FORCE_S5C" -eq 1 ]]; then
        echo "--- STEP 5c: Coordinate + summit-identity validation (--force: neu berechnen) ---"
        rm -f "$MATCHED_COORD" "$COORD_ISSUES"
    else
        echo "--- STEP 5c: Coordinate + summit-identity validation ---"
    fi

    if [[ -z "$NAMES_GPKG" ]]; then
        echo "ERROR: Step 5c requires --names-gpkg for BEV-NAMEN reference validation."
        exit 1
    fi

    qgis_process run script:AT_SOTA_Coordinate_Validation \
        --assigned_layer="$MATCHED_DIAG" \
        --dem_1m="$ALS_1M" \
        --names_gpkg_path="$NAMES_GPKG" \
        --names_layer_name="NAM_7300_GELAENDEFORM_P_20250325" \
        --f_codes="$COORD_FCODES" \
        --bev_search_radius="$COORD_BEV_RADIUS" \
        --local_max_radius="$COORD_LOCAL_MAX_RADIUS" \
        --output="$MATCHED_COORD" \
        --output_issues="$COORD_ISSUES"
fi

# -------------------------------------------------------------------
# STEP 5d — Final Assignment (coordinate-aware)
# -------------------------------------------------------------------
MATCHED_ASSIGNED="$RES_DIR/AT_SOTA_Matched_Assigned.gpkg"

if [[ -f "$MATCHED_ASSIGNED" && "$FORCE_S5D" -eq 0 ]]; then
    echo ""
    echo "--- STEP 5d: Final Assignment (coordinate-aware) ---"
    echo "    ✓ SKIP — AT_SOTA_Matched_Assigned.gpkg vorhanden"
    echo "    (--force-step5d um neu zu berechnen)"
else
    echo ""
    if [[ "$FORCE_S5D" -eq 1 ]]; then
        echo "--- STEP 5d: Final Assignment (coordinate-aware; --force: neu berechnen) ---"
        rm -f "$MATCHED_ASSIGNED"
    else
        echo "--- STEP 5d: Final Assignment (coordinate-aware) ---"
    fi
    qgis_process run script:AT_SOTA_Final_Assignment \
        --matched_layer="$MATCHED_COORD" \
        --pair_zdiff="$AMBIGUITY_ZDIFF" \
        --exact_official_dist=15 \
        --near_official_dist=100 \
        --replay_official_dist="$MATCH_ELEV_LINK_RADIUS" \
        --output="$MATCHED_ASSIGNED"
fi

# -------------------------------------------------------------------
# STEP 5e — Replay / manual backlog / worklists (conceptual downstream step)
# -------------------------------------------------------------------
echo ""
echo "--- STEP 5e: Replay / manual backlog / worklists ---"
echo "    Hinweis: Step 5e ist die operative Ableitung aus AT_SOTA_Matched_Assigned.gpkg"
echo "    und AT_SOTA_Coordinate_Issues.gpkg. Wenn ein separates Worklist-Skript verwendet"
echo "    wird, sollte es jetzt auf den coordinate-aware Assigned-Layer laufen."
echo "    Kein zusätzlicher Rechenschritt in dieser Batch-Datei."

# -------------------------------------------------------------------
# STEP 4b — Schlüsselsattel-Export
# -------------------------------------------------------------------
KEYCOL_POINTS="$RES_DIR/keycol_points.gpkg"
KEYCOL_LINES="$RES_DIR/peak_to_col_lines.gpkg"

if [[ -f "$KEYCOL_POINTS" && -f "$KEYCOL_LINES" && "$FORCE_S4B" -eq 0 ]]; then
    echo ""
    echo "--- STEP 4b: Schlüsselsattel-Export ---"
    echo "    ✓ SKIP — keycol_points.gpkg und peak_to_col_lines.gpkg vorhanden"
    echo "    (--force-step4b um neu zu berechnen)"
else
    echo ""
    if [[ "$FORCE_S4B" -eq 1 ]]; then
        echo "--- STEP 4b: Schlüsselsattel-Export (--force: neu berechnen) ---"
        rm -f "$KEYCOL_POINTS" "$KEYCOL_LINES"
    else
        echo "--- STEP 4b: Schlüsselsattel-Export ---"
    fi
    qgis_process run script:AT_SOTA_Export_Keycol \
        --matched_layer="$MATCHED_ASSIGNED" \
        --peaks_layer="$FINAL" \
        --peaks_raw="$PROM_RAW" \
        --db_no_peak_radius="$RAW_PEAK_RADIUS" \
        --db_no_peak_candidates="$DB_NO_PEAK_CANDIDATES" \
        --output_points="$KEYCOL_POINTS" \
        --output_lines="$KEYCOL_LINES"
fi

echo " Pipeline complete. Ergebnisse in: $RES_DIR"
echo "==================================================================="
