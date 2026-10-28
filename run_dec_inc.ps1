# Runs one DEC test and then one INC test on the same fleet, each followed by
# its results script. The INC test reuses the baseline the DEC test cached.
# Output: Z:\dataset\DEC\<name>, Z:\dataset\INC\<name>, baseline cache in
# Z:\dataset\Baseline (see ieee_pes_common.py); console logs in .\logs\.
#
# Usage (from this folder):
#   powershell -ExecutionPolicy Bypass -File .\run_dec_inc.ps1 -FleetSize 1000
param(
    [int]$FleetSize = 1000,
    [double]$DecTargetKW = 120,
    [double]$DecDurationHr = 1,
    [double]$IncTargetKW = 100,
    [double]$IncDurationHr = 4
)

Set-Location $PSScriptRoot
$py = Join-Path $PSScriptRoot ".venv\Scripts\python.exe"
$logDir = Join-Path $PSScriptRoot "logs"
New-Item -ItemType Directory -Force $logDir | Out-Null
$stamp = Get-Date -Format "yyyyMMdd_HHmm"
$master = Join-Path $logDir "run_${FleetSize}_$stamp.log"
function Log($msg) { "$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss')  $msg" | Tee-Object -FilePath $master -Append }

# Keep the PC awake (no sleep) while this script runs.
Add-Type -Namespace Win32 -Name Power -MemberDefinition '[DllImport("kernel32.dll")] public static extern uint SetThreadExecutionState(uint esFlags);'
[Win32.Power]::SetThreadExecutionState([uint32]"0x80000001") | Out-Null  # ES_CONTINUOUS | ES_SYSTEM_REQUIRED

# Output roots: Z:\dataset when the drive letter is visible to this process,
# otherwise the same share by its network path.
$root = "Z:\dataset"
if (-not (Test-Path $root)) { $root = "\\10.218.219.78\SharedDrive\dataset" }
if (-not (Test-Path $root)) { Log "ERROR: output folder not reachable (Z:\dataset or $root)"; exit 1 }
$env:OCHRE_DEC_RESULTS_DIR = Join-Path $root "DEC"
$env:OCHRE_INC_RESULTS_DIR = Join-Path $root "INC"
$env:OCHRE_BASELINE_DIR = Join-Path $root "Baseline"
$env:OCHRE_FLEET_SIZE = "$FleetSize"
$env:MPLBACKEND = "Agg"

function Run-Step($name, $script) {
    $log = Join-Path $logDir "${name}.log"
    Log "START $script ($name) -> $log"
    & $py $script *> $log
    $code = $LASTEXITCODE
    Log "END   $script ($name) exit code $code"
    return $code
}

Log "Fleet size $FleetSize, outputs under $root"

# ---- DEC ----
$decName = "IEEE_DEC_${FleetSize}_$([int]$DecTargetKW)kW_$($DecDurationHr)h"
$env:OCHRE_FILENAME = $decName
$env:OCHRE_TARGET_DELTA_KW = "$DecTargetKW"
$env:OCHRE_DURATION_HR = "$DecDurationHr"
if ((Run-Step $decName "ieee_gen_dec.py") -eq 0) { Run-Step "${decName}_results" "ieee_dec_results.py" | Out-Null }

# ---- INC ----
$incName = "IEEE_INC_${FleetSize}_$([int]$IncTargetKW)kW_$($IncDurationHr)h"
$env:OCHRE_FILENAME = $incName
$env:OCHRE_TARGET_REDUCTION_KW = "$IncTargetKW"
$env:OCHRE_DURATION_HR = "$IncDurationHr"
if ((Run-Step $incName "ieee_gen_inc.py") -eq 0) { Run-Step "${incName}_results" "ieee_inc_results.py" | Out-Null }

Log "ALL DONE"
