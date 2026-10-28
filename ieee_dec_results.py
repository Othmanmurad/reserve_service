# -*- coding: utf-8 -*-
"""
Author: Othman
Results script for ieee_gen_dec.py's IEEE generation-decrement (DEC /
downward-reserve, i.e. LOAD/ramp-up) power-capacity-cap dispatch study --
see that file's own module docstring for the full mechanics (target
fraction of the fleet, ramped in over a configurable duration, held, then
released in the same staggered order).

Merges what used to be two separate steps (HPWH_C1_parse_RU.py's WH-power
pivot + HPWH_C3_RU.py's per-unit/fleet reserve-capacity/sustain-time/CF/
FLC analysis and plots) into ONE file, read pipeline="IEEE_DEC" throughout
-- distinct from "RU"/"RU_V2" so this never collides with
HPWH_B4_Reserve_RU.py's (official/Excel) or HPWH_B3_Reserve_V2.py's own
run_context state. Deliberately a SEPARATE file from HPWH_C1_parse_RU.py /
HPWH_C3_RU.py, which are not touched here -- still needed for the Excel
calculator pipeline.

The WH-power pivot (process_data(), below) is kept purely for parity/
reference with the other C1 scripts in this project -- the metrics/plots
in this file read the RAW aggregated baseline/controlled CSVs directly
(same as HPWH_C3_RU.py always has), not the pivoted output.

All metric DEFINITIONS below are copied unchanged from HPWH_C3_RU.py's
per-unit/fleet logic -- see that file's own docstring for the full
rationale (Reserve Capacity / Sustain Time / baseline-vs-controlled
active energy / CF / FLC / added-load profile / cumulative energy), not
re-derived here. Only the pipeline tag and this file's own defaults
differ.
"""

import pandas as pd
import os
import json
import matplotlib.dates as mdates
import matplotlib.pyplot as plt
from run_context import load_filename, load_reserve_event, load_input_folder
from ieee_pes_common import DEC_RESULTS_ROOT

#########################################
# PATH SETUP
#########################################

working_dir = os.path.dirname(os.path.abspath(__file__))

# OCHRE_FILENAME overrides this when set; unset, inherit whichever run
# ieee_gen_dec.py last did (see run_context.py, pipeline="IEEE_DEC").
input_file_root = os.environ.get('OCHRE_FILENAME') or load_filename('IEEE_DEC_Test_dp_120_1h', pipeline="IEEE_DEC")

input_file_name1 = input_file_root + "_baseline"
input_file_name2 = input_file_root + "_controlled"

results_dir = os.path.join(DEC_RESULTS_ROOT, input_file_root)
os.makedirs(results_dir, exist_ok=True)

input_file_1 = os.path.join(results_dir, input_file_name1 + ".csv")
input_file_2 = os.path.join(results_dir, input_file_name2 + ".csv")

# Reported alongside the results, for reference -- not used for file
# access here (the metrics/plots below only ever read the aggregated
# CSVs above).
input_folder = load_input_folder('Input_Files', pipeline="IEEE_DEC")

#########################################
# RESERVE EVENT SETTINGS
#########################################

DISPATCH_DATE = '2018-01-12'   # fixed event date convention (see B-script's own EVENT_DATE comment)

_default_reserve_event = {'dispatch_time': '14:00', 'duration': 1.0}

# Prefer the per-folder config ieee_gen_dec.py writes alongside this run's
# own aggregated CSVs (results_dir/input_file_root -- a single shared
# load_reserve_event() slot per pipeline only ever reflects whichever run
# happened MOST RECENTLY, so re-running this script later against an OLDER
# test folder would otherwise silently pick up the wrong event window/
# target, e.g. from a later run in a parameter sweep). Fall back to the
# shared slot for older runs from before this per-folder file existed.
_run_config_file = os.path.join(results_dir, input_file_root + "_run_config.json")
try:
    with open(_run_config_file) as f:
        _reserve_event = json.load(f)
except (FileNotFoundError, json.JSONDecodeError):
    _reserve_event = load_reserve_event(_default_reserve_event, pipeline="IEEE_DEC")

DISPATCH_TIME = os.environ.get('OCHRE_DISPATCH_TIME') or _reserve_event['dispatch_time']
EVENT_DURATION_HR = float(os.environ.get('OCHRE_DURATION_HR') or _reserve_event['duration'])

dispatch_t = pd.to_datetime(f"{DISPATCH_DATE} {DISPATCH_TIME}")
event_end = dispatch_t + pd.Timedelta(hours=EVENT_DURATION_HR)

#########################################
# SETTINGS
#########################################

WH_POWER_COL = "Water Heating Electric Power (kW)"

# Minimum WH power (kW) to count a minute as "active" (compressor
# running) rather than idle/parasitic draw.
ONLINE_THRESHOLD_KW = 0.1

# How far around the event window the activation plot extends, in hours.
PLOT_MARGIN_HR = 2.0

# Target added-load (delta_p) and band, for the 24hr delta_p plot's
# reference line/shading -- loaded from whatever ieee_gen_dec.py actually
# used for this run (persisted alongside dispatch_time/duration), so a
# parameter sweep across runs doesn't need these kept in sync by hand.
TARGET_DELTA_KW = float(os.environ.get('OCHRE_TARGET_DELTA_KW') or _reserve_event.get('target_delta_kw', 120.0))
BAND_KW = float(os.environ.get('OCHRE_BAND_KW') or _reserve_event.get('band_kw', 0.5))

# Flex Load Capacity (FLC) inputs -- disabled for now (hardcoded to 1.0,
# i.e. no scaling), same reasoning/status as HPWH_C3_RU.py's own
# PARTICIPATION_RATE/REACHABLE_RATE: this run's dispatch mechanism
# already controls participation directly (TARGET_FRACTION in
# ieee_gen_dec.py), so a second post-hoc derate here would double-count it.
PARTICIPATION_RATE = 1.0
REACHABLE_RATE = 1.0
_SAMPLE_ENV = os.environ.get('OCHRE_SAMPLE')

#########################################
# C1: WH-POWER PIVOT (kept for parity/reference -- not read by anything
# below; the metrics/plots read the raw aggregated CSVs directly)
#########################################

def process_data(input_file, output_file, wanted_col):
    df = pd.read_csv(input_file)
    df = df.dropna(axis=0)

    df['time'] = pd.to_datetime(df['Time'], errors='coerce')
    df['hr_min'] = df['time'].dt.strftime('%H:%M')

    cols = ['Time', 'Total Electric Power (kW)', 'Total Electric Energy (kWh)', 'Water Heating Electric Power (kW)',
            'Water Heating COP (-)', 'Water Heating Deadband Upper Limit (C)', 'Water Heating Deadband Lower Limit (C)',
            'Water Heating Heat Pump COP (-)', 'Hot Water Outlet Temperature (C)', 'Temperature - Indoor (C)', 'time']

    unwanted_cols = cols.copy()
    unwanted_cols.remove(wanted_col)
    df = df.drop(unwanted_cols, axis=1)

    df_pivot = df.pivot_table(index='Home', columns='hr_min', values=wanted_col)
    df_pivot.to_csv(output_file, index=True)

def run_wh_power_pivot():
    output_file_1 = os.path.join(results_dir, input_file_name1 + "_WH_power.csv")
    output_file_2 = os.path.join(results_dir, input_file_name2 + "_WH_power.csv")
    process_data(input_file_1, output_file_1, WH_POWER_COL)
    process_data(input_file_2, output_file_2, WH_POWER_COL)
    print(f"WH power pivot written to: {output_file_1}")
    print(f"WH power pivot written to: {output_file_2}")

#########################################
# LOAD DATA
#########################################

def load_data():
    df_base = pd.read_csv(input_file_1)
    df_ctrl = pd.read_csv(input_file_2)

    df_base['Time'] = pd.to_datetime(df_base['Time'], errors='coerce')
    df_ctrl['Time'] = pd.to_datetime(df_ctrl['Time'], errors='coerce')

    return df_base, df_ctrl

#########################################
# TIMESTEP INFERENCE
#########################################

def infer_timestep_hours(df):
    """Derives the simulation timestep from the data's own timestamps
    (median gap for one home) instead of assuming 1-min resolution."""
    home_id = df['Home'].iloc[0]
    times = df.loc[df['Home'] == home_id, 'Time'].sort_values()
    if len(times) < 2:
        return 1 / 60  # fallback: assume 1 minute
    return times.diff().dropna().dt.total_seconds().median() / 3600

#########################################
# PER-UNIT RESERVE CAPACITY / SUSTAIN TIME
#########################################

def compute_unit_metrics(df_base, df_ctrl):
    """
    Picks the first home alphabetically (by Home ID) and computes its
    Reserve Capacity / Sustain Time / baseline-vs-controlled active
    energy over the event window -- see HPWH_C3_RU.py's own docstring
    for the full definitions (unchanged here).
    """
    home_id = sorted(df_ctrl['Home'].unique())[0]

    dt_hours = infer_timestep_hours(df_ctrl)

    base_window = df_base[(df_base['Home'] == home_id) &
                           (df_base['Time'] >= dispatch_t) & (df_base['Time'] < event_end)].sort_values('Time')
    ctrl_window = df_ctrl[(df_ctrl['Home'] == home_id) &
                           (df_ctrl['Time'] >= dispatch_t) & (df_ctrl['Time'] < event_end)].sort_values('Time')

    base_active = base_window[base_window[WH_POWER_COL] > ONLINE_THRESHOLD_KW]
    ctrl_active = ctrl_window[ctrl_window[WH_POWER_COL] > ONLINE_THRESHOLD_KW]

    reserve_capacity_kwh = ctrl_active[WH_POWER_COL].sum() * dt_hours
    sustain_time_hr = len(ctrl_active) * dt_hours
    baseline_sustain_time_hr = len(base_active) * dt_hours

    baseline_active_energy_kwh = base_active[WH_POWER_COL].sum() * dt_hours
    controlled_active_energy_kwh = reserve_capacity_kwh
    delta_active_energy_kwh = controlled_active_energy_kwh - baseline_active_energy_kwh

    return {
        "home_id": home_id,
        "dt_hours": dt_hours,
        "n_ctrl_active_minutes": len(ctrl_active),
        "n_base_active_minutes": len(base_active),
        "reserve_capacity_kwh": reserve_capacity_kwh,
        "sustain_time_hr": sustain_time_hr,
        "baseline_sustain_time_hr": baseline_sustain_time_hr,
        "baseline_active_energy_kwh": baseline_active_energy_kwh,
        "controlled_active_energy_kwh": controlled_active_energy_kwh,
        "delta_active_energy_kwh": delta_active_energy_kwh,
        "base_window": base_window,
        "ctrl_window": ctrl_window,
    }

#########################################
# PRINT SUMMARY
#########################################

def print_summary(metrics):
    print("=" * 50)
    print("PER-UNIT RESERVE CAPACITY / SUSTAIN TIME -- IEEE DEC")
    print("=" * 50)
    print(f"Fleet dataset: {input_folder}")
    print(f"Home (first alphabetically): {metrics['home_id']}")
    print(f"Dispatch time: {dispatch_t}")
    print(f"Event end:     {event_end}")
    print(f"Timestep used: {metrics['dt_hours'] * 60:.2f} min")
    print()
    print("--- Reserve Capacity / Sustain Time (controlled, active minutes only) ---")
    print(f"  Reserve Capacity: {metrics['reserve_capacity_kwh']:.4f} kWh "
          f"({metrics['n_ctrl_active_minutes']} active minutes)")
    print(f"  Sustain Time:     {metrics['sustain_time_hr']:.4f} hr")
    print()
    print("--- Sustain Time, baseline case (same definition, baseline's own active minutes) ---")
    print(f"  Baseline Sustain Time: {metrics['baseline_sustain_time_hr']:.4f} hr "
          f"({metrics['n_base_active_minutes']} active minutes)")
    print()
    print("--- Baseline vs. controlled active energy (same window, each trace's own active minutes) ---")
    print(f"  Baseline active energy:    {metrics['baseline_active_energy_kwh']:.4f} kWh "
          f"({metrics['n_base_active_minutes']} active minutes)")
    print(f"  Controlled active energy:  {metrics['controlled_active_energy_kwh']:.4f} kWh")
    print(f"  Delta (controlled - baseline): {metrics['delta_active_energy_kwh']:.4f} kWh")
    print("=" * 50)

#########################################
# PLOT: UNIT ACTIVATION (BASELINE + CONTROLLED)
#########################################

#########################################
# FLEET-LEVEL RESERVE CAPACITY / SUSTAIN TIME
#########################################

def compute_fleet_metrics(df_base, df_ctrl):
    dt_hours = infer_timestep_hours(df_ctrl)
    all_homes = sorted(df_ctrl['Home'].unique())

    base_window = df_base[(df_base['Time'] >= dispatch_t) & (df_base['Time'] < event_end)]
    ctrl_window = df_ctrl[(df_ctrl['Time'] >= dispatch_t) & (df_ctrl['Time'] < event_end)]

    base_fleet_power_series = base_window.groupby('Time')[WH_POWER_COL].sum()
    ctrl_fleet_power_series = ctrl_window.groupby('Time')[WH_POWER_COL].sum()
    avg_fleet_power_base_kw = base_fleet_power_series.mean() if not base_fleet_power_series.empty else float('nan')
    avg_fleet_power_ctrl_kw = ctrl_fleet_power_series.mean() if not ctrl_fleet_power_series.empty else float('nan')

    base_active = base_window[base_window[WH_POWER_COL] > ONLINE_THRESHOLD_KW]
    ctrl_active = ctrl_window[ctrl_window[WH_POWER_COL] > ONLINE_THRESHOLD_KW]

    base_per_unit = base_active.groupby('Home')[WH_POWER_COL].agg(['sum', 'count']).rename(
        columns={'sum': 'baseline_active_power_sum', 'count': 'n_base_active_minutes'})
    ctrl_per_unit = ctrl_active.groupby('Home')[WH_POWER_COL].agg(['sum', 'count']).rename(
        columns={'sum': 'controlled_active_power_sum', 'count': 'n_ctrl_active_minutes'})

    per_unit = pd.DataFrame(index=all_homes)
    per_unit = per_unit.join(base_per_unit).join(ctrl_per_unit).fillna(0)
    per_unit.index.name = 'Home'

    per_unit['baseline_active_energy_kwh'] = per_unit['baseline_active_power_sum'] * dt_hours
    per_unit['reserve_capacity_kwh'] = per_unit['controlled_active_power_sum'] * dt_hours
    per_unit['controlled_active_energy_kwh'] = per_unit['reserve_capacity_kwh']
    per_unit['sustain_time_hr'] = per_unit['n_ctrl_active_minutes'] * dt_hours
    per_unit['baseline_sustain_time_hr'] = per_unit['n_base_active_minutes'] * dt_hours
    per_unit['delta_active_energy_kwh'] = per_unit['controlled_active_energy_kwh'] - per_unit['baseline_active_energy_kwh']
    per_unit = per_unit.drop(columns=['baseline_active_power_sum', 'controlled_active_power_sum'])

    fleet_summary = {
        "n_units": len(all_homes),
        "dt_hours": dt_hours,
        "avg_fleet_power_base_kw": avg_fleet_power_base_kw,
        "avg_fleet_power_ctrl_kw": avg_fleet_power_ctrl_kw,
        "fleet_reserve_capacity_kwh": per_unit['reserve_capacity_kwh'].sum(),
        "fleet_baseline_active_energy_kwh": per_unit['baseline_active_energy_kwh'].sum(),
        "fleet_controlled_active_energy_kwh": per_unit['controlled_active_energy_kwh'].sum(),
        "fleet_delta_active_energy_kwh": per_unit['delta_active_energy_kwh'].sum(),
        "avg_sustain_time_hr": per_unit['sustain_time_hr'].mean(),
        "min_sustain_time_hr": per_unit['sustain_time_hr'].min(),
        "max_sustain_time_hr": per_unit['sustain_time_hr'].max(),
        "avg_baseline_sustain_time_hr": per_unit['baseline_sustain_time_hr'].mean(),
        "min_baseline_sustain_time_hr": per_unit['baseline_sustain_time_hr'].min(),
        "max_baseline_sustain_time_hr": per_unit['baseline_sustain_time_hr'].max(),
    }

    return per_unit, fleet_summary

#########################################
# FLEX LOAD CAPACITY (FLC)
#########################################

def compute_flex_load_capacity(fleet_summary):
    n_units = fleet_summary['n_units']
    sample = float(_SAMPLE_ENV) if _SAMPLE_ENV else n_units

    per_unit_baseline_kwh = fleet_summary['fleet_baseline_active_energy_kwh'] / n_units
    per_unit_controlled_kwh = fleet_summary['fleet_controlled_active_energy_kwh'] / n_units

    flc_baseline = per_unit_baseline_kwh * PARTICIPATION_RATE * REACHABLE_RATE * sample
    flc_controlled = per_unit_controlled_kwh * PARTICIPATION_RATE * REACHABLE_RATE * sample

    pct_diff_flc = (flc_controlled - flc_baseline) / flc_baseline * 100 if flc_baseline else float('nan')

    return {
        "n_units": n_units,
        "participation_rate": PARTICIPATION_RATE,
        "reachable_rate": REACHABLE_RATE,
        "sample": sample,
        "per_unit_baseline_kwh": per_unit_baseline_kwh,
        "per_unit_controlled_kwh": per_unit_controlled_kwh,
        "flc_baseline_kwh": flc_baseline,
        "flc_controlled_kwh": flc_controlled,
        "pct_diff_flc": pct_diff_flc,
    }

def print_flc_summary(flc_summary):
    print("=" * 50)
    print("FLEX LOAD CAPACITY (FLC) -- IEEE DEC")
    print("=" * 50)
    print(f"Participation rate: {flc_summary['participation_rate']}, "
          f"Reachable rate: {flc_summary['reachable_rate']}, "
          f"Sample: {flc_summary['sample']}")
    print(f"Per-unit baseline energy:   {flc_summary['per_unit_baseline_kwh']:.4f} kWh")
    print(f"Per-unit controlled energy: {flc_summary['per_unit_controlled_kwh']:.4f} kWh")
    print(f"FLC (baseline):   {flc_summary['flc_baseline_kwh']:.4f} kWh")
    print(f"FLC (controlled): {flc_summary['flc_controlled_kwh']:.4f} kWh")
    print(f"FLC % difference (controlled vs baseline): {flc_summary['pct_diff_flc']:.2f}%")
    print("=" * 50)

#########################################
# PRINT FLEET SUMMARY
#########################################

def print_fleet_summary(fleet_summary):
    print("=" * 50)
    print("FLEET RESERVE CAPACITY / SUSTAIN TIME -- IEEE DEC")
    print("=" * 50)
    print(f"Fleet dataset: {input_folder}")
    print(f"Number of units (n): {fleet_summary['n_units']}")
    print(f"Dispatch time: {dispatch_t}")
    print(f"Event end:     {event_end}")
    print(f"Timestep used: {fleet_summary['dt_hours'] * 60:.2f} min")
    print()
    print("--- Average fleet-aggregate power during the event window ---")
    print(f"  Baseline:   {fleet_summary['avg_fleet_power_base_kw']:.3f} kW")
    print(f"  Controlled: {fleet_summary['avg_fleet_power_ctrl_kw']:.3f} kW")
    print()
    print("--- Fleet Reserve Capacity (controlled, active minutes only, summed across units) ---")
    print(f"  Fleet Reserve Capacity: {fleet_summary['fleet_reserve_capacity_kwh']:.3f} kWh")
    print()
    print("--- Sustain Time across the fleet (controlled) ---")
    print(f"  Average: {fleet_summary['avg_sustain_time_hr']:.4f} hr")
    print(f"  Min:     {fleet_summary['min_sustain_time_hr']:.4f} hr")
    print(f"  Max:     {fleet_summary['max_sustain_time_hr']:.4f} hr")
    print()
    print("--- Sustain Time across the fleet (baseline) ---")
    print(f"  Average: {fleet_summary['avg_baseline_sustain_time_hr']:.4f} hr")
    print(f"  Min:     {fleet_summary['min_baseline_sustain_time_hr']:.4f} hr")
    print(f"  Max:     {fleet_summary['max_baseline_sustain_time_hr']:.4f} hr")
    print()
    print("--- Baseline vs. controlled active energy, fleet totals ---")
    print(f"  Baseline active energy (fleet):    {fleet_summary['fleet_baseline_active_energy_kwh']:.3f} kWh")
    print(f"  Controlled active energy (fleet):  {fleet_summary['fleet_controlled_active_energy_kwh']:.3f} kWh")
    print(f"  Delta (controlled - baseline):     {fleet_summary['fleet_delta_active_energy_kwh']:.3f} kWh")
    print("=" * 50)

#########################################
# PLOT: FLEET ACTIVATION (BASELINE + CONTROLLED)
#########################################

def plot_fleet_activation_combined():
    plot_start = dispatch_t - pd.Timedelta(hours=PLOT_MARGIN_HR)
    plot_end = event_end + pd.Timedelta(hours=PLOT_MARGIN_HR)

    df_base, df_ctrl = load_data()
    base_window = df_base[(df_base['Time'] >= plot_start) & (df_base['Time'] <= plot_end)]
    ctrl_window = df_ctrl[(df_ctrl['Time'] >= plot_start) & (df_ctrl['Time'] <= plot_end)]

    base_fleet = base_window.groupby('Time')[WH_POWER_COL].sum()
    ctrl_fleet = ctrl_window.groupby('Time')[WH_POWER_COL].sum()

    fig, ax = plt.subplots(figsize=(8, 5))

    ax.plot(base_fleet.index, base_fleet.values, color="#133CAD", linestyle='-',
            linewidth=1.3, label='Baseline')
    ax.plot(ctrl_fleet.index, ctrl_fleet.values, color="#ee5e0a", linestyle='--',
            linewidth=1.3, label='Controlled')

    ax.set_title(f'Fleet Water Heater Activation (DEC - Load-Up) (N={df_ctrl["Home"].nunique()})')
    ax.set_xlabel('Time')
    ax.set_ylabel('Fleet Power (kW)')
    ax.legend(loc='upper left')

    ax.xaxis.set_major_locator(mdates.HourLocator(interval=1))
    ax.xaxis.set_major_formatter(mdates.DateFormatter('%H:%M'))

    output_dir = results_dir
    os.makedirs(output_dir, exist_ok=True)
    photo_file = os.path.join(output_dir, input_file_root + "_fleet_activation_combined_plot.png")
    plt.savefig(photo_file, dpi=300, bbox_inches='tight')
    print(f"Combined fleet activation plot saved to: {photo_file}")
    return photo_file

#########################################
# PLOT: FLEET POWER OVER THE FULL SIMULATION WINDOW, EVENT SHADED
#########################################

def plot_fleet_power_with_event(df_base, df_ctrl):
    base_fleet = df_base.groupby('Time')[WH_POWER_COL].sum().rename('P_fleet_base')
    ctrl_fleet = df_ctrl.groupby('Time')[WH_POWER_COL].sum().rename('P_fleet_ctrl')

    fig, ax = plt.subplots(figsize=(8, 5))
    ax.plot(base_fleet.index, base_fleet.values, color="#133CAD", linestyle='-',
            linewidth=1.3, label='Baseline')
    ax.plot(ctrl_fleet.index, ctrl_fleet.values, color="#ee5e0a", linestyle='--',
            linewidth=1.3, label='Controlled')

    ax.axvline(dispatch_t, color='red', linestyle=':', linewidth=1.5, label='Dispatch start')
    ax.axvline(event_end, color='gray', linestyle=':', linewidth=1.5, label='Event end')
    ax.axvspan(dispatch_t, event_end, color='red', alpha=0.1)

    ax.set_title(f'Aggregated Fleet Water Heater Power During Reserve Service Event (DEC - Load-Up) (N={df_ctrl["Home"].nunique()})')
    ax.set_xlabel('Time')
    ax.set_ylabel('Fleet Power (kW)')
    ax.legend(loc='upper left')

    ax.xaxis.set_major_locator(mdates.HourLocator(byhour=range(0, 24, 3)))
    ax.xaxis.set_major_formatter(mdates.DateFormatter('%H:%M'))

    output_dir = results_dir
    os.makedirs(output_dir, exist_ok=True)
    photo_file = os.path.join(output_dir, input_file_root + "_fleet_power_event_plot.png")
    plt.savefig(photo_file, dpi=300, bbox_inches='tight')
    print(f"Fleet power (full window, event marked) plot saved to: {photo_file}")
    return photo_file

#########################################
# PLOT: DELTA-P (ADDED LOAD) OVER THE FULL 24HR WINDOW
#########################################

def plot_fleet_power_24hr(df_base, df_ctrl):
    """
    delta_p(t) = controlled - baseline, summed across the fleet, over the
    full simulated day. Single axis, no cumulative-energy curve.
    """
    base_fleet = df_base.groupby('Time')[WH_POWER_COL].sum()
    ctrl_fleet = df_ctrl.groupby('Time')[WH_POWER_COL].sum()
    delta_p = (ctrl_fleet - base_fleet).rename('delta_p_kw')

    fig, ax = plt.subplots(figsize=(10, 5))

    ax.axvspan(dispatch_t, event_end, color='green', alpha=0.08, label='Event window')
    ax.axvspan(event_end, delta_p.index.max(), color='orange', alpha=0.08, label='Post-event')

    ax.plot(delta_p.index, delta_p.values, color="#133CAD", linestyle='-', linewidth=1.3,
            label='delta_p(t) = controlled - baseline')
    ax.axhline(0, color='black', linestyle='-', linewidth=0.8)
    ax.axhline(TARGET_DELTA_KW, color='black', linestyle='--', linewidth=1.3,
               label=f'Target = {TARGET_DELTA_KW} kW')

    ax.axvline(dispatch_t, color='red', linestyle=':', linewidth=1.5, label='Dispatch start')
    ax.axvline(event_end, color='gray', linestyle=':', linewidth=1.5, label='Event end')

    ax.set_title(f'Delta-P (Added Load) Over the Full Day (DEC - Load-Up) (N={df_ctrl["Home"].nunique()})')
    ax.set_xlabel('Time')
    ax.set_ylabel('Change in consumption vs. baseline (kW)')
    ax.legend(loc='upper left', fontsize=8)

    # Scale the y-axis to the ACTUAL delta_p data, not to wherever the
    # target line (above) happens to sit -- matplotlib's autoscale
    # otherwise includes axhline(TARGET_DELTA_KW) in its data limits, so
    # an intentionally-unreachable target (e.g. the ideal/benchmark
    # tests' 100,000 kW) silently stretches the axis and flattens the
    # real curve into an invisible line near zero.
    data_min, data_max = delta_p.min(), delta_p.max()
    y_pad = max(1.0, 0.1 * (data_max - data_min))
    ax.set_ylim(min(0, data_min) - y_pad, data_max + y_pad)

    ax.xaxis.set_major_locator(mdates.HourLocator(byhour=range(0, 24, 3)))
    ax.xaxis.set_major_formatter(mdates.DateFormatter('%H:%M'))

    output_dir = results_dir
    os.makedirs(output_dir, exist_ok=True)
    photo_file = os.path.join(output_dir, input_file_root + "_fleet_power_24hr_plot.png")
    plt.savefig(photo_file, dpi=300, bbox_inches='tight')
    print(f"Delta-P (24hr) plot saved to: {photo_file}")
    return photo_file


def plot_fleet_power_event_window(df_base, df_ctrl):
    """
    delta_p(t) = controlled - baseline, windowed to the event +/-
    PLOT_MARGIN_HR -- replaces ieee_gen_dec.py's own {filename}_deltaP.png
    IN PLACE (same filename). Regenerating it here means fixing that plot
    doesn't require re-running the (multi-hour, for a 498-home fleet)
    simulation.
    """
    plot_start = dispatch_t - pd.Timedelta(hours=PLOT_MARGIN_HR)
    plot_end = event_end + pd.Timedelta(hours=PLOT_MARGIN_HR)

    base_window = df_base[(df_base['Time'] >= plot_start) & (df_base['Time'] <= plot_end)]
    ctrl_window = df_ctrl[(df_ctrl['Time'] >= plot_start) & (df_ctrl['Time'] <= plot_end)]
    base_fleet = base_window.groupby('Time')[WH_POWER_COL].sum()
    ctrl_fleet = ctrl_window.groupby('Time')[WH_POWER_COL].sum()
    delta_p = (ctrl_fleet - base_fleet).rename('delta_p_kw')

    fig, ax = plt.subplots(figsize=(10, 5))

    ax.axvspan(dispatch_t, event_end, color='green', alpha=0.08, label='Event window')
    ax.axvspan(event_end, plot_end, color='orange', alpha=0.08, label='Post-event')

    ax.plot(delta_p.index, delta_p.values, color="#133CAD", linestyle='-', linewidth=1.3,
            label='delta_p(t) = controlled - baseline')
    ax.axhline(0, color='black', linestyle='-', linewidth=0.8)
    ax.axhline(TARGET_DELTA_KW, color='black', linestyle='--', linewidth=1.3,
               label=f'Target = {TARGET_DELTA_KW} kW')

    ax.axvline(dispatch_t, color='red', linestyle=':', linewidth=1.5, label='Dispatch start')
    ax.axvline(event_end, color='gray', linestyle=':', linewidth=1.5, label='Event end')

    ax.set_title(f'Delta-P Dispatch -- Added Load vs. Target (DEC - Load-Up) (N={df_ctrl["Home"].nunique()})')
    ax.set_xlabel('Time')
    ax.set_ylabel('Change in consumption vs. baseline (kW)')
    ax.legend(loc='upper left', fontsize=8)
    ax.set_xlim(plot_start, plot_end)

    # See plot_fleet_power_24hr()'s matching comment -- scale to the
    # actual data, not to an intentionally-unreachable target line.
    data_min, data_max = delta_p.min(), delta_p.max()
    y_pad = max(1.0, 0.1 * (data_max - data_min))
    ax.set_ylim(min(0, data_min) - y_pad, data_max + y_pad)

    ax.xaxis.set_major_locator(mdates.HourLocator(interval=1))
    ax.xaxis.set_major_formatter(mdates.DateFormatter('%H:%M'))

    output_dir = results_dir
    os.makedirs(output_dir, exist_ok=True)
    photo_file = os.path.join(output_dir, input_file_root + "_deltaP.png")
    plt.savefig(photo_file, dpi=300, bbox_inches='tight')
    print(f"Delta-P plot (event window) saved to: {photo_file}")
    return photo_file

#########################################
# PLOT: ACTIVE UNITS OVER TIME
#########################################

def plot_active_units(total_homes):
    """
    Active_Count(t) -- number of units currently dispatched AND NOT YET
    RELEASED (i.e. still under forced LOAD control) -- read directly from
    ieee_gen_dec.py's own per-timestep {filename}_deltaP_log.csv (the
    controller's own bookkeeping of "active" units, same definition used
    throughout that script). Dual axis: left = raw count, right = % of
    the fleet. The right axis is the same series rescaled (not a second
    plotted line) so the two stay in lockstep.
    """
    log_file = os.path.join(results_dir, input_file_root + "_deltaP_log.csv")
    try:
        log_df = pd.read_csv(log_file, parse_dates=['Time'])
    except FileNotFoundError:
        print(f"(No per-timestep log found at {log_file} -- active-units plot skipped.)")
        return None

    # Service period ONLY -- drop the post-event tail entirely (no
    # "Post-event" shading/legend entry, since nothing outside the event
    # window is shown).
    log_df = log_df[(log_df['Time'] >= dispatch_t) & (log_df['Time'] <= event_end)]

    fig, ax = plt.subplots(figsize=(10, 5))
    ax2 = ax.twinx()

    ax.plot(log_df['Time'], log_df['Active_Count'], color="#133CAD", linestyle='-', linewidth=1.3,
            label='Active units (dispatched, not released)')

    ax.axvline(dispatch_t, color='red', linestyle=':', linewidth=1.5, label='Dispatch start')
    ax.axvline(event_end, color='gray', linestyle=':', linewidth=1.5, label='Event end')

    ax.set_title(f'Active Dispatched Units Over Time (DEC - Load-Up) (N={total_homes})')
    ax.set_xlabel('Time')
    ax.set_ylabel('Active units (count)')
    ax2.set_ylabel('Active units (% of fleet)')

    # Right axis is a straight unit conversion of the left axis -- same
    # data, so its limits are just the left axis's limits rescaled, not a
    # second plotted series.
    ax.relim()
    ax.autoscale_view()
    y_lo, y_hi = ax.get_ylim()
    ax2.set_ylim(y_lo / total_homes * 100, y_hi / total_homes * 100)

    ax.legend(loc='upper left', fontsize=8)

    ax.xaxis.set_major_locator(mdates.HourLocator(byhour=range(0, 24, 3)))
    ax.xaxis.set_major_formatter(mdates.DateFormatter('%H:%M'))

    output_dir = results_dir
    os.makedirs(output_dir, exist_ok=True)
    photo_file = os.path.join(output_dir, input_file_root + "_active_units_plot.png")
    plt.savefig(photo_file, dpi=300, bbox_inches='tight')
    print(f"Active units plot saved to: {photo_file}")
    return photo_file

#########################################
# PLOT: BASELINE UNIT STATUS OVER TIME (consuming power vs. idle)
#########################################

def plot_baseline_unit_status(df_base, total_homes):
    """
    How many homes are ACTUALLY drawing power under baseline (>
    ONLINE_THRESHOLD_KW) at each instant during the service period, vs
    idle -- i.e. the raw size of the fresh-candidate pool
    ieee_gen_dec.py's dispatch loop draws from every timestep (a fresh
    LOAD candidate must be IDLE in baseline right now -- see
    BASELINE_IDLE_THRESHOLD_KW, same 0.1kW threshold as
    ONLINE_THRESHOLD_KW here). Same dual-axis convention as
    plot_active_units() (left = raw count, right = % of fleet), but
    plots BOTH series (consuming vs. idle) since they're independently
    informative here, unlike the single dispatched-count series there.
    """
    base_window = df_base[(df_base['Time'] >= dispatch_t) & (df_base['Time'] <= event_end)]
    all_times = sorted(base_window['Time'].unique())
    active_count = base_window[base_window[WH_POWER_COL] > ONLINE_THRESHOLD_KW].groupby('Time').size()
    active_count = active_count.reindex(all_times, fill_value=0)
    idle_count = total_homes - active_count

    fig, ax = plt.subplots(figsize=(10, 5))
    ax2 = ax.twinx()

    ax.plot(active_count.index, active_count.values, color="#133CAD", linestyle='-', linewidth=1.3,
            label='Consuming power (baseline)')
    ax.plot(idle_count.index, idle_count.values, color="#ee5e0a", linestyle='--', linewidth=1.3,
            label='Idle (baseline)')

    ax.axvline(dispatch_t, color='red', linestyle=':', linewidth=1.5, label='Dispatch start')
    ax.axvline(event_end, color='gray', linestyle=':', linewidth=1.5, label='Event end')

    ax.set_title(f'Baseline Unit Status Over Time (DEC - Load-Up) (N={total_homes})')
    ax.set_xlabel('Time')
    ax.set_ylabel('Units (count)')
    ax2.set_ylabel('Units (% of fleet)')

    ax.set_ylim(0, total_homes)
    ax2.set_ylim(0, 100)

    ax.legend(loc='upper left', fontsize=8)

    ax.xaxis.set_major_locator(mdates.HourLocator(byhour=range(0, 24, 3)))
    ax.xaxis.set_major_formatter(mdates.DateFormatter('%H:%M'))

    output_dir = results_dir
    os.makedirs(output_dir, exist_ok=True)
    photo_file = os.path.join(output_dir, input_file_root + "_baseline_status_plot.png")
    plt.savefig(photo_file, dpi=300, bbox_inches='tight')
    print(f"Baseline unit status plot saved to: {photo_file}")
    return photo_file

def plot_control_unit_status(df_ctrl, total_homes):
    """
    Same as plot_baseline_unit_status() above, but for the CONTROLLED
    case: how many homes are actually drawing power (> ONLINE_THRESHOLD_KW)
    under the dispatch scenario at each instant, vs idle. For a never-
    dispatched home this tracks its own baseline activity (untouched);
    for a dispatched home it reflects the forced "load" draw while
    active, the lower "shed" draw while parked, or baseline activity
    again once released -- so this plot shows the REAL effect of the
    dispatch mechanism on fleet-wide activity, directly comparable to
    the baseline-status plot alongside it.
    """
    ctrl_window = df_ctrl[(df_ctrl['Time'] >= dispatch_t) & (df_ctrl['Time'] <= event_end)]
    all_times = sorted(ctrl_window['Time'].unique())
    active_count = ctrl_window[ctrl_window[WH_POWER_COL] > ONLINE_THRESHOLD_KW].groupby('Time').size()
    active_count = active_count.reindex(all_times, fill_value=0)
    idle_count = total_homes - active_count

    fig, ax = plt.subplots(figsize=(10, 5))
    ax2 = ax.twinx()

    ax.plot(active_count.index, active_count.values, color="#133CAD", linestyle='-', linewidth=1.3,
            label='Consuming power (controlled)')
    ax.plot(idle_count.index, idle_count.values, color="#ee5e0a", linestyle='--', linewidth=1.3,
            label='Idle (controlled)')

    ax.axvline(dispatch_t, color='red', linestyle=':', linewidth=1.5, label='Dispatch start')
    ax.axvline(event_end, color='gray', linestyle=':', linewidth=1.5, label='Event end')

    ax.set_title(f'Controlled Unit Status Over Time (DEC - Load-Up) (N={total_homes})')
    ax.set_xlabel('Time')
    ax.set_ylabel('Units (count)')
    ax2.set_ylabel('Units (% of fleet)')

    ax.set_ylim(0, total_homes)
    ax2.set_ylim(0, 100)

    ax.legend(loc='upper left', fontsize=8)

    ax.xaxis.set_major_locator(mdates.HourLocator(byhour=range(0, 24, 3)))
    ax.xaxis.set_major_formatter(mdates.DateFormatter('%H:%M'))

    output_dir = results_dir
    os.makedirs(output_dir, exist_ok=True)
    photo_file = os.path.join(output_dir, input_file_root + "_control_status_plot.png")
    plt.savefig(photo_file, dpi=300, bbox_inches='tight')
    print(f"Controlled unit status plot saved to: {photo_file}")
    return photo_file

#########################################
# PLOT: ADDED LOAD (DEMAND-INCREASE) PROFILE
#########################################

def compute_added_load_metrics(df_base, df_ctrl):
    """
    Same computation as the former plot_added_load_profile() -- no plot
    now, just the delta_p(t)/cumulative series and event-window metrics,
    still exported to CSV/JSON in MAIN EXECUTION.
    """
    base_fleet = df_base.groupby('Time')[WH_POWER_COL].sum()
    ctrl_fleet = df_ctrl.groupby('Time')[WH_POWER_COL].sum()
    delta_p = (ctrl_fleet - base_fleet).rename('delta_p_kw')

    dt_hours = infer_timestep_hours(df_ctrl)

    event_mask = (delta_p.index >= dispatch_t) & (delta_p.index < event_end)
    delta_p_event = delta_p[event_mask]
    avg_added_kw_event = delta_p_event.mean() if not delta_p_event.empty else float('nan')
    min_added_kw_event = delta_p_event.min() if not delta_p_event.empty else float('nan')

    added_load_kwh_event = delta_p_event.sum() * dt_hours if not delta_p_event.empty else float('nan')

    baseline_kwh_event = base_fleet[event_mask].sum() * dt_hours if not delta_p_event.empty else float('nan')
    pct_added_event = (added_load_kwh_event / baseline_kwh_event * 100) if baseline_kwh_event else float('nan')

    dip_mask = delta_p.index >= event_end
    delta_p_dip = delta_p[dip_mask]
    peak_dip_kw = delta_p_dip.min() if not delta_p_dip.empty else float('nan')

    cum_kwh = (delta_p.where(delta_p.index >= dispatch_t, 0.0) * dt_hours).cumsum()

    return (delta_p, cum_kwh, avg_added_kw_event, min_added_kw_event, peak_dip_kw,
            added_load_kwh_event, pct_added_event)

#########################################
# PARTICIPATION SUMMARY
#########################################

def print_participation_summary():
    """
    Reads <input_file_root>_release_summary.csv's 'participating' column
    -- written by ieee_gen_dec.py's own select_dispatched_homes() gate,
    the authoritative record of which units were actually selected for
    the power-capacity-capped dispatch (TARGET_FRACTION of the fleet).
    """
    release_summary_file = os.path.join(results_dir, input_file_root + "_release_summary.csv")
    if not os.path.exists(release_summary_file):
        print(f"(No release summary found at {release_summary_file} -- "
              f"participation rate unavailable.)")
        return None, None, float('nan'), []

    release_summary = pd.read_csv(release_summary_file)
    if 'participating' not in release_summary.columns:
        print("(Release summary found but has no 'participating' column -- "
              "an older run from before ieee_gen_dec.py's dispatch cap was added.)")
        return None, None, float('nan'), []

    n_participating = int(release_summary['participating'].sum())
    n_total = len(release_summary)
    pct_participating = n_participating / n_total * 100 if n_total else float('nan')
    participating_homes = release_summary.loc[release_summary['participating'] == True, 'Home'].tolist()

    print("=" * 50)
    print("PARTICIPATION -- IEEE DEC (power-capacity-capped dispatch)")
    print("=" * 50)
    print(f"Dispatched: {n_participating} / {n_total} units ({pct_participating:.1f}%)")
    print("=" * 50)

    return n_participating, n_total, pct_participating, participating_homes

#########################################
# AVERAGE UNIT STATUS (consuming power vs. idle, service period) --
# same windowing/threshold as plot_baseline_unit_status() /
# plot_control_unit_status(), just averaged into two numbers instead of
# plotted over time.
#########################################

def compute_avg_unit_status(df, total_homes):
    window = df[(df['Time'] >= dispatch_t) & (df['Time'] <= event_end)]
    all_times = sorted(window['Time'].unique())
    active_count = window[window[WH_POWER_COL] > ONLINE_THRESHOLD_KW].groupby('Time').size()
    active_count = active_count.reindex(all_times, fill_value=0)
    idle_count = total_homes - active_count
    return active_count.mean(), idle_count.mean()

#########################################
# PER-UNIT ACTIVITY (active minutes / dispatch cycles)
#########################################

def compute_unit_activity_stats(df_base, df_ctrl, participating_homes, active_threshold_kw=0.2):
    """
    For each participating home, over the service period [dispatch_t,
    event_end]: minutes where (controlled - baseline) power exceeds
    active_threshold_kw (a proxy for "currently under forced LOAD",
    distinct from the home's own normal baseline cycling), and the
    number of distinct active cycles (a rising edge in that mask -- each
    one is an admit -> shed/idle -> re-admit round). Returns
    (avg_active_minutes, avg_dispatch_cycles) averaged across
    participating homes.
    """
    if not participating_homes:
        return float('nan'), float('nan')

    dt_hours = infer_timestep_hours(df_ctrl)
    dt_minutes = dt_hours * 60

    pc = df_ctrl[df_ctrl['Home'].isin(participating_homes)].pivot_table(
        index='Time', columns='Home', values=WH_POWER_COL)
    pb = df_base[df_base['Home'].isin(participating_homes)].pivot_table(
        index='Time', columns='Home', values=WH_POWER_COL)

    window_mask = (pc.index >= dispatch_t) & (pc.index <= event_end)
    pc_w = pc.loc[window_mask]
    pb_w = pb.reindex(pc_w.index)[pc_w.columns]
    delta = pc_w - pb_w

    active_minutes = []
    dispatch_cycles = []
    for home in delta.columns:
        s = delta[home] > active_threshold_kw
        active_minutes.append(s.sum() * dt_minutes)
        rising_edges = (s & ~s.shift(1, fill_value=False)).sum()
        dispatch_cycles.append(rising_edges)

    avg_active_minutes = sum(active_minutes) / len(active_minutes)
    avg_dispatch_cycles = sum(dispatch_cycles) / len(dispatch_cycles)
    return avg_active_minutes, avg_dispatch_cycles

#########################################
# MAX SIMULTANEOUS ACTIVE UNITS
#########################################

def get_max_active_units():
    """
    Max simultaneous Active_Count (dispatched, not yet released) during
    the service period -- read from ieee_gen_dec.py's own per-timestep
    {filename}_deltaP_log.csv.
    """
    log_file = os.path.join(results_dir, input_file_root + "_deltaP_log.csv")
    try:
        log_df = pd.read_csv(log_file, parse_dates=['Time'])
    except FileNotFoundError:
        return float('nan')
    window = log_df[(log_df['Time'] >= dispatch_t) & (log_df['Time'] <= event_end)]
    return int(window['Active_Count'].max()) if not window.empty else float('nan')

#########################################
# TARGET TIMING (time to reach, hold duration, % in-band)
#########################################

def compute_timing_stats(delta_p_series):
    """
    From the fleet-aggregate delta_p(t) series, over [dispatch_t, event_end):
      - time_to_target_min: minutes from dispatch_t to the first time
        delta_p reaches TARGET_DELTA_KW - BAND_KW.
      - hold_duration_min: minutes delta_p then stays CONTINUOUSLY at or
        above that level before it first drops back below it (or to
        event_end, if it never drops out) -- a direct readout of the
        post-ramp dip, if any.
      - pct_in_band: % of timesteps from first-in-band onward (to
        event_end) spent within the +/- band (same "don't count the
        ramp-up against the hold" convention used in ieee_gen_dec.py).
    """
    event_mask = (delta_p_series.index >= dispatch_t) & (delta_p_series.index < event_end)
    s = delta_p_series[event_mask]

    at_or_above = s >= (TARGET_DELTA_KW - BAND_KW)
    if not at_or_above.any():
        return None, None, float('nan')

    first_in_band_time = s[at_or_above].index[0]
    time_to_target_min = (first_in_band_time - dispatch_t).total_seconds() / 60

    after = s[s.index >= first_in_band_time]
    out_of_band = after < (TARGET_DELTA_KW - BAND_KW)
    if out_of_band.any():
        first_exit_time = after[out_of_band].index[0]
        hold_duration_min = (first_exit_time - first_in_band_time).total_seconds() / 60
    else:
        hold_duration_min = (event_end - first_in_band_time).total_seconds() / 60

    in_band = after.between(TARGET_DELTA_KW - BAND_KW, TARGET_DELTA_KW + BAND_KW)
    pct_in_band = (in_band.mean() * 100) if len(in_band) else float('nan')

    return time_to_target_min, hold_duration_min, pct_in_band

#########################################
# MAIN EXECUTION
#########################################

# ---- C1: WH-power pivot (kept for parity/reference -- see module docstring) ----
run_wh_power_pivot()
print()

df_base, df_ctrl = load_data()
n_participating, n_total_units, pct_participating, participating_homes = print_participation_summary()
print()
metrics = compute_unit_metrics(df_base, df_ctrl)
print_summary(metrics)

output_dir = results_dir
os.makedirs(output_dir, exist_ok=True)
summary_out = {
    "input_folder": input_folder,
    "dispatch_time": DISPATCH_TIME,
    "duration_hr": EVENT_DURATION_HR,
    "home_id": metrics["home_id"],
    "reserve_capacity_kwh": metrics["reserve_capacity_kwh"],
    "sustain_time_hr": metrics["sustain_time_hr"],
    "baseline_sustain_time_hr": metrics["baseline_sustain_time_hr"],
    "baseline_active_energy_kwh": metrics["baseline_active_energy_kwh"],
    "controlled_active_energy_kwh": metrics["controlled_active_energy_kwh"],
    "delta_active_energy_kwh": metrics["delta_active_energy_kwh"],
}
with open(os.path.join(output_dir, input_file_root + "_unit_metrics.json"), "w") as f:
    json.dump(summary_out, f, indent=2)
print(f"Summary JSON written to: {os.path.join(output_dir, input_file_root + '_unit_metrics.json')}")

# ---- Fleet-level (same definitions, every home) ----
per_unit_df, fleet_summary = compute_fleet_metrics(df_base, df_ctrl)
print()
print_fleet_summary(fleet_summary)
fleet_combined_photo_file = plot_fleet_activation_combined()
fleet_power_event_photo_file = plot_fleet_power_with_event(df_base, df_ctrl)
fleet_power_24hr_photo_file = plot_fleet_power_24hr(df_base, df_ctrl)
deltaP_photo_file = plot_fleet_power_event_window(df_base, df_ctrl)
active_units_photo_file = plot_active_units(df_ctrl['Home'].nunique())
baseline_status_photo_file = plot_baseline_unit_status(df_base, df_ctrl['Home'].nunique())
control_status_photo_file = plot_control_unit_status(df_ctrl, df_ctrl['Home'].nunique())

(delta_p_series, cum_kwh_series, avg_added_kw_event, min_added_kw_event, peak_dip_kw,
 added_load_kwh_event, pct_added_event) = \
    compute_added_load_metrics(df_base, df_ctrl)
print()
print("--- Added load (demand-increase), event window ---")
print(f"  Average added kW: {avg_added_kw_event:.3f} kW")
print(f"  Minimum added kW: {min_added_kw_event:.3f} kW")
print(f"  Added load (area): {added_load_kwh_event:.3f} kWh")
print(f"  Added: {pct_added_event:.1f}% of baseline energy (event)")
print(f"  Peak post-event dip: {peak_dip_kw:.3f} kW (negative delta_p)")

added_load_csv = os.path.join(output_dir, input_file_root + "_added_load_profile.csv")
pd.DataFrame({'delta_p_kw': delta_p_series, 'cumulative_added_kwh': cum_kwh_series}).to_csv(added_load_csv)
print(f"Added load profile CSV written to: {added_load_csv}")

per_unit_csv = os.path.join(output_dir, input_file_root + "_per_unit_metrics.csv")
per_unit_df.to_csv(per_unit_csv)
print(f"Per-unit metrics CSV written to: {per_unit_csv}")

fleet_summary_out = {
    "input_folder": input_folder,
    "dispatch_time": DISPATCH_TIME,
    "duration_hr": EVENT_DURATION_HR,
    **fleet_summary,
    "avg_added_kw_event": float(avg_added_kw_event),
    "min_added_kw_event": float(min_added_kw_event),
    "added_load_kwh_event": float(added_load_kwh_event),
    "pct_added_event": float(pct_added_event),
    "peak_dip_kw": float(peak_dip_kw),
    "n_participating": n_participating,
    "n_total_units": n_total_units,
    "pct_participating": float(pct_participating),
}
fleet_json = os.path.join(output_dir, input_file_root + "_fleet_metrics.json")
with open(fleet_json, "w") as f:
    json.dump(fleet_summary_out, f, indent=2)
print(f"Fleet summary JSON written to: {fleet_json}")

# ---- Flex Load Capacity (FLC) ----
flc_summary = compute_flex_load_capacity(fleet_summary)
print()
print_flc_summary(flc_summary)

flc_json = os.path.join(output_dir, input_file_root + "_flc_summary.json")
with open(flc_json, "w") as f:
    json.dump(flc_summary, f, indent=2)
print(f"FLC summary JSON written to: {flc_json}")

# ---- Curated results summary -- ONLY the handful of numbers that
# matter for comparing test configs at a glance (fleet size, target,
# dispatch participation, timing, power range, per-unit behavior) --
# written to its own text file instead of teeing everything this script
# prints (which included a lot of noise not needed for that comparison). ----
avg_active_minutes, avg_dispatch_cycles = compute_unit_activity_stats(df_base, df_ctrl, participating_homes)
max_active_units = get_max_active_units()
pct_max_active = (max_active_units / n_total_units * 100) if n_total_units else float('nan')
time_to_target_min, hold_duration_min, pct_in_band = compute_timing_stats(delta_p_series)
max_event_mask = (delta_p_series.index >= dispatch_t) & (delta_p_series.index < event_end)
max_added_kw_event = delta_p_series[max_event_mask].max() if max_event_mask.any() else float('nan')
n_never_dispatched = n_total_units - n_participating

avg_base_active, avg_base_idle = compute_avg_unit_status(df_base, n_total_units)
avg_ctrl_active, avg_ctrl_idle = compute_avg_unit_status(df_ctrl, n_total_units)

summary_lines = [
    "IEEE DEC -- Results Summary",
    "=" * 60,
    f"Test: {input_file_root}",
    f"Fleet size: {n_total_units} homes",
    f"Target delta_p: {TARGET_DELTA_KW} kW (band +/- {BAND_KW} kW)",
    "",
    "--- Dispatch ---",
    f"Units dispatched: {n_participating} / {n_total_units} ({pct_participating:.1f}%)",
    f"Units never dispatched: {n_never_dispatched}",
    f"Max simultaneous active units: {max_active_units} ({pct_max_active:.1f}% of fleet)",
    "",
    "--- Timing ---",
    f"Time to reach target: {time_to_target_min:.1f} min after dispatch" if time_to_target_min is not None
        else "Time to reach target: target - band was never reached",
    f"Time fleet held target (continuous): {hold_duration_min:.1f} min" if hold_duration_min is not None
        else "Time fleet held target (continuous): n/a",
    f"% of time in-band (from first reaching target): {pct_in_band:.1f}%",
    "",
    "--- Power (delta_p, service period) ---",
    f"Average: {avg_added_kw_event:.3f} kW",
    f"Minimum: {min_added_kw_event:.3f} kW",
    f"Maximum: {max_added_kw_event:.3f} kW",
    "",
    "--- Per-unit behavior (dispatched units only) ---",
    f"Average active time: {avg_active_minutes:.1f} min",
    f"Average dispatch cycles: {avg_dispatch_cycles:.2f}",
    "",
    "--- Unit Status (service period average, whole fleet) ---",
    f"Baseline -- consuming power: {avg_base_active:.1f} ({avg_base_active / n_total_units * 100:.1f}%), "
        f"idle: {avg_base_idle:.1f} ({avg_base_idle / n_total_units * 100:.1f}%)",
    f"Controlled -- consuming power: {avg_ctrl_active:.1f} ({avg_ctrl_active / n_total_units * 100:.1f}%), "
        f"idle: {avg_ctrl_idle:.1f} ({avg_ctrl_idle / n_total_units * 100:.1f}%)",
    "=" * 60,
]
results_summary_path = os.path.join(output_dir, input_file_root + "_results_output.txt")
with open(results_summary_path, "w") as f:
    f.write("\n".join(summary_lines) + "\n")
print(f"Results summary written to: {results_summary_path}")
