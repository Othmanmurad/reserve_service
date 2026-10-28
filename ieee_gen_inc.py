# -*- coding: utf-8 -*-
"""
Forked from ieee_gen_dec.py for the IEEE generation-INCREMENT (INC /
SHED, i.e. downward-load / upward-reserve) dispatch study -- the mirror
image of ieee_gen_dec.py's generation-DECREMENT (DEC / LOAD-UP)
mechanism: DEC makes the fleet draw MORE because the grid has a
generation SURPLUS to soak up; INC makes the fleet draw LESS because
the grid has a generation DEFICIT (classic demand-response / upward
reserve) -- see ieee_gen_dec.py's own module docstring for the
project's INC/DEC terminology. Replicates the SHED direction of
HPWH_B3_Reserve_RD.py's mechanism (reduce load by commanding a lower
Setpoint/Deadband), but rebuilt on ieee_gen_dec.py's own validated
delta-P-capacity-capped architecture (tapered ramp, no-headroom
coldest/warmest-first ranking, recycling, gradual release), not RD's
older fixed-dispatch + power-capped-admission-recovery design.

Deliberately a SEPARATE file, not a modification of ieee_gen_dec.py,
HPWH_B3_Reserve_V2.py, HPWH_B3_Reserve_RD.py, HPWH_B4_Reserve_RU.py, or
the Excel-calculator pipeline scripts.

DISPATCH MECHANISM (reduction-capacity cap, with RECOVERY-based
recycling): the control target is the fleet's REDUCED load,
reduction(t) = sum(baseline kW) - sum(controlled kW) (positive while
shedding effectively) -- the mirror of ieee_gen_dec.py's delta_p(t).
TARGET_REDUCTION_KW is a POSITIVE magnitude (how much LESS the fleet
should draw vs. baseline); internally this script works entirely in
reduction-magnitude space (reduction(t) = -delta_p(t)) so every
admission/overshoot formula is a direct, readable mirror of
ieee_gen_dec.py's own.

THREE-STATE MECHANISM (mirror image of ieee_gen_dec.py's idle/load/shed):
  "idle"     -- baseline Setpoint/Deadband control (never dispatched,
                or fully retired).
  "shed"     -- the RESERVE ACTION: Setpoint=Tcontrol_SHEDC (126 deg F),
                Deadband=Tcontrol_deadbandC (10 deg F) -- a LOWER target
                than baseline, so the unit draws less than it would
                have. This is the SAME control signal ieee_gen_dec.py
                uses for its own "shed" (parking) state -- there, it's
                where ceilinged-out LOAD units park; here, it's the
                action itself.
  "recovery" -- the limit-breach PARKING state (mirrors ieee_gen_dec.py's
                ceiling-triggered "shed" parking, in the opposite
                direction): a "shed" unit whose tank has cooled to
                MIN_TANK_TEMP_C is forced into HP Duty Cycle=1.0 (the
                SAME control signal ieee_gen_dec.py's "load" action
                uses) to quickly restore a safe thermal buffer, instead
                of risking running out of hot water. Once warm again it
                re-enters the shed candidate pool (recycling).

Mechanics:
  1. Baseline runs FIRST, for every home, for the full simulation --
     stored as a per-home time-indexed power lookup, reused both for
     reduction(t) and (in finalize_and_save_home()) instead of re-
     running a second baseline there.
  2. Initial ramp-down (dispatch_t until reduction(t) first reaches
     target): every RAMP_BATCH_SPACING_MIN minutes, as many WARMEST-
     first candidates as the shortfall-implied count calls for (capped
     only by how many are actually available right now) are admitted
     into "shed" -- unlike ieee_gen_dec.py's ramp-up, there is
     deliberately NO flat per-batch admission cap here: the fresh-
     candidate pool (homes CURRENTLY actively drawing baseline power)
     is small and perishable -- it keeps shrinking minute to minute
     regardless of how many are admitted -- so a flat cap just wastes
     a large early pool instead of protecting against anything. (A
     flat cap earned its place in ieee_gen_dec.py because "load" is a
     raw HP Duty Cycle=1.0 force -- every admitted unit jumps to ~full
     draw instantly and simultaneously, so a big batch there means a
     big, synchronized, predictable power spike. "shed" is a Setpoint/
     Deadband command instead -- each admitted unit's response depends
     on where its own tank already was, so a big admission batch does
     NOT translate into a big synchronized power swing the same way.)
  3. Post-ramp, every timestep until event_end:
     a. Any "shed" unit whose tank has cooled to MIN_TANK_TEMP_C moves
        to "recovery" (not idle) -- checked every timestep.
     b. If reduction(t) is below TARGET_REDUCTION_KW - BAND_KW, admit
        enough NEW units into "shed" to close the shortfall, ranked
        warmest-first, from the COMBINED pool of: never-shed homes
        CURRENTLY ACTIVELY DRAWING POWER in their own baseline trace
        (so the SHED command actually reduces load), PLUS currently-
        "recovery" homes. No separate thermal-headroom eligibility
        filter -- warmest-first ranking alone keeps a near-floor
        candidate as a last resort.
     c. If reduction(t) overshoots TARGET_REDUCTION_KW + BAND_KW, the
        COLDEST "shed" units move to "recovery" early (optional,
        ALLOW_OVERSHOOT_RELEASE) -- mirrors ieee_gen_dec.py releasing
        its HOTTEST "load" units to "shed" early.
  4. At event_end: every unit still in "shed" or "recovery" is released
     straight to "idle" in groups of RELEASE_GROUP_SIZE, spaced
     RELEASE_GROUP_SPACING_MIN apart -- identical to ieee_gen_dec.py's
     own gradual wind-down.

A sanity assert checks, per home, that controlled power exactly equals
baseline power before dispatch_t -- if any home diverges, the run stops
and reports it rather than silently producing misleading reduction(t)
downstream.
"""

import os
import json
import shutil
import math
import datetime as dt
import threading
import pandas as pd
from ochre import Dwelling
import concurrent.futures
from run_context import save_filename, save_reserve_event, save_input_folder
from ieee_pes_common import (
    Input_folder, WEATHER_FILE, XML_ADDRESS, select_fleet, get_filtered_schedule_file,
    make_canonical_times, to_canonical, baseline_cache_dir, load_cached_baseline,
    save_cached_baseline, DAY_SLICE_BASE_START, SLICE_DAYS, INC_RESULTS_ROOT,
    unit_scratch_dir, remove_unit_scratch, SCRATCH_DIR,
)

_dwelling_init_lock = threading.Lock()

#########################################
# TEST SETUP -- change these for each test
#########################################

FLEET_SIZE = 1000             # number of units: the first FLEET_SIZE rows of Input_Files/virtual_units.csv (up to 10000)
TARGET_REDUCTION_KW = 100.0   # target load reduction (baseline - controlled), kW
EVENT_DURATION_HR = 4.0       # reserve event duration, hours

#########################################
# USER SETTINGS
#########################################

# Environment variables (set by run_dec_inc.ps1) override the TEST SETUP
# values when set.
FLEET_SIZE = int(os.environ.get('OCHRE_FLEET_SIZE', FLEET_SIZE))
TARGET_REDUCTION_KW = float(os.environ.get('OCHRE_TARGET_REDUCTION_KW', TARGET_REDUCTION_KW))
EVENT_DURATION_HR = float(os.environ.get('OCHRE_DURATION_HR', EVENT_DURATION_HR))

# Test name, built from the TEST SETUP values so different tests never
# overwrite each other's results. OCHRE_FILENAME overrides this when set.
filename = os.environ.get(
    'OCHRE_FILENAME', f"IEEE_INC_{FLEET_SIZE}_{TARGET_REDUCTION_KW:g}kW_{EVENT_DURATION_HR:g}h"
)

# pipeline="IEEE_INC" -- distinct from "IEEE_DEC"/"RU"/"RD" so this
# experimental file's last-run state never collides with any other
# pipeline's. Read back by ieee_inc_results.py.
save_filename(filename, pipeline="IEEE_INC")

# Inputs (base homes, weather, manifest) come from this project's own
# Input_Files/ -- see ieee_pes_common.py.
save_input_folder(Input_folder, pipeline="IEEE_INC")

# Per-test output folder on the shared drive (INC_RESULTS_ROOT in
# ieee_pes_common.py) -- keeps each run's aggregated CSVs alongside the
# analysis outputs they feed.
RESULTS_DIR = os.path.join(INC_RESULTS_ROOT, filename)

# Simulation parameters. Start is the CANONICAL start (every unit's own
# calendar window comes from the manifest; outputs use this clock).
Start = dt.datetime(2018, 1, 11, 0, 0)
Duration = 2  # days
t_res = int(os.environ.get('OCHRE_TIMESTEP_MIN', 1))  # minutes
CANONICAL_TIMES = make_canonical_times(Start, Duration, t_res)

# Baseline is cached per unit (Baseline_Cache/) and reused by every later
# DEC or INC test -- set OCHRE_REBUILD_BASELINE=1 to re-simulate it anyway
# (overwriting the cache) for the units in this fleet.
REBUILD_BASELINE = os.environ.get('OCHRE_REBUILD_BASELINE', '0') == '1'

# HPWH control parameters (deg F)
Tcontrol_SHEDF = 123
Tcontrol_deadbandF = 10
Tcontrol_LOADF = 130
Tcontrol_LOADdeadbandF = 2
TbaselineF = 130
TdeadbandF = 7
Tinit = 128

#########################################
# RESERVE EVENT SETTINGS
#########################################

if 'reserve_event' not in globals():
    reserve_event = {
        'dispatch_time': os.environ.get('OCHRE_DISPATCH_TIME', '14:00'),
        'duration': EVENT_DURATION_HR
    }

save_reserve_event(reserve_event, pipeline="IEEE_INC")

EVENT_DATE = (Start + dt.timedelta(days=Duration - 1)).date()

dispatch_t = pd.to_datetime(f"{EVENT_DATE} {reserve_event['dispatch_time']}")
event_end = dispatch_t + pd.Timedelta(hours=reserve_event['duration'])

#########################################
# DISPATCH SETTINGS
#########################################

# Target REDUCED load (baseline - controlled), reduction(t), in kW, as a
# POSITIVE magnitude, and the tolerance band around it. Internally this
# script works in reduction-magnitude space (reduction(t) = -delta_p(t))
# -- see module docstring. TARGET_REDUCTION_KW itself is set in TEST SETUP.
BAND_KW = float(os.environ.get('OCHRE_BAND_KW', 0.5))

reserve_event['target_reduction_kw'] = TARGET_REDUCTION_KW
reserve_event['band_kw'] = BAND_KW
save_reserve_event(reserve_event, pipeline="IEEE_INC")

os.makedirs(RESULTS_DIR, exist_ok=True)
with open(os.path.join(RESULTS_DIR, filename + "_run_config.json"), "w") as f:
    json.dump(reserve_event, f, indent=2)

# A candidate's own baseline power at the CURRENT timestep must be
# ABOVE this to count as "actively drawing in baseline" (eligible) --
# mirror of ieee_gen_dec.py's BASELINE_IDLE_THRESHOLD_KW (there,
# AT OR BELOW the same magnitude; see module docstring).
BASELINE_ACTIVE_THRESHOLD_KW = 0.1

RAMP_STEPS = max(1, round(3 / t_res))
EXPECTED_KW_PER_UNIT = 0.45

MAX_ADMISSIONS_SAFETY_CAP = 20

# Initial ramp-down: every RAMP_BATCH_SPACING_MIN minutes, admit as
# many warmest-first candidates as the shortfall calls for (capped only
# by availability, not by a flat batch-size limit) -- see module
# docstring for why no RAMP_BATCH_SIZE cap is used here, unlike
# ieee_gen_dec.py's ramp-up.
RAMP_BATCH_SPACING_MIN = 1

# If True, the COLDEST "shed" units move to "recovery" (not idle) early
# when reduction(t) overshoots TARGET_REDUCTION_KW + BAND_KW -- mirror
# of ieee_gen_dec.py's ALLOW_OVERSHOOT_RELEASE.
ALLOW_OVERSHOOT_RELEASE = True

# Post-event: NO reactivation of recovery units into shed. Every unit
# still in "shed" or "recovery" at event_end is released straight to
# "idle" in groups of RELEASE_GROUP_SIZE, spaced RELEASE_GROUP_SPACING_MIN
# apart -- identical mechanism to ieee_gen_dec.py.
RELEASE_GROUP_SIZE = 20
RELEASE_GROUP_SPACING_MIN = 15

# Forced recovery (reheat) -- the mirror of ieee_gen_dec.py's
# HP_DUTY_CYCLE_LOAD, used here for the "recovery" PARKING state
# instead of the reserve action itself (see module docstring).
HP_DUTY_CYCLE_RECOVERY = 1.0

# Max tank temperature (C) -- kept as ordinary equipment config (same
# value as ieee_gen_dec.py) so both mechanisms share identical
# equipment settings for apples-to-apples comparison. NOT used as a
# dispatch trigger here (see MIN_TANK_TEMP_C below for this script's
# own trigger threshold).
MAX_TANK_TEMP_C = 60

# Safety floor (deg C) for a "shed" unit's tank -- mirror of
# ieee_gen_dec.py's MAX_TANK_TEMP_C ceiling, in the opposite direction.
# At Tcontrol_SHEDF=123 (deg F), the shed setpoint's own natural
# thermostatic floor (Tcontrol_SHEDC - Tcontrol_deadbandC) lands almost
# EXACTLY here (45.0 deg C/113 deg F) -- unlike the original
# Tcontrol_SHEDF=126 setting (floor ~46.7 deg C/116 deg F, safely
# above this threshold, so it only ever acted as a rare backstop), this
# MIN_TANK_TEMP_C check now effectively becomes the PRIMARY trigger for
# the shed -> recovery transition, making that cycle controller-governed
# and deterministic -- the same role ieee_gen_dec.py's MAX_TANK_TEMP_C
# ceiling check plays for its "load" state (which also bypasses
# thermostat control via a raw HP Duty Cycle=1.0 command and so
# genuinely needs an explicit check rather than relying on OCHRE's own
# Setpoint/Deadband logic).
MIN_TANK_TEMP_C = 45.0

#########################################
# TEMPERATURE CONVERSIONS F to C
#########################################

def f_to_c(temp_f):
    return (temp_f - 32) * 5/9

def f_to_c_DB(temp_f):
    return 5/9 * temp_f

Tcontrol_SHEDC = f_to_c(Tcontrol_SHEDF)
Tcontrol_deadbandC = f_to_c_DB(Tcontrol_deadbandF)
Tcontrol_LOADC = f_to_c(Tcontrol_LOADF)
Tcontrol_LOADdeadbandC = f_to_c_DB(Tcontrol_LOADdeadbandF)
TbaselineC = f_to_c(TbaselineF)
TdeadbandC = f_to_c_DB(TdeadbandF)
TinitC = f_to_c(Tinit)

BASELINE_CTRL = {"Water Heating": {"Setpoint": TbaselineC, "Deadband": TdeadbandC, "Load Fraction": 1}}

#########################################
# OUTPUT COLUMNS (saved per unit; BASE_COLS is also what the baseline
# cache stores)
#########################################

CTRL_COLS = ["Time", "Total Electric Power (kW)",
             "Total Electric Energy (kWh)",
             "Water Heating Electric Power (kW)",
             "Water Heating COP (-)",
             "Water Heating Deadband Upper Limit (C)",
             "Water Heating Deadband Lower Limit (C)",
             "Water Heating Heat Pump COP (-)",
             "Tank Average Temperature (C)",
             "Hot Water Minimum Temperature (C)",
             "Hot Water Average Temperature (C)",
             "Hot Water Maximum Temperature (C)",
             "Hot Water Outlet Temperature (C)",
             "Temperature - Indoor (C)"]
BASE_COLS = [c for c in CTRL_COLS if c != "Tank Average Temperature (C)"]

#########################################
# TANK TEMPERATURE READ
#########################################

def get_tank_temperature_avg_c(hpwh_unit):
    """TRUE volume-weighted average tank temperature -- used ONLY for
    the saved "Tank Average Temperature (C)" output column -- NOT for
    dispatch control decisions (see get_tank_temperature_upper_c())."""
    return hpwh_unit.model.states.dot(hpwh_unit.model.vol_fractions)

def get_tank_temperature_upper_c(hpwh_unit):
    """Upper-node temperature -- the exact quantity OCHRE's own 'HP Duty
    Cycle' safety cutoff checks. Used for ALL MIN_TANK_TEMP_C-threshold
    dispatch decisions (candidate ranking, floor-triggered transition,
    overshoot ranking) -- same methodology as ieee_gen_dec.py."""
    return hpwh_unit.model.states[hpwh_unit.t_upper_idx]

#########################################
# DWELLING ARGS HELPER -- start_time and seed come from the unit's own
# manifest row (its calendar window / day-slice), not the canonical Start.
#########################################

def build_dwelling_args(unit, weather_file_path):
    return {
        "start_time": unit["start_time"],
        "time_res": dt.timedelta(minutes=t_res),
        "duration": dt.timedelta(days=Duration),
        "hpxml_file": os.path.join(unit["home_path"], XML_ADDRESS),
        "hpxml_schedule_file": get_filtered_schedule_file(unit["home_path"]),
        "weather_file": weather_file_path,
        "verbosity": 7,
        # Export results to this unit's OWN scratch folder every hour instead
        # of holding every timestep in memory (~halves per-unit memory). The
        # default output folder is the home's input folder under a fixed
        # name, where units sharing a base home would read back each
        # other's rows. Scratch is deleted once the unit is read back.
        "save_results": True,
        "output_path": unit_scratch_dir(filename, unit["unit_id"]),
        "output_to_parquet": True,
        "export_res": dt.timedelta(hours=1),
        "seed": unit["seed"],
        "Equipment": {
            "Water Heating": {
                "Initial Temperature (C)": TinitC,
                "hp_only_mode": True,
                # NOTE: OCHRE reads this kwarg as 'Max Tank Temperature (C)'
                # (see ochre/Equipment/WaterHeater.py: self.max_temp =
                # kwargs.get('Max Tank Temperature (C)', ...)). The key
                # previously used here, "Max Tank Temperature" (missing
                # the "(C)" suffix), silently never matched, so OCHRE was
                # using its own default (140F = 60C) instead of
                # MAX_TANK_TEMP_C the entire time.
                "Max Tank Temperature (C)": MAX_TANK_TEMP_C,
                "Upper Node": 3,
                "Lower Node": 10,
                "Upper Node Weight": 0.75,
                # Required for 'HP Duty Cycle' control -- see the forked
                # file's own comment for why.
                "ext_time_res": dt.timedelta(minutes=t_res),
            },
        }
    }

#########################################
# BASELINE -- run FIRST, full simulation, every home (parallel)
#########################################

def simulate_home_baseline(unit, weather_file_path):
    """
    Full-simulation baseline run (same Dwelling args/seed as the
    controlled run, baseline control throughout) -- the single source of
    truth for this unit's baseline power, reused for:
      - delta_p(t) during the dispatch loop (requires baseline power at
        EVERY timestep, not just the finalized/trimmed CSV),
      - candidate eligibility ("idle in baseline" check),
      - finalize_and_save_home()'s saved baseline CSV (no second baseline
        Dwelling run there anymore),
      - the pre-dispatch_t sanity assert against the controlled run.

    Returns the full (untrimmed) baseline frame on the canonical clock,
    BASE_COLS only -- exactly what the baseline cache stores.
    """
    dwelling_args_local = build_dwelling_args(unit, weather_file_path)

    with _dwelling_init_lock:
        base_dwelling = Dwelling(name="HPWH Baseline", **dwelling_args_local)

    for _ in base_dwelling.sim_times:
        base_dwelling.update(control_signal=BASELINE_CTRL)

    df_base_full, _, _ = base_dwelling.finalize()
    remove_unit_scratch(filename, unit["unit_id"])
    df_base_full = to_canonical(df_base_full, CANONICAL_TIMES)
    return df_base_full[[c for c in BASE_COLS if c in df_base_full.columns]]


def baseline_result(unit, df_base_full):
    """Per-unit baseline record used by the dispatch loop and finalize:
    the cached/simulated frame plus a {canonical time: WH power kW} lookup."""
    df_base_full = df_base_full.set_index('Time')
    return {
        "unit_id": unit["unit_id"],
        "df_base_full": df_base_full,
        "base_kw_lookup": df_base_full['Water Heating Electric Power (kW)'].to_dict(),
    }

#########################################
# PHASE 1 (CONTROLLED) -- run each home from Start up to dispatch_t,
# baseline control (identical to its own baseline run up to this point)
#########################################

def simulate_home_phase1(unit, weather_file_path):
    """
    Every unit pauses at the SAME global index (dispatch_t on the
    canonical clock) -- the shed-dispatch decision is made globally, in
    real time, by run_shed_dispatch(), which takes over from dispatch_t
    onward.
    """
    dwelling_args_local = build_dwelling_args(unit, weather_file_path)

    with _dwelling_init_lock:
        sim_dwelling = Dwelling(name="HPWH Controlled", **dwelling_args_local)
    hpwh_unit = sim_dwelling.get_equipment_by_end_use('Water Heating')

    # Step i of this unit's own calendar window == canonical step i.
    assert len(sim_dwelling.sim_times) == len(CANONICAL_TIMES)
    sim_times = CANONICAL_TIMES
    resume_idx = None
    tank_temp_log = []

    for i, sim_time in enumerate(sim_times):
        sim_dwelling.update(control_signal=BASELINE_CTRL)
        tank_temp_log.append((sim_time, get_tank_temperature_avg_c(hpwh_unit)))

        if sim_time >= dispatch_t and resume_idx is None:
            resume_idx = i + 1
            break  # pause here; the shed-dispatch loop resumes from resume_idx

    return {
        "unit_id": unit["unit_id"],
        "home_path": unit["home_path"],
        "sim_dwelling": sim_dwelling,
        "sim_times": sim_times,
        "tank_temp_log": tank_temp_log,
        "resume_idx": resume_idx,
    }

#########################################
# DISPATCH LOOP -- mirror image of ieee_gen_dec.py's run_delta_dispatch().
# Works in REDUCTION-magnitude space (reduction(t) = -delta_p(t)).
#########################################

def run_shed_dispatch(phase1_results, base_kw_lookup_by_unit):
    sim_times = phase1_results[0]["sim_times"]
    resume_idx = phase1_results[0]["resume_idx"]

    hpwh_unit_by_key = {id(r): r["sim_dwelling"].get_equipment_by_end_use('Water Heating') for r in phase1_results}
    base_kw_by_key = {id(r): base_kw_lookup_by_unit[r["unit_id"]] for r in phase1_results}
    current_avg_temp_by_key = {id(r): r["tank_temp_log"][-1][1] for r in phase1_results}
    current_ctrl_temp_by_key = {id(r): get_tank_temperature_upper_c(hpwh_unit_by_key[id(r)]) for r in phase1_results}

    # "shed" is the RESERVE ACTION here (mirrors ieee_gen_dec.py's
    # "load"); "recovery" is the limit-breach parking state (mirrors
    # ieee_gen_dec.py's "shed").
    state = {id(r): "idle" for r in phase1_results}
    ever_dispatched = {id(r): False for r in phase1_results}
    dispatch_idx_by_key = {}
    dispatch_time_by_key = {id(r): None for r in phase1_results}
    release_time_by_key = {id(r): None for r in phase1_results}
    release_reason_by_key = {id(r): None for r in phase1_results}
    admission_order_by_key = {id(r): None for r in phase1_results}

    shed_ctrl = {"Water Heating": {"Setpoint": Tcontrol_SHEDC, "Deadband": Tcontrol_deadbandC, "Load Fraction": 1}}
    recovery_ctrl = {"Water Heating": {"HP Duty Cycle": HP_DUTY_CYCLE_RECOVERY, "Load Fraction": 1}}
    baseline_ctrl = {"Water Heating": {"Setpoint": TbaselineC, "Deadband": TdeadbandC, "Load Fraction": 1}}

    event_end_idx = next((i for i, t in enumerate(sim_times) if t >= event_end), len(sim_times))

    log_rows = []
    next_admission_order = 0
    release_idx_by_key = {}
    release_schedule_set = False

    ramp_complete = False
    next_ramp_admit_idx = resume_idx

    first_in_band_time = None

    # reduction(t) as of the END of the PREVIOUS timestep (-delta_p_prev;
    # positive while shedding effectively). Zero before anything has
    # ever been dispatched.
    reduction_prev = 0.0

    for i in range(resume_idx, len(sim_times)):
        sim_time = sim_times[i]
        in_event = sim_time < event_end

        # ---- Phase A: thermal state transitions, based on tank temp as
        # of the END of the PREVIOUS step. A "shed" unit that has cooled
        # to the floor moves to "recovery" (during the event, so it can
        # rewarm and be recycled) or straight to "idle" (after the
        # event). ----
        for r in phase1_results:
            key = id(r)
            if state[key] == "shed" and current_ctrl_temp_by_key[key] <= MIN_TANK_TEMP_C:
                if in_event:
                    state[key] = "recovery"
                else:
                    state[key] = "idle"
                    release_time_by_key[key] = sim_time
                    release_reason_by_key[key] = "floor_reached"

        # ---- At event_end, fix a release schedule ONCE for every unit
        # still in "shed" or "recovery" -- identical mechanism to
        # ieee_gen_dec.py. ----
        if not release_schedule_set and i >= event_end_idx:
            still_out = [id(r) for r in phase1_results if state[id(r)] in ("shed", "recovery")]
            still_out.sort(key=lambda key: admission_order_by_key[key])
            for idx, key in enumerate(still_out):
                group_idx = idx // RELEASE_GROUP_SIZE
                delay_min = group_idx * RELEASE_GROUP_SPACING_MIN
                release_idx_by_key[key] = event_end_idx + round(delay_min / t_res)
            release_schedule_set = True

        # ---- Candidates: never-shed homes ACTIVELY DRAWING POWER in
        # their own baseline RIGHT NOW (fresh), PLUS currently-
        # "recovery" homes (recycled) -- ranked together, WARMEST-first.
        # No separate thermal-headroom eligibility filter -- mirror of
        # ieee_gen_dec.py's own reasoning. ----
        fresh_candidates = [
            r for r in phase1_results
            if not ever_dispatched[id(r)]
            and base_kw_by_key[id(r)].get(sim_time, 0.0) > BASELINE_ACTIVE_THRESHOLD_KW
        ]
        recovery_candidates = [
            r for r in phase1_results
            if state[id(r)] == "recovery"
        ]
        candidates = fresh_candidates + recovery_candidates
        n_candidates_available = len(candidates)

        active_keys_before = [id(r) for r in phase1_results if state[id(r)] == "shed"]
        pending_count = sum(
            1 for key in active_keys_before
            if (i - dispatch_idx_by_key.get(key, -10**9)) < RAMP_STEPS
        )
        pending_kw = pending_count * EXPECTED_KW_PER_UNIT

        # ---- Ramp-down complete? Checked first, using the PREVIOUS
        # timestep's known-true reduction -- once true, stays true for
        # the rest of the event. ----
        if in_event and not ramp_complete and reduction_prev >= TARGET_REDUCTION_KW - BAND_KW:
            ramp_complete = True

        if in_event and not ramp_complete:
            # ---- Initial ramp-down: every RAMP_BATCH_SPACING_MIN,
            # warmest-first -- sized ONLY by the shortfall-implied count
            # and how many candidates are actually available right now
            # (no flat batch-size cap -- see module docstring). ----
            if i >= next_ramp_admit_idx and n_candidates_available > 0:
                shortfall = TARGET_REDUCTION_KW - reduction_prev - pending_kw
                shortfall_implied = max(1, math.ceil(shortfall / EXPECTED_KW_PER_UNIT))
                n_admit = min(shortfall_implied, n_candidates_available)
                candidates.sort(key=lambda r: current_ctrl_temp_by_key[id(r)], reverse=True)
                for candidate in candidates[:n_admit]:
                    key = id(candidate)
                    if not ever_dispatched[key]:
                        ever_dispatched[key] = True
                        dispatch_time_by_key[key] = sim_time
                        admission_order_by_key[key] = next_admission_order
                        next_admission_order += 1
                    state[key] = "shed"
                    dispatch_idx_by_key[key] = i
                next_ramp_admit_idx = i + max(1, round(RAMP_BATCH_SPACING_MIN / t_res))

        # ---- Post-ramp: normal shortfall-sized admission. ----
        elif in_event and reduction_prev < TARGET_REDUCTION_KW - BAND_KW:
            shortfall = TARGET_REDUCTION_KW - reduction_prev - pending_kw
            n_admit = max(0, math.ceil(shortfall / EXPECTED_KW_PER_UNIT))
            n_admit = min(n_admit, n_candidates_available, MAX_ADMISSIONS_SAFETY_CAP)

            if n_admit > 0:
                candidates.sort(key=lambda r: current_ctrl_temp_by_key[id(r)], reverse=True)
                for candidate in candidates[:n_admit]:
                    key = id(candidate)
                    if not ever_dispatched[key]:
                        ever_dispatched[key] = True
                        dispatch_time_by_key[key] = sim_time
                        admission_order_by_key[key] = next_admission_order
                        next_admission_order += 1
                    state[key] = "shed"
                    dispatch_idx_by_key[key] = i

        # ---- Post-ramp: optional overshoot correction -- move the
        # COLDEST "shed" units to "recovery" early if reduction is over
        # the top of the band (mirror of ieee_gen_dec.py's hottest-load
        # overshoot release). ----
        elif in_event and ALLOW_OVERSHOOT_RELEASE and reduction_prev > TARGET_REDUCTION_KW + BAND_KW:
            overshoot = reduction_prev - (TARGET_REDUCTION_KW + BAND_KW)
            n_release = max(1, math.ceil(overshoot / EXPECTED_KW_PER_UNIT))
            releasable = sorted(active_keys_before, key=lambda key: current_ctrl_temp_by_key[key], reverse=False)
            for key in releasable[:n_release]:
                state[key] = "recovery"

        # ---- Step every home forward one timestep, per its CURRENT state ----
        for r in phase1_results:
            key = id(r)

            if state[key] in ("shed", "recovery") and key in release_idx_by_key and i >= release_idx_by_key[key]:
                state[key] = "idle"
                release_time_by_key[key] = sim_time
                release_reason_by_key[key] = "event_end_gradual"

            if state[key] == "shed":
                control_cmd = shed_ctrl
            elif state[key] == "recovery":
                control_cmd = recovery_ctrl
            else:
                control_cmd = baseline_ctrl

            r["sim_dwelling"].update(control_signal=control_cmd)
            fresh_temp = get_tank_temperature_avg_c(hpwh_unit_by_key[key])
            r["tank_temp_log"].append((sim_time, fresh_temp))
            current_avg_temp_by_key[key] = fresh_temp
            current_ctrl_temp_by_key[key] = get_tank_temperature_upper_c(hpwh_unit_by_key[key])

        # ---- reduction(t) AFTER stepping, at THIS sim_time -- the
        # control variable, over ALL ever-dispatched units. ----
        ever_dispatched_keys = [id(r) for r in phase1_results if ever_dispatched[id(r)]]
        active_keys_after = [id(r) for r in phase1_results if state[id(r)] == "shed"]
        recovery_keys_after = [id(r) for r in phase1_results if state[id(r)] == "recovery"]
        delta_p = sum(
            hpwh_unit_by_key[key].electric_kw - base_kw_by_key[key].get(sim_time, 0.0)
            for key in ever_dispatched_keys
        )
        reduction = -delta_p
        dispatched_power = sum(hpwh_unit_by_key[key].electric_kw for key in ever_dispatched_keys)
        total_fleet_power = sum(u.electric_kw for u in hpwh_unit_by_key.values())

        if in_event and first_in_band_time is None and reduction >= TARGET_REDUCTION_KW - BAND_KW:
            first_in_band_time = sim_time

        log_rows.append({
            "Time": sim_time,
            "DeltaP_kW": delta_p,
            "Reduction_kW": reduction,
            "Dispatched_Count": len(ever_dispatched_keys),
            "Active_Count": len(active_keys_after),
            "Recovery_Count": len(recovery_keys_after),
            "Pending_Count": pending_count,
            "Candidates_Available": n_candidates_available,
            "Dispatched_WH_Power_kW": dispatched_power,
            "Total_Fleet_WH_Power_kW": total_fleet_power,
        })

        reduction_prev = reduction

    never_dispatched = [r["unit_id"] for r in phase1_results if not ever_dispatched[id(r)]]
    if never_dispatched:
        print(f"NOTE: {len(never_dispatched)} units never dispatched within the event window "
              f"(target reached/not enough eligible candidates left).")

    for r in phase1_results:
        key = id(r)
        r["participating"] = ever_dispatched[key]
        r["admission_order"] = admission_order_by_key[key]
        r["dispatch_time"] = dispatch_time_by_key[key]
        r["release_time"] = release_time_by_key[key]
        r["release_reason"] = release_reason_by_key[key]

    return phase1_results, log_rows, first_in_band_time

#########################################
# FINALIZE -- per unit, parallel again. Baseline is NOT re-run here --
# reuses the cached/simulated baseline. Returns the unit's trimmed frames
# (tagged with its unit ID in the "Home" column); main() aggregates them
# in memory, so nothing is written into Input_Files.
#########################################

def finalize_and_save_home(phase1_result, baseline_result):
    unit_id = phase1_result["unit_id"]
    sim_dwelling = phase1_result["sim_dwelling"]
    tank_temp_log = phase1_result["tank_temp_log"]

    df_ctrl, _, _ = sim_dwelling.finalize()
    remove_unit_scratch(filename, unit_id)
    df_ctrl = to_canonical(df_ctrl, CANONICAL_TIMES)

    tank_temp_df = pd.DataFrame(tank_temp_log, columns=["Time", "Tank Average Temperature (C)"])
    tank_temp_df["Time"] = pd.to_datetime(tank_temp_df["Time"])

    df_base_full = baseline_result["df_base_full"]

    df_ctrl = remove_first_day(df_ctrl, Start)
    df_base = remove_first_day(df_base_full.reset_index(), Start)

    df_ctrl = df_ctrl.merge(tank_temp_df, on="Time", how="left")

    # ---- Sanity assert: before dispatch_t, controlled power must equal
    # baseline power (same seed, same control up to that point). ----
    pre_dispatch_ctrl = df_ctrl[df_ctrl['Time'] < dispatch_t].set_index('Time')['Water Heating Electric Power (kW)']
    pre_dispatch_base = df_base.set_index('Time')['Water Heating Electric Power (kW)']
    pre_dispatch_base = pre_dispatch_base.reindex(pre_dispatch_ctrl.index)
    diff = (pre_dispatch_ctrl - pre_dispatch_base).abs()
    max_diff = diff.max() if len(diff) else 0.0
    violation = None
    if len(diff) and max_diff > 1e-6:
        violation = (
            f"{unit_id}: controlled and baseline power diverge BEFORE "
            f"dispatch_t (max |diff| = {max_diff:.6f} kW at "
            f"{diff.idxmax()}) -- same seed/control should make these identical "
            f"(if the baseline came from the cache, it may be stale -- rerun with "
            f"OCHRE_REBUILD_BASELINE=1)."
        )

    df_ctrl = df_ctrl[[c for c in CTRL_COLS if c in df_ctrl.columns]].copy()
    df_base = df_base[[c for c in BASE_COLS if c in df_base.columns]].copy()
    df_ctrl["Home"] = unit_id
    df_base["Home"] = unit_id

    return df_ctrl, df_base, violation

#########################################
# DELETE FIRST DAY ONLY
#########################################

def remove_first_day(df, start_date):
    if 'Time' not in df.columns:
        df = df.reset_index()
        if 'index' in df.columns:
            df.rename(columns={'index': 'Time'}, inplace=True)

    df['Time'] = pd.to_datetime(df['Time'], errors='coerce')
    first_day_end = start_date + pd.Timedelta(days=1)
    return df[df['Time'] >= first_day_end].copy()

#########################################
# AGGREGATE RESULTS
#########################################

def aggregate_results(all_ctrl, all_base, work_dir):
    """Writes the per-unit frames returned by finalize_and_save_home() as
    the two aggregated CSVs the results script reads."""
    if all_ctrl:
        df_ctrl_all = pd.concat(all_ctrl, ignore_index=True)
        df_ctrl_all.to_csv(os.path.join(work_dir, filename + "_controlled.csv"), index=False)

    if all_base:
        df_base_all = pd.concat(all_base, ignore_index=True)
        df_base_all.to_csv(os.path.join(work_dir, filename + "_baseline.csv"), index=False)

    print(f"Aggregated CSVs written for {len(all_ctrl)} units.")

#########################################
# MAIN EXECUTION
#########################################

if __name__ == "__main__":
    os.makedirs(RESULTS_DIR, exist_ok=True)
    if not os.path.exists(WEATHER_FILE):
        raise FileNotFoundError(f"{WEATHER_FILE} not found -- run ieee_pes_prepare_inputs.py first.")

    fleet = select_fleet(FLEET_SIZE)
    homes = sorted({u["home_path"] for u in fleet})
    print(f"Fleet: {len(fleet)} virtual units from {len(homes)} base homes "
          f"(first {FLEET_SIZE} rows of Input_Files/virtual_units.csv)")

    # Filter each base home's schedule ONCE, before any worker threads
    # start, so units sharing a home never race on writing that file.
    for home in homes:
        get_filtered_schedule_file(home)

    print(f"Target: reduce fleet load by {TARGET_REDUCTION_KW}kW +/- {BAND_KW}kW vs. baseline (reduction), "
          f"EXPECTED_KW_PER_UNIT={EXPECTED_KW_PER_UNIT}, RAMP_STEPS={RAMP_STEPS}")
    print(f"Dispatch window: {dispatch_t} to {event_end} ({reserve_event['duration']}hr)")
    print(f"Ramp-down: shortfall-sized batches every {RAMP_BATCH_SPACING_MIN}min (no flat cap) "
          f"until target first reached.")
    print(f"Post-event: groups of {RELEASE_GROUP_SIZE} released to idle every "
          f"{RELEASE_GROUP_SPACING_MIN}min (no reactivation, no mass release).")
    print(f"MIN_TANK_TEMP_C={MIN_TANK_TEMP_C}, ALLOW_OVERSHOOT_RELEASE={ALLOW_OVERSHOOT_RELEASE}")

    # ---- Baseline FIRST, full simulation, every unit -- reused from
    # Baseline_Cache/ wherever this unit already has one (written by any
    # PRIOR DEC or INC test: both use identical baseline control, so the
    # cache is shared). Baseline is target/duration-independent (same
    # seed, same uncontrolled Setpoint/Deadband control throughout) --
    # confirmed numerically stable run-to-run (differences only at
    # floating-point noise level, ~1e-10 relative). The cache folder is
    # keyed by a hash of every baseline-affecting setting below, so
    # changing one starts a fresh cache instead of reusing a stale one.
    # Units without a cache entry (or all of them, with
    # OCHRE_REBUILD_BASELINE=1) are simulated now and added to it. ----
    dwelling_settings = {k: v for k, v in build_dwelling_args(fleet[0], WEATHER_FILE).items()
                         if k not in ("start_time", "hpxml_file", "hpxml_schedule_file", "seed", "weather_file",
                                      "verbosity", "save_results", "output_path", "output_to_parquet", "export_res")}
    cache_dir = baseline_cache_dir({
        "dwelling_args": dwelling_settings,
        "weather_file": os.path.basename(WEATHER_FILE),
        "baseline_ctrl": BASELINE_CTRL,
        "canonical_start": Start,
        "day_slices": {"base_start": DAY_SLICE_BASE_START, "slice_days": SLICE_DAYS},
    })

    baseline_results_by_unit = {}
    units_to_simulate = fleet
    if not REBUILD_BASELINE:
        units_to_simulate = []
        for unit in fleet:
            df_cached = load_cached_baseline(cache_dir, unit["unit_id"])
            if df_cached is None:
                units_to_simulate.append(unit)
            else:
                baseline_results_by_unit[unit["unit_id"]] = baseline_result(unit, df_cached)
    print(f"Baseline cache ({cache_dir}): reused "
          f"{len(baseline_results_by_unit)}/{len(fleet)} units, simulating {len(units_to_simulate)}.")

    if units_to_simulate:
        with concurrent.futures.ThreadPoolExecutor(max_workers=8) as executor:
            futures = {
                executor.submit(simulate_home_baseline, unit, WEATHER_FILE): unit
                for unit in units_to_simulate
            }
            for f in concurrent.futures.as_completed(futures):
                unit = futures[f]
                try:
                    df_base_full = f.result()
                    save_cached_baseline(cache_dir, unit["unit_id"], df_base_full)
                    baseline_results_by_unit[unit["unit_id"]] = baseline_result(unit, df_base_full)
                except Exception as e:
                    print(f"Baseline simulation failed for {unit['unit_id']}: {e}")

    print(f"Baseline ready for {len(baseline_results_by_unit)} units. Starting controlled Phase 1...")

    base_kw_lookup_by_unit = {u: r["base_kw_lookup"] for u, r in baseline_results_by_unit.items()}

    # ---- Phase 1 (controlled): every unit with a baseline runs
    # baseline-only from Start to dispatch_t (same global pause index for
    # every unit) ----
    phase1_results = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as executor:
        futures = {
            executor.submit(simulate_home_phase1, unit, WEATHER_FILE): unit["unit_id"]
            for unit in fleet if unit["unit_id"] in baseline_results_by_unit
        }

        for f in concurrent.futures.as_completed(futures):
            try:
                result = f.result()
                result["weather_file_path"] = WEATHER_FILE
                phase1_results.append(result)
            except Exception as e:
                print(f"Phase 1 simulation failed for {futures[f]}: {e}")

    print(f"Phase 1 complete for {len(phase1_results)} units. Starting shed dispatch...")

    phase1_results, log_rows, first_in_band_time = run_shed_dispatch(phase1_results, base_kw_lookup_by_unit)

    n_dispatched = sum(1 for r in phase1_results if r["participating"])
    print(f"Shed dispatch complete: {n_dispatched}/{len(phase1_results)} units dispatched. "
          f"Finalizing units...")

    # ---- Finalize + sanity-check each unit (parallel). A violation
    # stops the run before any aggregated output is produced. ----
    violations = []
    frames_by_unit = {}
    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as executor:
        futures = {
            executor.submit(finalize_and_save_home, r, baseline_results_by_unit[r["unit_id"]]): r["unit_id"]
            for r in phase1_results
        }
        for f in concurrent.futures.as_completed(futures):
            try:
                df_ctrl_unit, df_base_unit, violation = f.result()
                frames_by_unit[futures[f]] = (df_ctrl_unit, df_base_unit)
                if violation:
                    violations.append(violation)
            except Exception as e:
                print(f"Finalize/save failed for {futures[f]}: {e}")

    if violations:
        print("=" * 60)
        print("SANITY CHECK FAILED -- controlled/baseline power diverge before dispatch_t:")
        for v in violations:
            print(f"  {v}")
        print("=" * 60)
        raise RuntimeError(
            f"{len(violations)} unit(s) failed the pre-dispatch sanity check -- "
            f"see printed report above. Stopping before aggregated output is written."
        )

    print("Sanity check passed: controlled == baseline for every unit before dispatch_t.")

    # ---- Persist the per-home assignment / outcome summary ----
    release_summary = pd.DataFrame([
        {
            "Home": r["unit_id"],
            "participating": r["participating"],
            "admission_order": r["admission_order"],
            "dispatch_time": r["dispatch_time"],
            "release_time": r["release_time"],
            "release_reason": r["release_reason"],
        }
        for r in phase1_results
    ])
    release_summary.to_csv(os.path.join(RESULTS_DIR, filename + "_release_summary.csv"), index=False)
    print(f"Release summary written for {len(release_summary)} units")

    # ---- Per-timestep log CSV ----
    log_df = pd.DataFrame(log_rows)
    log_csv = os.path.join(RESULTS_DIR, filename + "_deltaP_log.csv")
    log_df.to_csv(log_csv, index=False)
    print(f"Delta-P log written ({len(log_df)} timesteps): {log_csv}")

    # ---- Summary stats over the event window ----
    event_log = log_df[(log_df['Time'] >= dispatch_t) & (log_df['Time'] < event_end)]
    mean_red = event_log['Reduction_kW'].mean() if not event_log.empty else float('nan')
    min_red = event_log['Reduction_kW'].min() if not event_log.empty else float('nan')
    max_red = event_log['Reduction_kW'].max() if not event_log.empty else float('nan')
    mean_sum_p = event_log['Total_Fleet_WH_Power_kW'].mean() if not event_log.empty else float('nan')

    if first_in_band_time is not None:
        time_to_target_min = (first_in_band_time - dispatch_t).total_seconds() / 60
    else:
        time_to_target_min = None

    hold_log = event_log[event_log['Time'] >= first_in_band_time] if first_in_band_time is not None else event_log.iloc[0:0]
    in_band = hold_log['Reduction_kW'].between(TARGET_REDUCTION_KW - BAND_KW, TARGET_REDUCTION_KW + BAND_KW)
    pct_in_band = (in_band.mean() * 100) if not hold_log.empty else float('nan')

    n_never_dispatched = len(phase1_results) - n_dispatched

    print("=" * 60)
    print("SHED DISPATCH SUMMARY")
    print("=" * 60)
    print(f"Target reduction: {TARGET_REDUCTION_KW} kW, Band: +/-{BAND_KW} kW")
    print(f"Mean reduction (event):  {mean_red:.3f} kW")
    print(f"Min reduction (event):   {min_red:.3f} kW")
    print(f"Max reduction (event):   {max_red:.3f} kW")
    print(f"Mean sum_p (event, diagnostic only): {mean_sum_p:.3f} kW")
    print(f"%% of hold-window timesteps in-band (from first_in_band_time): {pct_in_band:.1f}%")
    if time_to_target_min is not None:
        print(f"Time to first reach target - band: {time_to_target_min:.1f} min after dispatch")
    else:
        print("Target - band was NEVER reached during the event.")
    print(f"Units dispatched: {n_dispatched} / {len(phase1_results)}")
    print(f"Units never dispatched: {n_never_dispatched}")

    if not event_log.empty and not in_band.all():
        first_out_idx = (~in_band).idxmax()
        first_out_row = event_log.loc[first_out_idx]
        cause = "no eligible candidates remaining" if first_out_row['Candidates_Available'] == 0 else \
                "overshoot" if first_out_row['Reduction_kW'] > TARGET_REDUCTION_KW + BAND_KW else \
                "shortfall not yet closed"
        print(f"First out-of-band excursion at {first_out_row['Time']}: "
              f"reduction={first_out_row['Reduction_kW']:.3f} kW, candidates_available="
              f"{first_out_row['Candidates_Available']}, likely cause: {cause}")
    print("=" * 60)

    print("All simulations complete!")

    aggregate_results([frames_by_unit[u["unit_id"]][0] for u in fleet if u["unit_id"] in frames_by_unit],
                      [frames_by_unit[u["unit_id"]][1] for u in fleet if u["unit_id"] in frames_by_unit],
                      RESULTS_DIR)
    shutil.rmtree(os.path.join(SCRATCH_DIR, filename), ignore_errors=True)
