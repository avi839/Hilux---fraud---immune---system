<#
.SYNOPSIS
  Replay an IBM AMLworld transactions file from data\ through a headless HELIX pipeline
  and print the load report and detection summary (precision, recall, per pattern type).
  Wraps scripts\aml_replay.py; the API_Server and dashboard are not involved.

.PARAMETER Name
  Dataset prefix of the files in data\, e.g. HI-Small reads data\HI-Small_Trans.csv and,
  if present, data\HI-Small_Patterns.txt. Default HI-Small.

.PARAMETER Trans
  Explicit path to a *_Trans.csv file (overrides -Name).

.PARAMETER Patterns
  Explicit path to a *_Patterns.txt file (overrides the one derived from -Name).

.PARAMETER MaxRows
  Replay only the first N rows in timestamp order. Default 20000; 0 = the whole file
  (HI-Small has about 5 million rows, which takes a long time through the pipeline).

.PARAMETER Speed
  "max" (default) or a speed multiplier in [1, 1000000] relative to recorded time.

.PARAMETER LoadOnly
  Parse the file and report counts without running the pipeline.

.EXAMPLE
  powershell -ExecutionPolicy Bypass -File scripts\aml.ps1 -Name HI-Small -MaxRows 20000
#>
param(
  [string]$Name = "HI-Small",
  [string]$Trans = "",
  [string]$Patterns = "",
  [int]$MaxRows = 20000,
  [string]$Speed = "max",
  [switch]$LoadOnly
)

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
$python = Join-Path $root "backend\.venv\Scripts\python.exe"
if (-not (Test-Path -LiteralPath $python)) { throw "Backend venv not found: $python" }

if (-not $Trans) { $Trans = Join-Path $root "data\$($Name)_Trans.csv" }
if (-not (Test-Path -LiteralPath $Trans)) {
  throw "Not found: $Trans. Download the AMLworld files from Kaggle into data\ first (see README, 'Data')."
}
if (-not $Patterns -and -not $PSBoundParameters.ContainsKey("Trans")) {
  $guess = Join-Path $root "data\$($Name)_Patterns.txt"
  if (Test-Path -LiteralPath $guess) { $Patterns = $guess }
}

# The call operator passes each array element as one argument, so paths with spaces and
# '&' (this workspace) are safe.
$argv = @((Join-Path $PSScriptRoot "aml_replay.py"), $Trans, "--max-rows", "$MaxRows", "--speed", $Speed)
if ($Patterns) { $argv += @("--patterns", $Patterns) }
if ($LoadOnly) { $argv += "--load-only" }
& $python @argv
exit $LASTEXITCODE
