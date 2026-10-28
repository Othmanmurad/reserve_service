# -*- coding: utf-8 -*-
"""
One-time setup: builds this project's own self-contained input dataset,
Input_Files/, so nothing else in IEEE_PES needs the FlexLoadCalculator
folder. Runs NO simulations -- it only copies files and writes a CSV.

  Input_Files/
    bldg/<home_id>/home.xml            -- one copy per BASE home (498)
    bldg/<home_id>/in.schedules.csv
    Weather/<weather>.epw
    virtual_units.csv                  -- the N-unit fleet manifest

BASE HOMES: the 498 homes listed in home_list.txt (exactly the set every
earlier FlexLoadCalculator DEC test ran on). Only the two files OCHRE
needs are copied -- not the old per-home OCHRE outputs that also live in
those source folders.

VIRTUAL UNITS (manifest, not copies): each base home is reused for
several virtual units that differ by CALENDAR WINDOW (day-slicing, same
mechanism as before -- see ieee_gen_dec.py's DAY-SLICING note). Slice k
starts on DAY_SLICE_BASE_START + k*SLICE_DAYS days, so it simulates a
different real window of that home's own schedule and weather. Units
are ordered slice-major (slice 0 for every home, then slice 1, ...), so
taking the first M rows of the manifest (OCHRE_MAX_VIRTUAL_UNITS) always
gives an even spread across homes. When N isn't a multiple of the home
count, the homes that get the extra, last slice are picked by a fixed
random permutation (seed 42), not by home ID.

Usage (from this folder, with .venv active):
  python ieee_pes_prepare_inputs.py
Options via env vars:
  PREP_SOURCE_DIR   folder holding the source homes' <home_id> subfolders
                    (default: FlexLoadCalculator's HPWH All Input Files\\bldg)
  PREP_WEATHER_FILE source .epw file to copy
  PREP_N_UNITS      fleet size to write to the manifest (default 10000)

Re-running is safe: files already copied (same size) are skipped, and the
manifest is rewritten.
"""

import os
import math
import random
import shutil
import datetime as dt
import pandas as pd

from ieee_pes_common import (
    WORKING_DIR, HOMES_DIR, WEATHER_FILE, VIRTUAL_UNITS_FILE,
    XML_ADDRESS, CSV_ADDRESS, SLICE_DAYS, day_slice_start,
)

SOURCE_DIR = os.environ.get(
    'PREP_SOURCE_DIR', r"C:\Users\othma\FlexLoadCalculator\HPWH\HPWH All Input Files\bldg"
)
SOURCE_WEATHER_FILE = os.environ.get(
    'PREP_WEATHER_FILE',
    r"C:\Users\othma\FlexLoadCalculator\HPWH\Weather\USA_OR_Portland.Intl.AP.726980_TMY3.epw",
)
N_UNITS = int(os.environ.get('PREP_N_UNITS', 10000))
HOME_LIST_FILE = os.path.join(WORKING_DIR, "home_list.txt")


def copy_if_changed(src, dst):
    if os.path.exists(dst) and os.path.getsize(dst) == os.path.getsize(src):
        return False
    os.makedirs(os.path.dirname(dst), exist_ok=True)
    shutil.copy2(src, dst)
    return True


def build_manifest(home_ids, n_units):
    order = list(home_ids)
    random.Random(42).shuffle(order)
    n_slices = math.ceil(n_units / len(order))
    last_end = day_slice_start(n_slices - 1) + dt.timedelta(days=SLICE_DAYS)
    assert last_end.year == day_slice_start(0).year, (
        f"{n_slices} slices of {SLICE_DAYS} days run past the end of the year ({last_end})"
    )

    rows = []
    for k in range(n_slices):
        for home_id in order:
            if len(rows) == n_units:
                break
            rows.append({
                "virtual_id": f"{home_id}__d{k}",
                "home_id": home_id,
                "slice_idx": k,
                "start_time": day_slice_start(k),
                "seed": f"{home_id}#slice{k}",
            })
    return pd.DataFrame(rows)


if __name__ == "__main__":
    with open(HOME_LIST_FILE) as f:
        home_ids = [line.strip() for line in f if line.strip()]
    print(f"Base homes listed in {os.path.basename(HOME_LIST_FILE)}: {len(home_ids)}")

    missing = [h for h in home_ids
               if not all(os.path.isfile(os.path.join(SOURCE_DIR, h, n)) for n in (XML_ADDRESS, CSV_ADDRESS))]
    if missing:
        raise SystemExit(f"{len(missing)} listed homes are missing {XML_ADDRESS}/{CSV_ADDRESS} "
                         f"in {SOURCE_DIR}: {missing[:5]}")

    copied = 0
    for i, home_id in enumerate(home_ids, 1):
        for name in (XML_ADDRESS, CSV_ADDRESS):
            copied += copy_if_changed(os.path.join(SOURCE_DIR, home_id, name),
                                      os.path.join(HOMES_DIR, home_id, name))
        if i % 100 == 0 or i == len(home_ids):
            print(f"  homes ready: {i}/{len(home_ids)}")
    copied += copy_if_changed(SOURCE_WEATHER_FILE, WEATHER_FILE)
    print(f"Copied {copied} files (unchanged files skipped) into {os.path.dirname(HOMES_DIR)}")

    manifest = build_manifest(home_ids, N_UNITS)
    manifest.to_csv(VIRTUAL_UNITS_FILE, index=False)
    per_home = manifest['home_id'].value_counts()
    print(f"Manifest: {len(manifest)} virtual units from {manifest['home_id'].nunique()} base homes "
          f"({per_home.min()}-{per_home.max()} units per home, slices 0-{manifest['slice_idx'].max()}, "
          f"windows {manifest['start_time'].min():%b %d} to "
          f"{manifest['start_time'].max() + dt.timedelta(days=SLICE_DAYS - 1):%b %d})")
    print(f"Written to: {VIRTUAL_UNITS_FILE}")
