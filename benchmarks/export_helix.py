"""Export HELIX Cases for the benchmark test window (headless, in-process, no ports).

Uses HELIX's normal public pipeline the same way scripts/aml_replay.py does: the
Dataset_Loader loads ``benchmarks/.cache/window.csv`` (labels go to an in-memory
Ground_Truth_Store, never into detection), events are submitted to the AppState pipeline,
and nothing HELIX-side is changed. The replay is:

1. warm-up rows [helix_warmup_start, test_start) with ``helix.warmup = True`` (Warm_Up_Mode:
   Behavioural_DNA, change points and the graph learn, no Cases are created), then
2. the test rows [test_start, test_end) live, with the DEFAULT HelixConfig.

Writes ``benchmarks/results/helix_alerts.csv`` (one row per Case) plus a small JSON run report.

    backend\\.venv\\Scripts\\python.exe benchmarks\\export_helix.py               # full run
    backend\\.venv\\Scripts\\python.exe benchmarks\\export_helix.py --max-rows 3000 --out tiny
    backend\\.venv\\Scripts\\python.exe benchmarks\\export_helix.py --triage      # Auto_Triage on
"""

from __future__ import annotations

import argparse
import asyncio
import csv
import json
import logging
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

from common import RESULTS, ROOT, WINDOW_CSV, load_config

sys.path.insert(0, str(ROOT / "backend"))

from helix.api.state import AppState  # noqa: E402
from helix.config import startup_config  # noqa: E402
from helix.datasets import DatasetLoader, LoaderError, ReplayScope  # noqa: E402
from helix.evaluation import EvaluationMonitor, ground_truth_channel  # noqa: E402
from helix.sim.clock import ManualClock  # noqa: E402

FIELDS = ["case_id", "created_ts", "admitted_ts", "active_queue", "final_placement", "status",
          "closure_reason", "merged_into", "tier", "risk", "expected_loss", "priority",
          "n_accounts", "n_tx", "accounts", "tx_ids", "placements"]


def _ts(s: str) -> datetime:
    return datetime.strptime(s, "%Y/%m/%d %H:%M").replace(tzinfo=timezone.utc)


def _iso(t: datetime | None) -> str:
    return "" if t is None else t.astimezone(timezone.utc).isoformat()


def case_row(c) -> dict:
    placements = [(h.ts, str(h.new)) for h in c.history if h.action == "placement"]
    first_active = next((t for t, p in placements if p == "ACTIVE"), None)
    return {
        "case_id": c.case_id,
        "created_ts": _iso(c.created_at),
        "admitted_ts": _iso(first_active),
        "active_queue": "yes" if first_active is not None else "no",
        "final_placement": str(c.placement),
        "status": str(c.status),
        "closure_reason": "" if c.closure_reason is None else str(c.closure_reason),
        "merged_into": c.merged_into or "",
        "tier": str(c.tier),
        "risk": f"{float(c.risk):.6f}",
        "expected_loss": str(c.expected_loss),
        "priority": str(c.priority),
        "n_accounts": len(c.accounts),
        "n_tx": len(c.tx_ids),
        "accounts": ";".join(sorted(c.accounts)),
        "tx_ids": ";".join(sorted(c.tx_ids)),
        "placements": json.dumps([[_iso(t), p] for t, p in placements]),
    }


async def drain(state: AppState) -> None:
    while len(state.ingestor) > 0:
        await asyncio.sleep(0.05)
    await asyncio.sleep(0)


async def replay_phase(state: AppState, loader: DatasetLoader, n: int | None, name: str) -> float:
    t0 = time.monotonic()
    task = asyncio.create_task(loader.replay("max", count=n))
    last = t0
    while not task.done() or len(state.ingestor) > 0:
        await asyncio.sleep(0.2)
        if time.monotonic() - last >= 30:
            last = time.monotonic()
            el = last - t0
            done = state.ingestor.counters.delivered
            print(f"  [{name}] {el:6.0f} s  delivered {done}  queue {len(state.ingestor)}  "
                  f"cases {len(state.helix.cases.cases)}", flush=True)
    await task
    await drain(state)
    return time.monotonic() - t0


async def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--max-rows", type=int, default=0, help="replay only the first N rows of the HELIX scope")
    p.add_argument("--warmup-rows", type=int, default=None,
                   help="with --max-rows: how many of them are warm-up (default: by timestamp)")
    p.add_argument("--triage", action="store_true", help="enable Auto_Triage (triage.enabled=true)")
    p.add_argument("--triage-idle-s", type=float, default=None,
                   help="with --triage: triage.auto_clear_after_s (default config value: 86400)")
    p.add_argument("--out", default="helix_alerts", help="output stem in benchmarks/results/")
    a = p.parse_args()
    logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(name)s: %(message)s")

    w = load_config()["windows"]
    cfg = startup_config(None)  # the DEFAULT HelixConfig
    if a.triage:
        upd: dict = {"enabled": True}
        if a.triage_idle_s is not None:
            upd["auto_clear_after_s"] = float(a.triage_idle_s)
        cfg = cfg.model_copy(update={"triage": cfg.triage.model_copy(update=upd)})

    clock = ManualClock(datetime(2000, 1, 1, tzinfo=timezone.utc))
    writer, reader = ground_truth_channel(None)
    state = AppState.build(cfg, clock, monitor=EvaluationMonitor(reader), gt_writer=writer, gt_reader=reader)
    loader = DatasetLoader(cfg.dataset, state.ingestor, clock, writer)
    scope = ReplayScope(_ts(w["helix_warmup_start"]), _ts(w["test_end"]), a.max_rows or None)

    t0 = time.monotonic()
    rep = loader.load(WINDOW_CSV, None, scope)
    if isinstance(rep, LoaderError):
        print(f"load failed: {rep}", file=sys.stderr)
        return 1
    load_s = time.monotonic() - t0
    print(f"loaded {rep.in_scope} rows in {load_s:.1f} s (sort {rep.sort_mode}); "
          f"{rep.first_ts} .. {rep.last_ts}", flush=True)
    if rep.first_ts is not None and rep.first_ts > clock.now():
        clock.set(rep.first_ts)

    n_warm = loader.count_before(_ts(w["test_start"]))
    if a.max_rows and a.warmup_rows is not None:
        n_warm = min(a.warmup_rows, rep.in_scope)
    n_test = rep.in_scope - n_warm
    print(f"warm-up rows {n_warm}, test rows {n_test}", flush=True)

    await state.start()
    try:
        state.helix.warmup = True
        warm_s = await replay_phase(state, loader, n_warm, "warm-up") if n_warm else 0.0
        state.helix.warmup = False
        cases_after_warm = len(state.helix.cases.cases)
        print(f"warm-up done in {warm_s:.0f} s ({n_warm / max(warm_s, 1e-9):.0f} ev/s), "
              f"cases {cases_after_warm}", flush=True)
        test_s = await replay_phase(state, loader, None, "test") if n_test else 0.0
        if state.triage is not None:
            state.triage_step(force=True)
    finally:
        await state.stop()
    print(f"test done in {test_s:.0f} s ({n_test / max(test_s, 1e-9):.0f} ev/s); "
          f"worker errors {state.worker_errors}", flush=True)

    cases = sorted(state.helix.cases.cases.values(), key=lambda c: c.seq_no)
    RESULTS.mkdir(parents=True, exist_ok=True)
    out = RESULTS / f"{a.out}.csv"
    with open(out, "w", encoding="utf-8", newline="") as fh:
        wr = csv.DictWriter(fh, FIELDS, lineterminator="\n")
        wr.writeheader()
        for c in cases:
            wr.writerow(case_row(c))
    m = state.monitor.metrics()
    run = {
        "config": "default HelixConfig" + (
            f" + triage.enabled=true, triage.auto_clear_after_s={cfg.triage.auto_clear_after_s:g}" if a.triage else ""),
        "alert_budget_per_period": cfg.prioritiser.alert_budget,
        "period_s": cfg.prioritiser.period_s,
        "investigation_cost": cfg.prioritiser.investigation_cost,
        "scope": {"start": w["helix_warmup_start"], "test_start": w["test_start"], "end": w["test_end"],
                  "max_rows": a.max_rows or None},
        "rows_in_scope": rep.in_scope, "warmup_rows": n_warm, "test_rows": n_test,
        "load_s": round(load_s, 1), "warmup_s": round(warm_s, 1), "test_s": round(test_s, 1),
        "cases_after_warmup": cases_after_warm, "cases_total": len(cases),
        "cases_admitted": m.cases_admitted, "worker_errors": state.worker_errors,
        "auto_cleared": (state.triage.cleared_total if state.triage is not None else None),
    }
    (RESULTS / f"{a.out}.run.json").write_text(json.dumps(run, indent=2))
    print(f"wrote {out} ({len(cases)} cases)")
    print(json.dumps(run, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
