"""API tests for the Attacker_Agent routes and the ``--attacker`` / ``--immune`` CLI flags."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient

from helix.api import AppState, secure_app
from helix.api.__main__ import build_state, parse_args
from helix.config import HelixConfig
from helix.config.schema import GeneratorConfig
from helix.evaluation import EvaluationMonitor, ground_truth_channel
from helix.sim import SyntheticGenerator
from helix.sim.clock import ManualClock

pytestmark = pytest.mark.integration

API = "/api/v1"
START = datetime(2024, 6, 2, tzinfo=timezone.utc)


def const_clock() -> float:
    return 0.0


def gen_state() -> AppState:
    writer, reader = ground_truth_channel(None)
    cfg = HelixConfig(generator=GeneratorConfig(accounts=30, rate_eps=1))
    clock = ManualClock(START)
    gen = SyntheticGenerator(cfg.generator, clock, writer, seed=1)
    return AppState.build(cfg, clock, monitor=EvaluationMonitor(reader), generator=gen, gt_writer=writer,
                          gt_reader=reader, sensor_clock=const_clock)


def test_attacker_routes_without_generator():
    state = AppState.build(HelixConfig(), ManualClock(START), sensor_clock=const_clock)
    with TestClient(secure_app(state)) as client:
        body = client.get(f"{API}/attacker").json()
        assert body["configured"] is False and body["running"] is False and body["available"] is False
        assert body["recent_campaigns"] == [] and body["genomes_total"] == 0
        r = client.post(f"{API}/sim/attacker/start")
        assert r.status_code == 409 and "Synthetic_Generator" in r.json()["detail"]
        r = client.post(f"{API}/sim/attacker/stop")
        assert r.status_code == 200 and r.json() == {"running": False, "changed": False}
    assert state.attacker is None


def test_attacker_start_stop_and_stats():
    state = gen_state()
    with TestClient(secure_app(state)) as client:
        assert client.get(f"{API}/attacker").json()["available"] is True
        r = client.post(f"{API}/sim/attacker/start")
        assert r.status_code == 200 and r.json() == {"running": True, "changed": True}
        assert client.post(f"{API}/sim/attacker/start").json() == {"running": True, "changed": False}
        body = client.get(f"{API}/attacker").json()
        assert body["configured"] is True and body["running"] is True
        assert body["genomes_total"] == 7 and body["genomes_active"] == 7  # one per pattern type (Req 20.1)
        assert isinstance(body["seed"], int)
        for k in ("injected_total", "withheld_total", "pending_events", "campaigns_total", "campaigns_live",
                  "campaigns_completed", "mean_evasion", "recent_campaigns", "top_genomes"):
            assert k in body
        assert client.get(f"{API}/status").json()["attacker"] == {"configured": True, "running": True}
        r = client.post(f"{API}/sim/attacker/stop")
        assert r.json() == {"running": False, "changed": True}
        assert client.get(f"{API}/attacker").json()["running"] is False
        assert client.post(f"{API}/sim/attacker/stop").json()["changed"] is False


def test_attacker_campaign_is_reported():
    state = gen_state()
    runner = state.ensure_attacker()
    runner.step()  # first launch is due at attach time
    with TestClient(secure_app(state)) as client:
        body = client.get(f"{API}/attacker", params={"recent": 5}).json()
    assert body["campaigns_total"] >= 1
    c = body["recent_campaigns"][0]
    assert {"campaign_id", "genome_id", "pattern_type", "started_at", "events", "injected", "evasion",
            "completed"} <= set(c)
    assert c["events"] >= 1 and c["pattern_type"] is not None


def test_cli_flags_enable_attacker_and_immune():
    args = parse_args(["--attacker", "--immune", "--seed", "7"])
    assert args.attacker and args.immune
    state = build_state(args)
    assert state.immune is not None and state.miss_detector is not None
    assert state.attacker is not None and state.attacker_autostart is True
    assert state.attacker.agent.seed == 7
    assert state.attacker.agent.writer is state.gt_writer  # labels reach the monitor's reader

    plain = build_state(parse_args([]))
    assert plain.immune is None and plain.attacker is None and plain.attacker_autostart is False


def test_cli_attacker_without_generator_is_ignored():
    state = build_state(parse_args(["--attacker", "--no-generator"]))
    assert state.attacker is None and state.attacker_autostart is False


def test_attacker_autostart_runs_with_lifespan():
    state = gen_state()
    state.ensure_attacker()
    state.attacker_autostart = True
    with TestClient(secure_app(state)) as client:
        assert client.get(f"{API}/attacker").json()["running"] is True
    assert state.attacker_running() is False  # cancelled on shutdown
