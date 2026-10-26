"""Playbook and Canary routes (helix.api.soc_response_routes, Req 32, 33), mounted with
``include_response_routes`` on an app from ``create_app(state)``."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient

from helix.api import AppState, create_app
from helix.api.soc_response_routes import include_response_routes
from helix.config import HelixConfig
from helix.sim.clock import ManualClock
from helix.soc.canaries import CanaryKind, CanaryRegistry
from helix.soc.playbooks import PlaybookRunner

pytestmark = pytest.mark.integration

API = "/api/v1"
START = datetime(2024, 6, 1, tzinfo=timezone.utc)


@dataclass
class FakeCase:
    case_id: str
    tier: str
    accounts: set = field(default_factory=set)
    tx_ids: set = field(default_factory=set)


class FakeLedger:
    def __init__(self) -> None:
        self.entries: list = []

    def append(self, category, actor, payload):
        self.entries.append((category, actor, dict(payload)))


def make_client(**state_attrs) -> tuple[TestClient, AppState]:
    state = AppState.build(HelixConfig(), ManualClock(START), learner=False, sensor_clock=lambda: 0.0)
    for k, v in state_attrs.items():
        setattr(state, k, v)
    return TestClient(include_response_routes(create_app(state), state)), state


def test_playbook_listing_actions_and_approval_flow():
    led = FakeLedger()
    client, state = make_client(audit_ledger=led)
    with client:
        doc = client.get(f"{API}/soc/playbooks").json()
        assert {p["id"] for p in doc["playbooks"]} == {"PB-ATO-CONTAIN", "PB-CRITICAL-HOLD", "PB-MEDIUM-STEPUP"}
        assert all(p["active"] for p in doc["playbooks"])
        assert doc["dry_run"] is False and doc["approval_threshold"] == "HIGH" and "simulated" in doc["notice"]

        runner: PlaybookRunner = state.playbook_runner  # created by the router, audit sink wired
        runner.on_case_update(FakeCase("C-1", "MEDIUM", {"ACC000001"}, {"T1"}))
        runner.on_case_update(FakeCase("C-2", "CRITICAL", {"ACC000002"}, {"T2"}))

        acts = client.get(f"{API}/soc/playbook-actions").json()
        assert acts["count"] == 4 and [a["status"] for a in acts["actions"]] == [
            "PERFORMED", "PERFORMED", "PENDING", "PENDING"]
        pend = client.get(f"{API}/soc/playbook-actions", params={"status": "PENDING", "case_id": "C-2"}).json()
        hold, notify = pend["actions"]
        assert hold["type"] == "HOLD_TRANSACTION" and hold["targets"] == ["T2"] and hold["simulated"] is True
        assert client.get(f"{API}/soc/playbook-actions", params={"status": "DONE"}).status_code == 422

        ok = client.post(f"{API}/soc/playbook-actions/{hold['action_id']}/approve", json={"by": "alice"})
        assert ok.status_code == 200 and ok.json()["status"] == "PERFORMED" and ok.json()["decided_by"] == "alice"
        again = client.post(f"{API}/soc/playbook-actions/{hold['action_id']}/approve", json={"by": "alice"})
        assert again.status_code == 409 and again.json()["action_status"] == "PERFORMED"
        rej = client.post(f"{API}/soc/playbook-actions/{notify['action_id']}/reject",
                          json={"by": "bob", "reason": "duplicate"})
        assert rej.json()["status"] == "REJECTED" and rej.json()["decision_reason"] == "duplicate"
        assert client.post(f"{API}/soc/playbook-actions/PA-999999/approve", json={"by": "a"}).status_code == 404
        assert client.post(f"{API}/soc/playbook-actions/PA-000001/approve", json={}).status_code == 422
        assert client.post(f"{API}/soc/playbook-actions/nope/approve", json={"by": "a"}).status_code == 422

        st = client.get(f"{API}/soc/playbooks/state").json()
        assert [h["id"] for h in st["held_transactions"]] == ["T2"]
        assert [s["id"] for s in st["step_up_accounts"]] == ["ACC000001"]
        assert [n["channel"] for n in st["notifications"]] == ["fraud-ops"]
    events = [(actor, p["event"]) for _, actor, p in led.entries]
    assert ("alice", "approved") in events and ("bob", "rejected") in events


def test_dry_run_toggle():
    client, state = make_client()
    with client:
        assert client.post(f"{API}/soc/playbooks/dry-run", json={"enabled": "yes"}).status_code == 422
        assert client.post(f"{API}/soc/playbooks/dry-run", json={"enabled": True, "by": "lead"}).json() == {
            "dry_run": True}
        state.playbook_runner.on_case_update(FakeCase("C-1", "CRITICAL", {"A"}, {"T"}))
        acts = client.get(f"{API}/soc/playbook-actions").json()
        assert acts["dry_run"] is True and {a["status"] for a in acts["actions"]} == {"DRY_RUN"}
        assert client.get(f"{API}/soc/playbooks/state").json()["held_transactions"] == []
        assert client.post(f"{API}/soc/playbooks/dry-run", json={"enabled": False}).json() == {"dry_run": False}


def test_canary_hits_expose_only_touched_canaries():
    reg = CanaryRegistry.generate(9)
    client, _ = make_client(canaries=reg, canary_campaign_of=lambda eid: "camp-1" if eid == "T1" else None)
    acc, pay = reg.ids(CanaryKind.ACCOUNT)[0], reg.ids(CanaryKind.PAYEE)[0]
    with client:
        empty = client.get(f"{API}/soc/canaries/hits").json()
        assert empty == {"hit_count": 0, "dropped_hits": 0, "hits": [], "touched": [], "campaigns": []}

        from helix.codec import TransactionEvent

        reg.observe(TransactionEvent(tx_id="T1", ts=START, src="ACC000001", dst=acc, amount="5.00", currency="USD"))
        reg.observe(TransactionEvent(tx_id="T2", ts=START, src="ACC000002", dst=pay, amount="5.00", currency="USD"))
        r = client.get(f"{API}/soc/canaries/hits", params={"limit": 10})
        doc = r.json()
        assert doc["hit_count"] == 2 and [h["canary_id"] for h in doc["hits"]] == [acc, pay]
        assert doc["campaigns"][0]["campaign_id"] == "camp-1"
        assert doc["campaigns"][0]["canaries"][0]["canary_id"] == acc
        # untouched Canaries never appear anywhere in the payload
        untouched = set(reg.ids()) - {acc, pay}
        assert not any(u in r.text for u in untouched)
        assert client.get(f"{API}/soc/canaries/hits", params={"limit": 0}).status_code == 422


def test_default_canaries_come_from_the_generator_seed():
    client, state = make_client()
    with client:
        assert client.get(f"{API}/soc/canaries/hits").status_code == 200
    assert state.canaries.ids() == CanaryRegistry.generate(HelixConfig().generator.default_seed).ids()
