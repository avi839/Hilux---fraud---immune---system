"""Purple_Team_Exercise and Exercise_Scorecard (Req 37.1-37.4).

Design decisions:
- Isolation (Req 37.1, 37.2). ``isolate(live)`` builds a separate ``Helix`` (no Storage, no
  publisher, no Feedback_Learner, its own ``ManualClock`` starting at the live stream time and an
  empty Case_Manager) whose detection state is a *copy* of the live one:

  =========================  ============================================================
  live state                 copy
  =========================  ============================================================
  Behavioural_DNA            ``copy.deepcopy(helix.profiles)``
  Change_Point_Detector      ``copy.deepcopy(helix.cpd)`` (regime-shift hook rebound to the copy)
  Temporal_Graph             ``copy.deepcopy(helix.graph)``; Motif_Detector state likewise
  auth history               ``copy.deepcopy(helix.auth)``; the copied Auth_Sensor reads it
  Sensor_Agents              deep copies (Resilience_Mode wrappers are removed)
  Sensor_Weights             new dicts (``helix.weights``, motif weights, antibody / rule weights)
  Antibody_Pool              ``ImmuneSystem.from_dict(live.to_dict())`` on the copy's clock
  Detection_Rules            a new ``RuleEngine`` holding deep copies of the active versions
  Indicator_Store            ``isolate_component(store)``: ``store.copy()`` when available,
                             else ``type(store).from_snapshot(store.snapshot())``, else deepcopy
  =========================  ============================================================

  The Indicator_Store protocol (``IsolatableStore``) is defined here because the store does not
  exist yet: an object with ``copy()`` returning an independent store is enough. The copy is
  exposed as ``isolated.helix.indicators``; ``installers`` (callables ``(helix_copy,
  indicators_copy)``) let the integrator wire it (and anything else) into the copied pipeline.
  Read-only callables (the Canary predicate, the sensor timing clock) are shared, never mutable
  state. Nothing is written to Storage, to any label writer or to the live Cases.
- Campaigns. An exercise is a list of campaign specs: a Pattern_Genome type with optional
  parameter overrides (validated against the genome bounds) or the id of a live attacker genome,
  a seed and a repeat count. Events come from the attacker's own generators
  (``helix.attacker.campaigns.generate``) with one shared ``TargetPool`` over the copy's DNA
  accounts. Optional benign background traffic comes from a fresh Synthetic_Generator whose
  labels go to a throwaway in-memory writer.
- Scoring without the Ground_Truth_Store (Property 70). The exercise knows which events it
  generated, exactly like the attacker knows its own campaign events; it never reads stored
  labels. A campaign event is detected when it is in a Case of the copy that is (or was)
  admitted to the Active_Queue, at the copy's stream time of that admission (the
  Evaluation_Monitor's Detected_Event rule). Scorecard metrics:
  ``detection_rate`` = detected campaign Transaction_Events / campaign Transaction_Events;
  ``campaign_detection_rate`` = campaigns with >= 1 detected event / campaigns;
  ``median_detection_latency_s`` = median over detected campaigns of first detection minus first
  event ts; ``alert_volume`` = Cases admitted to the Active_Queue in the copy;
  ``false_positives`` = admitted Cases holding no campaign event (only background traffic can
  produce them, so this is 0 without background events). Per HELIX_Technique (every catalogue
  technique, so two scorecards always compare row by row): the deployed contributors of the copy
  (Coverage_Matrix), ``covered``, campaigns, events and the same rates / latency for campaigns
  whose genome type maps to the technique (``genome_techniques``).
- Storage (Req 37.4). Scorecards are kept in memory and, with a Storage, in the DuckDB table
  ``soc_scorecards`` (JSON payload; created here, reloaded on start). ``compare(a, b)`` returns
  ``{a, b, delta = b - a}`` for every metric and every technique field (``changed`` for flags).
"""

from __future__ import annotations

import copy
import json
import logging
import math
import statistics
import threading
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from types import MappingProxyType
from typing import Any, Optional, Protocol, runtime_checkable

import numpy as np

from helix.attacker.campaigns import CampaignContext, TargetPool, generate
from helix.attacker.genome import BOUNDS, PatternGenome, PatternType, default_params
from helix.cases import Placement
from helix.codec import AuthEvent, TransactionEvent
from helix.core import Helix
from helix.replay.engine import DefaultStage1Engine
from helix.sim.clock import ManualClock

from .console import storage_connection
from .coverage import pipeline_coverage
from .resilience import FaultySensor
from .techniques import default_catalogue, genome_techniques

log = logging.getLogger(__name__)

MAX_CAMPAIGNS = 50  # campaigns per exercise (after repeat counts)
MAX_EVENTS_PER_CAMPAIGN = 500
MAX_BACKGROUND = 5_000
DEFAULT_GAP_S = 600.0
SCORECARDS_TABLE = "soc_scorecards"
_SCORECARD_DDL = (f"CREATE TABLE IF NOT EXISTS {SCORECARDS_TABLE} (scorecard_id VARCHAR PRIMARY KEY, "
                  "seq BIGINT NOT NULL, created_at VARCHAR NOT NULL, payload VARCHAR NOT NULL)")

METRIC_KEYS = (
    "campaigns", "detected_campaigns", "campaign_detection_rate", "events", "detected_events",
    "detection_rate", "median_detection_latency_s", "alert_volume", "false_positives", "background_events",
    "cases_created",
)
TECHNIQUE_KEYS = (
    "covered", "contributors", "campaigns", "detected_campaigns", "events", "detected_events", "detection_rate",
    "campaign_detection_rate", "median_detection_latency_s",
)


# --- spec ------------------------------------------------------------------------------------------


class ExerciseSpecError(ValueError):
    def __init__(self, problems: Sequence[tuple[str, str]]) -> None:
        self.problems = tuple(problems)
        super().__init__("; ".join(f"{f}: {r}" for f, r in self.problems) or "invalid exercise")


@dataclass(frozen=True, slots=True)
class CampaignSpec:
    ptype: PatternType
    params: Mapping[str, Any] = field(default_factory=dict)  # overrides of the default genome
    seed: int = 0
    count: int = 1
    genome_id: Optional[str] = None  # a live attacker genome (its parameters were resolved)

    def genome(self, gid: str) -> PatternGenome:
        return PatternGenome(gid, self.ptype, {**default_params(self.ptype), **dict(self.params)}, None, 0)

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {"ptype": self.ptype.value, "params": dict(self.params), "seed": self.seed,
                             "count": self.count}
        if self.genome_id is not None:
            d["genome_id"] = self.genome_id
        return d


@dataclass(frozen=True, slots=True)
class ExerciseSpec:
    campaigns: tuple[CampaignSpec, ...]
    name: str = ""
    background_events: int = 0
    seed: int = 0
    gap_s: float = DEFAULT_GAP_S  # stream-time gap between campaign starts

    def to_dict(self) -> dict[str, Any]:
        return {"name": self.name, "campaigns": [c.to_dict() for c in self.campaigns],
                "background_events": self.background_events, "seed": self.seed, "gap_s": self.gap_s}

    @property
    def total_campaigns(self) -> int:
        return sum(c.count for c in self.campaigns)


def _is_int(v: Any) -> bool:
    return isinstance(v, int) and not isinstance(v, bool)


def parse_spec(doc: Any, attacker: Any = None) -> ExerciseSpec:
    """``ExerciseSpec`` from a JSON document; raises ``ExerciseSpecError`` naming every problem.

    ``{"name": str, "campaigns": [{"ptype": "FAN_OUT", "params": {...}, "seed": 0, "count": 1}
    | {"genome_id": "G..."}], "background_events": 0, "seed": 0, "gap_s": 600}``"""
    probs: list[tuple[str, str]] = []
    if not isinstance(doc, Mapping):
        raise ExerciseSpecError([("$", "body must be a JSON object")])
    name = doc.get("name", "")
    if not isinstance(name, str) or len(name) > 120:
        probs.append(("name", "must be a string of at most 120 characters"))
        name = ""
    raw = doc.get("campaigns")
    if not isinstance(raw, list) or not raw:
        probs.append(("campaigns", "must be a non-empty list"))
        raw = []
    out: list[CampaignSpec] = []
    for i, c in enumerate(raw):
        at = f"campaigns[{i}]"
        if not isinstance(c, Mapping):
            probs.append((at, "must be an object"))
            continue
        seed, count = c.get("seed", 0), c.get("count", 1)
        if not _is_int(seed) or not 0 <= seed < 2**32:
            probs.append((f"{at}.seed", "must be an integer in [0, 2^32)"))
            seed = 0
        if not _is_int(count) or not 1 <= count <= MAX_CAMPAIGNS:
            probs.append((f"{at}.count", f"must be an integer in [1, {MAX_CAMPAIGNS}]"))
            count = 1
        gid = c.get("genome_id")
        if gid is not None:
            if not isinstance(gid, str):
                probs.append((f"{at}.genome_id", "must be a string"))
                continue
            try:
                g = attacker.genome(gid) if attacker is not None else None
            except (KeyError, LookupError):
                g = None
            if g is None:
                probs.append((f"{at}.genome_id", f"unknown attacker genome {gid!r}"))
                continue
            out.append(CampaignSpec(g.ptype, dict(g.params), seed, count, gid))
            continue
        try:
            ptype = PatternType(c.get("ptype"))
        except ValueError:
            probs.append((f"{at}.ptype", f"must be one of {sorted({p.value for p in PatternType})}"))
            continue
        params = c.get("params", {})
        if not isinstance(params, Mapping):
            probs.append((f"{at}.params", "must be an object"))
            continue
        spec = BOUNDS[ptype]
        bad = False
        for k, v in params.items():
            if k not in spec:
                probs.append((f"{at}.params.{k}", f"unknown parameter for {ptype.value}"))
                bad = True
            elif not spec[k].contains(v):
                probs.append((f"{at}.params.{k}", f"must be {'an integer' if spec[k].kind == 'int' else 'a number'} "
                                                  f"in [{spec[k].lo:g}, {spec[k].hi:g}]"))
                bad = True
        if not bad:
            out.append(CampaignSpec(ptype, dict(params), seed, count))
    bg = doc.get("background_events", 0)
    if not _is_int(bg) or not 0 <= bg <= MAX_BACKGROUND:
        probs.append(("background_events", f"must be an integer in [0, {MAX_BACKGROUND}]"))
        bg = 0
    seed = doc.get("seed", 0)
    if not _is_int(seed) or not 0 <= seed < 2**32:
        probs.append(("seed", "must be an integer in [0, 2^32)"))
        seed = 0
    gap = doc.get("gap_s", DEFAULT_GAP_S)
    if isinstance(gap, bool) or not isinstance(gap, (int, float)) or not math.isfinite(gap) or not 0 <= gap <= 7 * 86_400:
        probs.append(("gap_s", "must be a number of seconds in [0, 604800]"))
        gap = DEFAULT_GAP_S
    total = sum(c.count for c in out)
    if total > MAX_CAMPAIGNS:
        probs.append(("campaigns", f"at most {MAX_CAMPAIGNS} campaigns per exercise (got {total})"))
    if probs:
        raise ExerciseSpecError(probs)
    return ExerciseSpec(tuple(out), name, bg, seed, float(gap))


# --- isolation ---------------------------------------------------------------------------------------


@runtime_checkable
class IsolatableStore(Protocol):
    """Minimal protocol for the Indicator_Store (and any other store a copy needs)."""

    def copy(self) -> Any: ...


def isolate_component(obj: Any) -> Any:
    """An independent copy of a stateful component: ``obj.copy()`` (not for plain containers),
    else ``type(obj).from_snapshot(obj.snapshot())``, else ``copy.deepcopy(obj)``."""
    if obj is None:
        return None
    if not isinstance(obj, (dict, list, set)) and callable(getattr(obj, "copy", None)):
        out = obj.copy()
        if out is obj:
            raise ValueError(f"{type(obj).__name__}.copy() returned the same object")
        return out
    snap = getattr(obj, "snapshot", None)
    restore = getattr(type(obj), "from_snapshot", None)
    if callable(snap) and callable(restore):
        return restore(snap())
    return copy.deepcopy(obj)


Installer = Callable[[Any, Any], None]


@dataclass
class Isolated:
    helix: Helix
    indicators: Any = None


def _unwrap(s: Any) -> Any:
    while isinstance(s, FaultySensor):
        s = s.inner
    return s


def _copy_rules(live: Any) -> Any:
    from helix.soc.rules import RuleEngine

    eng = RuleEngine(live.catalogue, backtest_window_s=live.backtest_window_s, half_life_days=live.half_life_days,
                     weights=dict(live.weights), clock=live.clock)
    active = {}
    for rid in live.rule_ids():
        v = live.active_version(rid)
        if v is not None:
            active[rid] = copy.deepcopy(live.get(rid, v))
    eng._versions = {rid: [rv] for rid, rv in active.items()}
    eng._active = MappingProxyType(active)
    return eng


def isolate(live: Any, *, indicators: Any = None, installers: Iterable[Installer] = ()) -> Isolated:
    """An isolated copy of the live detection state (see module docstring)."""
    cfg = live.cfg
    clock = ManualClock(live.clock.now())
    memo: dict[int, Any] = {}
    auth = copy.deepcopy(live.auth, memo) if getattr(live, "auth", None) is not None else None
    profiles = copy.deepcopy(live.profiles, memo)
    cpd = copy.deepcopy(live.cpd, memo)
    sensors = [copy.deepcopy(_unwrap(s), memo) for s in live.stage1.sensors]
    graph = copy.deepcopy(live.graph, memo)
    motifs = copy.deepcopy(live.motifs, memo)
    sensor_clock = getattr(live.stage1, "clock", None)
    kw = {"sensor_clock": sensor_clock} if sensor_clock is not None else {}
    cp = Helix(cfg, clock, sensors=sensors, motif_engine=motifs, weights=dict(live.weights), **kw)
    cp.auth = auth
    eng = DefaultStage1Engine(cfg, profiles=profiles, cpd=cpd, sensors=sensors,
                              **({"clock": sensor_clock} if sensor_clock is not None else {}))
    eng.weights = cp.weights  # shared dict, as in the live pipeline
    cp.stage1, cp.profiles, cp.cpd, cp.graph = eng, profiles, cpd, graph

    imm = getattr(live, "immune", None)
    if imm is not None:
        from helix.immune import ImmuneSystem, attach_immune

        clone = ImmuneSystem.from_dict(imm.to_dict(), imm.cfg, clock, weights=dict(imm.weights),
                                       is_canary=imm.is_canary, feature_cache_size=imm.feature_cache_size)
        attach_immune(cp, clone)
    rules = getattr(live, "rules", None)
    if rules is not None and cp.soc_enabled:
        cp.rules = _copy_rules(rules)
    ind = isolate_component(indicators)
    cp.indicators = ind
    for inst in installers:
        inst(cp, ind)
    return Isolated(cp, ind)


def live_fingerprint(helix: Any) -> str:
    """Canonical JSON of the live state an exercise must not change: Cases, Sensor_Weights
    (pipeline, learner, motif, antibody, rule), Antibody_Pool, Behavioural_DNA / CPD, graph,
    active rules, clock and counters. Equal strings mean equal state (for the isolation check)."""
    dna, cpd = helix.snapshot_state()
    cases = []
    for cid in sorted(helix.cases.cases):
        c = helix.cases.cases[cid]
        cases.append([cid, str(c.status), str(c.tier), c.risk, str(c.priority), sorted(c.tx_ids), c.assignee,
                      str(c.placement), len(c.history), None if c.label is None else str(c.label)])
    learner = getattr(helix, "learner", None)
    imm = getattr(helix, "immune", None)
    rules = getattr(helix, "rules", None)
    doc = {
        "seq_no": helix.seq_no, "clock": helix.clock.now().isoformat(), "dna": dna, "cpd": cpd,
        "weights": dict(sorted(helix.weights.items())),
        "motif_weights": {str(k): v for k, v in sorted(helix.motifs.weights.items(), key=lambda kv: str(kv[0]))},
        "learner": None if learner is None else {
            "sensor": dict(sorted(learner.sensor_weights.items())),
            "other": dict(sorted(learner.other_weights.items()))},
        "immune": None if imm is None else imm.to_dict(),
        "immune_weights": None if imm is None else dict(sorted(imm.weights.items())),
        "rules": None if rules is None else {"active": {r: rules.active_version(r) for r in rules.rule_ids()},
                                             "hits": rules.hits, "weights": dict(sorted(rules.weights.items()))},
        "cases": cases, "chains": sorted(helix.chains),
        "graph": [[e.tx_id, e.src, e.dst, e.ts.isoformat(), str(e.amount)] for e in helix.graph.edges()],
    }
    return json.dumps(doc, sort_keys=True, default=str)


# --- the exercise -----------------------------------------------------------------------------------------


@dataclass
class _Campaign:
    campaign_id: str
    ptype: PatternType
    techniques: tuple[str, ...]
    genome_id: Optional[str]
    tx: list[TransactionEvent]
    auth: int


def _median(xs: list[float]) -> Optional[float]:
    return statistics.median(xs) if xs else None


def _ratio(a: int, b: int) -> Optional[float]:
    return a / b if b else None


@dataclass(frozen=True)
class Scorecard:
    scorecard_id: str
    seq: int
    name: str
    created_at: datetime  # wall clock
    stream_start: datetime
    spec: Mapping[str, Any]
    catalogue_version: str
    metrics: Mapping[str, Any]
    techniques: Mapping[str, Mapping[str, Any]]  # technique id -> row
    campaigns: tuple[Mapping[str, Any], ...]
    notes: tuple[str, ...] = ()

    def summary(self) -> dict[str, Any]:
        return {"scorecard_id": self.scorecard_id, "name": self.name, "created_at": self.created_at.isoformat(),
                "metrics": dict(self.metrics)}

    def to_dict(self) -> dict[str, Any]:
        return {"scorecard_id": self.scorecard_id, "seq": self.seq, "name": self.name,
                "created_at": self.created_at.isoformat(), "stream_start": self.stream_start.isoformat(),
                "spec": dict(self.spec), "catalogue_version": self.catalogue_version, "metrics": dict(self.metrics),
                "techniques": [dict(v, technique_id=k) for k, v in self.techniques.items()],
                "campaigns": [dict(c) for c in self.campaigns], "notes": list(self.notes)}

    @classmethod
    def from_dict(cls, d: Mapping[str, Any]) -> Scorecard:
        techs = {t["technique_id"]: {k: v for k, v in t.items() if k != "technique_id"} for t in d["techniques"]}
        return cls(d["scorecard_id"], int(d["seq"]), d["name"], datetime.fromisoformat(d["created_at"]),
                   datetime.fromisoformat(d["stream_start"]), d["spec"], d["catalogue_version"], d["metrics"],
                   techs, tuple(d["campaigns"]), tuple(d.get("notes", ())))


class Exercise:
    """A prepared Purple_Team_Exercise: the isolated copy plus the generated events.
    ``execute()`` touches only the copy, so it may run off the event loop."""

    def __init__(self, scorecard_id: str, seq: int, spec: ExerciseSpec, iso: Isolated,
                 campaigns: list[_Campaign], background: list[TransactionEvent], auth_events: list[Any],
                 start: datetime, wall_clock: Callable[[], datetime]) -> None:
        self.scorecard_id, self.seq, self.spec, self.iso = scorecard_id, seq, spec, iso
        self.campaigns, self.background, self.auth_events = campaigns, background, auth_events
        self.start, self.wall_clock = start, wall_clock

    def events(self) -> list[Any]:
        items: list[tuple[datetime, int, int, Any]] = []
        n = 0
        for a in self.auth_events:
            items.append((a.ts, 0, n, a))
            n += 1
        for c in self.campaigns:
            for e in c.tx:
                items.append((e.ts, 1, n, e))
                n += 1
        for e in self.background:
            items.append((e.ts, 1, n, e))
            n += 1
        items.sort(key=lambda x: (x[0], x[1], x[2]))
        return [x[3] for x in items]

    def execute(self) -> Scorecard:
        cp = self.iso.helix
        detected_at: dict[str, datetime] = {}
        admitted: set[str] = set()
        created: set[str] = set()
        for ev in self.events():
            out = cp.process(ev)
            for ch in getattr(out, "case_changes", ()):
                created.add(ch.case_id)
                if ch.placement is not Placement.ACTIVE and ch.case_id not in admitted:
                    continue
                case = cp.cases.cases.get(ch.case_id)
                if case is None:
                    continue
                admitted.add(ch.case_id)
                now = cp.clock.now()
                for tx in case.tx_ids:
                    detected_at.setdefault(tx, now)
        return self._score(detected_at, admitted, created)

    def _score(self, detected_at: dict[str, datetime], admitted: set[str], created: set[str]) -> Scorecard:
        cp = self.iso.helix
        attack_tx = {e.tx_id for c in self.campaigns for e in c.tx}
        per_campaign = []
        tech: dict[str, dict[str, Any]] = {}
        for c in self.campaigns:
            hits = [detected_at[e.tx_id] for e in c.tx if e.tx_id in detected_at]
            first = min((e.ts for e in c.tx), default=None)
            lat = max(0.0, (min(hits) - first).total_seconds()) if hits and first is not None else None
            per_campaign.append({"campaign_id": c.campaign_id, "ptype": c.ptype.value, "genome_id": c.genome_id,
                                 "techniques": list(c.techniques), "events": len(c.tx), "auth_events": c.auth,
                                 "detected_events": len(hits), "detected": bool(hits),
                                 "first_event_ts": None if first is None else first.isoformat(),
                                 "first_detected_at": min(hits).isoformat() if hits else None,
                                 "detection_latency_s": lat})
            for t in c.techniques:
                acc = tech.setdefault(t, {"campaigns": 0, "detected_campaigns": 0, "events": 0,
                                          "detected_events": 0, "lat": []})
                acc["campaigns"] += 1
                acc["detected_campaigns"] += bool(hits)
                acc["events"] += len(c.tx)
                acc["detected_events"] += len(hits)
                if lat is not None:
                    acc["lat"].append(lat)
        events = sum(r["events"] for r in per_campaign)
        det = sum(r["detected_events"] for r in per_campaign)
        dc = sum(1 for r in per_campaign if r["detected"])
        lats = [r["detection_latency_s"] for r in per_campaign if r["detection_latency_s"] is not None]
        fp = 0
        for cid in admitted:
            case = cp.cases.cases.get(cid)
            if case is not None and case.merged_into is None and not (case.tx_ids & attack_tx):
                fp += 1
        metrics = {
            "campaigns": len(per_campaign), "detected_campaigns": dc,
            "campaign_detection_rate": _ratio(dc, len(per_campaign)),
            "events": events, "detected_events": det, "detection_rate": _ratio(det, events),
            "median_detection_latency_s": _median(lats),
            "alert_volume": len(admitted), "false_positives": fp,
            "background_events": len(self.background), "cases_created": len(created),
        }
        cov = pipeline_coverage(cp, rules=getattr(cp, "rules", None), immune=getattr(cp, "immune", None))
        rows: dict[str, dict[str, Any]] = {}
        for r in cov.rows:
            acc = tech.get(r.technique_id)
            rows[r.technique_id] = {
                "name": r.name, "domain": r.domain, "covered": not r.gap,
                "contributors": [d.contributor_id for d in r.contributors],
                "campaigns": 0 if acc is None else acc["campaigns"],
                "detected_campaigns": 0 if acc is None else acc["detected_campaigns"],
                "events": 0 if acc is None else acc["events"],
                "detected_events": 0 if acc is None else acc["detected_events"],
                "detection_rate": None if acc is None else _ratio(acc["detected_events"], acc["events"]),
                "campaign_detection_rate": None if acc is None else _ratio(acc["detected_campaigns"], acc["campaigns"]),
                "median_detection_latency_s": None if acc is None else _median(acc["lat"]),
            }
        notes = ("Scored from the exercise's own campaign events in an isolated copy; no stored labels are read.",)
        return Scorecard(self.scorecard_id, self.seq, self.spec.name or self.scorecard_id, self.wall_clock(),
                         self.start, self.spec.to_dict(), cov.catalogue_version, metrics, rows,
                         tuple(per_campaign), notes)


# --- scorecard store and comparison ---------------------------------------------------------------------


class ScorecardStore:
    """Exercise_Scorecards in memory, optionally persisted to DuckDB (``soc_scorecards``)."""

    def __init__(self, storage: Any = None) -> None:
        self.storage = storage
        self.storage_errors = 0
        self._cards: dict[str, Scorecard] = {}
        self._lock = threading.Lock()
        if storage is not None:
            try:
                with storage_connection(storage) as con:
                    con.execute(_SCORECARD_DDL)
                    rows = con.execute(f"SELECT payload FROM {SCORECARDS_TABLE} ORDER BY seq").fetchall()
                for (payload,) in rows:
                    sc = Scorecard.from_dict(json.loads(payload))
                    self._cards[sc.scorecard_id] = sc
            except Exception:  # noqa: BLE001
                self.storage_errors += 1
                log.exception("scorecards: load failed; continuing in memory")

    def next_seq(self) -> int:
        with self._lock:
            return max((c.seq for c in self._cards.values()), default=0) + 1

    def save(self, sc: Scorecard) -> Scorecard:
        with self._lock:
            if sc.scorecard_id in self._cards:
                raise ValueError(f"scorecard {sc.scorecard_id!r} already stored")
            self._cards[sc.scorecard_id] = sc
        if self.storage is not None:
            try:
                with storage_connection(self.storage) as con:
                    con.execute(_SCORECARD_DDL)
                    con.execute(f"INSERT INTO {SCORECARDS_TABLE} VALUES (?, ?, ?, ?)",
                                [sc.scorecard_id, sc.seq, sc.created_at.isoformat(), json.dumps(sc.to_dict())])
            except Exception:  # noqa: BLE001
                self.storage_errors += 1
                log.exception("scorecards: write of %s failed; kept in memory", sc.scorecard_id)
        return sc

    def get(self, scorecard_id: str) -> Scorecard:
        return self._cards[scorecard_id]  # KeyError for an unknown id

    def list(self) -> list[Scorecard]:
        """Newest first."""
        return sorted(self._cards.values(), key=lambda c: c.seq, reverse=True)

    def __len__(self) -> int:
        return len(self._cards)


def _num(v: Any) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def _diff(a: Any, b: Any) -> dict[str, Any]:
    if isinstance(a, bool) or isinstance(b, bool):
        return {"a": a, "b": b, "changed": a != b}
    if isinstance(a, list) or isinstance(b, list):
        sa, sb = set(a or ()), set(b or ())
        return {"a": len(sa), "b": len(sb), "delta": len(sb) - len(sa), "added": sorted(sb - sa),
                "removed": sorted(sa - sb)}
    return {"a": a, "b": b, "delta": (b - a) if _num(a) and _num(b) else None}


def compare(a: Scorecard, b: Scorecard) -> dict[str, Any]:
    """Per-metric and per-technique difference ``b - a`` (Req 37.4)."""
    techs = sorted(set(a.techniques) | set(b.techniques))
    out_t = {}
    for t in techs:
        ra, rb = a.techniques.get(t, {}), b.techniques.get(t, {})
        out_t[t] = {k: _diff(ra.get(k), rb.get(k)) for k in TECHNIQUE_KEYS}
    return {"a": a.scorecard_id, "b": b.scorecard_id,
            "metrics": {k: _diff(a.metrics.get(k), b.metrics.get(k)) for k in METRIC_KEYS},
            "techniques": out_t}


# --- service --------------------------------------------------------------------------------------------


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class PurpleTeam:
    """Runs Purple_Team_Exercises against isolated copies of ``helix`` and stores the scorecards.

    ``indicators``: the live Indicator_Store, or a zero-argument callable returning it (it may be
    attached later). ``attacker``: optional live Attacker_Agent for ``genome_id`` campaigns."""

    def __init__(self, helix: Any, *, store: Optional[ScorecardStore] = None, storage: Any = None,
                 indicators: Any = None, installers: Sequence[Installer] = (), attacker: Any = None,
                 wall_clock: Callable[[], datetime] = _utcnow) -> None:
        self.helix = helix
        self.store = store if store is not None else ScorecardStore(storage)
        self.indicators = indicators
        self.installers = list(installers)
        self.attacker = attacker
        self.wall_clock = wall_clock
        self._lock = threading.Lock()

    def parse(self, doc: Any) -> ExerciseSpec:
        att = self.attacker() if callable(self.attacker) and not hasattr(self.attacker, "genome") else self.attacker
        return parse_spec(doc, att)

    def _indicators(self) -> Any:
        ind = self.indicators
        return ind() if callable(ind) and not isinstance(ind, IsolatableStore) else ind

    def prepare(self, spec: ExerciseSpec) -> Exercise:
        """Copy the live state and generate the events (call where the live pipeline is not
        processing concurrently, e.g. on the event loop)."""
        with self._lock:
            seq = self.store.next_seq()
            sid = f"SC-{seq}"
            while True:
                try:
                    self.store.get(sid)
                except KeyError:
                    break
                seq += 1
                sid = f"SC-{seq}"
            iso = isolate(self.helix, indicators=self._indicators(), installers=self.installers)
        cp = iso.helix
        cfg = cp.cfg
        start = cp.clock.now() + timedelta(seconds=1)
        pool = TargetPool(sorted(cp.profiles.accounts()))
        rt = Decimal(str(getattr(getattr(cfg, "graph", None), "reporting_threshold", 10_000)))
        max_events = min(int(cfg.attacker.max_events), MAX_EVENTS_PER_CAMPAIGN)
        camps: list[_Campaign] = []
        auth: list[Any] = []
        i = 0
        for cs in spec.campaigns:
            for k in range(cs.count):
                cid = f"{sid}-c{i + 1}"
                g = cs.genome(f"{sid}-g{i + 1}")
                ctx = CampaignContext(rng=np.random.default_rng([spec.seed, cs.seed, k, i]),
                                      start=start + timedelta(seconds=spec.gap_s * i), targets=pool,
                                      id_prefix=f"purple:{sid}:{cid}", reporting_threshold=rt,
                                      max_events=max_events, auth=bool(cp.soc_enabled))
                evs = generate(g, ctx)
                tx = [e for e in evs if isinstance(e, TransactionEvent)]
                au = [e for e in evs if isinstance(e, AuthEvent)]
                auth += au
                camps.append(_Campaign(cid, cs.ptype, genome_techniques(cs.ptype), cs.genome_id, tx, len(au)))
                i += 1
        background: list[TransactionEvent] = []
        if spec.background_events:
            from helix.sim.generator import SyntheticGenerator
            from helix.sim.ground_truth import InMemoryGroundTruth

            gen = SyntheticGenerator(cfg.generator, ManualClock(start), InMemoryGroundTruth(), seed=spec.seed)
            background = [next(gen) for _ in range(spec.background_events)]
        return Exercise(sid, seq, spec, iso, camps, background, auth, start, self.wall_clock)

    def run(self, spec: ExerciseSpec) -> Scorecard:
        """Prepare, execute and store one exercise."""
        return self.store.save(self.prepare(spec).execute())

    def compare(self, a: str, b: str) -> dict[str, Any]:
        return compare(self.store.get(a), self.store.get(b))


__all__ = [
    "CampaignSpec",
    "Exercise",
    "ExerciseSpec",
    "ExerciseSpecError",
    "IsolatableStore",
    "Isolated",
    "METRIC_KEYS",
    "PurpleTeam",
    "Scorecard",
    "ScorecardStore",
    "TECHNIQUE_KEYS",
    "compare",
    "isolate",
    "isolate_component",
    "live_fingerprint",
    "parse_spec",
]
