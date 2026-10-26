"""Run the HELIX API_Server: ``python -m helix.api --config path``.

Builds the AppState from a validated HelixConfig (refusing to start on any config error),
optionally with DuckDB Storage, an Evaluation_Monitor, the Synthetic_Generator, the
Attacker_Agent (``--attacker``), the Immune_System (``--immune``) and cyber fusion with the
SOC components (``--soc``, persisted in Storage when ``--storage`` is given), and serves
it with uvicorn on ``api.bind_host:api.port`` (loopback by default; no authentication).

Dataset mode (Req 39, 40): ``--dataset amlworld --trans F [--patterns P]`` or ``--dataset csv
--mapping M --trans F``, with ``--speed max|N``, ``--max-rows N`` (default 20000, 0 = no limit),
``--warmup`` and ``--warmup-until ISO``. The files are checked before serving (exit 1 with
``dataset: <path>: <reason>``); ``--dataset`` with ``--generator`` is an argparse error.
"""

from __future__ import annotations

import argparse
import logging
import math
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional, Sequence

from helix.config import startup_config
from helix.datasets.feed import DEFAULT_MAX_ROWS, DatasetFeeder, DatasetSpec, DatasetStartupError
from helix.evaluation import EvaluationMonitor, ground_truth_channel
from helix.sim import SyntheticGenerator
from helix.sim.clock import ManualClock

from .app import secure_app
from .security import check_bind
from .state import AppState

log = logging.getLogger("helix.api")


def dataset_spec(args: argparse.Namespace) -> Optional[DatasetSpec]:
    """The validated ``--dataset`` options (Req 39.1, 40.2, 40.7), or None."""
    if getattr(args, "dataset", None) is None:
        return None
    return DatasetSpec(
        kind=args.dataset, trans=Path(args.trans),
        patterns=None if args.patterns is None else Path(args.patterns),
        mapping=None if args.mapping is None else Path(args.mapping),
        speed=args.speed, max_rows=args.max_rows or None,
        warmup=bool(args.warmup), warmup_until=args.warmup_until,
    )


def precheck_dataset(spec: DatasetSpec) -> DatasetFeeder:
    """Open and check the dataset files before anything is served (Req 39.9): on failure print
    ``dataset: <path>: <reason>`` to stderr and exit 1."""
    feeder = DatasetFeeder(spec, None)
    try:
        feeder.precheck()
    except DatasetStartupError as exc:
        print(f"dataset: {exc.path}: {exc.reason}", file=sys.stderr)
        raise SystemExit(1) from None
    return feeder


def build_state(args: argparse.Namespace) -> AppState:
    spec = dataset_spec(args)
    feeder = precheck_dataset(spec) if spec is not None else None
    cfg = startup_config(args.config)  # exits non-zero, listing every error, if invalid
    if feeder is not None:
        # Labels are written at load time, so stream time starts before any dataset
        # timestamp; the feeder moves it to the first in-scope row (as aml_replay.py does).
        clock = ManualClock(datetime(2000, 1, 1, tzinfo=timezone.utc))
    else:
        clock = ManualClock(datetime.now(timezone.utc))  # event-driven stream time
    storage = None
    if args.storage:
        from helix.storage import Storage

        storage = Storage.from_config(cfg).open(args.storage)
    writer, reader = ground_truth_channel(storage)
    monitor = EvaluationMonitor(reader)
    immune = bool(args.immune or cfg.immune.enabled)
    soc = bool(getattr(args, "soc", False) or cfg.soc.enabled)
    if soc and not cfg.soc.enabled:
        # Before the generator is built: it emits benign Auth_Events only with soc.enabled.
        cfg = cfg.model_copy(update={"soc": cfg.soc.model_copy(update={"enabled": True})})
    generator = None
    attacker = bool(args.attacker or cfg.attacker.enabled)
    # Dataset mode: the generator is configured only as the Attacker_Agent's account pool and
    # is never started (Req 39.8).
    if not args.no_generator and (feeder is None or attacker):
        generator = SyntheticGenerator(cfg.generator, clock, writer, seed=args.seed, soc=cfg.soc)
    state = AppState.build(cfg, clock, storage=storage, monitor=monitor, generator=generator, immune=immune,
                           gt_writer=writer, gt_reader=reader, soc=soc)
    state.generator_autostart = bool(args.generator) and feeder is None
    if feeder is not None:
        feeder.state = state
        state.dataset = feeder
    state.attacker_seed = args.seed
    if args.attacker or cfg.attacker.enabled:
        if generator is None:
            log.warning("--attacker ignored: the Attacker_Agent needs the Synthetic_Generator (--no-generator set)")
        else:
            state.ensure_attacker()
            state.attacker_autostart = True
    return state


def parse_args(argv: Optional[Sequence[str]] = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(prog="python -m helix.api", description="Run the HELIX API_Server.")
    p.add_argument("--config", default=None, help="YAML/JSON HelixConfig file (defaults when omitted)")
    p.add_argument("--storage", default=None, help="DuckDB file; in-memory only when omitted")
    p.add_argument("--host", default=None, help="override api.bind_host")
    p.add_argument("--port", type=int, default=None, help="override api.port")
    p.add_argument("--generator", action="store_true", help="start the Synthetic_Generator at startup")
    p.add_argument("--no-generator", action="store_true", help="do not configure the Synthetic_Generator")
    p.add_argument("--seed", type=int, default=None,
                   help="generator / attacker seed (default generator.default_seed)")
    p.add_argument("--attacker", action="store_true",
                   help="attach and start the Attacker_Agent at startup (also when attacker.enabled)")
    p.add_argument("--immune", action="store_true",
                   help="enable the Immune_System and its miss detection (also when immune.enabled)")
    p.add_argument("--soc", action="store_true",
                   help="enable cyber fusion and the SOC components (also when soc.enabled)")
    d = p.add_argument_group("dataset streaming (Req 39, 40)")
    d.add_argument("--dataset", choices=("amlworld", "csv"), default=None,
                   help="stream a dataset file into the live pipeline (excludes --generator)")
    d.add_argument("--trans", default=None, help="transactions CSV (AMLworld *_Trans.csv or a bank export)")
    d.add_argument("--patterns", default=None, help="AMLworld *_Patterns.txt (amlworld only)")
    d.add_argument("--mapping", default=None, help="Column_Mapping YAML/JSON (required with --dataset csv)")
    d.add_argument("--speed", type=_speed, default="max",
                   help='"max" (default: as fast as backpressure allows) or a multiplier in [1, 1000000]')
    d.add_argument("--max-rows", type=int, default=DEFAULT_MAX_ROWS,
                   help=f"stream only the first N rows in timestamp order (default {DEFAULT_MAX_ROWS}; 0 = no limit)")
    d.add_argument("--warmup", action="store_true",
                   help="Warm_Up_Mode: build DNA / CPD / graph state at max speed with Case creation suppressed")
    d.add_argument("--warmup-until", type=_iso_datetime, default=None,
                   help="with --warmup: warm up only on rows before this ISO 8601 time (naive = UTC)")
    p.add_argument("--allow-missing-ws-origin", action="store_true",
                   help="accept WebSocket handshakes without an Origin header (non-browser clients); "
                        "a wrong Origin is still rejected")
    p.add_argument("--log-level", default="info")
    args = p.parse_args(argv)
    _check_dataset_args(p, args)
    return args


def _speed(s: str) -> object:
    if s == "max":
        return "max"
    try:
        v = float(s)
    except ValueError:
        raise argparse.ArgumentTypeError('must be "max" or a number in [1, 1000000]') from None
    if not (math.isfinite(v) and 1 <= v <= 1_000_000):
        raise argparse.ArgumentTypeError('must be "max" or a number in [1, 1000000]')
    return v


def _iso_datetime(s: str) -> datetime:
    try:
        t = datetime.fromisoformat(s[:-1] + "+00:00" if s[-1:] in "Zz" and s else s)
    except ValueError:
        raise argparse.ArgumentTypeError(f"not an ISO 8601 date-time: {s!r}") from None
    return t.replace(tzinfo=timezone.utc) if t.tzinfo is None else t.astimezone(timezone.utc)


def _check_dataset_args(p: argparse.ArgumentParser, a: argparse.Namespace) -> None:
    """argparse errors (exit 2) for inconsistent dataset options (Req 39.7)."""
    if a.dataset is not None and a.generator:
        p.error("--dataset and --generator are mutually exclusive")
    if a.dataset is None:
        given = [f for f, v in (("--trans", a.trans), ("--patterns", a.patterns), ("--mapping", a.mapping),
                                ("--warmup", a.warmup), ("--warmup-until", a.warmup_until)) if v]
        if given:
            p.error(f"{', '.join(given)} need --dataset")
        return
    if a.trans is None:
        p.error("--dataset needs --trans")
    if a.patterns is not None and a.dataset != "amlworld":
        p.error("--patterns is accepted only with --dataset amlworld")
    if a.dataset == "csv" and a.mapping is None:
        p.error("--dataset csv needs --mapping")
    if a.mapping is not None and a.dataset != "csv":
        p.error("--mapping is accepted only with --dataset csv")
    if a.max_rows < 0:
        p.error("--max-rows must be >= 0 (0 = no limit)")
    if a.warmup_until is not None and not a.warmup:
        p.error("--warmup-until needs --warmup")


def main(argv: Optional[Sequence[str]] = None) -> None:
    import uvicorn

    args = parse_args(argv)
    logging.basicConfig(level=args.log_level.upper(), format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    state = build_state(args)
    host = args.host or state.cfg.api.bind_host
    port = args.port or state.cfg.api.port
    check_bind(host, log)  # Req 27.2: warn before serving on a non-loopback address
    app = secure_app(state, allow_missing_ws_origin=args.allow_missing_ws_origin)
    uvicorn.run(app, host=host, port=port, log_level=args.log_level.lower())


if __name__ == "__main__":
    main()
