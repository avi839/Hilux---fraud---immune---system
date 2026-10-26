"""SOC integration in the API_Server (task 18.1, Req 28-37): every SOC router is mounted by
``create_app`` / ``secure_app``, ``AppState`` builds the SOC components (``soc=True`` or
``soc.enabled``) with Storage persistence, ``SocConfig`` carries their settings and
``python -m helix.api --soc`` turns it all on."""

from __future__ import annotations

from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from helix.api import AppState, create_app, secure_app
from helix.api.__main__ import build_state, parse_args
from helix.api.soc_intel_routes import include_intel_routes
from helix.api.soc_ops_routes import include_ops_routes
from helix.api.soc_response_routes import include_response_routes
from helix.api.soc_routes import include_soc_routes
from helix.config import HelixConfig, SeverityTier, SocConfig
from helix.sim.clock import ManualClock
from helix.soc.canaries import CanaryKind, CanaryRegistry
from helix.soc.console import SocConsole
from helix.soc.intel import TLP, Indicator, IndicatorStore, IndicatorType
from helix.soc.ledger import AuditLedger
from helix.soc.playbooks import PlaybookRunner
from helix.soc.purple import PurpleTeam
from helix.soc.resilience import ResilienceMode
from helix.soc.rules import RuleEngine
from helix.storage import Storage

API = "/api/v1"
START = datetime(2024, 6, 1, tzinfo=timezone.utc)
RULES_DIR = Path(__file__).resolve().parents[2] / "rules"

SOC_PATHS = {
    f"{API}/auth-events", f"{API}/cases/{{case_id}}/killchain", f"{API}/soc/techniques", f"{API}/soc/coverage",
    f"{API}/soc/rules", f"{API}/soc/playbooks", f"{API}/soc/playbook-actions", f"{API}/soc/canaries/hits",
    f"{API}/soc/indicators", f"{API}/soc/indicators/export", f"{API}/soc/audit", f"{API}/soc/audit/verify",
    f"{API}/soc/console", f"{API}/soc/handoff-notes", f"{API}/soc/purple/scorecards", f"{API}/soc/resilience",
}


def const_clock() -> float:
    return 0.0


def make_state(cfg: HelixConfig | None = None, **kw) -> AppState:
    return AppState.build(cfg or HelixConfig(), ManualClock(START), learner=False, sensor_clock=const_clock, **kw)


def paths(app) -> list[str]:
    return [getattr(r, "path", "") for r in app.routes]


def test_create_and_secure_app_mount_every_soc_router_once():
    for factory in (create_app, secure_app):
        st = make_state()
        app = factory(st, manage_lifecycle=False)
        assert SOC_PATHS <= set(paths(app))
        before = Counter(paths(app))
        for include in (include_soc_routes, include_intel_routes, include_response_routes, include_ops_routes):
            assert include(app, st) is app  # idempotent on a create_app app
        assert Counter(paths(app)) == before


def test_soc_disabled_by_default_routes_answer_with_in_memory_defaults():
    st = make_state()
    assert st.soc_enabled is False
    assert all(v is None for v in st.soc_components().values())  # nothing built eagerly
    with TestClient(create_app(st, manage_lifecycle=False)) as c:
        r = c.post(f"{API}/auth-events", json={"event_id": "A1"})
        assert r.status_code == 503
        assert c.get(f"{API}/soc/techniques").status_code == 200
        assert c.get(f"{API}/soc/playbooks").json()["approval_threshold"] == "HIGH"
        assert c.get(f"{API}/soc/audit/verify").json()["valid"] is True
        assert c.get(f"{API}/soc/resilience").status_code == 200
    with pytest.raises(ValueError):
        st.enable_soc()


def test_build_with_soc_builds_every_component_and_routes_share_them():
    cfg = HelixConfig(soc=SocConfig(playbook_approval_threshold=SeverityTier.CRITICAL, playbook_dry_run=True,
                                    canary_accounts=3, canary_payees=2, canary_devices=1,
                                    indicator_default_confidence=60, rules_dir=str(RULES_DIR)))
    st = make_state(cfg.model_copy(update={"soc": cfg.soc.model_copy(update={"enabled": False})}), soc=True)
    assert st.cfg.soc.enabled and st.soc_enabled  # forced on before the pipeline was built
    comp = st.soc_components()
    types = {"rules": RuleEngine, "playbook_runner": PlaybookRunner, "canaries": CanaryRegistry,
             "indicators": IndicatorStore, "ledger": AuditLedger, "soc_console": SocConsole,
             "purple": PurpleTeam, "resilience": ResilienceMode}
    assert {k: type(v) for k, v in comp.items()} == types
    h = st.helix
    assert h.rules is comp["rules"] and h.indicators is st.indicators and h.ledger is st.ledger
    assert st.audit_ledger is st.ledger and st.playbook_runner.audit is st.ledger
    assert st.playbook_runner.approval_threshold is SeverityTier.CRITICAL and st.playbook_runner.dry_run
    assert st.canaries.counts() == {"ACCOUNT": 3, "PAYEE": 2, "DEVICE": 1}
    assert st.indicators.cfg.default_confidence == 60
    assert set(h.rules.rule_ids()) >= {"HXR-0001", "HXR-0002"}  # rules_dir loaded at startup
    assert st.resilience.enabled is False
    built = dict(comp)
    assert st.enable_soc() == built  # idempotent

    with TestClient(create_app(st, manage_lifecycle=False)) as c:
        assert st.soc_components() == built  # the routers reuse the built components
        doc = c.get(f"{API}/soc/playbooks").json()
        assert doc["approval_threshold"] == "CRITICAL" and doc["dry_run"] is True
        assert {r["rule_id"] for r in c.get(f"{API}/soc/rules").json()["rules"]} >= {"HXR-0001", "HXR-0002"}
        assert c.post(f"{API}/soc/handoff-notes", json={"author": "ana", "text": "hi"}).status_code == 201
        assert len(st.soc_console.notes) == 1
        assert c.post(f"{API}/soc/playbooks/dry-run", json={"enabled": False, "by": "ana"}).json() == {"dry_run": False}
        audit = c.get(f"{API}/soc/audit").json()
        assert audit["total"] == len(st.ledger) >= 1  # the playbook toggle was audited into the ledger


def test_same_seed_gives_the_same_canaries():
    a, b = make_state(soc=True), make_state(soc=True)
    assert a.canaries.ids(CanaryKind.ACCOUNT) == b.canaries.ids(CanaryKind.ACCOUNT)


def test_soc_state_persists_in_storage_across_restarts(tmp_path):
    db = tmp_path / "soc.duckdb"
    store = Storage(db).open()
    st = make_state(storage=store, soc=True)
    st.indicators.add(Indicator("MULE-9", IndicatorType.ACCOUNT, 80, "test", START - timedelta(days=1), START,
                                START + timedelta(days=30), TLP.AMBER))
    with TestClient(create_app(st, manage_lifecycle=False)) as c:
        assert c.post(f"{API}/soc/handoff-notes", json={"author": "ana", "text": "handover"}).status_code == 201
        assert c.post(f"{API}/soc/playbooks/dry-run", json={"enabled": True, "by": "ana"}).status_code == 200
        head = c.get(f"{API}/soc/audit").json()["head"]
    entries = len(st.ledger)
    assert entries >= 1
    store.close()

    store2 = Storage(db).open()
    try:
        st2 = make_state(storage=store2, soc=True)
        assert len(st2.ledger) == entries and st2.ledger.head().to_dict() == head
        with TestClient(create_app(st2, manage_lifecycle=False)) as c:
            assert c.get(f"{API}/soc/audit/verify").json()["valid"] is True
            assert [i["value"] for i in c.get(f"{API}/soc/indicators").json()["indicators"]] == ["MULE-9"]
            notes = c.get(f"{API}/soc/handoff-notes").json()["notes"]
            assert [(n["author"], n["text"]) for n in notes] == [("ana", "handover")]
    finally:
        store2.close()


def test_soc_config_new_fields_defaults_and_ranges():
    sc = SocConfig()
    assert sc.rules_dir is None and sc.playbook_approval_threshold is SeverityTier.HIGH
    assert sc.playbook_dry_run is False and (sc.canary_accounts, sc.canary_payees, sc.canary_devices) == (10, 10, 10)
    assert (sc.indicator_default_confidence, sc.indicator_default_expiry_days, sc.indicator_base_mass) == (75, 30.0, 0.8)
    bad = HelixConfig(soc=SocConfig(canary_accounts=-1, indicator_default_confidence=101,
                                    indicator_default_expiry_days=0, indicator_base_mass=1.5))
    assert {e.path for e in bad.validate()} == {"soc.canary_accounts", "soc.indicator_default_confidence",
                                                "soc.indicator_default_expiry_days", "soc.indicator_base_mass"}


def test_cli_soc_flag_builds_the_soc_state():
    assert parse_args([]).soc is False
    st = build_state(parse_args(["--soc", "--no-generator"]))
    assert st.cfg.soc.enabled and st.soc_enabled
    assert all(v is not None for v in st.soc_components().values())
    plain = build_state(parse_args(["--no-generator"]))
    assert plain.soc_enabled is False and plain.playbook_runner is None
