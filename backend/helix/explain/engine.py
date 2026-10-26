"""Explanation_Engine: deterministic plain-language reasons (design §14, Req 16).

Contributors (CONTRIBUTOR and MOTIF entries) are grouped by contributor ID. For each
group, with discounted masses ``f' = w·f`` and ``l' = w·l`` per entry, the group's
values are the strongest evidence in each direction, ``F' = max f'`` and
``L' = max l'``:

- ``F' > L'`` → reasons, ordered by ``(-F', name)``;
- ``L' > F'`` → mitigating, ordered by ``(-L', name)``;
- ``F' == L'`` (vacuous, zero weight, balanced) → insufficient, ordered by name,
  naming the missing field or history.

Each item cites its name, weight, tx_ids and the type template's comparison values,
taken from the representative entry (the one achieving the decisive maximum, first in
chain order on ties). Templates are looked up by contributor kind; anything else uses
the generic template (Req 16.9).

Determinism: the engine reads only chain and header values, walks the chain in its
canonical order, uses no clock and only locale-independent format specs.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal
from types import MappingProxyType
from typing import Any

from helix.explain.chain import EntryKind, EvidenceChain, EvidenceEntry

Template = Callable[[EvidenceEntry], str]
NO_EVIDENCE = "no contributing evidence"


# --- number formatting (locale-independent, no "-0.000") -------------------------


def fmt(x: float, nd: int = 3) -> str:
    s = f"{float(x):.{nd}f}"
    if s.startswith("-") and float(s) == 0.0:
        s = s[1:]
    return s


def fmt_amount(a: Decimal) -> str:
    return f"{Decimal(a).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP):f}"


# --- header -------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class CaseHeader:
    """Case-level values for the summary line (from the Case's stored assessment)."""

    case_id: str
    tier: str
    risk: float
    belief: float
    plausibility: float
    uncertainty: float
    conflict: float
    at_risk: tuple[tuple[str, Decimal], ...] = ()  # (ISO currency, amount), sorted by code

    @classmethod
    def from_case(cls, case: Any, currency: str | Mapping[str, str] = "EUR") -> CaseHeader:
        """Header from a ``helix.cases.Case``: stage-2 assessment when Motifs are
        attached, else the highest-risk event; vacuous when nothing is stored."""
        cr = getattr(case, "case_risk", None)
        fa = None
        if cr is not None:
            fa = cr.stage2 or cr.best_tx
        elif case.assessments:
            from helix.risk import best_assessment

            fa = best_assessment(list(case.assessments.values()))
        b, p, u, k = (fa.belief, fa.plausibility, fa.uncertainty, fa.conflict) if fa else (0.0, 1.0, 1.0, 0.0)
        totals: dict[str, Decimal] = {}
        for tx, amt in sorted(case.amounts.items()):
            ccy = currency if isinstance(currency, str) else currency[tx]
            totals[ccy] = totals.get(ccy, Decimal("0")) + amt
        tier_name = str(getattr(case.tier, "value", case.tier))  # SeverityTier's str() is "SeverityTier.X"
        return cls(case.case_id, tier_name, float(case.risk), b, p, u, k, tuple(sorted(totals.items())))


def summary_line(h: CaseHeader) -> str:
    amounts = ", ".join(f"{fmt_amount(a)} {c}" for c, a in sorted(h.at_risk)) or "n/a"
    return (f"{h.tier} | risk {fmt(h.risk)} | Bel {fmt(h.belief)} Pl {fmt(h.plausibility)} "
            f"U {fmt(h.uncertainty)} K {fmt(h.conflict)} | at risk {amounts}")


# --- templates ------------------------------------------------------------------------


def _n(e: EvidenceEntry, k: str) -> float:
    v = e.features[k]
    if isinstance(v, bool) or not isinstance(v, (int, float, Decimal)):
        raise TypeError(f"feature {k} is not numeric")
    return float(v)


def _s(e: EvidenceEntry, k: str) -> str:
    v = e.features[k]
    if v is None:
        raise KeyError(k)
    return str(v)


def t_amount(e: EvidenceEntry) -> str:
    return (f"amount {fmt(_n(e, 'amount'), 2)} vs baseline mean {fmt(_n(e, 'mean'), 2)} "
            f"(sd {fmt(_n(e, 'std'), 2)}, z {fmt(_n(e, 'z'), 2)})")


def t_velocity(e: EvidenceEntry) -> str:
    parts = [f"{w} {int(_n(e, f'count_{w}'))} vs expected {fmt(_n(e, f'expected_{w}'), 2)}"
             for w in ("1h", "24h", "7d") if f"count_{w}" in e.features]
    if not parts:
        raise KeyError("count_*")
    return f"events {'; '.join(parts)} (ratio {fmt(_n(e, 'ratio'), 2)})"


_WEEKDAYS = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")


def t_timing(e: EvidenceEntry) -> str:
    wd = int(_n(e, "weekday"))
    return (f"{int(_n(e, 'hour')):02d}:00 UTC on {_WEEKDAYS[wd]} vs baseline frequency "
            f"hour {fmt(_n(e, 'freq_hour'))}, weekday {fmt(_n(e, 'freq_weekday'))} (p {fmt(_n(e, 'p'), 4)})")


def t_geo(e: EvidenceEntry) -> str:
    return (f"{fmt(_n(e, 'distance_km'), 1)} km in {fmt(_n(e, 'dt_h'), 2)} h = {fmt(_n(e, 'speed_kmh'), 0)} km/h "
            f"vs plausible {fmt(_n(e, 'vmax_kmh'), 0)} km/h")


def t_counterparty(e: EvidenceEntry) -> str:
    known = _n(e, "known") >= 1.0
    return (f"payee {_s(e, 'dst')} {'known' if known else 'not seen before'} "
            f"(baseline {int(_n(e, 'n_counterparties'))} known counterparties)")


def t_drift(e: EvidenceEntry) -> str:
    cand = e.features.get("candidate_tx")
    return f"change-point state {_s(e, 'cpd_kind')}" + (f" (candidate {cand})" if cand else "")


def t_motif(e: EvidenceEntry) -> str:
    return f"{_s(e, 'motif_type')} over accounts {_s(e, 'accounts')} ({int(_n(e, 'n_tx'))} tx)"


def t_antibody(e: EvidenceEntry) -> str:
    aid = e.features.get("antibody_id") or e.contributor_id
    return f"antibody {aid} matched features {_s(e, 'matched')}"


def t_rule(e: EvidenceEntry) -> str:
    rid = e.features.get("rule_id") or e.contributor_id
    return f"rule {rid} version {_s(e, 'version')}"


def t_indicator(e: EvidenceEntry) -> str:
    return f"indicator {_s(e, 'value')} confidence {fmt(_n(e, 'confidence'))}"


_AUTH_PHRASES = {
    "failed_logins": lambda e: f"{int(_n(e, 'failed_logins'))} failed logins in a burst",
    "mfa_failure": lambda e: "MFA failure",
    "password_reset": lambda e: "password reset",
    "new_device": lambda e: f"login from new device {e.features.get('new_device') or '(unknown)'}",
    "new_payee": lambda e: (f"new payee {e.features.get('new_payee') or '(unknown)'} added"
                            + (" and paid here" if e.features.get("payee_match") == 1.0 else "")),
}


def t_auth(e: EvidenceEntry) -> str:
    """Auth_Sensor: the Suspicious_Auth_Activity before the payment (Req 28.8)."""
    window = fmt(_n(e, "window_s") / 60.0, 0)
    signals = [s for s in _s(e, "signals").split("|") if s]
    if not signals:  # benign linked login
        return f"payment follows a login from a known device ({int(_n(e, 'linked'))} linked auth events)"
    phrases = "; ".join(_AUTH_PHRASES[s](e) if s in _AUTH_PHRASES else s for s in signals)
    return (f"{phrases} within {window} min before the payment "
            f"(suspicious auth events {_s(e, 'suspicious_ids')})")


def t_generic(e: EvidenceEntry) -> str:
    m = e.mass
    assert m is not None
    return f"m(F) {fmt(m.f)} m(L) {fmt(m.l)} m(F,L) {fmt(m.t)}"


_MOTIF_KEYS = ("ROUND_TRIP_CYCLE", "FAN_OUT", "FAN_IN", "STRUCTURING", "MULE_CHAIN")

DEFAULT_TEMPLATES: Mapping[str, Template] = MappingProxyType({
    "amount": t_amount,
    "velocity": t_velocity,
    "timing": t_timing,
    "geo": t_geo,
    "counterparty": t_counterparty,
    "drift": t_drift,
    "auth": t_auth,
    **{k: t_motif for k in _MOTIF_KEYS},
    "antibody": t_antibody,
    "rule": t_rule,
    "indicator": t_indicator,
})

LABELS: Mapping[str, str] = MappingProxyType({
    "amount": "Amount", "velocity": "Velocity", "timing": "Timing", "geo": "Geo",
    "counterparty": "Counterparty", "drift": "Drift", "auth": "Auth",
    "ROUND_TRIP_CYCLE": "Round-trip cycle", "FAN_OUT": "Fan-out", "FAN_IN": "Fan-in",
    "STRUCTURING": "Structuring", "MULE_CHAIN": "Mule chain",
    "antibody": "Antibody", "rule": "Rule", "indicator": "Indicator",
})


def template_key(e: EvidenceEntry) -> str:
    """Registry key: sensor name, Motif type, or the lower-cased contributor type."""
    if e.ctype == "SENSOR":
        return e.contributor_id
    if e.ctype == "MOTIF":
        mt = e.features.get("motif_type")
        return str(mt) if mt else "motif"
    return str(e.ctype).lower()


# --- explanation model ------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ExplanationItem:
    name: str  # contributor ID (sort key)
    label: str
    ctype: str
    weight: float
    tx_ids: tuple[str, ...]
    f: float  # F' = max discounted mass on {FRAUD}
    l: float  # noqa: E741 - L' = max discounted mass on {LEGIT}
    comparison: str  # type template output, or the insufficiency reason
    generic: bool  # rendered with the generic template

    @property
    def text(self) -> str:
        return (f"{self.label}: {self.comparison} | weight {fmt(self.weight, 2)}, "
                f"F' {fmt(self.f)} vs L' {fmt(self.l)}, tx {', '.join(self.tx_ids)}")


@dataclass(frozen=True, slots=True)
class Explanation:
    case_id: str
    summary: str
    reasons: tuple[ExplanationItem, ...]
    mitigating: tuple[ExplanationItem, ...]
    insufficient: tuple[ExplanationItem, ...]
    notes: tuple[str, ...]  # missing-field, sensor-failure, change-point, late and truncation records
    message: str | None  # NO_EVIDENCE when the chain has no contributors

    @property
    def text(self) -> str:
        lines = [self.summary]
        if self.message:
            lines.append(self.message)
        for title, items, numbered in (("Reasons", self.reasons, True),
                                       ("Mitigating", self.mitigating, False),
                                       ("Insufficient evidence", self.insufficient, False)):
            if items:
                lines.append(f"{title} ({len(items)}):")
                lines.extend(f"  {f'{i}.' if numbered else '-'} {it.text}" for i, it in enumerate(items, 1))
        if self.notes:
            lines.append(f"Records ({len(self.notes)}):")
            lines.extend(f"  - {n}" for n in self.notes)
        return "\n".join(lines)


def _insufficient_reason(group: Sequence[EvidenceEntry]) -> str:
    reasons: list[str] = []
    for e in group:
        r = e.detail.get("vacuous_reason")
        if isinstance(r, str) and r:
            if r.startswith("missing:"):
                fields = [f for f in r[len("missing:"):].split(",") if f]
                hist = [f[len("dna."):] for f in fields if f.startswith("dna.")]
                ev = [f for f in fields if not f.startswith("dna.")]
                if ev:
                    reasons.append("missing field " + ", ".join(ev))
                if hist:
                    reasons.append("missing history for " + ", ".join(hist))
            elif r.startswith("history:"):
                reasons.append(f"insufficient history ({r[len('history:'):]} prior events)")
            else:
                reasons.append(f"sensor failure ({r})")
        elif e.weight == 0.0:
            reasons.append("weight 0")
    if not reasons:
        reasons.append("no decisive evidence (F' = L')")
    return "; ".join(dict.fromkeys(reasons))


def _note(e: EvidenceEntry) -> str | None:
    d = e.detail
    if e.kind is EntryKind.MISSING_FIELD:
        return f"{e.tx_id}: missing field {d.get('field')} (sensors {d.get('sensors')})"
    if e.kind is EntryKind.SENSOR_FAILURE:
        err = d.get("error_text")
        return f"{e.tx_id}: sensor {d.get('sensor')} {d.get('outcome')}" + (f": {err}" if err else "")
    if e.kind is EntryKind.CHANGE_POINT:
        cls = d.get("classification") or d.get("outcome")
        return f"{e.tx_id}: change point" + (f" {cls}" if cls else "")
    if e.kind is EntryKind.LATE:
        return f"{e.tx_id}: late event"
    if e.kind is EntryKind.TRUNCATION:
        return f"{e.tx_id}: motif search truncated ({d.get('motif_type') or d.get('motif_id')})"
    if e.kind is EntryKind.AUTH_EVENT:
        sig = d.get("signal")
        by = d.get("linked_by")
        how = f"suspicious ({sig})" if sig else "linked"
        if by:
            how += f" by {by}"
        return f"{e.tx_id}: auth {d.get('event_type')} {d.get('event_id')} at {e.ts.isoformat()} {how}"
    return None


class ExplanationEngine:
    """Template registry plus the partition/ordering logic. Stateless after init."""

    def __init__(self, templates: Mapping[str, Template] | None = None) -> None:
        self.templates: Mapping[str, Template] = MappingProxyType(dict(DEFAULT_TEMPLATES if templates is None
                                                                      else templates))

    def _render(self, e: EvidenceEntry) -> tuple[str, bool]:
        t = self.templates.get(template_key(e))
        if t is not None:
            try:
                return t(e), False
            except (KeyError, TypeError, ValueError, IndexError):
                pass  # entry lacks the template's fields: fall back to generic
        return t_generic(e), True

    def _item(self, name: str, group: list[EvidenceEntry], bucket: str) -> ExplanationItem:
        fs = [e.weight * e.mass.f for e in group]  # type: ignore[operator,union-attr]
        ls = [e.weight * e.mass.l for e in group]  # type: ignore[operator,union-attr]
        F, L = max(fs), max(ls)
        if bucket == "reason":
            rep = group[fs.index(F)]  # first in chain order on ties
        elif bucket == "mitigating":
            rep = group[ls.index(L)]
        else:
            rep = group[0]
        key = template_key(rep)
        label = LABELS.get(key, name)
        if bucket == "insufficient":
            comparison, generic = _insufficient_reason(group), False
        else:
            comparison, generic = self._render(rep)
        tx_ids = tuple(dict.fromkeys(e.tx_id for e in group))
        return ExplanationItem(name, label, str(rep.ctype), float(rep.weight), tx_ids,  # type: ignore[arg-type]
                               F, L, comparison, generic)

    def explain(self, case: CaseHeader | Any, chain: EvidenceChain | None,
                currency: str | Mapping[str, str] = "EUR") -> Explanation:
        header = case if isinstance(case, CaseHeader) else CaseHeader.from_case(case, currency)
        groups: dict[str, list[EvidenceEntry]] = {}
        notes: list[str] = []
        for e in (chain or ()):  # canonical (ts, tx_id, contributor_id) order
            if e.is_contributor:
                groups.setdefault(e.contributor_id, []).append(e)
            elif (n := _note(e)) is not None:
                notes.append(n)

        reasons, mitigating, insufficient = [], [], []
        for name in sorted(groups):
            g = groups[name]
            F = max(e.weight * e.mass.f for e in g)  # type: ignore[operator,union-attr]
            L = max(e.weight * e.mass.l for e in g)  # type: ignore[operator,union-attr]
            if F > L:
                reasons.append(self._item(name, g, "reason"))
            elif L > F:
                mitigating.append(self._item(name, g, "mitigating"))
            else:
                insufficient.append(self._item(name, g, "insufficient"))
        reasons.sort(key=lambda it: (-it.f, it.name))
        mitigating.sort(key=lambda it: (-it.l, it.name))
        return Explanation(
            case_id=header.case_id,
            summary=summary_line(header),
            reasons=tuple(reasons),
            mitigating=tuple(mitigating),
            insufficient=tuple(insufficient),
            notes=tuple(notes),
            message=None if groups else NO_EVIDENCE,
        )


def explain(case: CaseHeader | Any, chain: EvidenceChain | None,
            templates: Mapping[str, Template] | None = None,
            currency: str | Mapping[str, str] = "EUR") -> Explanation:
    """Module-level convenience: ``ExplanationEngine(templates).explain(case, chain)``."""
    return ExplanationEngine(templates).explain(case, chain, currency)
