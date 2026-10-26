"""Threat-intel and Audit_Ledger routes (helix.api.soc_intel_routes, Req 34, 35.4, 35.7, 35.8),
mounted with ``include_intel_routes`` on an app from ``create_app(state)``."""

from __future__ import annotations

import dataclasses
import json
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient

from helix.api import AppState, create_app
from helix.api.soc_intel_routes import include_intel_routes
from helix.cases import Label
from helix.codec import TransactionEvent
from helix.fusion import Mass
from helix.motifs import MotifEngine
from helix.sensors import SensorOutput
from helix.sim.clock import ManualClock
from helix.soc import soc_config
from helix.soc.intel import TLP, Indicator, IndicatorStore, IndicatorType
from helix.soc.ledger import NOTICE, AuditLedger

pytestmark = pytest.mark.integration

API = "/api/v1"
START = datetime(2024, 6, 1, tzinfo=timezone.utc)


def make_client(**attrs) -> tuple[TestClient, AppState]:
    state = AppState.build(soc_config(), ManualClock(START), learner=False, sensor_clock=lambda: 0.0)
    for k, v in attrs.items():
        setattr(state, k, v)
    return TestClient(include_intel_routes(create_app(state), state)), state


def ind(value: str, t: IndicatorType = IndicatorType.ACCOUNT, tlp: TLP = TLP.AMBER, days: float = 30) -> Indicator:
    return Indicator(value, t, 80, "test", START - timedelta(days=1), START, START + timedelta(days=days), tlp)


def test_router_creates_and_exposes_stores_when_none_are_configured():
    client, state = make_client()
    with client:
        assert isinstance(state.indicators, IndicatorStore) and isinstance(state.ledger, AuditLedger)
        assert state.helix.indicators is state.indicators and state.helix.ledger is state.ledger
        body = client.get(f"{API}/soc/indicators").json()
        assert body["count"] == 0 and body["summary"]["config"]["default_confidence"] == 75


def test_list_export_import_indicators():
    store = IndicatorStore(clock=lambda: START)
    store.add(ind("MULE-1"))
    store.add(ind("DEV-1", IndicatorType.DEVICE, tlp=TLP.RED))
    store.add(ind("OLD", days=-0.5))  # expired at START
    client, _ = make_client(indicators=store)
    with client:
        body = client.get(f"{API}/soc/indicators").json()
        assert body["total"] == 3 and {i["value"]: i["expired"] for i in body["indicators"]} == {
            "DEV-1": False, "MULE-1": False, "OLD": True}
        live = client.get(f"{API}/soc/indicators", params={"include_expired": "false", "type": "ACCOUNT"}).json()
        assert [i["value"] for i in live["indicators"]] == ["MULE-1"]
        assert client.get(f"{API}/soc/indicators", params={"type": "PHONE"}).status_code == 422

        default = client.get(f"{API}/soc/indicators/export").json()
        pats = [o["pattern"] for o in default["objects"] if o["type"] == "indicator"]
        assert "[x-helix-device:device_id = 'DEV-1']" not in pats and len(pats) == 2  # TLP:RED excluded
        full = client.get(f"{API}/soc/indicators/export", params={"include_red": "true"}).json()
        assert sum(o["type"] == "indicator" for o in full["objects"]) == 3

        bad = json.loads(json.dumps(full))
        bad_obj = next(o for o in bad["objects"] if o["type"] == "indicator")
        bad_obj["confidence"] = "high"
        r = client.post(f"{API}/soc/indicators/import", content=json.dumps(bad))
        assert r.status_code == 200
        rep = r.json()
        assert rep["imported"] == 2 and rep["skipped"] == 2
        assert rep["rejected"] == [{"id": bad_obj["id"], "errors": [
            {"field": "confidence", "reason": "must be an integer in [0, 100]"}]}]
        assert client.post(f"{API}/soc/indicators/import", content="{").status_code == 422
        nb = client.post(f"{API}/soc/indicators/import", json={"type": "nope"})
        assert nb.status_code == 422 and "bundle" in nb.json()["detail"]


def test_audit_listing_and_verification_with_tampering():
    ledger = AuditLedger(clock=lambda: START)
    for i in range(5):
        ledger.append("PLAYBOOK_ACTION" if i % 2 else "CONFIG_CHANGE", "admin", {"i": i})
    client, _ = make_client(ledger=ledger)
    with client:
        page = client.get(f"{API}/soc/audit", params={"offset": 1, "limit": 2}).json()
        assert page["total"] == 5 and [e["index"] for e in page["entries"]] == [1, 2]
        assert page["head"] == {"index": 4, "hash": ledger.head().hash}
        assert set(page["entries"][0]) == {"index", "ts", "category", "actor", "payload", "prev_hash", "hash"}
        cat = client.get(f"{API}/soc/audit", params={"category": "playbook_action"}).json()
        assert cat["total"] == 2
        assert client.get(f"{API}/soc/audit", params={"category": "bogus thing"}).status_code == 422
        assert client.get(f"{API}/soc/audit", params={"limit": 0}).status_code == 422

        ok = client.get(f"{API}/soc/audit/verify").json()
        assert ok == {"valid": True, "first_broken_index": None, "head": {"index": 4, "hash": ledger.head().hash},
                      "entries": 5, "reason": None, "notice": NOTICE}
        assert NOTICE == "tamper-evident, not tamper-proof"
        ledger._entries[3] = dataclasses.replace(ledger._entries[3], payload_json='{"i":42}')  # simulate tampering
        broken = client.get(f"{API}/soc/audit/verify").json()
        assert broken["valid"] is False and broken["first_broken_index"] == 3 and broken["notice"] == NOTICE


class Hot:
    """Test sensor: every event is strongly suspicious, so the first event opens a Case."""

    name = "fixed"

    def evaluate(self, ev, dna_before, ctx):
        return SensorOutput(Mass(0.9, 0.0, 0.1), {})


def test_label_through_the_pipeline_extracts_indicators_and_audits_the_label():
    state = AppState.build(soc_config(), ManualClock(START), learner=False, sensor_clock=lambda: 0.0,
                           sensors=[Hot()], motif_engine=MotifEngine(detectors=()))
    client = TestClient(include_intel_routes(create_app(state), state))
    h = state.helix
    out = h.process(TransactionEvent(tx_id="T1", ts=START, src="VICTIM", dst="MULE", amount=Decimal("9999.00"),
                                     currency="EUR", device_id="DEV-Z"))
    h.open_case(out.case_id, "inv")
    h.label(out.case_id, "inv", Label.CONFIRMED_FRAUD, "")
    with client:
        vals = {i["value"] for i in client.get(f"{API}/soc/indicators").json()["indicators"]}
        assert {"VICTIM", "MULE", "DEV-Z"} <= vals
        entries = client.get(f"{API}/soc/audit").json()["entries"]
        # Exactly one label entry; the wired Playbook_Runner also audits its PLAYBOOK_ACTIONs.
        labels = [(e["category"], e["payload"]["case_id"]) for e in entries if e["category"] == "FEEDBACK_LABEL"]
        assert labels == [("FEEDBACK_LABEL", out.case_id)]
        assert {e["category"] for e in entries} <= {"FEEDBACK_LABEL", "PLAYBOOK_ACTION"}
