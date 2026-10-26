"""Response_Playbooks and the Playbook_Runner (Req 32, 27.11).

Design decisions:
- A Response_Playbook is a small declarative YAML document (``yaml.safe_load`` only, Req 27.9
  style), versioned by ``(playbook_id, version)``::

      id: PB-ATO-CONTAIN
      version: 1
      title: Contain an account takeover
      trigger:                 # at least one of tiers / min_tier / techniques
        min_tier: HIGH         # Severity_Tier at or above
        tiers: [MEDIUM]        # or an explicit tier list
        techniques: [HX-T0006] # any overlap with the Case's HELIX_Technique tags
        match: any             # any (default) | all: how tier and technique conditions combine
      actions:                 # scheduled in this order
        - type: HOLD_TRANSACTION
        - type: NOTIFY
          params: {channel: fraud-ops, message: "ATO contained"}

  ``parse_playbook`` / ``Playbook.from_dict`` validate every field and raise
  ``PlaybookValidationError`` listing each problem with its field path, e.g.
  ``actions[1].type`` for an unknown Playbook_Action, ``trigger.tiers[0]`` for an unknown
  Severity_Tier and ``trigger.techniques[2]`` for an id outside the HELIX_Technique_Catalogue
  (Req 32.1). Unknown keys are rejected too, so a typo never silently disables a condition.
- Playbook_Actions are *simulated only* (Req 32.3, 32.4, 27.11): performing one changes
  ``SimulatedState`` (held tx ids, stepped-up accounts, frozen accounts, an in-memory
  notification outbox) and nothing else. This module imports no networking library and makes
  no I/O; NOTIFY "sends" by appending to ``SimulatedState.notifications``.
- Targets are fixed when an action is scheduled: HOLD_TRANSACTION -> the Case's tx ids,
  STEP_UP_AUTH / FREEZE_ACCOUNT -> the Case's accounts, NOTIFY -> its channel. Each step of a
  playbook is one Playbook_Action (``PA-<n>``) covering all its targets.
- Life cycle (``ActionStatus``): an action is scheduled (transient ``SCHEDULED``) and then,
  in the same call,
  * dry-run mode on -> ``DRY_RUN`` (recorded, simulated state untouched, Req 32.6);
  * Case tier at or above ``approval_threshold`` (default HIGH) -> ``PENDING`` until
    ``approve`` (-> ``PERFORMED``; or ``DRY_RUN`` if dry-run mode is on at approval time) or
    ``reject`` (-> ``REJECTED``) (Req 32.5);
  * otherwise -> ``PERFORMED``.
  Dry-run takes precedence over approval: nothing would change, so nothing needs a decision.
- Idempotence (Req 32.8): the runner remembers every ``(case_id, playbook_id, version)`` it
  has scheduled; a later update of the same Case never schedules that version again, whatever
  the outcome (performed, pending, rejected or dry-run). A new *version* of a playbook may run
  once more for the same Case. Only the highest loaded version of each enabled playbook is
  matched.
- Audit (Req 32.7): every scheduled / approved / rejected / performed / dry-run event (and a
  dry-run mode change) is appended to an optional ``AuditSink`` as
  ``append("PLAYBOOK_ACTION", actor, payload)`` with a JSON-safe payload naming the event,
  action id, action type, Case id, playbook id and version, targets and status. The system
  actor is ``"playbook_runner"``; approvals and rejections carry the investigator. A sink
  that raises is counted in ``audit_errors`` and does not stop the runner (the pipeline
  worker must not die on a ledger fault).
- Thread-safe (one ``RLock``): the pipeline worker calls ``on_case_update`` while REST
  handlers approve, reject and list.

Integration hook (pipeline wiring is a later step): call
``runner.on_case_update(case, techniques=case_techniques(helix.chain(case.case_id)))`` from a
Case change listener. ``case`` is duck-typed: ``case_id``, ``tier`` (``SeverityTier`` or its
name), ``accounts`` and ``tx_ids`` (iterables of ids), optional ``techniques`` (used when the
keyword is not given) and optional ``is_open`` (closed Cases are skipped).
"""

from __future__ import annotations

import re
import threading
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any, Optional, Protocol, Union

import yaml

from helix.config import TIER_ORDER, SeverityTier

from .techniques import Catalogue, default_catalogue

DATA_DIR = Path(__file__).resolve().parent / "data" / "playbooks"
AUDIT_CATEGORY = "PLAYBOOK_ACTION"
SYSTEM_ACTOR = "playbook_runner"
SIMULATED_NOTICE = ("Playbook_Actions are simulated state changes inside HELIX only; "
                    "nothing is sent to any bank, payment, identity or messaging system.")
PLAYBOOK_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")
ACTOR = re.compile(r"^.{1,64}$", re.S)
MAX_ACTIONS = 20
MAX_PLAYBOOK_BYTES = 64 * 1024
DEFAULT_CHANNEL = "fraud-ops"
_TIER_RANK = {t: i for i, t in enumerate(TIER_ORDER)}


class AuditSink(Protocol):
    """Append-only audit target (the Audit_Ledger of Req 35 provides this signature)."""

    def append(self, category: str, actor: str, payload: Mapping[str, Any]) -> Any: ...


class PlaybookActionType(str, Enum):
    HOLD_TRANSACTION = "HOLD_TRANSACTION"
    STEP_UP_AUTH = "STEP_UP_AUTH"
    FREEZE_ACCOUNT = "FREEZE_ACCOUNT"
    NOTIFY = "NOTIFY"

    def __str__(self) -> str:
        return self.value


class ActionStatus(str, Enum):
    SCHEDULED = "SCHEDULED"  # transient: only inside ``on_case_update`` (and its "scheduled" audit entry)
    PENDING = "PENDING"
    PERFORMED = "PERFORMED"
    REJECTED = "REJECTED"
    DRY_RUN = "DRY_RUN"

    def __str__(self) -> str:
        return self.value


class TriggerMatch(str, Enum):
    ANY = "any"
    ALL = "all"

    def __str__(self) -> str:
        return self.value


#: Allowed ``params`` per action type: name -> (max length, default).
ACTION_PARAMS: Mapping[PlaybookActionType, Mapping[str, tuple[int, Optional[str]]]] = {
    PlaybookActionType.HOLD_TRANSACTION: {"note": (500, None)},
    PlaybookActionType.STEP_UP_AUTH: {"note": (500, None)},
    PlaybookActionType.FREEZE_ACCOUNT: {"note": (500, None)},
    PlaybookActionType.NOTIFY: {"channel": (64, DEFAULT_CHANNEL), "message": (500, None)},
}


# --- errors ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class PlaybookProblem:
    field: str
    message: str

    def to_dict(self) -> dict[str, str]:
        return {"field": self.field, "reason": self.message}


class PlaybookValidationError(ValueError):
    """Invalid Response_Playbook definition; ``problems`` names every offending field (Req 32.1)."""

    def __init__(self, problems: Iterable[PlaybookProblem]) -> None:
        self.problems = tuple(problems)
        super().__init__("; ".join(f"{p.field}: {p.message}" for p in self.problems) or "invalid")

    @property
    def fields(self) -> tuple[str, ...]:
        return tuple(p.field for p in self.problems)


class ActionNotFound(KeyError):
    def __init__(self, action_id: str) -> None:
        self.action_id = action_id
        super().__init__(action_id)


class ActionStateError(RuntimeError):
    """Approve / reject of an action that is not PENDING."""

    def __init__(self, action_id: str, status: ActionStatus) -> None:
        self.action_id, self.status = action_id, status
        super().__init__(f"Playbook_Action {action_id} is {status.value}, not PENDING")


# --- definitions ----------------------------------------------------------------------


def _tier(v: Any) -> SeverityTier:
    return SeverityTier(getattr(v, "value", v))


def tier_at_least(a: Any, b: Any) -> bool:
    return _TIER_RANK[_tier(a)] >= _TIER_RANK[_tier(b)]


@dataclass(frozen=True, slots=True)
class Trigger:
    tiers: frozenset[SeverityTier] = frozenset()
    min_tier: Optional[SeverityTier] = None
    techniques: frozenset[str] = frozenset()
    match: TriggerMatch = TriggerMatch.ANY

    def tier_ok(self, tier: Optional[SeverityTier]) -> Optional[bool]:
        """None when the trigger has no tier condition."""
        if not self.tiers and self.min_tier is None:
            return None
        if tier is None:
            return False
        return tier in self.tiers or (self.min_tier is not None and tier_at_least(tier, self.min_tier))

    def technique_ok(self, techniques: Iterable[str]) -> Optional[bool]:
        if not self.techniques:
            return None
        return not self.techniques.isdisjoint(techniques)

    def matches(self, tier: Optional[SeverityTier], techniques: Iterable[str]) -> bool:
        conds = [c for c in (self.tier_ok(tier), self.technique_ok(set(techniques))) if c is not None]
        if not conds:
            return False
        return all(conds) if self.match is TriggerMatch.ALL else any(conds)

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {}
        if self.tiers:
            d["tiers"] = [t.value for t in TIER_ORDER if t in self.tiers]
        if self.min_tier is not None:
            d["min_tier"] = self.min_tier.value
        if self.techniques:
            d["techniques"] = sorted(self.techniques)
        d["match"] = self.match.value
        return d


@dataclass(frozen=True, slots=True)
class ActionSpec:
    type: PlaybookActionType
    params: tuple[tuple[str, str], ...] = ()

    def param(self, name: str) -> Optional[str]:
        for k, v in self.params:
            if k == name:
                return v
        return ACTION_PARAMS[self.type].get(name, (0, None))[1]

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {"type": self.type.value}
        if self.params:
            d["params"] = dict(self.params)
        return d


@dataclass(frozen=True, slots=True)
class Playbook:
    playbook_id: str
    version: int
    title: str
    trigger: Trigger
    actions: tuple[ActionSpec, ...]
    description: str = ""
    enabled: bool = True

    @property
    def key(self) -> tuple[str, int]:
        return (self.playbook_id, self.version)

    @classmethod
    def from_dict(cls, d: Any, catalogue: Optional[Catalogue] = None) -> Playbook:
        return _build(d, catalogue or default_catalogue())

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {"id": self.playbook_id, "version": self.version, "title": self.title}
        if self.description:
            d["description"] = self.description
        d["enabled"] = self.enabled
        d["trigger"] = self.trigger.to_dict()
        d["actions"] = [a.to_dict() for a in self.actions]
        return d


_TOP_KEYS = frozenset({"id", "version", "title", "description", "enabled", "trigger", "actions"})
_TRIGGER_KEYS = frozenset({"tiers", "min_tier", "techniques", "match"})
_ACTION_KEYS = frozenset({"type", "params"})


def _tier_or_problem(v: Any, where: str, out: list[PlaybookProblem]) -> Optional[SeverityTier]:
    if isinstance(v, str) and v in SeverityTier.__members__:
        return SeverityTier(v)
    out.append(PlaybookProblem(where, f"unknown Severity_Tier {v!r}; expected one of "
                                      f"{', '.join(t.value for t in TIER_ORDER)}"))
    return None


def _list(v: Any, where: str, out: list[PlaybookProblem]) -> list[Any]:
    if v is None:
        return []
    if not isinstance(v, list):
        out.append(PlaybookProblem(where, "must be a list"))
        return []
    return v


def _build(d: Any, cat: Catalogue) -> Playbook:
    p: list[PlaybookProblem] = []
    if not isinstance(d, Mapping):
        raise PlaybookValidationError([PlaybookProblem("document", "must be a mapping")])
    for k in d:
        if k not in _TOP_KEYS:
            p.append(PlaybookProblem(str(k), "unknown field"))
    pid = d.get("id")
    if not isinstance(pid, str) or not PLAYBOOK_ID.match(pid):
        p.append(PlaybookProblem("id", f"{pid!r} must match {PLAYBOOK_ID.pattern}"))
    ver = d.get("version")
    if isinstance(ver, bool) or not isinstance(ver, int) or not 1 <= ver <= 1_000_000:
        p.append(PlaybookProblem("version", f"{ver!r} must be an integer in [1, 1000000]"))
    title = d.get("title")
    if not isinstance(title, str) or not title.strip() or len(title) > 200:
        p.append(PlaybookProblem("title", "must be a non-empty string of at most 200 characters"))
    desc = d.get("description", "")
    if desc is None:
        desc = ""
    if not isinstance(desc, str) or len(desc) > 2000:
        p.append(PlaybookProblem("description", "must be a string of at most 2000 characters"))
    enabled = d.get("enabled", True)
    if not isinstance(enabled, bool):
        p.append(PlaybookProblem("enabled", "must be true or false"))

    # trigger
    trig = d.get("trigger")
    tiers: set[SeverityTier] = set()
    min_tier: Optional[SeverityTier] = None
    techs: list[str] = []
    match = TriggerMatch.ANY
    if not isinstance(trig, Mapping):
        p.append(PlaybookProblem("trigger", "must be a mapping with tiers, min_tier and/or techniques"))
    else:
        for k in trig:
            if k not in _TRIGGER_KEYS:
                p.append(PlaybookProblem(f"trigger.{k}", "unknown field"))
        for i, t in enumerate(_list(trig.get("tiers"), "trigger.tiers", p)):
            tv = _tier_or_problem(t, f"trigger.tiers[{i}]", p)
            if tv is not None:
                tiers.add(tv)
        if trig.get("min_tier") is not None:
            min_tier = _tier_or_problem(trig.get("min_tier"), "trigger.min_tier", p)
        for i, t in enumerate(_list(trig.get("techniques"), "trigger.techniques", p)):
            if not isinstance(t, str) or t not in cat:
                p.append(PlaybookProblem(f"trigger.techniques[{i}]",
                                         f"unknown HELIX_Technique id {t!r} (catalogue {cat.version})"))
            else:
                techs.append(t)
        m = trig.get("match", "any")
        try:
            match = TriggerMatch(m)
        except ValueError:
            p.append(PlaybookProblem("trigger.match", f"{m!r} must be 'any' or 'all'"))
        if not trig.get("tiers") and trig.get("min_tier") is None and not trig.get("techniques"):
            p.append(PlaybookProblem("trigger", "needs at least one of tiers, min_tier or techniques"))

    # actions
    specs: list[ActionSpec] = []
    raw = d.get("actions")
    if not isinstance(raw, list) or not 1 <= len(raw) <= MAX_ACTIONS:
        p.append(PlaybookProblem("actions", f"must be a list of 1 to {MAX_ACTIONS} actions"))
        raw = raw if isinstance(raw, list) else []
    for i, a in enumerate(raw):
        w = f"actions[{i}]"
        if not isinstance(a, Mapping):
            p.append(PlaybookProblem(w, "must be a mapping with a 'type'"))
            continue
        for k in a:
            if k not in _ACTION_KEYS:
                p.append(PlaybookProblem(f"{w}.{k}", "unknown field"))
        t = a.get("type")
        if not (isinstance(t, str) and t in PlaybookActionType.__members__):
            p.append(PlaybookProblem(f"{w}.type", f"unknown Playbook_Action {t!r}; expected one of "
                                                  f"{', '.join(PlaybookActionType.__members__)}"))
            continue
        at = PlaybookActionType(t)
        params = a.get("params") or {}
        clean: list[tuple[str, str]] = []
        if not isinstance(params, Mapping):
            p.append(PlaybookProblem(f"{w}.params", "must be a mapping"))
            params = {}
        allowed = ACTION_PARAMS[at]
        for k, v in params.items():
            if k not in allowed:
                p.append(PlaybookProblem(f"{w}.params.{k}", f"unknown parameter for {at.value}; allowed: "
                                                            f"{', '.join(allowed) or 'none'}"))
            elif not isinstance(v, str) or not v.strip() or len(v) > allowed[k][0]:
                p.append(PlaybookProblem(f"{w}.params.{k}", f"must be a non-empty string of at most "
                                                            f"{allowed[k][0]} characters"))
            else:
                clean.append((str(k), v))
        specs.append(ActionSpec(at, tuple(sorted(clean))))

    if p:
        raise PlaybookValidationError(p)
    return Playbook(str(pid), int(ver), str(title).strip(),  # type: ignore[arg-type]
                    Trigger(frozenset(tiers), min_tier, frozenset(techs), match),
                    tuple(specs), str(desc or ""), bool(enabled))


def parse_playbook(text: Union[str, bytes], catalogue: Optional[Catalogue] = None) -> Playbook:
    """Parse one Response_Playbook YAML document (safe loader only) and validate it."""
    if isinstance(text, bytes):
        if len(text) > MAX_PLAYBOOK_BYTES:
            raise PlaybookValidationError([PlaybookProblem("document", f"larger than {MAX_PLAYBOOK_BYTES} bytes")])
        try:
            text = text.decode("utf-8")
        except UnicodeDecodeError:
            raise PlaybookValidationError([PlaybookProblem("document", "must be UTF-8")]) from None
    if len(text) > MAX_PLAYBOOK_BYTES:
        raise PlaybookValidationError([PlaybookProblem("document", f"larger than {MAX_PLAYBOOK_BYTES} bytes")])
    try:
        doc = yaml.safe_load(text)
    except yaml.YAMLError as e:
        mark = getattr(e, "problem_mark", None)
        where = f"line {mark.line + 1}" if mark is not None else "document"
        raise PlaybookValidationError([PlaybookProblem(where, f"invalid YAML: {getattr(e, 'problem', e)}")]) from None
    return Playbook.from_dict(doc, catalogue)


def format_playbook(pb: Playbook) -> str:
    return yaml.safe_dump(pb.to_dict(), sort_keys=False, allow_unicode=True)


def load_playbooks(path: Union[str, Path, None] = None, catalogue: Optional[Catalogue] = None) -> list[Playbook]:
    """Every ``*.yaml`` playbook in ``path`` (default ``data/playbooks``), sorted by file name.
    The first invalid file raises ``PlaybookValidationError`` with the file name prefixed."""
    root = Path(path or DATA_DIR)
    out = []
    for f in sorted(root.glob("*.yaml")):
        try:
            out.append(parse_playbook(f.read_text(encoding="utf-8"), catalogue))
        except PlaybookValidationError as e:
            raise PlaybookValidationError(PlaybookProblem(f"{f.name}:{q.field}", q.message)
                                          for q in e.problems) from None
    return out


# --- simulated state --------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class EffectRecord:
    action_id: str
    case_id: str
    at: datetime


@dataclass(frozen=True, slots=True)
class Notification:
    action_id: str
    case_id: str
    channel: str
    message: str
    at: datetime

    def to_dict(self) -> dict[str, Any]:
        return {"action_id": self.action_id, "case_id": self.case_id, "channel": self.channel,
                "message": self.message, "at": self.at.isoformat()}


@dataclass
class SimulatedState:
    """The only thing a performed Playbook_Action changes (Req 32.3). In memory, no I/O."""

    held_tx: dict[str, EffectRecord] = field(default_factory=dict)
    step_up: dict[str, EffectRecord] = field(default_factory=dict)
    frozen: dict[str, EffectRecord] = field(default_factory=dict)
    notifications: list[Notification] = field(default_factory=list)

    def is_held(self, tx_id: str) -> bool:
        return tx_id in self.held_tx

    def requires_step_up(self, account_id: str) -> bool:
        return account_id in self.step_up

    def is_frozen(self, account_id: str) -> bool:
        return account_id in self.frozen

    def snapshot(self) -> tuple[Any, ...]:
        """Hashable value equal for equal states (used to show dry-run changes nothing)."""
        return (tuple(sorted((k, v.action_id) for k, v in self.held_tx.items())),
                tuple(sorted((k, v.action_id) for k, v in self.step_up.items())),
                tuple(sorted((k, v.action_id) for k, v in self.frozen.items())),
                tuple((n.action_id, n.channel, n.message) for n in self.notifications))

    def summary(self) -> dict[str, int]:
        return {"held_transactions": len(self.held_tx), "step_up_accounts": len(self.step_up),
                "frozen_accounts": len(self.frozen), "notifications": len(self.notifications)}

    def to_dict(self) -> dict[str, Any]:
        def recs(d: Mapping[str, EffectRecord]) -> list[dict[str, Any]]:
            return [{"id": k, "action_id": v.action_id, "case_id": v.case_id, "at": v.at.isoformat()}
                    for k, v in sorted(d.items())]

        return {"held_transactions": recs(self.held_tx), "step_up_accounts": recs(self.step_up),
                "frozen_accounts": recs(self.frozen),
                "notifications": [n.to_dict() for n in self.notifications]}


# --- actions ------------------------------------------------------------------------------


@dataclass
class PlaybookAction:
    action_id: str
    case_id: str
    playbook_id: str
    playbook_version: int
    step: int  # index in the playbook's action list (defined order)
    type: PlaybookActionType
    targets: tuple[str, ...]
    params: tuple[tuple[str, str], ...]
    case_tier: SeverityTier
    status: ActionStatus
    scheduled_at: datetime
    requires_approval: bool
    decided_by: Optional[str] = None
    decided_at: Optional[datetime] = None
    decision_reason: Optional[str] = None
    completed_at: Optional[datetime] = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "action_id": self.action_id, "case_id": self.case_id, "playbook_id": self.playbook_id,
            "playbook_version": self.playbook_version, "step": self.step, "type": self.type.value,
            "targets": list(self.targets), "params": dict(self.params), "case_tier": self.case_tier.value,
            "status": self.status.value, "requires_approval": self.requires_approval,
            "scheduled_at": self.scheduled_at.isoformat(),
            "decided_by": self.decided_by,
            "decided_at": None if self.decided_at is None else self.decided_at.isoformat(),
            "decision_reason": self.decision_reason,
            "completed_at": None if self.completed_at is None else self.completed_at.isoformat(),
            "simulated": True,
        }


def _ids(v: Any) -> tuple[str, ...]:
    if v is None:
        return ()
    if isinstance(v, str):
        return (v,)
    return tuple(sorted({str(x) for x in v}))


def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


class PlaybookRunner:
    """Matches Cases to Response_Playbooks and performs, queues or dry-runs their actions."""

    def __init__(
        self,
        playbooks: Iterable[Playbook] = (),
        *,
        approval_threshold: Union[SeverityTier, str] = SeverityTier.HIGH,
        dry_run: bool = False,
        audit: Optional[AuditSink] = None,
        clock: Optional[Callable[[], datetime]] = None,
        catalogue: Optional[Catalogue] = None,
    ) -> None:
        self.approval_threshold = _tier(approval_threshold)
        self._dry_run = bool(dry_run)
        self.audit = audit
        self.clock = clock or _now_utc
        self.catalogue = catalogue or default_catalogue()
        self.state = SimulatedState()
        self.audit_errors = 0
        self._lock = threading.RLock()
        self._playbooks: dict[tuple[str, int], Playbook] = {}
        self._actions: dict[str, PlaybookAction] = {}
        self._ran: set[tuple[str, str, int]] = set()  # (case_id, playbook_id, version)
        self._seq = 0
        for pb in playbooks:
            self.add(pb)

    @classmethod
    def with_defaults(cls, path: Union[str, Path, None] = None, **kw: Any) -> PlaybookRunner:
        """Runner loaded with the bundled playbooks (``data/playbooks/*.yaml``) or ``path``."""
        return cls(load_playbooks(path, kw.get("catalogue")), **kw)

    # -- playbook registry -----------------------------------------------------------------

    def add(self, pb: Union[Playbook, Mapping[str, Any], str, bytes]) -> Playbook:
        """Validate and register a playbook version (Req 32.1). Re-adding an identical version is
        a no-op; a different definition under an existing ``(id, version)`` is rejected."""
        if isinstance(pb, (str, bytes)):
            pb = parse_playbook(pb, self.catalogue)
        elif not isinstance(pb, Playbook):
            pb = Playbook.from_dict(pb, self.catalogue)
        else:
            pb = Playbook.from_dict(pb.to_dict(), self.catalogue)  # re-validate against our catalogue
        with self._lock:
            old = self._playbooks.get(pb.key)
            if old is not None and old != pb:
                raise PlaybookValidationError([PlaybookProblem(
                    "version", f"{pb.playbook_id} version {pb.version} already exists with a different "
                               "definition; use a new version number")])
            self._playbooks[pb.key] = pb
        return pb

    def playbooks(self) -> list[Playbook]:
        with self._lock:
            return [self._playbooks[k] for k in sorted(self._playbooks)]

    def active_playbooks(self) -> list[Playbook]:
        """Highest version of each playbook id, enabled ones only, sorted by id."""
        with self._lock:
            latest: dict[str, Playbook] = {}
            for pb in self._playbooks.values():
                cur = latest.get(pb.playbook_id)
                if cur is None or pb.version > cur.version:
                    latest[pb.playbook_id] = pb
            return [latest[k] for k in sorted(latest) if latest[k].enabled]

    def matching(self, tier: Any, techniques: Iterable[str] = ()) -> list[Playbook]:
        t = None if tier is None else _tier(tier)
        tech = set(techniques)
        return [pb for pb in self.active_playbooks() if pb.trigger.matches(t, tech)]

    # -- dry-run mode --------------------------------------------------------------------

    @property
    def dry_run(self) -> bool:
        return self._dry_run

    def set_dry_run(self, enabled: bool, by: str = SYSTEM_ACTOR) -> bool:
        """Switch dry-run mode; audited when it changes. Returns the new mode."""
        with self._lock:
            enabled = bool(enabled)
            if enabled != self._dry_run:
                self._dry_run = enabled
                self._audit(by, {"event": "dry_run_mode", "enabled": enabled,
                                 "at": self.clock().isoformat()})
            return self._dry_run

    # -- the hook ------------------------------------------------------------------------

    def on_case_update(self, case: Any, *, techniques: Optional[Iterable[str]] = None,
                       now: Optional[datetime] = None) -> list[PlaybookAction]:
        """Schedule every triggered playbook version not yet run for this Case (Req 32.2, 32.8).
        Returns the newly scheduled actions in scheduling order (playbook id, then step)."""
        if getattr(case, "is_open", True) is False:
            return []
        case_id = str(case.case_id)
        tier = _tier(case.tier)
        tech = set(techniques if techniques is not None else (getattr(case, "techniques", None) or ()))
        accounts = _ids(getattr(case, "accounts", ()))
        tx_ids = _ids(getattr(case, "tx_ids", ()))
        out: list[PlaybookAction] = []
        with self._lock:
            now = now or self.clock()
            for pb in self.matching(tier, tech):
                key = (case_id, pb.playbook_id, pb.version)
                if key in self._ran:
                    continue
                self._ran.add(key)
                for step, spec in enumerate(pb.actions):
                    out.append(self._schedule(case_id, tier, pb, step, spec, accounts, tx_ids, now))
        return out

    def _schedule(self, case_id: str, tier: SeverityTier, pb: Playbook, step: int, spec: ActionSpec,
                  accounts: tuple[str, ...], tx_ids: tuple[str, ...], now: datetime) -> PlaybookAction:
        self._seq += 1
        if spec.type is PlaybookActionType.HOLD_TRANSACTION:
            targets = tx_ids
        elif spec.type is PlaybookActionType.NOTIFY:
            targets = (spec.param("channel") or DEFAULT_CHANNEL,)
        else:
            targets = accounts
        needs = tier_at_least(tier, self.approval_threshold)
        a = PlaybookAction(
            action_id=f"PA-{self._seq:06d}", case_id=case_id, playbook_id=pb.playbook_id,
            playbook_version=pb.version, step=step, type=spec.type, targets=targets, params=spec.params,
            case_tier=tier, status=ActionStatus.SCHEDULED, scheduled_at=now,
            requires_approval=needs and not self._dry_run,
        )
        self._actions[a.action_id] = a
        self._audit(SYSTEM_ACTOR, self._payload(a, "scheduled"))
        if self._dry_run:
            self._mark_dry_run(a, now, SYSTEM_ACTOR)
        elif needs:
            a.status = ActionStatus.PENDING  # held for an Investigator decision (Req 32.5)
        else:
            self._perform(a, now, SYSTEM_ACTOR)
        return a

    # -- decisions -------------------------------------------------------------------------

    def approve(self, action_id: str, by: str, now: Optional[datetime] = None) -> PlaybookAction:
        """Approve a PENDING action: performed (or DRY_RUN in dry-run mode) (Req 32.5)."""
        _check_actor(by)
        with self._lock:
            a = self._pending(action_id)
            now = now or self.clock()
            a.decided_by, a.decided_at = by, now
            self._audit(by, self._payload(a, "approved"))
            if self._dry_run:
                self._mark_dry_run(a, now, by)
            else:
                self._perform(a, now, by)
            return a

    def reject(self, action_id: str, by: str, reason: Optional[str] = None,
               now: Optional[datetime] = None) -> PlaybookAction:
        """Reject a PENDING action: REJECTED, simulated state untouched (Req 32.5)."""
        _check_actor(by)
        if reason is not None and (not isinstance(reason, str) or len(reason) > 500):
            raise ValueError("reason must be a string of at most 500 characters")
        with self._lock:
            a = self._pending(action_id)
            now = now or self.clock()
            a.status, a.decided_by, a.decided_at, a.decision_reason = ActionStatus.REJECTED, by, now, reason
            a.completed_at = now
            self._audit(by, self._payload(a, "rejected"))
            return a

    def _pending(self, action_id: str) -> PlaybookAction:
        a = self._actions.get(action_id)
        if a is None:
            raise ActionNotFound(action_id)
        if a.status is not ActionStatus.PENDING:
            raise ActionStateError(action_id, a.status)
        return a

    # -- effects ---------------------------------------------------------------------------

    def _perform(self, a: PlaybookAction, now: datetime, actor: str) -> None:
        rec = EffectRecord(a.action_id, a.case_id, now)
        st = self.state
        if a.type is PlaybookActionType.HOLD_TRANSACTION:
            for t in a.targets:
                st.held_tx.setdefault(t, rec)
        elif a.type is PlaybookActionType.STEP_UP_AUTH:
            for t in a.targets:
                st.step_up.setdefault(t, rec)
        elif a.type is PlaybookActionType.FREEZE_ACCOUNT:
            for t in a.targets:
                st.frozen.setdefault(t, rec)
        else:  # NOTIFY: in-memory outbox only
            msg = dict(a.params).get("message") or (
                f"Case {a.case_id} ({a.case_tier.value}): playbook {a.playbook_id} v{a.playbook_version}")
            st.notifications.append(Notification(a.action_id, a.case_id, a.targets[0], msg, now))
        a.status, a.completed_at = ActionStatus.PERFORMED, now
        self._audit(actor, self._payload(a, "performed"))

    def _mark_dry_run(self, a: PlaybookAction, now: datetime, actor: str) -> None:
        a.status, a.completed_at = ActionStatus.DRY_RUN, now
        self._audit(actor, self._payload(a, "dry_run"))

    # -- audit -----------------------------------------------------------------------------

    @staticmethod
    def _payload(a: PlaybookAction, event: str) -> dict[str, Any]:
        return {"event": event, "action_id": a.action_id, "action": a.type.value, "case_id": a.case_id,
                "playbook_id": a.playbook_id, "playbook_version": a.playbook_version, "step": a.step,
                "targets": list(a.targets), "status": a.status.value,
                "requires_approval": a.requires_approval, "simulated": True}

    def _audit(self, actor: str, payload: Mapping[str, Any]) -> None:
        if self.audit is None:
            return
        try:
            self.audit.append(AUDIT_CATEGORY, actor, payload)
        except Exception:  # noqa: BLE001 - a ledger fault must not stop the pipeline worker
            self.audit_errors += 1

    # -- queries ---------------------------------------------------------------------------

    def action(self, action_id: str) -> PlaybookAction:
        with self._lock:
            a = self._actions.get(action_id)
            if a is None:
                raise ActionNotFound(action_id)
            return a

    def actions(self, case_id: Optional[str] = None,
                status: Union[ActionStatus, str, None] = None) -> list[PlaybookAction]:
        st = None if status is None else ActionStatus(getattr(status, "value", status))
        with self._lock:
            return [a for a in self._actions.values()
                    if (case_id is None or a.case_id == case_id) and (st is None or a.status is st)]

    def runs(self, case_id: Optional[str] = None) -> list[tuple[str, str, int]]:
        """``(case_id, playbook_id, version)`` already scheduled (each at most once, Req 32.8)."""
        with self._lock:
            return sorted(k for k in self._ran if case_id is None or k[0] == case_id)

    def summary(self) -> dict[str, Any]:
        with self._lock:
            counts = {s.value: 0 for s in ActionStatus}
            for a in self._actions.values():
                counts[a.status.value] += 1
            return {"dry_run": self._dry_run, "approval_threshold": self.approval_threshold.value,
                    "actions": counts, "simulated_state": self.state.summary(),
                    "audit_errors": self.audit_errors, "notice": SIMULATED_NOTICE}


def _check_actor(by: Any) -> None:
    if not isinstance(by, str) or not by.strip() or not ACTOR.match(by):
        raise ValueError("by must be a non-empty string of at most 64 characters")


__all__ = [
    "ACTION_PARAMS",
    "AUDIT_CATEGORY",
    "DATA_DIR",
    "SIMULATED_NOTICE",
    "SYSTEM_ACTOR",
    "ActionNotFound",
    "ActionSpec",
    "ActionStateError",
    "ActionStatus",
    "AuditSink",
    "EffectRecord",
    "Notification",
    "Playbook",
    "PlaybookAction",
    "PlaybookActionType",
    "PlaybookProblem",
    "PlaybookRunner",
    "PlaybookValidationError",
    "SimulatedState",
    "Trigger",
    "TriggerMatch",
    "format_playbook",
    "load_playbooks",
    "parse_playbook",
    "tier_at_least",
]
