<#
.SYNOPSIS
  Start the HELIX demo: API_Server (127.0.0.1:8000, synthetic generator on) and the
  dashboard dev server (http://localhost:5173). Ctrl+C stops both.

.PARAMETER Storage
  DuckDB file for this demo, relative to backend\. Default data\demo.duckdb, which is
  recreated on every start (the replay timeline reads Storage, and Case ids restart at C-1
  each run, so an old file would mix earlier runs into the timelines). Pass another path, or
  -Keep, to keep a file.

.PARAMETER Config
  HelixConfig file (YAML/JSON) passed to the API_Server, relative to backend\ or absolute.
  Default config\demo.json (backend\config\demo.json; see the note below).

.PARAMETER NoGenerator
  Do not start the synthetic generator at startup (it stays configured: use "Start generator"
  or "Load demo scenario" in the UI).

.PARAMETER Attacker
  Attach and start the Attacker_Agent at startup (passes --attacker).

.PARAMETER Immune
  Enable the Immune_System and its miss detection (passes --immune).

.PARAMETER Soc
  Enable cyber fusion and the SOC components (passes --soc): kill chain, rules, playbooks,
  honeytokens, threat intel, audit ledger, purple team, resilience mode.

.PARAMETER Dataset
  Stream a dataset instead of the synthetic generator (Req 39.10): "amlworld" or "csv".
  Passes --dataset ... --trans ... and omits --generator. -Trans is required.

.PARAMETER Trans
  Transactions CSV for -Dataset (AMLworld *_Trans.csv or a bank export). Checked before start.

.PARAMETER Patterns
  AMLworld *_Patterns.txt (with -Dataset amlworld): per-pattern detection metrics.

.PARAMETER Mapping
  Column_Mapping file (required with -Dataset csv), e.g. mappings\generic_bank.yaml.

.PARAMETER Speed
  "max" (default in dataset mode) or a replay multiplier in [1, 1000000], e.g. 60.

.PARAMETER MaxRows
  Stream only the first N rows in timestamp order (API_Server default 20000; 0 = no limit).

.PARAMETER Warmup
  Warm_Up_Mode: build Behavioural_DNA / CPD / graph state first, with Case creation suppressed.

.PARAMETER WarmupUntil
  With -Warmup: warm up only on rows before this ISO 8601 time (naive = UTC), then go live.

.EXAMPLE
  powershell -ExecutionPolicy Bypass -File scripts\demo.ps1 -Dataset amlworld -Trans data\HI-Small_Trans.csv -Patterns data\HI-Small_Patterns.txt -MaxRows 20000

.EXAMPLE
  powershell -ExecutionPolicy Bypass -File scripts\demo.ps1

.EXAMPLE
  powershell -ExecutionPolicy Bypass -File scripts\demo.ps1 -Attacker -Immune

.EXAMPLE
  powershell -ExecutionPolicy Bypass -File scripts\demo.ps1 -Soc -Attacker -Immune
#>
param(
  [string]$Storage = "data\demo.duckdb",
  [string]$Config = "config\demo.json",
  [switch]$Keep,
  [switch]$NoGenerator,
  [switch]$Attacker,
  [switch]$Immune,
  [switch]$Soc,
  [ValidateSet("amlworld", "csv")][string]$Dataset,
  [string]$Trans,
  [string]$Patterns,
  [string]$Mapping,
  [string]$Speed,
  [Nullable[int]]$MaxRows,
  [switch]$Warmup,
  [string]$WarmupUntil
)

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
$backend = Join-Path $root "backend"
$frontend = Join-Path $root "frontend"
$python = Join-Path $backend ".venv\Scripts\python.exe"

if (-not (Test-Path -LiteralPath $python)) { throw "Backend venv not found: $python (create it and install backend requirements first)" }

# Dataset mode (Req 39.10): check the files before anything starts. Relative paths are
# resolved from the current directory (the API_Server runs in backend\).
$datasetArgs = @()
if ($Dataset) {
  if (-not $Trans) { throw "-Dataset needs -Trans <path>" }
  if ($Dataset -eq "csv" -and -not $Mapping) { throw "-Dataset csv needs -Mapping <file>" }
  $checked = [ordered]@{ "--trans" = $Trans; "--patterns" = $Patterns; "--mapping" = $Mapping }
  $datasetArgs = @("--dataset", $Dataset)
  foreach ($flag in $checked.Keys) {
    $p = $checked[$flag]
    if (-not $p) { continue }
    if (-not (Test-Path -LiteralPath $p -PathType Leaf)) { throw "File not found for ${flag}: $p" }
    $datasetArgs += @($flag, (Resolve-Path -LiteralPath $p).Path)
  }
  if ($Speed) { $datasetArgs += @("--speed", $Speed) }
  if ($null -ne $MaxRows) { $datasetArgs += @("--max-rows", "$MaxRows") }
  if ($WarmupUntil -and -not $Warmup) { throw "-WarmupUntil needs -Warmup" }
  if ($Warmup) { $datasetArgs += "--warmup" }
  if ($WarmupUntil) { $datasetArgs += @("--warmup-until", $WarmupUntil) }
}
elseif ($Trans -or $Patterns -or $Mapping -or $Speed -or ($null -ne $MaxRows) -or $Warmup -or $WarmupUntil) {
  throw "-Trans, -Patterns, -Mapping, -Speed, -MaxRows, -Warmup and -WarmupUntil need -Dataset amlworld|csv"
}
if (-not (Test-Path -LiteralPath (Join-Path $frontend "node_modules"))) { throw "Run 'npm install' in $frontend first" }
foreach ($port in 8000, 5173) {
  if (Get-NetTCPConnection -LocalPort $port -State Listen -ErrorAction SilentlyContinue) {
    throw "Port $port is already in use; stop the other server first."
  }
}

$dbPath = Join-Path $backend $Storage
if (-not $Keep -and $Storage -eq "data\demo.duckdb") {
  foreach ($f in @($dbPath, "$dbPath.wal")) { if (Test-Path -LiteralPath $f) { Remove-Item -LiteralPath $f } }
}
New-Item -ItemType Directory -Force -Path (Split-Path -Parent $dbPath) | Out-Null

# Demo generator settings (backend\config\demo.json). Cases are bounded (risk.case_max_accounts
# / case_max_events / case_max_motifs, task 19.1), so the default 1,000 accounts at 50 ev/s no
# longer snowball Cases (largest stays at 50 accounts; ACT p50 ~2 ms after 3 minutes). That
# graph still gets denser over time: after ~4 minutes motif detection (REASON) finds 100+
# instances per event and the pipeline drifts below 50 ev/s. 20,000 accounts at 10 ev/s keeps
# the graph sparse for a long-running demo (measured: max 4 accounts, ACT ~0.1 ms).
$configPath = if ([System.IO.Path]::IsPathRooted($Config)) { $Config } else { Join-Path $backend $Config }
if (-not (Test-Path -LiteralPath $configPath)) { throw "Config file not found: $configPath" }

# Start-Process joins -ArgumentList with spaces and does not quote, so quote any argument
# with spaces (this workspace path has spaces and '&'). Relative paths resolve from backend\.
function Quote([string]$s) { if ($s -match '\s') { '"' + $s + '"' } else { $s } }
$apiArgs = @("-m", "helix.api", "--storage", (Quote $Storage), "--config", (Quote $Config))
if ($Dataset) { foreach ($a in $datasetArgs) { $apiArgs += (Quote $a) } }  # in place of --generator
elseif (-not $NoGenerator) { $apiArgs += "--generator" }
if ($Attacker) { $apiArgs += "--attacker" }
if ($Immune) { $apiArgs += "--immune" }
if ($Soc) { $apiArgs += "--soc" }

# Start-Process with -WorkingDirectory avoids cmd.exe shims, which break on the '&' in this path.
$api = Start-Process -FilePath $python -ArgumentList $apiArgs -WorkingDirectory $backend -NoNewWindow -PassThru
$web = Start-Process -FilePath "node" -ArgumentList @("node_modules/vite/bin/vite.js") -WorkingDirectory $frontend -NoNewWindow -PassThru

try {
  $deadline = (Get-Date).AddSeconds(60)
  while ((Get-Date) -lt $deadline) {
    $up = (Get-NetTCPConnection -LocalPort 8000 -State Listen -ErrorAction SilentlyContinue) -and
          (Get-NetTCPConnection -LocalPort 5173 -State Listen -ErrorAction SilentlyContinue)
    if ($up) { break }
    if ($api.HasExited) { throw "API_Server exited (code $($api.ExitCode))" }
    if ($web.HasExited) { throw "Vite exited (code $($web.ExitCode))" }
    Start-Sleep -Milliseconds 500
  }
  Write-Host ""
  Write-Host "HELIX dashboard: http://localhost:5173" -ForegroundColor Green
  Write-Host "API_Server:      http://127.0.0.1:8000/docs (no authentication; loopback only)"
  Write-Host "Press Ctrl+C to stop both."
  Write-Host ""
  Wait-Process -Id $api.Id, $web.Id
}
finally {
  # /T: the venv python.exe is a launcher that runs the real interpreter as a child process.
  foreach ($p in $web, $api) {
    if ($p -and -not $p.HasExited) { & taskkill.exe /PID $p.Id /T /F 2>&1 | Out-Null }
  }
}
