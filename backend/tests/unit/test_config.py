"""Unit tests for helix.config: defaults, named errors, cross-field rules, loading."""

from __future__ import annotations

import json

import pytest

from helix.config import (
    DEFAULT_BANDS,
    Band,
    ConfigLoadError,
    ConfigValidationError,
    HelixConfig,
    SeverityTier,
    ensure_valid,
    from_mapping,
    load_config,
    range_paths,
    startup_config,
    validate_bands,
    with_updates,
)


def paths(cfg: HelixConfig) -> list[str]:
    return [e.path for e in cfg.validate()]


def test_defaults_are_valid_and_match_requirements():
    cfg = HelixConfig()
    assert cfg.validate() == []
    assert cfg.ingest.lateness_tolerance_s == 300
    assert cfg.ingest.backpressure_limit == 10_000
    assert cfg.ingest.dedup_ttl_s >= cfg.graph.window_s == 7 * 86_400
    assert cfg.fusion.high_conflict == 0.95
    assert cfg.fusion.sensor_budget_ms == 10
    assert cfg.sensors.min_history_count == 5
    assert cfg.memory.half_life_days == 30
    assert cfg.graph.motif_budget_ms == 20
    assert cfg.graph.reporting_threshold == 10_000
    assert cfg.risk.alert_threshold == 0.6
    assert cfg.risk.bands == DEFAULT_BANDS
    assert cfg.prioritiser.alert_budget == 20 and cfg.prioritiser.period_s == 3_600
    assert (cfg.replay.hops, cfg.replay.max_nodes, cfg.replay.lead_h) == (2, 500, 24)
    assert cfg.storage.snapshot_interval_s == 60
    assert (cfg.generator.accounts, cfg.generator.rate_eps) == (1_000, 50)
    assert (cfg.learn.weight_lower, cfg.learn.weight_upper, cfg.learn.learning_rate) == (0.1, 1.0, 0.05)
    assert cfg.api.bind_host == "127.0.0.1"
    assert cfg.api.dashboard_origin == "http://localhost:5173"


def test_config_is_frozen():
    cfg = HelixConfig()
    with pytest.raises(Exception):
        cfg.fusion.high_conflict = 0.5  # type: ignore[misc]


@pytest.mark.parametrize(
    "path,value",
    [
        ("fusion.high_conflict", 0.0),  # Req 6.11: (0, 1] excludes 0
        ("fusion.high_conflict", 1.01),
        ("storage.snapshot_interval_s", 4),  # Req 24.4
        ("storage.snapshot_interval_s", 3_601),
        ("generator.accounts", 0),  # Req 3.12
        ("generator.rate_eps", 1_001),
        ("generator.default_seed", 4_294_967_296),
        ("risk.alert_threshold", 1.1),  # Req 13.12
        ("memory.half_life_days", 0.5),  # Req 8.8
        ("graph.motif_budget_ms", 0),  # Req 11.12
        ("prioritiser.period_s", 59),  # Req 14.15
        ("replay.hops", 5),  # Req 17.9
        ("learn.learning_rate", 0.0),  # Req 19.3
        ("dataset.speed", 0.5),  # Req 4.12
    ],
)
def test_single_out_of_range_setting_is_named(path, value):
    cfg = with_updates(HelixConfig(), {path: value})
    assert paths(cfg) == [path]


def test_boundaries_inclusive_where_specified():
    cfg = with_updates(
        HelixConfig(),
        {
            "fusion.high_conflict": 1.0,
            "storage.snapshot_interval_s": 5,
            "generator.default_seed": 4_294_967_295,
            "ingest.lateness_tolerance_s": 0,
            "dataset.speed": "max",
        },
    )
    assert cfg.validate() == []


def test_validate_collects_all_errors():
    cfg = with_updates(
        HelixConfig(),
        {"fusion.high_conflict": 0, "storage.snapshot_interval_s": 1, "generator.accounts": -3},
    )
    assert sorted(paths(cfg)) == ["fusion.high_conflict", "generator.accounts", "storage.snapshot_interval_s"]
    with pytest.raises(ConfigValidationError) as exc:
        ensure_valid(cfg)
    assert len(exc.value.errors) == 3


def test_dedup_ttl_must_cover_graph_window():
    cfg = with_updates(HelixConfig(), {"ingest.dedup_ttl_s": 3 * 86_400})
    assert paths(cfg) == ["ingest.dedup_ttl_s"]


def test_weight_bounds_must_be_ordered():
    cfg = with_updates(HelixConfig(), {"learn.weight_lower": 0.5, "learn.weight_upper": 0.5})
    assert paths(cfg) == ["learn.weight_lower"]


def _bands(*spec):
    return [Band(tier=t, lo=lo, hi=hi).model_dump() for t, lo, hi in spec]


L, M, H, C = SeverityTier.LOW, SeverityTier.MEDIUM, SeverityTier.HIGH, SeverityTier.CRITICAL


@pytest.mark.parametrize(
    "bands",
    [
        _bands((L, 0.6, 0.7), (M, 0.75, 0.8), (H, 0.8, 0.9), (C, 0.9, 1.0)),  # gap
        _bands((L, 0.6, 0.75), (M, 0.7, 0.8), (H, 0.8, 0.9), (C, 0.9, 1.0)),  # overlap
        _bands((M, 0.6, 0.7), (L, 0.7, 0.8), (H, 0.8, 0.9), (C, 0.9, 1.0)),  # misorder
        _bands((L, 0.6, 0.7), (M, 0.7, 0.8), (H, 0.8, 0.9), (C, 0.9, 0.95)),  # short cover
        _bands((L, 0.5, 0.7), (M, 0.7, 0.8), (H, 0.8, 0.9), (C, 0.9, 1.0)),  # not at threshold
    ],
)
def test_broken_bands_name_band_setting(bands):
    cfg = with_updates(HelixConfig(), {"risk.bands": bands})
    assert paths(cfg) == ["risk.bands"]


def test_bands_follow_alert_threshold():
    bands = _bands((L, 0.5, 0.7), (M, 0.7, 0.8), (H, 0.8, 0.9), (C, 0.9, 1.0))
    cfg = with_updates(HelixConfig(), {"risk.bands": bands, "risk.alert_threshold": 0.5})
    assert cfg.validate() == []
    assert validate_bands(DEFAULT_BANDS, 0.6) == []


def test_runtime_settings_are_only_budget_and_cost():
    runtime = {p for p, r in range_paths(HelixConfig).items() if r.runtime}
    assert runtime == {"prioritiser.alert_budget", "prioritiser.investigation_cost"}


def test_load_defaults_yaml_and_json(tmp_path):
    assert load_config(None) == HelixConfig()
    y = tmp_path / "helix.yaml"
    y.write_text("fusion:\n  high_conflict: 0.9\nprioritiser:\n  alert_budget: 7\n", encoding="utf-8")
    cfg = load_config(y)
    assert cfg.fusion.high_conflict == 0.9 and cfg.prioritiser.alert_budget == 7
    assert cfg.storage == HelixConfig().storage  # unspecified sections keep defaults
    j = tmp_path / "helix.json"
    j.write_text(json.dumps({"replay": {"hops": 3}}), encoding="utf-8")
    assert load_config(j).replay.hops == 3
    empty = tmp_path / "empty.yaml"
    empty.write_text("", encoding="utf-8")
    assert load_config(empty) == HelixConfig()


def test_load_reports_all_type_and_key_errors():
    with pytest.raises(ConfigLoadError) as exc:
        from_mapping({"fusion": {"high_conflict": "high", "bogus": 1}, "nope": {}})
    locs = {e.path for e in exc.value.errors}
    assert {"fusion.high_conflict", "fusion.bogus", "nope"} <= locs


def test_load_does_not_stop_at_out_of_range_values():
    cfg = from_mapping({"fusion": {"high_conflict": 5}, "replay": {"hops": 99}})
    assert sorted(paths(cfg)) == ["fusion.high_conflict", "replay.hops"]


def test_startup_config_exits_nonzero_and_logs_every_error(tmp_path, capsys):
    bad = tmp_path / "bad.yaml"
    bad.write_text("fusion:\n  high_conflict: 0\nstorage:\n  snapshot_interval_s: 1\n", encoding="utf-8")
    with pytest.raises(SystemExit) as exc:
        startup_config(bad)
    assert exc.value.code != 0
    err = capsys.readouterr().err
    assert "fusion.high_conflict" in err and "storage.snapshot_interval_s" in err


def test_startup_config_accepts_valid_file(tmp_path):
    good = tmp_path / "good.yaml"
    good.write_text("generator:\n  accounts: 10\n", encoding="utf-8")
    assert startup_config(good).generator.accounts == 10
