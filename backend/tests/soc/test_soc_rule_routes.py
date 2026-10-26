"""SOC technique and Detection_Rule routes (helix.api.soc_routes, Req 30, 31), mounted with
``include_soc_routes`` on an app from ``create_app(state)``."""

from __future__ import annotations

import json
import time
from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient

from helix.api import AppState, create_app
from helix.api.soc_routes import include_soc_routes
from helix.config import HelixConfig
from helix.sim.clock import ManualClock
from helix.soc import soc_config

pytestmark = pytest.mark.integration

API = "/api/v1"
START = datetime(2024, 6, 1, tzinfo=timezone.utc)
JSON = {"content-type": "application/json"}
YAML = {"content-type": "application/yaml"}
RULE = """\
id: R-BIG
title: Large payment
techniques: [HX-T0008, HX-T0014]
detection:
  big:
    tx.amount|gte: 1000
  condition: big
mass:
  fraud: 0.5
"""


def const_clock() -> float:
    return 0.0


def make_client(cfg: HelixConfig) -> tuple[TestClient, AppState]:
    state = AppState.build(cfg, ManualClock(START), learner=False, sensor_clock=const_clock)
    return TestClient(include_soc_routes(create_app(state), state)), state


def wait_for(pred, timeout: float = 10.0) -> None:
    deadline = time.monotonic() + timeout
    while not pred():
        if time.monotonic() > deadline:
            raise AssertionError("timed out waiting for the pipeline")
        time.sleep(0.01)


def tx_doc(tx_id: str, amount: str, src: str) -> str:
    return json.dumps({"tx_id": tx_id, "ts": START.isoformat(), "src": src, "dst": "X", "amount": amount,
                       "currency": "EUR"})


def test_catalogue_and_coverage_carry_version_notice_and_gaps():
    client, _ = make_client(soc_config())
    with client:
        cat = client.get(f"{API}/soc/techniques").json()
        assert cat["version"] == "1.0.0" and "not official MITRE data" in cat["notice"]
        assert {t["domain"] for t in cat["techniques"]} == {"FRAUD", "CYBER"}
        cov = client.get(f"{API}/soc/coverage").json()
        assert cov["catalogue_version"] == "1.0.0" and "not official MITRE data" in cov["notice"]
        assert set(cov["gaps"]) == {"HX-T0014", "HX-T0099", "HX-T0107", "HX-T0108"}
        row = next(r for r in cov["techniques"] if r["technique_id"] == "HX-T0105")
        assert row["contributors"] == [{"contributor_id": "auth", "kind": "SENSOR"}] and row["gap"] is False
        assert row["detection_rate"] is None and row["campaigns"] == 0


def test_rule_lifecycle_submit_backtest_activate_and_emit():
    client, state = make_client(soc_config())
    with client:
        bad = client.post(f"{API}/soc/rules", content=RULE.replace("tx.amount", "tx.amnt"), headers=YAML)
        assert bad.status_code == 422
        [err] = bad.json()["errors"]
        assert err["line"] == 6 and "unknown field 'tx.amnt'" in err["reason"]
        assert client.get(f"{API}/soc/rules").json()["rules"] == []

        r = client.post(f"{API}/soc/rules", content=json.dumps({"yaml": RULE}), headers=JSON)
        assert r.status_code == 201, r.text
        body = r.json()
        assert body["rule_id"] == "R-BIG" and body["version"] == 1 and body["active_version"] is None
        assert body["backtested"] is True and body["backtest"]["window_s"] == 7 * 86_400
        assert body["yaml"].startswith("id: R-BIG\nversion: 1\n")

        r2 = client.post(f"{API}/soc/rules", content=RULE.replace("1000", "900"), headers=YAML)
        assert r2.status_code == 201 and r2.json()["version"] == 2

        bt = client.post(f"{API}/soc/rules/R-BIG/versions/1/backtest", json={"window_s": 3600})
        assert bt.status_code == 200 and bt.json()["window_s"] == 3600 and bt.json()["hits"] == 0
        assert client.post(f"{API}/soc/rules/R-BIG/versions/9/backtest").status_code == 404
        assert client.post(f"{API}/soc/rules/R-BIG/versions/1/backtest", json={"window_s": 1}).status_code == 422

        act = client.post(f"{API}/soc/rules/R-BIG/activate", json={"version": 1, "by": "inv-1"})
        assert act.status_code == 200, act.text
        assert act.json()["active_version"] == 1 and act.json()["activated_by"] == "inv-1"
        assert client.post(f"{API}/soc/rules/R-NONE/activate").status_code == 404

        listed = client.get(f"{API}/soc/rules").json()["rules"]
        assert [(x["rule_id"], x["latest_version"], x["active_version"]) for x in listed] == [("R-BIG", 2, 1)]
        one = client.get(f"{API}/soc/rules/R-BIG").json()
        assert [v["version"] for v in one["versions"]] == [1, 2] and one["versions"][0]["backtest"]["hits"] == 0
        assert client.get(f"{API}/soc/rules/R-NONE").status_code == 404

        cov = client.get(f"{API}/soc/coverage").json()
        assert "HX-T0014" not in cov["gaps"]
        row = next(x for x in cov["techniques"] if x["technique_id"] == "HX-T0014")
        assert row["contributors"] == [{"contributor_id": "rule:R-BIG", "kind": "RULE", "version": 1}]

        assert client.post(f"{API}/events", content=tx_doc("T-1", "5000.00", "NEW"), headers=JSON).status_code == 202

        def case_of():
            return state.helix.cases.case_of("T-1")

        wait_for(lambda: case_of() is not None and state.helix.chain(case_of()) is not None)
        cid = case_of()
        tags = client.get(f"{API}/cases/{cid}/techniques").json()
        assert {t["technique_id"] for t in tags["techniques"]} == {"HX-T0008", "HX-T0014"}
        assert {"contributor_id": "rule:R-BIG", "tx_id": "T-1", "ctype": "RULE",
                "techniques": ["HX-T0008", "HX-T0014"]} in tags["contributors"]
        ev = client.get(f"{API}/cases/{cid}/evidence").json()
        assert any("rule R-BIG version 1" in it["comparison"] for it in ev["explanation"]["reasons"]), ev
        assert client.get(f"{API}/cases/C-404/techniques").status_code == 404

        off = client.post(f"{API}/soc/rules/R-BIG/deactivate")
        assert off.status_code == 200 and off.json()["previous_active_version"] == 1
        assert "HX-T0014" in client.get(f"{API}/soc/coverage").json()["gaps"]
        assert "/api/v1/soc/rules" in client.get("/openapi.json").json()["paths"]


def test_activation_is_503_with_the_flag_off_and_409_without_backtest():
    client, state = make_client(HelixConfig())
    with client:
        r = client.post(f"{API}/soc/rules", content=RULE, headers=YAML)
        assert r.status_code == 201 and r.json()["backtested"] is True
        assert client.post(f"{API}/soc/rules/R-BIG/activate").status_code == 503
        assert state.helix.rules is None  # the pipeline never evaluates rules with the flag off
        cov = client.get(f"{API}/soc/coverage").json()
        assert "HX-T0105" in cov["gaps"]  # no Auth_Sensor without cyber fusion

    client, state = make_client(soc_config())
    with client:
        state.helix.rules.source = None  # no backtest source: the version stays un-backtested
        assert client.post(f"{API}/soc/rules", content=RULE, headers=YAML).json()["backtested"] is False
        r = client.post(f"{API}/soc/rules/R-BIG/activate")
        assert r.status_code == 409 and "backtested" in r.json()["detail"]
        assert client.post(f"{API}/soc/rules", content=b"{not json", headers=JSON).status_code == 422
