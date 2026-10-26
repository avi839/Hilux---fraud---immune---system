"""Label_Suggester (helix.cases.suggest, design §26, Req 41.4, 41.5): unit tests and Property 88."""

from __future__ import annotations

import math

import pytest
from hypothesis import given
from hypothesis import strategies as st

from helix.cases import CaseStatus, Label
from helix.cases.suggest import NO_ASSESSMENT, RANK, case_assessment, suggest, suggest_case, suggest_from
from helix.config import HelixConfig, TriageConfig
from helix.fusion import FusedAssessment, Mass

from .helpers import at, fixed_helix, tx

CFG = TriageConfig()


def fa_of(f: float, t: float) -> FusedAssessment:
    m = Mass(f, 1.0 - f - t, t)
    return FusedAssessment("T", 1, m, m.f, m.f + m.t, m.t, 0.0, False, ())


# --- unit -------------------------------------------------------------------------------


def test_threshold_edges_use_the_defaults_0_7_and_0_2():
    assert (CFG.suggest_fraud_belief, CFG.suggest_fp_belief) == (0.7, 0.2)
    s = suggest(fa_of(0.7, 0.1), CFG)
    assert (s.label, s.confidence) == ("CONFIRMED_FRAUD", 0.7)
    s = suggest(fa_of(0.2, 0.7), CFG)
    assert s.label == "FALSE_POSITIVE" and math.isclose(s.confidence, 0.8)
    s = suggest(fa_of(0.69, 0.2), CFG)
    assert (s.label, s.confidence) == ("NONE", 0.0)
    s = suggest(fa_of(0.21, 0.5), CFG)
    assert (s.label, s.confidence) == ("NONE", 0.0)


def test_reason_states_belief_plausibility_and_unknown():
    assert suggest(fa_of(0.82, 0.13), CFG).reason == (
        "Belief 0.82, plausibility 0.95, unknown 0.13: committed evidence supports fraud")
    assert suggest(fa_of(0.1, 0.71), CFG).reason == (
        "Belief 0.10, plausibility 0.81, unknown 0.71: little committed fraud evidence; "
        "most of the risk is unknown (u=0.71)")
    assert suggest(fa_of(0.5, 0.2), CFG).reason.endswith(": evidence is between the suggestion thresholds")


def test_case_summary_and_detail_carry_the_suggestion_without_changing_the_case():
    h = fixed_helix()
    cid = h.process(tx(1, "A", "B", 95, at(0))).case_id  # stage-1 Bel 0.9
    case = h.cases.get(cid)
    before = (case.status, case.label, len(case.history), case.updated_at)
    s = h.case_summary(case)["suggested_label"]
    assert s["label"] == "CONFIRMED_FRAUD" and math.isclose(s["confidence"], 0.9)
    assert "Belief 0.90" in s["reason"]
    assert (case.status, case.label, len(case.history), case.updated_at) == before
    assert case.status is CaseStatus.OPEN and case.label is None
    # Only an investigator submission stores a Feedback_Label (Req 41.5).
    h.open_case(cid, "ana")
    h.label(cid, "ana", Label.FALSE_POSITIVE)  # the human may disagree with the suggestion
    assert h.cases.get(cid).label is Label.FALSE_POSITIVE


def test_case_without_an_assessment_suggests_none():
    from types import SimpleNamespace

    s = suggest_case(SimpleNamespace(case_risk=None, best_fa=None), CFG)
    assert (s.label, s.confidence, s.reason) == ("NONE", 0.0, NO_ASSESSMENT)


def test_stage2_assessment_is_preferred_when_motifs_are_attached():
    from types import SimpleNamespace

    best, s2 = fa_of(0.1, 0.5), fa_of(0.8, 0.1)
    assert case_assessment(SimpleNamespace(case_risk=SimpleNamespace(stage2=s2, best_tx=best))) is s2
    assert case_assessment(SimpleNamespace(case_risk=SimpleNamespace(stage2=None, best_tx=best))) is best


def test_fp_threshold_must_be_below_the_fraud_threshold():
    errs = HelixConfig(triage=TriageConfig(suggest_fraud_belief=0.3, suggest_fp_belief=0.3)).validate()
    assert [e.path for e in errs] == ["triage.suggest_fp_belief"]


# --- Property 88 --------------------------------------------------------------------------


@st.composite
def masses(draw) -> Mass:
    """A valid Mass_Function over {FRAUD, LEGIT, Θ} (sums to 1)."""
    f = draw(st.floats(0, 1))
    t = draw(st.floats(0, 1 - f))
    return Mass(f, max(0.0, 1.0 - f - t), t)


@st.composite
def thresholds(draw) -> TriageConfig:
    fp = draw(st.floats(0, 1, exclude_max=True))
    fraud = draw(st.floats(fp, 1).filter(lambda x: x > fp))
    return TriageConfig(suggest_fp_belief=fp, suggest_fraud_belief=fraud)


# Feature: helix-fraud-immune-system, Property 88: Suggested label is bounded, monotone and deterministic
@pytest.mark.property
@given(m1=masses(), m2=masses(), cfg=thresholds())
def test_suggested_label_is_bounded_monotone_and_deterministic(m1, m2, cfg):
    """**Validates: Requirements 41.4**"""
    for m in (m1, m2):
        s = suggest_from(m.f, m.f + m.t, cfg)
        assert 0.0 <= s.confidence <= 1.0
        if s.label == "CONFIRMED_FRAUD":
            assert m.f >= cfg.suggest_fraud_belief and s.confidence == m.f
        elif s.label == "FALSE_POSITIVE":
            assert m.f <= cfg.suggest_fp_belief and s.confidence == 1.0 - m.f
        else:
            assert cfg.suggest_fp_belief < m.f < cfg.suggest_fraud_belief and s.confidence == 0.0
        assert suggest_from(m.f, m.f + m.t, cfg) == s  # deterministic
    lo, hi = sorted((m1, m2), key=lambda m: m.f)
    assert RANK[suggest_from(lo.f, lo.f + lo.t, cfg).label] <= RANK[suggest_from(hi.f, hi.f + hi.t, cfg).label]
