# -*- coding: utf-8 -*-
# Filename: AT_SOTA_Refine1m.py
# Version:  3.0  - key-col 1m refinement (zonal min) added
#
# CHANGES vs. 2.1:
# NEW  Keycol 1m-Verfeinerung
#   If keycol_x/keycol_y are present (from PixelMinimax): zonal min(1m, 25m radius)
#   around the saddle pixel. Formula: prom_ref = z1m_max(summit) - z1m_min(saddle)
#   If keycol_x/y are missing: fall back to the old behaviour (key col from 10m).
#
# CHANGES vs. 2.0:
#
# FIX-A  field name 'zpk_1' secured in the formula
#   native:rastersampling with COLUMN_PREFIX='zpk_' produces 'zpk_1' (with
#   band number) or 'zpk_' (without). The pre-flight check now determines
#   the actual field name dynamically and uses it in the formula.
#
# FIX-B  empty check for the ambiguous band
#   In the Vienna test area (almost no mountains) ambig_peaks can be empty.
#   In that case the buffer/zonal/join steps are skipped
#   and only safe_peaks is returned as the result.
#
# FIX-C  Zonal Statistics Feldname abgesichert
#   native:zonalstatisticsfb with COLUMN_PREFIX='z1m_' and STATISTICS=[6]
#   produces 'z1m_max' in QGIS 3.3x, but older versions may
#   produce 'z1m_6'. The join field name is determined dynamically.

from qgis.PyQt.QtCore import QVariant
from qgis.core import (
    QgsProcessing, QgsProcessingAlgorithm,
    QgsProcessingParameterRasterLayer, QgsProcessingParameterVectorLayer,
    QgsProcessingParameterFeatureSink, QgsProcessingException
)
import processing


class ATSOTARefine1m(QgsProcessingAlgorithm):
    INPUT_PEAKS = 'INPUT_PEAKS'
    DEM_1M      = 'DEM_1M'
    OUTPUT_SOTA = 'OUTPUT_SOTA'

    def createInstance(self): return ATSOTARefine1m()
    def name(self):        return 'AT_SOTA_Refine1m'
    def displayName(self): return 'AT SOTA - 1 m Refinement (Gipfel + Sattel) v3.0'
    def group(self):       return 'SOTA'
    def groupId(self):     return 'SOTA'

    def initAlgorithm(self, config=None):
        self.addParameter(QgsProcessingParameterVectorLayer(
            self.INPUT_PEAKS,
            'Peaks mit 10 m Prominenz (keycol & prom Felder erforderlich)'))
        self.addParameter(QgsProcessingParameterRasterLayer(
            self.DEM_1M, '1 m ALS DEM'))
        self.addParameter(QgsProcessingParameterFeatureSink(
            self.OUTPUT_SOTA, 'Finale SOTA-gültige Peaks (P ≥ 150 m)'))

    # ------------------------------------------------------------------
    @staticmethod
    def _field_by_prefix(layer, prefix):
        """Returns the first field name that begins with 'prefix'."""
        prefix_l = prefix.lower()
        for f in layer.fields():
            if f.name().lower().startswith(prefix_l):
                return f.name()
        return None

    # ------------------------------------------------------------------
    def processAlgorithm(self, parameters, context, feedback):
        # ----------------------------------------------------------------
        # Robust layer loading for GeoPackage inputs passed as bare paths
        # from qgis_process CLI (e.g. /path/to/file.gpkg without |layername=).
        # parameterAsVectorLayer can return None for bare .gpkg paths.
        # Fallback: open with QgsVectorLayer using OGR directly.
        # ----------------------------------------------------------------
        from qgis.core import QgsVectorLayer as _QgsVL

        peaks = self.parameterAsVectorLayer(parameters, self.INPUT_PEAKS, context)
        if peaks is None or not peaks.isValid():
            raw = parameters.get(self.INPUT_PEAKS, '')
            if isinstance(raw, str) and raw.endswith('.gpkg'):
                peaks = _QgsVL(raw, 'peaks', 'ogr')
            if peaks is None or not peaks.isValid():
                raise QgsProcessingException(
                    f"INPUT_PEAKS Layer konnte nicht geladen werden: "
                    f"{parameters.get(self.INPUT_PEAKS)}")
            feedback.pushInfo(f"  INPUT_PEAKS via OGR fallback geladen: {peaks.name()}")

        dem1m = self.parameterAsRasterLayer(parameters, self.DEM_1M, context)

        # ----------------------------------------------------------------
        # pre-flight: check required fields
        # ----------------------------------------------------------------
        field_names = [f.name() for f in peaks.fields()]
        for required in ('prom', 'keycol'):
            if required not in field_names:
                raise QgsProcessingException(
                    f"Pflichtfeld '{required}' fehlt im Peaks-Layer. "
                    f"Vorhandene Felder: {field_names}. "
                    f"Layer muss von keycol_minimax v2.1 stammen.")

        # FIX-A: determine the actual zpk_ field name
        zpk_field = self._field_by_prefix(peaks, 'zpk_')
        if zpk_field is None:
            raise QgsProcessingException(
                f"Kein Feld mit Prefix 'zpk_' gefunden. "
                f"Vorhandene Felder: {field_names}")
        feedback.pushInfo(f"  Gipfelhöhen-Feld: '{zpk_field}'")

        # ----------------------------------------------------------------
        # STEP 1 — Aufteilung: Sicher (≥170 m) vs. Ambiguous (130–170 m)
        # ----------------------------------------------------------------
        feedback.pushInfo("Step 1/5 — Trenne sichere und ambiguous Peaks...")

        safe_peaks = processing.run("native:extractbyexpression", {
            'INPUT':      peaks,
            'EXPRESSION': '"prom" >= 170',
            'OUTPUT':     'TEMPORARY_OUTPUT',
        }, context=context, feedback=feedback)['OUTPUT']
        feedback.pushInfo(f"  Sichere Peaks (prom ≥ 170 m): {safe_peaks.featureCount()}")

        ambig_peaks = processing.run("native:extractbyexpression", {
            'INPUT':      peaks,
            'EXPRESSION': '"prom" >= 130 AND "prom" < 170',
            'OUTPUT':     'TEMPORARY_OUTPUT',
        }, context=context, feedback=feedback)['OUTPUT']
        feedback.pushInfo(f"  Ambiguous Peaks (130–170 m): {ambig_peaks.featureCount()}")

        # ----------------------------------------------------------------
        # FIX-B: empty check - the ambiguous band can be empty in any test area
        #
        # This is not an indication of flat terrain, but simply the
        # normal case when no peak happens to have a 10m prominence between
        # 130 and 170 m. Example Vienna test area: the Hermannskogel
        # (449 m) has a prominence far above 170 m and falls directly into
        # safe_peaks - the ambiguous band is empty, although the area
        # durchaus Topographie hat.
        # Without this check, buffer/zonal/join would run on an empty
        # layer and QGIS would abort with an error.
        # ----------------------------------------------------------------
        if ambig_peaks.featureCount() == 0:
            feedback.pushInfo(
                "  Keine Peaks im ambiguous band (130–170 m) — "
                "überspringe 1 m Refinement.")

            # extend the safe_peaks schema by prom_ref and z1m_max
            safe_with_ref = processing.run("native:fieldcalculator", {
                'INPUT':      safe_peaks,
                'FIELD_NAME': 'prom_ref',
                'FIELD_TYPE': 0,
                'FORMULA':    '"prom"',
                'OUTPUT':     'TEMPORARY_OUTPUT',
            }, context=context, feedback=feedback)['OUTPUT']

            merged = processing.run("native:fieldcalculator", {
                'INPUT':      safe_with_ref,
                'FIELD_NAME': 'z1m_max',
                'FIELD_TYPE': 0,
                'FORMULA':    'NULL',
                'OUTPUT':     parameters[self.OUTPUT_SOTA],
            }, context=context, feedback=feedback)['OUTPUT']

            from qgis.core import QgsVectorLayer as _QgsVL
            merged_lyr = _QgsVL(merged, 'merged', 'ogr') if isinstance(merged, str) else merged
            n_valid = merged_lyr.featureCount() if merged_lyr.isValid() else '?'
            feedback.pushInfo(
                f"Refinement abgeschlossen (kein ambiguous band). "
                f"SOTA-gültige Peaks: {n_valid}")
            return {self.OUTPUT_SOTA: merged}

        # ----------------------------------------------------------------
        # STEP 2 - buffer around ambiguous peaks (25 m radius)
        # ----------------------------------------------------------------
        feedback.pushInfo("Step 2/5 — Puffere ambiguous Peaks (25 m Radius)...")
        buffered = processing.run("native:buffer", {
            'INPUT':    ambig_peaks,
            'DISTANCE': 25,
            'SEGMENTS': 8,
            'OUTPUT':   'TEMPORARY_OUTPUT',
        }, context=context, feedback=feedback)['OUTPUT']

        # ----------------------------------------------------------------
        # STEP 3 - zonal maximum in the 1 m DEM
        # ----------------------------------------------------------------
        feedback.pushInfo("Step 3/5 — Berechne 1 m Zonal-Maximum (25 m Radius)...")
        zonal_max = processing.run("native:zonalstatisticsfb", {
            'INPUT':         buffered,
            'INPUT_RASTER':  dem1m,
            'STATISTICS':    [6],          # 6 = max
            'COLUMN_PREFIX': 'z1m_',
            'OUTPUT':        'TEMPORARY_OUTPUT',
        }, context=context, feedback=feedback)['OUTPUT']

        # FIX-C: determine the actual field name of the zonal max
        z1m_field = self._field_by_prefix(zonal_max, 'z1m_')
        if z1m_field is None:
            raise QgsProcessingException(
                "Kein 'z1m_*' Feld nach zonalstatisticsfb gefunden. "
                f"Felder: {[f.name() for f in zonal_max.fields()]}")
        feedback.pushInfo(f"  Zonal-Max Feld: '{z1m_field}'")

        # ----------------------------------------------------------------
        # STEP 3b - zonal min for saddle (keycol_x / keycol_y)
        # If PixelMinimax has supplied the saddle coordinates,
        # we also refine the key col to 1m precision.
        # ----------------------------------------------------------------
        has_keycol_coords = (
            'keycol_x' in [f.name() for f in ambig_peaks.fields()] and
            'keycol_y' in [f.name() for f in ambig_peaks.fields()]
        )

        z1m_col_field = None
        ambig_for_formula = ambig_peaks  # replaced by the joined layer if applicable

        if has_keycol_coords:
            feedback.pushInfo("Step 3b/5 — Keycol 1m Zonal-Min (25 m Radius)...")

            # create key-col points directly from keycol_x/keycol_y (no algorithm needed)
            from qgis.core import (QgsVectorLayer, QgsField, QgsFields,
                                   QgsFeature, QgsGeometry, QgsPointXY,
                                   QgsWkbTypes, QgsMemoryProviderUtils)
            from qgis.PyQt.QtCore import QVariant as _QV

            kc_fields = QgsFields()
            for fld in ambig_peaks.fields():
                kc_fields.append(fld)

            keycol_pts = QgsMemoryProviderUtils.createMemoryLayer(
                'keycol_pts', kc_fields, QgsWkbTypes.Point, ambig_peaks.crs())
            kc_dp = keycol_pts.dataProvider()

            for apf in ambig_peaks.getFeatures():
                kx = apf['keycol_x']
                ky = apf['keycol_y']
                if kx is None or ky is None:
                    continue
                try:
                    kx, ky = float(kx), float(ky)
                except (TypeError, ValueError):
                    continue
                nf = QgsFeature(kc_fields)
                nf.setGeometry(QgsGeometry.fromPointXY(QgsPointXY(kx, ky)))
                nf.setAttributes(apf.attributes())
                kc_dp.addFeature(nf)

            keycol_pts.updateExtents()
            feedback.pushInfo(f"  Keycol-Punkte erstellt: {keycol_pts.featureCount()}")

            # 25m buffer around saddle points
            kc_buf = processing.run("native:buffer", {
                'INPUT':    keycol_pts,
                'DISTANCE': 25,
                'SEGMENTS': 8,
                'OUTPUT':   'TEMPORARY_OUTPUT',
            }, context=context, feedback=feedback)['OUTPUT']

            # zonal min on 1m DEM
            kc_zonal = processing.run("native:zonalstatisticsfb", {
                'INPUT':         kc_buf,
                'INPUT_RASTER':  dem1m,
                'STATISTICS':    [5],          # 5 = min
                'COLUMN_PREFIX': 'z1m_col_',
                'OUTPUT':        'TEMPORARY_OUTPUT',
            }, context=context, feedback=feedback)['OUTPUT']

            z1m_col_field = self._field_by_prefix(kc_zonal, 'z1m_col_')
            if z1m_col_field:
                feedback.pushInfo(f"  Zonal-Min Sattel Feld: '{z1m_col_field}'")

                # join key-col min back onto ambig_peaks via attribute join
                # (keycol_pts inherits fid from ambig_peaks -> join via fid)
                ambig_for_formula = processing.run("native:joinattributestable", {
                    'INPUT':        ambig_peaks,
                    'FIELD':        'fid',
                    'INPUT_2':      kc_zonal,
                    'FIELD_2':      'fid',
                    'FIELDS_TO_COPY': [z1m_col_field],
                    'METHOD':       1,
                    'DISCARD_NONMATCHING': False,
                    'PREFIX':       '',
                    'OUTPUT':       'TEMPORARY_OUTPUT',
                }, context=context, feedback=feedback)['OUTPUT']
            else:
                feedback.pushWarning("  Kein z1m_col_* Feld — Keycol bleibt bei 10m")
                has_keycol_coords = False
        else:
            feedback.pushInfo("Step 3b/5 — keycol_x/y nicht vorhanden → Keycol bleibt 10m")

        # ----------------------------------------------------------------
        # STEP 4 - join 1 m elevation back onto the point layer
        # spatial join (point lies inside its own buffer -> intersects OK)
        # ----------------------------------------------------------------
        feedback.pushInfo("Step 4/5 — Joine 1 m Elevation zurück auf Punkte...")
        processing.run("native:createspatialindex",
                       {'INPUT': ambig_peaks}, context=context, feedback=feedback)
        processing.run("native:createspatialindex",
                       {'INPUT': zonal_max}, context=context, feedback=feedback)

        joined = processing.run("native:joinattributesbylocation", {
            'INPUT':       ambig_for_formula,
            'JOIN':        zonal_max,
            'PREDICATE':   [0],            # intersects
            'JOIN_FIELDS': [z1m_field],    # FIX-C: dynamischer Feldname
            'METHOD':      1,
            'PREFIX':      '',
            'OUTPUT':      'TEMPORARY_OUTPUT',
        }, context=context, feedback=feedback)['OUTPUT']

        # formula: summit 1m (coalesce), saddle 1m if present otherwise 10m
        if has_keycol_coords and z1m_col_field:
            formula = (
                f'coalesce("{z1m_field}", "{zpk_field}") - '
                f'coalesce("{z1m_col_field}", "keycol")'
            )
            feedback.pushInfo(f"  Prominenz-Formel (1m Gipfel + 1m Sattel): {formula}")
        else:
            formula = f'coalesce("{z1m_field}", "{zpk_field}") - "keycol"'
        feedback.pushInfo(f"  Prominenz-Formel: {formula}")

        calc_prom = processing.run("native:fieldcalculator", {
            'INPUT':      joined,
            'FIELD_NAME': 'prom_ref',
            'FIELD_TYPE': 0,
            'FORMULA':    formula,
            'OUTPUT':     'TEMPORARY_OUTPUT',
        }, context=context, feedback=feedback)['OUTPUT']

        # ----------------------------------------------------------------
        # filter: keep only peaks with prom_ref >= 150 m
        # ----------------------------------------------------------------
        valid_refined = processing.run("native:extractbyexpression", {
            'INPUT':      calc_prom,
            'EXPRESSION': '"prom_ref" >= 150',
            'OUTPUT':     'TEMPORARY_OUTPUT',
        }, context=context, feedback=feedback)['OUTPUT']
        feedback.pushInfo(
            f"  Ambiguous → gültig (prom_ref ≥ 150 m): {valid_refined.featureCount()}")

        # ----------------------------------------------------------------
        # STEP 5 — Schema angleichen + Merge
        #
        # safe_peaks needs prom_ref and z1m_max so that mergevectorlayers
        # produces no NULL columns.
        # ----------------------------------------------------------------
        feedback.pushInfo("Step 5/5 — Schema angleichen und mergen...")

        safe_with_ref = processing.run("native:fieldcalculator", {
            'INPUT':      safe_peaks,
            'FIELD_NAME': 'prom_ref',
            'FIELD_TYPE': 0,
            'FORMULA':    '"prom"',
            'OUTPUT':     'TEMPORARY_OUTPUT',
        }, context=context, feedback=feedback)['OUTPUT']

        safe_with_z1m = processing.run("native:fieldcalculator", {
            'INPUT':      safe_with_ref,
            'FIELD_NAME': z1m_field,       # FIX-C: gleicher Name wie im refined layer
            'FIELD_TYPE': 0,
            'FORMULA':    'NULL',
            'OUTPUT':     'TEMPORARY_OUTPUT',
        }, context=context, feedback=feedback)['OUTPUT']

        # if the keycol zonal-min field is present, add it to safe_peaks too
        if has_keycol_coords and z1m_col_field:
            safe_with_z1m = processing.run("native:fieldcalculator", {
                'INPUT':      safe_with_z1m,
                'FIELD_NAME': z1m_col_field,
                'FIELD_TYPE': 0,
                'FORMULA':    'NULL',
                'OUTPUT':     'TEMPORARY_OUTPUT',
            }, context=context, feedback=feedback)['OUTPUT']

        # mergevectorlayers adds 'layer' and 'path' columns automatically.
        # Remove them via deletecolumn before final output.
        merged_tmp = processing.run("native:mergevectorlayers", {
            'LAYERS': [safe_with_z1m, valid_refined],
            'OUTPUT': 'TEMPORARY_OUTPUT',
        }, context=context, feedback=feedback)['OUTPUT']

        # Remove QGIS-internal metadata columns added by mergevectorlayers
        cols_to_drop = [f.name() for f in merged_tmp.fields()
                        if f.name() in ('layer', 'path')]
        if cols_to_drop:
            merged_tmp = processing.run("native:deletecolumn", {
                'INPUT':  merged_tmp,
                'COLUMN': cols_to_drop,
                'OUTPUT': 'TEMPORARY_OUTPUT',
            }, context=context, feedback=feedback)['OUTPUT']

        merged = processing.run("native:savefeatures", {
            'INPUT':  merged_tmp,
            'OUTPUT': parameters[self.OUTPUT_SOTA],
        }, context=context, feedback=feedback)['OUTPUT']

        from qgis.core import QgsVectorLayer as _QgsVL
        merged_lyr = _QgsVL(merged, 'merged', 'ogr') if isinstance(merged, str) else merged
        n_valid = merged_lyr.featureCount() if merged_lyr.isValid() else '?'

        feedback.pushInfo(
            f"Refinement abgeschlossen. "
            f"SOTA-gültige Peaks (P >= 150 m): {n_valid}")
        feedback.pushInfo(
            f"Output-Felder: "
            f"{[f.name() for f in merged_lyr.fields()]}")

        return {self.OUTPUT_SOTA: merged}
