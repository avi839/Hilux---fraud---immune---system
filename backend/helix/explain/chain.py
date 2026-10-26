"""Evidence_Chain: per-Case evidence record, reproducibility and strict JSON (design §13, Req 15).

Entries are keyed ``(contributor_id, tx_id)`` (one per pair) and always iterated in
canonical order ``(ts, tx_id, contributor_id)``. Chains are immutable values: ``add``
and ``merge`` return a new chain and never alter an existing entry (Req 15.2, 15.3).

Entry kinds:
- CONTRIBUTOR: one per contributor per event, with its type, undiscounted mass, weight
  and input features (Req 15.1).
- FUSED: the event's stored Fused_Assessment (``~fused``) or a Case stage-2 assessment
  (``~stage2:<digest>``), with everything ``recompute`` needs (Req 15.4, 15.6).
- MOTIF: a Motif instance; also the stage-2 contributor for that instance.
- MISSING_FIELD / SENSOR_FAILURE / CHANGE_POINT / LATE / TRUNCATION / AUTH_EVENT: records
  under reserved ``~`` contributor IDs (AUTH_EVENT: Increment 4 cyber fusion, Req 28.7).

JSON (Req 15.7, 15.8): floats are written by ``json`` with ``float.__repr__`` (exact
IEEE round-trip), ``allow_nan=False``; Decimals are ``{"$decimal": "<str>"}``;
datetimes are ISO 8601 with offset. ``loads`` validates the whole document before
returning and raises ``EvidenceChainError`` naming the offending field.
"""

from __future__ import annotations

import hashlib
import json
import math
import numbers
from collections.abc import Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal, InvalidOperation
from enum import Enum
from types import MappingProxyType
from typing import Any, Union

from helix.fusion import TOL, Contribution, ContributorType, FusedAssessment, Mass, fuse, is_valid

SCHEMA = 1
Scalar = Union[str, int, float, bool, None, Decimal]

FUSED_ID = "~fused"
STAGE2_PREFIX = "~stage2:"
MISSING_PREFIX = "~missing:"
FAILURE_PREFIX = "~failure:"
TRUNCATION_PREFIX = "~truncated:"

#: Contributor types an entry may carry (Increment 4 types are accepted for forward use).
CONTRIBUTOR_TYPES = ("SENSOR", "MOTIF", "ANTIBODY", "FUSED", "RULE", "INDICATOR", "CANARY", "AUTH")


class EntryKind(str, Enum):
    CONTRIBUTOR = "CONTRIBUTOR"
    FUSED = "FUSED"
    MOTIF = "MOTIF"
    CHANGE_POINT = "CHANGE_POINT"
    MISSING_FIELD = "MISSING_FIELD"
    SENSOR_FAILURE = "SENSOR_FAILURE"
    LATE = "LATE"
    TRUNCATION = "TRUNCATION"
    #: An Auth_Event linked to (or flagged as Suspicious_Auth_Activity for) the entry's
    #: Transaction_Event (Req 28.7, 28.8). ``ts`` is the Auth_Event's own timestamp,
    #: contributor id ``~auth:<event_id>``, ctype ``AUTH``. Not a fusion contributor: its
    #: evidence reaches fusion through the ``auth`` Sensor_Agent entry.
    AUTH_EVENT = "AUTH_EVENT"

    def __str__(self) -> str:  # StrEnum-like behaviour on Python 3.10
        return self.value


AUTH_PREFIX = "~auth:"


#: Entry kinds that are contributors (explained, and used by recompute).
CONTRIBUTOR_KINDS = frozenset({EntryKind.CONTRIBUTOR, EntryKind.MOTIF})
_NEEDS_MASS = frozenset({EntryKind.CONTRIBUTOR, EntryKind.MOTIF, EntryKind.FUSED})


class EvidenceChainError(ValueError):
    """Invalid Evidence_Chain content; ``field`` is the offending field path."""

    def __init__(self, field_path: str, reason: str) -> None:
        super().__init__(f"{field_path}: {reason}")
        self.field = field_path
        self.reason = reason


# --- validation helpers ---------------------------------------------------------


def _is_num(x: object) -> bool:
    return isinstance(x, (int, float)) and not isinstance(x, bool)


def _check_scalar(v: object, path: str) -> None:
    if v is None or isinstance(v, (str, bool, int)):
        return
    if isinstance(v, float):
        if not math.isfinite(v):
            raise EvidenceChainError(path, f"non-finite number {v!r}")
        return
    if isinstance(v, Decimal):
        if not v.is_finite():
            raise EvidenceChainError(path, f"non-finite decimal {v!r}")
        return
    raise EvidenceChainError(path, f"unsupported value type {type(v).__name__}")


def _check_mapping(m: object, path: str) -> dict[str, Scalar]:
    if not isinstance(m, Mapping):
        raise EvidenceChainError(path, f"expected a mapping, got {type(m).__name__}")
    out: dict[str, Scalar] = {}
    for k, v in m.items():
        if not isinstance(k, str):
            raise EvidenceChainError(path, f"non-string key {k!r}")
        _check_scalar(v, f"{path}.{k}")
        if isinstance(v, float) and type(v) is not float:  # e.g. numpy.float64
            v = float(v)
        out[k] = v
    return out


def _check_mass(m: object, path: str) -> Mass:
    if not isinstance(m, Mass):
        raise EvidenceChainError(path, f"expected a Mass, got {type(m).__name__}")
    for name in ("f", "l", "t"):
        x = getattr(m, name)
        if not _is_num(x) or not math.isfinite(x):
            raise EvidenceChainError(f"{path}.{name}", f"not a finite number: {x!r}")
    if not is_valid(m):
        raise EvidenceChainError(path, f"violates the Mass_Function definition: {m!r}")
    return Mass(float(m.f), float(m.l), float(m.t))


def _validate_entry(e: EvidenceEntry, p: str) -> None:
    if not (isinstance(e.ts, datetime) and e.ts.utcoffset() is not None):
        raise EvidenceChainError(f"{p}.ts", "must be a timezone-aware datetime")
    for name in ("tx_id", "contributor_id"):
        v = getattr(e, name)
        if not (isinstance(v, str) and v):
            raise EvidenceChainError(f"{p}.{name}", "must be a non-empty string")
    try:
        kind = EntryKind(e.kind)
    except ValueError:
        raise EvidenceChainError(f"{p}.kind", f"unknown entry kind {e.kind!r}") from None
    object.__setattr__(e, "kind", kind)

    if kind in CONTRIBUTOR_KINDS:
        if e.ctype not in CONTRIBUTOR_TYPES:
            raise EvidenceChainError(f"{p}.ctype", f"unknown contributor type {e.ctype!r}")
    elif e.ctype is not None and e.ctype not in CONTRIBUTOR_TYPES:
        raise EvidenceChainError(f"{p}.ctype", f"unknown contributor type {e.ctype!r}")
    if e.ctype is not None:
        object.__setattr__(e, "ctype", str(e.ctype))

    if e.mass is None:
        if kind in _NEEDS_MASS:
            raise EvidenceChainError(f"{p}.mass", f"required for {kind.value} entries")
    else:
        object.__setattr__(e, "mass", _check_mass(e.mass, f"{p}.mass"))

    if e.weight is None:
        if kind in CONTRIBUTOR_KINDS:
            raise EvidenceChainError(f"{p}.weight", f"required for {kind.value} entries")
    else:
        w = e.weight
        if not (_is_num(w) and math.isfinite(w) and 0.0 <= w <= 1.0):
            raise EvidenceChainError(f"{p}.weight", f"must be a finite number in [0, 1], got {w!r}")
        object.__setattr__(e, "weight", float(w))

    object.__setattr__(e, "features", MappingProxyType(_check_mapping(e.features, f"{p}.features")))
    object.__setattr__(e, "detail", MappingProxyType(_check_mapping(e.detail, f"{p}.detail")))


# --- data model -------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class EvidenceEntry:
    ts: datetime  # timestamp of the Transaction_Event (timezone-aware)
    tx_id: str
    contributor_id: str
    kind: EntryKind
    ctype: str | None = None  # contributor type (ContributorType value or Increment 4 type)
    mass: Mass | None = None  # undiscounted (contributors) or fused (FUSED)
    weight: float | None = None  # Sensor_Weight applied
    features: Mapping[str, Scalar] = field(default_factory=dict)  # input features
    detail: Mapping[str, Scalar] = field(default_factory=dict)  # kind-specific record fields

    def __post_init__(self) -> None:
        _validate_entry(self, "entry")

    @property
    def key(self) -> tuple[str, str]:
        return (self.contributor_id, self.tx_id)

    @property
    def order(self) -> tuple[datetime, str, str]:
        return (self.ts, self.tx_id, self.contributor_id)

    @property
    def is_contributor(self) -> bool:
        return self.kind in CONTRIBUTOR_KINDS


def _ordered(entries: Iterable[EvidenceEntry]) -> list[EvidenceEntry]:
    return sorted(entries, key=lambda e: e.order)


def _union(base: dict[tuple[str, str], EvidenceEntry], new: Iterable[EvidenceEntry], strict: bool) -> None:
    """Add entries to ``base``. An identical duplicate is a no-op; a conflicting one
    raises when ``strict`` and is ignored otherwise (earlier entry kept unchanged)."""
    for e in new:
        if not isinstance(e, EvidenceEntry):
            raise TypeError(f"not an EvidenceEntry: {type(e).__name__}")
        prev = base.get(e.key)
        if prev is None:
            base[e.key] = e
        elif prev != e and strict:
            raise EvidenceChainError(
                f"entries[{e.contributor_id!r}, {e.tx_id!r}]",
                "already recorded with different content (entries are append-only)",
            )


@dataclass(frozen=True, slots=True)
class EvidenceChain:
    case_id: str
    entries: Mapping[tuple[str, str], EvidenceEntry] = field(default_factory=dict)
    schema: int = SCHEMA

    def __post_init__(self) -> None:
        if not (isinstance(self.case_id, str) and self.case_id):
            raise EvidenceChainError("case_id", "must be a non-empty string")
        if self.schema != SCHEMA or isinstance(self.schema, bool):
            raise EvidenceChainError("schema", f"unsupported schema {self.schema!r}")
        for k, e in self.entries.items():
            if not isinstance(e, EvidenceEntry) or k != e.key:
                raise EvidenceChainError("entries", f"key {k!r} does not match its entry")
        canonical = {e.key: e for e in _ordered(self.entries.values())}
        object.__setattr__(self, "entries", MappingProxyType(canonical))

    # -- construction ----------------------------------------------------------

    @classmethod
    def of(cls, case_id: str, entries: Iterable[EvidenceEntry] = ()) -> EvidenceChain:
        """Chain from entries in any order; conflicting duplicates raise."""
        acc: dict[tuple[str, str], EvidenceEntry] = {}
        _union(acc, entries, strict=True)
        return cls(case_id, acc)

    def add(self, *entries: EvidenceEntry) -> EvidenceChain:
        """Append entries (Req 15.2). Re-adding an identical entry is a no-op; changing
        an existing ``(contributor_id, tx_id)`` entry raises ``EvidenceChainError``."""
        acc = dict(self.entries)
        _union(acc, entries, strict=True)
        return EvidenceChain(self.case_id, acc, self.schema)

    def merge(self, *others: EvidenceChain) -> EvidenceChain:
        """Union for a Case merge (Req 15.3): at most one entry per (contributor, tx);
        on a conflicting pair the survivor's (``self``) entry is kept."""
        acc = dict(self.entries)
        for o in others:
            _union(acc, o.entries.values(), strict=False)
        return EvidenceChain(self.case_id, acc, self.schema)

    @classmethod
    def build(
        cls,
        case: Any,
        per_tx_sensor_results: Mapping[str, Sequence[Any]] | None = None,
        per_tx_ts: Mapping[str, datetime] | None = None,
        extra: Iterable[EvidenceEntry] = (),
        *,
        high_conflict: float = 0.95,
        currency: str | Mapping[str, str] = "EUR",
    ) -> EvidenceChain:
        """Chain for a Case from its stored assessments, Motifs and sensor results.

        ``per_tx_sensor_results`` holds SensorResult-like objects (``name``, ``outcome``,
        ``output`` with ``features``/``vacuous_reason``, ``elapsed_ms``, ``error_text``).
        Contributor masses and weights come from each stored ``FusedAssessment``, so the
        chain reproduces exactly what was fused. ``high_conflict`` must be the threshold
        the assessments were fused with. ``extra`` adds CHANGE_POINT / LATE / other records.
        """
        return cls.of(case.case_id, _build_entries(case, per_tx_sensor_results or {}, per_tx_ts or {},
                                                   high_conflict, currency, extra))

    # -- views -----------------------------------------------------------------

    def __iter__(self) -> Iterator[EvidenceEntry]:
        return iter(self.entries.values())

    def __len__(self) -> int:
        return len(self.entries)

    def contributors(self) -> list[EvidenceEntry]:
        return [e for e in self if e.is_contributor]

    def of_kind(self, kind: EntryKind) -> list[EvidenceEntry]:
        return [e for e in self if e.kind is kind]

    @property
    def tx_ids(self) -> tuple[str, ...]:
        """Contributing Transaction_Event IDs in chain order."""
        return tuple(dict.fromkeys(e.tx_id for e in self))

    # -- reproducibility (Req 15.6) ----------------------------------------------

    def recompute(self) -> dict[tuple[str, str], FusedAssessment]:
        """Re-fuse every FUSED entry from the stored undiscounted masses and weights."""
        motifs = {e.contributor_id: e for e in self if e.kind is EntryKind.MOTIF}
        out: dict[tuple[str, str], FusedAssessment] = {}
        for e in self.of_kind(EntryKind.FUSED):
            p = f"entries[{e.contributor_id!r}, {e.tx_id!r}].detail"
            hc = _detail_num(e, "hc_threshold", p)
            if _detail(e, "stage", p) == 2:
                seed = Mass(_detail_num(e, "seed_f", p), _detail_num(e, "seed_l", p), _detail_num(e, "seed_t", p))
                ids = [i for i in str(_detail(e, "motifs", p) or "").split("|") if i]
                ws = _stage2_weights(e, len(ids), p)
                cs = [Contribution("stage1", ContributorType.FUSED, seed, 1.0)]
                for n, mid in enumerate(ids):
                    m = motifs.get(mid)
                    if m is None:
                        raise EvidenceChainError(f"{p}.motifs", f"motif {mid!r} not in the chain")
                    fid = m.detail.get("fuser_id") or m.contributor_id
                    w = m.weight if ws is None else ws[n]
                    cs.append(Contribution(str(fid), ContributorType.MOTIF, m.mass, w))  # type: ignore[arg-type]
                fa, _ = fuse(cs, hc, tx_id=e.tx_id, stage=2)
            else:
                cs = [
                    Contribution(c.contributor_id, _ctype(c.ctype), c.mass, c.weight)  # type: ignore[arg-type]
                    for c in self
                    if c.kind is EntryKind.CONTRIBUTOR and c.tx_id == e.tx_id
                ]
                fa, _ = fuse(cs, hc, tx_id=e.tx_id, stage=1)
            out[e.key] = fa
        return out

    def verify(self, tol: float = TOL) -> list[str]:
        """Mismatches between stored and recomputed Fused_Assessments (empty = reproducible)."""
        problems: list[str] = []
        for key, fa in self.recompute().items():
            e = self.entries[key]
            assert e.mass is not None
            stored = {
                "mass.f": e.mass.f, "mass.l": e.mass.l, "mass.t": e.mass.t,
                "belief": e.detail.get("belief"), "plausibility": e.detail.get("plausibility"),
                "uncertainty": e.detail.get("uncertainty"), "conflict": e.detail.get("conflict"),
            }
            got = {
                "mass.f": fa.mass.f, "mass.l": fa.mass.l, "mass.t": fa.mass.t,
                "belief": fa.belief, "plausibility": fa.plausibility,
                "uncertainty": fa.uncertainty, "conflict": fa.conflict,
            }
            for name, v in stored.items():
                if not (_is_num(v) and abs(float(v) - got[name]) <= tol):  # type: ignore[arg-type]
                    problems.append(f"{key}: {name} stored {v!r} recomputed {got[name]!r}")
            if e.detail.get("high_conflict") is not fa.high_conflict:
                problems.append(f"{key}: high_conflict stored {e.detail.get('high_conflict')!r} "
                                f"recomputed {fa.high_conflict!r}")
        return problems


def _ctype(s: str | None) -> ContributorType | str:
    try:
        return ContributorType(s)
    except ValueError:
        return str(s)


def _stage2_weights(e: EvidenceEntry, n: int, p: str) -> list[float] | None:
    """Per-motif weights recorded on a stage-2 entry (None for entries written without them)."""
    raw = e.detail.get("motif_weights")
    if raw is None:
        return None
    parts = [x for x in str(raw).split("|") if x]
    try:
        ws = [float(x) for x in parts]
    except ValueError:
        raise EvidenceChainError(f"{p}.motif_weights", f"not a list of numbers: {raw!r}") from None
    if len(ws) != n or not all(math.isfinite(w) and 0.0 <= w <= 1.0 for w in ws):
        raise EvidenceChainError(f"{p}.motif_weights", f"expected {n} weights in [0, 1], got {raw!r}")
    return ws


def _detail(e: EvidenceEntry, k: str, p: str) -> Scalar:
    if k not in e.detail:
        raise EvidenceChainError(f"{p}.{k}", "missing required field")
    return e.detail[k]


def _detail_num(e: EvidenceEntry, k: str, p: str) -> float:
    v = _detail(e, k, p)
    if not _is_num(v):
        raise EvidenceChainError(f"{p}.{k}", f"expected a number, got {v!r}")
    return float(v)  # type: ignore[arg-type]


# --- builder --------------------------------------------------------------------


def fused_entry(
    fa: FusedAssessment,
    ts: datetime,
    high_conflict: float,
    *,
    contributor_id: str = FUSED_ID,
    amount: Decimal | None = None,
    currency: str | None = None,
    extra_detail: Mapping[str, Scalar] | None = None,
) -> EvidenceEntry:
    """FUSED entry holding a Fused_Assessment and its recompute inputs."""
    detail: dict[str, Scalar] = {
        "stage": int(fa.stage),
        "belief": fa.belief,
        "plausibility": fa.plausibility,
        "uncertainty": fa.uncertainty,
        "conflict": fa.conflict,
        "high_conflict": bool(fa.high_conflict),
        "hc_threshold": float(high_conflict),
        "amount": amount,
        "currency": currency,
    }
    detail.update(extra_detail or {})
    return EvidenceEntry(ts, fa.tx_id, contributor_id, EntryKind.FUSED, None, fa.mass, None, {}, detail)


def _scalar_or_none(v: object) -> bool:
    try:
        _check_scalar(v, "")
        return True
    except EvidenceChainError:
        return False


def _features_of(m: Mapping[Any, Any]) -> dict[str, Scalar]:
    """Scalar features only; numpy integers become ints, non-scalars are dropped."""
    out: dict[str, Scalar] = {}
    for k, v in m.items():
        if isinstance(v, numbers.Integral) and not isinstance(v, (bool, int)):
            v = int(v)
        if isinstance(k, str) and _scalar_or_none(v):
            out[k] = v
    return out


def _build_entries(
    case: Any,
    results_by_tx: Mapping[str, Sequence[Any]],
    ts_by_tx: Mapping[str, datetime],
    hc: float,
    currency: str | Mapping[str, str],
    extra: Iterable[EvidenceEntry],
) -> list[EvidenceEntry]:
    assessments: Mapping[str, FusedAssessment] = getattr(case, "assessments", {}) or {}
    amounts: Mapping[str, Decimal] = getattr(case, "amounts", {}) or {}
    motifs: Mapping[str, Any] = getattr(case, "motifs", {}) or {}
    case_risk = getattr(case, "case_risk", None)

    edge_ts: dict[str, datetime] = {}
    for m in motifs.values():
        for ed in getattr(m, "edges", ()):
            edge_ts.setdefault(ed.tx_id, ed.ts)

    def ts_of(tx: str) -> datetime:
        if tx in ts_by_tx:
            return ts_by_tx[tx]
        if tx in edge_ts:
            return edge_ts[tx]
        raise ValueError(f"evidence chain build: no timestamp for tx {tx!r}")

    def ccy_of(tx: str) -> str:
        if isinstance(currency, str):
            return currency
        if tx not in currency:
            raise ValueError(f"evidence chain build: no currency for tx {tx!r}")
        return currency[tx]

    out: list[EvidenceEntry] = []

    # Per-event contributors, fused assessment and records.
    for tx in sorted(set(assessments) | set(results_by_tx)):
        fa = assessments.get(tx)
        amt = amounts.get(tx) if fa is not None else None
        out.extend(tx_entries(tx, ts_of(tx), fa, results_by_tx.get(tx, ()), hc, amount=amt,
                              currency=ccy_of(tx) if amt is not None else None))

    # Motif instances (records and stage-2 contributors).
    s2_weights = stage2_weights(case_risk)
    for mid, m in sorted(motifs.items()):
        out.extend(motif_entries(mid, m, s2_weights, ts_by_tx))

    # Case stage-2 assessment (best event + Motifs).
    s2 = getattr(case_risk, "stage2", None) if case_risk is not None else None
    if s2 is not None:
        from helix.cases.manager import stage2_motifs  # local: the same selection the Case fused

        e = stage2_entry(case_risk, stage2_motifs(motifs), ts_of(s2.tx_id), hc)
        if e is not None:
            out.append(e)

    out.extend(extra)
    return out


# Building blocks shared by ``EvidenceChain.build`` and incremental (per-delta) updates.


def tx_entries(
    tx: str,
    ts: datetime,
    fa: FusedAssessment | None,
    results: Iterable[Any],
    hc: float,
    *,
    amount: Decimal | None = None,
    currency: str | None = None,
) -> list[EvidenceEntry]:
    """One event's entries: its contributors and FUSED entry (when assessed), SENSOR_FAILURE
    and MISSING_FIELD records from its sensor results."""
    out: list[EvidenceEntry] = []
    by_name = {str(r.name): r for r in results}
    if fa is not None:
        for c in fa.contributions:
            r = by_name.get(c.contributor_id)
            feats: dict[str, Scalar] = {}
            detail: dict[str, Scalar] = {}
            if r is not None:
                feats = _features_of(dict(r.output.features))
                detail = {"outcome": str(r.outcome), "vacuous_reason": r.output.vacuous_reason}
            out.append(EvidenceEntry(ts, tx, c.contributor_id, EntryKind.CONTRIBUTOR, str(c.kind),
                                     c.mass, float(c.weight), feats, detail))
        out.append(fused_entry(fa, ts, hc, amount=amount, currency=currency if amount is not None else None))

    missing: dict[str, list[str]] = {}
    for name in sorted(by_name):
        r = by_name[name]
        reason = r.output.vacuous_reason
        if isinstance(reason, str) and reason.startswith("missing:"):
            for f in reason[len("missing:"):].split(","):
                if f and not f.startswith("dna.") and f != "cpd_state":
                    missing.setdefault(f, []).append(name)
        outcome = str(r.outcome)
        if outcome != "SUCCESS":
            el = getattr(r, "elapsed_ms", None)
            out.append(EvidenceEntry(ts, tx, FAILURE_PREFIX + name, EntryKind.SENSOR_FAILURE, detail={
                "sensor": name,
                "outcome": outcome,
                "error_text": getattr(r, "error_text", None),
                "elapsed_ms": float(el) if _is_num(el) and math.isfinite(el) else None,
            }))
    for f, names in sorted(missing.items()):
        out.append(EvidenceEntry(ts, tx, MISSING_PREFIX + f, EntryKind.MISSING_FIELD,
                                 detail={"field": f, "sensors": ",".join(names)}))
    return out


def stage2_weights(case_risk: Any) -> dict[str, float]:
    """Motif fuser id -> weight used by the Case's stage-2 fusion (empty without one)."""
    s2 = getattr(case_risk, "stage2", None) if case_risk is not None else None
    return {c.contributor_id: float(c.weight) for c in (s2.contributions if s2 else ())}


def motif_entries(
    mid: str, m: Any, s2_weights: Mapping[str, float], ts_by_tx: Mapping[str, datetime] | None = None,
) -> list[EvidenceEntry]:
    """MOTIF entry (and TRUNCATION record) of one attached Motif instance."""
    from helix.motifs import contributor_id as motif_fuser_id  # local: keeps import light

    if m.edges:
        last = max(m.edges, key=lambda ed: (ed.ts, ed.tx_id))
        tx, ts = last.tx_id, last.ts
    else:
        tx = max(m.tx_ids)
        ts = (ts_by_tx or {}).get(tx, m.ts_end)
    fid = motif_fuser_id(m.motif_type)
    feats: dict[str, Scalar] = {
        "motif_type": str(m.motif_type),
        "accounts": " -> ".join(m.accounts),
        "tx_ids": "|".join(sorted(m.tx_ids)),
        "n_tx": len(m.tx_ids),
        "duration_s": float(m.duration_s),
    }
    for k, v in sorted(_features_of(dict(m.detail)).items()):
        feats[f"detail.{k}"] = v
    detail: dict[str, Scalar] = {"fuser_id": fid, "truncated": bool(m.truncated),
                                 "ts_start": m.ts_start.isoformat(), "ts_end": m.ts_end.isoformat()}
    out = [EvidenceEntry(ts, tx, mid, EntryKind.MOTIF, "MOTIF", m.mass, s2_weights.get(fid, 1.0), feats, detail)]
    if m.truncated:
        out.append(EvidenceEntry(ts, tx, TRUNCATION_PREFIX + mid, EntryKind.TRUNCATION,
                                 detail={"motif_id": mid, "motif_type": str(m.motif_type)}))
    return out


def stage2_entry(
    case_risk: Any, selection: Sequence[tuple[str, Any]], ts: datetime, hc: float,
) -> EvidenceEntry | None:
    """FUSED entry of the Case stage-2 assessment (best event + the selected Motif instances,
    ``helix.cases.manager.stage2_motifs`` order); None without a stage-2 assessment."""
    from helix.motifs import contributor_id as motif_fuser_id  # local: keeps import light

    s2 = getattr(case_risk, "stage2", None) if case_risk is not None else None
    if s2 is None:
        return None
    s2_weights = stage2_weights(case_risk)
    mids = [mid for mid, _ in selection]
    # Weights this fusion used: a learned weight may differ from the one on an earlier
    # (append-only, kept) MOTIF entry of the same instance.
    ws = [repr(s2_weights.get(motif_fuser_id(m.motif_type), 1.0)) for _, m in selection]
    digest = hashlib.sha1("|".join(mids).encode()).hexdigest()[:12]
    seed = case_risk.best_tx.mass
    return fused_entry(s2, ts, hc, contributor_id=STAGE2_PREFIX + digest, extra_detail={
        "motifs": "|".join(mids), "motif_weights": "|".join(ws), "seed_tx": case_risk.best_tx.tx_id,
        "seed_f": seed.f, "seed_l": seed.l, "seed_t": seed.t,
    })


class ChainBuilder:
    """Mutable, append-only accumulator behind one Case's ``EvidenceChain``.

    ``add`` costs O(new entries) instead of the O(chain) copy and re-sort of
    ``EvidenceChain.add`` / ``merge``; the immutable, canonically ordered chain is
    materialised by ``chain()`` only when read and cached until the next change. Union
    semantics match ``EvidenceChain.merge``: an entry whose ``(contributor_id, tx_id)`` is
    already present is ignored (the earlier entry is kept, Req 15.2, 15.3).
    """

    __slots__ = ("case_id", "_acc", "_chain")

    def __init__(self, case_id: str, chain: EvidenceChain | None = None) -> None:
        self.case_id = case_id
        self._acc: dict[tuple[str, str], EvidenceEntry] = dict(chain.entries) if chain is not None else {}
        self._chain: EvidenceChain | None = chain

    def add(self, entries: Iterable[EvidenceEntry]) -> list[EvidenceEntry]:
        """Union ``entries`` in; returns the ones actually added (new keys)."""
        acc = self._acc
        added: list[EvidenceEntry] = []
        for e in entries:
            if not isinstance(e, EvidenceEntry):
                raise TypeError(f"not an EvidenceEntry: {type(e).__name__}")
            if e.key not in acc:
                acc[e.key] = e
                added.append(e)
        if added:
            self._chain = None
        return added

    def chain(self) -> EvidenceChain:
        if self._chain is None:
            self._chain = EvidenceChain(self.case_id, self._acc)
        return self._chain

    def entries(self) -> Iterable[EvidenceEntry]:
        """Current entries in insertion (not canonical) order."""
        return self._acc.values()

    def __len__(self) -> int:
        return len(self._acc)

    def __contains__(self, key: object) -> bool:
        return key in self._acc


# --- JSON -------------------------------------------------------------------------

_ENTRY_FIELDS = ("ts", "tx_id", "contributor_id", "kind", "ctype", "mass", "weight", "features", "detail")
_TOP_FIELDS = ("schema", "case_id", "entries")
_DEC = "$decimal"


def _enc(v: Scalar) -> Any:
    return {_DEC: str(v)} if isinstance(v, Decimal) else v


def _entry_doc(e: EvidenceEntry) -> dict[str, Any]:
    return {
        "ts": e.ts.isoformat(),
        "tx_id": e.tx_id,
        "contributor_id": e.contributor_id,
        "kind": e.kind.value,
        "ctype": e.ctype,
        "mass": None if e.mass is None else {"f": e.mass.f, "l": e.mass.l, "t": e.mass.t},
        "weight": e.weight,
        "features": {k: _enc(v) for k, v in e.features.items()},
        "detail": {k: _enc(v) for k, v in e.detail.items()},
    }


def to_doc(chain: EvidenceChain) -> dict[str, Any]:
    return {"schema": chain.schema, "case_id": chain.case_id, "entries": [_entry_doc(e) for e in chain]}


def dumps(chain: EvidenceChain) -> str:
    """Canonical JSON: entries in chain order, sorted keys, exact float reprs."""
    return json.dumps(to_doc(chain), allow_nan=False, sort_keys=True, separators=(",", ":"))


class _NonFinite:
    """Placeholder for NaN / Infinity tokens so the validator can name the field."""

    __slots__ = ("token",)

    def __init__(self, token: str) -> None:
        self.token = token


def _no_dup_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    d: dict[str, Any] = {}
    for k, v in pairs:
        if k in d:
            raise EvidenceChainError("$", f"duplicate JSON key {k!r}")
        d[k] = v
    return d


def _fields(obj: object, names: Sequence[str], p: str) -> dict[str, Any]:
    if not isinstance(obj, dict):
        raise EvidenceChainError(p, f"expected an object, got {type(obj).__name__}")
    for n in names:
        if n not in obj:
            raise EvidenceChainError(f"{p}.{n}" if p != "$" else n, "missing required field")
    for k in sorted(obj):
        if k not in names:
            raise EvidenceChainError(f"{p}.{k}" if p != "$" else k, "unknown field")
    return obj


def _num(v: object, p: str) -> float:
    if isinstance(v, _NonFinite):
        raise EvidenceChainError(p, f"non-finite number {v.token}")
    if not _is_num(v):
        raise EvidenceChainError(p, f"expected a number, got {type(v).__name__}")
    if not math.isfinite(v):  # e.g. 1e999 parses to inf
        raise EvidenceChainError(p, f"non-finite number {v!r}")
    return v  # type: ignore[return-value]


def _str(v: object, p: str, nullable: bool = False) -> str | None:
    if v is None and nullable:
        return None
    if not isinstance(v, str):
        raise EvidenceChainError(p, f"expected a string, got {type(v).__name__}")
    return v


def _dec_value(v: Any, p: str) -> Scalar:
    if isinstance(v, _NonFinite):
        raise EvidenceChainError(p, f"non-finite number {v.token}")
    if isinstance(v, float):
        return _num(v, p)
    if v is None or isinstance(v, (str, bool, int)):
        return v
    if isinstance(v, dict) and set(v) == {_DEC} and isinstance(v[_DEC], str):
        try:
            d = Decimal(v[_DEC])
        except InvalidOperation:
            raise EvidenceChainError(p, f"invalid decimal {v[_DEC]!r}") from None
        if not d.is_finite():
            raise EvidenceChainError(p, f"non-finite decimal {v[_DEC]!r}")
        return d
    raise EvidenceChainError(p, f"unsupported value {v!r}")


def _map(v: Any, p: str) -> dict[str, Scalar]:
    if not isinstance(v, dict):
        raise EvidenceChainError(p, f"expected an object, got {type(v).__name__}")
    return {k: _dec_value(x, f"{p}.{k}") for k, x in v.items()}


def _parse_entry(raw: Any, p: str) -> EvidenceEntry:
    d = _fields(raw, _ENTRY_FIELDS, p)
    ts_s = _str(d["ts"], f"{p}.ts")
    try:
        ts = datetime.fromisoformat(ts_s)  # type: ignore[arg-type]
    except ValueError:
        raise EvidenceChainError(f"{p}.ts", f"invalid ISO 8601 timestamp {ts_s!r}") from None
    mass = None
    if d["mass"] is not None:
        md = _fields(d["mass"], ("f", "l", "t"), f"{p}.mass")
        mass = Mass(*(float(_num(md[k], f"{p}.mass.{k}")) for k in ("f", "l", "t")))
    weight = None if d["weight"] is None else _num(d["weight"], f"{p}.weight")
    kind = _str(d["kind"], f"{p}.kind")
    try:
        return EvidenceEntry(
            ts=ts,
            tx_id=_str(d["tx_id"], f"{p}.tx_id"),  # type: ignore[arg-type]
            contributor_id=_str(d["contributor_id"], f"{p}.contributor_id"),  # type: ignore[arg-type]
            kind=kind,  # type: ignore[arg-type]
            ctype=_str(d["ctype"], f"{p}.ctype", nullable=True),
            mass=mass,
            weight=weight,
            features=_map(d["features"], f"{p}.features"),
            detail=_map(d["detail"], f"{p}.detail"),
        )
    except EvidenceChainError as e:  # re-root "entry.x" at this entry's path
        raise EvidenceChainError(p + e.field[len("entry"):], e.reason) from None


def loads(s: str | bytes) -> EvidenceChain:
    """Strictly parse a serialised chain; raises ``EvidenceChainError(field)``.

    The whole document is validated before anything is returned; there is no partial
    result and no side effect.
    """
    if not isinstance(s, (str, bytes, bytearray)):
        raise EvidenceChainError("$", f"expected a JSON document, got {type(s).__name__}")
    try:
        doc = json.loads(s, parse_constant=_NonFinite, object_pairs_hook=_no_dup_keys)
    except EvidenceChainError:
        raise
    except (ValueError, RecursionError) as e:  # JSONDecodeError and UnicodeDecodeError are ValueErrors
        raise EvidenceChainError("$", f"malformed JSON: {e}") from None
    d = _fields(doc, _TOP_FIELDS, "$")
    schema = d["schema"]
    if not (isinstance(schema, int) and not isinstance(schema, bool) and schema == SCHEMA):
        raise EvidenceChainError("schema", f"unsupported schema {schema!r}")
    case_id = _str(d["case_id"], "case_id")
    if not isinstance(d["entries"], list):
        raise EvidenceChainError("entries", f"expected an array, got {type(d['entries']).__name__}")
    acc: dict[tuple[str, str], EvidenceEntry] = {}
    for i, raw in enumerate(d["entries"]):
        e = _parse_entry(raw, f"entries[{i}]")
        if e.key in acc:
            raise EvidenceChainError(f"entries[{i}]", f"duplicate (contributor_id, tx_id) {e.key!r}")
        acc[e.key] = e
    return EvidenceChain(case_id, acc)  # type: ignore[arg-type]
