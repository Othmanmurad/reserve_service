# -*- coding: utf-8 -*-
"""
Author: Othman
Results script for ieee_gen_inc.py's IEEE generation-INCREMENT (INC /
SHED, i.e. downward-load / upward-reserve) delta-P dispatch study -- see
that file's own module docstring for the full mechanics. Mirror image
of ieee_dec_results.py: every "added load" concept there becomes a
"reduction" here (reduction(t) = baseline(t) - controlled(t), positive
while shedding effectively), pipeline="IEEE_INC" instead of "IEEE_DEC"
so this never collides with ieee_gen_dec.py's own run_context state.
Reserve Capacity/Sustain Time/FLC (compressor-active-minutes concepts)
are UNCHANGED math-wise -- they measure absolute activity, not the
signed delta, so they read correctly in either direction without
modification.
"""

import pandas as pd
import os
import json
import matplotlib.dates as mdates
import matplotlib.pyplot as plt
from run_context import load_filename, load_reserve_event, load_input_folder
from ieee_pes_common import INC_RESULTS_ROOT

#########################################
# PATH SETUP
#########################################

working_dir = os.path.dirname(os.path.abspath(__file__))

# OCHRE_FILENAME overrides this when set; unset, inherit whichever run
# ieee_gen_inc.py last did (see run_context.py, pipeline="IEEE_INC").
input_file_root = os.environ.get('OCHRE_FILENAME') or load_filename('IEEE_INC_Test_100_1', pipeline="IEEE_INC")

input_file_name1 = input_file_root + "_baseline"
input_file_name2 = input_file_root + "_controlled"

results_dir = os.path.join(INC_RESULTS_ROOT, input_file_root)
os.makedirs(results_dir, exist_ok=True)

input_file_1 = os.path.join(results_dir, input_file_name1 + ".csv")
input_file_2 = os.path.join(results_dir, input_file_name2 + ".csv")

input_folder = load_input_folder('Input_Files', pipeline="IEEE_INC")

#########################################
# RESERVE EVENT SETTINGS
#########################################

DISPATCH_DATE = '2018-01-12'   # fixed event date convention (see ieee_gen_inc.py's own EVENT_DATE comment)

_default_reserve_event = {'dispatch_time': '14:00', 'duration': 4.0}

_run_config_file = os.path.join(results_dir, input_file_root + "_run_config.json")
try:
    with open(_run_config_file) as f:
        _reserve_event = json.load(f)
except (FileNotFoundError, json.JSONDecodeError):
    _reserve_event = load_reserve_event(_default_reserve_event, pipeline="IEEE_INC")

DISPATCH_TIME = os.environ.get('OCHRE_DISPATCH_TIME') or _reserve_event['dispatch_time']
EVENT_DURATION_HR = float(os.environ.get('OCHRE_DURATION_HR') or _reserve_event['duration'])

dispatch_t = pd.to_datetime(f"{DISPATCH_DATE} {DISPATCH_TIME}")
event_end = dispatch_t + pd.Timedelta(hours=EVENT_DURATION_HR)

#########################################
# SETTINGS
#########################################

WH_POWER_COL = "Water Heating Electric Power (kW)"
ONLINE_THRESHOLD_KW = 0.1
PLOT_MARGIN_HR = 2.0

# Target reduction (positive magnitude) and band -- loaded from whatever
# ieee_gen_inc.py actually used for this run.
TARGET_REDUCTION_KW = float(os.environ.get('OCHRE_TARGET_REDUCTION_KW') or _reserve_event.get('target_reduction_kw', 100.0))
BAND_KW = float(os.environ.get('OCHRE_BAND_KW') or _reserve_event.get('band_kw', 0.5))

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
    home_id = df['Home'].iloc[0]
    times = df.loc[df['Home'] == home_id, 'Time'].sort_values()
    if len(times) < 2:
        return 1 / 60
    return times.diff().dropna().dt.total_seconds().median() / 3600

#########################################
# PER-UNIT RESERVE CAPACITY / SUSTAIN TIME (unchanged math -- absolute
# activity, not the signed delta; see module docstring)
#########################################

def compute_unit_metrics(df_base, df_ctrl):
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

def print_summary(metrics):
    print("=" * 50)
    print("PER-UNIT RESERVE CAPACITY / SUSTAIN TIME -- IEEE INC")
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
# FLEET-LEVEL RESERVE CAPACITY / SUSTAIN TIME (unchanged math)
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
# FLEX LOAD CAPACITY (FLC) -- disabled for now, same status as ieee_dec_results.py
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
    print("FLEX LOAD CAPACITY (FLC) -- IEEE INC")
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

def print_fleet_summary(fleet_summary):
    print("=" * 50)
    print("FLEET RESERVE CAPACITY / SUSTAIN TIME -- IEEE INC")
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

    ax.set_title(f'Fleet Water Heater Activation (INC - Shed) (N={df_ctrl["Home"].nunique()})')
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

    ax.set_title(f'Aggregated Fleet Water Heater Power During Reserve Service Event (INC - Shed) (N={df_ctrl["Home"].nunique()})')
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
# PLOT: REDUCTION OVER THE FULL 24HR WINDOW
#########################################

def plot_fleet_power_24hr(df_base, df_ctrl):
    """reduction(t) = baseline - controlled, summed across the fleet, over
    the full simulated day (positive while shedding effectively)."""
    base_fleet = df_base.groupby('Time')[WH_POWER_COL].sum()
    ctrl_fleet = df_ctrl.groupby('Time')[WH_POWER_COL].sum()
    reduction = (base_fleet - ctrl_fleet).rename('reduction_kw')

    fig, ax = plt.subplots(figsize=(10, 5))

    ax.axvspan(dispatch_t, event_end, color='green', alpha=0.08, label='Event window')
    ax.axvspan(event_end, reduction.index.max(), color='orange', alpha=0.08, label='Post-event')

    ax.plot(reduction.index, reduction.values, color="#133CAD", linestyle='-', linewidth=1.3,
            label='reduction(t) = baseline - controlled')
    ax.axhline(0, color='black', linestyle='-', linewidth=0.8)
    ax.axhline(TARGET_REDUCTION_KW, color='black', linestyle='--', linewidth=1.3,
               label=f'Target = {TARGET_REDUCTION_KW} kW')

    ax.axvline(dispatch_t, color='red', linestyle=':', linewidth=1.5, label='Dispatch start')
    ax.axvline(event_end, color='gray', linestyle=':', linewidth=1.5, label='Event end')

    ax.set_title(f'Load Reduction Over the Full Day (INC - Shed) (N={df_ctrl["Home"].nunique()})')
    ax.set_xlabel('Time')
    ax.set_ylabel('Reduction vs. baseline (kW)')
    ax.legend(loc='upper left', fontsize=8)

    # Scale the y-axis to the ACTUAL reduction data, not to wherever the
    # target line (above) happens to sit -- matplotlib's autoscale
    # otherwise includes axhline(TARGET_REDUCTION_KW) in its data limits,
    # so an intentionally-unreachable target (e.g. the ideal/benchmark
    # tests' 100,000 kW) silently stretches the axis and flattens the
    # real curve into an invisible line near zero.
    data_min, data_max = reduction.min(), reduction.max()
    y_pad = max(1.0, 0.1 * (data_max - data_min))
    ax.set_ylim(min(0, data_min) - y_pad, data_max + y_pad)

    ax.xaxis.set_major_locator(mdates.HourLocator(byhour=range(0, 24, 3)))
    ax.xaxis.set_major_formatter(mdates.DateFormatter('%H:%M'))

    output_dir = results_dir
    os.makedirs(output_dir, exist_ok=True)
    photo_file = os.path.join(output_dir, input_file_root + "_fleet_power_24hr_plot.png")
    plt.savefig(photo_file, dpi=300, bbox_inches='tight')
    print(f"Reduction (24hr) plot saved to: {photo_file}")
    return photo_file


def plot_fleet_power_event_window(df_base, df_ctrl):
    """reduction(t) = baseline - controlled, windowed to the event +/-
    PLOT_MARGIN_HR -- replaces ieee_gen_inc.py's own {filename}_deltaP.png
    IN PLACE (same filename)."""
    plot_start = dispatch_t - pd.Timedelta(hours=PLOT_MARGIN_HR)
    plot_end = event_end + pd.Timedelta(hours=PLOT_MARGIN_HR)

    base_window = df_base[(df_base['Time'] >= plot_start) & (df_base['Time'] <= plot_end)]
    ctrl_window = df_ctrl[(df_ctrl['Time'] >= plot_start) & (df_ctrl['Time'] <= plot_end)]
    base_fleet = base_window.groupby('Time')[WH_POWER_COL].sum()
    ctrl_fleet = ctrl_window.groupby('Time')[WH_POWER_COL].sum()
    reduction = (base_fleet - ctrl_fleet).rename('reduction_kw')

    fig, ax = plt.subplots(figsize=(10, 5))

    ax.axvspan(dispatch_t, event_end, color='green', alpha=0.08, label='Event window')
    ax.axvspan(event_end, plot_end, color='orange', alpha=0.08, label='Post-event')

    ax.plot(reduction.index, reduction.values, color="#133CAD", linestyle='-', linewidth=1.3,
            label='reduction(t) = baseline - controlled')
    ax.axhline(0, color='black', linestyle='-', linewidth=0.8)
    ax.axhline(TARGET_REDUCTION_KW, color='black', linestyle='--', linewidth=1.3,
               label=f'Target = {TARGET_REDUCTION_KW} kW')

    ax.axvline(dispatch_t, color='red', linestyle=':', linewidth=1.5, label='Dispatch start')
    ax.axvline(event_end, color='gray', linestyle=':', linewidth=1.5, label='Event end')

    ax.set_title(f'Shed Dispatch -- Load Reduction vs. Target (INC - Shed) (N={df_ctrl["Home"].nunique()})')
    ax.set_xlabel('Time')
    ax.set_ylabel('Reduction vs. baseline (kW)')
    ax.legend(loc='upper left', fontsize=8)
    ax.set_xlim(plot_start, plot_end)

    # See plot_fleet_power_24hr()'s matching comment -- scale to the
    # actual data, not to an intentionally-unreachable target line.
    data_min, data_max = reduction.min(), reduction.max()
    y_pad = max(1.0, 0.1 * (data_max - data_min))
    ax.set_ylim(min(0, data_min) - y_pad, data_max + y_pad)

    ax.xaxis.set_major_locator(mdates.HourLocator(interval=1))
    ax.xaxis.set_major_formatter(mdates.DateFormatter('%H:%M'))

    output_dir = results_dir
    os.makedirs(output_dir, exist_ok=True)
    photo_file = os.path.join(output_dir, input_file_root + "_deltaP.png")
    plt.savefig(photo_file, dpi=300, bbox_inches='tight')
    print(f"Reduction plot (event window) saved to: {photo_file}")
    return photo_file

#########################################
# PLOT: ACTIVE (SHED) UNITS OVER TIME
#########################################

def plot_active_units(total_homes):
    """Active_Count(t) -- number of units currently SHED and NOT YET
    RELEASED -- read directly from ieee_gen_inc.py's own per-timestep
    {filename}_deltaP_log.csv."""
    log_file = os.path.join(results_dir, input_file_root + "_deltaP_log.csv")
    try:
        log_df = pd.read_csv(log_file, parse_dates=['Time'])
    except FileNotFoundError:
        print(f"(No per-timestep log found at {log_file} -- active-units plot skipped.)")
        return None

    log_df = log_df[(log_df['Time'] >= dispatch_t) & (log_df['Time'] <= event_end)]

    fig, ax = plt.subplots(figsize=(10, 5))
    ax2 = ax.twinx()

    ax.plot(log_df['Time'], log_df['Active_Count'], color="#133CAD", linestyle='-', linewidth=1.3,
            label='Active units (shed, not released)')

    ax.axvline(dispatch_t, color='red', linestyle=':', linewidth=1.5, label='Dispatch start')
    ax.axvline(event_end, color='gray', linestyle=':', linewidth=1.5, label='Event end')

    ax.set_title(f'Active Shed Units Over Time (INC - Shed) (N={total_homes})')
    ax.set_xlabel('Time')
    ax.set_ylabel('Active units (count)')
    ax2.set_ylabel('Active units (% of fleet)')

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
    ieee_gen_inc.py's dispatch loop draws from every timestep (a fresh
    SHED candidate must be actively drawing baseline power right now --
    see BASELINE_ACTIVE_THRESHOLD_KW, same 0.1kW threshold as
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

    ax.set_title(f'Baseline Unit Status Over Time (INC - Shed) (N={total_homes})')
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
    for a dispatched home it reflects the low "shed"/"recovery" draw
    while parked, or baseline activity again once released -- so this
    plot shows the REAL effect of the dispatch mechanism on fleet-wide
    activity, directly comparable to the baseline-status plot alongside
    it (mirrors ieee_dec_results.py's own plot_control_unit_status()).
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

    ax.set_title(f'Controlled Unit Status Over Time (INC - Shed) (N={total_homes})')
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
# REDUCTION PROFILE (mirror of ieee_dec_results.py's added-load profile)
#########################################

def compute_reduction_metrics(df_base, df_ctrl):
    """reduction(t) = baseline - controlled (positive while shedding
    effectively). Mirrors compute_added_load_metrics() in
    ieee_dec_results.py, sign-flipped."""
    base_fleet = df_base.groupby('Time')[WH_POWER_COL].sum()
    ctrl_fleet = df_ctrl.groupby('Time')[WH_POWER_COL].sum()
    reduction = (base_fleet - ctrl_fleet).rename('reduction_kw')

    dt_hours = infer_timestep_hours(df_ctrl)

    event_mask = (reduction.index >= dispatch_t) & (reduction.index < event_end)
    reduction_event = reduction[event_mask]
    avg_reduction_kw_event = reduction_event.mean() if not reduction_event.empty else float('nan')
    min_reduction_kw_event = reduction_event.min() if not reduction_event.empty else float('nan')

    reduced_load_kwh_event = reduction_event.sum() * dt_hours if not reduction_event.empty else float('nan')

    baseline_kwh_event = base_fleet[event_mask].sum() * dt_hours if not reduction_event.empty else float('nan')
    pct_reduced_event = (reduced_load_kwh_event / baseline_kwh_event * 100) if baseline_kwh_event else float('nan')

    dip_mask = reduction.index >= event_end
    reduction_dip = reduction[dip_mask]
    peak_rebound_kw = reduction_dip.min() if not reduction_dip.empty else float('nan')

    cum_kwh = (reduction.where(reduction.index >= dispatch_t, 0.0) * dt_hours).cumsum()

    return (reduction, cum_kwh, avg_reduction_kw_event, min_reduction_kw_event, peak_rebound_kw,
            reduced_load_kwh_event, pct_reduced_event)

#########################################
# PARTICIPATION SUMMARY
#########################################

def print_participation_summary():
    release_summary_file = os.path.join(results_dir, input_file_root + "_release_summary.csv")
    if not os.path.exists(release_summary_file):
        print(f"(No release summary found at {release_summary_file} -- "
              f"participation rate unavailable.)")
        return None, None, float('nan'), []

    release_summary = pd.read_csv(release_summary_file)
    if 'participating' not in release_summary.columns:
        print("(Release summary found but has no 'participating' column.)")
        return None, None, float('nan'), []

    n_participating = int(release_summary['participating'].sum())
    n_total = len(release_summary)
    pct_participating = n_participating / n_total * 100 if n_total else float('nan')
    participating_homes = release_summary.loc[release_summary['participating'] == True, 'Home'].tolist()

    print("=" * 50)
    print("PARTICIPATION -- IEEE INC (power-capacity-capped shed dispatch)")
    print("=" * 50)
    print(f"Dispatched: {n_participating} / {n_total} units ({pct_participating:.1f}%)")
    print("=" * 50)

    return n_participating, n_total, pct_participating, participating_homes

#########################################
# PER-UNIT ACTIVITY (active minutes / dispatch cycles)
#########################################

def compute_unit_activity_stats(df_base, df_ctrl, participating_homes, active_threshold_kw=0.2):
    """For each participating home, over the service period: minutes
    where (baseline - controlled) power exceeds active_threshold_kw (a
    proxy for "currently under forced SHED"), and the number of distinct
    shed cycles (a rising edge in that mask). Mirrors
    ieee_dec_results.py's own version, sign-flipped."""
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
    reduction = pb_w - pc_w

    active_minutes = []
    dispatch_cycles = []
    for home in reduction.columns:
        s = reduction[home] > active_threshold_kw
        active_minutes.append(s.sum() * dt_minutes)
        rising_edges = (s & ~s.shift(1, fill_value=False)).sum()
        dispatch_cycles.append(rising_edges)

    avg_active_minutes = sum(active_minutes) / len(active_minutes)
    avg_dispatch_cycles = sum(dispatch_cycles) / len(dispatch_cycles)
    return avg_active_minutes, avg_dispatch_cycles

#########################################
# MAX SIMULTANEOUS ACTIVE (SHED) UNITS
#########################################

def get_max_active_units():
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

def compute_timing_stats(reduction_series):
    """From the fleet-aggregate reduction(t) series, over [dispatch_t,
    event_end): mirrors ieee_dec_results.py's compute_timing_stats()
    exactly, operating on the reduction magnitude instead of delta_p."""
    event_mask = (reduction_series.index >= dispatch_t) & (reduction_series.index < event_end)
    s = reduction_series[event_mask]

    at_or_above = s >= (TARGET_REDUCTION_KW - BAND_KW)
    if not at_or_above.any():
        return None, None, float('nan')

    first_in_band_time = s[at_or_above].index[0]
    time_to_target_min = (first_in_band_time - dispatch_t).total_seconds() / 60

    after = s[s.index >= first_in_band_time]
    out_of_band = after < (TARGET_REDUCTION_KW - BAND_KW)
    if out_of_band.any():
        first_exit_time = after[out_of_band].index[0]
        hold_duration_min = (first_exit_time - first_in_band_time).total_seconds() / 60
    else:
        hold_duration_min = (event_end - first_in_band_time).total_seconds() / 60

    in_band = after.between(TARGET_REDUCTION_KW - BAND_KW, TARGET_REDUCTION_KW + BAND_KW)
    pct_in_band = (in_band.mean() * 100) if len(in_band) else float('nan')

    return time_to_target_min, hold_duration_min, pct_in_band

#########################################
# MAIN EXECUTION
#########################################

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

(reduction_series, cum_kwh_series, avg_reduction_kw_event, min_reduction_kw_event, peak_rebound_kw,
 reduced_load_kwh_event, pct_reduced_event) = \
    compute_reduction_metrics(df_base, df_ctrl)
print()
print("--- Load reduction, event window ---")
print(f"  Average reduction: {avg_reduction_kw_event:.3f} kW")
print(f"  Minimum reduction: {min_reduction_kw_event:.3f} kW")
print(f"  Reduced load (area): {reduced_load_kwh_event:.3f} kWh")
print(f"  Reduced: {pct_reduced_event:.1f}% of baseline energy (event)")
print(f"  Peak post-event rebound: {peak_rebound_kw:.3f} kW (negative reduction)")

reduction_csv = os.path.join(output_dir, input_file_root + "_reduction_profile.csv")
pd.DataFrame({'reduction_kw': reduction_series, 'cumulative_reduced_kwh': cum_kwh_series}).to_csv(reduction_csv)
print(f"Reduction profile CSV written to: {reduction_csv}")

per_unit_csv = os.path.join(output_dir, input_file_root + "_per_unit_metrics.csv")
per_unit_df.to_csv(per_unit_csv)
print(f"Per-unit metrics CSV written to: {per_unit_csv}")

fleet_summary_out = {
    "input_folder": input_folder,
    "dispatch_time": DISPATCH_TIME,
    "duration_hr": EVENT_DURATION_HR,
    **fleet_summary,
    "avg_reduction_kw_event": float(avg_reduction_kw_event),
    "min_reduction_kw_event": float(min_reduction_kw_event),
    "reduced_load_kwh_event": float(reduced_load_kwh_event),
    "pct_reduced_event": float(pct_reduced_event),
    "peak_rebound_kw": float(peak_rebound_kw),
    "n_participating": n_participating,
    "n_total_units": n_total_units,
    "pct_participating": float(pct_participating),
}
fleet_json = os.path.join(output_dir, input_file_root + "_fleet_metrics.json")
with open(fleet_json, "w") as f:
    json.dump(fleet_summary_out, f, indent=2)
print(f"Fleet summary JSON written to: {fleet_json}")

flc_summary = compute_flex_load_capacity(fleet_summary)
print()
print_flc_summary(flc_summary)

flc_json = os.path.join(output_dir, input_file_root + "_flc_summary.json")
with open(flc_json, "w") as f:
    json.dump(flc_summary, f, indent=2)
print(f"FLC summary JSON written to: {flc_json}")

# ---- Curated results summary -- mirrors ieee_dec_results.py's own. ----
avg_active_minutes, avg_dispatch_cycles = compute_unit_activity_stats(df_base, df_ctrl, participating_homes)
max_active_units = get_max_active_units()
pct_max_active = (max_active_units / n_total_units * 100) if n_total_units else float('nan')
time_to_target_min, hold_duration_min, pct_in_band = compute_timing_stats(reduction_series)
max_event_mask = (reduction_series.index >= dispatch_t) & (reduction_series.index < event_end)
max_reduction_kw_event = reduction_series[max_event_mask].max() if max_event_mask.any() else float('nan')
n_never_dispatched = n_total_units - n_participating

# Average fraction of the fleet actually drawing power (>ONLINE_THRESHOLD_KW)
# vs. idle/shed (no draw) over the service period -- same power-draw basis
# as plot_control_unit_status(), averaged over the event window.
_ctrl_status_window = df_ctrl[(df_ctrl['Time'] >= dispatch_t) & (df_ctrl['Time'] <= event_end)]
_consuming_count = _ctrl_status_window[_ctrl_status_window[WH_POWER_COL] > ONLINE_THRESHOLD_KW].groupby('Time').size()
_consuming_count = _consuming_count.reindex(sorted(_ctrl_status_window['Time'].unique()), fill_value=0)
avg_consuming_pct = _consuming_count.mean() / n_total_units * 100
avg_idle_shed_pct = 100 - avg_consuming_pct

summary_lines = [
    "IEEE INC -- Results Summary",
    "=" * 60,
    f"Test: {input_file_root}",
    f"Fleet size: {n_total_units} homes",
    f"Target reduction: {TARGET_REDUCTION_KW} kW (band +/- {BAND_KW} kW)",
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
    "--- Power (reduction, service period) ---",
    f"Average: {avg_reduction_kw_event:.3f} kW",
    f"Minimum: {min_reduction_kw_event:.3f} kW",
    f"Maximum: {max_reduction_kw_event:.3f} kW",
    "",
    "--- Power-Draw Status (service period, controlled) ---",
    f"Average consuming power (active draw): {avg_consuming_pct:.1f}% of fleet",
    f"Average idle/shed (no draw): {avg_idle_shed_pct:.1f}% of fleet",
    "",
    "--- Per-unit behavior (dispatched units only) ---",
    f"Average active time: {avg_active_minutes:.1f} min",
    f"Average dispatch cycles: {avg_dispatch_cycles:.2f}",
    "=" * 60,
]
results_summary_path = os.path.join(output_dir, input_file_root + "_results_output.txt")
with open(results_summary_path, "w") as f:
    f.write("\n".join(summary_lines) + "\n")
print(f"Results summary written to: {results_summary_path}")
