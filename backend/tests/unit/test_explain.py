"""Unit tests for helix.explain: Evidence_Chain and Explanation_Engine (Requirements 15, 16, 18.9)."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from types import SimpleNamespace

import pytest

from helix.cases.models import Case, CaseHistoryEntry, CaseStatus, Placement
from helix.config import SeverityTier
from helix.explain import (
    NO_EVIDENCE,
    CaseHeader,
    EntryKind,
    EvidenceChain,
    EvidenceChainError,
    EvidenceEntry,
    audit_decision,
    dumps,
    evidence_digest,
    explain,
    loads,
    noop_audit_hook,
)
from helix.fusion import VACUOUS, Contribution, ContributorType, Mass, fuse
from helix.graph import Edge
from helix.motifs import MotifInstance, MotifType, contributor_id, motif_mass
from helix.risk import assess_case, risk, tier
from helix.config import DEFAULT_BANDS
from helix.sensors import SensorOutput

T0 = datetime(2024, 6, 5, 11, 20, tzinfo=timezone.utc)
HC = 0.95


def result(tx: str, name: str, mass: Mass, feats: dict, reason: str | None = None,
           outcome: str = "SUCCESS", error_text: str | None = None) -> SimpleNamespace:
    """SensorResult-shaped object (the builder only needs these attributes)."""
    return SimpleNamespace(tx_id=tx, name=name, outcome=outcome, output=SensorOutput(mass, feats, reason),
                           elapsed_ms=0.5, error_text=error_text)


def make_case(results: dict[str, list], amounts: dict[str, Decimal], motifs: dict | None = None,
              weights: dict[str, float] | None = None) -> Case:
    w = weights or {}
    assessments = {
        tx: fuse([Contribution(r.name, ContributorType.SENSOR, r.output.mass, w.get(r.name, 1.0)) for r in rs],
                 HC, tx_id=tx)[0]
        for tx, rs in results.items()
    }
    motifs = motifs or {}
    mc = [Contribution(contributor_id(m.motif_type), ContributorType.MOTIF, m.mass, 1.0)
          for _, m in sorted(motifs.items())]
    cr = assess_case(list(assessments.values()), mc, HC)
    return Case(
        case_id="C-7", seq_no=7, status=CaseStatus.OPEN, tier=tier(cr.score, DEFAULT_BANDS), risk=cr.score,
        risk_at_creation=cr.score, expected_loss=Decimal("0"), priority=Decimal("0"), accounts={"ACC-ALICE"},
        tx_ids=set(results), assignee=None, created_at=T0, updated_at=T0, admitted_period=None,
        placement=Placement.ACTIVE, merged_into=None, label=None, label_note=None, escalated=False, history=[],
        assessments=assessments, amounts=amounts, motifs=motifs, case_risk=cr,
    )


def t007_results() -> list:
    tx = "T007"
    return [
        result(tx, "amount", Mass(0.6, 0.0, 0.4), {"amount": 4800.0, "mean": 37.08, "std": 14.2, "z": 335.4}),
        result(tx, "counterparty", Mass(0.35, 0.0, 0.65),
               {"dst": "ACC-UNKNOWN-77", "known": 0.0, "n_counterparties": 3.0}),
        result(tx, "drift", VACUOUS, {"history_count": 3.0}, "history:3"),
        result(tx, "geo", Mass(0.62, 0.0, 0.38),
               {"distance_km": 5003.6, "dt_h": 1.1667, "speed_kmh": 4288.8, "vmax_kmh": 900.0}),
        result(tx, "timing", Mass(0.0, 0.12, 0.88),
               {"hour": 11.0, "weekday": 2.0, "freq_hour": 0.2, "freq_weekday": 0.333, "p": 0.0667}),
        result(tx, "velocity", Mass(0.0, 0.2, 0.8),
               {"count_1h": 1.0, "expected_1h": 1.0, "count_24h": 2.0, "expected_24h": 3.1,
                "count_7d": 7.0, "expected_7d": 21.7, "mean_iat_s": 27900.0, "ratio": 1.0}),
    ]


def t007_chain(**kw) -> tuple[Case, EvidenceChain]:
    case = make_case({"T007": t007_results()}, {"T007": Decimal("4800.00")}, **kw)
    return case, EvidenceChain.build(case, {"T007": t007_results()}, {"T007": T0})


# --- Explanation_Engine ------------------------------------------------------------------


class TestT007Explanation:
    def test_renders_readable_t007_explanation(self) -> None:
        case, chain = t007_chain(weights={"geo": 0.9})
        ex = explain(case, chain)
        print("\n" + ex.text)
        assert [i.name for i in ex.reasons] == ["amount", "geo", "counterparty"]  # geo: 0.9*0.62 = 0.558 < 0.6
        assert [i.name for i in ex.mitigating] == ["velocity", "timing"]
        assert [i.name for i in ex.insufficient] == ["drift"]
        assert ex.message is None
        lines = ex.text.splitlines()
        assert lines[0].startswith(f"{case.tier.value} | risk {case.risk:.3f} | Bel ")
        assert lines[0].endswith("| at risk 4800.00 EUR")
        assert "Reasons (3):" in lines and "Mitigating (2):" in lines and "Insufficient evidence (1):" in lines
        assert "  1. Amount: amount 4800.00 vs baseline mean 37.08 (sd 14.20, z 335.40) | weight 1.00, " \
               "F' 0.600 vs L' 0.000, tx T007" in lines
        assert "  2. Geo: 5003.6 km in 1.17 h = 4289 km/h vs plausible 900 km/h | weight 0.90, " \
               "F' 0.558 vs L' 0.000, tx T007" in lines
        assert "  3. Counterparty: payee ACC-UNKNOWN-77 not seen before (baseline 3 known counterparties)" \
               " | weight 1.00, F' 0.350 vs L' 0.000, tx T007" in lines
        assert any(ln.startswith("  - Velocity: events 1h 1 vs expected 1.00") for ln in lines)
        assert any(ln.startswith("  - Timing: 11:00 UTC on Wed vs baseline frequency") for ln in lines)
        assert "  - Drift: insufficient history (3 prior events) | weight 1.00, F' 0.000 vs L' 0.000, tx T007" \
               in lines

    def test_summary_fields_to_3dp(self) -> None:
        h = CaseHeader("C-1", "HIGH", 0.81234, 0.7, 0.92349, 0.22351, 0.1, (("USD", Decimal("5")),
                                                                                ("EUR", Decimal("1.005"))))
        ex = explain(h, None)
        assert ex.summary == ("HIGH | risk 0.812 | Bel 0.700 Pl 0.923 U 0.224 K 0.100 | "
                              "at risk 1.01 EUR, 5.00 USD")

    def test_empty_or_absent_chain_shows_no_evidence(self) -> None:
        case, _ = t007_chain()
        for chain in (None, EvidenceChain("C-7")):
            ex = explain(case, chain)
            assert ex.message == NO_EVIDENCE
            assert ex.reasons == ex.mitigating == ex.insufficient == ()
            assert ex.text.splitlines()[1] == NO_EVIDENCE
            assert ex.summary.startswith(f"{case.tier.value} | risk ")

    def test_generic_template_for_untemplated_type(self) -> None:
        e = EvidenceEntry(T0, "T1", "canary:honeytoken", EntryKind.CONTRIBUTOR, "CANARY",
                          Mass(0.5, 0.1, 0.4), 0.8)
        ex = explain(CaseHeader("C-1", "LOW", 0.5, 0, 1, 1, 0), EvidenceChain.of("C-1", [e]))
        (it,) = ex.reasons
        assert it.generic and it.comparison == "m(F) 0.500 m(L) 0.100 m(F,L) 0.400"
        assert it.text.endswith("weight 0.80, F' 0.400 vs L' 0.080, tx T1")

    def test_template_missing_features_falls_back_to_generic(self) -> None:
        e = EvidenceEntry(T0, "T1", "amount", EntryKind.CONTRIBUTOR, "SENSOR", Mass(0.3, 0.0, 0.7), 1.0)
        (it,) = explain(CaseHeader("C-1", "LOW", 0.5, 0, 1, 1, 0), EvidenceChain.of("C-1", [e])).reasons
        assert it.generic and it.label == "Amount"

    def test_motif_template_and_missing_field_record(self) -> None:
        edges = (Edge("T13", "ACC-CARL", "ACC-DAVE", T0, Decimal("5000"), "EUR"),
                 Edge("T14", "ACC-DAVE", "ACC-ERIN", T0 + timedelta(minutes=25), Decimal("4600"), "EUR"))
        m = MotifInstance("MULE_CHAIN:abc", MotifType.MULE_CHAIN, frozenset({"T13", "T14"}),
                          ("ACC-CARL", "ACC-DAVE", "ACC-ERIN"), edges[0].ts, edges[1].ts, motif_mass(0.5),
                          True, edges)
        res = {"T13": [result("T13", "geo", VACUOUS, {}, "missing:lat,lon")],
               "T14": [result("T14", "amount", Mass(0.2, 0.0, 0.8), {}),
                       result("T14", "geo", VACUOUS, {}, "error", outcome="ERROR", error_text="boom")]}
        case = make_case(res, {"T13": Decimal("5000"), "T14": Decimal("4600")}, {m.motif_id: m})
        chain = EvidenceChain.build(case, res, {"T13": edges[0].ts, "T14": edges[1].ts})
        kinds = [e.kind for e in chain]
        assert kinds.count(EntryKind.MISSING_FIELD) == 2  # lat and lon, one record each
        assert kinds.count(EntryKind.SENSOR_FAILURE) == 1
        assert kinds.count(EntryKind.TRUNCATION) == 1
        assert sum(1 for e in chain if e.contributor_id.startswith("~stage2:")) == 1
        assert chain.verify() == []
        ex = explain(case, chain)
        mule = next(i for i in ex.reasons if i.name == "MULE_CHAIN:abc")
        assert mule.label == "Mule chain"
        assert mule.comparison == "MULE_CHAIN over accounts ACC-CARL -> ACC-DAVE -> ACC-ERIN (2 tx)"
        geo = next(i for i in ex.insufficient if i.name == "geo")
        assert geo.comparison == "missing field lat, lon; sensor failure (error)"
        assert "T13: missing field lat (sensors geo)" in ex.notes
        assert "T14: sensor geo ERROR: boom" in ex.notes
        assert "T14: motif search truncated (MULE_CHAIN)" in ex.notes


# --- Evidence_Chain ------------------------------------------------------------------------


class TestEvidenceChain:
    def test_build_records_contributors_and_fused(self) -> None:
        case, chain = t007_chain()
        contribs = chain.contributors()
        assert sorted(e.contributor_id for e in contribs) == sorted(r.name for r in t007_results())
        amt = chain.entries[("amount", "T007")]
        assert amt.ctype == "SENSOR" and amt.mass == Mass(0.6, 0.0, 0.4) and amt.weight == 1.0
        assert amt.features["amount"] == 4800.0
        fused = chain.entries[("~fused", "T007")]
        fa = case.assessments["T007"]
        assert fused.mass == fa.mass and fused.detail["conflict"] == fa.conflict
        assert fused.detail["amount"] == Decimal("4800.00") and fused.detail["currency"] == "EUR"
        assert chain.verify() == []
        assert risk(fa) == case.risk

    def test_ordering_by_ts_tx_contributor(self) -> None:
        a = EvidenceEntry(T0, "T2", "b", EntryKind.LATE)
        b = EvidenceEntry(T0, "T2", "a", EntryKind.LATE)
        c = EvidenceEntry(T0 - timedelta(seconds=1), "T9", "z", EntryKind.LATE)
        d = EvidenceEntry(T0, "T1", "z", EntryKind.LATE)
        chain = EvidenceChain.of("C-1", [a, b, c, d])
        assert [(e.tx_id, e.contributor_id) for e in chain] == [("T9", "z"), ("T1", "z"), ("T2", "a"), ("T2", "b")]

    def test_add_is_append_only(self) -> None:
        e = EvidenceEntry(T0, "T1", "amount", EntryKind.CONTRIBUTOR, "SENSOR", Mass(0.2, 0.1, 0.7), 1.0)
        chain = EvidenceChain.of("C-1", [e])
        assert chain.add(e) == chain  # identical re-add is a no-op
        changed = EvidenceEntry(T0, "T1", "amount", EntryKind.CONTRIBUTOR, "SENSOR", Mass(0.3, 0.1, 0.6), 1.0)
        with pytest.raises(EvidenceChainError):
            chain.add(changed)
        new = EvidenceEntry(T0, "T2", "amount", EntryKind.CONTRIBUTOR, "SENSOR", VACUOUS, 1.0)
        longer = chain.add(new)
        assert longer.entries[e.key] is e and len(longer) == 2 and len(chain) == 1

    def test_merge_is_deduplicated_union_keeping_records(self) -> None:
        shared = EvidenceEntry(T0, "T1", "amount", EntryKind.CONTRIBUTOR, "SENSOR", Mass(0.2, 0.1, 0.7), 1.0)
        rec1 = EvidenceEntry(T0, "T1", "~missing:lat", EntryKind.MISSING_FIELD, detail={"field": "lat"})
        rec2 = EvidenceEntry(T0, "T5", "~cp", EntryKind.CHANGE_POINT, detail={"classification": "ABRUPT_BREAK"})
        conflicting = EvidenceEntry(T0, "T1", "amount", EntryKind.CONTRIBUTOR, "SENSOR", VACUOUS, 0.5)
        a = EvidenceChain.of("C-1", [shared, rec1])
        b = EvidenceChain.of("C-2", [conflicting, rec2])
        m = a.merge(b)
        assert m.case_id == "C-1" and len(m) == 3
        assert m.entries[shared.key] == shared  # survivor wins on a conflicting pair
        assert rec1 in list(m) and rec2 in list(m)

    def test_entry_validation(self) -> None:
        with pytest.raises(EvidenceChainError, match="entry.ts"):
            EvidenceEntry(datetime(2024, 1, 1), "T1", "x", EntryKind.LATE)
        with pytest.raises(EvidenceChainError, match="entry.mass"):
            EvidenceEntry(T0, "T1", "x", EntryKind.CONTRIBUTOR, "SENSOR", Mass(0.9, 0.9, 0.0), 1.0)
        with pytest.raises(EvidenceChainError, match="entry.weight"):
            EvidenceEntry(T0, "T1", "x", EntryKind.CONTRIBUTOR, "SENSOR", VACUOUS, 1.5)
        with pytest.raises(EvidenceChainError, match="entry.features.x"):
            EvidenceEntry(T0, "T1", "x", EntryKind.LATE, features={"x": float("nan")})

    def test_verify_detects_tampered_fused_value(self) -> None:
        _, chain = t007_chain()
        doc = json.loads(dumps(chain))
        for e in doc["entries"]:
            if e["contributor_id"] == "~fused":
                e["detail"]["belief"] += 1e-6
        assert any("belief" in p for p in loads(json.dumps(doc)).verify())


class TestJson:
    def test_round_trip_exact(self) -> None:
        _, chain = t007_chain()
        back = loads(dumps(chain))
        assert back == chain and list(back) == list(chain) and dumps(back) == dumps(chain)

    def test_decimal_and_float_encoding(self) -> None:
        e = EvidenceEntry(T0, "T1", "x", EntryKind.LATE, features={"d": Decimal("4800.00"), "f": 0.1, "i": 3})
        s = dumps(EvidenceChain.of("C-1", [e]))
        assert '"d":{"$decimal":"4800.00"}' in s and '"f":0.1' in s and '"i":3' in s
        back = next(iter(loads(s)))
        assert back.features["d"] == Decimal("4800.00") and str(back.features["d"]) == "4800.00"
        assert type(back.features["i"]) is int

    @pytest.mark.parametrize("text, field", [
        ("{not json", "$"),
        ('{"schema":1,"case_id":"C-1"}', "entries"),
        ('{"schema":2,"case_id":"C-1","entries":[]}', "schema"),
        ('{"schema":1,"case_id":"C-1","entries":[],"x":1}', "x"),
    ])
    def test_rejects_with_field(self, text: str, field: str) -> None:
        with pytest.raises(EvidenceChainError) as ei:
            loads(text)
        assert ei.value.field == field

    def test_rejects_nan_and_invalid_mass_naming_field(self) -> None:
        _, chain = t007_chain()
        s = dumps(chain)
        doc = json.loads(s)
        doc["entries"][0]["weight"] = float("nan")
        with pytest.raises(EvidenceChainError) as ei:
            loads(json.dumps(doc))
        assert ei.value.field == "entries[0].weight"
        doc = json.loads(s)
        doc["entries"][1]["mass"]["f"] = 0.99
        with pytest.raises(EvidenceChainError) as ei:
            loads(json.dumps(doc))
        assert ei.value.field == "entries[1].mass"
        doc = json.loads(s)
        del doc["entries"][2]["tx_id"]
        with pytest.raises(EvidenceChainError) as ei:
            loads(json.dumps(doc))
        assert ei.value.field == "entries[2].tx_id"


# --- Audit_Ledger hook point (Req 18.9) -------------------------------------------------


class TestAuditHook:
    def test_escalation_and_label_reach_hook(self) -> None:
        _, chain = t007_chain()
        seen = []
        esc = CaseHistoryEntry("C-7", T0, "escalate", "HIGH", "CRITICAL", "inv-1")
        rec = audit_decision(esc, chain, hook=seen.append)
        assert seen == [rec] and rec.evidence_digest == evidence_digest(chain) and rec.actor == "inv-1"
        lab = CaseHistoryEntry("C-7", T0, "label", None, "CONFIRMED_FRAUD", "inv-1")
        assert audit_decision(lab, note="ok", hook=seen.append) is seen[-1] and len(seen) == 2

    def test_non_audited_actions_and_noop(self) -> None:
        assert audit_decision(CaseHistoryEntry("C-7", T0, "open", "OPEN", "IN_REVIEW", "inv-1")) is None
        assert noop_audit_hook(None) is None  # type: ignore[arg-type]
