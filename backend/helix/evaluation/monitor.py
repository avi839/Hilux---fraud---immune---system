"""Evaluation_Monitor (design §19; Req 4.20, 4.21, 14.12, 14.13, 20.14).

The monitor is the only reader of ground truth besides the immune miss-detector. The
pipeline feeds it three detection-side signals and never sees a label:

* ``on_event_ingested(tx_id, amount, ts)``: every admitted Transaction_Event.
* ``on_case_admitted(case_id, tx_ids, ts)``: a Case admitted to the Active_Queue, and again
  whenever an admitted Case gains events. Every event in such a Case is a Detected_Event,
  detected at the first ``ts`` it was reported (it stays detected if the Case later moves to
  the Backlog, merges or closes).
* ``on_period(report)``: the Prioritiser's period hook (``prioritiser.period_hooks``).

Labels are pulled lazily from a :class:`GroundTruthReader` at period ends and on
``metrics()``. Only FRAUD records are cached; an ingested event is fraud iff its label is
FRAUD when it is classified at the next refresh. Producers write labels before emitting
events (Req 4.5, 20.11), so this is exact under that contract; a FRAUD label written for an
event that was already classified as non-fraud is not picked up (documented limitation).
"""

from __future__ import annotations

import statistics
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from typing import TYPE_CHECKING, Iterable, Optional, Union

from helix.datasets.patterns import motif_for

from .ground_truth import GroundTruthReader, GTView

if TYPE_CHECKING:  # pragma: no cover
    from helix.cases.prioritiser import PeriodReport

NOT_APPLICABLE = "not applicable"
Ratio = Union[float, str]  # a value in [0, 1], or NOT_APPLICABLE
_ZERO = Decimal("0")


def _ratio(num: Union[int, Decimal], den: Union[int, Decimal]) -> Ratio:
    if den == 0:
        return NOT_APPLICABLE
    return float(Decimal(num) / Decimal(den))


@dataclass(frozen=True, slots=True)
class EvaluationPeriodReport:
    """Req 14.12/14.13: the Prioritiser's period report plus ground-truth fields."""

    period: int
    start: datetime
    end: datetime
    admitted: int
    backlog_size: int
    active_size: int
    captured_fraud_fraction: Ratio
    fraud_amount_captured: Decimal
    fraud_amount_ingested: Decimal
    detected_events: int


@dataclass(frozen=True, slots=True)
class LineageStats:
    """Req 20.14, per Pattern_Genome lineage (keyed by the root genome id)."""

    lineage_id: str
    genomes: tuple[str, ...]
    campaigns: int  # campaigns with at least one ingested event
    completed_campaigns: int  # marked via mark_campaign_completed (attacker, later)
    detected_campaigns: int  # campaigns with at least one Detected_Event
    detection_rate: Ratio  # detected_campaigns / campaigns
    event_detection_rate: Ratio  # detected campaign events / ingested campaign events
    median_latency_s: Optional[float]  # first event -> first Case admission, detected campaigns


@dataclass(frozen=True, slots=True)
class PatternTypeStats:
    """Req 4.21, per AMLworld pattern type."""

    pattern_type: str
    mapped: bool
    motif: Optional[str]  # HELIX MotifType value when mapped
    patterns: int
    detected_patterns: int  # patterns with at least one member that is a Detected_Event
    detection_rate: Ratio
    # Req 39.6 (design §24): member tx_ids that are Detected_Events / all member tx_ids.
    recall: Ratio = NOT_APPLICABLE
    # FRAUD-labelled Detected_Events / all Detected_Events, over the admitted Cases holding a
    # Motif of the mapped type. None (unavailable) for types that map to no Motif type.
    precision: Optional[Ratio] = None


@dataclass(frozen=True, slots=True)
class Metrics:
    ingested_events: int
    fraud_events_ingested: int
    cases_admitted: int
    detected_events: int
    fraud_events_detected: int
    precision: Ratio  # detected fraud events / detected events
    recall: Ratio  # detected fraud events / ingested fraud events
    alerts_per_hour: Ratio  # admitted Cases / observed stream hours
    latency_median_s: Optional[float]  # fraud event ts -> first admission containing it
    latency_mean_s: Optional[float]
    captured_fraud_fraction: Ratio
    lineages: dict[str, LineageStats] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class DatasetSummary:
    """Req 4.20/4.21: replay summary against the Dataset_Loader's labels."""

    precision: Ratio
    recall: Ratio
    captured_fraud_fraction: Ratio
    admissions_per_period: tuple[tuple[int, int], ...]  # (period, admitted)
    pattern_types: dict[str, PatternTypeStats]
    mapped_types: tuple[str, ...]
    unmapped_types: tuple[str, ...]


class EvaluationMonitor:
    def __init__(self, reader: GroundTruthReader) -> None:
        self._reader = reader
        self._cursor: Optional[datetime] = None
        self._last_len: Optional[int] = None
        self._labels: dict[str, GTView] = {}  # FRAUD records only, merged by event_id
        self._patterns: dict[str, tuple[str, set[str]]] = {}  # pattern_id -> (type, members)
        self._parents: dict[str, Optional[str]] = {}  # genome_id -> parent genome_id
        self._pending: list[tuple[str, Decimal, datetime]] = []
        self._fraud: dict[str, tuple[Decimal, datetime]] = {}  # ingested fraud: tx -> (amount, ts)
        self._ingested = 0
        self._detected_at: dict[str, datetime] = {}
        self._admitted_at: dict[str, datetime] = {}  # case_id -> first admission
        self._completed: set[str] = set()
        self._reports: list[EvaluationPeriodReport] = []
        self._t0: Optional[datetime] = None
        self._t1: Optional[datetime] = None
        self._case_txs: dict[str, set[str]] = {}  # admitted case_id -> reported tx_ids
        self._case_motifs: dict[str, set[str]] = {}  # admitted case_id -> MotifType values

    # ------------------------------------------------------------- pipeline hooks

    def on_event_ingested(self, tx_id: str, amount: Union[Decimal, int, str], ts: datetime) -> None:
        self._ingested += 1
        self._touch(ts)
        self._pending.append((tx_id, Decimal(amount), ts))

    def on_case_admitted(
        self, case_id: str, tx_ids: Iterable[str], ts: datetime, motifs: Iterable[str] = ()
    ) -> None:
        """``motifs``: the Motif type values the Case holds (from its published summary),
        used only for the per-pattern-type precision (Req 39.6)."""
        self._touch(ts)
        self._admitted_at.setdefault(case_id, ts)
        tx_ids = list(tx_ids)
        self._case_txs.setdefault(case_id, set()).update(tx_ids)
        mt = {str(getattr(m, "value", m)) for m in motifs}
        if mt:
            self._case_motifs.setdefault(case_id, set()).update(mt)
        for tx in tx_ids:
            prev = self._detected_at.get(tx)
            if prev is None or ts < prev:
                self._detected_at[tx] = ts

    def on_period(self, report: "PeriodReport") -> EvaluationPeriodReport:
        """Prioritiser period hook. "To date" means stream time before ``report.end``."""
        self.refresh()
        self._touch(report.start)
        self._touch(report.end)
        captured, ingested = self._fraud_amounts(report.end)
        out = EvaluationPeriodReport(
            period=report.period,
            start=report.start,
            end=report.end,
            admitted=report.admitted,
            backlog_size=report.backlog_size,
            active_size=report.active_size,
            captured_fraud_fraction=_ratio(captured, ingested),
            fraud_amount_captured=captured,
            fraud_amount_ingested=ingested,
            detected_events=sum(1 for t in self._detected_at.values() if t < report.end),
        )
        self._reports.append(out)
        return out

    on_period_end = on_period  # design §19 name

    def mark_campaign_completed(self, campaign_id: str) -> None:
        self._completed.add(campaign_id)

    # ------------------------------------------------------------- ground truth

    def refresh(self) -> None:
        """Pull new labels (skipped when the reader's size is unchanged), then classify
        events ingested since the last refresh."""
        n = len(self._reader)
        if n != self._last_len:
            latest = self._cursor
            for v in self._reader.labels_since(self._cursor):
                self._apply(v)
                if v.written_at is not None and (latest is None or v.written_at > latest):
                    latest = v.written_at
            self._cursor, self._last_len = latest, n
        for tx, amt, ts in self._pending:
            if tx in self._labels and tx not in self._fraud:
                self._fraud[tx] = (amt, ts)
        self._pending.clear()

    def _apply(self, v: GTView) -> None:
        if not v.is_fraud:
            self._labels.pop(v.event_id, None)
            self._fraud.pop(v.event_id, None)
            return
        old = self._labels.get(v.event_id)
        if old is not None:  # keep earlier non-null fields (label row, then pattern row)
            v = GTView(
                event_id=v.event_id, label=v.label, source=v.source or old.source,
                written_at=v.written_at or old.written_at,
                campaign_id=v.campaign_id or old.campaign_id, genome_id=v.genome_id or old.genome_id,
                parent_genome_id=v.parent_genome_id or old.parent_genome_id,
                pattern_id=v.pattern_id or old.pattern_id, pattern_type=v.pattern_type or old.pattern_type,
            )
        self._labels[v.event_id] = v
        if v.pattern_id is not None:
            self._patterns.setdefault(v.pattern_id, ((v.pattern_type or "UNKNOWN").upper(), set()))[1].add(
                v.event_id
            )
        if v.genome_id is not None and (v.parent_genome_id is not None or v.genome_id not in self._parents):
            self._parents[v.genome_id] = v.parent_genome_id

    # ------------------------------------------------------------- reports

    @property
    def reports(self) -> list[EvaluationPeriodReport]:
        return list(self._reports)

    def detected_events(self) -> frozenset[str]:
        return frozenset(self._detected_at)

    def captured_fraud_fraction(self, as_of: Optional[datetime] = None) -> Ratio:
        self.refresh()
        return _ratio(*self._fraud_amounts(as_of))

    def _fraud_amounts(self, as_of: Optional[datetime], source: Optional[str] = None) -> tuple[Decimal, Decimal]:
        captured = ingested = _ZERO
        for tx, (amt, ts) in self._fraud.items():
            if (as_of is not None and ts >= as_of) or (source is not None and self._labels[tx].source != source):
                continue
            ingested += amt
            d = self._detected_at.get(tx)
            if d is not None and (as_of is None or d < as_of):
                captured += amt
        return captured, ingested

    def metrics(self) -> Metrics:
        self.refresh()
        detected_fraud_any = sum(1 for tx in self._detected_at if tx in self._labels)
        detected_fraud = [tx for tx in self._fraud if tx in self._detected_at]
        lat = [max(0.0, (self._detected_at[tx] - self._fraud[tx][1]).total_seconds()) for tx in detected_fraud]
        hours = (self._t1 - self._t0).total_seconds() / 3600 if self._t0 is not None and self._t1 is not None else 0
        return Metrics(
            ingested_events=self._ingested,
            fraud_events_ingested=len(self._fraud),
            cases_admitted=len(self._admitted_at),
            detected_events=len(self._detected_at),
            fraud_events_detected=len(detected_fraud),
            precision=_ratio(detected_fraud_any, len(self._detected_at)),
            recall=_ratio(len(detected_fraud), len(self._fraud)),
            alerts_per_hour=NOT_APPLICABLE if hours <= 0 else len(self._admitted_at) / hours,
            latency_median_s=statistics.median(lat) if lat else None,
            latency_mean_s=statistics.fmean(lat) if lat else None,
            captured_fraud_fraction=_ratio(*self._fraud_amounts(None)),
            lineages=self._lineages(),
        )

    def _root(self, genome: str) -> str:
        seen = {genome}
        while (p := self._parents.get(genome)) is not None and p not in seen:
            seen.add(p)
            genome = p
        return genome

    def _lineages(self) -> dict[str, LineageStats]:
        # campaign -> (genome, [ingested txs])
        camps: dict[str, tuple[str, list[str]]] = {}
        for tx in self._fraud:
            v = self._labels[tx]
            if v.campaign_id is None or v.genome_id is None:
                continue
            camps.setdefault(v.campaign_id, (v.genome_id, []))[1].append(tx)
        by_lineage: dict[str, list[str]] = {}
        for cid, (genome, _) in camps.items():
            by_lineage.setdefault(self._root(genome), []).append(cid)
        out: dict[str, LineageStats] = {}
        for root, cids in sorted(by_lineage.items()):
            detected = 0
            ev_total = ev_detected = 0
            lat: list[float] = []
            for cid in cids:
                txs = camps[cid][1]
                ev_total += len(txs)
                hits = [self._detected_at[t] for t in txs if t in self._detected_at]
                ev_detected += len(hits)
                if hits:
                    detected += 1
                    first_ev = min(self._fraud[t][1] for t in txs)
                    lat.append(max(0.0, (min(hits) - first_ev).total_seconds()))
            out[root] = LineageStats(
                lineage_id=root,
                genomes=tuple(sorted({camps[c][0] for c in cids})),
                campaigns=len(cids),
                completed_campaigns=sum(1 for c in cids if c in self._completed),
                detected_campaigns=detected,
                detection_rate=_ratio(detected, len(cids)),
                event_detection_rate=_ratio(ev_detected, ev_total),
                median_latency_s=statistics.median(lat) if lat else None,
            )
        return out

    def pattern_type_stats(self, source: Optional[str] = None) -> dict[str, PatternTypeStats]:
        """Per AMLworld pattern type (Req 4.21, 39.6). ``source`` restricts the FRAUD labels
        counted by the precision (None: any source)."""
        self.refresh()
        acc: dict[str, list[int]] = {}
        members_of: dict[str, set[str]] = {}
        for ptype, members in self._patterns.values():
            c = acc.setdefault(ptype, [0, 0])
            c[0] += 1
            c[1] += any(m in self._detected_at for m in members)
            members_of.setdefault(ptype, set()).update(members)
        out: dict[str, PatternTypeStats] = {}
        for ptype, (n, d) in sorted(acc.items()):
            motif = motif_for(ptype)
            mem = members_of[ptype]
            precision: Optional[Ratio] = None
            if motif is not None:
                txs: set[str] = set()
                for cid, kinds in self._case_motifs.items():
                    if motif.value in kinds:
                        txs |= self._case_txs.get(cid, set())
                fraud = sum(
                    1 for tx in txs
                    if tx in self._labels and (source is None or self._labels[tx].source == source)
                )
                precision = _ratio(fraud, len(txs))
            out[ptype] = PatternTypeStats(
                pattern_type=ptype, mapped=motif is not None, motif=None if motif is None else motif.value,
                patterns=n, detected_patterns=d, detection_rate=_ratio(d, n),
                recall=_ratio(sum(1 for m in mem if m in self._detected_at), len(mem)), precision=precision,
            )
        return out

    def dataset_summary(self, source: Optional[str] = "LOADER") -> DatasetSummary:
        """Req 4.20/4.21. Fraud means a FRAUD label from ``source`` (None: any source);
        precision's denominator is still every Detected_Event."""
        self.refresh()
        types = self.pattern_type_stats(source)
        fraud = [tx for tx in self._fraud if source is None or self._labels[tx].source == source]
        hit = sum(1 for tx in fraud if tx in self._detected_at)
        detected_fraud_any = sum(
            1 for tx in self._detected_at
            if tx in self._labels and (source is None or self._labels[tx].source == source)
        )
        return DatasetSummary(
            precision=_ratio(detected_fraud_any, len(self._detected_at)),
            recall=_ratio(hit, len(fraud)),
            captured_fraud_fraction=_ratio(*self._fraud_amounts(None, source)),
            admissions_per_period=tuple((r.period, r.admitted) for r in self._reports),
            pattern_types=types,
            mapped_types=tuple(t for t, s in types.items() if s.mapped),
            unmapped_types=tuple(t for t, s in types.items() if not s.mapped),
        )

    aml_report = dataset_summary  # design §3a name

    def _touch(self, ts: datetime) -> None:
        if self._t0 is None or ts < self._t0:
            self._t0 = ts
        if self._t1 is None or ts > self._t1:
            self._t1 = ts
