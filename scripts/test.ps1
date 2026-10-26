<#
.SYNOPSIS
  Run the HELIX test suites: backend pytest, then frontend typecheck, vitest and build.
  Stops at the first failing step and exits with its code.

.PARAMETER Backend
  Run only the backend suite.

.PARAMETER Frontend
  Run only the frontend steps.

.PARAMETER PytestArgs
  pytest arguments as one space-separated string. Default "-q" (whole suite), e.g.
  -PytestArgs "tests/unit/test_config.py -q".

.NOTES
  The Playwright smoke test (npm run e2e) is not run here: it needs both servers running
  (scripts\demo.ps1) and Microsoft Edge installed.

.EXAMPLE
  powershell -ExecutionPolicy Bypass -File scripts\test.ps1
#>
param(
  [switch]$Backend,
  [switch]$Frontend,
  [string]$PytestArgs = "-q"
)

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
$backendDir = Join-Path $root "backend"
$frontendDir = Join-Path $root "frontend"
$python = Join-Path $backendDir ".venv\Scripts\python.exe"
$runBackend = $Backend -or -not $Frontend
$runFrontend = $Frontend -or -not $Backend

# The call operator with explicit node/python paths avoids npm's cmd.exe shims, which
# break on the '&' in this workspace path.
function Step([string]$name, [string]$dir, [string]$exe, [string[]]$argv) {
  Write-Host "==> $name" -ForegroundColor Cyan
  Push-Location -LiteralPath $dir
  try { & $exe @argv; $code = $LASTEXITCODE } finally { Pop-Location }
  if ($code -ne 0) { Write-Host "FAILED: $name (exit $code)" -ForegroundColor Red; exit $code }
}

if ($runBackend) {
  if (-not (Test-Path -LiteralPath $python)) { throw "Backend venv not found: $python" }
  Step "backend: pytest" $backendDir $python (@("-m", "pytest") + ($PytestArgs -split '\s+' | Where-Object { $_ }))
}
if ($runFrontend) {
  if (-not (Test-Path -LiteralPath (Join-Path $frontendDir "node_modules"))) { throw "Run 'npm install' in $frontendDir first" }
  Step "frontend: typecheck" $frontendDir "node" @("node_modules/typescript/bin/tsc", "-b")
  Step "frontend: vitest" $frontendDir "node" @("node_modules/vitest/vitest.mjs", "--run")
  Step "frontend: build" $frontendDir "node" @("node_modules/vite/bin/vite.js", "build")
}
Write-Host "All selected steps passed." -ForegroundColor Green
