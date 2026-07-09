#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
find_routing_targets.py  —  v1.0
Post-processing: routing target peak for each summit in peaks_prom_raw.gpkg.

The routing target peak is the next higher summit that the Union-Find had as
the target component at the key-col merge. Since target_pk_idx from the hot
UF loop is too expensive (+20 GB memmap), it is reconstructed geometrically here:

Method:
  For each summit P with key col K(P) at (kx, ky):
  - Sought: summit Q with zpk(Q) > zpk(P) and Q lies "behind" the key col
    (i.e. in the extension P->K or at least on the side facing K)
  - Practical: Q = the nearest summit with zpk > zpk(P) that lies closest
    to the key col (distance key col->Q minimised)

The result is an approximation. The exact target peak would only be
reconstructable from the internal UF state.

Usage:
  python3 find_routing_targets.py \\
      --input  /Volumes/Daten/AT_SOTA_150m/intermediate_step2v28/peaks_prom_raw.gpkg \\
      --output /Volumes/Daten/AT_SOTA_150m/intermediate_step2v28/peaks_prom_raw_targets.gpkg

Adds three new fields:
  target_pk_x     FLOAT  X coordinate of the routing target peak (EPSG:25833)
  target_pk_y     FLOAT  Y coordinate
  target_pk_elev  FLOAT  elevation of the routing target peak [m]
  target_pk_dist  FLOAT  distance key col -> target peak [km]
"""

import argparse
import math
import os
import shutil
import sqlite3
import struct
import sys
from datetime import datetime


def wkb_xy(blob):
    if not blob or len(blob) < 8: return None, None
    flags = blob[3]; env = {0:0,1:32,2:48,3:48,4:64}.get((flags>>1)&7,0)
    wkb = blob[8+env:]
    if len(wkb) < 21: return None, None
    e = "<" if wkb[0] == 1 else ">"
    x, y = struct.unpack_from(f"{e}dd", wkb, 5)
    return x, y


def build_grid_index(peaks, cell_m=10_000):
    """Spatial grid index: (cx,cy) -> list of peak dicts."""
    idx = {}
    for p in peaks:
        if p['x'] is None: continue
        cx = int(p['x'] // cell_m)
        cy = int(p['y'] // cell_m)
        idx.setdefault((cx, cy), []).append(p)
    return idx, cell_m


def find_nearest_higher(px, py, pz, kx, ky, grid, cell_m,
                        search_km=300):
    """
    Find nearest peak Q with zpk > pz, minimising distance from keycol (kx,ky).
    Expands search radius until a candidate is found.
    """
    search_m = min(search_km * 1000, 3_000_000)
    best = None
    best_d = float('inf')

    # Expand outward from keycol
    for radius in [50_000, 100_000, 200_000, 500_000, search_m]:
        cells_checked = 0
        cx0 = int((kx - radius) // cell_m)
        cx1 = int((kx + radius) // cell_m)
        cy0 = int((ky - radius) // cell_m)
        cy1 = int((ky + radius) // cell_m)
        for cx in range(cx0, cx1+1):
            for cy in range(cy0, cy1+1):
                for q in grid.get((cx, cy), []):
                    if q['zpk'] <= pz: continue
                    if q['x'] is None: continue
                    # Dist from KEYCOL to Q (routing target is beyond the keycol)
                    d = math.sqrt((q['x']-kx)**2 + (q['y']-ky)**2)
                    if d < best_d:
                        best_d = d
                        best = q
                cells_checked += 1
        if best is not None:
            break

    return best, best_d


def main():
    ap = argparse.ArgumentParser(
        description="Add routing-target fields to peaks_prom_raw.gpkg"
    )
    ap.add_argument("--input",  required=True)
    ap.add_argument("--output", required=False, default=None)
    ap.add_argument("--layer",  default="peaks_prom_raw")
    ap.add_argument("--search-km", type=float, default=500,
                    help="Max search radius for routing target [km]")
    ap.add_argument("--inplace", action="store_true",
                    help="Modify input file in-place (no --output needed)")
    args = ap.parse_args()
    if args.inplace or args.output is None:
        args.output = args.input

    if not os.path.exists(args.input):
        print(f"ERROR: input not found: {args.input}"); sys.exit(1)

    # Copy input to output (skip if in-place)
    if args.input != args.output:
        if os.path.exists(args.output):
            os.remove(args.output)
        shutil.copy2(args.input, args.output)

    conn = sqlite3.connect(args.output)
    cur  = conn.cursor()

    # Check layer
    cur.execute("SELECT table_name FROM gpkg_contents WHERE data_type='features'")
    layers = [r[0] for r in cur.fetchall()]
    tbl = args.layer if args.layer in layers else layers[0]

    # Add new columns if not present
    cur.execute(f"PRAGMA table_info({tbl})")
    existing = {r[1] for r in cur.fetchall()}
    for col, typ in [('target_pk_x','REAL'),('target_pk_y','REAL'),
                     ('target_pk_elev','REAL'),('target_pk_dist','REAL')]:
        if col not in existing:
            cur.execute(f"ALTER TABLE {tbl} ADD COLUMN {col} {typ}")
    conn.commit()

    # Load all peaks
    cur.execute(f"SELECT fid, geom, zpk_1, keycol_x, keycol_y FROM {tbl}")
    rows = cur.fetchall()
    print(f"Loaded {len(rows):,} peaks")

    peaks = []
    for fid, geom, zpk, kx, ky in rows:
        x, y = wkb_xy(geom)
        peaks.append({'fid': fid, 'x': x, 'y': y, 'zpk': zpk or 0,
                      'kx': kx, 'ky': ky})

    # Build spatial index
    grid, cell_m = build_grid_index(peaks)
    print(f"Grid index built ({len(grid)} cells)")

    # For each peak, find routing target
    print("Finding routing targets...")
    updates = []
    n = len(peaks)
    for i, p in enumerate(peaks):
        if i % 500 == 0:
            print(f"  {i:,} / {n:,}", end="\r", flush=True)
        if p['kx'] is None or p['ky'] is None:
            updates.append((None, None, None, None, p['fid']))
            continue
        tgt, dist_m = find_nearest_higher(
            p['x'], p['y'], p['zpk'],
            p['kx'], p['ky'],
            grid, cell_m, args.search_km
        )
        if tgt:
            updates.append((tgt['x'], tgt['y'], tgt['zpk'],
                            round(dist_m/1000, 2), p['fid']))
        else:
            updates.append((None, None, None, None, p['fid']))

    print(f"\nUpdating {len(updates):,} rows...")

    # Drop R-Tree/spatial triggers before UPDATE — they call ST_IsEmpty()
    # which requires SpatiaLite. Since only attribute columns are written
    # (no geometry changes) the spatial index stays valid without them.
    cur.execute("SELECT name FROM sqlite_master "
                "WHERE type='trigger' AND tbl_name=?", (tbl,))
    trigger_names = [r[0] for r in cur.fetchall()]
    for tn in trigger_names:
        cur.execute(f'DROP TRIGGER IF EXISTS "{tn}"')
    conn.commit()
    if trigger_names:
        print(f"  ({len(trigger_names)} spatial triggers suppressed"
              f" — geometries unchanged, index stays valid)")

    cur.executemany(
        f"UPDATE {tbl} SET target_pk_x=?, target_pk_y=?, "
        f"target_pk_elev=?, target_pk_dist=? WHERE fid=?",
        updates
    )
    conn.commit()

    n_found = sum(1 for u in updates if u[0] is not None)
    print(f"Routing targets found: {n_found:,} / {n:,}")
    print(f"Output: {args.output}")
    conn.close()


if __name__ == "__main__":
    main()
