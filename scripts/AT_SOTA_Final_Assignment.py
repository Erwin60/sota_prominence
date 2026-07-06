# -*- coding: utf-8 -*-
"""
AT_SOTA_Final_Assignment.py  —  Step 5d  v2.3-reviewfreeze-authbev-osm-scope-final

Purpose
-------
Final assignment layer after Step 5 (DB match), Step 5b (ambiguity diagnosis) and Step 5c (coordinate/summit-identity validation).

This step does NOT recompute prominence or key cols. It only assigns a
review-facing final summit role for each row:
  - final summit,
  - secondary shared-key-col candidate,
  - review hold,
  - replay hold,
  - unresolved reference / foreign context.

Main output
-----------
AT_SOTA_Matched_Assigned.gpkg

Design principles
-----------------
1) Preserve the productive main classes; add a new explicit decision layer.
2) Use BEV-supported official coordinates and heights as anchors whenever
   they clearly support one candidate.
3) Treat shared-key-col / low-z-difference pairs as summit groups rather
   than isolated rows.
4) Leave ridge-offset and hard residuals unresolved unless the evidence
   is strong enough for a reviewer-safe assignment.
5) If Step 5c coordinate/summit-identity validation flags a row, do not
   silently finalise it. Escalate affected final or shared-key-col rows to
   review-visible outcomes while preserving the diagnostic fields.
"""

from qgis.PyQt.QtCore import QVariant
from qgis.core import (
    QgsProcessing, QgsProcessingAlgorithm,
    QgsProcessingParameterVectorLayer,
    QgsProcessingParameterNumber,
    QgsProcessingParameterFeatureSink,
    QgsProcessingException,
    QgsFeature, QgsField, QgsFields, QgsGeometry, QgsPointXY,
    QgsWkbTypes, QgsFeatureSink,
)

class AT_SOTA_Final_Assignment(QgsProcessingAlgorithm):

    P_INPUT = 'matched_layer'
    P_PAIR_ZDIFF = 'pair_zdiff'
    P_EXACT_DIST = 'exact_official_dist'
    P_NEAR_DIST = 'near_official_dist'
    P_REPLAY_DIST = 'replay_official_dist'
    P_OUTPUT = 'output'

    def name(self):
        return 'AT_SOTA_Final_Assignment'

    def displayName(self):
        return 'AT SOTA Final Assignment (Step 5d v2.3 auth-BEV OSM scope)'

    def group(self):
        return 'AT SOTA Pipeline'

    def groupId(self):
        return 'at_sota'

    def createInstance(self):
        return AT_SOTA_Final_Assignment()

    def initAlgorithm(self, config=None):
        self.addParameter(QgsProcessingParameterVectorLayer(
            self.P_INPUT,
            'AT_SOTA_Matched_CoordValidated.gpkg',
            [QgsProcessing.TypeVectorPoint]))
        self.addParameter(QgsProcessingParameterNumber(
            self.P_PAIR_ZDIFF,
            'Max. height difference for shared-key-col auto-pairing (m)',
            type=QgsProcessingParameterNumber.Double,
            defaultValue=5.0, minValue=0.0, maxValue=50.0))
        self.addParameter(QgsProcessingParameterNumber(
            self.P_EXACT_DIST,
            'Exact official anchor distance (m)',
            type=QgsProcessingParameterNumber.Double,
            defaultValue=15.0, minValue=0.0, maxValue=200.0))
        self.addParameter(QgsProcessingParameterNumber(
            self.P_NEAR_DIST,
            'Near official anchor distance (m)',
            type=QgsProcessingParameterNumber.Double,
            defaultValue=100.0, minValue=0.0, maxValue=500.0))
        self.addParameter(QgsProcessingParameterNumber(
            self.P_REPLAY_DIST,
            'Ridge/replay threshold distance (m)',
            type=QgsProcessingParameterNumber.Double,
            defaultValue=250.0, minValue=50.0, maxValue=2000.0))
        self.addParameter(QgsProcessingParameterFeatureSink(
            self.P_OUTPUT,
            'AT_SOTA_Matched_Assigned.gpkg',
            QgsProcessing.TypeVectorPoint))

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

    @staticmethod
    def _safe_str(v):
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
    def _derive_name_evidence(cls, rec):
        """Derive BEV-authoritative and fallback name review fields.

        REMARK(v5.1-authbev): NAME/name_match describes only the BEV name at
        the calculated DEM point. It is not complete official BEV evidence.
        Authoritative BEV name evidence is present if either the calculated
        peak or the official/SOTA anchor has a BEV 7302/7303 name.
        """
        calc_name = cls._safe_str(rec.get('cv_bev_calc_name')) or cls._safe_str(rec.get('bev_calc_name')) or cls._safe_str(rec.get('NAME'))
        calc_fcode = rec.get('cv_bev_calc_fcode') if rec.get('cv_bev_calc_fcode') is not None else rec.get('bev_calc_fcode')
        off_name = cls._safe_str(rec.get('cv_bev_off_name'))
        off_fcode = rec.get('cv_bev_off_fcode')

        calc_auth = bool(calc_name and cls._is_auth_bev_fcode(calc_fcode))
        off_auth = bool(off_name and cls._is_auth_bev_fcode(off_fcode))
        if calc_auth:
            fc = int(float(calc_fcode))
            auth_available = 1
            auth_name = calc_name
            auth_source = f'BEV_CALC_{fc}'
        elif off_auth:
            fc = int(float(off_fcode))
            auth_available = 1
            auth_name = off_name
            auth_source = f'BEV_OFFICIAL_{fc}'
        else:
            auth_available = 0
            auth_name = None
            auth_source = 'NO_AUTHORITATIVE_BEV_NAME'

        sota_name = cls._safe_str(rec.get('sota_name'))
        official_display = cls._safe_str(rec.get('official_display_name'))
        sota_available = 1 if sota_name else 0
        assigned_sota_fallback = 1 if (auth_available == 0 and (sota_name or official_display)) else 0
        no_auth = 0 if auth_available else 1

        row_origin = (cls._safe_str(rec.get('row_origin')) or '').upper()
        coord = int(rec.get('cv_coordinate_review_required') or 0)
        ident = int(rec.get('cv_identity_review_required') or 0)
        replay = int(rec.get('final_replay_required') or 0) if rec.get('final_replay_required') is not None else 0
        geo_or_replay = 1 if (coord or ident or replay) else 0

        if row_origin != 'CALC_PEAK':
            queue = 'NOT_CALC_PEAK'
        elif auth_available == 1:
            queue = 'BEV_AUTH_AVAILABLE'
        elif assigned_sota_fallback:
            queue = 'N1_NO_AUTHORITATIVE_BEV_NAME_SOTA_DB_FALLBACK_WITH_GEO_OR_REPLAY' if geo_or_replay else 'N1_NO_AUTHORITATIVE_BEV_NAME_SOTA_DB_FALLBACK'
        else:
            queue = 'N1_NO_AUTHORITATIVE_BEV_NAME_WITH_GEO_OR_REPLAY' if geo_or_replay else 'N1_NO_AUTHORITATIVE_BEV_NAME_ONLY'

        # REMARK(v5.1-authbev-osm-scope): no_authoritative_bev_name is a
        # diagnostic flag and may also be true for FOREIGN_DB_CONTEXT or
        # SOTA_DB_ONLY rows. OSM fallback, however, is an operative reviewer
        # action and is allowed only for Austrian calculated-peak N1 name-
        # enrichment rows. It must not be set for foreign-context or DB-only
        # context rows.
        osm_allowed = 1 if queue.startswith('N1_NO_AUTHORITATIVE_BEV_NAME') else 0

        support = auth_source
        if auth_available == 0 and (calc_fcode == 7301 or off_fcode == 7301):
            support = 'BEV_CONTEXT_7301_ONLY'

        return {
            'bev_calc_name_available': 1 if calc_auth else 0,
            'bev_official_anchor_available': 1 if off_auth else 0,
            'bev_authoritative_name_available': auth_available,
            'bev_authoritative_name': auth_name,
            'bev_authoritative_name_source': auth_source,
            'bev_support_interpreted': support,
            'no_authoritative_bev_name': no_auth,
            'sota_db_name_available': sota_available,
            'assigned_name_is_sota_fallback': assigned_sota_fallback,
            'osm_name_review_allowed': osm_allowed,
            'name_source_review_queue': queue,
        }

    @classmethod
    def _point_geom(cls, x, y):
        fx = cls._f(x)
        fy = cls._f(y)
        if fx is None or fy is None:
            return None
        try:
            return QgsGeometry.fromPointXY(QgsPointXY(fx, fy))
        except Exception:
            return None

    @classmethod
    def _current_z(cls, rec):
        for key in ('z1m_max', 'zpk_1', 'z_bev', 'sota_elev'):
            v = cls._f(rec.get(key))
            if v is not None:
                return v
        return None

    @classmethod
    def _official_dist(cls, rec):
        # prefer explicit field from later diagnostics, otherwise derive from geometry
        for key in ('official_peak_dist_m', 'sota_dist_m'):
            v = cls._f(rec.get(key))
            if v is not None:
                return v
        geom = rec.get('_geom')
        og = cls._point_geom(rec.get('official_x'), rec.get('official_y'))
        if geom is None or og is None or geom.isEmpty() or og.isEmpty():
            return None
        try:
            return float(geom.distance(og))
        except Exception:
            return None

    @staticmethod
    def _position_class(dist_m, exact_thr, near_thr, replay_thr):
        if dist_m is None:
            return None
        d = float(dist_m)
        if d <= exact_thr:
            return 'EXACT_POS'
        if d <= 50.0:
            return 'VERY_NEAR_POS'
        if d <= near_thr:
            return 'NEAR_POS'
        if d <= replay_thr:
            return 'RIDGE_OFFSET'
        return 'REMOTE_OFFSET'

    @staticmethod
    def _row_score(rec, exact_thr, near_thr):
        score = 0.0
        off_dist = rec.get('_official_dist')
        if off_dist is not None:
            if off_dist <= exact_thr:
                score += 5.0
            elif off_dist <= 50.0:
                score += 3.0
            elif off_dist <= near_thr:
                score += 1.0

        if rec.get('_has_bev_name'):
            score += 1.5
        if rec.get('_has_bev_height'):
            score += 2.0

        op = rec.get('_official_preference')
        if op == 'OFFICIAL_SUMMIT_PREFERRED':
            score += 2.0
        elif op == 'CALC_POINT_SUPPORTED':
            score += 1.0
        elif op == 'DB_HEIGHT_SUPPORTED':
            score += 0.5

        if rec.get('_status') == 'DB_NO_PEAK':
            score += 1.0

        if rec.get('_name_match') == 1:
            score += 0.5

        bev_support = rec.get('_bev_support')
        if bev_support == 'BEV_SUPPORTS_CALC':
            score += 1.0
        elif bev_support == 'BEV_SUPPORTS_DB':
            score += 0.75

        z_bev = rec.get('_z_bev')
        if z_bev is not None:
            score += z_bev / 100000.0

        z_cur = rec.get('_z')
        if z_cur is not None:
            score += z_cur / 1000000.0
        return score

    @staticmethod
    def _singleton_decision(rec, exact_thr, near_thr, replay_thr):
        status = rec.get('_status')
        refined = rec.get('_refined') or status
        pos_class = rec.get('_position_class')
        bev_support = rec.get('_bev_support')
        review_hint = rec.get('_review_hint')
        role = 'REVIEW_REQUIRED'
        basis = 'DEFAULT_REVIEW'
        method = 'RULE_SINGLETON'
        confidence = 'LOW'
        final_flag = 0
        manual = 1
        replay = 0
        anchor_src = rec.get('_anchor_source')

        if status == 'MATCH_OK':
            role = 'FINAL_SOTA_SUMMIT'
            basis = 'MATCH_OK_STABLE'
            confidence = 'HIGH'
            final_flag = 1
            manual = 0
            anchor_src = anchor_src or 'MATCH_DB'
            return role, basis, method, confidence, final_flag, manual, replay, anchor_src

        if status == 'MATCH_ELEV':
            if pos_class in ('EXACT_POS', 'VERY_NEAR_POS', 'NEAR_POS'):
                if refined in ('MATCH_ELEV_EXACT_POS_DB_ISSUE', 'MATCH_ELEV_NEAR_POS_DB_ISSUE') or bev_support == 'BEV_SUPPORTS_CALC':
                    role = 'FINAL_SOTA_SUMMIT'
                    basis = 'DB_HEIGHT_CONFLICT_EXACT_OR_NEAR'
                    confidence = 'HIGH'
                    final_flag = 1
                    manual = 0
                    anchor_src = 'BEV_COORD'
                elif bev_support in ('BEV_SUPPORTS_DB',) or refined == 'MATCH_ELEV_POS_OK_CALC_HEIGHT_ISSUE':
                    role = 'FINAL_SOTA_SUMMIT_REVIEW'
                    basis = 'CALC_HEIGHT_CONFLICT_NEAR_OFFICIAL'
                    confidence = 'MEDIUM'
                    final_flag = 1
                    manual = 1
                    anchor_src = 'BEV_COORD'
                else:
                    role = 'FINAL_SOTA_SUMMIT_REVIEW'
                    basis = 'POS_OK_REFERENCE_POOR'
                    confidence = 'MEDIUM'
                    final_flag = 1
                    manual = 1
                    anchor_src = anchor_src or 'SOTA_DB_ONLY'
            else:
                if review_hint == 'POSSIBLE_WRONG_SUMMIT_MATCH':
                    role = 'REPLAY_REQUIRED'
                    basis = 'RIDGE_OFFSET_WRONG_SUMMIT_REVIEW'
                else:
                    role = 'REVIEW_REQUIRED'
                    basis = 'RIDGE_OFFSET_MANUAL'
                confidence = 'LOW'
                final_flag = 0
                manual = 1
                replay = 1 if pos_class in ('RIDGE_OFFSET', 'REMOTE_OFFSET') else 0
                anchor_src = anchor_src or 'BEV_COORD'
            return role, basis, method, confidence, final_flag, manual, replay, anchor_src

        if status == 'NEW_CALC':
            if refined == 'NEW_CALC_STRONG':
                role = 'FINAL_SOTA_SUMMIT'
                basis = 'CALC_STRONG_NO_CLOSE_DB'
                confidence = 'HIGH'
                final_flag = 1
                manual = 0
                anchor_src = anchor_src or 'CALC_ONLY'
            elif refined == 'NEW_CALC_STRONG_BORDER':
                role = 'FINAL_SOTA_SUMMIT_REVIEW'
                basis = 'CALC_STRONG_BORDER'
                confidence = 'MEDIUM'
                final_flag = 1
                manual = 1
                anchor_src = anchor_src or 'CALC_ONLY'
            elif refined in ('NEW_CALC_NEAR_DB_REVIEW', 'NEW_CALC_LOW_REFERENCE', 'NEW_CALC_MANUAL_REVIEW'):
                role = 'REVIEW_REQUIRED'
                basis = refined
                confidence = 'LOW'
                final_flag = 0
                manual = 1
                replay = 1 if refined == 'NEW_CALC_NEAR_DB_REVIEW' else 0
                anchor_src = anchor_src or ('BEV_COORD' if rec.get('_official_dist') is not None else 'CALC_ONLY')
            else:
                role = 'REVIEW_REQUIRED'
                basis = refined or 'NEW_CALC_REVIEW'
                confidence = 'LOW'
                final_flag = 0
                manual = 1
                anchor_src = anchor_src or 'CALC_ONLY'
            return role, basis, method, confidence, final_flag, manual, replay, anchor_src

        if status == 'DB_NO_PEAK':
            if refined == 'DB_NO_PEAK_RAW_ONLY':
                role = 'REVIEW_REQUIRED'
                basis = 'RAW_ONLY_NO_STABLE_PAIR'
                confidence = 'LOW'
                final_flag = 0
                manual = 1
                anchor_src = 'BEV_COORD'
            elif refined in ('DB_NO_PEAK_NEAR_CALC', 'DB_NO_PEAK_NEAR_CALC_REVIEW'):
                role = 'REVIEW_REQUIRED'
                basis = refined
                confidence = 'LOW'
                final_flag = 0
                manual = 1
                replay = 1
                anchor_src = 'BEV_COORD'
            elif refined == 'DB_NO_PEAK_NO_RAW_NO_CALC':
                role = 'UNRESOLVED_REFERENCE'
                basis = 'NO_RAW_NO_NEAR_CALC'
                confidence = 'LOW'
                final_flag = 0
                manual = 1
                replay = 1
                anchor_src = 'BEV_COORD'
            else:
                role = 'REVIEW_REQUIRED'
                basis = refined or 'DB_NO_PEAK_REVIEW'
                confidence = 'LOW'
                final_flag = 0
                manual = 1
                anchor_src = 'BEV_COORD'
            return role, basis, method, confidence, final_flag, manual, replay, anchor_src

        if status == 'FOREIGN_PEAK':
            role = 'FOREIGN_CONTEXT'
            basis = 'FOREIGN_REFERENCE_ONLY'
            confidence = 'HIGH'
            final_flag = 0
            manual = 0
            replay = 0
            anchor_src = anchor_src or 'SOTA_DB_ONLY'
            return role, basis, method, confidence, final_flag, manual, replay, anchor_src

        return role, basis, method, confidence, final_flag, manual, replay, anchor_src

    @classmethod
    def _coordinate_gate(cls, rec, ass):
        """Use Step 5c coordinate/summit-identity validation as a safety gate.

        v2.1 is intentionally less aggressive than v2.0. Pure coordinate
        displacement signals remain in the cv_* audit fields, but they no
        longer automatically turn every otherwise stable FINAL_SOTA_SUMMIT into
        FINAL_SOTA_SUMMIT_REVIEW. Final-role changes are reserved for hard
        coordinate/identity evidence.
        """
        coord = int(rec.get('cv_coordinate_review_required') or 0)
        ident = int(rec.get('cv_identity_review_required') or 0)
        name = int(rec.get('cv_name_review_required') or 0)
        issue_class = cls._safe_str(rec.get('cv_coord_issue_class')) or ''
        severity = cls._safe_str(rec.get('cv_coord_issue_severity')) or 'NONE'

        if not (coord or ident or name) or issue_class in ('', 'NO_COORDINATE_FLAG'):
            return ass

        hard_tokens = (
            'SHARED_KEYCOL_WITH_COORDINATE_CONFLICT',
            'CALC_HIGHER_THAN_OFFICIAL_NAMED_SUMMIT',
            'BEV_REFERENCE_POSITION_CONFLICT',
        )
        hard_identity = ident == 1 or any(tok in issue_class for tok in hard_tokens)
        hard_coordinate = severity == 'SEVERE'
        hard_review = hard_identity or hard_coordinate

        old_basis = cls._safe_str(ass.get('final_assignment_basis')) or 'UNKNOWN_BASIS'
        note = (
            'Step 5c coordinate/summit-identity validation flag: '
            f'{issue_class}' + (f' ({severity})' if severity else '')
        )

        role = cls._safe_str(ass.get('final_sota_role'))

        # Existing review/replay/unresolved rows should keep their role. Coordinate
        # validation enriches the basis when it is hard evidence, but soft coordinate
        # flags remain available through cv_* without inflating replay counts.
        if role in ('REVIEW_REQUIRED', 'UNRESOLVED_REFERENCE', 'REPLAY_REQUIRED', 'FINAL_SOTA_SUMMIT_REVIEW'):
            if hard_review:
                ass['final_manual_review_required'] = 1
                if role == 'UNRESOLVED_REFERENCE' and hard_identity:
                    ass['final_replay_required'] = 1
                ass['final_assignment_confidence'] = 'LOW' if severity in ('STRONG', 'SEVERE') or hard_identity else 'MEDIUM'
                if old_basis not in ('COORDINATE_IDENTITY_REVIEW', 'COORDINATE_REVIEW_REQUIRED', 'PAIR_COORD_IDENTITY_REVIEW'):
                    ass['final_assignment_basis'] = 'COORDINATE_REVIEW_REQUIRED'
                ass['final_review_note'] = note
            return ass

        if role == 'FINAL_SOTA_SUMMIT':
            if hard_review:
                ass['final_sota_role'] = 'FINAL_SOTA_SUMMIT_REVIEW'
                ass['final_sota_flag'] = 1
                ass['final_manual_review_required'] = 1
                ass['final_replay_required'] = 1 if hard_identity and severity in ('STRONG', 'SEVERE') else int(ass.get('final_replay_required') or 0)
                ass['final_assignment_basis'] = 'COORDINATE_IDENTITY_REVIEW' if hard_identity else 'COORDINATE_REVIEW_REQUIRED'
                ass['final_assignment_confidence'] = 'LOW' if severity in ('STRONG', 'SEVERE') or hard_identity else 'MEDIUM'
                ass['final_review_note'] = note
            else:
                # Preserve the stable final role; cv_* fields are the audit trail.
                ass['final_review_note'] = note
            return ass

        if role == 'SECONDARY_SHARED_KEYCOL':
            if hard_review:
                ass['final_sota_role'] = 'REVIEW_REQUIRED'
                ass['final_sota_flag'] = 0
                ass['final_manual_review_required'] = 1
                ass['final_replay_required'] = 1 if hard_identity or severity in ('STRONG', 'SEVERE') else int(ass.get('final_replay_required') or 0)
                ass['final_assignment_basis'] = 'PAIR_COORD_IDENTITY_REVIEW'
                ass['final_assignment_confidence'] = 'LOW'
                ass['final_review_note'] = note
            return ass

        if hard_review:
            ass['final_manual_review_required'] = 1
            ass['final_assignment_basis'] = 'COORDINATE_REVIEW_REQUIRED'
            ass['final_assignment_confidence'] = 'LOW'
            ass['final_review_note'] = note
        return ass

    def processAlgorithm(self, parameters, context, feedback):
        inp = self.parameterAsVectorLayer(parameters, self.P_INPUT, context)
        pair_zdiff = float(self.parameterAsDouble(parameters, self.P_PAIR_ZDIFF, context))
        exact_thr = float(self.parameterAsDouble(parameters, self.P_EXACT_DIST, context))
        near_thr = float(self.parameterAsDouble(parameters, self.P_NEAR_DIST, context))
        replay_thr = float(self.parameterAsDouble(parameters, self.P_REPLAY_DIST, context))

        if inp is None or not inp.isValid():
            raise QgsProcessingException('Input layer could not be loaded.')

        crs = inp.crs()
        out_fields = QgsFields()
        for fld in inp.fields():
            out_fields.append(fld)

        def _add(name, typ, ln=0, pr=0):
            if out_fields.indexFromName(name) < 0:
                out_fields.append(QgsField(name, typ, '', ln, pr, ''))

        # new final assignment fields
        _add('final_group_id', QVariant.String, 32)
        _add('final_group_type', QVariant.String, 32)
        _add('final_candidate_rank', QVariant.Int)
        _add('final_sota_flag', QVariant.Int)
        _add('final_sota_role', QVariant.String, 32)
        _add('final_assignment_basis', QVariant.String, 40)
        _add('final_assignment_method', QVariant.String, 32)
        _add('final_assignment_confidence', QVariant.String, 12)
        _add('final_manual_review_required', QVariant.Int)
        _add('final_replay_required', QVariant.Int)
        _add('final_anchor_source', QVariant.String, 24)
        _add('final_anchor_name', QVariant.String, 120)
        _add('final_anchor_dist_m', QVariant.Double, 10, 1)
        _add('final_partner_ref', QVariant.String, 30)
        _add('final_partner_status', QVariant.String, 24)
        _add('final_partner_name', QVariant.String, 120)
        _add('final_partner_dist_m', QVariant.Double, 10, 1)
        _add('final_partner_z_diff_m', QVariant.Double, 10, 1)
        _add('final_partner_keycol_dist_m', QVariant.Double, 10, 1)
        _add('final_decision_locked', QVariant.Int)
        _add('final_review_note', QVariant.String, 254)
        _add('final_review_user', QVariant.String, 64)
        _add('final_review_date', QVariant.String, 32)

        # REMARK(v5.1-authbev): production fields for name-source semantics.
        # These fields make reviewer Excel/QML/KMZ reproducible from the script
        # output and avoid post-hoc data-only corrections.
        _add('bev_calc_name_available', QVariant.Int)
        _add('bev_official_anchor_available', QVariant.Int)
        _add('bev_authoritative_name_available', QVariant.Int)
        _add('bev_authoritative_name', QVariant.String, 120)
        _add('bev_authoritative_name_source', QVariant.String, 48)
        _add('bev_support_interpreted', QVariant.String, 64)
        _add('no_authoritative_bev_name', QVariant.Int)
        _add('sota_db_name_available', QVariant.Int)
        _add('assigned_name_is_sota_fallback', QVariant.Int)
        _add('osm_name_review_allowed', QVariant.Int)
        _add('name_source_review_queue', QVariant.String, 96)
        _add('queue_manual_review', QVariant.Int)
        _add('queue_replay', QVariant.Int)
        _add('queue_name_enrichment', QVariant.Int)
        _add('review_queue_class', QVariant.String, 32)

        sink, sink_id = self.parameterAsSink(
            parameters, self.P_OUTPUT, context, out_fields,
            QgsWkbTypes.Point, crs
        )
        if sink is None:
            raise QgsProcessingException('Output sink could not be created.')

        # collect records
        records = {}
        for feat in inp.getFeatures():
            geom = feat.geometry()
            if geom is None or geom.isEmpty():
                continue
            rec = {f.name(): feat[f.name()] for f in inp.fields()}
            rec['_feat'] = feat
            rec['_fid'] = int(feat.id())
            rec['_geom'] = geom
            rec['_status'] = self._safe_str(rec.get('sota_status')) or 'UNKNOWN'
            rec['_refined'] = self._safe_str(rec.get('sota_status_refined')) or rec['_status']
            rec['_z'] = self._current_z(rec)
            rec['_z_bev'] = self._f(rec.get('z_bev'))
            rec['_official_dist'] = self._official_dist(rec)
            rec['_position_class'] = self._safe_str(rec.get('match_position_class')) or self._position_class(rec['_official_dist'], exact_thr, near_thr, replay_thr)
            rec['_official_preference'] = self._safe_str(rec.get('official_preference'))
            rec['_bev_support'] = self._safe_str(rec.get('bev_support'))
            rec['_review_hint'] = self._safe_str(rec.get('review_hint'))
            rec['_name_match'] = int(rec.get('name_match') or 0)
            name_ev = self._derive_name_evidence(rec)
            rec.update(name_ev)
            rec['_has_bev_name'] = int(name_ev.get('bev_authoritative_name_available') or 0)
            rec['_has_bev_height'] = 1 if rec['_z_bev'] is not None else 0
            rec['_anchor_source'] = 'BEV_COORD' if (self._f(rec.get('official_x')) is not None and self._f(rec.get('official_y')) is not None) else ('BEV_NAME' if rec['_has_bev_name'] else None)
            rec['_official_name'] = self._safe_str(name_ev.get('bev_authoritative_name')) or self._safe_str(rec.get('official_display_name')) or self._safe_str(rec.get('sota_name')) or self._safe_str(rec.get('NAME'))
            rec['_score'] = self._row_score(rec, exact_thr, near_thr)
            records[rec['_fid']] = rec

        # detect pair groups for shared-key-col / low-z-diff cases
        adjacency = {fid: set() for fid in records}
        for fid, rec in records.items():
            partner_id = rec.get('ambiguity_partner_fid')
            if partner_id is None:
                continue
            try:
                partner_id = int(partner_id)
            except Exception:
                continue
            if partner_id not in records:
                continue
            if int(rec.get('same_keycol_flag') or 0) != 1:
                continue
            zdiff = self._f(rec.get('ambiguity_partner_z_diff_m'))
            if zdiff is None or zdiff > pair_zdiff:
                continue
            adjacency[fid].add(partner_id)
            adjacency[partner_id].add(fid)

        # connected components on adjacency
        components = []
        seen = set()
        for fid in records:
            if fid in seen:
                continue
            if not adjacency.get(fid):
                continue
            stack = [fid]
            comp = []
            seen.add(fid)
            while stack:
                cur = stack.pop()
                comp.append(cur)
                for nb in adjacency.get(cur, []):
                    if nb not in seen:
                        seen.add(nb)
                        stack.append(nb)
            if len(comp) >= 2:
                components.append(sorted(comp))

        pair_members = set()
        group_counter = 1
        assignments = {}

        def next_group_id():
            nonlocal group_counter
            gid = 'GRP_{:05d}'.format(group_counter)
            group_counter += 1
            return gid

        # first assign pair groups
        for comp in components:
            members = [records[fid] for fid in comp]
            pair_members.update(comp)
            gid = next_group_id()

            # determine if there is a clear winner
            scores = [(m['_score'], m['_fid']) for m in members]
            scores.sort(reverse=True)
            top_score, top_fid = scores[0]
            second_score = scores[1][0] if len(scores) > 1 else -999
            top = records[top_fid]

            clear_official = (
                top.get('_official_dist') is not None and top.get('_official_dist') <= near_thr
            ) or top.get('_has_bev_height') == 1 or top.get('_has_bev_name') == 1

            clear_winner = (top_score - second_score >= 1.25) and clear_official

            if clear_winner:
                for m in members:
                    if m['_fid'] == top_fid:
                        assignments[m['_fid']] = dict(
                            final_group_id=gid,
                            final_group_type='SHARED_KEYCOL_PAIR',
                            final_candidate_rank=1,
                            final_sota_flag=1,
                            final_sota_role='FINAL_SOTA_SUMMIT',
                            final_assignment_basis='OFFICIAL_PREFERRED_SHARED_KEYCOL' if top.get('_official_dist') is not None else 'BEV_HEIGHT_HIGHER_SHARED_KEYCOL',
                            final_assignment_method='RULE_SHARED_KEYCOL_PAIR',
                            final_assignment_confidence='MEDIUM',
                            final_manual_review_required=0,
                            final_replay_required=0,
                            final_anchor_source=top.get('_anchor_source') or 'BEV_COORD',
                            final_anchor_name=top.get('_official_name'),
                            final_anchor_dist_m=top.get('_official_dist'),
                            final_partner_ref=self._safe_str(m.get('ambiguity_partner_ref')),
                            final_partner_status=self._safe_str(m.get('ambiguity_partner_status')),
                            final_partner_name=self._safe_str(m.get('ambiguity_partner_name')),
                            final_partner_dist_m=self._f(m.get('ambiguity_partner_dist_m')),
                            final_partner_z_diff_m=self._f(m.get('ambiguity_partner_z_diff_m')),
                            final_partner_keycol_dist_m=self._f(m.get('ambiguity_partner_keycol_dist_m')),
                            final_decision_locked=0,
                            final_review_note=None,
                            final_review_user=None,
                            final_review_date=None,
                        )
                    else:
                        assignments[m['_fid']] = dict(
                            final_group_id=gid,
                            final_group_type='SHARED_KEYCOL_PAIR',
                            final_candidate_rank=2,
                            final_sota_flag=0,
                            final_sota_role='SECONDARY_SHARED_KEYCOL',
                            final_assignment_basis='PAIR_SECONDARY_TO_FINAL',
                            final_assignment_method='RULE_SHARED_KEYCOL_PAIR',
                            final_assignment_confidence='MEDIUM',
                            final_manual_review_required=0,
                            final_replay_required=0,
                            final_anchor_source=m.get('_anchor_source') or 'BEV_COORD',
                            final_anchor_name=m.get('_official_name'),
                            final_anchor_dist_m=m.get('_official_dist'),
                            final_partner_ref=self._safe_str(m.get('ambiguity_partner_ref')),
                            final_partner_status=self._safe_str(m.get('ambiguity_partner_status')),
                            final_partner_name=self._safe_str(m.get('ambiguity_partner_name')),
                            final_partner_dist_m=self._f(m.get('ambiguity_partner_dist_m')),
                            final_partner_z_diff_m=self._f(m.get('ambiguity_partner_z_diff_m')),
                            final_partner_keycol_dist_m=self._f(m.get('ambiguity_partner_keycol_dist_m')),
                            final_decision_locked=0,
                            final_review_note=None,
                            final_review_user=None,
                            final_review_date=None,
                        )
            else:
                # keep whole group unresolved / review
                rank = 1
                for m in sorted(members, key=lambda r: r['_score'], reverse=True):
                    assignments[m['_fid']] = dict(
                        final_group_id=gid,
                        final_group_type='SHARED_KEYCOL_PAIR',
                        final_candidate_rank=rank,
                        final_sota_flag=0,
                        final_sota_role='REVIEW_REQUIRED',
                        final_assignment_basis='SHARED_KEYCOL_REVIEW_REQUIRED',
                        final_assignment_method='RULE_SHARED_KEYCOL_PAIR',
                        final_assignment_confidence='LOW',
                        final_manual_review_required=1,
                        final_replay_required=1,
                        final_anchor_source=m.get('_anchor_source') or 'BEV_COORD',
                        final_anchor_name=m.get('_official_name'),
                        final_anchor_dist_m=m.get('_official_dist'),
                        final_partner_ref=self._safe_str(m.get('ambiguity_partner_ref')),
                        final_partner_status=self._safe_str(m.get('ambiguity_partner_status')),
                        final_partner_name=self._safe_str(m.get('ambiguity_partner_name')),
                        final_partner_dist_m=self._f(m.get('ambiguity_partner_dist_m')),
                        final_partner_z_diff_m=self._f(m.get('ambiguity_partner_z_diff_m')),
                        final_partner_keycol_dist_m=self._f(m.get('ambiguity_partner_keycol_dist_m')),
                        final_decision_locked=0,
                        final_review_note=None,
                        final_review_user=None,
                        final_review_date=None,
                    )
                    rank += 1

        # now singletons and other rows
        for fid, rec in records.items():
            if fid in assignments:
                continue
            gid = next_group_id()
            role, basis, method, conf, fflag, man, replay, anchor_src = self._singleton_decision(rec, exact_thr, near_thr, replay_thr)
            assignments[fid] = dict(
                final_group_id=gid,
                final_group_type='MATCH_ELEV_SINGLE' if rec['_status'] == 'MATCH_ELEV' else ('SINGLETON' if rec['_status'] in ('MATCH_OK', 'NEW_CALC') else ('UNRESOLVED' if rec['_status'] == 'DB_NO_PEAK' else rec['_status'])),
                final_candidate_rank=1,
                final_sota_flag=fflag,
                final_sota_role=role,
                final_assignment_basis=basis,
                final_assignment_method=method,
                final_assignment_confidence=conf,
                final_manual_review_required=man,
                final_replay_required=replay,
                final_anchor_source=anchor_src,
                final_anchor_name=rec.get('_official_name'),
                final_anchor_dist_m=rec.get('_official_dist'),
                final_partner_ref=self._safe_str(rec.get('ambiguity_partner_ref')),
                final_partner_status=self._safe_str(rec.get('ambiguity_partner_status')),
                final_partner_name=self._safe_str(rec.get('ambiguity_partner_name')),
                final_partner_dist_m=self._f(rec.get('ambiguity_partner_dist_m')),
                final_partner_z_diff_m=self._f(rec.get('ambiguity_partner_z_diff_m')),
                final_partner_keycol_dist_m=self._f(rec.get('ambiguity_partner_keycol_dist_m')),
                final_decision_locked=0,
                final_review_note=None,
                final_review_user=None,
                final_review_date=None,
            )

        idx = out_fields.indexFromName
        final_summits = 0
        review_n = 0
        replay_n = 0

        for fid, rec in records.items():
            feat = rec['_feat']
            ass = self._coordinate_gate(rec, assignments[fid].copy())

            # REMARK(v5.1-authbev): derive reviewer queue fields from the final
            # assignment and authoritative BEV-name evidence. These are part of
            # the reproducible Step-5d output, not an external Excel-only patch.
            queue_replay = int(ass.get('final_replay_required') or 0)
            queue_name = 1 if self._safe_str(rec.get('name_source_review_queue')).startswith('N1_NO_AUTHORITATIVE_BEV_NAME') else 0
            queue_manual = int(ass.get('final_manual_review_required') or 0)
            if queue_replay:
                review_queue_class = 'REPLAY'
            elif queue_name:
                review_queue_class = 'NAME_ENRICHMENT'
            elif queue_manual:
                review_queue_class = 'MANUAL_REVIEW'
            elif int(ass.get('final_sota_flag') or 0) == 1:
                review_queue_class = 'FINAL_CONTEXT'
            else:
                review_queue_class = 'CONTEXT'

            # REMARK(v5.1-authbev-osm-scope-final): OSM fallback is an
            # operative reviewer action only for active NAME_ENRICHMENT rows.
            # Replay rows may also lack authoritative BEV name evidence, but
            # replay/local recalculation has priority and OSM must not be
            # exposed as an immediate name decision there.
            osm_name_review_allowed_final = 1 if (
                review_queue_class == 'NAME_ENRICHMENT'
                and int(rec.get('no_authoritative_bev_name') or 0) == 1
                and (self._safe_str(rec.get('row_origin')) or '').upper() == 'CALC_PEAK'
            ) else 0

            ass.update({
                'bev_calc_name_available': rec.get('bev_calc_name_available'),
                'bev_official_anchor_available': rec.get('bev_official_anchor_available'),
                'bev_authoritative_name_available': rec.get('bev_authoritative_name_available'),
                'bev_authoritative_name': rec.get('bev_authoritative_name'),
                'bev_authoritative_name_source': rec.get('bev_authoritative_name_source'),
                'bev_support_interpreted': rec.get('bev_support_interpreted'),
                'no_authoritative_bev_name': rec.get('no_authoritative_bev_name'),
                'sota_db_name_available': rec.get('sota_db_name_available'),
                'assigned_name_is_sota_fallback': rec.get('assigned_name_is_sota_fallback'),
                'osm_name_review_allowed': osm_name_review_allowed_final,
                'name_source_review_queue': rec.get('name_source_review_queue'),
                'queue_manual_review': queue_manual,
                'queue_replay': queue_replay,
                'queue_name_enrichment': queue_name,
                'review_queue_class': review_queue_class,
            })

            attrs = list(feat.attributes()) + [None] * (out_fields.count() - len(feat.attributes()))
            for key, val in ass.items():
                if idx(key) >= 0:
                    attrs[idx(key)] = val
            out_feat = QgsFeature(out_fields)
            out_feat.setGeometry(rec['_geom'])
            out_feat.setAttributes(attrs)
            sink.addFeature(out_feat, QgsFeatureSink.FastInsert)
            if int(ass.get('final_sota_flag') or 0) == 1:
                final_summits += 1
            if int(ass.get('final_manual_review_required') or 0) == 1:
                review_n += 1
            if int(ass.get('final_replay_required') or 0) == 1:
                replay_n += 1

        feedback.pushInfo('=== Final Assignment v2.3 (Step 5d; coordinate-aware; auth-BEV OSM scope final) ===')
        feedback.pushInfo(f'  Input features: {len(records)}')
        feedback.pushInfo(f'  Final summit rows: {final_summits}')
        feedback.pushInfo(f'  Manual review rows: {review_n}')
        feedback.pushInfo(f'  Replay-required rows: {replay_n}')
        feedback.pushInfo('=============================')

        return {self.P_OUTPUT: sink_id}
