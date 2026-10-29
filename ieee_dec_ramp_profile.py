# -*- coding: utf-8 -*-
"""
Large-scale counterpart of FlexLoadCalculator's ieee_dec_ramp_profile.py --
comparison plots across a hand-picked set of IEEE DEC tests already run by
ieee_gen_dec.py, all sharing the same dispatch start time so their ramp-up
/ sustain / ramp-down shapes line up on a common elapsed-time axis. Reads
each test's saved files from its own <DEC_RESULTS_ROOT>/<test name>/ folder
(no re-simulation):

  {name}_run_config.json      -- dispatch time, duration, target
  {name}_deltaP_log.csv       -- Time, DeltaP_kW, Active_Count (full day)
  {name}_release_summary.csv  -- one row per unit (fleet size, admissions)
  {name}_baseline_WH_power.csv -- per-unit baseline WH power by HH:MM

so the TESTS list below only needs each test's folder name -- target,
duration and fleet size come from the test itself.

  1. 2D (raw): elapsed time (hr) vs. fleet delta_p (kW) -- one line per
     test, showing the ramp-up, hold, and release-driven ramp-down shape.
  2. 2D (smoothed): same axes/style as (1), each curve replaced by its
     rolling average.
  3. Normalized: delta_p/target vs. elapsed/duration, event window only.
  4. Response time & baseline status at the moment of dispatch.

Outputs are written to DEC_RESULTS_ROOT itself (next to the test folders).
"""

import os
import json
import pandas as pd
import matplotlib.pyplot as plt
from ieee_pes_common import DEC_RESULTS_ROOT

#########################################
# TESTS TO COMPARE -- folder names under DEC_RESULTS_ROOT (= the
# `filename` each ieee_gen_dec.py run printed / saved under).
# Optional label: ("label", "folder"); a bare "folder" gets an automatic
# "<N> units, <target>kW/<duration>h" label.
#########################################

TESTS = [
    "IEEE_DEC_1000_120kW_1h",
    #("10k test", "IEEE_DEC_10000_2000kW_6h"),
]

COLORS = ["black", "#133CAD", "#ee5e0a", "#2ca02c", "#d62728",
          "#9467bd", "#8c564b", "#17becf", "#7f7f7f"]

ROLLING_WINDOW_MIN = 3


def test_file(folder, suffix):
    return os.path.join(DEC_RESULTS_ROOT, folder, folder + suffix)


def load_test(entry):
    label, folder = entry if isinstance(entry, tuple) else (None, entry)

    with open(test_file(folder, "_run_config.json")) as f:
        cfg = json.load(f)
    target = float(cfg['target_delta_kw'])
    dur = float(cfg['duration'])

    df = pd.read_csv(test_file(folder, "_deltaP_log.csv"), parse_dates=['Time']).sort_values('Time')
    dispatch_t = pd.to_datetime(f"{df['Time'].iloc[0].date()} {cfg['dispatch_time']}")
    df = df[df['Time'] >= dispatch_t]
    elapsed_hr = (df['Time'] - dispatch_t).dt.total_seconds() / 3600

    n_units = len(pd.read_csv(test_file(folder, "_release_summary.csv"), usecols=['Home']))
    if label is None:
        label = f"{n_units} units, {target:g}kW/{dur:g}h"

    return {
        'label': label, 'folder': folder, 'target': target, 'dur': dur,
        'dispatch_t': dispatch_t, 'n_units': n_units,
        't': elapsed_hr.values, 'dp': df['DeltaP_kW'].values, 'ac': df['Active_Count'].values,
    }


tests = [load_test(entry) for entry in TESTS]

fleet_sizes = sorted({tr['n_units'] for tr in tests})
fleet_text = f"N={fleet_sizes[0]}" if len(fleet_sizes) == 1 else "N=" + "/".join(map(str, fleet_sizes))
dispatch_times = sorted({tr['dispatch_t'].strftime('%H:%M') for tr in tests})
dispatch_text = dispatch_times[0] if len(dispatch_times) == 1 else ", ".join(dispatch_times)

#########################################
# PLOT 1 -- 2D: time vs. magnitude
#########################################

fig1, ax1 = plt.subplots(figsize=(11, 6))
for tr, color in zip(tests, COLORS):
    ax1.plot(tr['t'], tr['dp'], color=color, linewidth=1.3, label=tr['label'])

ax1.axhline(0, color='black', linestyle='-', linewidth=0.8)
ax1.set_xlabel('Elapsed time since dispatch (hours)')
ax1.set_ylabel('Reserve magnitude (kW)')
ax1.set_title('Fleet Reserve Magnitude Over Time \n'
              f'(DEC - Load-Up, {fleet_text}, all tests dispatched at {dispatch_text})')
ax1.legend(loc='upper right', fontsize=8)
ax1.set_xlim(left=0)

plot1_file = os.path.join(DEC_RESULTS_ROOT, "IEEE_DEC_RampProfile_2D.png")
plt.savefig(plot1_file, dpi=300, bbox_inches='tight')
print(f"2D ramp profile plot saved to: {plot1_file}")

#########################################
# PLOT 2 -- 2D, same axes/style as Plot 1, but each curve replaced by a
# rolling average (centered) of delta_p(t) -- smooths out the
# per-timestep admission/release noise so the underlying ramp-up / sustain
# / ramp-down SHAPE is easier to read and compare across tests.
#########################################

fig1b, ax1b = plt.subplots(figsize=(11, 6))
for tr, color in zip(tests, COLORS):
    dp_avg = pd.Series(tr['dp']).rolling(ROLLING_WINDOW_MIN, min_periods=1, center=True).mean()
    ax1b.plot(tr['t'], dp_avg.values, color=color, linewidth=1.5, label=tr['label'])

ax1b.axhline(0, color='black', linestyle='-', linewidth=0.8)
ax1b.set_xlabel('Elapsed time since dispatch (hours)')
ax1b.set_ylabel('Reserve magnitude (kW)')
ax1b.set_title('Fleet Reserve Magnitude Over Time\n'
               f'(DEC - Load-Up, {fleet_text}, all tests dispatched at {dispatch_text})')
ax1b.legend(loc='upper right', fontsize=8)
ax1b.set_xlim(left=0)

plot1b_file = os.path.join(DEC_RESULTS_ROOT, "IEEE_DEC_RampProfile_2D_Averaged.png")
plt.savefig(plot1b_file, dpi=300, bbox_inches='tight')
print(f"Averaged 2D ramp profile plot saved to: {plot1b_file}")

#########################################
# PLOT 3 -- normalized: delta_p/target vs. elapsed/duration, EVENT WINDOW
# ONLY (not the post-event ramp-down tail) -- puts every test on the same
# 0-1 x ~0-1 scale so ramp speed and hold quality (sag) are directly
# comparable regardless of actual target/duration/fleet size.
#########################################

fig3, ax3 = plt.subplots(figsize=(11, 6))
for tr, color in zip(tests, COLORS):
    mask = tr['t'] <= tr['dur']
    t_norm = tr['t'][mask] / tr['dur']
    dp_norm = tr['dp'][mask] / tr['target']
    ax3.plot(t_norm, dp_norm, color=color, linewidth=1.3, label=tr['label'])

ax3.axhline(1.0, color='black', linestyle='--', linewidth=1.0, label='Target (normalized = 1.0)')
ax3.axhline(0, color='black', linestyle='-', linewidth=0.6)
ax3.set_xlabel('Elapsed time / event duration (fraction)')
ax3.set_ylabel('delta_p / target (fraction)')
ax3.set_title('Normalized Hold Quality -- delta_p/target vs. time/duration, event window only\n'
              f'(DEC - Load-Up, {fleet_text})')
ax3.set_xlim(0, 1)
ax3.legend(loc='lower left', fontsize=8)

plot3_file = os.path.join(DEC_RESULTS_ROOT, "IEEE_DEC_RampProfile_Normalized.png")
plt.savefig(plot3_file, dpi=300, bbox_inches='tight')
print(f"Normalized hold-quality plot saved to: {plot3_file}")

#########################################
# RESPONSE-TIME & BASELINE-STATUS ANALYSIS
#
# For each test, cross-references each dispatched unit's admission time
# (from its {folder}_release_summary.csv, written by ieee_gen_dec.py)
# against that same unit's own baseline (uncontrolled) water-heater power
# at that exact minute (from {folder}_baseline_WH_power.csv), to answer:
#   1. How long after the dispatch signal was each unit actually admitted
#      into control -- <=10 min / 10-30 min / >30 min / never?
#   2. At the moment of admission, was the unit already actively drawing
#      power in baseline, or was it idle? Uses the same 0.1 kW threshold
#      as BASELINE_IDLE_THRESHOLD_KW in ieee_gen_dec.py, whose eligibility
#      filter only ever admits units that are currently idle in baseline,
#      so this is expected to be ~100% idle.
#########################################

BASELINE_STATUS_THRESHOLD_KW = 0.1
RESPONSE_BINS = [(0, 10, '<=10 min'), (10, 30, '10-30 min'), (30, None, '>30 min')]


def analyze_response_and_status(tr):
    folder = tr['folder']
    rel = pd.read_csv(test_file(folder, "_release_summary.csv"), parse_dates=['dispatch_time'])
    dispatched = rel.dropna(subset=['dispatch_time']).copy()
    dispatched['response_min'] = (dispatched['dispatch_time'] - tr['dispatch_t']).dt.total_seconds() / 60.0

    n_total = len(rel)
    n_dispatched = len(dispatched)
    n_never = n_total - n_dispatched

    bin_counts = {}
    for lo, hi, name in RESPONSE_BINS:
        if hi is None:
            mask = dispatched['response_min'] > lo
        elif lo == 0:
            mask = dispatched['response_min'] <= hi
        else:
            mask = (dispatched['response_min'] > lo) & (dispatched['response_min'] <= hi)
        bin_counts[name] = int(mask.sum())

    # Only the HH:MM columns someone was admitted at are needed -- avoids
    # loading all 1440 columns for a 10k-unit fleet.
    hhmm = dispatched['dispatch_time'].dt.strftime('%H:%M')
    wh = pd.read_csv(test_file(folder, "_baseline_WH_power.csv"),
                     usecols=['Home'] + sorted(hhmm.unique())).set_index('Home')
    col_pos = {c: i for i, c in enumerate(wh.columns)}
    row_pos = wh.index.get_indexer(dispatched['Home'])
    col_idx = hhmm.map(col_pos).values
    power_at_dispatch = wh.values[row_pos, col_idx]

    n_active = int((power_at_dispatch > BASELINE_STATUS_THRESHOLD_KW).sum())
    n_idle = n_dispatched - n_active

    return {
        'n_total': n_total, 'n_dispatched': n_dispatched, 'n_never': n_never,
        'bin_counts': bin_counts, 'n_active': n_active, 'n_idle': n_idle,
    }


all_stats = {}
lines = [
    "IEEE DEC -- Response Time & Baseline-Status Analysis",
    "=" * 70,
    f"Dispatch signal issued at: {dispatch_text}",
    f"Fleet size: {fleet_text}",
    "",
]

for tr in tests:
    stats = analyze_response_and_status(tr)
    all_stats[tr['label']] = stats
    n_disp = stats['n_dispatched']

    lines.append(f"Test: {tr['label']} ({tr['folder']})")
    lines.append(f"  Target: {tr['target']:g} kW, duration: {tr['dur']:g} h, dispatched at {tr['dispatch_t']}")
    lines.append(f"  Dispatched: {n_disp} / {stats['n_total']} ({100 * n_disp / stats['n_total']:.1f}%)")
    lines.append(f"  Never dispatched: {stats['n_never']}")
    lines.append("  Response time since dispatch signal:")
    for lo, hi, name in RESPONSE_BINS:
        c = stats['bin_counts'][name]
        pct = 100 * c / n_disp if n_disp else 0.0
        lines.append(f"    {name:<10}: {c} ({pct:.1f}% of dispatched)")
    lines.append("  Baseline status at moment of dispatch (load-up command):")
    pct_active = 100 * stats['n_active'] / n_disp if n_disp else 0.0
    pct_idle = 100 * stats['n_idle'] / n_disp if n_disp else 0.0
    lines.append(f"    Actively consuming power when load-up was issued: {stats['n_active']} ({pct_active:.1f}% of dispatched)")
    lines.append(f"    Idle (not consuming power) when load-up was issued: {stats['n_idle']} ({pct_idle:.1f}% of dispatched)")
    lines.append("-" * 70)

results_txt_file = os.path.join(DEC_RESULTS_ROOT, "IEEE_DEC_RampProfile_response_results.txt")
with open(results_txt_file, 'w') as f:
    f.write("\n".join(lines) + "\n")
print(f"Response/status results written to: {results_txt_file}")

# -- plot: response-time buckets (left) and baseline-status split (right) --

labels_list = [tr['label'] for tr in tests]
bin_names = [name for _, _, name in RESPONSE_BINS]
bin_colors = ['#2ca02c', '#ee5e0a', '#d62728']

bin_pcts = {name: [] for name in bin_names}
never_pct = []
active_pcts = []
idle_pcts = []

for label in labels_list:
    stats = all_stats[label]
    n_total = stats['n_total']
    n_disp = stats['n_dispatched']
    for name in bin_names:
        bin_pcts[name].append(100 * stats['bin_counts'][name] / n_total)
    never_pct.append(100 * stats['n_never'] / n_total)
    active_pcts.append(100 * stats['n_active'] / n_disp if n_disp else 0.0)
    idle_pcts.append(100 * stats['n_idle'] / n_disp if n_disp else 0.0)

x = list(range(len(labels_list)))
fig4, (axA, axB) = plt.subplots(1, 2, figsize=(16, 6))

bottom = [0.0] * len(labels_list)
for name, color in zip(bin_names, bin_colors):
    axA.bar(x, bin_pcts[name], bottom=bottom, label=name, color=color)
    bottom = [b + v for b, v in zip(bottom, bin_pcts[name])]
axA.bar(x, never_pct, bottom=bottom, label='Never dispatched', color='#7f7f7f')
axA.set_xticks(x)
axA.set_xticklabels(labels_list, rotation=45, ha='right')
axA.set_ylabel(f'% of fleet ({fleet_text})')
axA.set_title('Response Time to Dispatch Signal\n(DEC - Load-Up)')
axA.legend(fontsize=8)
axA.set_ylim(0, 100)

axB.bar(x, active_pcts, label='Actively consuming power at dispatch', color='#d62728')
axB.bar(x, idle_pcts, bottom=active_pcts, label='Idle at dispatch', color='#133CAD')
axB.set_xticks(x)
axB.set_xticklabels(labels_list, rotation=45, ha='right')
axB.set_ylabel('% of dispatched units')
axB.set_title('Baseline Status at Moment of Dispatch\n(DEC - Load-Up)')
axB.legend(fontsize=8)
axB.set_ylim(0, 100)

plt.tight_layout()
plot4_file = os.path.join(DEC_RESULTS_ROOT, "IEEE_DEC_RampProfile_ResponseAnalysis.png")
plt.savefig(plot4_file, dpi=300, bbox_inches='tight')
print(f"Response/status analysis plot saved to: {plot4_file}")
