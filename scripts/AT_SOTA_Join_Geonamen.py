# -*- coding: utf-8 -*-
# Filename: AT_SOTA_Join_Geonamen.py
# Version:  5.1-reviewfreeze-authbev
#
# CHANGES vs. 2.0:
#
# 1. Geonamen-Filter auf F_CODE 7302 und 7303 (laut BEV-Spezifikation):
#    7302 = Berggipfel, 7303 = Hochpunkt
#    (7301 entfernt — betrifft Bergkuppen/Hügel, nicht SOTA-relevante Gipfel)
#
# 2. Geonamen-Layer: explizite Unterstützung für den Layer-Namen
#    "DLM_7000_Namen_20250325 — NAM_7300_GELAENDEFORM_P_20250325"
#    Der Layer-Name wird als Default im Parameter vorbelegt.
#
# 3. Neuer Parameter P_BL_PATH: Bundesländer-GeoPackage (BL_202504.gpkg).
#    Spatial Join (point-in-polygon) übernimmt 'land_id' und 'land'.
#    Ist optional — wird der Pfad weggelassen, bleiben die Felder NULL.
#
# 4. Ausgabe-Felder:
#    NAME, name_dist_m, name_match  (wie bisher)
#    land_id, land                  (neu: aus Bundesländer-Layer)

from qgis.PyQt.QtCore import QVariant
from qgis.core import (
    QgsProcessing, QgsProcessingAlgorithm,
    QgsProcessingParameterVectorLayer, QgsProcessingParameterFile,
    QgsProcessingParameterString, QgsProcessingParameterNumber,
    QgsProcessingParameterFeatureSink, QgsProcessingParameterDefinition,
    QgsProcessingException,
    QgsFeature, QgsField, QgsFields,
    QgsVectorLayer, QgsFeatureSink, QgsSpatialIndex, QgsFeatureRequest
)
import processing
import math


class AT_SOTA_Join_Geonamen(QgsProcessingAlgorithm):

    P_PEAKS      = 'peaks_layer'
    P_GPKG_PATH  = 'geonamen_gpkg_path'
    P_GPKG_LAYER = 'geonamen_layer_name'
    P_RADIUS     = 'search_radius'
    P_BL_PATH      = 'bl_gpkg_path'
    P_BORDER_PATH  = 'border_gpkg_path'
    P_OUTPUT     = 'output'

    # Default-Layer-Name laut BEV DLM 2025
    DEFAULT_LAYER = 'DLM_7000_Namen_20250325 — NAM_7300_GELAENDEFORM_P_20250325'

    def createInstance(self): return self.__class__()
    def name(self):        return 'AT_SOTA_Join_Geonamen'
    def displayName(self): return 'AT SOTA — Join BEV Geonamen + Bundesländer + Grenzkontext (v4.1)'
    def group(self):       return 'SOTA'
    def groupId(self):     return 'SOTA'
    def shortHelpString(self):
        return (
            "Joins BEV DLM Geonamen (F_CODE 7302/7303) and Bundesland to SOTA peaks.\n\n"
            "F_CODE:\n"
            "  7302 = Berggipfel\n"
            "  7303 = Hochpunkt\n\n"
            "Output fields:\n"
            "  NAME        — legacy BEV name at calculated peak point (NULL if no calc-point match)\n"
            "  bev_calc_name / bev_calc_fcode / bev_calc_dist_m — explicit calc-point BEV match\n"
            "  no_bev_at_calc_point — 1 if no BEV 7302/7303 was found at calculated point\n"
            "  name_dist_m — Distanz zum Geonamen-Punkt (m)\n"
            "  name_match  — 0=kein Match, 1=Match\n"
            "  land_id     — Bundesland-ID (aus BL_202504.gpkg)\n"
            "  land        — Bundesland-Name"
        )

    def initAlgorithm(self, config=None):
        self.addParameter(QgsProcessingParameterVectorLayer(
            self.P_PEAKS, 'Peaks (EPSG:25833 empfohlen)',
            [QgsProcessing.TypeVectorPoint]))

        self.addParameter(QgsProcessingParameterFile(
            self.P_GPKG_PATH, 'BEV DLM Geonamen GeoPackage (*.gpkg)',
            behavior=QgsProcessingParameterFile.File,
            fileFilter='GeoPackage (*.gpkg)'))

        lyr = QgsProcessingParameterString(
            self.P_GPKG_LAYER,
            'Layer-Name im Geonamen-GeoPackage',
            defaultValue=self.DEFAULT_LAYER,
            multiLine=False, optional=True)
        lyr.setFlags(lyr.flags() | QgsProcessingParameterDefinition.FlagAdvanced)
        self.addParameter(lyr)

        self.addParameter(QgsProcessingParameterNumber(
            self.P_RADIUS, 'Suchradius (m) [20–100]',
            type=QgsProcessingParameterNumber.Double,
            defaultValue=30.0, minValue=20.0, maxValue=100.0))

        bl = QgsProcessingParameterFile(
            self.P_BL_PATH,
            'Bundesländer GeoPackage (*.gpkg) — optional',
            behavior=QgsProcessingParameterFile.File,
            fileFilter='GeoPackage (*.gpkg)',
            optional=True)
        self.addParameter(bl)

        border = QgsProcessingParameterFile(
            self.P_BORDER_PATH,
            'Staatsgrenz GeoPackage für border_dist_m (*.gpkg) — optional',
            behavior=QgsProcessingParameterFile.File,
            fileFilter='GeoPackage (*.gpkg)',
            optional=True)
        self.addParameter(border)

        sink_param = QgsProcessingParameterFeatureSink(
            self.P_OUTPUT, 'Output: Peaks mit Namen + Bundesland',
            type=QgsProcessing.TypeVectorPoint)
        sink_param.setFlags(
            sink_param.flags() | QgsProcessingParameterDefinition.FlagAdvanced)
        self.addParameter(sink_param)

    # ------------------------------------------------------------------
    @staticmethod
    def _open_layer(gpkg_path: str, layer_name: str, label: str, feedback):
        """Open a layer from a GeoPackage. layer_name='' tries first layer."""
        if layer_name:
            uri = f'{gpkg_path}|layername={layer_name}'
            vl  = QgsVectorLayer(uri, label, 'ogr')
            if vl and vl.isValid():
                feedback.pushInfo(f"  Opened '{layer_name}' from {gpkg_path}")
                return vl
            # Try alternative URI format
            uri2 = f'geopackage:?path={gpkg_path}&layer={layer_name}'
            vl2  = QgsVectorLayer(uri2, label, 'ogr')
            if vl2 and vl2.isValid():
                feedback.pushInfo(f"  Opened '{layer_name}' (URI2) from {gpkg_path}")
                return vl2
            raise QgsProcessingException(
                f"Cannot open layer '{layer_name}' in {gpkg_path}.")

        vl = QgsVectorLayer(gpkg_path, label, 'ogr')
        if not vl or not vl.isValid():
            raise QgsProcessingException(f"Cannot open GeoPackage: {gpkg_path}")
        feedback.pushInfo(f"  Using first layer '{vl.name()}' from {gpkg_path}")
        return vl

    @staticmethod
    def _open_geonamen(gpkg_path: str, layer_name: str, feedback):
        """Open and validate the Geonamen layer."""
        # Try the explicitly given (or default) layer name first
        candidates = [layer_name] if layer_name else []
        candidates += [
            'DLM_7000_Namen_20250325 — NAM_7300_GELAENDEFORM_P_20250325',
            'NAM_7300_GELAENDEFORM_P_20250325',
            'DLM_7000_NAMEN_20250325',
            'NAMEN', 'Geonamen', 'geonamen',
        ]

        for name in candidates:
            if not name:
                continue
            for uri in [f'{gpkg_path}|layername={name}',
                        f'geopackage:?path={gpkg_path}&layer={name}']:
                vl = QgsVectorLayer(uri, 'geonamen', 'ogr')
                if not (vl and vl.isValid()):
                    continue
                fn = [f.name() for f in vl.fields()]
                if 'F_CODE' in fn and ('NAME' in fn or 'Name' in fn):
                    feedback.pushInfo(f"  Geonamen-Layer: '{name}'")
                    feedback.pushInfo(f"  Felder: {fn}")
                    return vl

        # Fallback: first layer
        vl = QgsVectorLayer(gpkg_path, 'geonamen', 'ogr')
        if not vl or not vl.isValid():
            raise QgsProcessingException(f"Cannot open GeoPackage: {gpkg_path}")
        fn = [f.name() for f in vl.fields()]
        if 'F_CODE' not in fn:
            raise QgsProcessingException(f"No F_CODE in Geonamen layer. Fields: {fn}")
        if 'NAME' not in fn and 'Name' not in fn:
            raise QgsProcessingException(f"No NAME in Geonamen layer. Fields: {fn}")
        feedback.pushInfo(f"  Geonamen fallback layer '{vl.name()}'")
        return vl

    @staticmethod
    def _lookup_land_from_bl(geom, bl_lyr, sindex_bl):
        if geom is None or geom.isEmpty() or bl_lyr is None or sindex_bl is None:
            return None, None, None
        for cid in sindex_bl.intersects(geom.boundingBox()):
            bf = next(bl_lyr.getFeatures(QgsFeatureRequest(cid)), None)
            if bf is None or bf.geometry() is None or bf.geometry().isEmpty():
                continue
            bg = bf.geometry()
            try:
                if bg.contains(geom) or bg.intersects(geom) or bg.distance(geom) <= 0.5:
                    return bf['land_id'], bf['land'], 'bl_join'
            except Exception:
                continue
        pt = geom.asPoint()
        best = None
        best_d = float('inf')
        for cid in sindex_bl.nearestNeighbor(pt, 3):
            bf = next(bl_lyr.getFeatures(QgsFeatureRequest(cid)), None)
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

    @staticmethod
    def _lookup_bl_context(geom, own_land_id, bl_lyr):
        if geom is None or geom.isEmpty() or bl_lyr is None or own_land_id in (None, ''):
            return None, None, None, None
        min_d = float('inf')
        best_land_id = None
        best_land = None
        for bf in bl_lyr.getFeatures():
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

    # ------------------------------------------------------------------
    def processAlgorithm(self, parameters, context, feedback):
        peaks      = self.parameterAsVectorLayer(parameters, self.P_PEAKS,      context)
        gpkg_path  = self.parameterAsFile(parameters,        self.P_GPKG_PATH,  context)
        layer_name = (self.parameterAsString(parameters,     self.P_GPKG_LAYER, context)
                      or self.DEFAULT_LAYER)
        radius_m   = float(self.parameterAsDouble(parameters, self.P_RADIUS,    context))
        bl_path      = self.parameterAsFile(parameters, self.P_BL_PATH,     context) or ''
        border_path  = self.parameterAsFile(parameters, self.P_BORDER_PATH, context) or ''

        if not peaks or not peaks.isValid():
            raise QgsProcessingException("Invalid peaks layer.")
        if not gpkg_path:
            raise QgsProcessingException("Geonamen GeoPackage path is required.")
        if not (20.0 <= radius_m <= 100.0):
            raise QgsProcessingException(
                f"Search radius {radius_m:.1f} m outside 20–100 m.")

        # ----------------------------------------------------------------
        # 1 — Geonamen layer laden & reprojektion
        # ----------------------------------------------------------------
        feedback.pushInfo("Öffne Geonamen-Layer...")
        geonamen_src = self._open_geonamen(gpkg_path, layer_name, feedback)

        if geonamen_src.crs() != peaks.crs():
            feedback.pushInfo(
                f"  Reprojektion {geonamen_src.crs().authid()} → {peaks.crs().authid()}")
            geonamen = processing.run("native:reprojectlayer", {
                'INPUT':      geonamen_src,
                'TARGET_CRS': peaks.crs().authid(),
                'OPERATION':  '',
                'OUTPUT':     'memory:geonamen_reproj',
            }, context=context, feedback=feedback)['OUTPUT']
        else:
            geonamen = geonamen_src

        fn = [f.name() for f in geonamen.fields()]
        name_field = 'NAME' if 'NAME' in fn else 'Name'

        # ----------------------------------------------------------------
        # 2 — Filter: nur F_CODE 7302 (Berggipfel) und 7303 (Hochpunkt)
        # ----------------------------------------------------------------
        feedback.pushInfo("Filtere Geonamen auf F_CODE IN (7302, 7303)...")
        geonamen_filt = processing.run("native:extractbyexpression", {
            'INPUT':      geonamen,
            'EXPRESSION': '"F_CODE" IN (7302, 7303)',
            'OUTPUT':     'memory:geonamen_filt',
        }, context=context, feedback=feedback)['OUTPUT']
        feedback.pushInfo(
            f"  {geonamen_filt.featureCount()} Geonamen-Features verfügbar.")

        sindex_geo = QgsSpatialIndex(geonamen_filt.getFeatures())

        # ----------------------------------------------------------------
        # 3 — Bundesländer laden (optional)
        # ----------------------------------------------------------------
        bl_lyr    = None
        sindex_bl = None
        if bl_path:
            feedback.pushInfo("Öffne Bundesländer-Layer...")
            bl_src = self._open_layer(bl_path, '', 'bundeslaender', feedback)

            # Validierung der Pflichtfelder
            bl_fn = [f.name() for f in bl_src.fields()]
            feedback.pushInfo(f"  BL-Felder: {bl_fn}")
            for req in ('land_id', 'land'):
                if req not in bl_fn:
                    raise QgsProcessingException(
                        f"Pflichtfeld '{req}' fehlt im Bundesländer-Layer. "
                        f"Vorhandene Felder: {bl_fn}")

            if bl_src.crs() != peaks.crs():
                feedback.pushInfo(
                    f"  Reprojektion BL {bl_src.crs().authid()} → {peaks.crs().authid()}")
                bl_lyr = processing.run("native:reprojectlayer", {
                    'INPUT':      bl_src,
                    'TARGET_CRS': peaks.crs().authid(),
                    'OPERATION':  '',
                    'OUTPUT':     'memory:bl_reproj',
                }, context=context, feedback=feedback)['OUTPUT']
            else:
                bl_lyr = bl_src

            sindex_bl = QgsSpatialIndex(bl_lyr.getFeatures())
            feedback.pushInfo(
                f"  {bl_lyr.featureCount()} Bundesland-Polygone geladen.")

        # ----------------------------------------------------------------
        # 4 — Output-Schema aufbauen
        # ----------------------------------------------------------------
        out_fields = QgsFields()
        for fld in peaks.fields():
            out_fields.append(fld)

        def _add_field(name, typ, type_name='', length=0, prec=0):
            if out_fields.indexFromName(name) < 0:
                out_fields.append(QgsField(name, typ, type_name, length, prec, ''))

        # Legacy Step-4 fields: these describe only the BEV name found at the
        # calculated DEM peak point. They must not be interpreted as complete
        # official BEV name evidence for the summit case.
        _add_field('NAME',            QVariant.String, 'text',   100)
        _add_field('name_dist_m',     QVariant.Double, 'double',  10, 2)
        _add_field('name_match',      QVariant.Int,    'integer',  1)

        # REMARK(v5.1-authbev): explicit calc-point BEV semantics.
        _add_field('bev_calc_name',            QVariant.String, 'text',   120)
        _add_field('bev_calc_fcode',           QVariant.Int,    'integer',  4)
        _add_field('bev_calc_dist_m',          QVariant.Double, 'double',  10, 2)
        _add_field('bev_calc_name_available',  QVariant.Int,    'integer',  1)
        _add_field('no_bev_at_calc_point',     QVariant.Int,    'integer',  1)
        _add_field('bev_support_calc',         QVariant.String, 'text',    40)
        _add_field('land_id',         QVariant.Int,    'integer',  4)
        _add_field('land',            QVariant.String, 'text',    50)
        _add_field('BL_quelle',       QVariant.String, 'text',    12)
        _add_field('border_dist_m',   QVariant.Double, 'double',  10, 1)
        _add_field('border_zone',     QVariant.String, 'text',    20)
        _add_field('bl_border_dist_m', QVariant.Double, 'double', 10, 1)
        _add_field('bl_border_zone',  QVariant.String, 'text',    20)
        _add_field('neighbor_land_id', QVariant.Int,   'integer', 4)
        _add_field('neighbor_land',   QVariant.String, 'text',    50)
        _add_field('admin_context',   QVariant.String, 'text',    24)
        _add_field('admin_review',    QVariant.Int,    'integer', 1)
        _add_field('z_bev',           QVariant.Double, 'double',  10, 1)
        _add_field('z_bev_diff',      QVariant.Double, 'double',  10, 1)

        sink, sink_id = self.parameterAsSink(
            parameters, self.P_OUTPUT, context,
            out_fields, peaks.wkbType(), peaks.crs())
        if sink is None:
            raise QgsProcessingException("Could not create output sink.")

        # ----------------------------------------------------------------
        # 4b — Staatsgrenze für border_dist_m laden
        # ----------------------------------------------------------------
        self._border_idx = None
        border_lyr = None
        if border_path:
            feedback.pushInfo("Lade Staatsgrenz-Layer für border_dist_m...")
            b_src = self._open_layer(border_path, '', 'staatsgrenze', feedback)
            if b_src.crs() != peaks.crs():
                b_src = processing.run("native:reprojectlayer", {
                    'INPUT': b_src, 'TARGET_CRS': peaks.crs().authid(),
                    'OPERATION': '', 'OUTPUT': 'memory:border_reproj',
                }, context=context, feedback=feedback)['OUTPUT']
            # Polygon → Linien für Abstandsberechnung zur Grenzlinie
            border_lyr = processing.run("native:polygonstolines", {
                'INPUT': b_src, 'OUTPUT': 'memory:border_lines',
            }, context=context, feedback=feedback)['OUTPUT']
            from qgis.core import QgsSpatialIndex as _QSI_BORDER
            self._border_idx = _QSI_BORDER(border_lyr.getFeatures())
            feedback.pushInfo(
                f"  border_dist_m aktiv: {border_lyr.featureCount()} Grenz-Segmente")

        # ----------------------------------------------------------------
        # 5 — Hauptschleife: Geonamen + Bundesland je Peak
        # ----------------------------------------------------------------
        total        = peaks.featureCount() or 0
        matched_name = 0
        matched_bl   = 0

        for pf in peaks.getFeatures():
            if feedback.isCanceled():
                break

            out_f = QgsFeature(out_fields)
            out_f.setGeometry(pf.geometry())
            attrs = list(pf.attributes()) + [None] * (out_fields.count() - len(pf.attributes()))
            pt = pf.geometry().asPoint()

            # --- Geonamen: nächster Nachbar im Suchradius ---
            chosen_name = None
            chosen_dist = None
            chosen_fcode = None
            name_flag   = 0
            best_d      = radius_m + 1.0

            chosen_hoehe = None  # BEV HOEHE_BODEN
            for cid in sindex_geo.nearestNeighbor(pt, 5):
                cf = next(geonamen_filt.getFeatures(QgsFeatureRequest(cid)), None)
                if cf is None:
                    continue
                d = pf.geometry().distance(cf.geometry())
                if math.isfinite(d) and d <= radius_m and d < best_d:
                    nm = cf[name_field]
                    best_d      = d
                    chosen_name = str(nm) if nm is not None else None
                    chosen_dist = float(d)
                    chosen_fcode = int(cf['F_CODE']) if cf.fieldNameIndex('F_CODE') >= 0 and cf['F_CODE'] is not None else None
                    name_flag   = 1
                    # BEV-Höhe aus HOEHE_BODEN
                    hb_idx = cf.fieldNameIndex('HOEHE_BODEN')
                    if hb_idx >= 0:
                        hb = cf['HOEHE_BODEN']
                        try:
                            chosen_hoehe = float(hb) if hb is not None else None
                        except (TypeError, ValueError):
                            chosen_hoehe = None

            attrs[out_fields.indexFromName('NAME')]        = chosen_name
            attrs[out_fields.indexFromName('name_dist_m')] = chosen_dist
            attrs[out_fields.indexFromName('name_match')]  = name_flag

            # REMARK(v5.1-authbev): NAME/name_match are calc-point fields only.
            # A missing NAME means no BEV 7302/7303 at the calculated DEM point;
            # it does not mean that there is no authoritative BEV name at the
            # official/SOTA anchor. Step 5c consolidates both evidences.
            attrs[out_fields.indexFromName('bev_calc_name')] = chosen_name
            attrs[out_fields.indexFromName('bev_calc_fcode')] = chosen_fcode
            attrs[out_fields.indexFromName('bev_calc_dist_m')] = chosen_dist
            attrs[out_fields.indexFromName('bev_calc_name_available')] = 1 if (chosen_name and chosen_fcode in (7302, 7303)) else 0
            attrs[out_fields.indexFromName('no_bev_at_calc_point')] = 0 if (chosen_name and chosen_fcode in (7302, 7303)) else 1
            if chosen_name and chosen_fcode in (7302, 7303):
                attrs[out_fields.indexFromName('bev_support_calc')] = f'BEV_CALC_{chosen_fcode}'
            else:
                attrs[out_fields.indexFromName('bev_support_calc')] = 'NO_BEV_AT_CALC_POINT'

            if name_flag:
                matched_name += 1

            # --- Bundesland: Punkt-in-Polygon (amtliche Polygone) ---
            land_id_val = None
            land_val    = None
            bl_quelle   = None
            if sindex_bl is not None:
                land_id_val, land_val, bl_quelle = self._lookup_land_from_bl(pf.geometry(), bl_lyr, sindex_bl)
                if land_val not in (None, ''):
                    matched_bl += 1

            attrs[out_fields.indexFromName('land_id')]   = land_id_val
            attrs[out_fields.indexFromName('land')]      = land_val
            attrs[out_fields.indexFromName('BL_quelle')] = bl_quelle

            # --- Staatsgrenze: Abstand zur Grenzlinie ---
            border_dist = None
            border_zone = None
            if border_lyr is not None and self._border_idx is not None:
                from qgis.core import QgsFeatureRequest as _QFR
                min_d = float('inf')
                for cid in self._border_idx.nearestNeighbor(pt, 3):
                    bf = next(border_lyr.getFeatures(_QFR(cid)), None)
                    if bf is None:
                        continue
                    d = pf.geometry().distance(bf.geometry())
                    if d < min_d:
                        min_d = d
                if min_d < float('inf'):
                    # Alle berechneten Peaks liegen innerhalb des AT-Borders
                    # (geclippt in SeamlessHydrology Step 8) → Abstand ist positiv
                    border_dist = round(min_d, 1)
                    if border_dist > 50:
                        border_zone = 'INNER'
                    elif border_dist >= 0:
                        border_zone = 'BORDER_ZONE'
                    else:
                        border_zone = 'OUTER'

            attrs[out_fields.indexFromName('border_dist_m')] = border_dist
            attrs[out_fields.indexFromName('border_zone')]   = border_zone

            # --- Bundesländer-Grenzkontext ---
            bl_border_dist = None
            bl_border_zone = None
            neighbor_land_id = None
            neighbor_land = None
            admin_context = None
            admin_review = 0
            if bl_lyr is not None and land_id_val not in (None, ''):
                bl_border_dist, bl_border_zone, neighbor_land_id, neighbor_land =                     self._lookup_bl_context(pf.geometry(), land_id_val, bl_lyr)
            admin_context = self._admin_context(border_zone, bl_border_zone)
            admin_review = self._admin_review(admin_context)
            attrs[out_fields.indexFromName('bl_border_dist_m')] = bl_border_dist
            attrs[out_fields.indexFromName('bl_border_zone')]   = bl_border_zone
            attrs[out_fields.indexFromName('neighbor_land_id')] = neighbor_land_id
            attrs[out_fields.indexFromName('neighbor_land')]    = neighbor_land
            attrs[out_fields.indexFromName('admin_context')]    = admin_context
            attrs[out_fields.indexFromName('admin_review')]     = admin_review

            # --- BEV-Referenzhöhe und Differenz zur ALS-Höhe ---
            z_bev_val  = round(chosen_hoehe, 1) if chosen_hoehe is not None else None
            z_bev_diff = None
            if z_bev_val is not None:
                # Verfeinerte ALS-Höhe bevorzugen, sonst 10m-Raster
                z_als = None
                z1m_idx = pf.fieldNameIndex('z1m_max')
                zpk_idx = pf.fieldNameIndex('zpk_1')
                if z1m_idx >= 0 and pf['z1m_max'] is not None:
                    try:
                        z_als = float(pf['z1m_max'])
                    except (TypeError, ValueError):
                        pass
                if z_als is None and zpk_idx >= 0 and pf['zpk_1'] is not None:
                    try:
                        z_als = float(pf['zpk_1'])
                    except (TypeError, ValueError):
                        pass
                if z_als is not None:
                    z_bev_diff = round(z_als - z_bev_val, 1)
            attrs[out_fields.indexFromName('z_bev')]      = z_bev_val
            attrs[out_fields.indexFromName('z_bev_diff')] = z_bev_diff

            out_f.setAttributes(attrs)
            sink.addFeature(out_f, QgsFeatureSink.FastInsert)

        pct_n = 100.0 * matched_name / total if total else 0.0
        pct_b = 100.0 * matched_bl   / total if total else 0.0
        feedback.pushInfo(
            f"[Geonamen]    {matched_name}/{total} ({pct_n:.1f}%)  "
            f"radius={radius_m:.1f} m")
        if sindex_bl is not None:
            feedback.pushInfo(
                f"[Bundesland]  {matched_bl}/{total} ({pct_b:.1f}%)")
        else:
            feedback.pushInfo("[Bundesland]  nicht ausgeführt (kein BL-Pfad angegeben)")

        return {self.P_OUTPUT: sink_id}


def classFactory():
    return AT_SOTA_Join_Geonamen()
