#!/usr/bin/env bash
# Runs one INC (shed) test, then its results script.
# Baselines come from the shared cache (simulated and cached if missing), so
# an earlier DEC test on the same fleet has already done that work.
# Output: <root>/INC/<name>, baseline cache in <root>/Baseline (see
# ieee_pes_common.py); console logs in ./logs/.
#
# Usage (from this folder):
#   bash run_inc.sh --fleet-size 1000
#   bash run_inc.sh --fleet-size 1500 --target-kw 150 --duration-hr 2 --root /mnt/archive/dataset
# Run it inside screen/tmux (or with nohup) so it survives closing the SSH session.

FLEET_SIZE=1000
TARGET_KW=100
DURATION_HR=4
# Output root: the shared drive's dataset folder (SharedDrive is /mnt/archive
# on the Ubuntu server that hosts it; Z:\dataset on Windows).
ROOT="${OCHRE_DATASET_ROOT:-/mnt/archive/dataset}"

while [ $# -gt 0 ]; do
    case "$1" in
        --fleet-size)  FLEET_SIZE="$2"; shift 2 ;;
        --target-kw)   TARGET_KW="$2"; shift 2 ;;
        --duration-hr) DURATION_HR="$2"; shift 2 ;;
        --root)        ROOT="$2"; shift 2 ;;
        -h|--help)     sed -n '2,11p' "$0"; exit 0 ;;
        *) echo "Unknown option: $1 (see --help)"; exit 1 ;;
    esac
done

cd "$(dirname "$(readlink -f "$0")")"
PY="$PWD/.venv/bin/python"
LOG_DIR="$PWD/logs"
mkdir -p "$LOG_DIR"
NAME="IEEE_INC_${FLEET_SIZE}_$(printf '%.0f' "$TARGET_KW")kW_${DURATION_HR}h"
MASTER="$LOG_DIR/run_${NAME}_$(date +%Y%m%d_%H%M).log"
log() { echo "$(date '+%Y-%m-%d %H:%M:%S')  $*" | tee -a "$MASTER"; }

if [ ! -x "$PY" ]; then log "ERROR: $PY not found -- create the .venv first (see requirements.txt)"; exit 1; fi
if [ ! -d "$ROOT" ]; then log "ERROR: output folder not reachable ($ROOT) -- is the shared drive mounted?"; exit 1; fi

export OCHRE_INC_RESULTS_DIR="$ROOT/INC"
export OCHRE_BASELINE_DIR="$ROOT/Baseline"
export OCHRE_FLEET_SIZE="$FLEET_SIZE"
export OCHRE_FILENAME="$NAME"
export OCHRE_TARGET_REDUCTION_KW="$TARGET_KW"
export OCHRE_DURATION_HR="$DURATION_HR"
export MPLBACKEND=Agg

run_step() {  # run_step <name> <script>
    local log_file="$LOG_DIR/$1.log"
    log "START $2 ($1) -> $log_file"
    "$PY" "$2" > "$log_file" 2>&1
    local code=$?
    log "END   $2 ($1) exit code $code"
    return $code
}

log "INC test $NAME: fleet size $FLEET_SIZE, outputs under $ROOT"
run_step "$NAME" ieee_gen_inc.py && run_step "${NAME}_results" ieee_inc_results.py
CODE=$?
log "ALL DONE (exit code $CODE)"
exit $CODE
