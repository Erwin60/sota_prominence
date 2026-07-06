# -*- coding: utf-8 -*-
"""
AT_SOTA_Ambiguity_Diagnosis.py  —  Step 5b  v1.1

Diagnostic enrichment after AT_SOTA_Match_DB.

Principles
----------
1) The core prominence / summit logic is NOT changed.
2) MATCH_ELEV is treated primarily as a position-vs-height review class:
   many cases lie on or very near the official point and are not true
   summit-pair ambiguities.
3) NEW_CALC <-> DB_NO_PEAK is treated as a partner / shared-keycol problem.
4) Official points and official<->calculated links are written not only for
   strict ambiguity cases, but also for review-relevant diagnostic link cases.
"""

from qgis.PyQt.QtCore import QVariant
from qgis.core import (
    QgsProcessing, QgsProcessingAlgorithm,
    QgsProcessingParameterVectorLayer,
    QgsProcessingParameterNumber,
    QgsProcessingParameterFeatureSink,
    QgsProcessingException,
    QgsFeature, QgsField, QgsFields, QgsGeometry, QgsPointXY,
    QgsWkbTypes, QgsFeatureSink, QgsSpatialIndex, QgsFeatureRequest,
)


class AT_SOTA_Ambiguity_Diagnosis(QgsProcessingAlgorithm):

    P_MATCHED = 'matched_layer'
    P_PAIR_RADIUS = 'ambiguity_pair_radius'
    P_ZDIFF = 'ambiguity_zdiff'
    P_SOFT_ZDIFF = 'ambiguity_soft_zdiff'
    P_KEYCOL = 'ambiguity_keycol_dist'
    P_MATCH_ELEV_LINK = 'match_elev_link_radius'
    P_OUTPUT = 'output'
    P_OFFICIAL = 'output_official_points'
    P_LINKS = 'output_links'

    def name(self):
        return 'AT_SOTA_Ambiguity_Diagnosis'

    def displayName(self):
        return 'AT SOTA Ambiguity Diagnosis (Step 5b v1.1 review-aware)'

    def group(self):
        return 'AT SOTA Pipeline'

    def groupId(self):
        return 'at_sota'

    def createInstance(self):
        return AT_SOTA_Ambiguity_Diagnosis()

    def initAlgorithm(self, config=None):
        self.addParameter(QgsProcessingParameterVectorLayer(
            self.P_MATCHED,
            'AT_SOTA_Matched.gpkg (from Step 5)',
            [QgsProcessing.TypeVectorPoint]))
        self.addParameter(QgsProcessingParameterNumber(
            self.P_PAIR_RADIUS,
            'Radius für NEW_CALC <-> DB_NO_PEAK Partnerdiagnose (m)',
            type=QgsProcessingParameterNumber.Double,
            defaultValue=800.0, minValue=50.0, maxValue=2000.0))
        self.addParameter(QgsProcessingParameterNumber(
            self.P_ZDIFF,
            'Strenge Max. Höhendifferenz für Partnerfälle (m)',
            type=QgsProcessingParameterNumber.Double,
            defaultValue=5.0, minValue=0.0, maxValue=50.0))
        self.addParameter(QgsProcessingParameterNumber(
            self.P_SOFT_ZDIFF,
            'Weiche Max. Höhendifferenz für Partnerfälle (m)',
            type=QgsProcessingParameterNumber.Double,
            defaultValue=10.0, minValue=0.0, maxValue=50.0))
        self.addParameter(QgsProcessingParameterNumber(
            self.P_KEYCOL,
            'Max. Key-Col Distanz für shared-keycol Diagnose (m)',
            type=QgsProcessingParameterNumber.Double,
            defaultValue=40.0, minValue=0.0, maxValue=500.0))
        self.addParameter(QgsProcessingParameterNumber(
            self.P_MATCH_ELEV_LINK,
            'Review-Link-Radius für MATCH_ELEV -> offizieller Gipfel (m)',
            type=QgsProcessingParameterNumber.Double,
            defaultValue=250.0, minValue=20.0, maxValue=1000.0))
        self.addParameter(QgsProcessingParameterFeatureSink(
            self.P_OUTPUT,
            'AT_SOTA_Matched diagnostisch erweitert',
            QgsProcessing.TypeVectorPoint))
        self.addParameter(QgsProcessingParameterFeatureSink(
            self.P_OFFICIAL,
            'Offizielle Gipfelpunkte für Review-/Ambiguitätsfälle',
            QgsProcessing.TypeVectorPoint))
        self.addParameter(QgsProcessingParameterFeatureSink(
            self.P_LINKS,
            'Linien offizieller Gipfel ↔ berechneter Gipfel',
            QgsProcessing.TypeVectorLine))

    @staticmethod
    def _f(value):
        try:
            if value is None:
                return None
            v = float(value)
            if v != v:
                return None
            return v
        except Exception:
            return None

    @staticmethod
    def _safe_str(v):
        if v is None:
            return None
        s = str(v).strip()
        return s if s else None

    @staticmethod
    def _point_geom(x, y):
        if x is None or y is None:
            return None
        try:
            return QgsGeometry.fromPointXY(QgsPointXY(float(x), float(y)))
        except Exception:
            return None

    @classmethod
    def _current_z(cls, rec):
        for key in ('z1m_max', 'zpk_1', 'sota_elev'):
            v = cls._f(rec.get(key))
            if v is not None:
                return v
        return None

    @classmethod
    def _official_name(cls, rec):
        name = cls._safe_str(rec.get('official_display_name'))
        if name:
            return name, cls._safe_str(rec.get('official_name_source')) or 'UNKNOWN'
        name = cls._safe_str(rec.get('NAME'))
        if name:
            return name, 'BEV_NAME'
        name = cls._safe_str(rec.get('sota_name'))
        if name:
            return name, 'SOTA_DB'
        return None, 'UNKNOWN'

    @classmethod
    def _official_geom(cls, rec):
        ox = cls._f(rec.get('official_x'))
        oy = cls._f(rec.get('official_y'))
        g = cls._point_geom(ox, oy)
        if g is not None:
            return g
        if rec.get('sota_status') in ('DB_NO_PEAK', 'FOREIGN_PEAK'):
            return rec.get('_geom')
        return None

    @classmethod
    def _keycol_geom(cls, rec):
        for xk, yk in (
            ('keycol_x', 'keycol_y'),
            ('raw_peak_keycol_x', 'raw_peak_keycol_y'),
            ('nearest_calc_keycol_x', 'nearest_calc_keycol_y'),
        ):
            kx = cls._f(rec.get(xk))
            ky = cls._f(rec.get(yk))
            if kx is not None and ky is not None:
                return cls._point_geom(kx, ky)
        return None

    @staticmethod
    def _geom_distance(g1, g2):
        if g1 is None or g2 is None or g1.isEmpty() or g2.isEmpty():
            return None
        try:
            return float(g1.distance(g2))
        except Exception:
            return None

    @classmethod
    def _keycol_distance(cls, rec_a, rec_b):
        return cls._geom_distance(cls._keycol_geom(rec_a), cls._keycol_geom(rec_b))

    @staticmethod
    def _ambiguity_score(partner_dist, z_diff, keycol_dist, pair_radius, zdiff_thr, keycol_thr):
        score = 0
        if partner_dist is not None:
            if partner_dist <= min(250.0, pair_radius * 0.4):
                score += 2
            elif partner_dist <= pair_radius:
                score += 1
        if z_diff is not None:
            if z_diff <= 3.0:
                score += 2
            elif z_diff <= zdiff_thr:
                score += 1
        if keycol_dist is not None:
            if keycol_dist <= min(20.0, keycol_thr):
                score += 2
            elif keycol_dist <= keycol_thr:
                score += 1
        return score

    @staticmethod
    def _manual_review_required(refined_status):
        if refined_status in (
            'MATCH_OK',
            'MATCH_ELEV_EXACT_POS_DB_ISSUE',
            'MATCH_ELEV_NEAR_POS_DB_ISSUE',
            'NEW_CALC_STRONG',
            'NEW_CALC_STRONG_BORDER',
        ):
            return 0
        return 1

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

    def processAlgorithm(self, parameters, context, feedback):
        matched = self.parameterAsVectorLayer(parameters, self.P_MATCHED, context)
        pair_radius = float(self.parameterAsDouble(parameters, self.P_PAIR_RADIUS, context))
        zdiff_thr = float(self.parameterAsDouble(parameters, self.P_ZDIFF, context))
        soft_zdiff_thr = float(self.parameterAsDouble(parameters, self.P_SOFT_ZDIFF, context))
        keycol_thr = float(self.parameterAsDouble(parameters, self.P_KEYCOL, context))
        match_elev_link_radius = float(self.parameterAsDouble(parameters, self.P_MATCH_ELEV_LINK, context))

        if matched is None or not matched.isValid():
            raise QgsProcessingException('Matched-Layer konnte nicht geladen werden.')

        crs = matched.crs()

        out_fields = QgsFields()
        for fld in matched.fields():
            out_fields.append(fld)

        def _add(name, typ, ln=0, pr=0):
            if out_fields.indexFromName(name) < 0:
                out_fields.append(QgsField(name, typ, '', ln, pr, ''))

        _add('ambiguity_flag', QVariant.Int)
        _add('diagnostic_link_flag', QVariant.Int)
        _add('diagnostic_link_reason', QVariant.String, 40)
        _add('ambiguity_class', QVariant.String, 48)
        _add('ambiguity_score', QVariant.Int)
        _add('same_keycol_flag', QVariant.Int)
        _add('reciprocal_nearest_flag', QVariant.Int)
        _add('ambiguity_partner_fid', QVariant.Int)
        _add('ambiguity_partner_status', QVariant.String, 24)
        _add('ambiguity_partner_ref', QVariant.String, 30)
        _add('ambiguity_partner_name', QVariant.String, 120)
        _add('ambiguity_partner_dist_m', QVariant.Double, 10, 1)
        _add('ambiguity_partner_z_diff_m', QVariant.Double, 10, 1)
        _add('ambiguity_partner_keycol_dist_m', QVariant.Double, 10, 1)
        _add('official_peak_dist_m', QVariant.Double, 10, 1)
        _add('official_preference', QVariant.String, 32)
        _add('sota_status_refined', QVariant.String, 48)
        _add('new_calc_class', QVariant.String, 40)
        _add('manual_review_required', QVariant.Int)

        sink, sink_id = self.parameterAsSink(
            parameters, self.P_OUTPUT, context, out_fields,
            QgsWkbTypes.Point, crs)
        if sink is None:
            raise QgsProcessingException('Output-Sink für Diagnosed-Layer konnte nicht erstellt werden.')

        off_fields = QgsFields()
        for name, typ, ln, pr in [
            ('source_fid', QVariant.Int, 0, 0),
            ('source_status', QVariant.String, 24, 0),
            ('source_ref', QVariant.String, 30, 0),
            ('sota_status_refined', QVariant.String, 48, 0),
            ('ambiguity_class', QVariant.String, 48, 0),
            ('diagnostic_link_reason', QVariant.String, 40, 0),
            ('official_display_name', QVariant.String, 120, 0),
            ('official_name_source', QVariant.String, 16, 0),
            ('official_preference', QVariant.String, 32, 0),
            ('partner_dist_m', QVariant.Double, 10, 1),
            ('partner_z_diff_m', QVariant.Double, 10, 1),
            ('same_keycol_flag', QVariant.Int, 0, 0),
            ('reciprocal_nearest_flag', QVariant.Int, 0, 0),
            ('admin_context', QVariant.String, 24, 0),
            ('land', QVariant.String, 60, 0),
        ]:
            off_fields.append(QgsField(name, typ, '', ln, pr, ''))
        off_sink, off_id = self.parameterAsSink(
            parameters, self.P_OFFICIAL, context, off_fields,
            QgsWkbTypes.Point, crs)
        if off_sink is None:
            raise QgsProcessingException('Output-Sink für offizielle Punkte konnte nicht erstellt werden.')

        link_fields = QgsFields()
        for name, typ, ln, pr in [
            ('source_fid', QVariant.Int, 0, 0),
            ('source_status', QVariant.String, 24, 0),
            ('source_ref', QVariant.String, 30, 0),
            ('sota_status_refined', QVariant.String, 48, 0),
            ('ambiguity_class', QVariant.String, 48, 0),
            ('diagnostic_link_reason', QVariant.String, 40, 0),
            ('official_display_name', QVariant.String, 120, 0),
            ('official_name_source', QVariant.String, 16, 0),
            ('official_preference', QVariant.String, 32, 0),
            ('partner_fid', QVariant.Int, 0, 0),
            ('partner_status', QVariant.String, 24, 0),
            ('partner_ref', QVariant.String, 30, 0),
            ('partner_name', QVariant.String, 120, 0),
            ('partner_dist_m', QVariant.Double, 10, 1),
            ('partner_z_diff_m', QVariant.Double, 10, 1),
            ('partner_keycol_dist_m', QVariant.Double, 10, 1),
            ('same_keycol_flag', QVariant.Int, 0, 0),
            ('reciprocal_nearest_flag', QVariant.Int, 0, 0),
            ('line_len_m', QVariant.Double, 10, 1),
            ('admin_context', QVariant.String, 24, 0),
            ('land', QVariant.String, 60, 0),
        ]:
            link_fields.append(QgsField(name, typ, '', ln, pr, ''))
        link_sink, link_id = self.parameterAsSink(
            parameters, self.P_LINKS, context, link_fields,
            QgsWkbTypes.LineString, crs)
        if link_sink is None:
            raise QgsProcessingException('Output-Sink für Ambiguity-Links konnte nicht erstellt werden.')

        records = []
        rec_by_id = {}
        calc_index = QgsSpatialIndex()
        db_index = QgsSpatialIndex()

        for feat in matched.getFeatures():
            geom = feat.geometry()
            if geom is None or geom.isEmpty():
                continue
            rec = {f.name(): feat[f.name()] for f in matched.fields()}
            rec['_fid'] = int(feat.id())
            rec['_feat'] = feat
            rec['_geom'] = geom
            rec['_status'] = self._safe_str(rec.get('sota_status'))
            rec['_row_origin'] = self._safe_str(rec.get('row_origin'))
            rec['_z'] = self._current_z(rec)
            rec['_official_geom'] = self._official_geom(rec)
            rec['_keycol_geom'] = self._keycol_geom(rec)
            name, name_src = self._official_name(rec)
            rec['_official_display_name'] = name
            rec['_official_name_source'] = name_src
            records.append(rec)
            rec_by_id[rec['_fid']] = rec

            if rec['_row_origin'] == 'CALC_PEAK' and rec['_status'] in ('MATCH_OK', 'MATCH_ELEV', 'NEW_CALC'):
                qf = QgsFeature()
                qf.setId(rec['_fid'])
                qf.setGeometry(geom)
                calc_index.addFeature(qf)

            if rec['_status'] == 'DB_NO_PEAK' and rec['_official_geom'] is not None and not rec['_official_geom'].isEmpty():
                qf = QgsFeature()
                qf.setId(rec['_fid'])
                qf.setGeometry(rec['_official_geom'])
                db_index.addFeature(qf)

        def _best_near(spatial_index, geom, radius, limit, exclude_id=None, use_official=False):
            if geom is None or geom.isEmpty():
                return None, None
            best = None
            best_d = radius + 1.0
            for cid in spatial_index.nearestNeighbor(geom.asPoint(), int(limit)):
                cid = int(cid)
                if exclude_id is not None and cid == int(exclude_id):
                    continue
                cand = rec_by_id.get(cid)
                if cand is None:
                    continue
                cand_geom = cand['_official_geom'] if use_official else cand['_geom']
                if cand_geom is None or cand_geom.isEmpty():
                    continue
                d = self._geom_distance(geom, cand_geom)
                if d is None or d > radius:
                    continue
                if d < best_d:
                    best = cand
                    best_d = d
            return best, (round(best_d, 1) if best is not None else None)

        def _best_calc_near(geom, radius, exclude_id=None):
            return _best_near(calc_index, geom, radius, 40, exclude_id=exclude_id, use_official=False)

        def _best_db_near(geom, radius, exclude_id=None):
            return _best_near(db_index, geom, radius, 40, exclude_id=exclude_id, use_official=True)

        def _reciprocal_db_calc(db_rec, calc_rec):
            if db_rec is None or calc_rec is None:
                return 0
            best_calc, _ = _best_calc_near(db_rec['_official_geom'], pair_radius, exclude_id=None)
            best_db, _ = _best_db_near(calc_rec['_geom'], pair_radius, exclude_id=None)
            if best_calc is None or best_db is None:
                return 0
            return 1 if int(best_calc['_fid']) == int(calc_rec['_fid']) and int(best_db['_fid']) == int(db_rec['_fid']) else 0

        idx = out_fields.indexFromName
        n_amb = 0
        n_diag = 0
        n_off = 0
        n_links = 0

        for rec in records:
            feat = rec['_feat']
            status = rec['_status']
            geom = rec['_geom']
            current_z = rec['_z']
            partner = None
            partner_ref = None
            partner_name = None
            partner_status = None
            partner_dist_m = None
            partner_z_diff_m = None
            partner_keycol_dist_m = None
            same_keycol_flag = 0
            reciprocal_nearest_flag = 0
            ambiguity_flag = 0
            diagnostic_link_flag = 0
            diagnostic_link_reason = None
            ambiguity_class = None
            ambiguity_score = None
            official_preference = None
            refined_status = status
            new_calc_class = None
            official_geom = rec['_official_geom']
            official_display_name = rec['_official_display_name']
            official_name_source = rec['_official_name_source']
            official_peak_dist_m = self._geom_distance(geom, official_geom)

            if status == 'MATCH_ELEV':
                pos_class = self._position_class(official_peak_dist_m)
                bev_support = self._safe_str(rec.get('bev_support'))
                review_hint = self._safe_str(rec.get('review_hint'))
                if official_peak_dist_m is not None and official_peak_dist_m <= match_elev_link_radius:
                    diagnostic_link_flag = 1
                    diagnostic_link_reason = 'MATCH_ELEV_OFFICIAL_LINK'
                if pos_class == 'EXACT_POS' and bev_support == 'BEV_SUPPORTS_CALC':
                    ambiguity_class = 'MATCH_ELEV_EXACT_POS_DB_ISSUE'
                    refined_status = ambiguity_class
                    official_preference = 'CALC_POINT_SUPPORTED'
                elif pos_class in ('VERY_NEAR_POS', 'NEAR_POS') and bev_support == 'BEV_SUPPORTS_CALC':
                    ambiguity_class = 'MATCH_ELEV_NEAR_POS_DB_ISSUE'
                    refined_status = ambiguity_class
                    official_preference = 'CALC_POINT_SUPPORTED'
                elif pos_class in ('EXACT_POS', 'VERY_NEAR_POS', 'NEAR_POS') and bev_support == 'BEV_SUPPORTS_DB':
                    ambiguity_class = 'MATCH_ELEV_POS_OK_CALC_HEIGHT_ISSUE'
                    refined_status = ambiguity_class
                    official_preference = 'DB_HEIGHT_SUPPORTED'
                elif review_hint == 'POSSIBLE_WRONG_SUMMIT_MATCH' and official_peak_dist_m is not None and official_peak_dist_m <= match_elev_link_radius:
                    ambiguity_flag = 1
                    diagnostic_link_flag = 1
                    diagnostic_link_reason = 'MATCH_ELEV_WRONG_SUMMIT_REVIEW'
                    ambiguity_class = 'MATCH_ELEV_NEAR_OFFICIAL_WRONG_SUMMIT'
                    refined_status = ambiguity_class
                    official_preference = 'OFFICIAL_SUMMIT_REVIEW'
                elif pos_class in ('RIDGE_OFFSET', 'REMOTE_OFFSET'):
                    ambiguity_class = 'MATCH_ELEV_RIDGE_REVIEW'
                    refined_status = ambiguity_class
                    official_preference = 'MANUAL_REVIEW'
                else:
                    ambiguity_class = self._safe_str(rec.get('match_case_class')) or 'MATCH_ELEV_MANUAL_REVIEW'
                    refined_status = ambiguity_class

            elif status == 'DB_NO_PEAK':
                partner, partner_dist_m = _best_calc_near(official_geom, pair_radius)
                if partner is not None:
                    partner_ref = self._safe_str(partner.get('sota_ref'))
                    partner_name = partner['_official_display_name']
                    partner_status = self._safe_str(partner.get('sota_status'))
                    partner_z = partner['_z']
                    partner_z_diff_m = abs((current_z or 0.0) - (partner_z or 0.0)) if current_z is not None and partner_z is not None else None
                    partner_keycol_dist_m = self._keycol_distance(rec, partner)
                    same_keycol_flag = 1 if partner_keycol_dist_m is not None and partner_keycol_dist_m <= keycol_thr else 0
                    reciprocal_nearest_flag = _reciprocal_db_calc(rec, partner)
                    ambiguity_score = self._ambiguity_score(partner_dist_m, partner_z_diff_m, partner_keycol_dist_m, pair_radius, zdiff_thr, keycol_thr)
                    if partner_z_diff_m is not None and partner_z_diff_m <= zdiff_thr and same_keycol_flag == 1 and reciprocal_nearest_flag == 1:
                        ambiguity_flag = 1
                        diagnostic_link_flag = 1
                        diagnostic_link_reason = 'DB_NO_PEAK_SHARED_KEYCOL'
                        ambiguity_class = 'DB_NO_PEAK_SHARED_KEYCOL_STRICT'
                        refined_status = ambiguity_class
                        official_preference = 'OFFICIAL_SUMMIT_PREFERRED'
                    elif partner_z_diff_m is not None and partner_z_diff_m <= soft_zdiff_thr and reciprocal_nearest_flag == 1:
                        diagnostic_link_flag = 1
                        diagnostic_link_reason = 'DB_NO_PEAK_NEAR_CALC_REVIEW'
                        ambiguity_class = 'DB_NO_PEAK_NEAR_CALC_REVIEW'
                        refined_status = ambiguity_class
                        official_preference = 'OFFICIAL_SUMMIT_REVIEW'
                    else:
                        diagnostic_link_flag = 1
                        diagnostic_link_reason = 'DB_NO_PEAK_NEAR_CALC'
                        ambiguity_class = 'DB_NO_PEAK_NEAR_CALC_NO_KEYCOL' if same_keycol_flag == 0 else 'DB_NO_PEAK_NEAR_CALC'
                        refined_status = ambiguity_class
                        official_preference = 'MANUAL_REVIEW'
                else:
                    if int(rec.get('raw_peak_found') or 0) == 1:
                        ambiguity_class = 'DB_NO_PEAK_RAW_ONLY'
                    else:
                        ambiguity_class = 'DB_NO_PEAK_NO_RAW_NO_CALC'
                    refined_status = ambiguity_class

            elif status == 'NEW_CALC':
                partner, partner_dist_m = _best_db_near(geom, pair_radius)
                prom_ref = self._f(rec.get('prom_ref'))
                name_match = int(rec.get('name_match') or 0)
                has_bev = 1 if self._f(rec.get('z_bev')) is not None else 0
                if partner is not None:
                    partner_ref = self._safe_str(partner.get('sota_ref'))
                    partner_name = partner['_official_display_name']
                    partner_status = self._safe_str(partner.get('sota_status'))
                    partner_z = self._f(partner.get('sota_elev')) or partner['_z']
                    partner_z_diff_m = abs((current_z or 0.0) - (partner_z or 0.0)) if current_z is not None and partner_z is not None else None
                    partner_keycol_dist_m = self._keycol_distance(rec, partner)
                    same_keycol_flag = 1 if partner_keycol_dist_m is not None and partner_keycol_dist_m <= keycol_thr else 0
                    reciprocal_nearest_flag = _reciprocal_db_calc(partner, rec)
                    ambiguity_score = self._ambiguity_score(partner_dist_m, partner_z_diff_m, partner_keycol_dist_m, pair_radius, zdiff_thr, keycol_thr)
                    if partner_z_diff_m is not None and partner_z_diff_m <= zdiff_thr and same_keycol_flag == 1 and reciprocal_nearest_flag == 1:
                        ambiguity_flag = 1
                        diagnostic_link_flag = 1
                        diagnostic_link_reason = 'NEW_CALC_SHARED_KEYCOL'
                        ambiguity_class = 'NEW_CALC_SHARED_KEYCOL_PAIR'
                        refined_status = ambiguity_class
                        new_calc_class = ambiguity_class
                        official_preference = 'OFFICIAL_SUMMIT_PREFERRED'
                        official_geom = partner['_official_geom']
                        official_display_name = partner['_official_display_name']
                        official_name_source = partner['_official_name_source']
                        official_peak_dist_m = self._geom_distance(geom, official_geom)
                    elif partner_z_diff_m is not None and partner_z_diff_m <= soft_zdiff_thr and reciprocal_nearest_flag == 1:
                        diagnostic_link_flag = 1
                        diagnostic_link_reason = 'NEW_CALC_NEAR_DB_REVIEW'
                        ambiguity_class = 'NEW_CALC_NEAR_DB_REVIEW'
                        refined_status = ambiguity_class
                        new_calc_class = ambiguity_class
                        official_preference = 'OFFICIAL_SUMMIT_REVIEW'
                        official_geom = partner['_official_geom']
                        official_display_name = partner['_official_display_name']
                        official_name_source = partner['_official_name_source']
                        official_peak_dist_m = self._geom_distance(geom, official_geom)
                    elif prom_ref is not None and prom_ref >= 250.0 and name_match == 1 and int(rec.get('admin_review') or 0) == 0:
                        ambiguity_class = 'NEW_CALC_STRONG'
                        refined_status = ambiguity_class
                        new_calc_class = ambiguity_class
                    elif prom_ref is not None and prom_ref >= 180.0 and int(rec.get('admin_review') or 0) == 1:
                        ambiguity_class = 'NEW_CALC_STRONG_BORDER'
                        refined_status = ambiguity_class
                        new_calc_class = ambiguity_class
                    elif name_match == 0 and has_bev == 0:
                        ambiguity_class = 'NEW_CALC_LOW_REFERENCE'
                        refined_status = ambiguity_class
                        new_calc_class = ambiguity_class
                    else:
                        diagnostic_link_flag = 1 if official_geom is not None else 0
                        diagnostic_link_reason = 'NEW_CALC_MANUAL_REVIEW' if diagnostic_link_flag == 1 else None
                        ambiguity_class = 'NEW_CALC_MANUAL_REVIEW'
                        refined_status = ambiguity_class
                        new_calc_class = ambiguity_class
                else:
                    if prom_ref is not None and prom_ref >= 250.0 and name_match == 1 and int(rec.get('admin_review') or 0) == 0:
                        ambiguity_class = 'NEW_CALC_STRONG'
                    elif prom_ref is not None and prom_ref >= 180.0 and int(rec.get('admin_review') or 0) == 1:
                        ambiguity_class = 'NEW_CALC_STRONG_BORDER'
                    elif name_match == 0 and has_bev == 0:
                        ambiguity_class = 'NEW_CALC_LOW_REFERENCE'
                    else:
                        ambiguity_class = 'NEW_CALC_MANUAL_REVIEW'
                    refined_status = ambiguity_class
                    new_calc_class = ambiguity_class

            if partner is not None:
                partner_ref = partner_ref or self._safe_str(partner.get('sota_ref'))
                partner_status = partner_status or self._safe_str(partner.get('sota_status'))

            attrs = list(feat.attributes()) + [None] * (out_fields.count() - len(feat.attributes()))
            if idx('ambiguity_flag') >= 0:
                attrs[idx('ambiguity_flag')] = ambiguity_flag
            if idx('diagnostic_link_flag') >= 0:
                attrs[idx('diagnostic_link_flag')] = diagnostic_link_flag
            if idx('diagnostic_link_reason') >= 0:
                attrs[idx('diagnostic_link_reason')] = diagnostic_link_reason
            if idx('ambiguity_class') >= 0:
                attrs[idx('ambiguity_class')] = ambiguity_class
            if idx('ambiguity_score') >= 0:
                attrs[idx('ambiguity_score')] = ambiguity_score
            if idx('same_keycol_flag') >= 0:
                attrs[idx('same_keycol_flag')] = same_keycol_flag
            if idx('reciprocal_nearest_flag') >= 0:
                attrs[idx('reciprocal_nearest_flag')] = reciprocal_nearest_flag
            if idx('ambiguity_partner_fid') >= 0:
                attrs[idx('ambiguity_partner_fid')] = int(partner['_fid']) if partner is not None else None
            if idx('ambiguity_partner_status') >= 0:
                attrs[idx('ambiguity_partner_status')] = partner_status
            if idx('ambiguity_partner_ref') >= 0:
                attrs[idx('ambiguity_partner_ref')] = partner_ref
            if idx('ambiguity_partner_name') >= 0:
                attrs[idx('ambiguity_partner_name')] = partner_name
            if idx('ambiguity_partner_dist_m') >= 0:
                attrs[idx('ambiguity_partner_dist_m')] = partner_dist_m
            if idx('ambiguity_partner_z_diff_m') >= 0:
                attrs[idx('ambiguity_partner_z_diff_m')] = partner_z_diff_m
            if idx('ambiguity_partner_keycol_dist_m') >= 0:
                attrs[idx('ambiguity_partner_keycol_dist_m')] = partner_keycol_dist_m
            if idx('official_peak_dist_m') >= 0:
                attrs[idx('official_peak_dist_m')] = official_peak_dist_m
            if idx('official_preference') >= 0:
                attrs[idx('official_preference')] = official_preference
            if idx('sota_status_refined') >= 0:
                attrs[idx('sota_status_refined')] = refined_status
            if idx('new_calc_class') >= 0:
                attrs[idx('new_calc_class')] = new_calc_class
            if idx('manual_review_required') >= 0:
                attrs[idx('manual_review_required')] = self._manual_review_required(refined_status)
            if idx('official_display_name') >= 0 and official_display_name not in (None, ''):
                attrs[idx('official_display_name')] = official_display_name
            if idx('official_name_source') >= 0 and official_name_source not in (None, ''):
                attrs[idx('official_name_source')] = official_name_source
            if idx('official_x') >= 0 and official_geom is not None and not official_geom.isEmpty():
                attrs[idx('official_x')] = official_geom.asPoint().x()
            if idx('official_y') >= 0 and official_geom is not None and not official_geom.isEmpty():
                attrs[idx('official_y')] = official_geom.asPoint().y()

            out_f = QgsFeature(out_fields)
            out_f.setGeometry(geom)
            out_f.setAttributes(attrs)
            sink.addFeature(out_f, QgsFeatureSink.FastInsert)

            if ambiguity_flag == 1:
                n_amb += 1
            if diagnostic_link_flag == 1:
                n_diag += 1

            if (ambiguity_flag == 1 or diagnostic_link_flag == 1) and official_geom is not None and not official_geom.isEmpty():
                of = QgsFeature(off_fields)
                of.setGeometry(official_geom)
                of.setAttributes([
                    rec['_fid'], status, self._safe_str(rec.get('sota_ref')), refined_status,
                    ambiguity_class, diagnostic_link_reason,
                    official_display_name, official_name_source,
                    official_preference, partner_dist_m, partner_z_diff_m,
                    same_keycol_flag, reciprocal_nearest_flag,
                    rec.get('admin_context'), rec.get('land')
                ])
                off_sink.addFeature(of, QgsFeatureSink.FastInsert)
                n_off += 1

                partner_geom = geom
                if status == 'DB_NO_PEAK' and partner is not None:
                    partner_geom = partner['_geom']
                if partner_geom is not None and not partner_geom.isEmpty():
                    line = QgsGeometry.fromPolylineXY([
                        official_geom.asPoint(),
                        partner_geom.asPoint()
                    ])
                    lf = QgsFeature(link_fields)
                    lf.setGeometry(line)
                    lf.setAttributes([
                        rec['_fid'], status, self._safe_str(rec.get('sota_ref')), refined_status,
                        ambiguity_class, diagnostic_link_reason,
                        official_display_name, official_name_source,
                        official_preference,
                        int(partner['_fid']) if partner is not None else None,
                        partner_status, partner_ref, partner_name,
                        partner_dist_m, partner_z_diff_m, partner_keycol_dist_m,
                        same_keycol_flag, reciprocal_nearest_flag,
                        round(float(line.length()), 1),
                        rec.get('admin_context'), rec.get('land')
                    ])
                    link_sink.addFeature(lf, QgsFeatureSink.FastInsert)
                    n_links += 1

        feedback.pushInfo('=== Ambiguity Diagnosis v1.1 ===')
        feedback.pushInfo(f'  Eingabefeatures: {len(records)}')
        feedback.pushInfo(f'  Strenge Ambiguity-Faelle: {n_amb}')
        feedback.pushInfo(f'  Diagnostic-Link-Faelle: {n_diag}')
        feedback.pushInfo(f'  Offizielle Punkte: {n_off}')
        feedback.pushInfo(f'  Offizieller ↔ berechneter Link: {n_links}')
        feedback.pushInfo('===============================')

        return {
            self.P_OUTPUT: sink_id,
            self.P_OFFICIAL: off_id,
            self.P_LINKS: link_id,
        }


def classFactory(iface=None):
    return AT_SOTA_Ambiguity_Diagnosis()
