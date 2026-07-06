# -*- coding: utf-8 -*-
"""
AT_SOTA_Export_Keycol.py  —  Step 4b  v2.5

Key changes vs v2.2
------------------
1) If a diagnosed matched layer is provided, qualifying peaks from AT_SOTA_Final
   are enriched from the nearest CALC_PEAK row in the matched/diagnosed layer,
   so output lines/points carry reliable status attributes instead of NULL.
2) Additional ambiguity / official-reference fields are carried into
   keycol_points / peak_to_col_lines.
3) DB_NO_PEAK still prefers raw-peak diagnostics from Step 5 and uses a
   configurable nearest-neighbour fallback on peaks_prom_raw when needed.

Key changes vs v2.4
------------------
4) DB_NO_PEAK rows with raw_peak_fid/raw_peak_keycol_x/raw_peak_keycol_y now
   also enrich z_sattel, z_gipfel and prom_ref from the corresponding raw peak
   whenever peaks_prom_raw is provided. Previously this enrichment happened
   only when keycol_x/keycol_y were missing, so raw-only below-threshold cases
   could export a key-col geometry but still show empty saddle/height values.
"""

from qgis.PyQt.QtCore import QVariant
from qgis.core import (
    QgsProcessing, QgsProcessingAlgorithm,
    QgsProcessingParameterVectorLayer,
    QgsProcessingParameterNumber,
    QgsProcessingParameterFeatureSink,
    QgsProcessingException,
    QgsFeature, QgsField, QgsFields, QgsGeometry,
    QgsPointXY, QgsWkbTypes, QgsFeatureSink,
    QgsSpatialIndex, QgsFeatureRequest,
)


class AT_SOTA_Export_Keycol(QgsProcessingAlgorithm):
    P_PEAKS = 'peaks_layer'
    P_RAW = 'peaks_raw'
    P_MATCHED = 'matched_layer'
    P_RADIUS = 'db_no_peak_radius'
    P_CANDIDATES = 'db_no_peak_candidates'
    P_POINTS = 'output_points'
    P_LINES = 'output_lines'

    def name(self):
        return 'AT_SOTA_Export_Keycol'

    def displayName(self):
        return 'AT SOTA Export Keycol (Step 4b, v2.7 robust raw-only value enrichment)'

    def group(self):
        return 'AT SOTA Pipeline'

    def groupId(self):
        return 'at_sota'

    def createInstance(self):
        return AT_SOTA_Export_Keycol()

    def initAlgorithm(self, config=None):
        self.addParameter(QgsProcessingParameterVectorLayer(
            self.P_PEAKS, 'Peaks (AT_SOTA_Final.gpkg)',
            [QgsProcessing.TypeVectorPoint]))
        self.addParameter(QgsProcessingParameterVectorLayer(
            self.P_RAW, 'Roh-Peaks (peaks_prom_raw.gpkg) — optional fallback für DB_NO_PEAK',
            [QgsProcessing.TypeVectorPoint], optional=True))
        self.addParameter(QgsProcessingParameterVectorLayer(
            self.P_MATCHED, 'AT_SOTA_Matched_Diagnosed.gpkg oder AT_SOTA_Matched.gpkg',
            [QgsProcessing.TypeVectorPoint], optional=True))
        self.addParameter(QgsProcessingParameterNumber(
            self.P_RADIUS,
            'Suchradius DB_NO_PEAK → Roh-Peak (m)',
            type=QgsProcessingParameterNumber.Double,
            defaultValue=600.0, minValue=50.0, maxValue=5000.0))
        self.addParameter(QgsProcessingParameterNumber(
            self.P_CANDIDATES,
            'Anzahl zu prüfender Roh-Peak-Kandidaten für DB_NO_PEAK',
            type=QgsProcessingParameterNumber.Integer,
            defaultValue=20, minValue=3, maxValue=200))
        self.addParameter(QgsProcessingParameterFeatureSink(
            self.P_POINTS, 'Schlüsselsattel-Punkte', QgsProcessing.TypeVectorPoint))
        self.addParameter(QgsProcessingParameterFeatureSink(
            self.P_LINES, 'Gipfel–Sattel-Linien', QgsProcessing.TypeVectorLine))

    @staticmethod
    def _f(v):
        try:
            if v is None:
                return None
            x = float(v)
            if x != x:
                return None
            return x
        except Exception:
            return None

    def processAlgorithm(self, parameters, context, feedback):
        peaks = self.parameterAsVectorLayer(parameters, self.P_PEAKS, context)
        raw_lyr = self.parameterAsVectorLayer(parameters, self.P_RAW, context)
        matched = self.parameterAsVectorLayer(parameters, self.P_MATCHED, context)
        radius = self.parameterAsDouble(parameters, self.P_RADIUS, context)
        candidate_count = int(self.parameterAsInt(parameters, self.P_CANDIDATES, context))

        if peaks is None or not peaks.isValid():
            raise QgsProcessingException('Peaks-Layer nicht gefunden.')
        crs = peaks.crs()

        def _fields():
            f = QgsFields()
            def a(name, typ, ln=0, pr=0):
                f.append(QgsField(name, typ, '', ln, pr, ''))
            a('fid_peak', QVariant.Int)
            a('NAME', QVariant.String, 100)
            a('sota_ref', QVariant.String, 30)
            a('sota_name', QVariant.String, 100)
            a('official_display_name', QVariant.String, 120)
            a('official_name_source', QVariant.String, 16)
            a('sota_status', QVariant.String, 24)
            a('sota_status_refined', QVariant.String, 40)
            a('new_calc_class', QVariant.String, 32)
            a('ambiguity_class', QVariant.String, 40)
            a('ambiguity_score', QVariant.Int)
            a('official_preference', QVariant.String, 28)
            a('manual_review_required', QVariant.Int)
            a('final_group_id', QVariant.String, 32)
            a('final_group_type', QVariant.String, 32)
            a('final_candidate_rank', QVariant.Int)
            a('final_sota_flag', QVariant.Int)
            a('final_sota_role', QVariant.String, 32)
            a('final_assignment_basis', QVariant.String, 40)
            a('final_assignment_confidence', QVariant.String, 12)
            a('final_manual_review_required', QVariant.Int)
            a('final_replay_required', QVariant.Int)
            a('row_origin', QVariant.String, 24)
            a('z_sattel', QVariant.Double, 10, 1)
            a('z_gipfel', QVariant.Double, 10, 1)
            a('prom_ref', QVariant.Double, 10, 1)
            a('land_id', QVariant.Int)
            a('land', QVariant.String, 60)
            a('BL_quelle', QVariant.String, 12)
            a('border_dist_m', QVariant.Double, 10, 1)
            a('border_zone', QVariant.String, 20)
            a('bl_border_dist_m', QVariant.Double, 10, 1)
            a('bl_border_zone', QVariant.String, 20)
            a('neighbor_land_id', QVariant.Int)
            a('neighbor_land', QVariant.String, 60)
            a('admin_context', QVariant.String, 24)
            a('admin_review', QVariant.Int)
            a('match_elev_diff_m', QVariant.Double, 10, 1)
            a('bev_support', QVariant.String, 24)
            a('review_hint', QVariant.String, 40)
            a('db_no_peak_reason', QVariant.String, 32)
            a('raw_peak_zone', QVariant.String, 16)
            a('raw_peak_fid', QVariant.Int)
            a('raw_peak_dist_m', QVariant.Double, 10, 1)
            a('ambiguity_partner_dist_m', QVariant.Double, 10, 1)
            a('ambiguity_partner_z_diff_m', QVariant.Double, 10, 1)
            a('ambiguity_partner_keycol_dist_m', QVariant.Double, 10, 1)
            return f

        pt_fields = _fields()
        ln_fields = _fields()
        ln_fields.append(QgsField('laenge_m', QVariant.Double, '', 10, 1, ''))

        pt_sink, pt_id = self.parameterAsSink(parameters, self.P_POINTS, context, pt_fields, QgsWkbTypes.Point, crs)
        ln_sink, ln_id = self.parameterAsSink(parameters, self.P_LINES, context, ln_fields, QgsWkbTypes.LineString, crs)
        if pt_sink is None or ln_sink is None:
            raise QgsProcessingException('Output-Sinks nicht erstellbar.')

        def _fv(feat, fname):
            idx = feat.fieldNameIndex(fname)
            return feat[fname] if idx >= 0 else None

        def _write(peak_geom, col_x, col_y, attrs_common):
            x = self._f(col_x)
            y = self._f(col_y)
            if x is None or y is None:
                return False
            col_pt = QgsGeometry.fromPointXY(QgsPointXY(x, y))
            pf = QgsFeature(pt_fields)
            pf.setGeometry(col_pt)
            pf.setAttributes(attrs_common)
            pt_sink.addFeature(pf, QgsFeatureSink.FastInsert)

            peak_pt = peak_geom.asPoint()
            line = QgsGeometry.fromPolylineXY([QgsPointXY(peak_pt.x(), peak_pt.y()), QgsPointXY(x, y)])
            lf = QgsFeature(ln_fields)
            lf.setGeometry(line)
            lf.setAttributes(attrs_common + [round(float(line.length()), 1)])
            ln_sink.addFeature(lf, QgsFeatureSink.FastInsert)
            return True

        def _common_attrs(feat, fid_peak, z_sattel, z_gipfel, prom_ref, raw_peak_fid=None, raw_peak_dist_m=None):
            def _r(v):
                vv = self._f(v)
                return round(vv, 1) if vv is not None else None
            return [
                fid_peak,
                _fv(feat, 'NAME') or _fv(feat, 'sota_name') or _fv(feat, 'official_display_name'),
                _fv(feat, 'sota_ref'),
                _fv(feat, 'sota_name'),
                _fv(feat, 'official_display_name') or _fv(feat, 'NAME') or _fv(feat, 'sota_name'),
                _fv(feat, 'official_name_source'),
                _fv(feat, 'sota_status'),
                _fv(feat, 'sota_status_refined') or _fv(feat, 'sota_status'),
                _fv(feat, 'new_calc_class'),
                _fv(feat, 'ambiguity_class'),
                _fv(feat, 'ambiguity_score'),
                _fv(feat, 'official_preference'),
                _fv(feat, 'manual_review_required'),
                _fv(feat, 'final_group_id'),
                _fv(feat, 'final_group_type'),
                _fv(feat, 'final_candidate_rank'),
                _fv(feat, 'final_sota_flag'),
                _fv(feat, 'final_sota_role'),
                _fv(feat, 'final_assignment_basis'),
                _fv(feat, 'final_assignment_confidence'),
                _fv(feat, 'final_manual_review_required'),
                _fv(feat, 'final_replay_required'),
                _fv(feat, 'row_origin'),
                _r(z_sattel),
                _r(z_gipfel),
                _r(prom_ref),
                _fv(feat, 'land_id'),
                _fv(feat, 'land'),
                _fv(feat, 'BL_quelle'),
                _fv(feat, 'border_dist_m'),
                _fv(feat, 'border_zone'),
                _fv(feat, 'bl_border_dist_m'),
                _fv(feat, 'bl_border_zone'),
                _fv(feat, 'neighbor_land_id'),
                _fv(feat, 'neighbor_land'),
                _fv(feat, 'admin_context'),
                _fv(feat, 'admin_review'),
                _fv(feat, 'match_elev_diff_m'),
                _fv(feat, 'bev_support'),
                _fv(feat, 'review_hint'),
                _fv(feat, 'db_no_peak_reason'),
                _fv(feat, 'raw_peak_zone'),
                raw_peak_fid if raw_peak_fid is not None else _fv(feat, 'raw_peak_fid'),
                raw_peak_dist_m if raw_peak_dist_m is not None else _fv(feat, 'raw_peak_dist_m'),
                _fv(feat, 'ambiguity_partner_dist_m'),
                _fv(feat, 'ambiguity_partner_z_diff_m'),
                _fv(feat, 'ambiguity_partner_keycol_dist_m'),
            ]

        # build matched calc index for pass 1 enrichment
        matched_calc_idx = None
        if matched is not None:
            matched_calc_idx = QgsSpatialIndex()
            for mf in matched.getFeatures():
                if _fv(mf, 'row_origin') == 'CALC_PEAK':
                    qf = QgsFeature()
                    qf.setId(mf.id())
                    qf.setGeometry(mf.geometry())
                    matched_calc_idx.addFeature(qf)

        def _nearest_matched_calc(geom):
            if matched is None or matched_calc_idx is None or geom is None or geom.isEmpty():
                return None
            best = None
            best_d = 5.0
            for cid in matched_calc_idx.nearestNeighbor(geom.asPoint(), 8):
                mf = next(matched.getFeatures(QgsFeatureRequest(cid)), None)
                if mf is None or mf.geometry() is None or mf.geometry().isEmpty():
                    continue
                d = geom.distance(mf.geometry())
                if d <= best_d:
                    best = mf
                    best_d = d
            return best

        # Pass 1 — qualifying peaks from AT_SOTA_Final
        n_ok = n_skip = 0
        feedback.pushInfo('Verarbeite AT_SOTA_Final (qualifizierte Gipfel)...')
        for pf in peaks.getFeatures():
            if feedback.isCanceled():
                break
            src_feat = _nearest_matched_calc(pf.geometry()) or pf
            kx = _fv(pf, 'keycol_x')
            ky = _fv(pf, 'keycol_y')
            attrs = _common_attrs(
                src_feat,
                int(pf.id()),
                _fv(pf, 'keycol'),
                _fv(pf, 'z1m_max') or _fv(pf, 'zpk_1'),
                _fv(pf, 'prom_ref'),
            )
            ok = _write(pf.geometry(), kx, ky, attrs)
            if ok:
                n_ok += 1
            else:
                n_skip += 1
        feedback.pushInfo(f'  Qualifiziert: {n_ok} exportiert, {n_skip} ohne keycol')

        # Pass 2 — DB_NO_PEAK
        n_db = n_db_skip = 0
        raw_idx = None
        raw_by_feature_id = {}
        raw_by_attr_fid = {}

        if raw_lyr is not None:
            feedback.pushInfo('Roh-Peak-Layer geladen: %s Features' % raw_lyr.featureCount())
            feedback.pushInfo('Roh-Peak-Felder: ' + ', '.join([f.name() for f in raw_lyr.fields()]))
            raw_idx = QgsSpatialIndex()
            for rf in raw_lyr.getFeatures():
                raw_copy = QgsFeature(rf)
                raw_by_feature_id[int(rf.id())] = raw_copy
                raw_idx.addFeature(rf)
                # Some GeoPackage exports preserve the original feature id as an
                # attribute called fid. Keep this as a robust fallback because
                # DB_NO_PEAK diagnostics usually store raw_peak_fid from Step 5.
                if rf.fieldNameIndex('fid') >= 0:
                    try:
                        raw_by_attr_fid[int(float(rf['fid']))] = raw_copy
                    except Exception:
                        pass

        def _raw_feature_from_id(raw_peak_fid):
            rid = self._f(raw_peak_fid)
            if rid is None:
                return None
            rid = int(rid)
            return raw_by_feature_id.get(rid) or raw_by_attr_fid.get(rid)

        def _first_existing_num(feat, *names):
            """Return the first numeric field value that is really usable.

            QGIS/OGR NULL values are not always plain Python None. Some arrive
            as QVariant/QPyNullVariant-like objects. Testing only `val is not
            None` can therefore keep a NULL marker, which later converts to
            NULL in the output although the control-flow counted it as present.
            This helper validates every candidate through self._f().
            """
            if feat is None:
                return None
            for name in names:
                if feat.fieldNameIndex(name) >= 0:
                    val = self._f(feat[name])
                    if val is not None:
                        return val
            return None

        def _enrich_from_raw_peak(raw_feat, kx, ky, z_sattel, z_gipfel, prom_ref):
            """Fill missing DB_NO_PEAK export values from a raw peak feature.

            Important: keep already known values from the diagnosed DB row, but
            copy raw key-col elevation / raw peak height / raw prominence when
            they are missing. This fixes raw-only under-threshold cases where
            raw_peak_keycol_x/y already exist, but z_sattel stayed NULL in the
            old export because the nearest-neighbour fallback was not triggered.
            """
            if raw_feat is None:
                return kx, ky, z_sattel, z_gipfel, prom_ref
            if self._f(kx) is None:
                kx = _first_existing_num(raw_feat, 'keycol_x', 'raw_peak_keycol_x')
            if self._f(ky) is None:
                ky = _first_existing_num(raw_feat, 'keycol_y', 'raw_peak_keycol_y')
            if self._f(z_sattel) is None:
                z_sattel = _first_existing_num(raw_feat, 'keycol', 'z_sattel', 'z_col', 'z1m_col_min', 'z1m_col_1', 'z1m_col_minimum')
            if self._f(z_gipfel) is None:
                z_gipfel = _first_existing_num(raw_feat, 'z1m_max', 'zpk_1', 'zpk_', 'z_gipfel')
            if self._f(prom_ref) is None:
                prom_ref = _first_existing_num(raw_feat, 'prom_ref', 'prom', 'raw_peak_prom')
            return kx, ky, z_sattel, z_gipfel, prom_ref

        if matched is not None:
            feedback.pushInfo('Verarbeite DB_NO_PEAK aus Matched/Diagnosed...')
            n_raw_by_id = 0
            n_raw_by_nearest = 0
            n_formula_sattel = 0
            n_remaining_z_sattel_null = 0
            for mf in matched.getFeatures():
                if feedback.isCanceled():
                    break
                if _fv(mf, 'sota_status') != 'DB_NO_PEAK':
                    continue
                if mf.geometry() is None or mf.geometry().isEmpty():
                    n_db_skip += 1
                    continue

                raw_peak_fid = _fv(mf, 'raw_peak_fid')
                raw_peak_dist_m = _fv(mf, 'raw_peak_dist_m')
                kx = _first_existing_num(mf, 'raw_peak_keycol_x')
                ky = _first_existing_num(mf, 'raw_peak_keycol_y')
                z_sattel = _first_existing_num(mf, 'keycol')
                z_gipfel = _first_existing_num(mf, 'z1m_max', 'zpk_1')
                prom_ref = None

                # First preference: Step-5 raw-peak id. This is the important
                # path for DB_NO_PEAK_RAW_ONLY rows where keycol coordinates are
                # already present but the old export left z_sattel empty.
                raw_feat = _raw_feature_from_id(raw_peak_fid)
                if raw_feat is not None:
                    n_raw_by_id += 1
                kx, ky, z_sattel, z_gipfel, prom_ref = _enrich_from_raw_peak(
                    raw_feat, kx, ky, z_sattel, z_gipfel, prom_ref)

                # Fallback: nearest raw peak search, used when Step 5 did not
                # provide a usable raw_peak_fid or key-col coordinates.
                if (self._f(kx) is None or self._f(ky) is None or self._f(z_sattel) is None or self._f(z_gipfel) is None or self._f(prom_ref) is None) and raw_idx is not None:
                    best_rf = None
                    best_d = radius + 1.0
                    for cid in raw_idx.nearestNeighbor(mf.geometry().asPoint(), candidate_count):
                        rf = raw_by_feature_id.get(int(cid))
                        if rf is None or rf.geometry() is None or rf.geometry().isEmpty():
                            continue
                        d = mf.geometry().distance(rf.geometry())
                        if d <= radius and d < best_d:
                            best_d = d
                            best_rf = rf
                    if best_rf is not None:
                        raw_peak_fid = int(best_rf.id())
                        raw_peak_dist_m = round(float(best_d), 1)
                        n_raw_by_nearest += 1
                        kx, ky, z_sattel, z_gipfel, prom_ref = _enrich_from_raw_peak(
                            best_rf, kx, ky, z_sattel, z_gipfel, prom_ref)

                if self._f(z_gipfel) is None:
                    z_gipfel = _first_existing_num(mf, 'sota_elev')
                if self._f(prom_ref) is None:
                    prom_ref = _first_existing_num(mf, 'raw_peak_prom')
                # Last-resort transparency fallback: if the raw peak carries a
                # prominence but no saddle elevation was copied, derive the
                # saddle from height - prominence. For raw-only DB_NO_PEAK rows
                # this is better than exporting an empty z_sattel; it does NOT
                # change any SOTA role or threshold decision.
                if self._f(z_sattel) is None and self._f(z_gipfel) is not None and self._f(prom_ref) is not None:
                    z_sattel = self._f(z_gipfel) - self._f(prom_ref)
                    n_formula_sattel += 1
                if self._f(z_sattel) is None:
                    n_remaining_z_sattel_null += 1

                attrs = _common_attrs(mf, int(_fv(mf, 'fid') or mf.id()), z_sattel, z_gipfel, prom_ref,
                                      raw_peak_fid=raw_peak_fid, raw_peak_dist_m=raw_peak_dist_m)
                ok = _write(mf.geometry(), kx, ky, attrs)
                if ok:
                    n_db += 1
                else:
                    n_db_skip += 1
            feedback.pushInfo(f'  DB_NO_PEAK: {n_db} mit Sattel, {n_db_skip} ohne exportierbaren Sattel')
            feedback.pushInfo(f'  Raw-Enrichment: {n_raw_by_id} via raw_peak_fid, {n_raw_by_nearest} via nearest raw peak')
            feedback.pushInfo(f'  z_sattel fallback aus z_gipfel-prom_ref: {n_formula_sattel}')
            feedback.pushInfo(f'  verbleibende DB_NO_PEAK mit z_sattel NULL: {n_remaining_z_sattel_null}')
        else:
            feedback.pushInfo('  Kein matched/diagnosed layer angegeben — DB_NO_PEAK Sättel nicht exportiert.')

        feedback.pushInfo('=== Schlüsselsattel-Export ===')
        feedback.pushInfo(f'  Qualifizierte Gipfel: {n_ok}')
        feedback.pushInfo(f'  DB_NO_PEAK mit Sattel: {n_db}')
        feedback.pushInfo(f'  Gesamt: {n_ok + n_db}')
        feedback.pushInfo('==============================')

        return {self.P_POINTS: pt_id, self.P_LINES: ln_id}


def classFactory(iface=None):
    return AT_SOTA_Export_Keycol()
