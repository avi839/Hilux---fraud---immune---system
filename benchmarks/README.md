# AMLworld benchmark: HELIX vs baselines at equal investigator capacity

Head-to-head comparison of HELIX (default config, unchanged) with a threshold rule,
Isolation Forest and XGBoost on IBM AMLworld HI-Small. Method, assumptions and caveats:
[BENCHMARK_NOTES.md](../BENCHMARK_NOTES.md). Results: [results/results.md](results/results.md).

Nothing here imports HELIX detection modules. `export_helix.py` drives HELIX through its
normal public pipeline (AppState + Dataset_Loader, headless, no ports), the same way
`scripts/aml_replay.py` does.

## Commands

Run from the repository root with the backend virtualenv (Python packages pinned in
`backend/requirements-dev.txt`). `data/HI-Small_Trans.csv` must be present.

```powershell
# 1. Extract the benchmark window once (DuckDB, a few seconds) -> benchmarks/.cache/window.csv
backend\.venv\Scripts\python.exe benchmarks\prepare_data.py
#    (optional) rows and laundering per day / hour, used to choose the windows
backend\.venv\Scripts\python.exe benchmarks\prepare_data.py --profile

# 2. HELIX export: warm-up replay, then the test window live -> results/helix_alerts.csv
#    tiny smoke run first (a few seconds):
backend\.venv\Scripts\python.exe benchmarks\export_helix.py --max-rows 4000 --warmup-rows 2000 --out tiny
#    full run (about 20 min for the configured windows):
backend\.venv\Scripts\python.exe benchmarks\export_helix.py
#    optional Auto_Triage variant -> results/helix_alerts_triage.csv
backend\.venv\Scripts\python.exe benchmarks\export_helix.py --triage --triage-idle-s 3600 --out helix_alerts_triage

# 3. Baselines + scoring + outputs (about 30 s)
backend\.venv\Scripts\python.exe benchmarks\run_benchmark.py

# Tests for the scoring logic
backend\.venv\Scripts\python.exe -m pytest benchmarks\tests -q
```

## Files

| File | Purpose |
|---|---|
| `config.json` | windows, capacities, costs, seeds, USD rate table, value cap |
| `common.py` | paths, config, reader for the cached window (same account keys and tx ids as HELIX) |
| `prepare_data.py` | one-off DuckDB extraction of the window, sorted by (timestamp, file row) |
| `export_helix.py` | headless HELIX replay and Case export |
| `baselines.py` | causal features, rule, Isolation Forest, XGBoost |
| `scoring.py` | alert grouping, top-N per hour, TP rule, ROI metrics |
| `run_benchmark.py` | end to end: baselines, scoring, `results.csv`, `results.md`, `summary.json`, chart |
| `tests/test_scoring.py` | unit and property tests of the scoring logic |

`benchmarks/.cache/` (extracted window, logs) is gitignored.

Outputs in `results/`: `results.csv` (every system x seed x N x minutes-per-case),
`results.md`, `summary.json` (machine-readable, read by the frontend),
`value_per_hour_N50.png`, `helix_alerts.csv` + `.run.json` (HELIX export) and, when run,
`helix_alerts_triage.csv` + `.run.json`.
