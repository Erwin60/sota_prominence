# -*- coding: utf-8 -*-
# Filename: AT_SOTA_Match_DB.py
# Version:  3.5
#
# PURPOSE OF THIS PATCH
# ---------------------
# The matching logic remains unchanged in principle:
#   - nearest Austrian SOTA entry within match radius
#   - status = MATCH_OK / MATCH_ELEV / NEW_CALC
#   - unmatched Austrian DB entries are appended as DB_NO_PEAK
#   - foreign entries in the border buffer are appended as FOREIGN_PEAK
#
# What changes is the analytical traceability of the output:
#   1) DB_NO_PEAK rows now receive land / land_id / BL_quelle directly
#      from the Bundeslaender polygons (BL_202504.gpkg), not from sota_ref.
#   2) border_dist_m / border_zone are also populated for DB_NO_PEAK and
#      FOREIGN_PEAK, so later audit statistics can run directly on the
#      primary layer.
#   3) MATCH_ELEV now writes explicit diagnostic fields:
#      match_elev_diff_m, bev_support, review_hint, sota_name_match.
#   4) Optional raw-peak diagnostics for DB_NO_PEAK are available already
#      in Step 5 (instead of reconstructing them later in SQL only):
#      raw_peak_found, raw_peak_dist_m, raw_peak_prom, raw_peak_fid,
#      raw_peak_keycol_x, raw_peak_keycol_y, db_no_peak_reason.
#   5) Official-point fields are written directly for later ambiguity
#      diagnosis and QGIS visualisation: official_x, official_y,
#      official_display_name, official_name_source.
#
# This patch is diagnostic / audit-oriented and does NOT alter the main
# summit/prominence computation logic.

import csv
import math
import os
import re

from qgis.PyQt.QtCore import QVariant
from qgis import processing
from qgis.core import (
    QgsProcessing, QgsProcessingAlgorithm,
    QgsProcessingParameterVectorLayer, QgsProcessingParameterFile,
    QgsProcessingParameterNumber, QgsProcessingParameterFeatureSink,
    QgsProcessingException,
    QgsFeature, QgsField, QgsFields, QgsGeometry, QgsPointXY,
    QgsCoordinateReferenceSystem, QgsCoordinateTransform, QgsProject,
    QgsWkbTypes, QgsFeatureSink, QgsSpatialIndex,
    QgsVectorLayer, QgsRectangle, QgsFeatureRequest,
)


class AT_SOTA_Match_DB(QgsProcessingAlgorithm):

    P_PEAKS = 'peaks_layer'
    P_CSV = 'sota_csv_path'
    P_RADIUS = 'match_radius'
    P_OUTPUT = 'output'

    ASSOC_FILTER = 'Austria'
    VALID_TO_FILT = '31/12/2099'
    BORDER_ZONE_THRESHOLD_M = 50.0
    BL_BORDER_ZONE_THRESHOLD_M = 50.0

    LAND_BY_PREFIX = {
        'OE/VB-': (8, 'Vorarlberg'),
        'OE/TI-': (7, 'Tirol'),
        'OE/TL-': (7, 'Tirol'),
        'OE/SB-': (5, 'Salzburg'),
        'OE/KT-': (2, 'Kärnten'),
        'OE/ST-': (6, 'Steiermark'),
        'OE/OO-': (4, 'Oberösterreich'),
        'OE/NO-': (3, 'Niederösterreich'),
        'OE/BL-': (1, 'Burgenland'),
        'OE/WI-': (9, 'Wien'),
    }

    def createInstance(self):
        return self.__class__()

    def name(self):
        return 'AT_SOTA_Match_DB'

    def displayName(self):
        return 'AT SOTA — Match mit SOTA-Datenbank v3.5 (diagnostic partner fields + position classes)'

    def group(self):
        return 'SOTA'

    def groupId(self):
        return 'SOTA'

    def initAlgorithm(self, config=None):
        self.addParameter(QgsProcessingParameterVectorLayer(
            self.P_PEAKS, 'Berechnete Peaks (AT_SOTA_Final.gpkg)',
            [QgsProcessing.TypeVectorPoint]))
        self.addParameter(QgsProcessingParameterFile(
            self.P_CSV, 'SOTA-Datenbank CSV (SOTA_2026.csv)',
            behavior=QgsProcessingParameterFile.File,
            fileFilter='CSV (*.csv)'))
        self.addParameter(QgsProcessingParameterNumber(
            self.P_RADIUS, 'Match-Radius (m)',
            type=QgsProcessingParameterNumber.Double,
            defaultValue=500.0, minValue=50.0, maxValue=2000.0))
        self.addParameter(QgsProcessingParameterFile(
            'border_gpkg',
            'Staatsgrenz GeoPackage für räumliche Filterung und border_dist_m (optional)',
            behavior=QgsProcessingParameterFile.File,
            fileFilter='GeoPackage (*.gpkg)',
            optional=True))
        self.addParameter(QgsProcessingParameterNumber(
            'neighbor_radius',
            'Radius für Nachbarland-SOTA im Buffer (m, 0 = deaktiviert)',
            type=QgsProcessingParameterNumber.Double,
            defaultValue=40000.0, minValue=0.0, maxValue=100000.0))
        self.addParameter(QgsProcessingParameterFile(
            'bl_gpkg_path',
            'Bundesländer GeoPackage (*.gpkg) — optional, für DB_NO_PEAK land_id/land via Spatial Join',
            behavior=QgsProcessingParameterFile.File,
            fileFilter='GeoPackage (*.gpkg)',
            optional=True))

        # Optional diagnostics: attach nearest raw peak information already
        # in Step 5 so later audits do not depend on virtual-layer repairs.
        self.addParameter(QgsProcessingParameterVectorLayer(
            'raw_peaks_layer',
            'Roh-Peaks (peaks_prom_raw.gpkg) — optional für DB_NO_PEAK Diagnose',
            [QgsProcessing.TypeVectorPoint], optional=True))
        self.addParameter(QgsProcessingParameterNumber(
            'raw_peak_radius',
            'DB_NO_PEAK Diagnose: Suchradius zum Roh-Peak (m)',
            type=QgsProcessingParameterNumber.Double,
            defaultValue=600.0, minValue=50.0, maxValue=5000.0))
        self.addParameter(QgsProcessingParameterNumber(
            'raw_peak_candidates',
            'DB_NO_PEAK Diagnose: Anzahl Roh-Peak-Kandidaten',
            type=QgsProcessingParameterNumber.Integer,
            defaultValue=20, minValue=3, maxValue=200))
        self.addParameter(QgsProcessingParameterNumber(
            'near_calc_radius',
            'DB_NO_PEAK Diagnose: Suchradius zum qualifizierten berechneten Gipfel (m)',
            type=QgsProcessingParameterNumber.Double,
            defaultValue=1000.0, minValue=100.0, maxValue=5000.0))
        self.addParameter(QgsProcessingParameterNumber(
            'near_calc_candidates',
            'DB_NO_PEAK Diagnose: Anzahl zu prüfender berechneter Gipfel',
            type=QgsProcessingParameterNumber.Integer,
            defaultValue=30, minValue=3, maxValue=300))

        self.addParameter(QgsProcessingParameterFeatureSink(
            self.P_OUTPUT,
            'Einheitliche Ergebnistabelle (Peaks + DB-Einträge + Nachbarn)',
            type=QgsProcessing.TypeVectorPoint))

    # ------------------------------------------------------------------
    @staticmethod
    def _open_vector(path: str, label: str):
        if not path:
            return None
        vl = QgsVectorLayer(path, label, 'ogr')
        if vl and vl.isValid():
            return vl
        return None

    @staticmethod
    def _reproject_if_needed(layer, target_crs, context, feedback, mem_name):
        if layer is None:
            return None
        if layer.crs() == target_crs:
            return layer
        return processing.run('native:reprojectlayer', {
            'INPUT': layer,
            'TARGET_CRS': target_crs.authid(),
            'OUTPUT': f'memory:{mem_name}',
        }, context=context, feedback=feedback)['OUTPUT']

    @staticmethod
    def _norm_name(value):
        if value is None:
            return ''
        s = str(value).strip().lower()
        s = s.replace('ä', 'ae').replace('ö', 'oe').replace('ü', 'ue').replace('ß', 'ss')
        s = re.sub(r'[^a-z0-9]+', ' ', s)
        return re.sub(r'\s+', ' ', s).strip()
    @staticmethod
    def _repair_mojibake(value):
        if value is None:
            return None
        s = str(value)
        if not any(tok in s for tok in ('Ã', 'Â', 'â')):
            return s
        try:
            fixed = s.encode('latin1').decode('utf-8')
            return fixed
        except Exception:
            return s

    @classmethod
    def _clean_display_name(cls, value):
        if value is None:
            return None
        s = cls._repair_mojibake(value)
        s = str(s).strip()
        return s if s else None

    @staticmethod
    def _position_class(dist_m):
        if dist_m is None:
            return None
        d = float(dist_m)
        if d <= 15.0:
            return 'EXACT_POS'
        if d <= 50.0:
            return 'VERY_NEAR_POS'
        if d <= 100.0:
            return 'NEAR_POS'
        if d <= 250.0:
            return 'RIDGE_OFFSET'
        return 'REMOTE_OFFSET'

    @staticmethod
    def _height_class(elev_diff):
        if elev_diff is None:
            return None
        d = float(elev_diff)
        if d <= 10.0:
            return 'DB_HEIGHT_OK'
        if d <= 20.0:
            return 'DB_HEIGHT_MINOR_CONFLICT'
        return 'DB_HEIGHT_MAJOR_CONFLICT'

    @classmethod
    def _match_case_class(cls, status, official_peak_dist_m, bev_support, review_hint):
        if status != 'MATCH_ELEV':
            return status
        pos = cls._position_class(official_peak_dist_m)
        if pos in ('EXACT_POS', 'VERY_NEAR_POS', 'NEAR_POS') and bev_support == 'BEV_SUPPORTS_CALC':
            return 'MATCH_ELEV_POS_OK_DB_HEIGHT_ISSUE'
        if pos in ('EXACT_POS', 'VERY_NEAR_POS', 'NEAR_POS') and bev_support == 'BEV_SUPPORTS_DB':
            return 'MATCH_ELEV_POS_OK_CALC_HEIGHT_ISSUE'
        if pos in ('EXACT_POS', 'VERY_NEAR_POS', 'NEAR_POS') and bev_support in ('NO_BEV', 'NO_BEV_AT_CALC_POINT'):
            return 'MATCH_ELEV_POS_OK_NO_BEV'
        if review_hint == 'POSSIBLE_WRONG_SUMMIT_MATCH':
            return 'MATCH_ELEV_WRONG_SUMMIT_REVIEW'
        if pos == 'RIDGE_OFFSET':
            return 'MATCH_ELEV_RIDGE_OFFSET'
        if pos == 'REMOTE_OFFSET':
            return 'MATCH_ELEV_REMOTE_OFFSET'
        return 'MATCH_ELEV_MANUAL_REVIEW'

    @classmethod
    def _land_from_sota_ref(cls, sota_ref):
        ref = str(sota_ref or '').strip().upper()
        for prefix, (land_id, land) in cls.LAND_BY_PREFIX.items():
            if ref.startswith(prefix):
                return land_id, land, 'sota_ref'
        return None, None, 'missing'

    @classmethod
    def _resolve_land(cls, land_id_val, land_val, sota_ref):
        if land_val not in (None, ''):
            return land_id_val, land_val, 'land'
        return cls._land_from_sota_ref(sota_ref)

    @staticmethod
    def _lookup_land_from_bl(geom, bl_layer, bl_idx):
        if geom is None or geom.isEmpty() or bl_layer is None or bl_idx is None:
            return None, None, None

        # 1) exact candidate search via bbox overlap
        for cid in bl_idx.intersects(geom.boundingBox()):
            bf = next(bl_layer.getFeatures(QgsFeatureRequest(cid)), None)
            if bf is None or bf.geometry() is None or bf.geometry().isEmpty():
                continue
            bg = bf.geometry()
            try:
                if bg.contains(geom) or bg.intersects(geom) or bg.distance(geom) <= 0.5:
                    return bf['land_id'], bf['land'], 'bl_join'
            except Exception:
                continue

        # 2) robust fallback for reprojected edge cases near polygon border
        pt = geom.asPoint()
        best = None
        best_d = float('inf')
        for cid in bl_idx.nearestNeighbor(pt, 3):
            bf = next(bl_layer.getFeatures(QgsFeatureRequest(cid)), None)
            if bf is None or bf.geometry() is None or bf.geometry().isEmpty():
                continue
            try:
                d = bf.geometry().distance(geom)
            except Exception:
                continue
            if math.isfinite(d) and d < best_d:
                best_d = d
                best = bf
        if best is not None and best_d <= 1.0:
            return best['land_id'], best['land'], 'bl_join'
        return None, None, None

    @classmethod
    def _border_zone(cls, dist_m):
        if dist_m is None:
            return None
        if dist_m > cls.BORDER_ZONE_THRESHOLD_M:
            return 'INNER'
        if dist_m >= 0:
            return 'BORDER_ZONE'
        return 'OUTER'

    @staticmethod
    def _distance_to_border(geom, border_layer, border_idx):
        if geom is None or geom.isEmpty() or border_layer is None or border_idx is None:
            return None
        pt = geom.asPoint()
        min_d = float('inf')
        for cid in border_idx.nearestNeighbor(pt, 5):
            bf = next(border_layer.getFeatures(QgsFeatureRequest(cid)), None)
            if bf is None or bf.geometry() is None or bf.geometry().isEmpty():
                continue
            d = geom.distance(bf.geometry())
            if math.isfinite(d) and d < min_d:
                min_d = d
        if min_d == float('inf'):
            return None
        return round(float(min_d), 1)

    @classmethod
    def _bl_border_zone(cls, dist_m):
        if dist_m is None:
            return None
        if dist_m > cls.BL_BORDER_ZONE_THRESHOLD_M:
            return 'INNER'
        if dist_m >= 0:
            return 'BORDER_ZONE'
        return 'OUTER'

    @staticmethod
    def _lookup_bl_context(geom, own_land_id, bl_layer):
        if geom is None or geom.isEmpty() or bl_layer is None or own_land_id in (None, ''):
            return None, None, None, None
        min_d = float('inf')
        best_land_id = None
        best_land = None
        for bf in bl_layer.getFeatures():
            try:
                cand_id = bf['land_id'] if bf.fieldNameIndex('land_id') >= 0 else None
                if str(cand_id) == str(own_land_id):
                    continue
                bg = bf.geometry()
                if bg is None or bg.isEmpty():
                    continue
                d = geom.distance(bg)
            except Exception:
                continue
            if math.isfinite(d) and d < min_d:
                min_d = d
                best_land_id = cand_id
                best_land = bf['land'] if bf.fieldNameIndex('land') >= 0 else None
        if min_d == float('inf'):
            return None, None, None, None
        dist = round(float(min_d), 1)
        zone = 'BORDER_ZONE' if dist <= 50.0 else 'INNER'
        return dist, zone, best_land_id, best_land

    @staticmethod
    def _admin_context(nat_zone, bl_zone):
        at_near = (nat_zone == 'BORDER_ZONE')
        bl_near = (bl_zone == 'BORDER_ZONE')
        if at_near and bl_near:
            return 'AT_AND_BL_BORDER'
        if at_near:
            return 'AT_BORDER'
        if bl_near:
            return 'BL_BORDER'
        return 'INNER'

    @staticmethod
    def _admin_review(admin_context):
        return 0 if admin_context in (None, '', 'INNER') else 1

    @staticmethod
    def _raw_peak_zone(dist_m):
        if dist_m is None:
            return 'NO_RAW_PEAK'
        d = float(dist_m)
        if d <= 100.0:
            return 'RAW_0_100'
        if d <= 300.0:
            return 'RAW_100_300'
        if d <= 600.0:
            return 'RAW_300_600'
        return 'RAW_GT_600'

    @staticmethod
    def _bev_support(z_als, z_bev, sota_elev):
        if z_bev is None or z_als is None or sota_elev is None:
            return 'NO_BEV_AT_CALC_POINT'
        d_calc = abs(float(z_bev) - float(z_als))
        d_db = abs(float(z_bev) - float(sota_elev))
        if abs(d_calc - d_db) <= 0.5:
            return 'BEV_EQUAL'
        return 'BEV_SUPPORTS_CALC' if d_calc < d_db else 'BEV_SUPPORTS_DB'

    @staticmethod
    def _review_hint(status, sota_dist_m, name_match, bev_support):
        if status != 'MATCH_ELEV':
            return None
        if bev_support == 'BEV_SUPPORTS_CALC':
            return 'LIKELY_DB_ELEV_ISSUE'
        if bev_support == 'BEV_SUPPORTS_DB':
            return 'LIKELY_CALC_ELEV_ISSUE'
        if name_match == 0 or (sota_dist_m is not None and float(sota_dist_m) > 150.0):
            return 'POSSIBLE_WRONG_SUMMIT_MATCH'
        return 'MANUAL_REVIEW'

    @staticmethod
    def _float_or_none(value):
        try:
            if value is None:
                return None
            v = float(value)
            if math.isnan(v):
                return None
            return v
        except Exception:
            return None

    @staticmethod
    def _best_feature(layer, spatial_index, geom, radius, k):
        if layer is None or spatial_index is None or geom is None or geom.isEmpty():
            return None, None
        pt = geom.asPoint()
        best_feat = None
        best_d = radius + 1.0
        for cid in spatial_index.nearestNeighbor(pt, int(k)):
            rf = next(layer.getFeatures(QgsFeatureRequest(cid)), None)
            if rf is None or rf.geometry() is None or rf.geometry().isEmpty():
                continue
            d = geom.distance(rf.geometry())
            if d <= radius and d < best_d:
                best_d = d
                best_feat = rf
        if best_feat is None:
            return None, None
        return best_feat, round(float(best_d), 1)

    @classmethod
    def _best_raw_peak(cls, raw_layer, raw_idx, geom, radius, k):
        return cls._best_feature(raw_layer, raw_idx, geom, radius, k)

    # ------------------------------------------------------------------
    def processAlgorithm(self, parameters, context, feedback):
        peaks = self.parameterAsVectorLayer(parameters, self.P_PEAKS, context)
        csv_path = self.parameterAsFile(parameters, self.P_CSV, context)
        radius = float(self.parameterAsDouble(parameters, self.P_RADIUS, context))
        neighbor_radius = float(self.parameterAsDouble(parameters, 'neighbor_radius', context))
        border_gpkg_path = self.parameterAsFile(parameters, 'border_gpkg', context) or ''
        bl_gpkg_path = self.parameterAsFile(parameters, 'bl_gpkg_path', context) or ''
        raw_layer = self.parameterAsVectorLayer(parameters, 'raw_peaks_layer', context)
        raw_peak_radius = float(self.parameterAsDouble(parameters, 'raw_peak_radius', context))
        raw_peak_candidates = int(self.parameterAsInt(parameters, 'raw_peak_candidates', context))
        near_calc_radius = float(self.parameterAsDouble(parameters, 'near_calc_radius', context))
        near_calc_candidates = int(self.parameterAsInt(parameters, 'near_calc_candidates', context))

        if not peaks or not peaks.isValid():
            raise QgsProcessingException('Ungültiger Peak-Layer.')
        if not csv_path or not os.path.exists(csv_path):
            raise QgsProcessingException(f'CSV nicht gefunden: {csv_path}')

        # Optional Bundesländer-Layer for authoritative land_id/land.
        bl_layer = None
        bl_idx = None
        if bl_gpkg_path:
            bl_layer = self._open_vector(bl_gpkg_path, 'bundeslaender')
            if bl_layer and bl_layer.isValid():
                bl_layer = self._reproject_if_needed(bl_layer, peaks.crs(), context, feedback, 'bl_reproj_matchdb')
                bl_fields = [f.name() for f in bl_layer.fields()]
                for req in ('land_id', 'land'):
                    if req not in bl_fields:
                        raise QgsProcessingException(
                            f"Pflichtfeld '{req}' fehlt im Bundesländer-Layer. Vorhandene Felder: {bl_fields}")
                bl_idx = QgsSpatialIndex(bl_layer.getFeatures())
                feedback.pushInfo('Bundesländer-Layer für land_id/land aktiv.')
            else:
                feedback.pushWarning('Bundesländer-Layer konnte nicht geöffnet werden.')
                bl_layer = None
                bl_idx = None

        # Optional border layer for border_dist_m / border_zone. Use the border line,
        # not the polygon interior, so inside points do not get distance 0 by definition.
        border_layer = None
        border_idx = None
        if border_gpkg_path:
            border_layer = self._open_vector(border_gpkg_path, 'border')
            if border_layer and border_layer.isValid():
                border_layer = self._reproject_if_needed(border_layer, peaks.crs(), context, feedback, 'border_reproj_matchdb')
                border_layer = processing.run('native:polygonstolines', {
                    'INPUT': border_layer,
                    'OUTPUT': 'memory:border_lines_matchdb',
                }, context=context, feedback=feedback)['OUTPUT']
                border_idx = QgsSpatialIndex(border_layer.getFeatures())
                feedback.pushInfo('Border-Layer für border_dist_m aktiv (Grenzlinie).')
            else:
                feedback.pushWarning('Border-Layer konnte nicht für border_dist_m geöffnet werden.')
                border_layer = None
                border_idx = None

        raw_idx = QgsSpatialIndex(raw_layer.getFeatures()) if raw_layer is not None else None
        if raw_idx is not None:
            feedback.pushInfo(
                f'Roh-Peak-Diagnose aktiv: Radius {raw_peak_radius:.0f} m, '
                f'Kandidaten {raw_peak_candidates}.')
        feedback.pushInfo(
            f'Nearest-CALC-Diagnose aktiv: Radius {near_calc_radius:.0f} m, '
            f'Kandidaten {near_calc_candidates}.')

        # ----------------------------------------------------------------
        # 1 — SOTA-CSV laden und filtern
        # ----------------------------------------------------------------
        feedback.pushInfo(f'Lade SOTA-CSV: {os.path.basename(csv_path)}')
        sota_list = []

        with open(csv_path, newline='', encoding='utf-8-sig') as f:
            reader = csv.DictReader(f)
            headers = reader.fieldnames or []

            def _col(*candidates):
                for c in candidates:
                    for h in headers:
                        if h.strip().lower() == c.lower():
                            return h
                return None

            c_assoc = _col('AssociationName', 'Association')
            c_valid = _col('ValidTo', 'valid_to', 'validto')
            c_code = _col('SummitCode', 'summitcode', 'summit_code', 'Code')
            c_name = _col('SummitName', 'summitname', 'summit_name', 'Name')
            c_alt = _col('AltM', 'altm', 'Altitude', 'altitude', 'elev_m')
            c_lat = _col('Latitude', 'latitude', 'lat')
            c_lon = _col('Longitude', 'longitude', 'lon', 'lng')

            missing = [n for n, v in [
                ('AssociationName', c_assoc), ('ValidTo', c_valid),
                ('SummitCode', c_code), ('SummitName', c_name),
                ('AltM', c_alt), ('Latitude', c_lat), ('Longitude', c_lon)
            ] if v is None]
            if missing:
                raise QgsProcessingException(
                    f'Spalten fehlen in CSV: {missing}\nVorhanden: {headers}')

            for row in reader:
                valid_to = (row.get(c_valid) or '').strip()
                assoc = (row.get(c_assoc) or '').strip()
                is_austria = (assoc == self.ASSOC_FILTER)
                is_valid = (valid_to == self.VALID_TO_FILT)
                if not (is_valid and (is_austria or neighbor_radius > 0)):
                    continue
                try:
                    lat = float(row[c_lat])
                    lon = float(row[c_lon])
                    alt = float(row[c_alt]) if row.get(c_alt) else None
                except (ValueError, TypeError):
                    continue
                sota_list.append({
                    'code': (row.get(c_code) or '').strip(),
                    'name': (row.get(c_name) or '').strip(),
                    'altm': alt,
                    'lat': lat,
                    'lon': lon,
                    'assoc': assoc,
                    'is_foreign': not is_austria,
                    'matched': False,
                })

        at_count = sum(1 for r in sota_list if not r['is_foreign'])
        fgn_count = sum(1 for r in sota_list if r['is_foreign'])
        feedback.pushInfo(f'  {at_count} Austria-Einträge, {fgn_count} Nachbarland-Einträge geladen')

        # ----------------------------------------------------------------
        # 2 — SOTA-Punkte in Peak-CRS projizieren + Spatial Index
        # ----------------------------------------------------------------
        peaks_crs = peaks.crs()
        xform = QgsCoordinateTransform(
            QgsCoordinateReferenceSystem('EPSG:4326'),
            peaks_crs, QgsProject.instance())

        sota_geoms = []
        sindex = QgsSpatialIndex()
        for i, rec in enumerate(sota_list):
            try:
                pt = xform.transform(QgsPointXY(rec['lon'], rec['lat']))
            except Exception:
                sota_geoms.append(None)
                continue
            geom = QgsGeometry.fromPointXY(pt)
            sota_geoms.append(geom)
            qf = QgsFeature()
            qf.setId(i)
            qf.setGeometry(geom)
            sindex.addFeature(qf)

        # ----------------------------------------------------------------
        # 3 — Output-Schema
        # ----------------------------------------------------------------
        out_fields = QgsFields()
        for fld in peaks.fields():
            out_fields.append(fld)

        def _add(name, typ, tn='', ln=0, pr=0):
            if out_fields.indexFromName(name) < 0:
                out_fields.append(QgsField(name, typ, tn, ln, pr, ''))

        _add('row_origin', QVariant.String, 'text', 24)
        _add('sota_status', QVariant.String, 'text', 20)
        _add('sota_ref', QVariant.String, 'text', 30)
        _add('sota_name', QVariant.String, 'text', 100)
        _add('sota_assoc', QVariant.String, 'text', 60)
        _add('sota_elev', QVariant.Double, 'double', 10, 1)
        _add('official_x', QVariant.Double, 'double', 14, 3)
        _add('official_y', QVariant.Double, 'double', 14, 3)
        _add('official_display_name', QVariant.String, 'text', 120)
        _add('official_name_source', QVariant.String, 'text', 16)
        _add('sota_dist_m', QVariant.Double, 'double', 10, 1)
        _add('match_elev_diff_m', QVariant.Double, 'double', 10, 1)
        _add('sota_name_match', QVariant.Int, 'integer', 1)
        _add('bev_support', QVariant.String, 'text', 40)
        _add('bev_support_note', QVariant.String, 'text', 160)
        _add('review_hint', QVariant.String, 'text', 40)
        _add('BL_quelle', QVariant.String, 'text', 12)
        _add('bl_border_dist_m', QVariant.Double, 'double', 10, 1)
        _add('bl_border_zone', QVariant.String, 'text', 20)
        _add('neighbor_land_id', QVariant.Int, 'integer', 4)
        _add('neighbor_land', QVariant.String, 'text', 60)
        _add('admin_context', QVariant.String, 'text', 24)
        _add('admin_review', QVariant.Int, 'integer', 1)
        _add('raw_peak_found', QVariant.Int, 'integer', 1)
        _add('raw_peak_zone', QVariant.String, 'text', 16)
        _add('raw_peak_fid', QVariant.Int, 'integer', 10)
        _add('raw_peak_dist_m', QVariant.Double, 'double', 10, 1)
        _add('raw_peak_prom', QVariant.Double, 'double', 10, 1)
        _add('raw_peak_keycol_x', QVariant.Double, 'double', 14, 3)
        _add('raw_peak_keycol_y', QVariant.Double, 'double', 14, 3)
        _add('db_no_peak_reason', QVariant.String, 'text', 32)
        _add('official_peak_dist_m', QVariant.Double, 'double', 10, 1)
        _add('match_position_class', QVariant.String, 'text', 28)
        _add('match_height_class', QVariant.String, 'text', 32)
        _add('match_case_class', QVariant.String, 'text', 40)
        _add('nearest_calc_found', QVariant.Int, 'integer', 1)
        _add('nearest_calc_fid', QVariant.Int, 'integer', 10)
        _add('nearest_calc_dist_m', QVariant.Double, 'double', 10, 1)
        _add('nearest_calc_name', QVariant.String, 'text', 120)
        _add('nearest_calc_ref', QVariant.String, 'text', 30)
        _add('nearest_calc_status', QVariant.String, 'text', 24)
        _add('nearest_calc_z', QVariant.Double, 'double', 10, 1)
        _add('nearest_calc_prom', QVariant.Double, 'double', 10, 1)
        _add('nearest_calc_has_keycol', QVariant.Int, 'integer', 1)
        _add('nearest_calc_keycol_x', QVariant.Double, 'double', 14, 3)
        _add('nearest_calc_keycol_y', QVariant.Double, 'double', 14, 3)

        sink, sink_id = self.parameterAsSink(
            parameters, self.P_OUTPUT, context,
            out_fields, QgsWkbTypes.Point, peaks_crs)
        if sink is None:
            raise QgsProcessingException('Output Sink nicht erstellbar.')

        n_fields_peaks = len(peaks.fields())
        idx = out_fields.indexFromName

        peak_index = QgsSpatialIndex()
        peak_feature_cache = {}
        peak_diag_by_fid = {}
        for pf in peaks.getFeatures():
            if pf.geometry() is None or pf.geometry().isEmpty():
                continue
            peak_feature_cache[int(pf.id())] = pf
            qf = QgsFeature()
            qf.setId(int(pf.id()))
            qf.setGeometry(pf.geometry())
            peak_index.addFeature(qf)

        # ----------------------------------------------------------------
        # 4 — Berechnete Peaks matchen
        # ----------------------------------------------------------------
        n_ok = n_elev = n_new = 0
        zpk_field = 'zpk_1' if peaks.fields().indexFromName('zpk_1') >= 0 else None
        z1m_field = 'z1m_max' if peaks.fields().indexFromName('z1m_max') >= 0 else None

        for pf in peaks.getFeatures():
            if feedback.isCanceled():
                break

            pt = pf.geometry().asPoint()
            zpk = self._float_or_none(pf[zpk_field]) if zpk_field else None
            z_als = self._float_or_none(pf[z1m_field]) if z1m_field and pf[z1m_field] is not None else zpk

            best_d, best_i = radius + 1.0, -1
            for cid in sindex.nearestNeighbor(pt, 5):
                if sota_geoms[cid] is None or sota_list[cid]['is_foreign']:
                    continue
                d = pf.geometry().distance(sota_geoms[cid])
                if d <= radius and d < best_d:
                    best_d, best_i = d, cid

            if best_i >= 0:
                rec = sota_list[best_i]
                rec['matched'] = True
                sota_ref = rec['code']
                sota_name = rec['name']
                sota_assoc = rec['assoc']
                sota_elev = rec['altm']
                sota_dist = round(float(best_d), 1)
                official_geom = sota_geoms[best_i] if best_i < len(sota_geoms) else None
                elev_diff = abs((zpk or 0.0) - (sota_elev or 0.0)) if (zpk is not None and sota_elev is not None) else None
                status = 'MATCH_OK' if elev_diff is not None and elev_diff <= 10.0 else 'MATCH_ELEV'
                if status == 'MATCH_OK':
                    n_ok += 1
                else:
                    n_elev += 1
                name_match = 1 if self._norm_name(pf['NAME']) and self._norm_name(pf['NAME']) == self._norm_name(sota_name) else 0
                bev_support = self._bev_support(z_als, self._float_or_none(pf['z_bev']) if peaks.fields().indexFromName('z_bev') >= 0 else None, sota_elev)
                review_hint = self._review_hint(status, sota_dist, name_match, bev_support)
                bev_support_note = 'bev_support is calc-point height/name evidence only; official BEV anchor evidence is consolidated in Step 5c.'
                official_display_name = self._clean_display_name(pf['NAME']) if peaks.fields().indexFromName('NAME') >= 0 and pf['NAME'] not in (None, '') else self._clean_display_name(sota_name)
                official_name_source = 'BEV_NAME' if peaks.fields().indexFromName('NAME') >= 0 and pf['NAME'] not in (None, '') else ('SOTA_DB' if self._clean_display_name(sota_name) not in (None, '') else 'UNKNOWN')
                official_peak_dist = sota_dist
                match_position_class = self._position_class(official_peak_dist)
                match_height_class = self._height_class(elev_diff)
                match_case_class = self._match_case_class(status, official_peak_dist, bev_support, review_hint)
            else:
                status = 'NEW_CALC'
                n_new += 1
                sota_ref = sota_name = sota_assoc = None
                sota_elev = sota_dist = elev_diff = None
                name_match = None
                bev_support = 'NO_BEV_AT_CALC_POINT'
                review_hint = None
                bev_support_note = 'No BEV 7302/7303 at calculated point; official BEV anchor evidence is consolidated in Step 5c.'
                official_geom = None
                official_display_name = self._clean_display_name(pf['NAME']) if peaks.fields().indexFromName('NAME') >= 0 and pf['NAME'] not in (None, '') else None
                official_name_source = 'BEV_NAME' if official_display_name not in (None, '') else None
                official_peak_dist = None
                match_position_class = None
                match_height_class = None
                match_case_class = 'NEW_CALC'

            out_f = QgsFeature(out_fields)
            out_f.setGeometry(pf.geometry())
            attrs = list(pf.attributes()) + [None] * (out_fields.count() - n_fields_peaks)

            land_id_in = pf['land_id'] if pf.fieldNameIndex('land_id') >= 0 else None
            land_in = pf['land'] if pf.fieldNameIndex('land') >= 0 else None
            border_dist_in = pf['border_dist_m'] if pf.fieldNameIndex('border_dist_m') >= 0 else None
            border_zone_in = pf['border_zone'] if pf.fieldNameIndex('border_zone') >= 0 else None
            bl_border_dist_in = pf['bl_border_dist_m'] if pf.fieldNameIndex('bl_border_dist_m') >= 0 else None
            bl_border_zone_in = pf['bl_border_zone'] if pf.fieldNameIndex('bl_border_zone') >= 0 else None
            neighbor_land_id_in = pf['neighbor_land_id'] if pf.fieldNameIndex('neighbor_land_id') >= 0 else None
            neighbor_land_in = pf['neighbor_land'] if pf.fieldNameIndex('neighbor_land') >= 0 else None
            admin_context_in = pf['admin_context'] if pf.fieldNameIndex('admin_context') >= 0 else None
            admin_review_in = pf['admin_review'] if pf.fieldNameIndex('admin_review') >= 0 else None

            if land_in in (None, '') and bl_idx is not None:
                land_id_bl, land_bl, bl_quelle_val = self._lookup_land_from_bl(pf.geometry(), bl_layer, bl_idx)
                if land_bl not in (None, ''):
                    land_id_in, land_in = land_id_bl, land_bl
                else:
                    _lid, _land, bl_quelle_val = self._resolve_land(land_id_in, land_in, sota_ref)
                    land_id_in = _lid if land_id_in in (None, '') else land_id_in
                    land_in = _land if land_in in (None, '') else land_in
            else:
                _lid, _land, bl_quelle_val = self._resolve_land(land_id_in, land_in, sota_ref)
                land_id_in = land_id_in if land_id_in not in (None, '') else _lid
                land_in = land_in if land_in not in (None, '') else _land

            if border_dist_in in (None, '') and border_idx is not None:
                border_dist_in = self._distance_to_border(pf.geometry(), border_layer, border_idx)
            if border_zone_in in (None, ''):
                border_zone_in = self._border_zone(self._float_or_none(border_dist_in))

            if (bl_border_dist_in in (None, '') or bl_border_zone_in in (None, '') or
                    neighbor_land_id_in in (None, '') or neighbor_land_in in (None, '')) and bl_layer is not None:
                _bld, _blz, _nid, _nland = self._lookup_bl_context(pf.geometry(), land_id_in, bl_layer)
                if bl_border_dist_in in (None, ''):
                    bl_border_dist_in = _bld
                if bl_border_zone_in in (None, ''):
                    bl_border_zone_in = _blz
                if neighbor_land_id_in in (None, ''):
                    neighbor_land_id_in = _nid
                if neighbor_land_in in (None, ''):
                    neighbor_land_in = _nland

            if admin_context_in in (None, ''):
                admin_context_in = self._admin_context(border_zone_in, bl_border_zone_in)
            if admin_review_in in (None, ''):
                admin_review_in = self._admin_review(admin_context_in)

            attrs[idx('row_origin')] = 'CALC_PEAK'
            attrs[idx('sota_status')] = status
            attrs[idx('sota_ref')] = sota_ref
            attrs[idx('sota_name')] = sota_name
            attrs[idx('sota_assoc')] = sota_assoc
            attrs[idx('sota_elev')] = sota_elev
            if idx('official_x') >= 0:
                attrs[idx('official_x')] = official_geom.asPoint().x() if official_geom is not None and not official_geom.isEmpty() else None
            if idx('official_y') >= 0:
                attrs[idx('official_y')] = official_geom.asPoint().y() if official_geom is not None and not official_geom.isEmpty() else None
            if idx('official_display_name') >= 0:
                attrs[idx('official_display_name')] = official_display_name
            if idx('official_name_source') >= 0:
                attrs[idx('official_name_source')] = official_name_source
            attrs[idx('sota_dist_m')] = sota_dist
            attrs[idx('match_elev_diff_m')] = elev_diff
            attrs[idx('sota_name_match')] = name_match
            attrs[idx('bev_support')] = bev_support
            if idx('bev_support_note') >= 0:
                attrs[idx('bev_support_note')] = bev_support_note
            attrs[idx('review_hint')] = review_hint
            if idx('official_peak_dist_m') >= 0:
                attrs[idx('official_peak_dist_m')] = official_peak_dist
            if idx('match_position_class') >= 0:
                attrs[idx('match_position_class')] = match_position_class
            if idx('match_height_class') >= 0:
                attrs[idx('match_height_class')] = match_height_class
            if idx('match_case_class') >= 0:
                attrs[idx('match_case_class')] = match_case_class
            if idx('land_id') >= 0:
                attrs[idx('land_id')] = land_id_in
            if idx('land') >= 0:
                attrs[idx('land')] = land_in
            if idx('border_dist_m') >= 0:
                attrs[idx('border_dist_m')] = border_dist_in
            if idx('border_zone') >= 0:
                attrs[idx('border_zone')] = border_zone_in
            if idx('bl_border_dist_m') >= 0:
                attrs[idx('bl_border_dist_m')] = bl_border_dist_in
            if idx('bl_border_zone') >= 0:
                attrs[idx('bl_border_zone')] = bl_border_zone_in
            if idx('neighbor_land_id') >= 0:
                attrs[idx('neighbor_land_id')] = neighbor_land_id_in
            if idx('neighbor_land') >= 0:
                attrs[idx('neighbor_land')] = neighbor_land_in
            if idx('admin_context') >= 0:
                attrs[idx('admin_context')] = admin_context_in
            if idx('admin_review') >= 0:
                attrs[idx('admin_review')] = admin_review_in
            attrs[idx('BL_quelle')] = bl_quelle_val
            out_f.setAttributes(attrs)
            sink.addFeature(out_f, QgsFeatureSink.FastInsert)
            peak_diag_by_fid[int(pf.id())] = {
                'status': status,
                'sota_ref': sota_ref,
                'sota_name': sota_name,
                'z': z_als if z_als is not None else zpk,
                'prom': self._float_or_none(pf['prom_ref']) if pf.fieldNameIndex('prom_ref') >= 0 else None,
                'keycol_x': self._float_or_none(pf['keycol_x']) if pf.fieldNameIndex('keycol_x') >= 0 else None,
                'keycol_y': self._float_or_none(pf['keycol_y']) if pf.fieldNameIndex('keycol_y') >= 0 else None,
                'display_name': official_display_name,
            }

        # ----------------------------------------------------------------
        # 5 — Filtergeometrie für DB_NO_PEAK / FOREIGN_PEAK
        # ----------------------------------------------------------------
        _filter_geom = None
        if border_gpkg_path:
            try:
                buf_result = processing.run('native:buffer', {
                    'INPUT': border_gpkg_path,
                    'DISTANCE': neighbor_radius if neighbor_radius > 0 else 1000,
                    'SEGMENTS': 8,
                    'OUTPUT': 'memory:filter_buf',
                }, context=context, feedback=feedback)['OUTPUT']
                if buf_result.crs() != peaks.crs():
                    buf_result = processing.run('native:reprojectlayer', {
                        'INPUT': buf_result,
                        'TARGET_CRS': peaks.crs().authid(),
                        'OUTPUT': 'memory:filter_reproj',
                    }, context=context, feedback=feedback)['OUTPUT']
                geoms = [f.geometry() for f in buf_result.getFeatures()
                         if f.geometry() and not f.geometry().isNull()]
                if geoms:
                    _filter_geom = geoms[0]
                    for g in geoms[1:]:
                        _filter_geom = _filter_geom.combine(g)
                feedback.pushInfo(
                    f'  Raumfilter: Border-Buffer {neighbor_radius:.0f} m '
                    f"({'gültig' if _filter_geom and not _filter_geom.isNull() else 'UNGÜLTIG'})")
            except Exception as e:
                feedback.pushWarning(f'  Raumfilter konnte nicht erstellt werden: {e}')

        if _filter_geom is None or _filter_geom.isNull():
            ext = peaks.extent()
            buf = neighbor_radius if neighbor_radius > 0 else 5000
            rect = QgsRectangle(
                ext.xMinimum() - buf, ext.yMinimum() - buf,
                ext.xMaximum() + buf, ext.yMaximum() + buf)
            _filter_geom = QgsGeometry.fromRect(rect)
            feedback.pushInfo(f'  Raumfilter: Bounding-Box-Fallback ({buf:.0f} m)')

        # ----------------------------------------------------------------
        # 6 — Austrian DB entries without computed peak → DB_NO_PEAK
        # ----------------------------------------------------------------
        n_db_no_peak = 0
        n_db_with_raw = 0
        n_foreign = 0

        for i, rec in enumerate(sota_list):
            if sota_geoms[i] is None or rec.get('is_foreign', False) or rec['matched']:
                continue
            pt_geom = sota_geoms[i]
            if _filter_geom and not _filter_geom.isNull() and not _filter_geom.contains(pt_geom):
                continue

            land_id_val, land_val, bl_quelle = self._lookup_land_from_bl(pt_geom, bl_layer, bl_idx)
            if land_val in (None, ''):
                land_id_val, land_val, bl_quelle = self._land_from_sota_ref(rec['code'])
            border_dist = self._distance_to_border(pt_geom, border_layer, border_idx)
            border_zone = self._border_zone(border_dist)
            bl_border_dist, bl_border_zone, neighbor_land_id, neighbor_land = self._lookup_bl_context(pt_geom, land_id_val, bl_layer)
            admin_context = self._admin_context(border_zone, bl_border_zone)
            admin_review = self._admin_review(admin_context)
            calc_feat, calc_dist = self._best_feature(peaks, peak_index, pt_geom, near_calc_radius, near_calc_candidates)
            calc_meta = peak_diag_by_fid.get(int(calc_feat.id())) if calc_feat is not None else None
            calc_found = 1 if calc_feat is not None else 0
            calc_fid = int(calc_feat.id()) if calc_feat is not None else None
            calc_name = None
            if calc_meta is not None and calc_meta.get('display_name') not in (None, ''):
                calc_name = calc_meta.get('display_name')
            elif calc_feat is not None and calc_feat.fieldNameIndex('NAME') >= 0:
                calc_name = self._clean_display_name(calc_feat['NAME'])
            calc_ref = calc_meta.get('sota_ref') if calc_meta is not None else None
            calc_status = calc_meta.get('status') if calc_meta is not None else None
            calc_z = calc_meta.get('z') if calc_meta is not None else (self._float_or_none(calc_feat['z1m_max']) if calc_feat is not None and calc_feat.fieldNameIndex('z1m_max') >= 0 else None)
            calc_prom = calc_meta.get('prom') if calc_meta is not None else (self._float_or_none(calc_feat['prom_ref']) if calc_feat is not None and calc_feat.fieldNameIndex('prom_ref') >= 0 else None)
            calc_kx = calc_meta.get('keycol_x') if calc_meta is not None else (self._float_or_none(calc_feat['keycol_x']) if calc_feat is not None and calc_feat.fieldNameIndex('keycol_x') >= 0 else None)
            calc_ky = calc_meta.get('keycol_y') if calc_meta is not None else (self._float_or_none(calc_feat['keycol_y']) if calc_feat is not None and calc_feat.fieldNameIndex('keycol_y') >= 0 else None)
            calc_has_keycol = 1 if calc_kx is not None and calc_ky is not None else 0
            raw_feat, raw_dist = self._best_raw_peak(raw_layer, raw_idx, pt_geom, raw_peak_radius, raw_peak_candidates)
            raw_found = 1 if raw_feat is not None else 0
            raw_peak_fid = int(raw_feat.id()) if raw_feat is not None else None
            raw_prom = self._float_or_none(raw_feat['prom']) if raw_feat is not None and raw_feat.fieldNameIndex('prom') >= 0 else None
            raw_kx = self._float_or_none(raw_feat['keycol_x']) if raw_feat is not None and raw_feat.fieldNameIndex('keycol_x') >= 0 else None
            raw_ky = self._float_or_none(raw_feat['keycol_y']) if raw_feat is not None and raw_feat.fieldNameIndex('keycol_y') >= 0 else None
            if raw_feat is None and calc_feat is None:
                db_reason = 'NO_RAW_PEAK_NO_CALC_IN_RADIUS'
            elif raw_feat is None and calc_feat is not None:
                db_reason = 'NO_RAW_PEAK_NEAR_CALC'
            elif raw_kx is None or raw_ky is None:
                db_reason = 'RAW_PEAK_NO_KEYCOL'
            else:
                db_reason = 'RAW_PEAK_WITH_KEYCOL'
                n_db_with_raw += 1

            n_db_no_peak += 1
            out_f = QgsFeature(out_fields)
            out_f.setGeometry(pt_geom)
            attrs = [None] * out_fields.count()
            attrs[idx('row_origin')] = 'SOTA_DB_ONLY'
            attrs[idx('land_id')] = land_id_val if idx('land_id') >= 0 else None
            attrs[idx('land')] = land_val if idx('land') >= 0 else None
            attrs[idx('border_dist_m')] = border_dist if idx('border_dist_m') >= 0 else None
            attrs[idx('border_zone')] = border_zone if idx('border_zone') >= 0 else None
            attrs[idx('sota_status')] = 'DB_NO_PEAK'
            attrs[idx('sota_ref')] = rec['code']
            attrs[idx('sota_name')] = rec['name']
            attrs[idx('sota_assoc')] = rec['assoc']
            attrs[idx('sota_elev')] = rec['altm']
            if idx('official_x') >= 0:
                attrs[idx('official_x')] = pt_geom.asPoint().x()
            if idx('official_y') >= 0:
                attrs[idx('official_y')] = pt_geom.asPoint().y()
            if idx('official_display_name') >= 0:
                attrs[idx('official_display_name')] = self._clean_display_name(rec['name'])
            if idx('official_name_source') >= 0:
                attrs[idx('official_name_source')] = 'SOTA_DB' if self._clean_display_name(rec['name']) not in (None, '') else 'UNKNOWN'
            attrs[idx('BL_quelle')] = bl_quelle
            if idx('bl_border_dist_m') >= 0:
                attrs[idx('bl_border_dist_m')] = bl_border_dist
            if idx('bl_border_zone') >= 0:
                attrs[idx('bl_border_zone')] = bl_border_zone
            if idx('neighbor_land_id') >= 0:
                attrs[idx('neighbor_land_id')] = neighbor_land_id
            if idx('neighbor_land') >= 0:
                attrs[idx('neighbor_land')] = neighbor_land
            if idx('admin_context') >= 0:
                attrs[idx('admin_context')] = admin_context
            if idx('admin_review') >= 0:
                attrs[idx('admin_review')] = admin_review
            attrs[idx('raw_peak_found')] = raw_found
            if idx('raw_peak_zone') >= 0:
                attrs[idx('raw_peak_zone')] = self._raw_peak_zone(raw_dist)
            attrs[idx('raw_peak_fid')] = raw_peak_fid
            attrs[idx('raw_peak_dist_m')] = raw_dist
            attrs[idx('raw_peak_prom')] = raw_prom
            attrs[idx('raw_peak_keycol_x')] = raw_kx
            attrs[idx('raw_peak_keycol_y')] = raw_ky
            attrs[idx('db_no_peak_reason')] = db_reason
            if idx('nearest_calc_found') >= 0:
                attrs[idx('nearest_calc_found')] = calc_found
            if idx('nearest_calc_fid') >= 0:
                attrs[idx('nearest_calc_fid')] = calc_fid
            if idx('nearest_calc_dist_m') >= 0:
                attrs[idx('nearest_calc_dist_m')] = calc_dist
            if idx('nearest_calc_name') >= 0:
                attrs[idx('nearest_calc_name')] = calc_name
            if idx('nearest_calc_ref') >= 0:
                attrs[idx('nearest_calc_ref')] = calc_ref
            if idx('nearest_calc_status') >= 0:
                attrs[idx('nearest_calc_status')] = calc_status
            if idx('nearest_calc_z') >= 0:
                attrs[idx('nearest_calc_z')] = calc_z
            if idx('nearest_calc_prom') >= 0:
                attrs[idx('nearest_calc_prom')] = calc_prom
            if idx('nearest_calc_has_keycol') >= 0:
                attrs[idx('nearest_calc_has_keycol')] = calc_has_keycol
            if idx('nearest_calc_keycol_x') >= 0:
                attrs[idx('nearest_calc_keycol_x')] = calc_kx
            if idx('nearest_calc_keycol_y') >= 0:
                attrs[idx('nearest_calc_keycol_y')] = calc_ky
            out_f.setAttributes(attrs)
            sink.addFeature(out_f, QgsFeatureSink.FastInsert)
            peak_diag_by_fid[int(pf.id())] = {
                'status': status,
                'sota_ref': sota_ref,
                'sota_name': sota_name,
                'z': z_als if z_als is not None else zpk,
                'prom': self._float_or_none(pf['prom_ref']) if pf.fieldNameIndex('prom_ref') >= 0 else None,
                'keycol_x': self._float_or_none(pf['keycol_x']) if pf.fieldNameIndex('keycol_x') >= 0 else None,
                'keycol_y': self._float_or_none(pf['keycol_y']) if pf.fieldNameIndex('keycol_y') >= 0 else None,
                'display_name': official_display_name,
            }

        # ----------------------------------------------------------------
        # 7 — Foreign peaks in border buffer → FOREIGN_PEAK
        # ----------------------------------------------------------------
        if neighbor_radius > 0 and _filter_geom and not _filter_geom.isNull():
            for i, rec in enumerate(sota_list):
                if sota_geoms[i] is None or not rec.get('is_foreign', False):
                    continue
                pt_geom = sota_geoms[i]
                try:
                    if not _filter_geom.contains(pt_geom):
                        continue
                except Exception:
                    continue
                n_foreign += 1
                border_dist = self._distance_to_border(pt_geom, border_layer, border_idx)
                border_zone = self._border_zone(border_dist)
                admin_context = 'AT_BORDER' if border_zone == 'BORDER_ZONE' else 'OUTER_BUFFER'
                out_f = QgsFeature(out_fields)
                out_f.setGeometry(pt_geom)
                attrs = [None] * out_fields.count()
                attrs[idx('row_origin')] = 'FOREIGN_DB_CONTEXT'
                attrs[idx('border_dist_m')] = border_dist if idx('border_dist_m') >= 0 else None
                attrs[idx('border_zone')] = border_zone if idx('border_zone') >= 0 else None
                if idx('admin_context') >= 0:
                    attrs[idx('admin_context')] = admin_context
                if idx('admin_review') >= 0:
                    attrs[idx('admin_review')] = self._admin_review(admin_context)
                attrs[idx('sota_status')] = 'FOREIGN_PEAK'
                attrs[idx('sota_ref')] = rec['code']
                attrs[idx('sota_name')] = rec['name']
                attrs[idx('sota_assoc')] = rec['assoc']
                attrs[idx('sota_elev')] = rec['altm']
                attrs[idx('BL_quelle')] = 'missing'
                out_f.setAttributes(attrs)
                sink.addFeature(out_f, QgsFeatureSink.FastInsert)

        # ----------------------------------------------------------------
        # 8 — Summary
        # ----------------------------------------------------------------
        total_calc = n_ok + n_elev + n_new
        feedback.pushInfo('\n=== SOTA-DB Abgleich ===')
        feedback.pushInfo(f'  MATCH_OK    (grün):  {n_ok}')
        feedback.pushInfo(f'  MATCH_ELEV  (orange):{n_elev}  ← Höhenabw. > 10m, Diagnosefelder gesetzt')
        feedback.pushInfo(f'  NEW_CALC    (blau):  {n_new}  ← Kandidaten, nicht in DB')
        feedback.pushInfo(f'  DB_NO_PEAK  (rot):   {n_db_no_peak}  ← In DB, kein berechneter Peak')
        if raw_idx is not None:
            feedback.pushInfo(f'  DB_NO_PEAK mit Roh-Peak-Diagnose: {n_db_with_raw}')
        total_out = total_calc + n_db_no_peak + n_foreign
        feedback.pushInfo(f'  Zeilen gesamt:       {total_out}')
        if at_count > 0:
            cov = 100.0 * (n_ok + n_elev) / at_count
            feedback.pushInfo(f'  DB-Abdeckung (AT):   {cov:.1f}%  ({n_ok+n_elev}/{at_count} AT-Einträge)')
        feedback.pushInfo('========================\n')

        return {self.P_OUTPUT: sink_id}


def classFactory():
    return AT_SOTA_Match_DB()
