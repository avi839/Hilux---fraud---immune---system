"""HELIX_Technique_Catalogue, contributor mapping, Case technique tags and Coverage_Matrix (Req 30)."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional

import pytest
import yaml

from helix.attacker.campaigns import AUTH_VARIANTS
from helix.attacker.genome import PatternType
from helix.config import KillChainStage
from helix.explain import EntryKind, EvidenceEntry
from helix.fusion import Mass
from helix.immune.features import TECHNIQUES as IMMUNE_PROVISIONAL
from helix.immune.features import UNATTRIBUTED
from helix.motifs import MotifType
from helix.soc.coverage import (
    Detector,
    attacker_rates,
    builtin_detectors,
    coverage_matrix,
    pipeline_coverage,
    rates_from_outcomes,
)
from helix.soc.rules import RuleEngine, StaticBacktestSource
from helix.soc.techniques import (
    CATALOGUE_PATH,
    NOTICE,
    TECHNIQUE_ID,
    UNATTRIBUTED_TECHNIQUE,
    Catalogue,
    CatalogueError,
    ContributorMapping,
    Domain,
    UnknownTechniqueError,
    antibody_techniques,
    builtin_contributor_keys,
    case_techniques,
    default_catalogue,
    default_mapping,
    entry_techniques,
    genome_techniques,
    sensor_names,
)

TS = datetime(2024, 6, 1, tzinfo=timezone.utc)


def raw_catalogue() -> dict:
    return yaml.safe_load(CATALOGUE_PATH.read_text(encoding="utf-8"))


# --- catalogue (Req 30.1, 30.8) ---------------------------------------------------------


def test_catalogue_is_versioned_unique_and_covers_both_domains():
    cat = default_catalogue()
    assert cat.version == "1.0.0"
    ids = [t.technique_id for t in cat]
    assert all(TECHNIQUE_ID.match(i) for i in ids) and len(ids) == len(set(ids))
    assert {t.domain for t in cat} == {Domain.FRAUD, Domain.CYBER}
    assert all(t.name and t.description and isinstance(t.kill_chain_stage, KillChainStage) for t in cat)
    assert "not official MITRE data" in cat.notice and "ATT&CK" in cat.notice
    doc = cat.to_dict()
    assert doc["version"] == "1.0.0" and doc["notice"] == cat.notice and len(doc["techniques"]) == len(cat)


def test_immune_provisional_ids_are_catalogue_entries_with_the_same_meaning():
    cat = default_catalogue()
    for kind, tid in IMMUNE_PROVISIONAL.items():
        assert tid in cat, (kind, tid)
    assert cat.get("HX-T0001").name == "Round-trip cycle"
    assert cat.get("HX-T0006").name == "Account takeover cash-out"


@pytest.mark.parametrize("mutate,needle", [
    (lambda d: d["techniques"][0].update(id="HX-T12"), "HX-T followed by four digits"),
    (lambda d: d["techniques"][1].update(id=d["techniques"][0]["id"]), "duplicate id"),
    (lambda d: d.update(techniques=[t for t in d["techniques"] if t["domain"] != "CYBER"]), "no CYBER technique"),
    (lambda d: d["techniques"][0].update(domain="PHYSICAL"), "must be FRAUD or CYBER"),
    (lambda d: d["techniques"][0].update(kill_chain_stage="EXFIL"), "is not a Kill_Chain_Stage"),
    (lambda d: d["techniques"][0].update(name=""), "name: must be a non-empty string"),
    (lambda d: d.update(version=""), "version: must be a non-empty string"),
])
def test_catalogue_validation_rejects(mutate, needle):
    d = raw_catalogue()
    mutate(d)
    with pytest.raises(CatalogueError) as ei:
        Catalogue.from_dict(d)
    assert any(needle in p for p in ei.value.problems), ei.value.problems


# --- mapping (Req 30.2, 30.3) --------------------------------------------------------------


def test_every_builtin_sensor_and_motif_detector_maps_to_known_techniques():
    mp = default_mapping()
    keys = builtin_contributor_keys()
    assert {f"sensor.{n}" for n in sensor_names()} | {f"motif.{t.value}" for t in MotifType} == set(keys)
    mp.require(keys)  # Req 30.2: one or more ids each
    for k in keys:
        assert mp.techniques(k) and all(t in default_catalogue() for t in mp.techniques(k))


def test_mapping_with_unknown_id_is_rejected_and_reports_the_id():
    cat = default_catalogue()
    with pytest.raises(UnknownTechniqueError) as ei:
        ContributorMapping(cat, {"sensor.amount": ("HX-T0008", "HX-T9999"), "motif.FAN_IN": ("HX-T8888",)})
    assert ei.value.unknown == ("HX-T9999", "HX-T8888")
    assert "HX-T9999" in str(ei.value)
    mp = default_mapping()
    with pytest.raises(UnknownTechniqueError):
        mp.with_mapping("sensor.amount", ["HX-T7777"])
    assert mp.techniques("sensor.amount") == ("HX-T0008",)  # previous mapping kept
    with pytest.raises(CatalogueError, match="has no HELIX_Technique mapping"):
        ContributorMapping(cat, {"sensor.amount": ("HX-T0008",)}).require(["sensor.amount", "sensor.geo"])
    with pytest.raises(UnknownTechniqueError):
        cat.check(["HX-T0001", "HX-T0000"])


def test_antibody_tags_always_map_to_at_least_one_technique():
    assert antibody_techniques([UNATTRIBUTED]) == (UNATTRIBUTED_TECHNIQUE,)
    assert antibody_techniques([]) == (UNATTRIBUTED_TECHNIQUE,)
    assert antibody_techniques(["HX-T0002", "HX-T9999"]) == ("HX-T0002",)


def test_genome_tagging_covers_every_pattern_type_and_auth_variant():
    cat = default_catalogue()
    for pt in PatternType:
        tags = genome_techniques(pt)
        assert tags and all(t in cat for t in tags), pt
    assert genome_techniques("ROUND_TRIP") == ("HX-T0001",)
    for v in AUTH_VARIANTS:
        tags = genome_techniques(PatternType.ACCOUNT_TAKEOVER, v)
        assert "HX-T0006" in tags and len(tags) > 1 and all(t in cat for t in tags)
    assert "HX-T0103" in genome_techniques(PatternType.ACCOUNT_TAKEOVER, "sim_swap")
    with pytest.raises(ValueError):
        genome_techniques("PHISHING")
    with pytest.raises(ValueError):
        genome_techniques(PatternType.ACCOUNT_TAKEOVER, "vishing")


# --- entries and Case tags (Req 30.4) -------------------------------------------------------


def sensor(name: str, f: float, w: float = 1.0, **feats) -> EvidenceEntry:
    return EvidenceEntry(TS, "T1", name, EntryKind.CONTRIBUTOR, "SENSOR", Mass(f, 0.0, 1.0 - f), w, feats)


def test_entry_techniques_per_contributor_type():
    assert entry_techniques(sensor("amount", 0.4)) == ("HX-T0008",)
    assert entry_techniques(sensor("amount", 0.0)) == ()  # no FRAUD evidence: no tag
    assert entry_techniques(sensor("amount", 0.4, w=0.0)) == ()
    auth = sensor("auth", 0.5, signals="failed_logins|new_payee")
    assert entry_techniques(auth) == ("HX-T0006", "HX-T0101", "HX-T0102", "HX-T0106")
    motif = EvidenceEntry(TS, "T1", "M-1", EntryKind.MOTIF, "MOTIF", Mass(0.6, 0, 0.4), 1.0,
                          {"motif_type": "FAN_OUT"})
    assert entry_techniques(motif) == ("HX-T0002",)
    rule = EvidenceEntry(TS, "T1", "rule:R1", EntryKind.CONTRIBUTOR, "RULE", Mass(0.3, 0, 0.7), 1.0,
                         {"rule_id": "R1", "version": 2, "techniques": "HX-T0014|HX-T0012"})
    assert entry_techniques(rule) == ("HX-T0012", "HX-T0014")
    ab = EvidenceEntry(TS, "T1", "antibody:AB1", EntryKind.CONTRIBUTOR, "ANTIBODY", Mass(0.5, 0, 0.5), 1.0,
                       {"antibody_id": "AB1", "tags": "UNATTRIBUTED"})
    assert entry_techniques(ab) == ("HX-T0099",)
    rec = EvidenceEntry(TS, "T1", "~late", EntryKind.LATE, detail={"lateness_s": 1.0, "received_at": "x"})
    assert entry_techniques(rec) == ()
    assert case_techniques([sensor("amount", 0.4), motif, rule, ab, rec]) == (
        "HX-T0002", "HX-T0008", "HX-T0012", "HX-T0014", "HX-T0099")
    assert case_techniques(None) == ()


def test_ato_case_tags_are_the_union_over_its_chain():
    from tests.e2e.test_soc_ato import ato_case, run
    from helix.soc import soc_config

    _, h, _ = run(soc_config())
    case = ato_case(h)
    chain = h.chain(case.case_id)
    tags = case_techniques(chain)
    assert {"HX-T0006", "HX-T0101", "HX-T0105", "HX-T0106"} <= set(tags)
    assert set(tags) == {t for e in chain for t in entry_techniques(e)}


# --- coverage (Req 30.6, 30.7, 30.8) ----------------------------------------------------------


def test_builtin_coverage_flags_exactly_the_uncovered_techniques_as_gaps():
    cm = coverage_matrix(builtin_detectors())
    assert cm.catalogue_version == "1.0.0" and cm.notice == NOTICE
    assert [r.technique_id for r in cm.rows] == sorted(t.technique_id for t in default_catalogue())
    assert set(cm.gaps) == {"HX-T0014", "HX-T0099", "HX-T0107", "HX-T0108"}
    for r in cm.rows:
        assert r.gap is (not r.contributors)
        assert r.detection_rate is None and r.campaigns == 0
    assert [c.contributor_id for c in cm.row("HX-T0002").contributors] == ["motif:FAN_OUT"]
    assert {c.contributor_id for c in cm.row("HX-T0006").contributors} == {"auth"}
    doc = cm.to_dict()
    assert doc["summary"] == {"techniques": len(cm.rows), "covered": len(cm.rows) - 4, "gaps": 4}
    assert "not official MITRE data" in doc["notice"]


def test_without_the_auth_sensor_cyber_techniques_are_gaps():
    from helix.sensors import SENSOR_NAMES

    cm = coverage_matrix(builtin_detectors(SENSOR_NAMES))
    cyber = {t.technique_id for t in default_catalogue() if t.domain is Domain.CYBER}
    assert cyber <= set(cm.gaps)


def test_active_rule_and_antibody_close_gaps():
    eng = RuleEngine(source=StaticBacktestSource(at=TS))
    eng.submit("id: R-DORMANT\ntitle: dormant\ntechniques: [HX-T0014, HX-T0108]\n"
               "detection:\n  s:\n    dna.seconds_since_last|gte: 2592000\n  condition: s\nmass: {fraud: 0.4}\n")
    assert "HX-T0014" in coverage_matrix(builtin_detectors() + [
        Detector(r.contributor_id, "RULE", r.techniques, r.version) for r in eng.active_rules()]).gaps
    eng.activate("R-DORMANT")

    @dataclass
    class Ab:  # the duck-typed shape of helix.immune.Antibody the matrix reads
        contributor_id: str
        tags: tuple
        active: bool = True

    class Pool:
        def pool(self):
            return [Ab("antibody:AB1", (UNATTRIBUTED,)), Ab("antibody:AB2", ("HX-T0107",), active=False)]

    cm = pipeline_coverage(None, rules=eng, immune=Pool())
    assert set(cm.gaps) == {"HX-T0107"}
    row = cm.row("HX-T0014")
    assert [(c.contributor_id, c.kind, c.version) for c in row.contributors] == [("rule:R-DORMANT", "RULE", 1)]
    assert cm.row("HX-T0099").contributors[0].kind == "ANTIBODY"


def test_attacker_detection_rate_per_technique():
    assert rates_from_outcomes([(("HX-T0002",), 1.0), (("HX-T0002", "HX-T0006"), 0.5)]) == {
        "HX-T0002": (0.75, 2), "HX-T0006": (0.5, 1)}

    @dataclass
    class Camp:
        genome_id: str
        evasion: Optional[float]

    @dataclass
    class Gen:
        ptype: PatternType

    class Attacker:  # the duck-typed AttackerAgent surface the matrix reads
        def completed_campaigns(self):
            return [Camp("G1", 0.25), Camp("G1", 0.75), Camp("G2", 0.0), Camp("G3", None)]

        def genome(self, gid):
            return {"G1": Gen(PatternType.FAN_OUT), "G2": Gen(PatternType.ACCOUNT_TAKEOVER),
                    "G3": Gen(PatternType.FAN_IN)}[gid]

    rates = attacker_rates(Attacker())
    assert rates == {"HX-T0002": (0.5, 2), "HX-T0006": (1.0, 1)}
    cm = coverage_matrix(builtin_detectors(), rates)
    assert cm.row("HX-T0002").detection_rate == 0.5 and cm.row("HX-T0002").campaigns == 2
    assert cm.row("HX-T0003").detection_rate is None  # unscored campaign only
