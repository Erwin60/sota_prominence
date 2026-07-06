# -*- coding: utf-8 -*-
# Filename: AT_SOTA_Refine1m.py
# Version:  3.0  — Keycol 1m-Verfeinerung (Zonal-Min) hinzugefügt
#
# CHANGES vs. 2.1:
# NEW  Keycol 1m-Verfeinerung
#   Wenn keycol_x/keycol_y vorhanden (aus PixelMinimax): Zonal-Min(1m, 25m Radius)
#   rund um den Sattel-Pixel. Formel: prom_ref = z1m_max(Gipfel) - z1m_min(Sattel)
#   Falls keycol_x/y fehlen: Fallback auf altes Verhalten (keycol aus 10m).
#
# CHANGES vs. 2.0:
#
# FIX-A  Feldname 'zpk_1' in der Formel abgesichert
#   native:rastersampling mit COLUMN_PREFIX='zpk_' erzeugt 'zpk_1' (mit
#   Bandnummer) oder 'zpk_' (ohne). Der Pre-Flight-Check ermittelt jetzt
#   den tatsächlichen Feldnamen dynamisch und verwendet ihn in der Formel.
#
# FIX-B  Leerprüfung für ambiguous band
#   Im Wien-Testgebiet (fast keine Berge) kann ambig_peaks leer sein.
#   In diesem Fall werden die Buffer/Zonal/Join-Schritte übersprungen
#   und nur safe_peaks wird als Ergebnis zurückgegeben.
#
# FIX-C  Zonal Statistics Feldname abgesichert
#   native:zonalstatisticsfb mit COLUMN_PREFIX='z1m_' und STATISTICS=[6]
#   erzeugt 'z1m_max' in QGIS 3.3x, aber ältere Versionen können
#   'z1m_6' erzeugen. Der Join-Feldname wird dynamisch bestimmt.

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
        """Gibt den ersten Feldnamen zurück, der mit 'prefix' beginnt."""
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
        # Pre-Flight: Pflichtfelder prüfen
        # ----------------------------------------------------------------
        field_names = [f.name() for f in peaks.fields()]
        for required in ('prom', 'keycol'):
            if required not in field_names:
                raise QgsProcessingException(
                    f"Pflichtfeld '{required}' fehlt im Peaks-Layer. "
                    f"Vorhandene Felder: {field_names}. "
                    f"Layer muss von keycol_minimax v2.1 stammen.")

        # FIX-A: tatsächlichen zpk_-Feldnamen ermitteln
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
        # FIX-B: Leerprüfung — ambiguous band kann in jedem Testgebiet leer sein
        #
        # Das ist kein Hinweis auf flaches Gelände, sondern einfach der
        # normale Fall wenn kein Peak zufällig eine 10m-Prominenz zwischen
        # 130 und 170 m hat. Beispiel Wien-Testgebiet: der Hermannskogel
        # (449 m) hat eine Prominenz weit über 170 m und fällt direkt in
        # safe_peaks — der ambiguous band ist leer, obwohl das Gebiet
        # durchaus Topographie hat.
        # Ohne diese Prüfung würden buffer/zonal/join auf einem leeren
        # Layer laufen und QGIS würde mit einem Fehler abbrechen.
        # ----------------------------------------------------------------
        if ambig_peaks.featureCount() == 0:
            feedback.pushInfo(
                "  Keine Peaks im ambiguous band (130–170 m) — "
                "überspringe 1 m Refinement.")

            # Schema der safe_peaks um prom_ref und z1m_max erweitern
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
        # STEP 2 — Puffer um ambiguous Peaks (25 m Radius)
        # ----------------------------------------------------------------
        feedback.pushInfo("Step 2/5 — Puffere ambiguous Peaks (25 m Radius)...")
        buffered = processing.run("native:buffer", {
            'INPUT':    ambig_peaks,
            'DISTANCE': 25,
            'SEGMENTS': 8,
            'OUTPUT':   'TEMPORARY_OUTPUT',
        }, context=context, feedback=feedback)['OUTPUT']

        # ----------------------------------------------------------------
        # STEP 3 — Zonales Maximum im 1 m DEM
        # ----------------------------------------------------------------
        feedback.pushInfo("Step 3/5 — Berechne 1 m Zonal-Maximum (25 m Radius)...")
        zonal_max = processing.run("native:zonalstatisticsfb", {
            'INPUT':         buffered,
            'INPUT_RASTER':  dem1m,
            'STATISTICS':    [6],          # 6 = max
            'COLUMN_PREFIX': 'z1m_',
            'OUTPUT':        'TEMPORARY_OUTPUT',
        }, context=context, feedback=feedback)['OUTPUT']

        # FIX-C: tatsächlichen Feldnamen des Zonal-Max ermitteln
        z1m_field = self._field_by_prefix(zonal_max, 'z1m_')
        if z1m_field is None:
            raise QgsProcessingException(
                "Kein 'z1m_*' Feld nach zonalstatisticsfb gefunden. "
                f"Felder: {[f.name() for f in zonal_max.fields()]}")
        feedback.pushInfo(f"  Zonal-Max Feld: '{z1m_field}'")

        # ----------------------------------------------------------------
        # STEP 3b — Zonal-Min für Sattel (keycol_x / keycol_y)
        # Falls PixelMinimax die Sattel-Koordinaten mitgeliefert hat,
        # verfeinern wir auch den Keycol auf 1m Präzision.
        # ----------------------------------------------------------------
        has_keycol_coords = (
            'keycol_x' in [f.name() for f in ambig_peaks.fields()] and
            'keycol_y' in [f.name() for f in ambig_peaks.fields()]
        )

        z1m_col_field = None
        ambig_for_formula = ambig_peaks  # wird ggf. durch gejointen Layer ersetzt

        if has_keycol_coords:
            feedback.pushInfo("Step 3b/5 — Keycol 1m Zonal-Min (25 m Radius)...")

            # Keycol-Punkte direkt aus keycol_x/keycol_y erstellen (kein Algorithmus nötig)
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

            # 25m Puffer um Sattel-Punkte
            kc_buf = processing.run("native:buffer", {
                'INPUT':    keycol_pts,
                'DISTANCE': 25,
                'SEGMENTS': 8,
                'OUTPUT':   'TEMPORARY_OUTPUT',
            }, context=context, feedback=feedback)['OUTPUT']

            # Zonal-Min auf 1m DEM
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

                # Join keycol-Min zurück auf ambig_peaks via Attribut-Join
                # (keycol_pts erbt fid aus ambig_peaks → Join über fid)
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
        # STEP 4 — 1 m Höhe zurück auf Punkt-Layer joinen
        # Spatial Join (Punkt liegt im eigenen Puffer → intersects OK)
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

        # Formel: Gipfel 1m (coalesce), Sattel 1m wenn vorhanden sonst 10m
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
        # Filtere: nur Peaks mit prom_ref ≥ 150 m behalten
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
        # safe_peaks braucht prom_ref und z1m_max damit mergevectorlayers
        # keine NULL-Spalten erzeugt.
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

        # Falls keycol-Zonal-Min Feld vorhanden, auch in safe_peaks ergänzen
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
