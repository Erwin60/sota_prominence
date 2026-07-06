# -*- coding: utf-8 -*-
# Filename: AT_SOTA_Coordinate_Validation.py
# Version:  1.5-reviewfreeze-authbev
#
# Step 5c — Coordinate and summit-identity validation
#
# Purpose:
#   Enrich AT_SOTA_Matched_CoordValidated.gpkg with 1 m DEM evidence for:
#     1) the SOTA/official anchor coordinate,
#     2) the calculated peak coordinate where present,
#     3) nearest BEV-NAMEN reference points.
#
# The step does not recompute prominence or key cols. It creates explicit
# review fields that separate summit identity, final geometry and naming.
#
# v1.4 sanitizer note:
#   The output GeoPackage sink can reject features when string attributes exceed
#   declared field lengths or when provider-specific values are not represented
#   as plain Python scalars. Therefore Step 5c now sanitizes output attributes:
#   long text is clipped to the declared field length, NumPy/Python numeric
#   values are converted to plain int/float, non-finite numbers become NULL,
#   and the output geometry is normalized to a simple 2D point. This does not
#   alter source prominence, key-col or matching logic; it preserves all input
#   rows and documents review flags robustly.

from qgis.PyQt.QtCore import QVariant
from qgis.core import (
    QgsProcessing, QgsProcessingAlgorithm, QgsProcessingParameterVectorLayer,
    QgsProcessingParameterRasterLayer, QgsProcessingParameterFile,
    QgsProcessingParameterString, QgsProcessingParameterNumber,
    QgsProcessingParameterFeatureSink, QgsProcessingParameterDefinition,
    QgsProcessingException, QgsFeature, QgsField, QgsFields, QgsGeometry,
    QgsPointXY, QgsVectorLayer, QgsSpatialIndex, QgsFeatureRequest,
    QgsCoordinateTransform, QgsCoordinateReferenceSystem, QgsWkbTypes,
    QgsFeatureSink
)
import processing
import math
import os
import numpy as np
from osgeo import gdal


class AT_SOTA_Coordinate_Validation(QgsProcessingAlgorithm):

    P_ASSIGNED = 'assigned_layer'
    P_DEM_1M = 'dem_1m'
    P_NAMES_GPKG = 'names_gpkg_path'
    P_NAMES_LAYER = 'names_layer_name'
    P_BEV_RADIUS = 'bev_search_radius'
    P_LOCAL_RADIUS = 'local_max_radius'
    P_FCODES = 'f_codes'
    P_OUTPUT = 'output'
    P_OUTPUT_ISSUES = 'output_issues'

    DEFAULT_NAMES_LAYER = 'NAM_7300_GELAENDEFORM_P_20250325'

    def createInstance(self):
        return self.__class__()

    def name(self):
        return 'AT_SOTA_Coordinate_Validation'

    def displayName(self):
        return 'AT SOTA Coordinate + Summit Identity Validation (Step 5c v1.5 auth-BEV)'

    def group(self):
        return 'SOTA'

    def groupId(self):
        return 'SOTA'

    def shortHelpString(self):
        return (
            'Step 5c: validates SOTA/DB anchors, BEV-NAMEN reference points and calculated peaks '
            'against the 1 m DEM. This is an audit/enrichment step only: it does not recompute prominence, '
            'key cols or final_sota_role. It writes coordinate_issue_class, coordinate_issue_severity, '
            'recommended geometry source and review flags.'
        )

    def initAlgorithm(self, config=None):
        self.addParameter(QgsProcessingParameterVectorLayer(
            self.P_ASSIGNED, 'AT_SOTA_Matched_CoordValidated.gpkg',
            [QgsProcessing.TypeVectorPoint]))

        self.addParameter(QgsProcessingParameterRasterLayer(
            self.P_DEM_1M, 'ALS 1 m DEM raster'))

        self.addParameter(QgsProcessingParameterFile(
            self.P_NAMES_GPKG, 'BEV DLM NAMEN GeoPackage',
            behavior=QgsProcessingParameterFile.File,
            fileFilter='GeoPackage (*.gpkg)', optional=True))

        p_layer = QgsProcessingParameterString(
            self.P_NAMES_LAYER, 'BEV NAMEN layer name',
            defaultValue=self.DEFAULT_NAMES_LAYER, optional=True)
        p_layer.setFlags(p_layer.flags() | QgsProcessingParameterDefinition.FlagAdvanced)
        self.addParameter(p_layer)

        self.addParameter(QgsProcessingParameterString(
            self.P_FCODES, 'BEV F_CODE values to inspect',
            defaultValue='7301,7302,7303', optional=True))

        self.addParameter(QgsProcessingParameterNumber(
            self.P_BEV_RADIUS, 'BEV nearest-name search radius (m)',
            type=QgsProcessingParameterNumber.Double,
            defaultValue=100.0, minValue=10.0, maxValue=1000.0))

        self.addParameter(QgsProcessingParameterNumber(
            self.P_LOCAL_RADIUS, 'Local 1 m maximum search radius (m)',
            type=QgsProcessingParameterNumber.Double,
            defaultValue=50.0, minValue=5.0, maxValue=250.0))

        self.addParameter(QgsProcessingParameterFeatureSink(
            self.P_OUTPUT, 'Output: Assigned layer with coordinate validation fields',
            type=QgsProcessing.TypeVectorPoint))

        self.addParameter(QgsProcessingParameterFeatureSink(
            self.P_OUTPUT_ISSUES, 'Output: coordinate / identity issue candidates',
            type=QgsProcessing.TypeVectorPoint))

    # ------------------------------------------------------------------
    @staticmethod
    def _f(v):
        try:
            if v is None:
                return None
            x = float(v)
            if math.isnan(x) or math.isinf(x):
                return None
            return x
        except Exception:
            return None

    @staticmethod
    def _i(v):
        try:
            if v is None:
                return None
            return int(float(v))
        except Exception:
            return None

    @staticmethod
    def _s(v):
        if v is None:
            return None
        s = str(v).strip()
        if not s or s.upper() == 'NULL':
            return None
        return s

    @staticmethod
    def _is_auth_bev_fcode(v):
        try:
            return int(float(v)) in (7302, 7303)
        except Exception:
            return False

    @classmethod
    def _authoritative_bev_name(cls, calc_name, calc_fcode, off_name, off_fcode):
        """Return consolidated authoritative BEV 7302/7303 name evidence.

        REMARK(v5.1-authbev): A missing BEV name at the calculated DEM point
        is not identical to missing official BEV name evidence. The official
        SOTA/DB anchor may carry a valid BEV 7302/7303 name.
        """
        calc_name = cls._s(calc_name)
        off_name = cls._s(off_name)
        if calc_name and cls._is_auth_bev_fcode(calc_fcode):
            fc = int(float(calc_fcode))
            return 1, calc_name, f'BEV_CALC_{fc}', 1, 0, 0
        if off_name and cls._is_auth_bev_fcode(off_fcode):
            fc = int(float(off_fcode))
            return 1, off_name, f'BEV_OFFICIAL_{fc}', 0, 1, 0
        return 0, None, 'NO_AUTHORITATIVE_BEV_NAME', 0, 0, 1

    @staticmethod
    def _fv(feat, name):
        try:
            if feat.fieldNameIndex(name) < 0:
                return None
            return feat[name]
        except Exception:
            return None

    @staticmethod
    def _add_field(fields, name, typ, length=0, prec=0):
        if fields.indexFromName(name) < 0:
            fields.append(QgsField(name, typ, '', length, prec, ''))

    @staticmethod
    def _sanitize_for_field(val, field):
        """Return a provider-safe value for a QGIS output field.

        This sanitizer prevents GeoPackage write failures caused by overlong
        class strings, NumPy scalar types, NaN/Inf values or unexpected Python
        objects. It is a write-safety mechanism, not a modelling rule: the
        underlying prominence, key-col and matching evidence is not recomputed
        or reclassified here.
        """
        if val is None:
            return None
        try:
            if isinstance(val, (np.integer,)):
                val = int(val)
            elif isinstance(val, (np.floating,)):
                val = float(val)
        except Exception:
            pass

        typ = field.type()
        if typ == QVariant.Double:
            try:
                x = float(val)
                if not math.isfinite(x):
                    return None
                return x
            except Exception:
                return None
        if typ in (QVariant.Int, QVariant.LongLong):
            try:
                return int(float(val))
            except Exception:
                return None
        if typ == QVariant.String:
            try:
                txt = str(val)
            except Exception:
                txt = ''
            ln = field.length() or 0
            if ln > 0 and len(txt) > ln:
                return txt[:ln]
            return txt
        return val

    @staticmethod
    def _parse_fcodes(text):
        vals = []
        for part in (text or '7301,7302,7303').split(','):
            part = part.strip()
            if not part:
                continue
            try:
                vals.append(int(part))
            except Exception:
                pass
        return sorted(set(vals)) or [7301, 7302, 7303]

    @staticmethod
    def _open_names_layer(gpkg_path, layer_name, feedback):
        if not gpkg_path:
            return None
        candidates = []
        if layer_name:
            candidates.append(layer_name)
        candidates += [
            'NAM_7300_GELAENDEFORM_P_20250325',
            'DLM_7000_Namen_20250325 — NAM_7300_GELAENDEFORM_P_20250325',
            'DLM_7000_Namen_20250325 - NAM_7300_GELAENDEFORM_P_20250325',
            'NAMEN', 'Geonamen', 'geonamen'
        ]
        for name in candidates:
            uri = f'{gpkg_path}|layername={name}'
            vl = QgsVectorLayer(uri, 'bev_names', 'ogr')
            if vl and vl.isValid():
                fns = [f.name() for f in vl.fields()]
                if 'F_CODE' in fns and ('NAME' in fns or 'Name' in fns):
                    feedback.pushInfo(f"  BEV NAMEN layer opened: {name}")
                    return vl
        vl = QgsVectorLayer(gpkg_path, 'bev_names', 'ogr')
        if vl and vl.isValid():
            fns = [f.name() for f in vl.fields()]
            if 'F_CODE' in fns and ('NAME' in fns or 'Name' in fns):
                feedback.pushInfo(f"  BEV NAMEN fallback layer opened: {vl.name()}")
                return vl
        raise QgsProcessingException(f'Cannot open a valid BEV NAMEN layer in {gpkg_path}')

    def _severity(self, dz, dist):
        dz = self._f(dz)
        dist = self._f(dist)
        if dz is None or dist is None:
            return 'NO_DATA'
        if dz >= 25 and dist >= 25:
            return 'SEVERE'
        if dz >= 10 and dist >= 25:
            return 'STRONG'
        if dz >= 5 and dist >= 20:
            return 'REVIEW'
        if dz >= 3 and dist >= 10:
            return 'MINOR'
        return 'NONE'

    def _point_from_xy(self, x, y):
        if x is None or y is None:
            return None
        return QgsPointXY(float(x), float(y))

    def _transform_point(self, pt, tr):
        if pt is None:
            return None
        if tr is None:
            return pt
        try:
            return tr.transform(pt)
        except Exception:
            return None

    # ------------------------------------------------------------------
    class DemSampler:
        def __init__(self, path):
            self.path = path
            self.ds = gdal.Open(path, gdal.GA_ReadOnly)
            if self.ds is None:
                raise QgsProcessingException(f'Cannot open DEM: {path}')
            self.band = self.ds.GetRasterBand(1)
            self.gt = self.ds.GetGeoTransform()
            self.inv_gt = gdal.InvGeoTransform(self.gt)
            self.nodata = self.band.GetNoDataValue()
            self.xsize = self.ds.RasterXSize
            self.ysize = self.ds.RasterYSize
            self.pixel_w = abs(self.gt[1])
            self.pixel_h = abs(self.gt[5])

        def _clean(self, v):
            try:
                x = float(v)
                if math.isnan(x) or math.isinf(x):
                    return None
                if self.nodata is not None and abs(x - float(self.nodata)) < 1e-9:
                    return None
                return x
            except Exception:
                return None

        def xy_to_pixel(self, x, y):
            px = self.inv_gt[0] + self.inv_gt[1] * x + self.inv_gt[2] * y
            py = self.inv_gt[3] + self.inv_gt[4] * x + self.inv_gt[5] * y
            return int(math.floor(px)), int(math.floor(py))

        def sample(self, x, y):
            if x is None or y is None:
                return None
            px, py = self.xy_to_pixel(x, y)
            if px < 0 or py < 0 or px >= self.xsize or py >= self.ysize:
                return None
            arr = self.band.ReadAsArray(px, py, 1, 1)
            if arr is None:
                return None
            return self._clean(arr[0, 0])

        def local_max(self, x, y, radius_m):
            if x is None or y is None:
                return None
            px, py = self.xy_to_pixel(x, y)
            if px < 0 or py < 0 or px >= self.xsize or py >= self.ysize:
                return None
            rx = int(math.ceil(radius_m / max(self.pixel_w, 1e-9)))
            ry = int(math.ceil(radius_m / max(self.pixel_h, 1e-9)))
            xoff = max(px - rx, 0)
            yoff = max(py - ry, 0)
            xend = min(px + rx + 1, self.xsize)
            yend = min(py + ry + 1, self.ysize)
            cols = xend - xoff
            rows = yend - yoff
            if cols <= 0 or rows <= 0:
                return None
            arr = self.band.ReadAsArray(xoff, yoff, cols, rows)
            if arr is None:
                return None
            arr = np.asarray(arr, dtype=float)
            if self.nodata is not None:
                arr[np.isclose(arr, float(self.nodata))] = np.nan
            yy, xx = np.indices(arr.shape)
            abs_px = xoff + xx
            abs_py = yoff + yy
            xs = self.gt[0] + (abs_px + 0.5) * self.gt[1] + (abs_py + 0.5) * self.gt[2]
            ys = self.gt[3] + (abs_px + 0.5) * self.gt[4] + (abs_py + 0.5) * self.gt[5]
            dist = np.sqrt((xs - x) ** 2 + (ys - y) ** 2)
            arr[dist > radius_m] = np.nan
            if np.all(np.isnan(arr)):
                return None
            flat_idx = np.nanargmax(arr)
            rr, cc = np.unravel_index(flat_idx, arr.shape)
            zmax = float(arr[rr, cc])
            xmax = float(xs[rr, cc])
            ymax = float(ys[rr, cc])
            dmax = float(math.hypot(xmax - x, ymax - y))
            return zmax, xmax, ymax, dmax

    # ------------------------------------------------------------------
    def _sample_metrics(self, sampler, pt_dem, radius_m):
        if pt_dem is None:
            return None, None, None, None, None, None
        x, y = pt_dem.x(), pt_dem.y()
        z = sampler.sample(x, y)
        lm = sampler.local_max(x, y, radius_m)
        if lm is None:
            return z, None, None, None, None, None
        zmax, xmax, ymax, dist = lm
        dz = None if z is None or zmax is None else zmax - z
        return z, zmax, xmax, ymax, dist, dz

    def _nearest_bev(self, pt_layer, bev_idx, bev_features, bev_radius, fcode_prefer=7302):
        if pt_layer is None or bev_idx is None:
            return None
        ids = bev_idx.nearestNeighbor(pt_layer, 20)
        best_preferred = None
        best_any = None
        for fid in ids:
            bf = bev_features.get(fid)
            if bf is None:
                continue
            try:
                d = bf.geometry().distance(QgsGeometry.fromPointXY(pt_layer))
            except Exception:
                continue
            if d is None or not math.isfinite(d) or d > bev_radius:
                continue
            fcode = self._i(bf['F_CODE']) if bf.fieldNameIndex('F_CODE') >= 0 else None
            cand = (float(d), bf)
            if best_any is None or cand[0] < best_any[0]:
                best_any = cand
            if fcode == fcode_prefer:
                if best_preferred is None or cand[0] < best_preferred[0]:
                    best_preferred = cand
        return best_preferred or best_any

    def _classify(self, feat, off_metrics, calc_metrics, bev_off_metrics, bev_calc_metrics, bev_off_dist, bev_off_fcode):
        # metrics tuple: z, zmax, xmax, ymax, dist, dz
        off_z, off_zmax, _, _, off_distmax, off_dz = off_metrics
        calc_z, calc_zmax, _, _, calc_distmax, calc_dz = calc_metrics
        bev_z, bev_zmax, _, _, bev_distmax, bev_dz = bev_off_metrics
        severity = self._severity(off_dz, off_distmax)
        calc_sev = self._severity(calc_dz, calc_distmax)
        bev_sev = self._severity(bev_dz, bev_distmax)

        classes = []
        coord_review = 0
        ident_review = 0
        name_review = 0
        geom_source = 'UNCHANGED'

        if severity in ('REVIEW', 'STRONG', 'SEVERE'):
            coord_review = 1
            if bev_off_fcode == 7302 and bev_z is not None and bev_sev in ('NONE', 'MINOR'):
                classes.append('DB_COORD_SHIFT_BEV_SUPPORT')
                geom_source = 'BEV_NAME_7302_REVIEW'
            elif calc_z is not None and calc_sev in ('NONE', 'MINOR'):
                classes.append('DB_COORD_SHIFT_CALC_SUPPORT')
                geom_source = 'CALC_PEAK_REVIEW'
            else:
                classes.append('OFFICIAL_POINT_NOT_ON_LOCAL_MAX')
                geom_source = 'MANUAL_REVIEW'

        if bev_z is not None and bev_sev in ('REVIEW', 'STRONG', 'SEVERE'):
            coord_review = 1
            classes.append('BEV_REFERENCE_POSITION_CONFLICT')

        if calc_z is not None and calc_sev in ('REVIEW', 'STRONG', 'SEVERE'):
            coord_review = 1
            classes.append('CALC_POINT_NOT_ON_LOCAL_MAX')

        # Higher CALC maximum than BEV official summit anchor: ridge identity review.
        if calc_zmax is not None and bev_zmax is not None and calc_zmax - bev_zmax >= 3.0:
            ident_review = 1
            classes.append('CALC_HIGHER_THAN_OFFICIAL_NAMED_SUMMIT')

        same_kc = self._i(self._fv(feat, 'same_keycol_flag')) or 0
        fgt = self._s(self._fv(feat, 'final_group_type'))
        if same_kc == 1 and classes:
            ident_review = 1
            classes.append('SHARED_KEYCOL_WITH_COORDINATE_CONFLICT')

        # Name correction signal: SOTA and BEV names are close but not identical is left for manual review.
        sota_name = self._s(self._fv(feat, 'sota_name'))
        official_display_name = self._s(self._fv(feat, 'official_display_name'))
        if sota_name and official_display_name and sota_name.lower() != official_display_name.lower():
            # Avoid flagging every SOTA-vs-BEV spelling; only flag if coordinate or identity review also present.
            if coord_review or ident_review:
                name_review = 1
                classes.append('DB_NAME_OR_REFERENCE_CHECK')

        if not classes:
            classes = ['NO_COORDINATE_FLAG']
            severity_final = 'NONE'
        else:
            sev_order = {'NONE': 0, 'MINOR': 1, 'REVIEW': 2, 'STRONG': 3, 'SEVERE': 4, 'NO_DATA': -1}
            severities = [severity, calc_sev, bev_sev]
            severity_final = max(severities, key=lambda s: sev_order.get(s, -1))
            if severity_final == 'NO_DATA':
                severity_final = 'REVIEW'

        return ';'.join(dict.fromkeys(classes)), severity_final, coord_review, ident_review, name_review, geom_source

    def _safe_output_point_geometry(self, geom, fallback_pt=None):
        """Return a simple 2D point geometry accepted by a point sink.

        Some upstream layers may contain valid but non-simple point encodings
        (PointZ/M, MultiPoint, or occasionally non-point geometries). A point
        GeoPackage sink can silently reject such features. This helper keeps
        every input row by normalising the output geometry to a simple 2D point.
        """
        try:
            if geom is not None and not geom.isEmpty():
                if QgsWkbTypes.geometryType(geom.wkbType()) == QgsWkbTypes.PointGeometry:
                    if QgsWkbTypes.isMultiType(geom.wkbType()):
                        pts = geom.asMultiPoint()
                        if pts:
                            return QgsGeometry.fromPointXY(QgsPointXY(pts[0]))
                    else:
                        return QgsGeometry.fromPointXY(QgsPointXY(geom.asPoint()))
                cen = geom.centroid()
                if cen is not None and not cen.isEmpty():
                    return QgsGeometry.fromPointXY(QgsPointXY(cen.asPoint()))
        except Exception:
            pass
        if fallback_pt is not None:
            try:
                return QgsGeometry.fromPointXY(QgsPointXY(fallback_pt))
            except Exception:
                pass
        return QgsGeometry()

    # ------------------------------------------------------------------
    def processAlgorithm(self, parameters, context, feedback):
        assigned = self.parameterAsVectorLayer(parameters, self.P_ASSIGNED, context)
        dem = self.parameterAsRasterLayer(parameters, self.P_DEM_1M, context)
        names_path = self.parameterAsFile(parameters, self.P_NAMES_GPKG, context) or ''
        names_layer = self.parameterAsString(parameters, self.P_NAMES_LAYER, context) or self.DEFAULT_NAMES_LAYER
        fcodes = self._parse_fcodes(self.parameterAsString(parameters, self.P_FCODES, context))
        bev_radius = float(self.parameterAsDouble(parameters, self.P_BEV_RADIUS, context))
        local_radius = float(self.parameterAsDouble(parameters, self.P_LOCAL_RADIUS, context))

        if not assigned or not assigned.isValid():
            raise QgsProcessingException('Invalid assigned layer.')
        if not dem or not dem.isValid():
            raise QgsProcessingException('Invalid 1 m DEM raster.')

        sampler = self.DemSampler(dem.source())

        # Coordinate transform from assigned / BEV layer CRS to DEM CRS.
        dem_crs = dem.crs()
        tr_ass_to_dem = None
        if assigned.crs() != dem_crs:
            tr_ass_to_dem = QgsCoordinateTransform(assigned.crs(), dem_crs, context.transformContext())

        feedback.pushInfo(f'1 m DEM: {dem.source()}')
        feedback.pushInfo(f'Local max radius: {local_radius:.1f} m')
        feedback.pushInfo(f'BEV search radius: {bev_radius:.1f} m')
        feedback.pushInfo(f'BEV F_CODE inspected: {fcodes}')

        # Load BEV names and build index.
        bev_idx = None
        bev_features = {}
        tr_bev_to_ass = None
        name_field = 'NAME'
        if names_path:
            bev_lyr = self._open_names_layer(names_path, names_layer, feedback)
            if bev_lyr.crs() != assigned.crs():
                tr_bev_to_ass = QgsCoordinateTransform(bev_lyr.crs(), assigned.crs(), context.transformContext())
            fns = [f.name() for f in bev_lyr.fields()]
            name_field = 'NAME' if 'NAME' in fns else 'Name'
            feats = []
            for bf in bev_lyr.getFeatures():
                fcode = self._i(bf['F_CODE']) if bf.fieldNameIndex('F_CODE') >= 0 else None
                if fcode not in fcodes:
                    continue
                geom = bf.geometry()
                if geom is None or geom.isEmpty():
                    continue
                if tr_bev_to_ass is not None:
                    try:
                        geom = QgsGeometry(geom)
                        geom.transform(tr_bev_to_ass)
                        bf2 = QgsFeature(bf)
                        bf2.setGeometry(geom)
                        bf = bf2
                    except Exception:
                        continue
                feats.append(bf)
                bev_features[bf.id()] = bf
            # QGIS 3.44/macOS does not accept a plain Python list in
            # the QgsSpatialIndex constructor. Build the index explicitly.
            bev_idx = QgsSpatialIndex()
            for _bf in feats:
                bev_idx.addFeature(_bf)
            feedback.pushInfo(f'BEV name features indexed: {len(bev_features)}')

        # Output schema: assigned + validation fields.
        out_fields = QgsFields()
        for f in assigned.fields():
            out_fields.append(f)

        add = self._add_field
        # cv_coord_issue_class may contain several semicolon-separated
        # classes. Use generous field lengths and still sanitize before writing.
        for n, ln in [
            ('cv_coord_issue_class', 240),
            ('cv_coord_issue_severity', 24),
            ('cv_final_geometry_source', 80),
            ('cv_bev_off_name', 120),
            ('cv_bev_calc_name', 120),
            # REMARK(v5.1-authbev): consolidated official BEV 7302/7303 evidence.
            ('bev_authoritative_name', 120),
            ('bev_authoritative_name_source', 48),
            ('bev_support_interpreted', 64),
            ('cv_note', 255),
        ]:
            add(out_fields, n, QVariant.String, ln, 0)
        for n in [
            'cv_coordinate_review_required', 'cv_identity_review_required', 'cv_name_review_required',
            'cv_bev_off_fcode', 'cv_bev_calc_fcode',
            'bev_calc_name_available', 'bev_official_anchor_available',
            'bev_authoritative_name_available', 'no_authoritative_bev_name',
            'osm_name_review_allowed'
        ]:
            add(out_fields, n, QVariant.Int, 4, 0)
        for n in [
            'cv_sota_z1m', 'cv_sota_zmax', 'cv_sota_distmax_m', 'cv_sota_dzmax_m',
            'cv_calc_z1m', 'cv_calc_zmax', 'cv_calc_distmax_m', 'cv_calc_dzmax_m',
            'cv_bev_off_dist_m', 'cv_bev_off_z1m', 'cv_bev_off_zmax', 'cv_bev_off_distmax_m', 'cv_bev_off_dzmax_m',
            'cv_bev_calc_dist_m', 'cv_bev_calc_z1m', 'cv_bev_calc_zmax', 'cv_bev_calc_distmax_m', 'cv_bev_calc_dzmax_m'
        ]:
            add(out_fields, n, QVariant.Double, 12, 3)

        sink, sink_id = self.parameterAsSink(parameters, self.P_OUTPUT, context, out_fields, QgsWkbTypes.Point, assigned.crs())
        if sink is None:
            raise QgsProcessingException('Could not create output sink.')

        issue_fields = QgsFields()
        for n, t, l, p in [
            ('source_fid', QVariant.Int, 8, 0),
            ('sota_ref', QVariant.String, 30, 0),
            ('display_name', QVariant.String, 100, 0),
            ('sota_status', QVariant.String, 32, 0),
            ('sota_status_refined', QVariant.String, 48, 0),
            ('final_sota_role', QVariant.String, 48, 0),
            ('issue_class', QVariant.String, 160, 0),
            ('severity', QVariant.String, 24, 0),
            ('coord_review', QVariant.Int, 1, 0),
            ('identity_review', QVariant.Int, 1, 0),
            ('name_review', QVariant.Int, 1, 0),
            ('recommended_geometry_source', QVariant.String, 48, 0),
            ('sota_dzmax_m', QVariant.Double, 12, 3),
            ('sota_distmax_m', QVariant.Double, 12, 3),
            ('calc_dzmax_m', QVariant.Double, 12, 3),
            ('calc_distmax_m', QVariant.Double, 12, 3),
            ('bev_off_name', QVariant.String, 100, 0),
            ('bev_off_fcode', QVariant.Int, 4, 0),
            ('bev_off_dist_m', QVariant.Double, 12, 3),
            ('bev_off_dzmax_m', QVariant.Double, 12, 3),
        ]:
            issue_fields.append(QgsField(n, t, '', l, p, ''))
        issue_sink, issue_sink_id = self.parameterAsSink(parameters, self.P_OUTPUT_ISSUES, context, issue_fields, QgsWkbTypes.Point, assigned.crs())
        if issue_sink is None:
            raise QgsProcessingException('Could not create issue output sink.')

        n = 0
        n_issues = 0
        for feat in assigned.getFeatures():
            if feedback.isCanceled():
                break
            n += 1

            # Official/SOTA anchor point: prefer explicit official_x/y, then feature geometry.
            ox = self._f(self._fv(feat, 'official_x'))
            oy = self._f(self._fv(feat, 'official_y'))
            if ox is not None and oy is not None:
                off_pt_ass = QgsPointXY(ox, oy)
            else:
                g = feat.geometry()
                off_pt_ass = g.asPoint() if g and not g.isEmpty() else None
            off_pt_dem = self._transform_point(off_pt_ass, tr_ass_to_dem)
            off_metrics = self._sample_metrics(sampler, off_pt_dem, local_radius)

            # Calculated point: only reliable for CALC_PEAK rows.
            calc_pt_ass = None
            if self._s(self._fv(feat, 'row_origin')) == 'CALC_PEAK':
                g = feat.geometry()
                if g and not g.isEmpty():
                    calc_pt_ass = g.asPoint()
            calc_pt_dem = self._transform_point(calc_pt_ass, tr_ass_to_dem)
            calc_metrics = self._sample_metrics(sampler, calc_pt_dem, local_radius)

            # Nearest BEV around official anchor and around calculated point.
            bev_off_name = None; bev_off_fcode = None; bev_off_dist = None
            bev_off_metrics = (None, None, None, None, None, None)
            if bev_idx is not None and off_pt_ass is not None:
                nb = self._nearest_bev(off_pt_ass, bev_idx, bev_features, bev_radius, 7302)
                if nb is not None:
                    bev_off_dist, bf = nb
                    bev_off_name = self._s(bf[name_field])
                    bev_off_fcode = self._i(bf['F_CODE'])
                    bpt_ass = bf.geometry().asPoint()
                    bpt_dem = self._transform_point(bpt_ass, tr_ass_to_dem)
                    bev_off_metrics = self._sample_metrics(sampler, bpt_dem, local_radius)

            bev_calc_name = None; bev_calc_fcode = None; bev_calc_dist = None
            bev_calc_metrics = (None, None, None, None, None, None)
            if bev_idx is not None and calc_pt_ass is not None:
                nb = self._nearest_bev(calc_pt_ass, bev_idx, bev_features, bev_radius, 7302)
                if nb is not None:
                    bev_calc_dist, bf = nb
                    bev_calc_name = self._s(bf[name_field])
                    bev_calc_fcode = self._i(bf['F_CODE'])
                    bpt_ass = bf.geometry().asPoint()
                    bpt_dem = self._transform_point(bpt_ass, tr_ass_to_dem)
                    bev_calc_metrics = self._sample_metrics(sampler, bpt_dem, local_radius)

            # REMARK(v5.1-authbev): consolidate BEV name evidence from two valid anchors:
            #   1) BEV near calculated DEM peak, and
            #   2) BEV near official/SOTA anchor.
            # OSM fallback is allowed only if neither provides BEV 7302/7303.
            (
                bev_auth_available,
                bev_auth_name,
                bev_auth_source,
                bev_calc_name_available,
                bev_official_anchor_available,
                no_auth_bev_name,
            ) = self._authoritative_bev_name(
                bev_calc_name, bev_calc_fcode, bev_off_name, bev_off_fcode
            )
            if bev_auth_available:
                bev_support_interpreted = bev_auth_source
            elif bev_calc_fcode == 7301 or bev_off_fcode == 7301:
                bev_support_interpreted = 'BEV_CONTEXT_7301_ONLY'
            else:
                bev_support_interpreted = 'NO_AUTHORITATIVE_BEV_NAME'
            osm_name_review_allowed = 1 if no_auth_bev_name == 1 else 0

            issue_class, sev, coord_rev, ident_rev, name_rev, geom_source = self._classify(
                feat, off_metrics, calc_metrics, bev_off_metrics, bev_calc_metrics, bev_off_dist, bev_off_fcode
            )

            out = QgsFeature(out_fields)
            out.setGeometry(self._safe_output_point_geometry(feat.geometry(), off_pt_ass))
            attrs = list(feat.attributes()) + [None] * (out_fields.count() - len(feat.attributes()))
            def setv(name, val):
                idx = out_fields.indexFromName(name)
                if idx >= 0:
                    field = out_fields.at(idx)
                    if isinstance(val, float) and math.isfinite(val):
                        val = round(val, 3)
                    attrs[idx] = self._sanitize_for_field(val, field)

            # unpack metrics
            oz, ozmax, _, _, odmax, odz = off_metrics
            cz, czmax, _, _, cdmax, cdz = calc_metrics
            boz, bozmax, _, _, bodmax, bodz = bev_off_metrics
            bcz, bczmax, _, _, bcdmax, bcdz = bev_calc_metrics

            for name, val in [
                ('cv_coord_issue_class', issue_class), ('cv_coord_issue_severity', sev), ('cv_coordinate_review_required', coord_rev),
                ('cv_identity_review_required', ident_rev), ('cv_name_review_required', name_rev), ('cv_final_geometry_source', geom_source),
                ('cv_sota_z1m', oz), ('cv_sota_zmax', ozmax), ('cv_sota_distmax_m', odmax), ('cv_sota_dzmax_m', odz),
                ('cv_calc_z1m', cz), ('cv_calc_zmax', czmax), ('cv_calc_distmax_m', cdmax), ('cv_calc_dzmax_m', cdz),
                ('cv_bev_off_name', bev_off_name), ('cv_bev_off_fcode', bev_off_fcode), ('cv_bev_off_dist_m', bev_off_dist),
                ('cv_bev_off_z1m', boz), ('cv_bev_off_zmax', bozmax), ('cv_bev_off_distmax_m', bodmax), ('cv_bev_off_dzmax_m', bodz),
                ('cv_bev_calc_name', bev_calc_name), ('cv_bev_calc_fcode', bev_calc_fcode), ('cv_bev_calc_dist_m', bev_calc_dist),
                ('cv_bev_calc_z1m', bcz), ('cv_bev_calc_zmax', bczmax), ('cv_bev_calc_distmax_m', bcdmax), ('cv_bev_calc_dzmax_m', bcdz),
                ('bev_calc_name_available', bev_calc_name_available),
                ('bev_official_anchor_available', bev_official_anchor_available),
                ('bev_authoritative_name_available', bev_auth_available),
                ('bev_authoritative_name', bev_auth_name),
                ('bev_authoritative_name_source', bev_auth_source),
                ('bev_support_interpreted', bev_support_interpreted),
                ('no_authoritative_bev_name', no_auth_bev_name),
                ('osm_name_review_allowed', osm_name_review_allowed),
            ]:
                setv(name, val)
            note_parts = []
            if issue_class != 'NO_COORDINATE_FLAG':
                note_parts.append('Coordinate/summit-identity review: compare SOTA/DB anchor, BEV-NAMEN point and calculated peak before final DB correction.')
            if no_auth_bev_name == 1:
                note_parts.append('No authoritative BEV 7302/7303 name at calc point or official/SOTA anchor; OSM may be inspected as fallback name evidence only.')
            elif bev_calc_name_available == 0 and bev_official_anchor_available == 1:
                note_parts.append('No BEV name at calculated point, but authoritative BEV 7302/7303 exists at official/SOTA anchor; do not treat as missing official BEV name.')
            setv('cv_note', ' '.join(note_parts))

            # Final provider-safety pass. Some GeoPackage providers enforce
            # declared field lengths and scalar types more strictly than QGIS
            # in-memory layers. This keeps all input rows writable while the
            # cv_* fields still explicitly report the coordinate issue.
            attrs = [self._sanitize_for_field(v, out_fields.at(i)) for i, v in enumerate(attrs)]
            out.setAttributes(attrs)
            if not sink.addFeature(out, QgsFeatureSink.FastInsert):
                # Retry without FastInsert and with a sanitized point geometry.
                out.setGeometry(self._safe_output_point_geometry(feat.geometry(), off_pt_ass))
                if not sink.addFeature(out):
                    raise QgsProcessingException(
                        f'Coordinate validation could not write input feature id={feat.id()} '
                        f'sota_ref={self._s(self._fv(feat, "sota_ref"))}'
                    )

            if issue_class != 'NO_COORDINATE_FLAG':
                n_issues += 1
                iss = QgsFeature(issue_fields)
                iss.setGeometry(QgsGeometry.fromPointXY(off_pt_ass) if off_pt_ass is not None else feat.geometry())
                display = self._s(self._fv(feat, 'sota_name')) or self._s(self._fv(feat, 'NAME')) or self._s(self._fv(feat, 'official_display_name'))
                iss.setAttributes([
                    feat.id(), self._s(self._fv(feat, 'sota_ref')), display,
                    self._s(self._fv(feat, 'sota_status')), self._s(self._fv(feat, 'sota_status_refined')),
                    self._s(self._fv(feat, 'final_sota_role')), issue_class, sev, coord_rev, ident_rev, name_rev, geom_source,
                    None if odz is None else round(odz,3), None if odmax is None else round(odmax,3),
                    None if cdz is None else round(cdz,3), None if cdmax is None else round(cdmax,3),
                    bev_off_name, bev_off_fcode, None if bev_off_dist is None else round(bev_off_dist,3),
                    None if bodz is None else round(bodz,3)
                ])
                issue_sink.addFeature(iss, QgsFeatureSink.FastInsert)

        feedback.pushInfo('=== Coordinate + summit-identity validation ===')
        feedback.pushInfo(f'  processed rows: {n}')
        feedback.pushInfo(f'  issue candidates: {n_issues}')
        feedback.pushInfo('  NOTE: This step does not change final_sota_role or recompute prominence.')
        feedback.pushInfo('================================================')

        return {self.P_OUTPUT: sink_id, self.P_OUTPUT_ISSUES: issue_sink_id}


def classFactory():
    return AT_SOTA_Coordinate_Validation()
