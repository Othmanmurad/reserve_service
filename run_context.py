# -*- coding: utf-8 -*-
"""
Shared last-run hand-off between the B (simulation) and C (analysis)
scripts -- filename AND reserve_event (dispatch_time/duration). A B
script calls save_filename()/save_reserve_event() with whatever it
actually used (env/init_globals override or its own hardcoded default);
a C script calls the matching load_*() to pick that same value back up,
so it stops needing its own copy kept in sync by hand. Persisted to
small files (not env vars) so this also works across separate manual
runs, not just within one excel_ochre.py process.

`pipeline` scopes the state to a specific B/C pipeline (e.g. "RD", "RU")
so independent pipelines running in the same session (see excel_ochre.py,
which triggers both a ramp-down and a ramp-up run per Excel signal) don't
stomp on each other's last-run values. Omit it (the default) to use the
single shared, unscoped slot -- this is what the original,
not-pipeline-specific scripts (HPWH_B3_Reserve_V3.py,
HPWH_C1_parse_OCHRE_data_final.py, HPWH_C3_Reserve_Calculations.py) still
use, unchanged.
"""

import os
import json

_STATE_DIR = os.path.dirname(os.path.abspath(__file__))


def _state_file(pipeline=None):
    suffix = f"_{pipeline}" if pipeline else ""
    return os.path.join(_STATE_DIR, f".last_run_filename{suffix}")


def save_filename(name, pipeline=None):
    with open(_state_file(pipeline), "w") as f:
        f.write(name)


def load_filename(default, pipeline=None):
    try:
        with open(_state_file(pipeline)) as f:
            saved = f.read().strip()
        return saved or default
    except FileNotFoundError:
        return default


def _reserve_event_file(pipeline=None):
    suffix = f"_{pipeline}" if pipeline else ""
    return os.path.join(_STATE_DIR, f".last_reserve_event{suffix}.json")


def save_reserve_event(reserve_event, pipeline=None):
    with open(_reserve_event_file(pipeline), "w") as f:
        json.dump(reserve_event, f)


def load_reserve_event(default, pipeline=None):
    try:
        with open(_reserve_event_file(pipeline)) as f:
            saved = json.load(f)
        return saved or default
    except (FileNotFoundError, json.JSONDecodeError):
        return default


def _input_folder_file(pipeline=None):
    suffix = f"_{pipeline}" if pipeline else ""
    return os.path.join(_STATE_DIR, f".last_input_folder{suffix}")


def save_input_folder(name, pipeline=None):
    with open(_input_folder_file(pipeline), "w") as f:
        f.write(name)


def load_input_folder(default, pipeline=None):
    try:
        with open(_input_folder_file(pipeline)) as f:
            saved = f.read().strip()
        return saved or default
    except FileNotFoundError:
        return default
