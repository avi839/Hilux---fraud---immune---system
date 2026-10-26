"""Replay an IBM AMLworld ``*_Trans.csv`` file through a headless HELIX pipeline.

The API_Server has no Dataset_Loader endpoint or flag, so this script wires the pieces
itself: ``DatasetLoader.load`` writes the labels (and optional ``*_Patterns.txt``
membership) to an in-memory Ground_Truth_Store, then ``replay`` submits the events to the
same AppState pipeline the API_Server uses (no HTTP server, no Storage). At the end it
prints the load report and the Evaluation_Monitor's dataset summary (Req 4.8, 4.20, 4.21).

Run from anywhere (scripts\\aml.ps1 wraps it):

    backend\\.venv\\Scripts\\python.exe scripts\\aml_replay.py data\\HI-Small_Trans.csv \\
        --patterns data\\HI-Small_Patterns.txt --max-rows 20000
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1] / "backend"
sys.path.insert(0, str(BACKEND))

from helix.api.state import AppState  # noqa: E402
from helix.config import startup_config  # noqa: E402
from helix.datasets import DatasetLoader, LoaderError, ReplayScope, validate_speed  # noqa: E402
from helix.evaluation import EvaluationMonitor, ground_truth_channel  # noqa: E402
from helix.sim.clock import ManualClock  # noqa: E402


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Replay an AMLworld transactions CSV through HELIX.")
    p.add_argument("trans", type=Path, help="AMLworld *_Trans.csv file")
    p.add_argument("--patterns", type=Path, default=None, help="matching *_Patterns.txt file (optional)")
    p.add_argument("--config", default=None, help="YAML/JSON HelixConfig file (dataset.* settings apply)")
    p.add_argument("--max-rows", type=int, default=20_000,
                   help="replay only the first N rows in timestamp order (default 20000; 0 = no limit)")
    p.add_argument("--speed", default="max",
                   help='"max" (default: as fast as backpressure allows) or a multiplier in [1, 1000000]')
    p.add_argument("--load-only", action="store_true", help="parse and report only; do not run the pipeline")
    p.add_argument("--log-level", default="warning")
    return p.parse_args()


def fmt(r: object) -> str:
    return f"{r:.3f}" if isinstance(r, float) else str(r)


async def main() -> int:
    args = parse_args()
    logging.basicConfig(level=args.log_level.upper(), format="%(levelname)s %(name)s: %(message)s")
    speed: object = args.speed if args.speed == "max" else float(args.speed)
    validate_speed(speed)
    cfg = startup_config(args.config)

    # Labels are written at load time, so start the clock before any AMLworld timestamp and
    # move it to the first in-scope event afterwards (the pipeline advances it from there).
    clock = ManualClock(datetime(2000, 1, 1, tzinfo=timezone.utc))
    writer, reader = ground_truth_channel(None)
    base = ReplayScope.from_config(cfg.dataset)
    scope = ReplayScope(base.start, base.end, args.max_rows or base.max_rows)

    state = None
    if not args.load_only:
        monitor = EvaluationMonitor(reader)
        state = AppState.build(cfg, clock, monitor=monitor, gt_writer=writer, gt_reader=reader)
        loader = DatasetLoader(cfg.dataset, state.ingestor, clock, writer)
    else:
        loader = DatasetLoader(cfg.dataset, _NoIngest(), clock, writer)

    t0 = time.monotonic()
    rep = loader.load(args.trans, args.patterns, scope)
    if isinstance(rep, LoaderError):
        print(f"load failed: {rep}", file=sys.stderr)
        return 1
    print(f"Loaded {rep.path} in {time.monotonic() - t0:.1f} s (sort: {rep.sort_mode})")
    print(f"  rows read {rep.rows_read}, parsed {rep.parsed}, skipped {rep.skipped}, "
          f"out of scope {rep.out_of_scope}, in scope {rep.in_scope} ({rep.laundering} laundering)")
    if rep.skip_reasons:
        for reason, n in sorted(rep.skip_reasons.items(), key=lambda kv: -kv[1])[:5]:
            print(f"    skipped {n}: {reason}")
    if rep.first_ts is not None and rep.last_ts is not None:
        print(f"  in-scope time span {rep.first_ts.isoformat()} .. {rep.last_ts.isoformat()} "
              f"({(rep.last_ts - rep.first_ts).total_seconds() / 3600:.1f} h)")
    if args.patterns is not None:
        print(f"  pattern rows matched {rep.pattern_rows_matched}, unmatched {rep.unmatched_pattern_rows}, "
              f"patterns in scope {sum(rep.patterns.values())}, by type {rep.patterns}")
    if state is None or not rep.in_scope:
        return 0

    if rep.first_ts is not None and rep.first_ts > clock.now():
        clock.set(rep.first_ts)
    await state.start()
    try:
        t1 = time.monotonic()
        task = asyncio.create_task(loader.replay(speed))  # type: ignore[arg-type]
        last = t1
        while not task.done() or len(state.ingestor) > 0:
            await asyncio.sleep(0.2)
            if time.monotonic() - last >= 5:
                last = time.monotonic()
                print(f"  ... {state.ingestor.counters.delivered} events processed, "
                      f"queue {len(state.ingestor)}, cases {len(state.helix.cases.cases)}")
        rep = await task
        took = time.monotonic() - t1
    finally:
        await state.stop()

    print(f"Replayed {rep.emitted} events in {took:.1f} s ({rep.emitted / max(took, 1e-9):.0f} ev/s); "
          f"worker errors {state.worker_errors}")
    print(f"  cases opened {len(state.helix.cases.cases)}")
    m = state.monitor.metrics()
    # Per hour of replayed data. The monitor's own alerts_per_hour also spans the pre-data
    # clock start above (the first Prioritiser period begins there), so it is not used here.
    span_h = (rep.last_ts - rep.first_ts).total_seconds() / 3600 if rep.first_ts and rep.last_ts else 0.0
    per_h = f"{m.cases_admitted / span_h:.1f}" if span_h > 0 else "n/a"
    print(f"  alerts (Cases admitted to the Active_Queue) {m.cases_admitted}, per data hour {per_h}, "
          f"detected events {m.detected_events}, fraud events ingested {m.fraud_events_ingested}, "
          f"fraud events detected {m.fraud_events_detected}, latency median {fmt(m.latency_median_s)} s")
    s = state.monitor.dataset_summary()
    print(f"  precision {fmt(s.precision)}, recall {fmt(s.recall)}, "
          f"captured fraud amount {fmt(s.captured_fraud_fraction)}")
    for t, st in s.pattern_types.items():
        print(f"  {t:<15} mapped={st.mapped!s:<5} motif={st.motif or '-':<12} "
              f"detected {st.detected_patterns}/{st.patterns} ({fmt(st.detection_rate)}), "
              f"recall {fmt(st.recall)}, precision {fmt(st.precision) if st.precision is not None else 'n/a'}")
    print(f"  unmapped types {list(s.unmapped_types)}")
    return 0


class _NoIngest:
    """Ingest stub for --load-only (replay is never called)."""

    backpressure = False

    def submit(self, raw: object) -> object:  # pragma: no cover - unused
        raise RuntimeError("--load-only does not replay")


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
