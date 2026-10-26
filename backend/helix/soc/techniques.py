"""HELIX_Technique_Catalogue and contributor-to-technique mapping (Req 30.1-30.5).

Design decisions:
- The catalogue lives in ``helix/soc/data/techniques.yaml`` (versioned, ``yaml.safe_load``
  only). ``Catalogue`` validates it on construction: ids match ``HX-T`` + four digits and are
  unique, each technique has a name, a FRAUD or CYBER domain, a description and a
  Kill_Chain_Stage, and both domains are present (Req 30.1). It is ATT&CK-inspired, not official
  MITRE data; ``NOTICE`` travels with every API payload (Req 30.8).
- Contributors are mapped through *contributor keys*, the kill-chain stage-key scheme of
  ``helix.cases.killchain.stage_keys`` (``sensor.<name>``, ``auth.<signal>``, ``motif.<TYPE>``),
  configured in ``helix/soc/data/contributors.yaml``. Detection_Rules carry their technique
  tags in the rule (validated at acceptance) and Antibodies their tags; the Immune_System's
  ``UNATTRIBUTED`` tag maps to HX-T0099 so every Antibody has at least one technique (Req 30.2).
- Any mapping that names an id outside the catalogue is rejected with ``UnknownTechniqueError``
  listing every unknown id (Req 30.3).
- Case technique tags (Req 30.4) are a pure function of the Evidence_Chain:
  ``case_techniques(chain)`` is the union of ``entry_techniques`` over the chain's contributor
  entries, so the invariant holds by construction and is re-derivable from a stored chain.
  Interpretation: a SENSOR entry contributes techniques only when it carried FRAUD evidence
  (``w·m(F) > 0``); a sensor that reported "looks normal" does not tag the Case (otherwise every
  Case would carry every sensor's techniques). RULE entries use the techniques recorded at match
  time (the version that fired), ANTIBODY entries their recorded tags.
- ``genome_techniques`` is the Pattern_Genome tagging function for the Attacker_Agent (Req 30.5);
  wiring it into the attacker and the Ground_Truth_Store is an integration step outside this
  package.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from enum import Enum
from functools import lru_cache
from pathlib import Path
from typing import Any, Optional, Union

import yaml

from helix.cases.killchain import stage_keys
from helix.config.schema import KillChainStage

DATA_DIR = Path(__file__).resolve().parent / "data"
CATALOGUE_PATH = DATA_DIR / "techniques.yaml"
MAPPING_PATH = DATA_DIR / "contributors.yaml"

TECHNIQUE_ID = re.compile(r"^HX-T\d{4}$")
NOTICE = "ATT&CK-inspired technique catalogue; not official MITRE data."
#: Antibody tag for an unknown origin (``helix.immune.features.UNATTRIBUTED``) and its technique.
ANTIBODY_UNATTRIBUTED = "UNATTRIBUTED"
UNATTRIBUTED_TECHNIQUE = "HX-T0099"
RULE_TECHNIQUE_SEP = "|"
ANTIBODY_TAG_SEP = ","


class Domain(str, Enum):
    FRAUD = "FRAUD"
    CYBER = "CYBER"

    def __str__(self) -> str:
        return self.value


class CatalogueError(ValueError):
    """Invalid catalogue or mapping; ``problems`` lists every violation found."""

    def __init__(self, problems: Iterable[str]) -> None:
        self.problems = tuple(problems)
        super().__init__("; ".join(self.problems) or "invalid")


class UnknownTechniqueError(CatalogueError):
    """A mapping references ids absent from the catalogue (Req 30.3)."""

    def __init__(self, unknown: Iterable[str], where: str = "mapping") -> None:
        self.unknown = tuple(dict.fromkeys(unknown))
        self.where = where
        super().__init__([f"{where}: unknown HELIX_Technique id {u!r}" for u in self.unknown])


def is_technique_id(x: object) -> bool:
    return isinstance(x, str) and TECHNIQUE_ID.match(x) is not None


@dataclass(frozen=True, slots=True)
class Technique:
    technique_id: str
    name: str
    domain: Domain
    kill_chain_stage: KillChainStage
    description: str

    def to_dict(self) -> dict[str, Any]:
        return {"technique_id": self.technique_id, "name": self.name, "domain": self.domain.value,
                "kill_chain_stage": self.kill_chain_stage.value, "description": self.description}


def _str(d: Mapping[str, Any], k: str, where: str, problems: list[str]) -> str:
    v = d.get(k)
    if not isinstance(v, str) or not v.strip():
        problems.append(f"{where}.{k}: must be a non-empty string")
        return ""
    return v


@dataclass(frozen=True)
class Catalogue:
    version: str
    techniques: tuple[Technique, ...]
    notice: str = NOTICE
    _index: Mapping[str, Technique] = field(default_factory=dict, repr=False, compare=False)

    def __post_init__(self) -> None:
        problems: list[str] = []
        if not (isinstance(self.version, str) and self.version.strip()):
            problems.append("version: must be a non-empty string")
        index: dict[str, Technique] = {}
        for t in self.techniques:
            if not is_technique_id(t.technique_id):
                problems.append(f"technique {t.technique_id!r}: id must match HX-T followed by four digits")
            elif t.technique_id in index:
                problems.append(f"technique {t.technique_id!r}: duplicate id")
            index[t.technique_id] = t
        domains = {t.domain for t in self.techniques}
        for d in Domain:
            if d not in domains:
                problems.append(f"techniques: no {d.value} technique (both FRAUD and CYBER are required)")
        if problems:
            raise CatalogueError(problems)
        object.__setattr__(self, "techniques", tuple(sorted(self.techniques, key=lambda t: t.technique_id)))
        object.__setattr__(self, "_index", index)

    # -- construction ------------------------------------------------------------

    @classmethod
    def from_dict(cls, d: Any) -> Catalogue:
        if not isinstance(d, Mapping):
            raise CatalogueError(["catalogue: must be a mapping"])
        problems: list[str] = []
        version = d.get("version")
        if isinstance(version, (int, float)) and not isinstance(version, bool):
            version = str(version)
        raw = d.get("techniques")
        if not isinstance(raw, list) or not raw:
            raise CatalogueError([*problems, "techniques: must be a non-empty list"])
        out: list[Technique] = []
        for n, t in enumerate(raw):
            w = f"techniques[{n}]"
            if not isinstance(t, Mapping):
                problems.append(f"{w}: must be a mapping")
                continue
            tid = t.get("id")
            if not is_technique_id(tid):
                problems.append(f"{w}.id: {tid!r} must match HX-T followed by four digits")
            name = _str(t, "name", w, problems)
            desc = _str(t, "description", w, problems)
            try:
                dom = Domain(t.get("domain"))
            except ValueError:
                problems.append(f"{w}.domain: {t.get('domain')!r} must be FRAUD or CYBER")
                dom = Domain.FRAUD
            try:
                stage = KillChainStage(t.get("kill_chain_stage"))
            except ValueError:
                problems.append(f"{w}.kill_chain_stage: {t.get('kill_chain_stage')!r} is not a Kill_Chain_Stage")
                stage = KillChainStage.ACCOUNT_TAKEOVER
            out.append(Technique(str(tid), name, dom, stage, desc))
        if problems:
            raise CatalogueError(problems)
        notice = d.get("notice") if isinstance(d.get("notice"), str) and d.get("notice") else NOTICE
        return cls(version if isinstance(version, str) else "", tuple(out), notice)

    @classmethod
    def load(cls, path: Union[str, Path, None] = None) -> Catalogue:
        text = Path(path or CATALOGUE_PATH).read_text(encoding="utf-8")
        return cls.from_dict(yaml.safe_load(text))

    def to_dict(self) -> dict[str, Any]:
        return {"version": self.version, "notice": self.notice,
                "techniques": [t.to_dict() for t in self.techniques]}

    # -- lookups -----------------------------------------------------------------

    @property
    def ids(self) -> frozenset[str]:
        return frozenset(self._index)

    def get(self, technique_id: str) -> Optional[Technique]:
        return self._index.get(technique_id)

    def __contains__(self, technique_id: object) -> bool:
        return technique_id in self._index

    def __len__(self) -> int:
        return len(self.techniques)

    def __iter__(self):  # type: ignore[no-untyped-def]
        return iter(self.techniques)

    def unknown(self, ids: Iterable[str]) -> tuple[str, ...]:
        return tuple(dict.fromkeys(i for i in ids if i not in self._index))

    def check(self, ids: Iterable[str], where: str = "mapping") -> tuple[str, ...]:
        """``ids`` (order kept, de-duplicated) if every id is in the catalogue, else
        ``UnknownTechniqueError`` naming each unknown id (Req 30.3)."""
        ids = tuple(dict.fromkeys(ids))
        bad = self.unknown(ids)
        if bad:
            raise UnknownTechniqueError(bad, where)
        return ids


@dataclass(frozen=True)
class ContributorMapping:
    """``contributor key -> technique ids``; every id is in ``catalogue`` (Req 30.2, 30.3)."""

    catalogue: Catalogue
    mappings: Mapping[str, tuple[str, ...]]

    def __post_init__(self) -> None:
        problems: list[str] = []
        unknown: list[str] = []
        clean: dict[str, tuple[str, ...]] = {}
        for k, ids in self.mappings.items():
            if not isinstance(k, str) or not k:
                problems.append(f"mapping key {k!r}: must be a non-empty string")
                continue
            if isinstance(ids, str) or not isinstance(ids, Iterable):
                problems.append(f"mapping {k!r}: must be a list of technique ids")
                continue
            ids = tuple(dict.fromkeys(ids))
            if not ids:
                problems.append(f"mapping {k!r}: needs at least one technique id")
            unknown += [i for i in ids if i not in self.catalogue]
            clean[k] = ids
        if unknown:
            raise UnknownTechniqueError(unknown, "contributor mapping")
        if problems:
            raise CatalogueError(problems)
        object.__setattr__(self, "mappings", dict(clean))

    @classmethod
    def from_dict(cls, d: Any, catalogue: Catalogue) -> ContributorMapping:
        if not isinstance(d, Mapping) or not isinstance(d.get("mappings"), Mapping):
            raise CatalogueError(["contributor mapping: needs a 'mappings' mapping"])
        return cls(catalogue, {k: tuple(v) if isinstance(v, list) else v for k, v in d["mappings"].items()})

    @classmethod
    def load(cls, catalogue: Catalogue, path: Union[str, Path, None] = None) -> ContributorMapping:
        return cls.from_dict(yaml.safe_load(Path(path or MAPPING_PATH).read_text(encoding="utf-8")), catalogue)

    def techniques(self, key: str) -> tuple[str, ...]:
        return self.mappings.get(key, ())

    def techniques_of_keys(self, keys: Iterable[str]) -> tuple[str, ...]:
        return tuple(sorted({t for k in keys for t in self.mappings.get(k, ())}))

    def missing(self, keys: Iterable[str]) -> tuple[str, ...]:
        return tuple(k for k in keys if not self.mappings.get(k))

    def require(self, keys: Iterable[str]) -> None:
        """Every key maps to at least one technique (Req 30.2), else ``CatalogueError``."""
        miss = self.missing(keys)
        if miss:
            raise CatalogueError([f"contributor {k!r} has no HELIX_Technique mapping" for k in miss])

    def with_mapping(self, key: str, ids: Iterable[str]) -> ContributorMapping:
        """New mapping with ``key`` (re)mapped; unknown ids are rejected and ``self`` is kept."""
        return ContributorMapping(self.catalogue, {**self.mappings, key: tuple(ids)})


@lru_cache(maxsize=1)
def default_catalogue() -> Catalogue:
    return Catalogue.load()


@lru_cache(maxsize=1)
def default_mapping() -> ContributorMapping:
    return ContributorMapping.load(default_catalogue())


# --- built-in contributors ------------------------------------------------------------


def sensor_names() -> tuple[str, ...]:
    from helix.sensors import SENSOR_NAMES, AuthSensor

    return (*SENSOR_NAMES, AuthSensor.name)


def motif_types() -> tuple[str, ...]:
    from helix.motifs import MotifType

    return tuple(t.value for t in MotifType)


def builtin_contributor_keys() -> tuple[str, ...]:
    """Mapping keys every built-in Sensor_Agent and Motif_Detector must have (Req 30.2)."""
    return (*(f"sensor.{n}" for n in sensor_names()), *(f"motif.{t}" for t in motif_types()))


def sensor_techniques(name: str, mapping: Optional[ContributorMapping] = None) -> tuple[str, ...]:
    """Techniques a Sensor_Agent can detect (``sensor.<name>``, plus ``auth.*`` for the auth sensor)."""
    mp = mapping or default_mapping()
    keys = [f"sensor.{name}"]
    if name == "auth":
        keys += [k for k in mp.mappings if k.startswith("auth.")]
    return mp.techniques_of_keys(keys)


def motif_techniques(motif_type: Any, mapping: Optional[ContributorMapping] = None) -> tuple[str, ...]:
    mp = mapping or default_mapping()
    return mp.techniques_of_keys([f"motif.{getattr(motif_type, 'value', motif_type)}"])


def antibody_techniques(tags: Iterable[str], catalogue: Optional[Catalogue] = None) -> tuple[str, ...]:
    """Technique ids of an Antibody's tags: catalogue ids are kept, ``UNATTRIBUTED`` (and an
    Antibody with no known tag) maps to HX-T0099, so the result is never empty (Req 30.2)."""
    cat = catalogue or default_catalogue()
    out = {t for t in tags if t in cat}
    if not out:
        out = {UNATTRIBUTED_TECHNIQUE} if UNATTRIBUTED_TECHNIQUE in cat else set()
    return tuple(sorted(out))


# --- attacker genome tagging (Req 30.5) --------------------------------------------------

GENOME_TECHNIQUES: Mapping[str, tuple[str, ...]] = {
    "ROUND_TRIP_CYCLE": ("HX-T0001",),
    "FAN_OUT": ("HX-T0002",),
    "FAN_IN": ("HX-T0003",),
    "STRUCTURING": ("HX-T0004",),
    "MULE_CHAIN": ("HX-T0005",),
    "ACCOUNT_TAKEOVER": ("HX-T0006",),
    "CARD_TESTING": ("HX-T0007",),
}
#: Extra techniques of an ATO campaign's auth leg (``helix.attacker.campaigns.AUTH_VARIANTS``).
AUTH_VARIANT_TECHNIQUES: Mapping[str, tuple[str, ...]] = {
    "account_takeover": ("HX-T0101", "HX-T0105", "HX-T0106"),
    "credential_stuffing": ("HX-T0102", "HX-T0105", "HX-T0106"),
    "sim_swap": ("HX-T0103", "HX-T0106"),
    "device_takeover": ("HX-T0104", "HX-T0105", "HX-T0106"),
}


def genome_techniques(ptype: Any, auth_variant: Optional[str] = None) -> tuple[str, ...]:
    """HELIX_Technique tags of a Pattern_Genome type (and, for ATO, of its auth-leg variant).
    One or more catalogue ids, sorted; ``ValueError`` for an unknown type or variant."""
    v = str(getattr(ptype, "value", ptype)).upper()
    if v == "ROUND_TRIP":
        v = "ROUND_TRIP_CYCLE"
    if v not in GENOME_TECHNIQUES:
        raise ValueError(f"no HELIX_Technique mapping for Pattern_Genome type {ptype!r}")
    out = set(GENOME_TECHNIQUES[v])
    if auth_variant is not None:
        if auth_variant not in AUTH_VARIANT_TECHNIQUES:
            raise ValueError(f"unknown auth-leg variant {auth_variant!r}")
        out |= set(AUTH_VARIANT_TECHNIQUES[auth_variant])
    return tuple(sorted(out))


# --- Evidence_Chain entries and Case tags (Req 30.4) ---------------------------------------


def _kind(e: Any) -> str:
    k = getattr(e, "kind", "")
    return str(getattr(k, "value", k))


def _split(v: Any, sep: str) -> list[str]:
    return [x.strip() for x in str(v or "").split(sep) if x.strip()]


def entry_techniques(e: Any, catalogue: Optional[Catalogue] = None,
                     mapping: Optional[ContributorMapping] = None) -> tuple[str, ...]:
    """Technique ids of one Evidence_Chain entry (sorted; empty for non-contributor records and
    for sensor entries without FRAUD evidence). Ids outside the catalogue are ignored."""
    if _kind(e) not in ("CONTRIBUTOR", "MOTIF"):
        return ()
    cat = catalogue or default_catalogue()
    mp = mapping or default_mapping()
    ctype = str(getattr(e, "ctype", "") or "")
    feats: Mapping[str, Any] = getattr(e, "features", {}) or {}
    if ctype == "RULE":
        ids = set(_split(feats.get("techniques"), RULE_TECHNIQUE_SEP))
    elif ctype == "ANTIBODY":
        ids = set(antibody_techniques(_split(feats.get("tags"), ANTIBODY_TAG_SEP), cat))
    else:
        ids = set(mp.techniques_of_keys(stage_keys(e)))
    return tuple(sorted(i for i in ids if i in cat))


@dataclass(frozen=True, slots=True)
class EntryTechniques:
    contributor_id: str
    tx_id: str
    ctype: Optional[str]
    techniques: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        return {"contributor_id": self.contributor_id, "tx_id": self.tx_id, "ctype": self.ctype,
                "techniques": list(self.techniques)}


def chain_techniques(chain: Iterable[Any], catalogue: Optional[Catalogue] = None,
                     mapping: Optional[ContributorMapping] = None) -> list[EntryTechniques]:
    """Per-contributor technique ids in chain order (entries with no technique omitted)."""
    out = []
    for e in chain:
        ids = entry_techniques(e, catalogue, mapping)
        if ids:
            out.append(EntryTechniques(e.contributor_id, e.tx_id, getattr(e, "ctype", None), ids))
    return out


def case_techniques(chain: Optional[Iterable[Any]], catalogue: Optional[Catalogue] = None,
                    mapping: Optional[ContributorMapping] = None) -> tuple[str, ...]:
    """A Case's HELIX_Technique tags: the union over its Evidence_Chain contributors (Req 30.4)."""
    if chain is None:
        return ()
    return tuple(sorted({t for e in chain for t in entry_techniques(e, catalogue, mapping)}))


__all__ = [
    "ANTIBODY_UNATTRIBUTED",
    "AUTH_VARIANT_TECHNIQUES",
    "CATALOGUE_PATH",
    "GENOME_TECHNIQUES",
    "MAPPING_PATH",
    "NOTICE",
    "TECHNIQUE_ID",
    "UNATTRIBUTED_TECHNIQUE",
    "Catalogue",
    "CatalogueError",
    "ContributorMapping",
    "Domain",
    "EntryTechniques",
    "Technique",
    "UnknownTechniqueError",
    "antibody_techniques",
    "builtin_contributor_keys",
    "case_techniques",
    "chain_techniques",
    "default_catalogue",
    "default_mapping",
    "entry_techniques",
    "genome_techniques",
    "is_technique_id",
    "motif_techniques",
    "sensor_names",
    "sensor_techniques",
]
