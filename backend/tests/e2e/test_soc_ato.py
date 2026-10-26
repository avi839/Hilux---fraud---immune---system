"""End-to-end cyber fusion (Req 28, 29): an account takeover read as one incident.

JSON documents -> StreamIngestor (``submit`` / ``submit_auth``) -> Helix with ``soc.enabled``.
The victim has a week of routine payments, each after a login from the known device. Then:
6 failed logins in 3 minutes -> LOGIN_SUCCESS from a new device -> NEW_PAYEE_ADDED -> a large
transfer to that payee in the attacker's session. Expected: one Case whose most advanced
Kill_Chain_Stage is CASH_OUT, whose Expected_Loss carries the Kill_Chain_Multiplier, and whose
explanation names the auth evidence.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from helix.cases import Case
from helix.config import HelixConfig, KillChainStage
from helix.core import Helix
from helix.core.pipeline import AuthOutcome, EventOutcome
from helix.explain import EntryKind, dumps, explain, loads
from helix.ingest import StreamIngestor
from helix.sim.clock import ManualClock
from helix.soc import soc_config

START = datetime(2024, 6, 1, tzinfo=timezone.utc)
VICTIM, SHOP, MULE = "ACC-VICTIM", "SHOP-1", "MULE-1"
KNOWN_DEV, ATK_DEV, ATK_SESSION = "DEV-V1", "DEV-ATK", "S-ATK"
ATTACK_AT = START + timedelta(days=8, hours=10)


def const_clock() -> float:
    return 0.0


def tx_doc(tx_id: str, ts: datetime, dst: str, amount: str, device: str, session: str) -> str:
    return json.dumps({"tx_id": tx_id, "ts": ts.isoformat(), "src": VICTIM, "dst": dst, "amount": amount,
                       "currency": "EUR", "channel": "online", "lat": 52.52, "lon": 13.405,
                       "device_id": device, "session_id": session})


def auth_doc(event_id: str, ts: datetime, kind: str, session: str, device: str, **extra: str) -> str:
    return json.dumps({"event_id": event_id, "ts": ts.isoformat(), "account_id": VICTIM, "session_id": session,
                       "event_type": kind, "device_id": device, "ip": "203.0.113.7", **extra,
                       "unknown_field": "ignored"})


def scenario() -> tuple[list[str], list[str]]:
    """(transaction docs, auth docs) of the routine week plus the takeover."""
    txs, auths = [], []
    auths.append(auth_doc("A-ENROL", START - timedelta(days=1), "LOGIN_SUCCESS", "S-ENROL", KNOWN_DEV))
    for d in range(8):
        ts = START + timedelta(days=d, hours=10)
        s = f"S-R{d}"
        auths.append(auth_doc(f"A-R{d}", ts - timedelta(seconds=10), "LOGIN_SUCCESS", s, KNOWN_DEV))
        txs.append(tx_doc(f"T-R{d}", ts, SHOP, f"{48 + d % 4}.50", KNOWN_DEV, s))
    t = ATTACK_AT
    for i in range(6):  # >= the default failed-login count of 5, within 10 minutes
        auths.append(auth_doc(f"A-F{i}", t, "LOGIN_FAILURE", f"S-F{i}", ATK_DEV))
        t += timedelta(seconds=30)
    auths.append(auth_doc("A-LOGIN", t, "LOGIN_SUCCESS", ATK_SESSION, ATK_DEV))
    auths.append(auth_doc("A-PAYEE", t + timedelta(seconds=40), "NEW_PAYEE_ADDED", ATK_SESSION, ATK_DEV,
                          payee_account_id=MULE))
    txs.append(tx_doc("T-ATO", t + timedelta(minutes=2), MULE, "4800.00", ATK_DEV, ATK_SESSION))
    return txs, auths


def run(cfg: HelixConfig) -> tuple[StreamIngestor, Helix, list]:
    ing = StreamIngestor(cfg.ingest, ManualClock(START))
    txs, auths = scenario()
    for doc in auths:
        assert ing.submit_auth(doc).accepted
    for doc in txs:
        assert ing.submit(doc).accepted
    h = Helix(cfg, ManualClock(START), sensor_clock=const_clock)
    return ing, h, [h.process(adm) for adm in ing.drain()]


@pytest.fixture(scope="module")
def soc_run():
    return run(soc_config())


def ato_case(h: Helix) -> Case:
    cs = [c for c in h.cases.open_cases() if "T-ATO" in c.tx_ids]
    assert len(cs) == 1, [(c.case_id, sorted(c.tx_ids)) for c in h.cases.open_cases()]
    return cs[0]


def test_ato_sequence_yields_cash_out_case_with_auth_reason(soc_run):
    ing, h, outs = soc_run
    assert ing.auth_counters.delivered == 17 and ing.counters.delivered == 9
    assert sum(isinstance(o, AuthOutcome) and o.recorded for o in outs) == 17
    login = next(o for o in outs if isinstance(o, AuthOutcome) and o.event.event_id == "A-LOGIN")
    assert login.new_device

    case = ato_case(h)
    assert case.tx_ids == {"T-ATO"}  # Auth_Events alone never create Cases
    kc = h.kill_chain(case.case_id)
    assert kc is not None and kc.most_advanced is KillChainStage.CASH_OUT
    assert case.kill_chain_stage is KillChainStage.CASH_OUT
    stages = [s.stage for s in kc.stages]
    assert stages == sorted(stages, key=list(KillChainStage).index)  # stage order (Req 29.4)
    for st in (KillChainStage.CREDENTIAL_ACCESS, KillChainStage.ACCOUNT_TAKEOVER,
               KillChainStage.PAYEE_MANIPULATION, KillChainStage.CASH_OUT):
        assert st in stages, (st, stages)
    first = {s.stage: s.first_ts for s in kc.stages}
    assert first[KillChainStage.CREDENTIAL_ACCESS] == ATTACK_AT  # the first failed login
    assert first[KillChainStage.CREDENTIAL_ACCESS] < first[KillChainStage.PAYEE_MANIPULATION] \
        < first[KillChainStage.CASH_OUT]

    # Kill_Chain_Multiplier applied before Case_Priority (Req 29.6, 29.7)
    assert case.expected_loss_base is not None
    assert case.expected_loss == (case.expected_loss_base * Decimal("1.5")).quantize(Decimal("0.01"))
    assert case.priority == h.prioritiser.priority_for(case.expected_loss)
    assert case.priority >= h.prioritiser.priority_for(case.expected_loss_base)

    # Evidence_Chain: auth sensor entry naming the suspicious events, plus AUTH_EVENT links
    chain = h.chain(case.case_id)
    sensor = next(e for e in chain.contributors() if e.contributor_id == "auth")
    assert sensor.mass.f > 0
    ids = set(str(sensor.features["suspicious_ids"]).split("|"))
    assert {"A-F0", "A-F5", "A-LOGIN", "A-PAYEE"} <= ids
    assert "A-R7" not in ids  # the routine login is not suspicious
    linked = {e.detail["event_id"]: e for e in chain.of_kind(EntryKind.AUTH_EVENT)}
    assert linked["A-LOGIN"].detail["linked_by"] == "session+device"  # Req 28.7
    assert linked["A-PAYEE"].detail["signal"] == "new_payee"
    assert chain.verify() == []
    assert loads(dumps(chain)) == chain

    ex = explain(case, chain, currency="EUR")
    auth = [it for it in ex.reasons if it.name == "auth"]
    assert auth, [it.name for it in ex.reasons]
    assert "failed logins" in auth[0].comparison and "new device DEV-ATK" in auth[0].comparison
    assert "new payee MULE-1" in auth[0].comparison
    assert any("auth LOGIN_SUCCESS A-LOGIN" in n for n in ex.notes)


def test_routine_payments_create_no_case(soc_run):
    _, h, outs = soc_run
    for o in outs:
        if isinstance(o, EventOutcome) and o.event.tx_id.startswith("T-R"):
            assert o.case_id is None, o.event.tx_id


def test_same_stream_with_flag_off_ignores_auth_and_never_multiplies():
    ing, h, outs = run(HelixConfig())
    assert all(not o.recorded for o in outs if isinstance(o, AuthOutcome))
    assert h.auth is None and h.auth_ignored == 17
    assert "auth" not in {s.name for s in h.stage1.sensors}
    for case in h.cases.cases.values():
        assert case.kill_chain_stage is None and case.expected_loss_base is None
        assert "kill_chain_stage" not in h.case_summary(case)
