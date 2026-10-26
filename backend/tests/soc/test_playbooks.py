"""Response_Playbooks and the Playbook_Runner (helix.soc.playbooks, Req 32, 27.11)."""

from __future__ import annotations

import socket
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Mapping

import pytest

from helix.config import SeverityTier
from helix.soc.playbooks import (
    AUDIT_CATEGORY,
    SYSTEM_ACTOR,
    ActionNotFound,
    ActionStateError,
    ActionStatus,
    Playbook,
    PlaybookActionType,
    PlaybookRunner,
    PlaybookValidationError,
    format_playbook,
    load_playbooks,
    parse_playbook,
)

T0 = datetime(2024, 6, 1, tzinfo=timezone.utc)


@dataclass
class FakeCase:
    case_id: str
    tier: Any
    accounts: set = field(default_factory=set)
    tx_ids: set = field(default_factory=set)
    techniques: tuple = ()
    is_open: bool = True


class FakeLedger:
    """In-memory stand-in for the Audit_Ledger's ``append(category, actor, payload)``."""

    def __init__(self) -> None:
        self.entries: list[tuple[str, str, dict]] = []

    def append(self, category: str, actor: str, payload: Mapping[str, Any]) -> int:
        self.entries.append((category, actor, dict(payload)))
        return len(self.entries) - 1

    def events(self) -> list[tuple[str, str, str]]:
        return [(p["event"], p.get("action_id", ""), actor) for _, actor, p in self.entries]


def pb(id_="PB-T", version=1, trigger=None, actions=None, **kw) -> dict:
    return {"id": id_, "version": version, "title": "test", "trigger": trigger or {"tiers": ["LOW"]},
            "actions": [{"type": "NOTIFY"}] if actions is None else actions, **kw}


def runner(*pbs, **kw) -> PlaybookRunner:
    clock = iter(T0 + timedelta(seconds=i) for i in range(10_000))
    return PlaybookRunner([Playbook.from_dict(p) for p in pbs], clock=lambda: next(clock), **kw)


# --- definition validation (Req 32.1) ----------------------------------------------------


def test_bundled_playbooks_load_and_round_trip():
    pbs = load_playbooks()
    assert {p.playbook_id for p in pbs} == {"PB-ATO-CONTAIN", "PB-CRITICAL-HOLD", "PB-MEDIUM-STEPUP"}
    for p in pbs:
        assert parse_playbook(format_playbook(p)) == p


@pytest.mark.parametrize("doc,field_", [
    (pb(actions=[{"type": "NOTIFY"}, {"type": "WIRE_FUNDS"}]), "actions[1].type"),
    (pb(trigger={"tiers": ["LOW", "SEVERE"]}), "trigger.tiers[1]"),
    (pb(trigger={"min_tier": "URGENT"}), "trigger.min_tier"),
    (pb(trigger={"techniques": ["HX-T0001", "HX-T9999"]}), "trigger.techniques[1]"),
    (pb(trigger={"tiers": ["LOW"], "severity": "HIGH"}), "trigger.severity"),
    (pb(trigger={"match": "all"}), "trigger"),
    (pb(actions=[{"type": "NOTIFY", "params": {"url": "https://x"}}]), "actions[0].params.url"),
    (pb(actions=[{"type": "FREEZE_ACCOUNT", "params": {"channel": "a"}}]), "actions[0].params.channel"),
    (pb(version=0), "version"),
    (pb(id_="bad id"), "id"),
    (pb(actions=[]), "actions"),
    ({**pb(), "webhook": "https://x"}, "webhook"),
])
def test_invalid_definition_is_rejected_naming_the_offending_field(doc, field_):
    with pytest.raises(PlaybookValidationError) as ei:
        Playbook.from_dict(doc)
    assert field_ in ei.value.fields


def test_yaml_errors_and_unsafe_tags_are_rejected():
    with pytest.raises(PlaybookValidationError) as ei:
        parse_playbook("id: [unclosed\n")
    assert "invalid YAML" in str(ei.value)
    with pytest.raises(PlaybookValidationError):  # safe_load refuses python object tags
        parse_playbook("!!python/object/apply:os.system ['echo hi']\n")


def test_conflicting_redefinition_of_a_version_is_rejected_and_identical_readd_is_a_noop():
    r = runner(pb())
    r.add(pb())  # identical: fine
    with pytest.raises(PlaybookValidationError) as ei:
        r.add(pb(actions=[{"type": "HOLD_TRANSACTION"}]))
    assert ei.value.fields == ("version",)
    assert len(r.playbooks()) == 1


# --- trigger matching and ordered scheduling (Req 32.2) -------------------------------------


def test_trigger_matches_on_tiers_min_tier_and_techniques():
    r = runner(pb("PB-TIER", trigger={"tiers": ["MEDIUM"]}),
               pb("PB-MIN", trigger={"min_tier": "HIGH"}),
               pb("PB-TECH", trigger={"techniques": ["HX-T0006"]}),
               pb("PB-ALL", trigger={"min_tier": "HIGH", "techniques": ["HX-T0103"], "match": "all"}))
    ids = lambda tier, tech=(): [p.playbook_id for p in r.matching(tier, tech)]  # noqa: E731
    assert ids("LOW") == []
    assert ids("MEDIUM") == ["PB-TIER"]
    assert ids(SeverityTier.CRITICAL) == ["PB-MIN"]
    assert ids("LOW", ["HX-T0006"]) == ["PB-TECH"]
    assert ids("MEDIUM", ["HX-T0103"]) == ["PB-TIER"]
    assert ids("HIGH", ["HX-T0103"]) == ["PB-ALL", "PB-MIN"]


def test_only_the_highest_enabled_version_is_matched():
    r = runner(pb(version=1), pb(version=2, trigger={"tiers": ["HIGH"]}))
    assert r.matching("LOW") == [] and [p.version for p in r.matching("HIGH")] == [2]
    r.add(pb(version=3, enabled=False))
    assert r.matching("HIGH") == [] and r.active_playbooks() == []


def test_actions_are_scheduled_in_defined_order_with_case_targets():
    acts = [{"type": "FREEZE_ACCOUNT"}, {"type": "HOLD_TRANSACTION"}, {"type": "STEP_UP_AUTH"},
            {"type": "NOTIFY", "params": {"channel": "ops", "message": "hi"}}]
    r = runner(pb(actions=acts))
    out = r.on_case_update(FakeCase("C-1", "LOW", {"A2", "A1"}, {"T9", "T1"}))
    assert [a.type.value for a in out] == [a["type"] for a in acts]
    assert [a.step for a in out] == [0, 1, 2, 3]
    assert out[0].targets == ("A1", "A2") and out[1].targets == ("T1", "T9") and out[3].targets == ("ops",)
    assert all(a.status is ActionStatus.PERFORMED for a in out)
    st = r.state
    assert st.is_frozen("A1") and st.is_frozen("A2") and st.is_held("T1") and st.requires_step_up("A2")
    assert [(n.channel, n.message, n.case_id) for n in st.notifications] == [("ops", "hi", "C-1")]


def test_closed_cases_are_not_scheduled():
    r = runner(pb())
    assert r.on_case_update(FakeCase("C-1", "LOW", is_open=False)) == []
    assert r.actions() == []


def test_techniques_keyword_overrides_case_attribute():
    r = runner(pb(trigger={"techniques": ["HX-T0006"]}))
    assert r.on_case_update(FakeCase("C-1", "LOW", techniques=("HX-T0001",))) == []
    assert len(r.on_case_update(FakeCase("C-1", "LOW"), techniques=["HX-T0006"])) == 1


# --- approval, dry-run, audit (Req 32.5-32.7) ------------------------------------------------


def test_actions_at_or_above_threshold_wait_for_approval():
    led = FakeLedger()
    r = runner(pb(trigger={"min_tier": "MEDIUM"}, actions=[{"type": "HOLD_TRANSACTION"}, {"type": "FREEZE_ACCOUNT"}]),
               audit=led)
    medium = r.on_case_update(FakeCase("C-1", "MEDIUM", {"A1"}, {"T1"}))
    assert [a.status for a in medium] == [ActionStatus.PERFORMED] * 2  # below HIGH: automatic
    hold, freeze = r.on_case_update(FakeCase("C-2", "HIGH", {"A2"}, {"T2"}))
    assert hold.status is freeze.status is ActionStatus.PENDING and hold.requires_approval
    assert not r.state.is_held("T2") and not r.state.is_frozen("A2")
    assert [a.action_id for a in r.actions(status="PENDING")] == [hold.action_id, freeze.action_id]

    r.approve(hold.action_id, by="alice")
    assert hold.status is ActionStatus.PERFORMED and hold.decided_by == "alice" and r.state.is_held("T2")
    r.reject(freeze.action_id, by="bob", reason="customer verified")
    assert freeze.status is ActionStatus.REJECTED and not r.state.is_frozen("A2")
    with pytest.raises(ActionStateError):
        r.approve(freeze.action_id, by="alice")
    with pytest.raises(ActionNotFound):
        r.reject("PA-999999", by="alice")
    with pytest.raises(ValueError):
        r.approve(hold.action_id, by="")

    tail = [(e, actor) for e, aid, actor in led.events() if aid in (hold.action_id, freeze.action_id)]
    assert tail == [("scheduled", SYSTEM_ACTOR), ("scheduled", SYSTEM_ACTOR), ("approved", "alice"),
                    ("performed", "alice"), ("rejected", "bob")]
    assert {c for c, _, _ in led.entries} == {AUDIT_CATEGORY}
    p = led.entries[-1][2]
    assert p["case_id"] == "C-2" and p["action"] == "FREEZE_ACCOUNT" and p["playbook_id"] == "PB-T"
    assert p["simulated"] is True and p["targets"] == ["A2"]


def test_dry_run_records_actions_without_changing_simulated_state():
    led = FakeLedger()
    r = runner(pb(trigger={"min_tier": "LOW"}, actions=[{"type": "HOLD_TRANSACTION"}, {"type": "NOTIFY"}]),
               audit=led, dry_run=True)
    before = r.state.snapshot()
    out = r.on_case_update(FakeCase("C-1", "CRITICAL", {"A1"}, {"T1"}))
    assert [a.status for a in out] == [ActionStatus.DRY_RUN] * 2 and not out[0].requires_approval
    assert r.state.snapshot() == before
    assert [e for e, _, _ in led.events()] == ["scheduled", "dry_run", "scheduled", "dry_run"]


def test_pending_action_approved_while_dry_run_is_on_becomes_dry_run():
    led = FakeLedger()
    r = runner(pb(trigger={"tiers": ["HIGH"]}, actions=[{"type": "FREEZE_ACCOUNT"}]), audit=led)
    [a] = r.on_case_update(FakeCase("C-1", "HIGH", {"A1"}))
    assert r.set_dry_run(True, by="lead") is True
    r.approve(a.action_id, by="alice")
    assert a.status is ActionStatus.DRY_RUN and not r.state.is_frozen("A1")
    assert ("dry_run_mode", "", "lead") in led.events()


def test_failing_audit_sink_is_counted_and_does_not_stop_the_runner():
    class Broken:
        def append(self, category, actor, payload):
            raise OSError("disk full")

    r = runner(pb(), audit=Broken())
    [a] = r.on_case_update(FakeCase("C-1", "LOW"))
    assert a.status is ActionStatus.PERFORMED and r.audit_errors == 2  # scheduled + performed


# --- idempotence (Req 32.8) ---------------------------------------------------------------


def test_each_playbook_version_runs_at_most_once_per_case():
    r = runner(pb())
    case = FakeCase("C-1", "LOW", {"A1"}, {"T1"})
    assert len(r.on_case_update(case)) == 1
    for i in range(5):
        case.tx_ids.add(f"T{i + 2}")
        assert r.on_case_update(case) == []
    assert len(r.on_case_update(FakeCase("C-2", "LOW"))) == 1  # another Case runs it again
    r.add(pb(version=2))
    assert [a.playbook_version for a in r.on_case_update(case)] == [2]  # a new version runs once
    assert r.on_case_update(case) == []
    assert r.runs("C-1") == [("C-1", "PB-T", 1), ("C-1", "PB-T", 2)]


def test_rejected_or_dry_run_versions_are_not_rescheduled():
    r = runner(pb(trigger={"tiers": ["HIGH"]}))
    [a] = r.on_case_update(FakeCase("C-1", "HIGH"))
    r.reject(a.action_id, by="bob")
    assert r.on_case_update(FakeCase("C-1", "HIGH")) == []
    r.set_dry_run(True)
    r.on_case_update(FakeCase("C-2", "HIGH"))
    r.set_dry_run(False)
    assert r.on_case_update(FakeCase("C-2", "HIGH")) == []


# --- simulated only (Req 32.3, 32.4, 27.11) -------------------------------------------------


def test_playbook_actions_never_open_a_network_connection(monkeypatch):
    def no_network(*a, **k):
        raise AssertionError("Playbook_Action attempted a network connection")

    monkeypatch.setattr(socket, "socket", no_network)
    monkeypatch.setattr(socket, "create_connection", no_network)
    monkeypatch.setattr(socket, "getaddrinfo", no_network)
    led = FakeLedger()
    r = PlaybookRunner.with_defaults(audit=led, clock=lambda: T0)
    case = FakeCase("C-1", "HIGH", {"A1"}, {"T1"}, techniques=("HX-T0006",))
    acts = r.on_case_update(case)
    acts += r.on_case_update(FakeCase("C-2", "MEDIUM", {"A2"}, {"T2"}))
    acts += r.on_case_update(FakeCase("C-3", "CRITICAL", {"A3"}, {"T3"}))
    assert {a.type for a in acts} == set(PlaybookActionType)
    for a in r.actions(status=ActionStatus.PENDING):
        r.approve(a.action_id, by="alice")
    assert all(a.status is ActionStatus.PERFORMED for a in acts)
    assert r.state.is_held("T1") and r.state.is_frozen("A1") and r.state.requires_step_up("A2")
    assert r.state.notifications and len(led.entries) > 0


def test_runner_audit_entries_are_accepted_by_the_audit_ledger():
    """Compatibility with 16.4's Audit_Ledger (``append(category, actor, payload)``)."""
    ledger_mod = pytest.importorskip("helix.soc.ledger")
    led = ledger_mod.AuditLedger(clock=lambda: T0)
    r = runner(pb(trigger={"min_tier": "LOW"}), audit=led)
    [a] = r.on_case_update(FakeCase("C-1", "HIGH", {"A1"}, {"T1"}))
    r.approve(a.action_id, by="alice")
    r.set_dry_run(True, by="lead")
    assert r.audit_errors == 0 and len(led) == 4 and led.verify().valid
