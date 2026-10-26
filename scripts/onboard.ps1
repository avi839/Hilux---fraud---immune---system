<#
.SYNOPSIS
  Onboard_Validator (Req 40.9): dry-run a Column_Mapping over a bank CSV export.

.DESCRIPTION
  Parses at most the first -Rows rows with the CSV_Adapter. It does not start the HELIX loop
  and writes nothing to the Ground_Truth_Store. Prints the rows parsed, the rows rejected per
  reason with example row numbers, the percentage of parsed rows containing each
  Transaction_Event field, the label counts and the timestamp range. Exit code 1 when the
  mapping or the file header is invalid (every error is listed).

.PARAMETER Mapping
  Column_Mapping file (.yaml/.yml/.json), e.g. mappings\generic_bank.yaml.

.PARAMETER Trans
  CSV transaction export.

.PARAMETER Rows
  Parse at most N rows (default 1000).

.EXAMPLE
  powershell -ExecutionPolicy Bypass -File scripts\onboard.ps1 -Mapping mappings\generic_bank.yaml -Trans backend\tests\fixtures\onboarding\generic_bank.csv

.EXAMPLE
  powershell -ExecutionPolicy Bypass -File scripts\onboard.ps1 -Mapping mappings\amlworld.yaml -Trans data\HI-Small_Trans.csv -Rows 5000
#>
param(
  [Parameter(Mandatory = $true)][string]$Mapping,
  [Parameter(Mandatory = $true)][string]$Trans,
  [ValidateRange(1, [int]::MaxValue)][int]$Rows = 1000
)

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
$backend = Join-Path $root "backend"
$python = Join-Path $backend ".venv\Scripts\python.exe"
if (-not (Test-Path -LiteralPath $python)) { throw "Backend venv not found: $python (create it and install backend requirements first)" }

# Resolve relative paths from the current directory before switching to backend\.
foreach ($name in "Mapping", "Trans") {
  $p = Get-Variable -Name $name -ValueOnly
  if (-not (Test-Path -LiteralPath $p)) { throw "${name} file not found: $p" }
  Set-Variable -Name $name -Value (Resolve-Path -LiteralPath $p).Path
}

Push-Location $backend
try {
  & $python -m helix.datasets.onboard --mapping $Mapping --trans $Trans --rows $Rows
  exit $LASTEXITCODE
}
finally {
  Pop-Location
}
