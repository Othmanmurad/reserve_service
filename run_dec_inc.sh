#!/usr/bin/env bash
# Linux version of run_dec_inc.ps1: runs one DEC test and then one INC test
# on the same fleet, each followed by its results script. The INC test
# reuses the baseline the DEC test cached.
# Output: <root>/DEC/<name>, <root>/INC/<name>, baseline cache in
# <root>/Baseline (see ieee_pes_common.py); console logs in ./logs/.
#
# Usage (from this folder):
#   bash run_dec_inc.sh --fleet-size 1000
#   bash run_dec_inc.sh --fleet-size 1500 --dec-target-kw 200 --dec-duration-hr 2 \
#                       --inc-target-kw 150 --inc-duration-hr 4 --root /mnt/archive/dataset
# Run it inside tmux (or with nohup) so it survives closing the SSH session.

FLEET_SIZE=1000
DEC_TARGET_KW=120
DEC_DURATION_HR=1
INC_TARGET_KW=100
INC_DURATION_HR=4
# Output root: the shared drive's dataset folder (SharedDrive is /mnt/archive
# on the Ubuntu server that hosts it; Z:\dataset on Windows).
ROOT="${OCHRE_DATASET_ROOT:-/mnt/archive/dataset}"

while [ $# -gt 0 ]; do
    case "$1" in
        --fleet-size)      FLEET_SIZE="$2"; shift 2 ;;
        --dec-target-kw)   DEC_TARGET_KW="$2"; shift 2 ;;
        --dec-duration-hr) DEC_DURATION_HR="$2"; shift 2 ;;
        --inc-target-kw)   INC_TARGET_KW="$2"; shift 2 ;;
        --inc-duration-hr) INC_DURATION_HR="$2"; shift 2 ;;
        --root)            ROOT="$2"; shift 2 ;;
        -h|--help)         sed -n '2,13p' "$0"; exit 0 ;;
        *) echo "Unknown option: $1 (see --help)"; exit 1 ;;
    esac
done

cd "$(dirname "$(readlink -f "$0")")"
PY="$PWD/.venv/bin/python"
LOG_DIR="$PWD/logs"
mkdir -p "$LOG_DIR"
MASTER="$LOG_DIR/run_${FLEET_SIZE}_$(date +%Y%m%d_%H%M).log"
log() { echo "$(date '+%Y-%m-%d %H:%M:%S')  $*" | tee -a "$MASTER"; }

if [ ! -x "$PY" ]; then log "ERROR: $PY not found -- create the .venv first (see requirements.txt)"; exit 1; fi
if [ ! -d "$ROOT" ]; then log "ERROR: output folder not reachable ($ROOT) -- is the shared drive mounted?"; exit 1; fi

export OCHRE_DEC_RESULTS_DIR="$ROOT/DEC"
export OCHRE_INC_RESULTS_DIR="$ROOT/INC"
export OCHRE_BASELINE_DIR="$ROOT/Baseline"
export OCHRE_FLEET_SIZE="$FLEET_SIZE"
export MPLBACKEND=Agg

run_step() {  # run_step <name> <script>
    local log_file="$LOG_DIR/$1.log"
    log "START $2 ($1) -> $log_file"
    "$PY" "$2" > "$log_file" 2>&1
    local code=$?
    log "END   $2 ($1) exit code $code"
    return $code
}

log "Fleet size $FLEET_SIZE, outputs under $ROOT"

# ---- DEC ----
DEC_NAME="IEEE_DEC_${FLEET_SIZE}_$(printf '%.0f' "$DEC_TARGET_KW")kW_${DEC_DURATION_HR}h"
export OCHRE_FILENAME="$DEC_NAME"
export OCHRE_TARGET_DELTA_KW="$DEC_TARGET_KW"
export OCHRE_DURATION_HR="$DEC_DURATION_HR"
run_step "$DEC_NAME" ieee_gen_dec.py && run_step "${DEC_NAME}_results" ieee_dec_results.py

# ---- INC ----
INC_NAME="IEEE_INC_${FLEET_SIZE}_$(printf '%.0f' "$INC_TARGET_KW")kW_${INC_DURATION_HR}h"
export OCHRE_FILENAME="$INC_NAME"
export OCHRE_TARGET_REDUCTION_KW="$INC_TARGET_KW"
export OCHRE_DURATION_HR="$INC_DURATION_HR"
run_step "$INC_NAME" ieee_gen_inc.py && run_step "${INC_NAME}_results" ieee_inc_results.py

log "ALL DONE"
