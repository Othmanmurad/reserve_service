# -*- coding: utf-8 -*-
"""
Forked from HPWH_B3_Reserve_V2.py (itself a clone of HPWH_B4_Reserve_RU.py)
for the IEEE generation-decrement (DEC / downward-reserve, i.e. LOAD/
ramp-up) dispatch study -- see the project's own INC/DEC terminology
discussion: DEC = the fleet INCREASES load because the grid has a
generation SURPLUS to soak up, which is what the forced LOAD/duty-cycle
mechanism below does (same physics as HPWH_B4_Reserve_RU.py, unchanged).

Deliberately a SEPARATE file, not a modification of HPWH_B3_Reserve_V2.py,
HPWH_B4_Reserve_RU.py, HPWH_C1_parse_RU.py, or HPWH_C3_RU.py -- those are
still needed for the Excel calculator pipeline and are not touched here.

DISPATCH MECHANISM (delta-P capacity cap, with SHED-based recycling): the
control target is the fleet's ADDED load, delta_p(t) = sum(controlled kW)
- sum(baseline kW), not raw total power -- total power is dominated by
the never-dispatched homes' own normal hot-water-driven usage, which
swings by tens of kW across the day for reasons that have nothing to do
with dispatch (still computed/logged as sum_p(t), for reference only).

Each unit is in exactly one of three states: "idle" (baseline Setpoint/
Deadband control -- never dispatched, or fully retired), "load" (forced
HP Duty Cycle=1.0, actively adding load), or "shed" (an explicit LOW
Setpoint/Deadband control, Tcontrol_SHEDC/Tcontrol_deadbandC -- a unit
that has topped out under LOAD, parked in a controlled low-draw state
instead of being handed back to plain baseline). A unit in "shed" cools
over time and, once it regains tank headroom, becomes eligible to be
RE-ADMITTED into "load" again -- so the same unit can cycle load -> shed
-> load -> shed... multiple times across a long event, instead of being a
one-shot contributor. This is what lets the fleet hold delta_p(t) at
target for the FULL event duration: the earlier one-shot version
exhausted its pool of never-dispatched candidates partway through a long
event (confirmed: delta_p decayed after ~2hr in a 4hr/100kW test); with
recycling, already-dispatched units become reusable candidates again
once they've cooled, instead of sitting permanently retired. Mechanics:

  1. Baseline runs FIRST, for every home, for the full simulation --
     stored as a per-home time-indexed power lookup, reused both for
     delta_p(t) and (in finalize_and_save_home()) instead of re-running a
     second baseline there.
  2. Initial ramp-up (dispatch_t until delta_p(t) first reaches target):
     up to RAMP_BATCH_SIZE units admitted into "load" every
     RAMP_BATCH_SPACING_MIN minutes, coldest-first -- a deliberately
     paced climb instead of one large instantaneous (or capped-but-still-
     large) batch, which was found to overshoot and then oscillate as
     the whole batch got shed and recycled together. Each batch is also
     capped at the shortfall-implied count, so it TAPERS down as delta_p
     approaches target instead of always taking a full fixed-size step
     that can overshoot right at the end.
  3. Post-ramp, every timestep until event_end:
     a. Any "load" unit whose tank has reached MAX_TANK_TEMP_C moves to
        "shed" (not idle) -- this is checked directly every timestep,
        instead of relying on OCHRE's own silent internal Max Tank
        Temperature cutoff, so the controller always knows a unit has
        stopped contributing the instant it happens.
     b. If delta_p(t) is below TARGET_DELTA_KW - BAND_KW, admit enough
        NEW units into "load" to close the shortfall (sized from the
        shortfall itself, accounting for units still ramping up from a
        recent admission), ranked coldest-average-temperature-first,
        from the COMBINED pool of: never-dispatched homes CURRENTLY IDLE
        in their own baseline trace (so the LOAD command actually adds
        load), PLUS currently-"shed" homes. No separate thermal-headroom
        eligibility filter -- coldest-first ranking alone keeps a
        near-ceiling candidate as a last resort, picked only once every
        genuinely-cold candidate is already used.
     c. If delta_p(t) overshoots TARGET_DELTA_KW + BAND_KW, the hottest
        "load" units move to "shed" early (optional,
        ALLOW_OVERSHOOT_RELEASE) -- NOT to idle, so they remain
        recyclable instead of being permanently retired.
  4. At event_end: NO reactivation of shed units into load (that produced
     a sharp post-event power spike; removed). Instead, every unit still
     in "load" or "shed" is released straight to "idle" in groups of
     RELEASE_GROUP_SIZE, spaced RELEASE_GROUP_SPACING_MIN apart -- the
     fleet winds down gradually instead of all at once or at full power.

A sanity assert checks, per home, that controlled power exactly equals
baseline power before dispatch_t (same seed, same control up to that
point) -- if any home diverges, the run stops and reports it rather than
silently producing misleading delta_p(t) downstream.
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
import random
from run_context import save_filename, save_reserve_event, save_input_folder
from ieee_pes_common import (
    Input_folder, WEATHER_FILE, XML_ADDRESS, select_fleet, get_filtered_schedule_file,
    make_canonical_times, to_canonical, baseline_cache_dir, load_cached_baseline,
    save_cached_baseline, DAY_SLICE_BASE_START, SLICE_DAYS, DEC_RESULTS_ROOT,
    unit_scratch_dir, remove_unit_scratch, SCRATCH_DIR,
)

# OCHRE seeds the *global* NumPy RNG inside Dwelling.__init__ (see
# ochre.Simulator.__init__ -> np.random.seed(seed)) and then immediately
# draws from it while generating stochastic equipment schedules. Since
# homes run concurrently across ThreadPoolExecutor worker threads, two
# Dwelling() constructions overlapping in time would reset/consume that
# shared global state out of order, corrupting each other's draws and
# defeating the point of a per-home seed. Serializing construction (only,
# not the much more expensive per-timestep simulation loop that follows)
# avoids that race.
_dwelling_init_lock = threading.Lock()

#########################################
# USER SETTINGS
#########################################

# Distinct name per test run -- change before each run so results don't
# overwrite each other. OCHRE_FILENAME overrides this when set.
filename = os.environ.get('OCHRE_FILENAME', 'IEEE_DEC_Testing')

# pipeline="IEEE_DEC" -- distinct from "RU"/"RU_V2" so this experimental
# file's last-run state never collides with HPWH_B4_Reserve_RU.py's
# (official/Excel) or HPWH_B3_Reserve_V2.py's own state. Read back by
# ieee_dec_results.py.
save_filename(filename, pipeline="IEEE_DEC")

# Number of virtual units in this test's fleet -- the FIRST FLEET_SIZE
# rows of Input_Files/virtual_units.csv (see ieee_pes_common.py: any size
# up to the manifest's 10000 works, and a smaller fleet is always a subset
# of a larger one). OCHRE_FLEET_SIZE overrides this when set.
FLEET_SIZE = int(os.environ.get('OCHRE_FLEET_SIZE', 1000))

# Inputs (base homes, weather, manifest) come from this project's own
# Input_Files/ -- see ieee_pes_common.py.
save_input_folder(Input_folder, pipeline="IEEE_DEC")

# Per-test output folder on the shared drive (DEC_RESULTS_ROOT in
# ieee_pes_common.py) -- keeps each run's aggregated CSVs alongside the
# analysis outputs they feed.
RESULTS_DIR = os.path.join(DEC_RESULTS_ROOT, filename)

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

# HPWH control parameters (°F)
Tcontrol_SHEDF = 126
Tcontrol_deadbandF = 10
Tcontrol_LOADF = 130
Tcontrol_LOADdeadbandF = 2
TbaselineF = 130
TdeadbandF = 7
Tinit = 128

#########################################
# RESERVE EVENT SETTINGS
#########################################

# DEC = LOAD (increase consumption) -- see module docstring. This file is
# single-purpose (DEC/LOAD only) -- the dispatch loop always commands LOAD.

# OCHRE_DISPATCH_TIME / OCHRE_DURATION_HR override these when set.
if 'reserve_event' not in globals():
    reserve_event = {
        'dispatch_time': os.environ.get('OCHRE_DISPATCH_TIME', '14:00'),
        'duration': float(os.environ.get('OCHRE_DURATION_HR', 1.0))
    }

save_reserve_event(reserve_event, pipeline="IEEE_DEC")

# The single calendar day the reserve event happens on -- the LAST day of
# the simulation (see the file this was forked from for the full
# rationale).
EVENT_DATE = (Start + dt.timedelta(days=Duration - 1)).date()

# Single SHARED event clock -- every home dispatches/releases against the
# SAME dispatch_t/event_end. The delta-P dispatch loop is what actually
# staggers individual units WITHIN this shared window.
dispatch_t = pd.to_datetime(f"{EVENT_DATE} {reserve_event['dispatch_time']}")
event_end = dispatch_t + pd.Timedelta(hours=reserve_event['duration'])

#########################################
# DISPATCH SETTINGS
#########################################

# Target ADDED load (controlled - baseline), delta_p(t), in kW, and the
# tolerance band around it. Admission triggers when delta_p < TARGET_DELTA_KW
# - BAND_KW; optional overshoot release triggers when delta_p > TARGET_DELTA_KW
# + BAND_KW (see ALLOW_OVERSHOOT_RELEASE). This represents the fleet's
# actual reserve contribution relative to baseline -- NOT raw total power,
# which also includes the never-dispatched homes' own normal hot-water
# usage (computed/logged alongside as sum_p(t), for reference only).
TARGET_DELTA_KW = float(os.environ.get('OCHRE_TARGET_DELTA_KW', 120.0))
BAND_KW = float(os.environ.get('OCHRE_BAND_KW', 0.5))

# Persist alongside dispatch_time/duration so ieee_dec_results.py's plots
# (which need the same target/band for their reference line/shading) pick
# up this run's actual values automatically instead of needing a second,
# easy-to-forget copy of the same env vars passed to the results script.
reserve_event['target_delta_kw'] = TARGET_DELTA_KW
reserve_event['band_kw'] = BAND_KW
save_reserve_event(reserve_event, pipeline="IEEE_DEC")

# ALSO written per-folder, inside RESULTS_DIR itself -- the save_reserve_event()
# file above is a single shared slot per pipeline, so it only ever reflects
# whichever run happened MOST RECENTLY. Re-running ieee_dec_results.py later
# against an OLDER test folder (e.g. a parameter sweep across several
# differently-named runs) would silently pick up the wrong event window/
# target from that shared slot. This per-folder copy is read first.
os.makedirs(RESULTS_DIR, exist_ok=True)
with open(os.path.join(RESULTS_DIR, filename + "_run_config.json"), "w") as f:
    json.dump(reserve_event, f, indent=2)

# A candidate's own baseline power at the CURRENT timestep must be at or
# below this to count as "idle in baseline" (eligible) -- same convention
# as ONLINE_THRESHOLD_KW elsewhere in this project for the idle-vs-active
# distinction.
BASELINE_IDLE_THRESHOLD_KW = 0.1

# Units admitted within the last RAMP_STEPS timesteps are treated as
# "pending" (not yet fully reflected in delta_p(t), since their draw
# ramps up over a few timesteps) and their estimated contribution is
# subtracted from the shortfall before sizing the next admission batch --
# avoids repeatedly over-ordering admissions while recent ones are still
# ramping. Defaults to ~3 minutes' worth of timesteps.
RAMP_STEPS = max(1, round(3 / t_res))
EXPECTED_KW_PER_UNIT = 0.45

# Optional hard cap on a single timestep's admission batch size, purely as
# a safety limit against a pathological burst during the STEADY-STATE
# top-up phase (post-ramp) -- the actual batch size there is normally
# sized from the shortfall itself (see run_delta_dispatch()). The initial
# climb to target no longer uses this cap at all -- see RAMP_BATCH_SIZE/
# RAMP_BATCH_SPACING_MIN below (a flat MAX_ADMISSIONS_SAFETY_CAP=240 for
# the first admission caused a large synchronized batch that overshot,
# then oscillated as it was shed and recycled together).
MAX_ADMISSIONS_SAFETY_CAP = 20

# Initial ramp-up: admit up to RAMP_BATCH_SIZE units every
# RAMP_BATCH_SPACING_MIN minutes (coldest-first, same candidate pool) --
# a deliberately paced climb instead of one large instantaneous or
# capped-but-still-large batch. Each batch is ALSO capped at the
# shortfall-implied count (ceil(remaining_shortfall / EXPECTED_KW_PER_UNIT)),
# so it tapers down automatically as delta_p approaches target, instead
# of always taking a full RAMP_BATCH_SIZE step that can overshoot right
# at the end. Once the target is first reached, control switches to the
# normal per-timestep shortfall-sized admission/overshoot mechanism for
# the rest of the event.
RAMP_BATCH_SIZE = 30
RAMP_BATCH_SPACING_MIN = 1

# If True, the hottest "load" units move to "shed" (not idle) early when
# delta_p(t) overshoots TARGET_DELTA_KW + BAND_KW -- they remain
# recyclable (see module docstring) instead of being permanently retired.
# Only active once the initial ramp-up (above) is complete.
ALLOW_OVERSHOOT_RELEASE = True

# Post-event: NO reactivation of shed units into load (removed -- it
# produced a sharp post-event power spike). Instead, every unit still in
# "load" or "shed" at event_end is released straight to "idle" in groups
# of RELEASE_GROUP_SIZE, spaced RELEASE_GROUP_SPACING_MIN apart, so the
# fleet winds down gradually instead of all at once or all at full power.
RELEASE_GROUP_SIZE = 20
RELEASE_GROUP_SPACING_MIN = 15

# Forced load-up -- ported unchanged from HPWH_B3_Reserve_V2.py / see that
# file's own comment for why this MUST be exactly 1.0.
HP_DUTY_CYCLE_LOAD = 1.0

# Max tank temperature (C) -- exposed as a parameter so a sensitivity
# case can be run directly by changing this one value. 60C is the
# intended value (matches OCHRE's own default, 140F) -- the kwarg-key fix
# in build_dwelling_args() (below) now makes this setting actually take
# effect either way, but the target value itself is 60C, not 70C.
MAX_TANK_TEMP_C = 60

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

def get_tank_temperature_c(hpwh_unit):
    """Lower-node temperature only -- kept for reference/diagnostics."""
    return hpwh_unit.model.states[hpwh_unit.t_lower_idx]

def get_tank_temperature_avg_c(hpwh_unit):
    """
    TRUE volume-weighted average tank temperature across all thermal
    nodes -- the same quantity as OCHRE's own 'Hot Water Average
    Temperature (C)' output column (see ochre/Models/Water.py:
    generate_results(), 'Hot Water Average Temperature (C)' =
    self.states.dot(self.vol_fractions)). Used ONLY for the saved
    "Tank Average Temperature (C)" output column -- NOT for dispatch
    control decisions (see get_tank_temperature_upper_c() for why).
    """
    return hpwh_unit.model.states.dot(hpwh_unit.model.vol_fractions)

def get_tank_temperature_upper_c(hpwh_unit):
    """
    Upper-node temperature -- the EXACT quantity OCHRE's own internal
    'HP Duty Cycle' safety cutoff checks (see
    ochre/Equipment/WaterHeater.py: run_duty_cycle_control(), t_tank =
    self.model.states[self.t_upper_idx]; if t_tank > self.max_temp:
    ...forcing off). Used for ALL MAX_TANK_TEMP_C-threshold dispatch
    decisions (candidate headroom, reached-ceiling transition, overshoot
    ranking) so this controller's own state machine triggers in sync with
    when OCHRE actually stops the compressor -- the volume-weighted
    average reaches a given threshold well AFTER the upper node already
    has (confirmed: units were found silently drawing ~0 kW at average
    temps of 50-58C, nowhere near a 70C average-based threshold, because
    OCHRE had already force-shut them off based on the upper node alone).
    """
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
    canonical clock) -- the delta-P dispatch decision is made globally, in
    real time, by run_delta_dispatch(), which takes over from dispatch_t
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
            break  # pause here; the delta-P dispatch loop resumes from resume_idx

    return {
        "unit_id": unit["unit_id"],
        "home_path": unit["home_path"],
        "sim_dwelling": sim_dwelling,
        "sim_times": sim_times,
        "tank_temp_log": tank_temp_log,
        "resume_idx": resume_idx,
    }

#########################################
# DISPATCH LOOP
#########################################

def run_delta_dispatch(phase1_results, base_kw_lookup_by_unit):
    """
    Single synchronized lockstep loop across every home's already-
    constructed Dwelling, starting at dispatch_t (same index for every
    home -- see simulate_home_phase1()). See the module docstring and the
    DISPATCH SETTINGS section above for the full mechanics.

    Why single-threaded: deciding whether/how many units to admit
    requires the TRUE fleet-wide delta_p(t) at THIS instant, which only
    exists if every home is stepped forward in lockstep.

    Returns (phase1_results, log_rows) -- log_rows is a list of dicts,
    one per timestep, matching the columns ieee_gen_dec.py's MAIN
    EXECUTION writes to the per-timestep log CSV.
    """
    sim_times = phase1_results[0]["sim_times"]
    resume_idx = phase1_results[0]["resume_idx"]  # same for every home -- see simulate_home_phase1()

    hpwh_unit_by_key = {id(r): r["sim_dwelling"].get_equipment_by_end_use('Water Heating') for r in phase1_results}
    base_kw_by_key = {id(r): base_kw_lookup_by_unit[r["unit_id"]] for r in phase1_results}
    # current_avg_temp_by_key feeds ONLY the saved "Tank Average
    # Temperature (C)" output column. All MAX_TANK_TEMP_C-threshold
    # control decisions below use current_ctrl_temp_by_key (upper-node
    # temperature) instead -- see get_tank_temperature_upper_c().
    current_avg_temp_by_key = {id(r): r["tank_temp_log"][-1][1] for r in phase1_results}
    current_ctrl_temp_by_key = {id(r): get_tank_temperature_upper_c(hpwh_unit_by_key[id(r)]) for r in phase1_results}

    # Each unit's current control state: "idle" (baseline), "load"
    # (forced LOAD), or "shed" (parked, cooling, recyclable -- see module
    # docstring). ever_dispatched permanently marks a unit once it has
    # been admitted at least once (so it's never again treated as a FRESH
    # never-dispatched candidate -- it can still be re-admitted later via
    # the shed-recycling path).
    state = {id(r): "idle" for r in phase1_results}
    ever_dispatched = {id(r): False for r in phase1_results}
    dispatch_idx_by_key = {}  # last (re-)admission index, for ramp/pending tracking
    dispatch_time_by_key = {id(r): None for r in phase1_results}  # FIRST-ever admission time
    release_time_by_key = {id(r): None for r in phase1_results}  # LAST transition-to-idle time
    release_reason_by_key = {id(r): None for r in phase1_results}
    admission_order_by_key = {id(r): None for r in phase1_results}  # order of FIRST admission

    load_ctrl = {"Water Heating": {"HP Duty Cycle": HP_DUTY_CYCLE_LOAD, "Load Fraction": 1}}
    shed_ctrl = {"Water Heating": {"Setpoint": Tcontrol_SHEDC, "Deadband": Tcontrol_deadbandC, "Load Fraction": 1}}
    baseline_ctrl = {"Water Heating": {"Setpoint": TbaselineC, "Deadband": TdeadbandC, "Load Fraction": 1}}

    # Found by direct search against sim_times (not derived arithmetically
    # from resume_idx -- see historical note elsewhere in this project on
    # why that silently inherits a one-step offset).
    event_end_idx = next((i for i, t in enumerate(sim_times) if t >= event_end), len(sim_times))

    log_rows = []
    next_admission_order = 0
    release_idx_by_key = {}
    release_schedule_set = False

    # Initial ramp-up: a fixed RAMP_BATCH_SIZE admitted every
    # RAMP_BATCH_SPACING_MIN, until the target is first reached -- see
    # DISPATCH SETTINGS. next_ramp_admit_idx starts at resume_idx so the
    # very first in-event timestep is immediately eligible to admit.
    ramp_complete = False
    next_ramp_admit_idx = resume_idx

    # First sim_time >= target - band reached, for the "time to first
    # reach target" acceptance metric.
    first_in_band_time = None

    # delta_p(t) as of the END of the PREVIOUS timestep -- what this
    # iteration's admission/overshoot decision is gated on. Zero before
    # anything has ever been dispatched. Recomputed AFTER stepping, below,
    # using ALL ever-dispatched units (load, shed, or retired-to-idle) --
    # see module docstring: the whole-fleet-visible delta_p is what the
    # controller needs to hold at target, including the negative
    # "coasting" contribution from units already retired (their tank is
    # hotter than their own baseline run, from the earlier forced
    # heating, so post-retirement they draw LESS than baseline for a
    # while). Computing it at the SAME sim_time as the baseline lookup,
    # right after stepping (not before, against the previous step's
    # electric_kw reading), also fixes a one-timestep misalignment an
    # earlier version had between the controlled reading and the baseline
    # lookup.
    delta_p_prev = 0.0

    for i in range(resume_idx, len(sim_times)):
        sim_time = sim_times[i]
        in_event = sim_time < event_end

        # ---- Phase A: thermal state transitions, based on tank temp as
        # of the END of the PREVIOUS step. A "load" unit that has reached
        # the tank ceiling moves to "shed" (during the event, so it can
        # cool and be recycled) or straight to "idle" (after the event --
        # no more recycling, it just finishes). ----
        for r in phase1_results:
            key = id(r)
            if state[key] == "load" and current_ctrl_temp_by_key[key] >= MAX_TANK_TEMP_C:
                if in_event:
                    state[key] = "shed"
                else:
                    state[key] = "idle"
                    release_time_by_key[key] = sim_time
                    release_reason_by_key[key] = "setpoint_reached"

        # ---- At event_end, fix a release schedule ONCE for every unit
        # still in "load" or "shed": groups of RELEASE_GROUP_SIZE, spaced
        # RELEASE_GROUP_SPACING_MIN apart, released straight to "idle" --
        # NO reactivation of shed units into load (that produced a sharp
        # post-event power spike; removed). This winds the fleet down
        # gradually instead of all at once or at full power. ----
        if not release_schedule_set and i >= event_end_idx:
            still_out = [id(r) for r in phase1_results if state[id(r)] in ("load", "shed")]
            still_out.sort(key=lambda key: admission_order_by_key[key])
            for idx, key in enumerate(still_out):
                group_idx = idx // RELEASE_GROUP_SIZE
                delay_min = group_idx * RELEASE_GROUP_SPACING_MIN
                release_idx_by_key[key] = event_end_idx + round(delay_min / t_res)
            release_schedule_set = True

        # ---- Candidates: never-dispatched homes idle in their own
        # baseline RIGHT NOW (fresh), PLUS currently-"shed" homes
        # (recycled) -- ranked together, coldest-first. No separate
        # thermal-headroom eligibility filter: coldest-first ranking
        # alone keeps a near-ceiling candidate as a last resort (picked
        # only once every genuinely-cold candidate is already used),
        # which is what we want in a thin-pool moment -- using a
        # marginal candidate for a short contribution beats leaving the
        # shortfall unaddressed. ----
        fresh_candidates = [
            r for r in phase1_results
            if not ever_dispatched[id(r)]
            and base_kw_by_key[id(r)].get(sim_time, 0.0) <= BASELINE_IDLE_THRESHOLD_KW
        ]
        shed_candidates = [
            r for r in phase1_results
            if state[id(r)] == "shed"
        ]
        candidates = fresh_candidates + shed_candidates
        n_candidates_available = len(candidates)

        active_keys_before = [id(r) for r in phase1_results if state[id(r)] == "load"]
        pending_count = sum(
            1 for key in active_keys_before
            if (i - dispatch_idx_by_key.get(key, -10**9)) < RAMP_STEPS
        )
        pending_kw = pending_count * EXPECTED_KW_PER_UNIT

        # ---- Ramp-up complete? Checked first, using the PREVIOUS
        # timestep's known-true delta_p -- once true, stays true for the
        # rest of the event (never reverts to ramp-mode on a later dip). ----
        if in_event and not ramp_complete and delta_p_prev >= TARGET_DELTA_KW - BAND_KW:
            ramp_complete = True

        if in_event and not ramp_complete:
            # ---- Initial ramp: up to RAMP_BATCH_SIZE every
            # RAMP_BATCH_SPACING_MIN, coldest-first -- but capped at the
            # shortfall-implied count too, so the batch TAPERS down as
            # delta_p approaches target instead of always taking a full
            # fixed-size step that can overshoot right at the end (the
            # cap only bites once the remaining gap is smaller than
            # RAMP_BATCH_SIZE units' worth -- early batches are
            # unaffected and still admit the full RAMP_BATCH_SIZE). ----
            if i >= next_ramp_admit_idx and n_candidates_available > 0:
                shortfall = TARGET_DELTA_KW - delta_p_prev - pending_kw
                shortfall_implied = max(1, math.ceil(shortfall / EXPECTED_KW_PER_UNIT))
                n_admit = min(RAMP_BATCH_SIZE, shortfall_implied, n_candidates_available)
                candidates.sort(key=lambda r: current_ctrl_temp_by_key[id(r)], reverse=False)
                for candidate in candidates[:n_admit]:
                    key = id(candidate)
                    if not ever_dispatched[key]:
                        ever_dispatched[key] = True
                        dispatch_time_by_key[key] = sim_time
                        admission_order_by_key[key] = next_admission_order
                        next_admission_order += 1
                    state[key] = "load"
                    dispatch_idx_by_key[key] = i
                next_ramp_admit_idx = i + max(1, round(RAMP_BATCH_SPACING_MIN / t_res))

        # ---- Post-ramp: normal shortfall-sized admission (against the
        # PREVIOUS timestep's known-true delta_p), accounting for units
        # still ramping up, pulling from BOTH fresh and recycled-shed
        # candidates. ----
        elif in_event and delta_p_prev < TARGET_DELTA_KW - BAND_KW:
            shortfall = TARGET_DELTA_KW - delta_p_prev - pending_kw
            n_admit = max(0, math.ceil(shortfall / EXPECTED_KW_PER_UNIT))
            n_admit = min(n_admit, n_candidates_available, MAX_ADMISSIONS_SAFETY_CAP)

            if n_admit > 0:
                candidates.sort(key=lambda r: current_ctrl_temp_by_key[id(r)], reverse=False)
                for candidate in candidates[:n_admit]:
                    key = id(candidate)
                    if not ever_dispatched[key]:
                        ever_dispatched[key] = True
                        dispatch_time_by_key[key] = sim_time
                        admission_order_by_key[key] = next_admission_order
                        next_admission_order += 1
                    state[key] = "load"
                    dispatch_idx_by_key[key] = i

        # ---- Post-ramp: optional overshoot correction, move hottest
        # "load" units to "shed" (NOT idle) early if delta_p is over the
        # top of the band -- keeps them recyclable instead of
        # permanently retiring them. ----
        elif in_event and ALLOW_OVERSHOOT_RELEASE and delta_p_prev > TARGET_DELTA_KW + BAND_KW:
            overshoot = delta_p_prev - (TARGET_DELTA_KW + BAND_KW)
            n_release = max(1, math.ceil(overshoot / EXPECTED_KW_PER_UNIT))
            releasable = sorted(active_keys_before, key=lambda key: current_ctrl_temp_by_key[key], reverse=True)
            for key in releasable[:n_release]:
                state[key] = "shed"

        # ---- Step every home forward one timestep, per its CURRENT state ----
        for r in phase1_results:
            key = id(r)

            if state[key] in ("load", "shed") and key in release_idx_by_key and i >= release_idx_by_key[key]:
                state[key] = "idle"
                release_time_by_key[key] = sim_time
                release_reason_by_key[key] = "event_end_gradual"

            if state[key] == "load":
                control_cmd = load_ctrl
            elif state[key] == "shed":
                control_cmd = shed_ctrl
            else:
                control_cmd = baseline_ctrl

            r["sim_dwelling"].update(control_signal=control_cmd)
            fresh_temp = get_tank_temperature_avg_c(hpwh_unit_by_key[key])
            r["tank_temp_log"].append((sim_time, fresh_temp))
            current_avg_temp_by_key[key] = fresh_temp
            current_ctrl_temp_by_key[key] = get_tank_temperature_upper_c(hpwh_unit_by_key[key])

        # ---- delta_p(t) AFTER stepping, at THIS sim_time -- the control
        # variable, over ALL ever-dispatched units (load, shed, or
        # retired). sum_p(t) (total_fleet_power) is also computed here,
        # but ONLY for logging/diagnostics -- it no longer drives
        # admission/release. ----
        ever_dispatched_keys = [id(r) for r in phase1_results if ever_dispatched[id(r)]]
        active_keys_after = [id(r) for r in phase1_results if state[id(r)] == "load"]
        shed_keys_after = [id(r) for r in phase1_results if state[id(r)] == "shed"]
        delta_p = sum(
            hpwh_unit_by_key[key].electric_kw - base_kw_by_key[key].get(sim_time, 0.0)
            for key in ever_dispatched_keys
        )
        dispatched_power = sum(hpwh_unit_by_key[key].electric_kw for key in ever_dispatched_keys)
        total_fleet_power = sum(u.electric_kw for u in hpwh_unit_by_key.values())

        if in_event and first_in_band_time is None and delta_p >= TARGET_DELTA_KW - BAND_KW:
            first_in_band_time = sim_time

        log_rows.append({
            "Time": sim_time,
            "DeltaP_kW": delta_p,
            "Dispatched_Count": len(ever_dispatched_keys),
            "Active_Count": len(active_keys_after),
            "Shed_Count": len(shed_keys_after),
            "Pending_Count": pending_count,
            "Candidates_Available": n_candidates_available,
            "Dispatched_WH_Power_kW": dispatched_power,
            "Total_Fleet_WH_Power_kW": total_fleet_power,
        })

        delta_p_prev = delta_p

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

    print(f"Target: {TARGET_DELTA_KW}kW +/- {BAND_KW}kW added load (delta_p), "
          f"EXPECTED_KW_PER_UNIT={EXPECTED_KW_PER_UNIT}, RAMP_STEPS={RAMP_STEPS}")
    print(f"Dispatch window: {dispatch_t} to {event_end} ({reserve_event['duration']}hr)")
    print(f"Ramp-up: {RAMP_BATCH_SIZE} units every {RAMP_BATCH_SPACING_MIN}min until target first reached.")
    print(f"Post-event: groups of {RELEASE_GROUP_SIZE} released to idle every "
          f"{RELEASE_GROUP_SPACING_MIN}min (no reactivation, no mass release).")
    print(f"MAX_TANK_TEMP_C={MAX_TANK_TEMP_C}, ALLOW_OVERSHOOT_RELEASE={ALLOW_OVERSHOOT_RELEASE}")

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

    print(f"Phase 1 complete for {len(phase1_results)} units. Starting delta-P dispatch...")

    phase1_results, log_rows, first_in_band_time = run_delta_dispatch(phase1_results, base_kw_lookup_by_unit)

    n_dispatched = sum(1 for r in phase1_results if r["participating"])
    print(f"Delta-P dispatch complete: {n_dispatched}/{len(phase1_results)} units dispatched. "
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

    # ---- Summary stats over the event window (delta_p = DeltaP_kW, the
    # control variable; sum_p stats kept alongside as diagnostics) ----
    event_log = log_df[(log_df['Time'] >= dispatch_t) & (log_df['Time'] < event_end)]
    mean_dp = event_log['DeltaP_kW'].mean() if not event_log.empty else float('nan')
    min_dp = event_log['DeltaP_kW'].min() if not event_log.empty else float('nan')
    max_dp = event_log['DeltaP_kW'].max() if not event_log.empty else float('nan')
    mean_sum_p = event_log['Total_Fleet_WH_Power_kW'].mean() if not event_log.empty else float('nan')

    if first_in_band_time is not None:
        time_to_target_min = (first_in_band_time - dispatch_t).total_seconds() / 60
    else:
        time_to_target_min = None

    # In-band % is reported from first_in_band_time onward (once the ramp has
    # actually closed the gap) rather than from dispatch_t, so the ~2-minute
    # ramp-up doesn't count against the hold.
    hold_log = event_log[event_log['Time'] >= first_in_band_time] if first_in_band_time is not None else event_log.iloc[0:0]
    in_band = hold_log['DeltaP_kW'].between(TARGET_DELTA_KW - BAND_KW, TARGET_DELTA_KW + BAND_KW)
    pct_in_band = (in_band.mean() * 100) if not hold_log.empty else float('nan')

    n_never_dispatched = len(phase1_results) - n_dispatched

    print("=" * 60)
    print("DELTA-P DISPATCH SUMMARY")
    print("=" * 60)
    print(f"Target: {TARGET_DELTA_KW} kW, Band: +/-{BAND_KW} kW")
    print(f"Mean delta_p (event):  {mean_dp:.3f} kW")
    print(f"Min delta_p (event):   {min_dp:.3f} kW")
    print(f"Max delta_p (event):   {max_dp:.3f} kW")
    print(f"Mean sum_p (event, diagnostic only): {mean_sum_p:.3f} kW")
    print(f"%% of hold-window timesteps in-band (from first_in_band_time): {pct_in_band:.1f}%")
    if time_to_target_min is not None:
        print(f"Time to first reach target - band: {time_to_target_min:.1f} min after dispatch")
    else:
        print("Target - band was NEVER reached during the event.")
    print(f"Units dispatched: {n_dispatched} / {len(phase1_results)}")
    print(f"Units never dispatched: {n_never_dispatched}")

    # Report the first out-of-band excursion (if any) with a likely cause.
    if not event_log.empty and not in_band.all():
        first_out_idx = (~in_band).idxmax()
        first_out_row = event_log.loc[first_out_idx]
        cause = "no eligible candidates remaining" if first_out_row['Candidates_Available'] == 0 else \
                "overshoot" if first_out_row['DeltaP_kW'] > TARGET_DELTA_KW + BAND_KW else \
                "shortfall not yet closed"
        print(f"First out-of-band excursion at {first_out_row['Time']}: "
              f"delta_p={first_out_row['DeltaP_kW']:.3f} kW, candidates_available="
              f"{first_out_row['Candidates_Available']}, likely cause: {cause}")
    print("=" * 60)

    # ---- No plots are generated here -- ieee_dec_results.py owns all
    # plotting (plot_fleet_power_event_window() regenerates the event-
    # window figure from the aggregated CSVs this run just wrote). ----

    # ---- Finalize/save already happened above (needed for the sanity
    # check before anything else could run) -- just aggregate now. ----
    print("All simulations complete!")

    aggregate_results([frames_by_unit[u["unit_id"]][0] for u in fleet if u["unit_id"] in frames_by_unit],
                      [frames_by_unit[u["unit_id"]][1] for u in fleet if u["unit_id"] in frames_by_unit],
                      RESULTS_DIR)
    shutil.rmtree(os.path.join(SCRATCH_DIR, filename), ignore_errors=True)
