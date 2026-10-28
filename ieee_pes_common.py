# -*- coding: utf-8 -*-
"""
Shared input / fleet / baseline-cache helpers for ieee_gen_dec.py and
ieee_gen_inc.py (and the one-time ieee_pes_prepare_inputs.py setup).

INPUTS: this project's own Input_Files/ (base homes + weather + the
virtual-unit manifest), built once by ieee_pes_prepare_inputs.py. Nothing
here reads outside this folder.

FLEET: a test runs on the FIRST fleet_size rows of the manifest. The
manifest is ordered slice-major (every base home's slice 0, then slice 1,
...), so any fleet size uses every home before reusing one, and a
smaller fleet is always a subset of a larger one (1000 is inside 1500,
which is inside 3400, ...).

CANONICAL TIME AXIS: each virtual unit simulates its OWN calendar window
(its manifest start_time), but every dispatch decision and saved output
uses one shared clock -- step index i maps to canonical_times[i] for every
unit, so the event happens at the same step for everyone regardless of
which real dates a unit simulated.

BASELINE CACHE: baseline depends only on the unit (home, window, seed) and
the baseline-affecting settings, never on a test's target/duration -- so
each unit's baseline is simulated once and reused by every later DEC or
INC test. Cached files live under Baseline_Cache/<settings hash>/, so
changing any baseline-affecting setting starts a fresh cache instead of
silently reusing a stale one.
"""

import os
import json
import hashlib
import shutil
import datetime as dt
import pandas as pd
from ochre.utils.schedule import ALL_SCHEDULE_NAMES

#########################################
# PATHS
#########################################

WORKING_DIR = os.path.dirname(os.path.abspath(__file__))
Input_folder = "Input_Files"
INPUT_DIR = os.path.join(WORKING_DIR, Input_folder)
HOMES_DIR = os.path.join(INPUT_DIR, "bldg")
WEATHER_FILE = os.path.join(INPUT_DIR, "Weather", "USA_OR_Portland.Intl.AP.726980_TMY3.epw")
VIRTUAL_UNITS_FILE = os.path.join(INPUT_DIR, "virtual_units.csv")
XML_ADDRESS = "home.xml"
CSV_ADDRESS = "in.schedules.csv"

FILTERED_SCHED_DIR = os.path.join(WORKING_DIR, "Filtered_Schedules")

# Large-scale outputs live on the shared drive (each test in its own
# <root>/<test name>/ folder; the baseline cache under its own root).
# Env vars override, e.g. with a UNC path when Z: isn't mapped.
DEC_RESULTS_ROOT = os.environ.get('OCHRE_DEC_RESULTS_DIR', r"Z:\dataset\DEC")
INC_RESULTS_ROOT = os.environ.get('OCHRE_INC_RESULTS_DIR', r"Z:\dataset\INC")
BASELINE_CACHE_DIR = os.environ.get('OCHRE_BASELINE_DIR', r"Z:\dataset\Baseline")

# Per-unit OCHRE scratch output (local disk -- fast, and deleted as soon as
# each unit is finalized). See unit_scratch_dir().
SCRATCH_DIR = os.environ.get('OCHRE_SCRATCH_DIR', os.path.join(WORKING_DIR, "_ochre_scratch"))

#########################################
# DAY-SLICES (manifest calendar windows) -- slice k starts SLICE_DAYS*k
# days after DAY_SLICE_BASE_START. SLICE_DAYS matches the test scripts'
# own simulation Duration (2 days), so windows don't overlap.
#########################################

DAY_SLICE_BASE_START = dt.datetime(2018, 1, 11, 0, 0)
SLICE_DAYS = 2

def day_slice_start(slice_idx):
    return DAY_SLICE_BASE_START + dt.timedelta(days=slice_idx * SLICE_DAYS)

#########################################
# FLEET SELECTION
#########################################

def load_manifest():
    if not os.path.exists(VIRTUAL_UNITS_FILE):
        raise FileNotFoundError(
            f"{VIRTUAL_UNITS_FILE} not found -- run ieee_pes_prepare_inputs.py first."
        )
    return pd.read_csv(VIRTUAL_UNITS_FILE, parse_dates=["start_time"])

def select_fleet(fleet_size):
    """First fleet_size manifest rows, as a list of unit dicts."""
    manifest = load_manifest()
    if fleet_size > len(manifest):
        raise ValueError(
            f"Fleet size {fleet_size} is larger than the manifest ({len(manifest)} units) -- "
            f"rebuild it with ieee_pes_prepare_inputs.py (PREP_N_UNITS) first."
        )
    return [
        {
            "unit_id": row.virtual_id,
            "home_id": row.home_id,
            "home_path": os.path.join(HOMES_DIR, row.home_id),
            "start_time": row.start_time.to_pydatetime(),
            "seed": row.seed,
        }
        for row in manifest.head(fleet_size).itertuples(index=False)
    ]

#########################################
# SCHEDULE FILTERING -- once per base home, cached in Filtered_Schedules/
# (never written into Input_Files). Call it for every home BEFORE starting
# worker threads, so concurrent units of the same home never race on it.
#########################################

def get_filtered_schedule_file(home_path):
    home_id = os.path.basename(home_path)
    filtered_sched_file = os.path.join(FILTERED_SCHED_DIR, home_id, 'filtered_schedules.csv')
    if os.path.exists(filtered_sched_file):
        return filtered_sched_file

    df_sched = pd.read_csv(os.path.join(home_path, CSV_ADDRESS))
    valid_schedule_names = set(ALL_SCHEDULE_NAMES.keys())
    filtered_columns = [col for col in df_sched.columns if col in valid_schedule_names]
    dropped_columns = [col for col in df_sched.columns if col not in filtered_columns]
    if dropped_columns:
        print(f"Dropped invalid schedules for {home_id}: {dropped_columns}")

    os.makedirs(os.path.dirname(filtered_sched_file), exist_ok=True)
    df_sched[filtered_columns].to_csv(filtered_sched_file, index=False)
    return filtered_sched_file

#########################################
# CANONICAL TIME AXIS
#########################################

def make_canonical_times(start, duration_days, t_res_min):
    n_steps = round(duration_days * 24 * 60 / t_res_min)
    return list(pd.date_range(start, periods=n_steps, freq=f"{t_res_min}min"))

def to_canonical(df, canonical_times):
    """Relabels df's Time (column or index) onto canonical_times, by ROW
    POSITION. df must have exactly one row per canonical step, in order."""
    if 'Time' not in df.columns:
        df = df.reset_index()
        if 'index' in df.columns:
            df = df.rename(columns={'index': 'Time'})
    assert len(df) == len(canonical_times), (
        f"frame has {len(df)} rows, expected {len(canonical_times)} -- Duration/t_res mismatch?"
    )
    df = df.copy()
    df['Time'] = canonical_times
    return df

#########################################
# PER-UNIT OCHRE SCRATCH OUTPUT -- OCHRE keeps every timestep's results in
# memory unless it exports them periodically (export_res), which needs a
# results folder. Each unit gets its OWN folder (units sharing a base home
# would otherwise overwrite each other's files), which roughly halves
# per-unit memory (~43 MB -> ~21 MB measured at dispatch time). The folder
# is deleted once the unit's results have been read back.
#########################################

def unit_scratch_dir(run_name, unit_id):
    return os.path.join(SCRATCH_DIR, run_name, unit_id)

def remove_unit_scratch(run_name, unit_id):
    shutil.rmtree(unit_scratch_dir(run_name, unit_id), ignore_errors=True)

#########################################
# BASELINE CACHE
#########################################

def baseline_cache_dir(settings):
    """Cache folder for one set of baseline-affecting settings (a dict) --
    named by a short hash of them, with the settings themselves saved
    alongside for reference."""
    text = json.dumps(settings, sort_keys=True, default=str)
    cache_dir = os.path.join(BASELINE_CACHE_DIR, hashlib.sha1(text.encode()).hexdigest()[:10])
    os.makedirs(cache_dir, exist_ok=True)
    settings_file = os.path.join(cache_dir, "settings.json")
    if not os.path.exists(settings_file):
        with open(settings_file, "w") as f:
            f.write(json.dumps(settings, sort_keys=True, default=str, indent=2))
    return cache_dir

def load_cached_baseline(cache_dir, unit_id):
    path = os.path.join(cache_dir, unit_id + ".parquet")
    if not os.path.exists(path):
        return None
    df = pd.read_parquet(path)
    df['Time'] = pd.to_datetime(df['Time'])
    return df

def save_cached_baseline(cache_dir, unit_id, df):
    df.to_parquet(os.path.join(cache_dir, unit_id + ".parquet"), index=False)
