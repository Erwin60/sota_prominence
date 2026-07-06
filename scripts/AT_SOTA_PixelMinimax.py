# -*- coding: utf-8 -*-
# Filename: AT_SOTA_PixelMinimax_memmap.py
# Version:  2.6b — memmap/int64 + lightweight progress
#           O2: SORT_CHUNK via Bash-Parameter (100M) — einzige wirksame Optimierung
#           O3/O4 entfernt: auf Apple Silicon Unified Memory kontraproduktiv
#           target_pk: als Post-Processing via find_routing_targets.py
#
# ZIEL DIESER VERSION:
#   Die Rechenlogik bleibt UNVERÄNDERT:
#     - gleiches bilineares Resampling
#     - gleiche 3x3-Maxima-Erkennung
#     - gleiche absteigende Pixel-Reihenfolge
#     - gleiche 8er-Nachbarschaft
#     - gleiche Union-Find / NaN-Guard Keycol-Logik
#     - gleiche Output-Felder
#
#   Geändert wird NUR das Speicherlayout:
#     - große Union-Find-Arrays liegen als np.memmap auf SSD
#     - die absteigende Pixel-Reihenfolge wird als externer Merge-Sort erzeugt
#     - Scratch-Verzeichnis über SOTA_TMPDIR / SOTA_PIXMEM_DIR steuerbar
#
# EMPFOHLENE ENV-VARS (vom Shell-Skript gesetzt):
#   SOTA_TMPDIR       z.B. /Volumes/Daten/AT_SOTA_150m/tmp/qgis_tmp
#   SOTA_PIXMEM_DIR   z.B. /Volumes/Daten/AT_SOTA_150m/tmp/pixelminimax_memmap
#   SOTA_SORT_CHUNK   Anzahl Pixel pro Sortier-Chunk, Standard 20_000_000
#
# WICHTIG:
#   Diese Version ändert NICHT die mathematische Methode. Sie reduziert nur
#   die RAM-Spitze, indem große Datenstrukturen file-backed auf SSD liegen.

from qgis.PyQt.QtCore import QVariant
from qgis.core import (
    QgsProcessing, QgsProcessingAlgorithm,
    QgsProcessingParameterRasterLayer, QgsProcessingParameterFile,
    QgsProcessingParameterNumber, QgsProcessingParameterFeatureSink,
    QgsProcessingException,
    QgsFeature, QgsField, QgsFields, QgsGeometry, QgsPointXY,
    QgsCoordinateReferenceSystem, QgsCoordinateTransform, QgsProject,
    QgsWkbTypes, QgsFeatureSink, QgsVectorLayer
)
import heapq
import math
import os
import shutil
import tempfile
import time

import numpy as np
from osgeo import gdal
from scipy.ndimage import maximum_filter


class AT_SOTA_PixelMinimax(QgsProcessingAlgorithm):

    P_DEM = 'input_dem'
    P_BORDER = 'border_poly'
    P_RESOLUTION = 'resolution'
    P_MIN_PROM = 'min_prominence'
    P_OUTPUT = 'output'

    def createInstance(self):
        return self.__class__()

    def name(self):
        return 'AT_SOTA_PixelMinimax'

    def displayName(self):
        return 'AT SOTA — Pixel Minimax Prominenz v2.6 (memmap, int64, lightweight progress)'

    def group(self):
        return 'SOTA'

    def groupId(self):
        return 'SOTA'

    def initAlgorithm(self, config=None):
        self.addParameter(QgsProcessingParameterRasterLayer(
            self.P_DEM, 'Eingabe DEM (AT_10m_PADDED.tif)'))

        self.addParameter(QgsProcessingParameterFile(
            self.P_BORDER,
            'Grenzpolygon für Output-Clip (optional, beliebige CRS)',
            behavior=QgsProcessingParameterFile.File,
            fileFilter='GeoPackage (*.gpkg)', optional=True))

        self.addParameter(QgsProcessingParameterNumber(
            self.P_RESOLUTION,
            'Rechenauflösung (m) — 10m nativ (±5m Genauigkeit)',
            type=QgsProcessingParameterNumber.Integer,
            defaultValue=10, minValue=10, maxValue=200))

        self.addParameter(QgsProcessingParameterNumber(
            self.P_MIN_PROM,
            'Minimale Prominenz Ausgabe (m)',
            type=QgsProcessingParameterNumber.Double,
            defaultValue=130.0, minValue=1.0))

        self.addParameter(QgsProcessingParameterFeatureSink(
            self.P_OUTPUT, 'Gipfel mit Prominenz (→ AT_SOTA_Refine1m)',
            type=QgsProcessing.TypeVectorPoint))

    @staticmethod
    def _scratch_base():
        return (
            os.environ.get('SOTA_TMPDIR')
            or os.environ.get('TMPDIR')
            or tempfile.gettempdir()
        )

    @staticmethod
    def _memmap_base(run_tmp_dir: str) -> str:
        mm_root = os.environ.get('SOTA_PIXMEM_DIR')
        if mm_root:
            os.makedirs(mm_root, exist_ok=True)
            return tempfile.mkdtemp(prefix='pm2_mm_', dir=mm_root)
        mm_root = os.path.join(run_tmp_dir, 'memmap')
        os.makedirs(mm_root, exist_ok=True)
        return mm_root

    @staticmethod
    def _format_seconds(seconds: float) -> str:
        seconds = max(0, int(seconds))
        h, rem = divmod(seconds, 3600)
        m, s = divmod(rem, 60)
        return f"{h:02d}:{m:02d}:{s:02d}"



    @staticmethod
    def _dir_size_bytes(path: str) -> int:
        total = 0
        try:
            for root, _dirs, files in os.walk(path):
                for name in files:
                    try:
                        total += os.path.getsize(os.path.join(root, name))
                    except OSError:
                        pass
        except OSError:
            pass
        return total

    @staticmethod
    def _format_bytes(num_bytes: int) -> str:
        value = float(max(0, num_bytes))
        units = ['B', 'KB', 'MB', 'GB', 'TB']
        unit = units[0]
        for unit in units:
            if value < 1024.0 or unit == units[-1]:
                break
            value /= 1024.0
        if unit == 'B':
            return f"{int(value)} {unit}"
        return f"{value:.1f} {unit}"

    @staticmethod
    def _chunked_valid_min(flat_dem, flat_valid, chunk_size: int) -> float:
        best = np.inf
        n = flat_dem.shape[0]
        for start in range(0, n, chunk_size):
            end = min(n, start + chunk_size)
            mask = flat_valid[start:end]
            if not np.any(mask):
                continue
            cand = float(np.min(flat_dem[start:end][mask]))
            if cand < best:
                best = cand
        return float(best)

    @staticmethod
    def _init_parent(parent_mm, n: int, chunk_size: int):
        for start in range(0, n, chunk_size):
            end = min(n, start + chunk_size)
            parent_mm[start:end] = np.arange(start, end, dtype=np.int64)

    @staticmethod
    def _write_run(run_path: str, arr: np.ndarray):
        mm = np.memmap(run_path, dtype=np.int64, mode='w+', shape=(arr.shape[0],))
        mm[:] = arr
        mm.flush()
        del mm

    def _build_sorted_runs(self, flat_dem, flat_valid, n: int, scratch_dir: str, feedback):
        chunk_size = int(os.environ.get('SOTA_SORT_CHUNK', '20000000'))
        if chunk_size < 1_000_000:
            chunk_size = 1_000_000

        run_specs = []
        total_chunks = max(1, (n + chunk_size - 1) // chunk_size)
        valid_total = int(np.count_nonzero(flat_valid))

        feedback.pushInfo(
            f"Externer Sort: Chunk-Größe={chunk_size:,} Pixel | gültige Pixel={valid_total:,}")

        for chunk_no, start in enumerate(range(0, n, chunk_size), start=1):
            end = min(n, start + chunk_size)
            rel = np.flatnonzero(flat_valid[start:end])
            if rel.size == 0:
                continue

            local_idx = (rel + start).astype(np.int64, copy=False)
            # Exakte Ordnung wie zuvor:
            #   primär   Elevation absteigend
            #   sekundär Pixelindex aufsteigend
            order = np.lexsort((local_idx, -flat_dem[local_idx]))
            sorted_idx = local_idx[order]

            run_path = os.path.join(scratch_dir, f'sort_run_{len(run_specs):04d}.bin')
            self._write_run(run_path, sorted_idx)
            run_specs.append((run_path, int(sorted_idx.shape[0])))

            feedback.setProgress(min(20, int(20 * chunk_no / total_chunks)))
            feedback.pushInfo(
                f"  Sort-Run {len(run_specs):03d}: {sorted_idx.shape[0]:,} Pixel "
                f"({chunk_no}/{total_chunks})")

            del rel, local_idx, order, sorted_idx

        return run_specs, valid_total

    @staticmethod
    def _iter_sorted_indices(flat_dem, run_specs):
        runs = []
        heap = []

        for run_id, (run_path, run_len) in enumerate(run_specs):
            mm = np.memmap(run_path, dtype=np.int64, mode='r', shape=(run_len,))
            runs.append(mm)
            first_idx = int(mm[0])
            # heapq = Min-Heap → negative Höhe für absteigende Reihenfolge.
            # Bei gleicher Höhe entscheidet der Pixelindex aufsteigend.
            heapq.heappush(heap, (-float(flat_dem[first_idx]), first_idx, run_id, 0))

        try:
            while heap:
                _neg_elev, idx, run_id, pos = heapq.heappop(heap)
                yield idx

                next_pos = pos + 1
                run_mm = runs[run_id]
                if next_pos < run_mm.shape[0]:
                    next_idx = int(run_mm[next_pos])
                    heapq.heappush(
                        heap,
                        (-float(flat_dem[next_idx]), next_idx, run_id, next_pos)
                    )
        finally:
            for mm in runs:
                del mm

    @staticmethod
    def _new_memmap(path: str, dtype, shape, fill_value=None):
        mm = np.memmap(path, dtype=dtype, mode='w+', shape=shape)
        if fill_value is not None:
            mm[:] = fill_value
        return mm

    def processAlgorithm(self, parameters, context, feedback):
        dem_layer = self.parameterAsRasterLayer(parameters, self.P_DEM, context)
        border_path = self.parameterAsFile(parameters, self.P_BORDER, context) or ''
        resolution = int(self.parameterAsInt(parameters, self.P_RESOLUTION, context))
        min_prom = float(self.parameterAsDouble(parameters, self.P_MIN_PROM, context))

        if not dem_layer or not dem_layer.isValid():
            raise QgsProcessingException('Ungültiger DEM Layer.')

        dem_path = dem_layer.source()
        feedback.pushInfo(f'DEM: {dem_path}')
        feedback.pushInfo(f'Auflösung: {resolution}m | Min. Prominenz: {min_prom}')

        run_tmp_dir = tempfile.mkdtemp(prefix='sota_pm2_', dir=self._scratch_base())
        memmap_dir = self._memmap_base(run_tmp_dir)
        coarse_path = os.path.join(run_tmp_dir, f'dem_{resolution}m.tif')

        feedback.pushInfo(f'Scratch: {run_tmp_dir}')
        feedback.pushInfo(f'Memmap : {memmap_dir}')

        mm_objects = []
        run_specs = []

        try:
            # ------------------------------------------------------------
            # 1 — DEM auf Rechenauflösung resampling
            #     WICHTIG: AT_10m_PADDED.tif ist im Produktionslauf bereits
            #     ein 10-m-DEM. Dann ist ein erneutes gdal.Warp auf 10 m
            #     redundant und kann wegen der Dateigröße unnötig ein sehr
            #     großes temporäres TIFF erzeugen.
            #     Die Rechenlogik bleibt unverändert: wenn die Auflösung des
            #     Eingabe-DEM bereits exakt der Zielauflösung entspricht,
            #     verwenden wir das DEM direkt. Nur wenn die Auflösung
            #     abweicht, wird wie bisher bilinear resampelt.
            # ------------------------------------------------------------
            src_ds = gdal.Open(dem_path)
            if src_ds is None:
                raise QgsProcessingException(f'GDAL konnte das Eingabe-DEM nicht öffnen: {dem_path}')
            src_gt = src_ds.GetGeoTransform()
            src_px_x = abs(float(src_gt[1]))
            src_px_y = abs(float(src_gt[5]))
            src_ds = None

            eps = 1e-6
            if abs(src_px_x - resolution) < eps and abs(src_px_y - resolution) < eps:
                feedback.pushInfo(
                    f'Eingabe-DEM ist bereits {resolution}m — verwende Original direkt, kein zusätzliches Warp-TIFF.'
                )
                dem_open_path = dem_path
            else:
                feedback.pushInfo(f'Resampling auf {resolution}m (bilinear)...')
                warp_ds = gdal.Warp(
                    coarse_path, dem_path,
                    xRes=resolution, yRes=resolution,
                    resampleAlg='bilinear',
                    creationOptions=['COMPRESS=LZW', 'BIGTIFF=YES', 'TILED=YES']
                )
                if warp_ds is None:
                    raise QgsProcessingException('GDAL Warp fehlgeschlagen.')
                warp_ds = None
                dem_open_path = coarse_path

            # ------------------------------------------------------------
            # 2 — DEM laden
            # ------------------------------------------------------------
            ds = gdal.Open(dem_open_path)
            if ds is None:
                raise QgsProcessingException(f'GDAL konnte das resampelte DEM nicht öffnen: {coarse_path}')

            band = ds.GetRasterBand(1)
            dem = band.ReadAsArray().astype(np.float32, copy=False)
            nodata_val = band.GetNoDataValue()
            gt = ds.GetGeoTransform()   # x_min, px_w, 0, y_max, 0, -px_h
            crs_wkt = ds.GetProjection()
            ds = None

            h, w = dem.shape
            n = h * w
            feedback.pushInfo(f'DEM: {h}×{w} = {n:,} Pixel')

            valid = np.isfinite(dem)
            if nodata_val is not None:
                valid &= (dem != nodata_val)
            valid_count = int(np.count_nonzero(valid))
            feedback.pushInfo(f'Gültige Pixel: {valid_count:,}')

            # ------------------------------------------------------------
            # 3 — Lokale Maxima erkennen
            # ------------------------------------------------------------
            dem_safe = np.array(dem, copy=True)
            dem_safe[~valid] = -np.inf
            fmax = maximum_filter(dem_safe, size=3, mode='constant', cval=-np.inf)
            peak_mask = (fmax == dem_safe) & valid
            peak_count = int(np.count_nonzero(peak_mask))
            feedback.pushInfo(f'Lokale Maxima: {peak_count:,}')

            del fmax, dem_safe

            flat_dem = dem.ravel()
            flat_valid = valid.ravel()
            flat_peak = peak_mask.ravel()
            peak_indices = np.flatnonzero(flat_peak).astype(np.int64, copy=False)

            # Kein Copy: flat_dem/flat_valid/flat_peak bleiben Views.
            del valid, peak_mask

            # Exaktes regionales Minimum ohne große Vollkopie.
            global_min = self._chunked_valid_min(flat_dem, flat_valid, chunk_size=10_000_000)

            # ------------------------------------------------------------
            # 4 — Externe Sortierung + memmap-Arrays
            # ------------------------------------------------------------
            run_specs, valid_count_sorted = self._build_sorted_runs(
                flat_dem=flat_dem,
                flat_valid=flat_valid,
                n=n,
                scratch_dir=memmap_dir,
                feedback=feedback,
            )
            if valid_count_sorted != valid_count:
                raise QgsProcessingException(
                    f'Inkonsistenz im externen Sort: valid_count={valid_count:,} '
                    f'!= valid_count_sorted={valid_count_sorted:,}')

            parent = self._new_memmap(os.path.join(memmap_dir, 'parent.bin'), np.int64, (n,))
            self._init_parent(parent, n=n, chunk_size=10_000_000)
            mm_objects.append(parent)

            rank = self._new_memmap(os.path.join(memmap_dir, 'rank.bin'), np.int8, (n,), fill_value=0)
            mm_objects.append(rank)

            comp_pk_elev = self._new_memmap(
                os.path.join(memmap_dir, 'comp_pk_elev.bin'), np.float32, (n,), fill_value=np.float32(-np.inf)
            )
            mm_objects.append(comp_pk_elev)

            comp_pk_idx = self._new_memmap(
                os.path.join(memmap_dir, 'comp_pk_idx.bin'), np.int64, (n,), fill_value=-1
            )
            mm_objects.append(comp_pk_idx)

            key_col = self._new_memmap(
                os.path.join(memmap_dir, 'key_col.bin'), np.float32, (n,), fill_value=np.float32(np.nan)
            )
            mm_objects.append(key_col)

            key_col_px = self._new_memmap(
                os.path.join(memmap_dir, 'key_col_px.bin'), np.int64, (n,), fill_value=-1
            )
            mm_objects.append(key_col_px)

            processed = self._new_memmap(
                os.path.join(memmap_dir, 'processed.bin'), np.uint8, (n,), fill_value=0
            )
            mm_objects.append(processed)

            if peak_indices.size > 0:
                comp_pk_elev[peak_indices] = flat_dem[peak_indices]
                comp_pk_idx[peak_indices] = peak_indices

            feedback.pushInfo(
                f'Dtypes: parent={parent.dtype}, comp_pk_idx={comp_pk_idx.dtype}, '
                f'key_col_px={key_col_px.dtype}, peak_indices={peak_indices.dtype}'
            )
            initial_memmap_bytes = self._dir_size_bytes(memmap_dir)
            feedback.pushInfo(
                f'Run-Dateien: {len(run_specs):,} | Memmap initial: '
                f'{self._format_bytes(initial_memmap_bytes)}'
            )

            # ------------------------------------------------------------
            # 5 — Absteigendes Union-Find
            # ------------------------------------------------------------
            feedback.pushInfo('Absteigendes Union-Find läuft (memmap / externer Merge-Sort)...')

            def find(x):
                root = int(x)
                while int(parent[root]) != root:
                    root = int(parent[root])
                while int(parent[x]) != root:
                    tmp = int(parent[x])
                    parent[x] = root
                    x = tmp
                return root

            neighbors = [(-1, -1), (-1, 0), (-1, 1),
                         (0, -1),           (0, 1),
                         (1, -1),  (1, 0),  (1, 1)]
            report_every_pct = max(1, valid_count // 100)
            report_every_px = max(1, int(os.environ.get('SOTA_PROGRESS_PIXELS', '25000000')))
            next_report = min(report_every_pct, report_every_px)
            uf_start = time.time()
            n_assigned = 0

            feedback.pushInfo(
                f'UF-Progress: Bericht alle min(1%={report_every_pct:,}, '
                f'{report_every_px:,} Pixel)'
            )

            for i, flat_idx in enumerate(self._iter_sorted_indices(flat_dem, run_specs), start=1):
                if feedback.isCanceled():
                    break
                if i >= next_report or i == valid_count:
                    now = time.time()
                    elapsed = max(1e-9, now - uf_start)
                    rate = i / elapsed
                    eta = (valid_count - i) / rate if rate > 0 else float('inf')
                    feedback.setProgress(20 + int(65 * i / valid_count))
                    pct_now = 100.0 * i / valid_count
                    feedback.pushInfo(
                        f'UF {i:,} / {valid_count:,} ({pct_now:.1f}%) | '
                        f'keycols={n_assigned:,} | elapsed={self._format_seconds(elapsed)} | '
                        f'rate={rate:,.0f} px/s | eta={self._format_seconds(eta)}'
                    )
                    while next_report <= i:
                        next_report += min(report_every_pct, report_every_px)

                flat_idx = int(flat_idx)
                elev = float(flat_dem[flat_idx])
                r = flat_idx // w
                c = flat_idx % w
                processed[flat_idx] = 1

                for dr, dc in neighbors:
                    nr = r + dr
                    nc = c + dc
                    if not (0 <= nr < h and 0 <= nc < w):
                        continue
                    nidx = nr * w + nc
                    if not (processed[nidx] and flat_valid[nidx]):
                        continue

                    ra = find(flat_idx)
                    rb = find(nidx)
                    if ra == rb:
                        continue

                    ea = float(comp_pk_elev[ra])
                    ia = int(comp_pk_idx[ra])
                    eb = float(comp_pk_elev[rb])
                    ib = int(comp_pk_idx[rb])

                    # keycol VOR Union (identisch zur bisherigen Logik)
                    if ia >= 0 and ib >= 0:
                        if ea > eb and math.isnan(float(key_col[ib])):
                            key_col[ib] = elev
                            key_col_px[ib] = flat_idx
                            n_assigned += 1
                        elif eb > ea and math.isnan(float(key_col[ia])):
                            key_col[ia] = elev
                            key_col_px[ia] = flat_idx
                            n_assigned += 1
                        elif ea == eb:
                            if math.isnan(float(key_col[ia])):
                                key_col[ia] = elev
                                key_col_px[ia] = flat_idx
                                n_assigned += 1
                            if math.isnan(float(key_col[ib])):
                                key_col[ib] = elev
                                key_col_px[ib] = flat_idx
                                n_assigned += 1

                    # Union by rank (identisch)
                    if int(rank[ra]) < int(rank[rb]):
                        ra, rb = rb, ra
                        ea, eb, ia, ib = eb, ea, ib, ia
                    parent[rb] = ra
                    if int(rank[ra]) == int(rank[rb]):
                        rank[ra] = int(rank[ra]) + 1
                    if eb > ea:
                        comp_pk_elev[ra] = eb
                        comp_pk_idx[ra] = ib

            feedback.setProgress(90)
            uf_total = time.time() - uf_start
            feedback.pushInfo(
                f'keycol zugewiesen: {n_assigned:,} | Regionales Min: {global_min:.1f}m | '
                f'UF total={self._format_seconds(uf_total)} | '
                f'memmap scratch={self._format_bytes(initial_memmap_bytes)}')

            # ------------------------------------------------------------
            # 6 — Border-Polygon laden (mit CRS-Transformation)
            # ------------------------------------------------------------
            border_geom = None
            if border_path and os.path.exists(border_path):
                bl = QgsVectorLayer(border_path, 'border', 'ogr')
                if bl.isValid():
                    dem_crs = QgsCoordinateReferenceSystem()
                    dem_crs.createFromWkt(crs_wkt)
                    border_crs = bl.crs()

                    for f in bl.getFeatures():
                        geom = f.geometry()
                        if border_crs != dem_crs:
                            xform = QgsCoordinateTransform(
                                border_crs, dem_crs, QgsProject.instance())
                            geom.transform(xform)
                        border_geom = geom
                        break
                    feedback.pushInfo(
                        f"Border: {os.path.basename(border_path)} "
                        f"({border_crs.authid()} → {dem_crs.authid()})")

            # ------------------------------------------------------------
            # 7 — Output (Feldnamen kompatibel mit AT_SOTA_Refine1m)
            # ------------------------------------------------------------
            dem_crs = QgsCoordinateReferenceSystem()
            dem_crs.createFromWkt(crs_wkt)

            out_fields = QgsFields()
            out_fields.append(QgsField('zpk_1', QVariant.Double, 'double', 10, 2, ''))
            out_fields.append(QgsField('keycol', QVariant.Double, 'double', 10, 2, ''))
            out_fields.append(QgsField('prom', QVariant.Double, 'double', 10, 2, ''))
            out_fields.append(QgsField('keycol_x', QVariant.Double, 'double', 14, 2, ''))
            out_fields.append(QgsField('keycol_y', QVariant.Double, 'double', 14, 2, ''))

            sink, sink_id = self.parameterAsSink(
                parameters, self.P_OUTPUT, context,
                out_fields, QgsWkbTypes.Point, dem_crs)
            if sink is None:
                raise QgsProcessingException('Output Sink konnte nicht erstellt werden.')

            n_out = 0
            for flat_idx in peak_indices:
                fi = int(flat_idx)
                pk_elev = float(flat_dem[fi])
                kc_raw = float(key_col[fi])
                kc = kc_raw if not math.isnan(kc_raw) else global_min
                prom = pk_elev - kc

                if prom < min_prom:
                    continue

                r = fi // w
                c = fi % w
                x = gt[0] + (c + 0.5) * gt[1]
                y = gt[3] + (r + 0.5) * gt[5]

                pt = QgsGeometry.fromPointXY(QgsPointXY(x, y))
                if border_geom is not None and not border_geom.contains(pt):
                    continue

                kc_fi = int(key_col_px[fi])
                if kc_fi >= 0:
                    kc_r = kc_fi // w
                    kc_c = kc_fi % w
                    kc_x = gt[0] + (kc_c + 0.5) * gt[1]
                    kc_y = gt[3] + (kc_r + 0.5) * gt[5]
                else:
                    kc_x = kc_y = None

                out_f = QgsFeature(out_fields)
                out_f.setGeometry(pt)
                out_f.setAttributes([pk_elev, kc, prom, kc_x, kc_y])
                sink.addFeature(out_f, QgsFeatureSink.FastInsert)
                n_out += 1

            feedback.pushInfo(f'SOTA-Gipfel (prom ≥ {min_prom}m): {n_out}')
            feedback.setProgress(100)
            return {self.P_OUTPUT: sink_id}

        finally:
            for mm in mm_objects:
                try:
                    mm.flush()
                except Exception:
                    pass
                try:
                    del mm
                except Exception:
                    pass

            keep_tmp = os.environ.get('SOTA_KEEP_TMP', '').strip() == '1'
            if not keep_tmp:
                for path in [memmap_dir, run_tmp_dir]:
                    try:
                        if path and os.path.isdir(path):
                            shutil.rmtree(path, ignore_errors=True)
                    except Exception:
                        pass



def classFactory():
    return AT_SOTA_PixelMinimax()
