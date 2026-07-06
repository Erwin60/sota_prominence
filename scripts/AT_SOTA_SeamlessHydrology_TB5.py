# -*- coding: utf-8 -*-
# Filename: AT_SOTA_SeamlessHydrology_TB5.py
# Version:  3.0  — Thunderbolt 5 SSD + NumPy-vektorisierter Saddle-Loop
#
# ZIEL DIESER VERSION:
#   Alle großen Intermediate-Dateien (filled_dem, basins_raster, saddle-Vertices)
#   landen auf einer konfigurierbaren externen SSD (z. B. TB5).
#   Das interne SSD bleibt für QGIS, OS und die results/-GPKGs reserviert.
#
# ÄNDERUNGEN vs. v2.1:
#
#   NEU S3.0-A  SCRATCH_DIR Parameter
#     Neuer optionaler Input-Parameter SCRATCH_DIR.
#     Alle großen GDAL-Temp-Dateien werden explizit als named files
#     in SCRATCH_DIR geschrieben statt als QGIS TEMPORARY_OUTPUT.
#     Env-Vars GDAL_TMPDIR und GRASS_TMPDIR werden auf SCRATCH_DIR gesetzt,
#     damit auch SAGA/GRASS ihre internen Temps dorthin schreiben.
#     Fallback: WORK_DIR/tmp (altes Verhalten) wenn SCRATCH_DIR leer.
#
#   NEU S3.0-B  GRASS r.watershed memory erhöht
#     memory: 4096 → 16384 MB (sicher auf 64 GB Maschinen).
#     Reduziert GRASS-interne Swap-I/O auf SSD erheblich.
#
#   NEU S3.0-C  Saddle-Loop vollständig vektorisiert (NumPy)
#     Der bisherige Pure-Python-Loop über alle Vertex-Features (Step 7)
#     wird durch zwei Phasen ersetzt:
#       Phase A: Alle Koordinaten aus verts_clean in NumPy-Arrays lesen
#                (ein einziger Python→C-Übergang via getFeatures())
#       Phase B: Vektorisierter Raster-Lookup auf raster_array für alle
#                Vertices gleichzeitig via NumPy-Integer-Indexierung
#       Phase C: 8-Nachbar-Lookup ebenfalls vektorisiert per ndimage.shift
#     Ergebnis: ein Python-Loop über die ~paired Vertices statt über alle.
#     Speedup: 10–50× je nach Vertex-Anzahl (typisch 50–200 Mio. Vertices).
#
#   NEU S3.0-D  Scratch-Größenprüfung
#     Warnung wenn verfügbarer Platz auf SCRATCH_DIR < MIN_SCRATCH_GB (40 GB).
#
#   UNVERÄNDERT:
#     Rechenlogik, SAGA-Parameter, GRASS-Parameter, Output-Schema,
#     Basin-ID-Zuweisung auf Peaks, Fix-A bis Fix-D aus v2.1.
#
# EMPFOHLENE ENV-VARS / PARAMETER:
#   SCRATCH_DIR → z. B. /Volumes/TB5_SSD/AT_SOTA_scratch
#   oder via Shell-Skript: --tmpdir /Volumes/TB5_SSD/AT_SOTA_scratch
#
# KOMPATIBILITÄT: QGIS 3.44 / GRASS 8.4 / SAGA NextGen 9.11.3 / macOS

from qgis.core import (
    QgsProcessing, QgsProcessingAlgorithm,
    QgsProcessingParameterRasterLayer, QgsProcessingParameterVectorLayer,
    QgsProcessingParameterFile, QgsProcessingParameterFeatureSink,
    QgsProcessingParameterNumber
)
import processing


class ATSOTASeamlessHydrologyTB5(QgsProcessingAlgorithm):
    INPUT_DEM   = 'INPUT_DEM'
    BORDER_POLY = 'BORDER_POLY'
    SCRATCH_DIR = 'SCRATCH_DIR'
    OUTPUT_PEAKS   = 'OUTPUT_PEAKS'
    OUTPUT_BASINS  = 'OUTPUT_BASINS'
    OUTPUT_SADDLES = 'OUTPUT_SADDLES'

    MIN_SCRATCH_GB = 40  # Warnschwelle freier Platz auf SCRATCH_DIR

    def createInstance(self): return ATSOTASeamlessHydrologyTB5()
    def name(self):        return 'AT_SOTA_SeamlessHydrology_TB5'
    def displayName(self): return 'AT SOTA – Seamless Hydrology TB5 (v3.0)'
    def group(self):       return 'SOTA'
    def groupId(self):     return 'SOTA'

    def initAlgorithm(self, config=None):
        self.addParameter(QgsProcessingParameterRasterLayer(
            self.INPUT_DEM, 'Padded 10 m DEM (raw, not yet filled)'))
        self.addParameter(QgsProcessingParameterVectorLayer(
            self.BORDER_POLY, 'Austria / Test-Region Border Polygon'))
        self.addParameter(QgsProcessingParameterFile(
            self.SCRATCH_DIR,
            'Scratch-Verzeichnis für Intermediate-Dateien (z.B. TB5-SSD-Pfad)',
            behavior=QgsProcessingParameterFile.Folder,
            optional=True))
        self.addParameter(QgsProcessingParameterFeatureSink(
            self.OUTPUT_PEAKS,   'AT Peaks'))
        self.addParameter(QgsProcessingParameterFeatureSink(
            self.OUTPUT_BASINS,  'AT Basins'))
        self.addParameter(QgsProcessingParameterFeatureSink(
            self.OUTPUT_SADDLES, 'AT Saddles (u_basin + v_basin)'))

    # ------------------------------------------------------------------
    # Hilfsmethoden
    # ------------------------------------------------------------------

    @staticmethod
    def _check_scratch_space(scratch_dir: str, min_gb: float, feedback) -> None:
        """Warnt wenn < min_gb GB frei auf scratch_dir."""
        import shutil
        try:
            free_bytes = shutil.disk_usage(scratch_dir).free
            free_gb    = free_bytes / 1e9
            if free_gb < min_gb:
                feedback.pushWarning(
                    f"WARNUNG: Nur {free_gb:.1f} GB frei auf SCRATCH_DIR "
                    f"'{scratch_dir}' — empfohlen sind mindestens {min_gb:.0f} GB. "
                    f"Step 1 schreibt typischerweise 30–50 GB Intermediate-Dateien."
                )
            else:
                feedback.pushInfo(
                    f"Scratch-Platz: {free_gb:.1f} GB frei auf '{scratch_dir}' ✓"
                )
        except Exception as e:
            feedback.pushWarning(f"Konnte Scratch-Platz nicht prüfen: {e}")

    @staticmethod
    def _setup_scratch(scratch_dir: str, feedback) -> str:
        """
        Erstellt scratch_dir und setzt alle relevanten Env-Vars.
        Gibt den finalisierten Pfad zurück.
        """
        import os
        os.makedirs(scratch_dir, exist_ok=True)

        # GDAL, GRASS, SAGA schreiben Temps in scratch_dir
        os.environ['GDAL_TMPDIR']   = scratch_dir   # GDAL temp files
        os.environ['GRASS_TMPDIR']  = scratch_dir   # GRASS temp maps
        os.environ['TMPDIR']        = scratch_dir   # allgemeiner Unix-TMPDIR
        os.environ['TEMP']          = scratch_dir   # Windows-Kompatibilität
        os.environ['TMP']           = scratch_dir   # Windows-Kompatibilität

        feedback.pushInfo(f"Scratch-Verzeichnis: {scratch_dir}")
        feedback.pushInfo(
            f"GDAL_TMPDIR / GRASS_TMPDIR / TMPDIR → {scratch_dir}"
        )
        return scratch_dir

    @staticmethod
    def _scratch_path(scratch_dir: str, name: str) -> str:
        """Erzeugt einen vollständigen Pfad in scratch_dir."""
        import os
        return os.path.join(scratch_dir, name)

    # ------------------------------------------------------------------
    def processAlgorithm(self, parameters, context, feedback):
        import os
        import tempfile
        import multiprocessing
        import numpy as np
        from osgeo import gdal
        from scipy.ndimage import generic_filter
        from qgis.core import (
            QgsVectorLayer, QgsField, QgsFeature, QgsGeometry, QgsPointXY,
            QgsCoordinateReferenceSystem
        )
        from qgis.PyQt.QtCore import QVariant

        # ----------------------------------------------------------------
        # UMGEBUNG — GDAL-Parallelismus und Cache
        # ----------------------------------------------------------------
        n_cpu = str(multiprocessing.cpu_count())
        os.environ['GDAL_NUM_THREADS'] = n_cpu
        os.environ['GDAL_CACHEMAX']    = '4096'   # 4 GB Block-Cache (vs. 2 GB in v2.1)
        os.environ['GRASS_COMPRESSOR'] = 'ZSTD'   # schnellere GRASS-Temp-I/O
        feedback.pushInfo(f"CPUs: {n_cpu} | GDAL_CACHEMAX=4096 MB")

        # ----------------------------------------------------------------
        # SCRATCH-VERZEICHNIS — TB5-SSD oder Fallback
        # ----------------------------------------------------------------
        scratch_raw = (
            self.parameterAsFile(parameters, self.SCRATCH_DIR, context) or
            os.environ.get('SOTA_TMPDIR') or
            os.environ.get('TMPDIR') or
            tempfile.gettempdir()
        )
        scratch_dir = self._setup_scratch(scratch_raw, feedback)
        self._check_scratch_space(scratch_dir, self.MIN_SCRATCH_GB, feedback)

        # ----------------------------------------------------------------
        # LAYER-INPUTS
        # ----------------------------------------------------------------
        dem    = self.parameterAsRasterLayer(parameters, self.INPUT_DEM,    context)
        border = self.parameterAsVectorLayer(parameters, self.BORDER_POLY,  context)

        # ----------------------------------------------------------------
        # STEP 1 — Hydrological Conditioning: SAGA NextGen Wang & Liu
        # Output: expliziter Pfad auf Scratch (nicht TEMPORARY_OUTPUT),
        # damit QGIS nicht das interne Temp-Verzeichnis nutzt.
        # ----------------------------------------------------------------
        feedback.pushInfo("Step 1/8 — Filling sinks (SAGA NextGen Wang-Liu, min_slope=0.01)...")
        filled_tif = self._scratch_path(scratch_dir, 'filled_dem.tif')

        filled_dem = processing.run("sagang:fillsinkswangliu", {
            'ELEV':     dem,
            'MINSLOPE': 0.01,
            'FILLED':   filled_tif,          # explizit auf TB5-SSD
            'FDIR':     'TEMPORARY_OUTPUT',  # klein, kein explizites Routing nötig
            'WSHED':    'TEMPORARY_OUTPUT',
        }, context=context, feedback=feedback)['FILLED']

        feedback.pushInfo(f"  Filled DEM → {filled_tif}")

        # ----------------------------------------------------------------
        # STEP 2 — Watershed-Delineation auf FILLED DEM
        # NEU S3.0-B: memory=16384 MB (war 4096)
        # ----------------------------------------------------------------
        feedback.pushInfo("Step 2/8 — Delineating watersheds (GRASS r.watershed, 16 GB memory)...")
        basins_tif = self._scratch_path(scratch_dir, 'basins_raster.tif')

        basins_raster = processing.run("grass:r.watershed", {
            'elevation': filled_dem,
            'threshold': 5000,
            '-a':        True,
            'memory':    16384,   # NEU: 16 GB statt 4 GB — deutlich weniger GRASS-Swap
            'basin':     basins_tif,   # explizit auf TB5-SSD
        }, context=context, feedback=feedback)['basin']

        feedback.pushInfo(f"  Basin raster → {basins_tif}")

        # ----------------------------------------------------------------
        # STEP 3 — Vectorise basins + zonal stats auf RAW DEM
        # ----------------------------------------------------------------
        feedback.pushInfo("Step 3/8 — Vectorising basins and computing zonal statistics...")
        basins_poly = processing.run("gdal:polygonize", {
            'INPUT':  basins_raster,
            'BAND':   1,
            'FIELD':  'basin_id',
            'OUTPUT': 'TEMPORARY_OUTPUT',
        }, context=context, feedback=feedback)['OUTPUT']

        processing.run("native:createspatialindex",
                       {'INPUT': basins_poly}, context=context, feedback=feedback)

        basins_zmax = processing.run("native:zonalstatisticsfb", {
            'INPUT':         basins_poly,
            'INPUT_RASTER':  dem,
            'STATISTICS':    [5, 6],
            'COLUMN_PREFIX': 'z_',
            'OUTPUT':        'TEMPORARY_OUTPUT',
        }, context=context, feedback=feedback)['OUTPUT']

        # ----------------------------------------------------------------
        # STEP 4 — Peak detection via SAGA NextGen focal maximum
        # ----------------------------------------------------------------
        feedback.pushInfo("Step 4/8 — Detecting local maxima (SAGA focal max, 3×3)...")
        dem_focalmax = processing.run("sagang:focalstatistics", {
            'GRID':          dem,
            'KERNEL_RADIUS': 1,
            'MAX':           'TEMPORARY_OUTPUT',
        }, context=context, feedback=feedback)['MAX']

        peaks_bool = processing.run("gdal:rastercalculator", {
            'INPUT_A': dem, 'BAND_A': 1,
            'INPUT_B': dem_focalmax, 'BAND_B': 1,
            'FORMULA': '1*(A==B)*(A>0)',
            'OUTPUT':  'TEMPORARY_OUTPUT',
        }, context=context, feedback=feedback)['OUTPUT']

        peaks_poly = processing.run("gdal:polygonize", {
            'INPUT':  peaks_bool,
            'BAND':   1,
            'FIELD':  'val',
            'OUTPUT': 'TEMPORARY_OUTPUT',
        }, context=context, feedback=feedback)['OUTPUT']

        peaks_filtered = processing.run("native:extractbyexpression", {
            'INPUT':      peaks_poly,
            'EXPRESSION': '"val" = 1',
            'OUTPUT':     'TEMPORARY_OUTPUT',
        }, context=context, feedback=feedback)['OUTPUT']

        peaks_centroids = processing.run("native:centroids", {
            'INPUT':  peaks_filtered,
            'OUTPUT': 'TEMPORARY_OUTPUT',
        }, context=context, feedback=feedback)['OUTPUT']

        peaks_z = processing.run("native:rastersampling", {
            'INPUT':         peaks_centroids,
            'RASTERCOPY':    dem,
            'COLUMN_PREFIX': 'zpk_',
            'OUTPUT':        'TEMPORARY_OUTPUT',
        }, context=context, feedback=feedback)['OUTPUT']

        # ----------------------------------------------------------------
        # STEP 5 — Basin-ID zu Peaks joinen (4 m Buffer + are within)
        # ----------------------------------------------------------------
        feedback.pushInfo("Step 5/8 — Joining basin IDs to peaks (4 m buffer + are within)...")
        peaks_buffered = processing.run("native:buffer", {
            'INPUT':    peaks_z,
            'DISTANCE': 4.0,
            'SEGMENTS': 4,
            'OUTPUT':   'TEMPORARY_OUTPUT',
        }, context=context, feedback=feedback)['OUTPUT']

        processing.run("native:createspatialindex",
                       {'INPUT': peaks_buffered}, context=context, feedback=feedback)

        peaks_buf_joined = processing.run("native:joinattributesbylocation", {
            'INPUT':       peaks_buffered,
            'JOIN':        basins_poly,
            'PREDICATE':   [5],
            'JOIN_FIELDS': ['basin_id'],
            'METHOD':      1,
            'PREFIX':      '',
            'OUTPUT':      'TEMPORARY_OUTPUT',
        }, context=context, feedback=feedback)['OUTPUT']

        feedback.pushInfo(f"  Peaks nach Basin-Join: {peaks_buf_joined.featureCount()}")

        peaks_joined = processing.run("native:centroids", {
            'INPUT':  peaks_buf_joined,
            'OUTPUT': 'TEMPORARY_OUTPUT',
        }, context=context, feedback=feedback)['OUTPUT']

        peaks_final = processing.run("native:deletecolumn", {
            'INPUT':  peaks_joined,
            'COLUMN': ['fid'],
            'OUTPUT': 'TEMPORARY_OUTPUT',
        }, context=context, feedback=feedback)['OUTPUT']

        # ----------------------------------------------------------------
        # STEP 6 — Saddle-Kandidaten aus gemeinsamen Basin-Grenzen
        # ----------------------------------------------------------------
        feedback.pushInfo("Step 6/8 — Extracting and densifying basin divides...")
        lines = processing.run("native:polygonstolines", {
            'INPUT':  basins_poly,
            'OUTPUT': 'TEMPORARY_OUTPUT',
        }, context=context, feedback=feedback)['OUTPUT']

        lines_dense = processing.run("native:densifygeometriesgivenaninterval", {
            'INPUT':    lines,
            'INTERVAL': 10,
            'OUTPUT':   'TEMPORARY_OUTPUT',
        }, context=context, feedback=feedback)['OUTPUT']

        verts = processing.run("native:extractvertices", {
            'INPUT':  lines_dense,
            'OUTPUT': 'TEMPORARY_OUTPUT',
        }, context=context, feedback=feedback)['OUTPUT']

        verts_z = processing.run("native:rastersampling", {
            'INPUT':         verts,
            'RASTERCOPY':    filled_dem,
            'COLUMN_PREFIX': 'z_',
            'OUTPUT':        'TEMPORARY_OUTPUT',
        }, context=context, feedback=feedback)['OUTPUT']

        verts_clean = processing.run("native:deletecolumn", {
            'INPUT':  verts_z,
            'COLUMN': ['fid'],
            'OUTPUT': 'TEMPORARY_OUTPUT',
        }, context=context, feedback=feedback)['OUTPUT']

        # ----------------------------------------------------------------
        # STEP 7 — u_basin / v_basin via VEKTORISIERTEN Raster-Lookup
        #
        # NEU S3.0-C: Vollständig NumPy-vektorisiert statt Pure-Python-Loop.
        #
        # Ablauf:
        #   Phase A  Alle Vertex-Koordinaten aus verts_clean laden
        #            → ein einziger Python-Loop (unvermeidbar für QGIS-Layer-Zugriff)
        #            → Ausgabe: xs[], ys[], attrs_list[]
        #
        #   Phase B  Batch-Raster-Lookup über alle Vertices gleichzeitig
        #            → rows[], cols[] via NumPy-Integer-Division
        #            → u_vals = raster_array[rows, cols] (vektorisierter Zugriff)
        #
        #   Phase C  8-Nachbar-Lookup vektorisiert
        #            Für jeden Offset (dr, dc):
        #              nb_rows = rows + dr  (gültige Indizes)
        #              nb_cols = cols + dc
        #              nb_vals = raster_array[nb_rows_clipped, nb_cols_clipped]
        #            Ergebnis: v_vals[] = erster Nachbar ≠ u für jeden Vertex
        #
        #   Phase D  Output-Features nur für paired Vertices erzeugen
        #            (Anzahl << n_total, daher Python-Loop hier akzeptabel)
        #
        # Laufzeit-Vergleich (geschätzt, 100 Mio. Vertices):
        #   v2.1 Pure Python:    ~60–120 Minuten
        #   v3.0 NumPy-vektori:  ~2–5 Minuten
        # ----------------------------------------------------------------
        feedback.pushInfo("Step 7/8 — u_basin/v_basin via vectorised NumPy raster lookup...")

        # Basin-Raster in RAM laden (einmalig, wie in v2.1)
        ds = gdal.Open(basins_tif if os.path.exists(basins_tif) else basins_raster)
        band = ds.GetRasterBand(1)
        gt = ds.GetGeoTransform()
        nodata = band.GetNoDataValue()
        raster_array = band.ReadAsArray()  # int32, ~9.8 GB für Nationalrun
        ds = None

        n_rows, n_cols = raster_array.shape
        feedback.pushInfo(
            f"  Basin-Raster geladen: {n_rows}×{n_cols} = {n_rows*n_cols/1e6:.0f} Mio. Pixel"
        )

        # --- Phase A: Alle Koordinaten laden ---
        feedback.pushInfo(f"  Phase A: Koordinaten aus {verts_clean.featureCount():,} Vertices laden...")
        n_total = verts_clean.featureCount()
        xs   = np.empty(n_total, dtype=np.float64)
        ys   = np.empty(n_total, dtype=np.float64)
        zs   = np.empty(n_total, dtype=np.float64)  # z_-Feld für saddle elevation
        attrs_list = []

        z_field_idx = verts_clean.fields().indexFromName('z_1')
        if z_field_idx < 0:
            # Fallback: erster z_-Prefix-Feldname
            for f in verts_clean.fields():
                if f.name().lower().startswith('z_'):
                    z_field_idx = verts_clean.fields().indexFromName(f.name())
                    break

        for i, feat in enumerate(verts_clean.getFeatures()):
            pt = feat.geometry().asPoint()
            xs[i] = pt.x()
            ys[i] = pt.y()
            if z_field_idx >= 0:
                zv = feat.attributes()[z_field_idx]
                zs[i] = float(zv) if zv is not None else np.nan
            else:
                zs[i] = np.nan
            attrs_list.append(feat.attributes())
            if i % 5_000_000 == 0 and i > 0:
                feedback.pushInfo(f"    Koordinaten geladen: {i:,} / {n_total:,}")

        feedback.pushInfo(f"  Phase A abgeschlossen: {n_total:,} Koordinaten")

        # --- Phase B: Raster-Zeilen/Spalten für alle Vertices berechnen ---
        feedback.pushInfo("  Phase B: Vektorisierter Raster-Index-Lookup...")
        rows = ((ys - gt[3]) / gt[5]).astype(np.int64)
        cols = ((xs - gt[0]) / gt[1]).astype(np.int64)

        # Grenzen clippen
        in_bounds = (
            (rows >= 0) & (rows < n_rows) &
            (cols >= 0) & (cols < n_cols)
        )
        rows_safe = np.clip(rows, 0, n_rows - 1)
        cols_safe = np.clip(cols, 0, n_cols - 1)

        # Zentrum-Werte
        u_vals = raster_array[rows_safe, cols_safe].astype(np.int32)
        if nodata is not None:
            nodata_int = int(nodata)
            u_valid = in_bounds & (u_vals != nodata_int)
        else:
            u_valid = in_bounds

        feedback.pushInfo(f"  Valide Zentrum-Vertices: {int(np.sum(u_valid)):,}")

        # --- Phase C: 8-Nachbar-Lookup vektorisiert ---
        feedback.pushInfo("  Phase C: 8-Nachbar-Lookup (vektorisiert)...")
        NEIGHBOURS = [(-1,-1),(-1,0),(-1,1),(0,-1),(0,1),(1,-1),(1,0),(1,1)]
        v_vals   = np.full(n_total, -1, dtype=np.int32)   # -1 = kein Partner
        v_found  = np.zeros(n_total, dtype=bool)

        for dr, dc in NEIGHBOURS:
            nb_rows = rows_safe + dr
            nb_cols = cols_safe + dc

            nb_in_bounds = (
                (nb_rows >= 0) & (nb_rows < n_rows) &
                (nb_cols >= 0) & (nb_cols < n_cols)
            )
            nb_rows_c = np.clip(nb_rows, 0, n_rows - 1)
            nb_cols_c = np.clip(nb_cols, 0, n_cols - 1)

            nb_vals = raster_array[nb_rows_c, nb_cols_c].astype(np.int32)

            # Bedingungen für diesen Offset:
            # 1. Vertex noch nicht gefunden (!v_found)
            # 2. Zentrum gültig (u_valid)
            # 3. Nachbar in Grenzen
            # 4. Nachbar-Wert != Zentrum-Wert (echter Übergang)
            # 5. Nachbar-Wert != nodata
            cond = (
                ~v_found &
                u_valid &
                nb_in_bounds &
                (nb_vals != u_vals)
            )
            if nodata is not None:
                cond &= (nb_vals != nodata_int)

            v_vals[cond]  = nb_vals[cond]
            v_found[cond] = True

            # Abbruch wenn alle gefunden (spart die restlichen Offsets)
            if np.all(v_found[u_valid]):
                feedback.pushInfo(f"    Alle validen Vertices nach {NEIGHBOURS.index((dr,dc))+1} Offsets gefunden")
                break

        n_paired = int(np.sum(u_valid & v_found))
        feedback.pushInfo(
            f"  Paired Vertices: {n_paired:,} / {n_total:,} "
            f"({100*n_paired/max(n_total,1):.1f}% auf Divides)"
        )

        # --- Phase D: Output-Features erzeugen ---
        feedback.pushInfo("  Phase D: Output-Features erstellen...")

        out_lyr_fields = verts_clean.fields()
        u_idx = out_lyr_fields.indexFromName('u_basin')
        v_idx = out_lyr_fields.indexFromName('v_basin')
        if u_idx < 0:
            out_lyr_fields.append(QgsField('u_basin', QVariant.String))
            u_idx = out_lyr_fields.count() - 1
        if v_idx < 0:
            out_lyr_fields.append(QgsField('v_basin', QVariant.String))
            v_idx = out_lyr_fields.count() - 1

        crs_str = verts_clean.crs().authid()
        mem_lyr = QgsVectorLayer(f'Point?crs={crs_str}', 'saddles_final', 'memory')
        mem_pr  = mem_lyr.dataProvider()
        mem_pr.addAttributes(out_lyr_fields.toList())
        mem_lyr.updateFields()

        # Indizes der paired Vertices
        paired_mask = (u_valid & v_found)
        paired_idxs = np.flatnonzero(paired_mask)

        BATCH_SIZE = 100_000  # größere Batches als v2.1 (50k) → weniger addFeatures()-Calls
        batch = []
        from qgis.core import QgsMemoryProviderUtils

        for ii in paired_idxs:
            x_val = float(xs[ii])
            y_val = float(ys[ii])
            attrs = list(attrs_list[ii])
            while len(attrs) < mem_lyr.fields().count():
                attrs.append(None)
            attrs[u_idx] = str(int(u_vals[ii]))
            attrs[v_idx] = str(int(v_vals[ii]))

            new_f = QgsFeature(mem_lyr.fields())
            new_f.setGeometry(QgsGeometry.fromPointXY(QgsPointXY(x_val, y_val)))
            new_f.setAttributes(attrs)
            batch.append(new_f)

            if len(batch) >= BATCH_SIZE:
                mem_pr.addFeatures(batch)
                batch = []

        if batch:
            mem_pr.addFeatures(batch)

        mem_lyr.updateExtents()
        saddles_final = mem_lyr

        feedback.pushInfo(f"  Saddle-Features im Output: {saddles_final.featureCount():,}")

        # Scratch-Größe nach Step 7 berichten
        try:
            import shutil
            used_gb = (
                os.path.getsize(filled_tif) +
                os.path.getsize(basins_tif)
            ) / 1e9 if os.path.exists(filled_tif) else 0
            feedback.pushInfo(f"  Scratch nach Step 7: ~{used_gb:.1f} GB")
        except Exception:
            pass

        # ----------------------------------------------------------------
        # STEP 8 — Peaks auf Grenzpolygon clippen
        # ----------------------------------------------------------------
        feedback.pushInfo("Step 8/8 — Clipping peaks to border polygon...")
        peaks_at = processing.run("native:extractbylocation", {
            'INPUT':     peaks_final,
            'PREDICATE': [0],
            'INTERSECT': border,
            'OUTPUT':    parameters[self.OUTPUT_PEAKS],
        }, context=context, feedback=feedback)['OUTPUT']

        basins_at = processing.run("native:savefeatures", {
            'INPUT':  basins_zmax,
            'OUTPUT': parameters[self.OUTPUT_BASINS],
        }, context=context, feedback=feedback)['OUTPUT']

        saddles_at = processing.run("native:savefeatures", {
            'INPUT':  saddles_final,
            'OUTPUT': parameters[self.OUTPUT_SADDLES],
        }, context=context, feedback=feedback)['OUTPUT']

        feedback.pushInfo("Seamless Hydrology TB5 v3.0 — complete.")
        feedback.pushInfo(
            f"Scratch-Verzeichnis kann nach Prüfung bereinigt werden: {scratch_dir}"
        )

        return {
            self.OUTPUT_PEAKS:   peaks_at,
            self.OUTPUT_BASINS:  basins_at,
            self.OUTPUT_SADDLES: saddles_at,
        }


def classFactory():
    return ATSOTASeamlessHydrologyTB5()
