"""Detection-as-Code Rule_Engine (Req 31): schema, line-numbered errors, versioning, backtest,
activation, pipeline emission and ground-truth isolation."""

from __future__ import annotations

import json
import time
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

import pytest

from helix.codec import AuthEvent, AuthEventType, TransactionEvent
from helix.config import HelixConfig
from helix.core import Helix
from helix.explain import EntryKind, dumps, explain, loads
from helix.fusion import ContributorType, fuse
from helix.memory import DNAView
from helix.sim.clock import ManualClock
from helix.soc import soc_config
from helix.soc.rules import (
    DEFAULT_BACKTEST_WINDOW_S,
    MAX_RULE_BYTES,
    RuleEngine,
    RuleNotFound,
    RuleStateError,
    RuleValidationError,
    StaticBacktestSource,
    attach_rules,
    auth_getter,
    backtest_rule,
    format_rule,
    parse_rule,
    references_ground_truth,
    tx_getter,
)
from helix.soc.techniques import case_techniques

T0 = datetime(2024, 6, 1, tzinfo=timezone.utc)
RULES_DIR = Path(__file__).resolve().parents[2] / "rules"

BIG = """\
id: R-BIG
title: Large payment
techniques: [HX-T0008]
detection:
  big:
    tx.amount|gte: 1000
  condition: big
mass:
  fraud: 0.5
"""


def tx(tx_id: str, ts: datetime, amount: str = "10.00", src: str = "A", dst: str = "B", **kw) -> TransactionEvent:
    return TransactionEvent(tx_id=tx_id, ts=ts, src=src, dst=dst, amount=Decimal(amount), currency="EUR", **kw)


def auth(event_id: str, ts: datetime, kind: str = "LOGIN_FAILURE", account: str = "A", **kw) -> AuthEvent:
    return AuthEvent(event_id=event_id, ts=ts, account_id=account, session_id="S", event_type=kind, **kw)


def problems(text: str) -> list:
    with pytest.raises(RuleValidationError) as ei:
        parse_rule(text)
    return list(ei.value.problems)


# --- parsing and validation (Req 31.1, 31.2, 31.9) ----------------------------------------------


def test_example_rules_directory_parses():
    eng = RuleEngine()
    res = eng.load_directory(RULES_DIR)
    assert [r.status for r in res] == ["accepted", "accepted"]
    assert eng.rule_ids() == ["HXR-0001", "HXR-0002"]
    r = eng.get("HXR-0001").rule
    assert r.version == 1 and r.techniques == ("HX-T0014",) and r.mass_fraud == 0.4
    assert eng.get("HXR-0002").rule.target.value == "auth"


def test_defaults_and_fields():
    r = parse_rule(BIG)
    assert (r.rule_id, r.title, r.status.value, r.target.value, r.version) == ("R-BIG", "Large payment",
                                                                               "experimental", "transaction", None)
    assert r.mass_legit == 0.0 and r.contributor_id == "rule:R-BIG" and r.fields == {"tx.amount"}
    assert r.mass.t == pytest.approx(0.5)


@pytest.mark.parametrize("text,line,needle", [
    (BIG.replace("tx.amount|gte", "tx.amont|gte"), 6, "unknown field 'tx.amont'"),
    (BIG.replace("tx.amount|gte", "tx.amount|regex"), 6, "modifier 'regex' is not allowed"),
    (BIG.replace("1000", "'1000'"), 6, "expected a number"),
    (BIG.replace("[HX-T0008]", "[HX-T9999]"), 3, "unknown HELIX_Technique id 'HX-T9999'"),
    (BIG.replace("[HX-T0008]", "[T1110]"), 3, "is not a HELIX_Technique id"),
    (BIG.replace("condition: big", "condition: big and small"), 7, "unknown selection 'small'"),
    (BIG.replace("condition: big", "condition: big and"), 7, "ends unexpectedly"),
    (BIG.replace("fraud: 0.5", "fraud: 0.8\n  legit: 0.5"), 8, "fraud + legit must be <= 1"),
    (BIG.replace("fraud: 0.5", "fraud: 0"), 8, "must not be vacuous"),
    (BIG.replace("fraud: 0.5", "fraud: 1.5"), 9, "must be a number in [0, 1]"),
    (BIG + "owner: me\n", 10, "unknown key"),
    (BIG.replace("id: R-BIG", "id: 'bad id'"), 1, "must match"),
    (BIG.replace("title: Large payment\n", ""), 1, "title: required key is missing"),
    (BIG.replace("  condition: big\n", ""), 4, "detection.condition: required key is missing"),
    (BIG.replace("big:\n", "and:\n").replace("condition: big", "condition: x"), 5, "selection names"),
    (BIG + "title: again\n", 10, "duplicate key"),
    (BIG.replace("tx.amount|gte: 1000", "tx.amount|gte: 2024-01-01"), 6, "unsupported value type 'timestamp'"),
])
def test_invalid_rules_report_line_and_cause(text, line, needle):
    ps = problems(text)
    hit = [p for p in ps if needle in str(p)]
    assert hit, [str(p) for p in ps]
    assert hit[0].line == line, [str(p) for p in ps]
    assert f"line {line}" in str(hit[0])


def test_yaml_syntax_error_has_line_number():
    [p] = problems("id: R\ntitle: t\ndetection: [unclosed\nmass: {fraud: 0.5}\n")
    assert p.line is not None and p.line >= 3 and "YAML syntax error" in p.message


def test_safe_loader_only_no_code_execution_no_aliases(tmp_path):
    marker = tmp_path / "pwned"
    evil = BIG.replace("title: Large payment",
                       f"title: !!python/object/apply:os.system ['echo x > {marker.as_posix()}']")
    ps = problems(evil)
    assert any("unsupported value type" in p.message or "unsupported YAML tag" in p.message for p in ps)
    assert not marker.exists()
    bomb = "a: &a [x, x]\nb: &b [*a, *a]\nc: [*b, *b]\n"
    assert any("aliases are not allowed" in p.message for p in problems(bomb))
    assert any("merge keys" in p.message for p in problems("base: &b {fraud: 0.5}\nmass:\n  <<: *b\n"))
    assert any("single document" in p.message for p in problems(BIG + "---\n" + BIG))
    assert any("larger than" in p.message for p in problems("x: " + "a" * (MAX_RULE_BYTES + 1)))
    assert problems("")[0].message == "empty document"
    assert "must be a YAML mapping" in problems("- a\n- b\n")[0].message


@pytest.mark.parametrize("field", [
    "ground_truth.label", "groundtruth.is_fraud", "gt.genome_id", "tx.label", "tx.is_fraud", "tx.campaign_id",
    "auth.pattern_type", "dna.genome_id", "labels.FRAUD", "truth.x", "attacker.campaign",
])
def test_rules_referencing_ground_truth_are_rejected(field):
    assert references_ground_truth(field)
    ps = problems(BIG.replace("tx.amount|gte: 1000", f"{field}: FRAUD"))
    [p] = [p for p in ps if "Ground_Truth_Store" in p.message]
    assert p.line == 6 and field in p.message


@pytest.mark.parametrize("field", ["tx.amount", "auth.event_id", "dna.count", "tx.src", "auth.payee_account_id"])
def test_ordinary_fields_are_not_ground_truth(field):
    assert not references_ground_truth(field)


def test_auth_fields_need_an_auth_target():
    text = BIG.replace("tx.amount|gte: 1000", "auth.event_type: LOGIN_FAILURE")
    assert any("for target transaction" in p.message for p in problems(text))
    ok = parse_rule(text.replace("techniques:", "target: auth\ntechniques:"))
    assert ok.target.value == "auth"
    bad = text.replace("techniques:", "target: auth\ntechniques:").replace("LOGIN_FAILURE", "LOGOUT")
    assert any("is not one of" in p.message for p in problems(bad))


# --- evaluation semantics ---------------------------------------------------------------------


def rule(sel: str, cond: str = "s", target: str = "transaction"):
    return parse_rule(f"id: R\ntitle: t\ntarget: {target}\ntechniques: [HX-T0008]\n"
                      f"detection:\n{sel}\n  condition: {cond}\nmass: {{fraud: 0.5}}\n")


def test_modifiers_lists_exists_and_missing_values():
    ev = tx("T1", T0.replace(hour=3), "1500.00", dst="MULE-7", channel="web")
    get = tx_getter(ev, DNAView.empty("A"))
    assert rule("  s:\n    tx.amount|gt: 1499.99\n    tx.hour|lt: 6").matches(get)
    assert not rule("  s:\n    tx.amount|gt: 1500").matches(get)
    assert rule("  s:\n    tx.dst|startswith: [SHOP, MULE]").matches(get)
    assert rule("  s:\n    tx.dst|contains: E-7\n    tx.channel: web").matches(get)
    assert not rule("  s:\n    tx.mcc: '5411'").matches(get)  # missing field never equals
    assert rule("  s:\n    tx.mcc|exists: false\n    tx.channel|exists: true").matches(get)
    assert rule("  s:\n    dna.count: 0\n    dna.known_counterparty: false").matches(get)
    assert not rule("  s:\n    dna.amount_mean|gte: 0").matches(get)  # no history: unknown
    assert rule("  s:\n    dna.seconds_since_last|exists: false").matches(get)


def test_condition_precedence_not_and_or():
    ev = tx("T1", T0, "5.00")
    get = tx_getter(ev, None)
    a, b = "  a:\n    tx.amount: 5\n", "  b:\n    tx.amount: 6\n"
    assert rule(a + b.rstrip(), "b or a and not b").matches(get)
    assert not rule(a + b.rstrip(), "not a or b").matches(get)
    assert rule(a + b.rstrip(), "not (b and a)").matches(get)


def test_auth_getter_fields():
    a = auth("E1", T0.replace(hour=2), "LOGIN_SUCCESS", device_id="D9")
    r = rule("  s:\n    auth.event_type: LOGIN_SUCCESS\n    auth.new_device: true\n    auth.hour|lt: 6",
             target="auth")
    assert r.matches(auth_getter(a, True)) and not r.matches(auth_getter(a, False))


# --- versioning, backtest, activation (Req 31.2-31.5) --------------------------------------------


def test_versions_increase_by_one_and_invalid_submission_changes_nothing():
    eng = RuleEngine(source=StaticBacktestSource(at=T0))
    assert eng.submit(BIG).version == 1
    eng.activate("R-BIG")
    v2 = eng.submit(BIG.replace("1000", "2000"))
    assert v2.version == 2 and v2.backtest is not None
    assert eng.active_version("R-BIG") == 1  # a new version is not active until activated
    eng.activate("R-BIG", 2)
    with pytest.raises(RuleValidationError) as ei:
        eng.submit(BIG.replace("tx.amount|gte", "tx.nope|gte"))
    assert ei.value.problems[0].line == 6
    assert [v.version for v in eng.versions("R-BIG")] == [1, 2]
    assert eng.active_version("R-BIG") == 2 and eng.active_rules()[0].selections[0].items[0].values == (2000,)
    v3 = eng.submit(BIG.replace("id: R-BIG", "id: R-BIG\nversion: 99"))
    assert v3.version == 3  # the document's own version is not trusted


def test_activation_requires_a_backtest_and_known_version():
    eng = RuleEngine()  # no backtest source configured
    eng.submit(BIG)
    with pytest.raises(RuleStateError, match="not been backtested"):
        eng.activate("R-BIG")
    with pytest.raises(RuleStateError, match="no backtest source"):
        eng.backtest("R-BIG")
    eng.backtest("R-BIG", 1, source=StaticBacktestSource(at=T0))
    assert eng.activate("R-BIG").activated_at is not None
    with pytest.raises(RuleNotFound):
        eng.activate("R-BIG", 7)
    with pytest.raises(RuleNotFound):
        eng.get("R-NONE")
    assert eng.deactivate("R-BIG") == 1 and eng.active_version("R-BIG") is None and not eng.active_rules()


def test_backtest_window_hits_and_estimated_false_positives():
    now = T0 + timedelta(days=10)
    txs = [
        tx("OLD", now - timedelta(days=8), "5000.00"),  # outside the default 7-day window
        tx("T1", now - timedelta(days=6), "5000.00"),
        tx("T2", now - timedelta(days=1), "1200.00"),
        tx("T3", now - timedelta(hours=1), "50.00"),
        tx("T4", now, "1000.00"),
    ]
    src = StaticBacktestSource(txs, confirmed={"T2", "OLD"}, at=now)
    eng = RuleEngine(source=src)
    rep = eng.submit(BIG).backtest
    assert rep is not None and rep.window_s == DEFAULT_BACKTEST_WINDOW_S
    assert rep.transactions_scanned == 4 and rep.auth_events_scanned == 0
    assert rep.hits == 3 and set(rep.sample_hits) == {"T1", "T2", "T4"}
    assert rep.estimated_false_positives == 2  # T1, T4: not in a CONFIRMED_FRAUD Case
    short = eng.backtest("R-BIG", 1, window_s=2 * 86_400)
    assert short.hits == 2 and short.estimated_false_positives == 1
    doc = short.to_dict()
    assert doc["hits"] == 2 and doc["window_end"] == now.isoformat()


def test_auth_rule_backtest_counts_auth_events_and_dna_rule_rebuilds_dna():
    now = T0 + timedelta(days=1)
    auths = [(auth(f"F{i}", now - timedelta(minutes=i)), None) for i in range(4)]
    auths.append((auth("OK", now, "LOGIN_SUCCESS"), False))
    src = StaticBacktestSource(auths=auths, confirmed={"F0"}, at=now)
    r = rule("  s:\n    auth.event_type: LOGIN_FAILURE", target="auth").with_version(1)
    rep = backtest_rule(r, src)
    assert (rep.auth_events_scanned, rep.hits, rep.estimated_false_positives) == (5, 4, 3)

    txs = [tx(f"T{i}", now - timedelta(hours=10 - i), "20.00", dst=f"D{i % 2}") for i in range(6)]
    known = rule("  s:\n    dna.known_counterparty: true").with_version(1)
    rep = backtest_rule(known, StaticBacktestSource(txs, at=now))
    assert rep.hits == 4 and any("rebuilt from the backtest window" in n for n in rep.notes)


def test_rules_directory_rescan_picks_up_edits_and_reports_errors(tmp_path):
    (tmp_path / "big.yaml").write_text(BIG, encoding="utf-8")
    (tmp_path / "bad.yml").write_text(BIG.replace("R-BIG", "R-BAD").replace("tx.amount", "tx.amnt"),
                                      encoding="utf-8")
    eng = RuleEngine(source=StaticBacktestSource(at=T0))
    res = {Path(r.path).name: r for r in eng.load_directory(tmp_path)}
    assert res["big.yaml"].status == "accepted" and res["big.yaml"].version == 1
    assert res["bad.yml"].status == "rejected" and res["bad.yml"].problems[0].line == 6
    assert [r.status for r in eng.load_directory(tmp_path)] == ["unchanged", "unchanged"]
    (tmp_path / "big.yaml").write_text(BIG.replace("1000", "900"), encoding="utf-8")
    res = {Path(r.path).name: r for r in eng.load_directory(tmp_path)}
    assert res["big.yaml"].status == "accepted" and res["big.yaml"].version == 2
    assert eng.rule_ids() == ["R-BIG"]


# --- pipeline emission (Req 31.5, 31.6, 30.4) -------------------------------------------------------


def const_clock() -> float:
    return 0.0


def test_attach_needs_cyber_fusion_and_default_pipeline_has_no_rules():
    h = Helix(HelixConfig(), ManualClock(T0), sensor_clock=const_clock)
    assert h.rules is None
    with pytest.raises(ValueError, match="soc.enabled"):
        attach_rules(h)


def test_activated_rule_applies_to_the_next_event_and_is_recorded_in_the_chain():
    h = Helix(soc_config(), ManualClock(T0), sensor_clock=const_clock)
    eng = attach_rules(h)
    assert h.rules is eng and eng.source is not None
    eng.submit(BIG)

    before = h.process(tx("T1", T0, "5000.00", src="NEW", dst="X"))
    assert all(c.kind is not ContributorType.RULE for c in before.stage1.contributions)

    t_act = time.monotonic()
    eng.activate("R-BIG")
    out = h.process(tx("T2", T0 + timedelta(seconds=1), "5000.00", src="NEW2", dst="X"))
    assert time.monotonic() - t_act < 5.0  # applies without a restart (Req 31.5)
    rc = [c for c in out.stage1.contributions if c.kind is ContributorType.RULE]
    assert [(c.contributor_id, c.mass.f, c.weight) for c in rc] == [("rule:R-BIG", 0.5, 1.0)]
    sensors_only = [c for c in out.stage1.contributions if c.kind is not ContributorType.RULE]
    assert out.stage1 == fuse([*sensors_only, *rc], h.cfg.fusion.high_conflict, tx_id="T2", stage=1)[0]
    assert out.risk > before.risk and out.case_id is not None

    chain = h.chain(out.case_id)
    [e] = [e for e in chain.contributors() if e.ctype == "RULE"]
    assert e.contributor_id == "rule:R-BIG" and e.features["rule_id"] == "R-BIG" and e.features["version"] == 1
    assert e.features["techniques"] == "HX-T0008" and e.mass.f == 0.5
    assert chain.verify() == [] and loads(dumps(chain)) == chain
    assert "HX-T0008" in case_techniques(chain)
    ex = explain(h.cases.get(out.case_id), chain, currency="EUR")
    assert any("rule R-BIG version 1" in it.comparison for it in ex.reasons), [it.comparison for it in ex.reasons]

    small = h.process(tx("T3", T0 + timedelta(seconds=2), "5.00", src="NEW3", dst="X"))
    assert not any(c.kind is ContributorType.RULE for c in small.stage1.contributions)
    eng.deactivate("R-BIG")
    off = h.process(tx("T4", T0 + timedelta(seconds=3), "5000.00", src="NEW4", dst="X"))
    assert not any(c.kind is ContributorType.RULE for c in off.stage1.contributions)
    assert eng.hits == 1 and eng.eval_errors == 0


def test_auth_rule_fires_on_the_takeover_payment():
    from tests.e2e.test_soc_ato import ATTACK_AT, scenario

    from helix.ingest import StreamIngestor

    cfg = soc_config()
    ing = StreamIngestor(cfg.ingest, ManualClock(T0))
    txs, auths = scenario()
    for d in auths:
        assert ing.submit_auth(d).accepted
    for d in txs:
        assert ing.submit(d).accepted
    h = Helix(cfg, ManualClock(T0), sensor_clock=const_clock)
    eng = attach_rules(h)
    eng.submit("id: R-STUFF\ntitle: failed logins\ntarget: auth\ntechniques: [HX-T0102]\n"
               "detection:\n  f:\n    auth.event_type: LOGIN_FAILURE\n  condition: f\nmass: {fraud: 0.2}\n")
    eng.activate("R-STUFF")
    outs = {o.event.tx_id: o for o in (h.process(a) for a in ing.drain()) if hasattr(o, "stage1")}
    assert not any(c.kind is ContributorType.RULE for o in outs.values() if o.event.tx_id != "T-ATO"
                   for c in o.stage1.contributions)
    ato = outs["T-ATO"]
    [c] = [c for c in ato.stage1.contributions if c.kind is ContributorType.RULE]
    assert c.contributor_id == "rule:R-STUFF"
    chain = h.chain(ato.case_id)
    [e] = [e for e in chain.contributors() if e.ctype == "RULE"]
    assert set(str(e.features["matched"]).split("|")) == {f"A-F{i}" for i in range(6)}
    assert "HX-T0102" in case_techniques(chain)
    assert ato.event.ts > ATTACK_AT


def test_rule_kind_survives_storage(tmp_path):
    from helix.fusion import Contribution, Mass
    from helix.storage import Storage

    fa, _ = fuse([Contribution("rule:R1", ContributorType.RULE, Mass(0.5, 0.0, 0.5), 1.0)], 0.95, tx_id="T1")
    st = Storage(tmp_path / "h.duckdb").open()
    try:
        st.record_event(tx("T1", T0), seq_no=1, received_at=T0)
        st.record_assessment(fa, risk=0.75)
        assert st.flush()
        [sa] = st.assessments("T1")
        got = getattr(sa, "assessment", sa)
        assert [c.kind for c in got.contributions] == [ContributorType.RULE]
    finally:
        st.close()


def test_format_rule_is_yaml_and_json_safe():
    r = parse_rule(BIG).with_version(4)
    text = format_rule(r)
    assert text.startswith("id: R-BIG\nversion: 4\n")
    assert parse_rule(text) == r
    json.dumps(r.summary())
    assert r.matches(tx_getter(tx("T", T0, "1000.00"), None))
    assert AuthEventType.LOGIN_FAILURE.value in parse_rule(
        BIG.replace("tx.amount|gte: 1000", "auth.event_type: LOGIN_FAILURE").replace(
            "techniques:", "target: auth\ntechniques:")).selections[0].items[0].values
