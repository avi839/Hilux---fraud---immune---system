"""Detection-as-Code Rule_Engine (Req 31): Sigma-style YAML Detection_Rules.

Rule document (one YAML document per rule)::

    id: HXR-0001                     # [A-Za-z0-9][A-Za-z0-9_.-]{0,63}
    version: 3                       # optional; assigned by the engine on acceptance
    title: Dormant account moves money
    description: optional free text
    status: experimental             # experimental | test | stable | deprecated (default experimental)
    target: transaction              # transaction | auth (default transaction)
    techniques: [HX-T0014]           # one or more HELIX_Technique ids from the catalogue
    detection:
      dormant:                       # a selection: every item must match (AND)
        dna.seconds_since_last|gte: 2592000
        tx.amount|gte: 1000
      small:
        tx.amount|lt: 10
      condition: dormant and not small   # and / or / not / parentheses over selection names
    mass:                            # Mass_Function emitted on a match
      fraud: 0.4
      legit: 0.0                     # optional, default 0; uncertainty = 1 - fraud - legit

Selection items are ``<field>`` or ``<field>|<modifier>`` mapped to a value or a list of values
(any value may match: OR). Modifiers: none (equality), ``gt``, ``gte``, ``lt``, ``lte`` (numbers),
``contains``, ``startswith``, ``endswith`` (strings, case-sensitive) and ``exists`` (true/false).
A missing (null) field matches only ``exists: false``. Fields:

- ``target: transaction`` - ``tx.*`` (Transaction_Event fields plus ``tx.hour`` / ``tx.weekday``,
  UTC) and ``dna.*`` (the source account's pre-event Behavioural_DNA: ``count``,
  ``amount_mean``, ``amount_std``, ``amount_ratio``, ``iat_mean``, ``tx_24h``,
  ``counterparties``, ``known_counterparty``, ``seconds_since_last``, ``known_channel``);
- ``target: auth`` - ``auth.*`` (Auth_Event fields plus ``auth.hour`` and ``auth.new_device``).
  Live, an auth rule fires on a Transaction_Event when an Auth_Event of the source account
  inside the Auth_Correlation_Window matches; the backtest counts the matching Auth_Events.

Design decisions:
- Safe parsing (Req 27.9, 31.9): ``yaml.compose`` with ``SafeLoader`` builds a node tree (no
  construction), scalars are built with ``SafeConstructor`` only, and only str / int / float /
  bool / null scalars, plain mappings and sequences are accepted. Aliases (billion-laughs),
  merge keys, custom tags, duplicate keys and documents above 64 KiB are rejected. Every
  problem carries the 1-based line number and the cause (Req 31.2).
- Ground-truth isolation (Req 31.7): the engine never imports the read side of the
  Ground_Truth_Store; a field whose path names a ground-truth concept (the field names of
  ``GroundTruthRecord`` other than generic ones like ``event_id``, a ``label``/``truth`` root,
  ``is_fraud`` ...) is rejected with an explicit Ground_Truth_Store message before the generic
  unknown-field check. The record *type* is imported only for its field names.
- Versioning (Req 31.3): each accepted document becomes version ``latest + 1`` of its rule id
  (the document's own ``version`` is ignored on submission). Invalid submissions change nothing,
  so the previously active version stays active (Req 31.2).
- Backtest (Req 31.4): on acceptance the version is replayed over the stored events of the
  backtest window (default 7 days, ending at the source's stream time). Transaction rules see a
  Behavioural_DNA rebuilt from the window's events only (an approximation of the live DNA,
  reported in ``notes``). Estimated false positives are hits on events outside Cases labelled
  CONFIRMED_FRAUD (investigator labels; no ground truth).
- Activation (Req 31.5): only a backtested version can be activated; the active set is an
  immutable mapping swapped atomically, read by the pipeline on every event, so the version
  applies from the next processed event without a restart (well under 5 s).
- Emission (Req 31.6): each matching active rule adds a ``Contribution`` (``rule:<id>``,
  ``ContributorType.RULE``, the rule's Mass_Function, weight 1 unless a learned weight exists)
  to stage-1 fusion and a SUCCESS result whose features carry ``rule_id``, ``version``,
  ``techniques`` and the matched ids, which the Evidence_Chain records for the contributor.
- Round-trip (Req 31.8): ``format_rule`` writes a canonical document; for every valid rule
  ``parse_rule(format_rule(r)) == r``.
- A failing rule evaluation is isolated: it is counted in ``RuleEngine.eval_errors`` and skipped.
"""

from __future__ import annotations

import dataclasses
import hashlib
import logging
import math
import re
import threading
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta, timezone
from enum import Enum
from pathlib import Path
from types import MappingProxyType
from typing import Any, Optional, Protocol, Union

import yaml
from yaml.constructor import ConstructorError, SafeConstructor
from yaml.nodes import MappingNode, Node, ScalarNode, SequenceNode

from helix.codec.auth import AuthEventType
from helix.fusion import Contribution, ContributorType, FusedAssessment, Mass, fuse
from helix.sensors.base import SensorOutput
from helix.sensors.runner import SensorOutcome, SensorResult
from helix.sim.ground_truth import GroundTruthRecord

from .techniques import Catalogue, default_catalogue, is_technique_id

log = logging.getLogger(__name__)

CONTRIBUTOR_PREFIX = "rule:"
MAX_RULE_BYTES = 64 * 1024
MAX_CONDITION_LEN = 2_000
MAX_CONDITION_DEPTH = 32
MAX_VALUES = 256
MAX_STRING = 256
MAX_TITLE = 200
MAX_DESCRIPTION = 4_000
MAX_LISTED = 50
DEFAULT_BACKTEST_WINDOW_S = 7 * 86_400.0
DEFAULT_AUTH_WINDOW_S = 1_800.0
TOL = 1e-9

RULE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")
SELECTION_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,63}$")
KEYWORDS = frozenset({"and", "or", "not"})
RESERVED = KEYWORDS | {"condition"}
TOP_KEYS = ("id", "version", "title", "description", "status", "target", "techniques", "detection", "mass")
REQUIRED_KEYS = ("id", "title", "techniques", "detection", "mass")

Scalar = Union[str, int, float, bool]


class RuleStatus(str, Enum):
    EXPERIMENTAL = "experimental"
    TEST = "test"
    STABLE = "stable"
    DEPRECATED = "deprecated"

    def __str__(self) -> str:
        return self.value


class RuleTarget(str, Enum):
    TRANSACTION = "transaction"
    AUTH = "auth"

    def __str__(self) -> str:
        return self.value


# --- fields ------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class FieldSpec:
    kind: str  # number | string | bool | enum
    choices: tuple[str, ...] = ()


_NUM, _STR, _BOOL = FieldSpec("number"), FieldSpec("string"), FieldSpec("bool")

TX_FIELDS: Mapping[str, FieldSpec] = MappingProxyType({
    "tx.tx_id": _STR, "tx.src": _STR, "tx.dst": _STR, "tx.amount": _NUM, "tx.currency": _STR,
    "tx.channel": _STR, "tx.mcc": _STR, "tx.lat": _NUM, "tx.lon": _NUM, "tx.device_id": _STR,
    "tx.session_id": _STR, "tx.hour": _NUM, "tx.weekday": _NUM,
})
DNA_FIELDS: Mapping[str, FieldSpec] = MappingProxyType({
    "dna.count": _NUM, "dna.amount_mean": _NUM, "dna.amount_std": _NUM, "dna.amount_ratio": _NUM,
    "dna.iat_mean": _NUM, "dna.tx_24h": _NUM, "dna.counterparties": _NUM,
    "dna.known_counterparty": _BOOL, "dna.seconds_since_last": _NUM, "dna.known_channel": _BOOL,
})
AUTH_FIELDS: Mapping[str, FieldSpec] = MappingProxyType({
    "auth.event_id": _STR, "auth.account_id": _STR, "auth.session_id": _STR,
    "auth.event_type": FieldSpec("enum", tuple(t.value for t in AuthEventType)),
    "auth.device_id": _STR, "auth.ip": _STR, "auth.lat": _NUM, "auth.lon": _NUM,
    "auth.payee_account_id": _STR, "auth.hour": _NUM, "auth.new_device": _BOOL,
})
FIELDS: Mapping[RuleTarget, Mapping[str, FieldSpec]] = MappingProxyType({
    RuleTarget.TRANSACTION: MappingProxyType({**TX_FIELDS, **DNA_FIELDS}),
    RuleTarget.AUTH: AUTH_FIELDS,
})

MODIFIERS: Mapping[str, frozenset[str]] = MappingProxyType({
    "number": frozenset({"eq", "gt", "gte", "lt", "lte", "exists"}),
    "string": frozenset({"eq", "contains", "startswith", "endswith", "exists"}),
    "enum": frozenset({"eq", "exists"}),
    "bool": frozenset({"eq", "exists"}),
})
ALL_MODIFIERS = frozenset().union(*MODIFIERS.values())

# Ground-truth references (Req 31.7). Record fields that are also ordinary event fields
# (``event_id``) or too generic (``source``, ``written_at``) are not treated as ground truth.
_GT_GENERIC = frozenset({"event_id", "source", "written_at"})
GT_FIELD_NAMES = frozenset(f.name for f in dataclasses.fields(GroundTruthRecord)) - _GT_GENERIC | frozenset(
    {"is_fraud", "fraud_label", "is_attack", "attack_label", "labels", "label", "gt_label"})
GT_ROOTS = frozenset({"groundtruth", "groundtruthstore", "gt", "truth", "label", "labels", "attacker"})


def references_ground_truth(field_path: str) -> bool:
    """True if a field path names a Ground_Truth_Store concept (root or any segment)."""
    segs = [s for s in field_path.split(".") if s]
    if not segs:
        return False
    if segs[0].lower().replace("_", "").replace("-", "") in GT_ROOTS:
        return True
    return any(s.lower() in GT_FIELD_NAMES for s in segs)


# --- model ---------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Item:
    field: str
    modifier: str  # "eq" is written without a suffix
    values: tuple[Scalar, ...]

    @property
    def key(self) -> str:
        return self.field if self.modifier == "eq" else f"{self.field}|{self.modifier}"


@dataclass(frozen=True, slots=True)
class Selection:
    name: str
    items: tuple[Item, ...]


Cond = tuple  # ("sel", name) | ("not", c) | ("and", (c, ...)) | ("or", (c, ...))


@dataclass(frozen=True)
class DetectionRule:
    rule_id: str
    title: str
    techniques: tuple[str, ...]
    selections: tuple[Selection, ...]
    condition: str
    mass_fraud: float
    mass_legit: float = 0.0
    status: RuleStatus = RuleStatus.EXPERIMENTAL
    target: RuleTarget = RuleTarget.TRANSACTION
    description: Optional[str] = None
    version: Optional[int] = None
    _ast: Any = field(default=None, compare=False, repr=False, hash=False)

    def __post_init__(self) -> None:
        if self._ast is None:
            object.__setattr__(self, "_ast", parse_condition(self.condition, {s.name for s in self.selections}))

    @property
    def contributor_id(self) -> str:
        return CONTRIBUTOR_PREFIX + self.rule_id

    @property
    def mass(self) -> Mass:
        return Mass(self.mass_fraud, self.mass_legit, max(0.0, 1.0 - self.mass_fraud - self.mass_legit))

    @property
    def fields(self) -> frozenset[str]:
        return frozenset(i.field for s in self.selections for i in s.items)

    def with_version(self, version: Optional[int]) -> DetectionRule:
        return replace(self, version=version, _ast=self._ast)

    def matches(self, get: Callable[[str], Any]) -> bool:
        """Evaluate the condition with ``get(field) -> value`` (None = missing)."""
        sels = {s.name: s for s in self.selections}
        cache: dict[str, bool] = {}

        def sel(name: str) -> bool:
            if name not in cache:
                cache[name] = all(match_item(i, get(i.field)) for i in sels[name].items)
            return cache[name]

        return eval_condition(self._ast, sel)

    def summary(self) -> dict[str, Any]:
        return {"rule_id": self.rule_id, "version": self.version, "title": self.title,
                "status": self.status.value, "target": self.target.value, "techniques": list(self.techniques),
                "mass": {"fraud": self.mass_fraud, "legit": self.mass_legit}}


# --- errors -----------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class RuleProblem:
    line: Optional[int]  # 1-based; None when no position applies
    path: str  # e.g. "detection.big.tx.amount|gte"
    message: str

    def __str__(self) -> str:
        where = f"line {self.line}" if self.line is not None else "document"
        return f"{where}: {self.path}: {self.message}" if self.path else f"{where}: {self.message}"

    def to_dict(self) -> dict[str, Any]:
        return {"line": self.line, "field": self.path, "reason": self.message}


class RuleValidationError(ValueError):
    """Invalid Detection_Rule document; ``problems`` holds every problem found (Req 31.2)."""

    def __init__(self, problems: Iterable[RuleProblem]) -> None:
        self.problems = tuple(problems)
        super().__init__("; ".join(str(p) for p in self.problems) or "invalid Detection_Rule")


class RuleNotFound(LookupError):
    pass


class RuleStateError(RuntimeError):
    """The requested transition is not allowed (e.g. activating a version that was not backtested)."""


# --- condition expressions ------------------------------------------------------------------------

_TOKEN = re.compile(r"\s*(?:(\()|(\))|([A-Za-z_][A-Za-z0-9_]*)|(\S))")


class ConditionError(ValueError):
    pass


def _tokens(text: str) -> list[str]:
    out = []
    for m in _TOKEN.finditer(text):
        if m.group(4) is not None:
            raise ConditionError(f"unexpected character {m.group(4)!r}")
        tok = m.group(1) or m.group(2) or m.group(3)
        if tok:
            out.append(tok)
    return out


def parse_condition(text: str, names: Iterable[str]) -> Cond:
    """Parse ``and`` / ``or`` / ``not`` / parentheses over selection ``names``
    (precedence not > and > or). Raises ``ConditionError``."""
    if not isinstance(text, str) or not text.strip():
        raise ConditionError("condition must be a non-empty expression")
    if len(text) > MAX_CONDITION_LEN:
        raise ConditionError(f"condition longer than {MAX_CONDITION_LEN} characters")
    known = set(names)
    toks = _tokens(text)
    pos = 0

    def peek() -> Optional[str]:
        return toks[pos] if pos < len(toks) else None

    def take() -> str:
        nonlocal pos
        t = toks[pos]
        pos += 1
        return t

    def expr(depth: int) -> Cond:
        if depth > MAX_CONDITION_DEPTH:
            raise ConditionError(f"condition nested deeper than {MAX_CONDITION_DEPTH}")
        parts = [conj(depth)]
        while peek() == "or":
            take()
            parts.append(conj(depth))
        return parts[0] if len(parts) == 1 else ("or", tuple(parts))

    def conj(depth: int) -> Cond:
        parts = [neg(depth)]
        while peek() == "and":
            take()
            parts.append(neg(depth))
        return parts[0] if len(parts) == 1 else ("and", tuple(parts))

    def neg(depth: int) -> Cond:
        if peek() == "not":
            take()
            if depth + 1 > MAX_CONDITION_DEPTH:
                raise ConditionError(f"condition nested deeper than {MAX_CONDITION_DEPTH}")
            return ("not", neg(depth + 1))
        return atom(depth)

    def atom(depth: int) -> Cond:
        t = peek()
        if t is None:
            raise ConditionError("condition ends unexpectedly")
        if t == "(":
            take()
            inner = expr(depth + 1)
            if peek() != ")":
                raise ConditionError("missing ')'")
            take()
            return inner
        if t == ")" or t in KEYWORDS:
            raise ConditionError(f"unexpected {t!r}")
        take()
        if t not in known:
            raise ConditionError(f"unknown selection {t!r}")
        return ("sel", t)

    tree = expr(0)
    if pos != len(toks):
        raise ConditionError(f"unexpected {toks[pos]!r}")
    return tree


def eval_condition(c: Cond, sel: Callable[[str], bool]) -> bool:
    op = c[0]
    if op == "sel":
        return sel(c[1])
    if op == "not":
        return not eval_condition(c[1], sel)
    if op == "and":
        return all(eval_condition(x, sel) for x in c[1])
    return any(eval_condition(x, sel) for x in c[1])


def _num(v: Any) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def match_item(item: Item, value: Any) -> bool:
    if item.modifier == "exists":
        return (value is not None) is bool(item.values[0])
    if value is None:
        return False
    m = item.modifier
    for v in item.values:
        if m == "eq":
            if isinstance(v, bool) or isinstance(value, bool):
                if isinstance(v, bool) and isinstance(value, bool) and v is value:
                    return True
            elif value == v:
                return True
        elif m in ("gt", "gte", "lt", "lte"):
            if _num(value) and _num(v):
                x = float(value)
                if (m == "gt" and x > v) or (m == "gte" and x >= v) or (m == "lt" and x < v) or (m == "lte" and x <= v):
                    return True
        elif isinstance(value, str) and isinstance(v, str):
            if (m == "contains" and v in value) or (m == "startswith" and value.startswith(v)) or (
                    m == "endswith" and value.endswith(v)):
                return True
    return False


# --- field resolution ------------------------------------------------------------------------------


def tx_getter(ev: Any, dna: Union[Any, Callable[[], Any], None]) -> Callable[[str], Any]:
    """``get(field)`` for a Transaction_Event and the source account's pre-event DNAView
    (or a zero-argument callable returning it, called at most once)."""
    cache: list[Any] = []

    def view() -> Any:
        if not cache:
            cache.append(dna() if callable(dna) else dna)
        return cache[0]

    def get(f: str) -> Any:
        root, _, name = f.partition(".")
        if root == "tx":
            if name == "amount":
                return float(ev.amount)
            if name == "hour":
                return ev.ts.astimezone(timezone.utc).hour
            if name == "weekday":
                return ev.ts.astimezone(timezone.utc).weekday()
            return getattr(ev, name, None)
        d = view()
        if d is None:
            return None
        if name == "count":
            return d.count
        if name == "amount_mean":
            return d.amount.mean if d.amount.w > 0 else None
        if name == "amount_std":
            return math.sqrt(max(0.0, d.amount.var)) if d.amount.w > 0 else None
        if name == "amount_ratio":
            return float(ev.amount) / d.amount.mean if d.amount.w > 0 and d.amount.mean > 0 else None
        if name == "iat_mean":
            return d.iat.mean if d.iat.w > 0 else None
        if name == "tx_24h":
            return d.recent_count(ev.ts - timedelta(hours=24))
        if name == "counterparties":
            return len(d.counterparties)
        if name == "known_counterparty":
            return ev.dst in d.counterparties
        if name == "seconds_since_last":
            return None if d.t_last is None else (ev.ts - d.t_last).total_seconds()
        if name == "known_channel":
            return None if ev.channel is None else ev.channel in d.channel_counts
        return None

    return get


def auth_getter(ev: Any, new_device: Optional[bool] = None) -> Callable[[str], Any]:
    def get(f: str) -> Any:
        name = f.partition(".")[2]
        if name == "event_type":
            return ev.event_type.value
        if name == "hour":
            return ev.ts.astimezone(timezone.utc).hour
        if name == "new_device":
            return new_device
        return getattr(ev, name, None)

    return get


# --- YAML: safe compose with line numbers ---------------------------------------------------------


@dataclass(slots=True)
class _Y:
    v: Any  # dict[str, _Y] | list[_Y] | scalar
    line: int


_MAP_TAG = "tag:yaml.org,2002:map"
_SEQ_TAG = "tag:yaml.org,2002:seq"
_SCALAR_TAGS = frozenset({"tag:yaml.org,2002:str", "tag:yaml.org,2002:int", "tag:yaml.org,2002:float",
                          "tag:yaml.org,2002:bool", "tag:yaml.org,2002:null"})


def _line(node: Node) -> int:
    return node.start_mark.line + 1


def _to_y(node: Node, ctor: SafeConstructor, seen: set[int], problems: list[RuleProblem], path: str) -> Optional[_Y]:
    if id(node) in seen:
        problems.append(RuleProblem(_line(node), path, "YAML aliases are not allowed"))
        return None
    seen.add(id(node))
    if isinstance(node, MappingNode):
        if node.tag != _MAP_TAG:
            problems.append(RuleProblem(_line(node), path, f"unsupported YAML tag {node.tag!r}"))
            return None
        out: dict[str, _Y] = {}
        for k, v in node.value:
            if k.tag == "tag:yaml.org,2002:merge":
                problems.append(RuleProblem(_line(k), path, "YAML merge keys are not allowed"))
                continue
            if not isinstance(k, ScalarNode) or k.tag != "tag:yaml.org,2002:str":
                problems.append(RuleProblem(_line(k), path, "mapping keys must be plain strings"))
                continue
            key = str(k.value)
            sub = f"{path}.{key}" if path else key
            if key in out:
                problems.append(RuleProblem(_line(k), sub, "duplicate key"))
                continue
            y = _to_y(v, ctor, seen, problems, sub)
            if y is not None:
                out[key] = _Y(y.v, _line(k))  # report a value at its key's line
        return _Y(out, _line(node))
    if isinstance(node, SequenceNode):
        if node.tag != _SEQ_TAG:
            problems.append(RuleProblem(_line(node), path, f"unsupported YAML tag {node.tag!r}"))
            return None
        items = []
        for n, v in enumerate(node.value):
            y = _to_y(v, ctor, seen, problems, f"{path}[{n}]")
            if y is not None:
                items.append(y)
        return _Y(items, _line(node))
    if node.tag not in _SCALAR_TAGS:
        if node.tag.startswith("tag:yaml.org,2002:"):
            kind = node.tag[len("tag:yaml.org,2002:"):]
            problems.append(RuleProblem(_line(node), path, f"unsupported value type {kind!r}; quote it to use a string"))
        else:
            problems.append(RuleProblem(_line(node), path, f"unsupported YAML tag {node.tag!r}"))
        return None
    try:
        val = ctor.construct_object(node, deep=True)
    except ConstructorError as e:
        problems.append(RuleProblem(_line(node), path, f"cannot read value: {e.problem or e}"))
        return None
    return _Y(val, _line(node))


def _compose(text: Union[str, bytes]) -> _Y:
    if isinstance(text, (bytes, bytearray)):
        if len(text) > MAX_RULE_BYTES:
            raise RuleValidationError([RuleProblem(None, "", f"document larger than {MAX_RULE_BYTES} bytes")])
        try:
            text = bytes(text).decode("utf-8")
        except UnicodeDecodeError as e:
            raise RuleValidationError([RuleProblem(None, "", f"not UTF-8 text: {e.reason}")]) from None
    if not isinstance(text, str):
        raise RuleValidationError([RuleProblem(None, "", "document must be text")])
    if len(text.encode("utf-8")) > MAX_RULE_BYTES:
        raise RuleValidationError([RuleProblem(None, "", f"document larger than {MAX_RULE_BYTES} bytes")])
    try:
        node = yaml.compose(text, Loader=yaml.SafeLoader)
    except yaml.MarkedYAMLError as e:
        mark = e.problem_mark or e.context_mark
        cause = " ".join(x for x in (e.context, e.problem) if x) or str(e)
        raise RuleValidationError([RuleProblem(None if mark is None else mark.line + 1, "",
                                               f"YAML syntax error: {cause}")]) from None
    except yaml.YAMLError as e:
        raise RuleValidationError([RuleProblem(None, "", f"YAML error: {e}")]) from None
    if node is None:
        raise RuleValidationError([RuleProblem(1, "", "empty document")])
    problems: list[RuleProblem] = []
    y = _to_y(node, SafeConstructor(), set(), problems, "")
    if problems or y is None:
        raise RuleValidationError(problems)
    return y


# --- validation -----------------------------------------------------------------------------------


def _check_value(v: Any, spec: FieldSpec, modifier: str) -> Optional[str]:
    if isinstance(v, (dict, list)) or v is None:
        return "values must be scalars (string, number or boolean)"
    if spec.kind == "number" and modifier != "exists":
        if not _num(v):
            return f"expected a number, got {v!r}"
        if not math.isfinite(float(v)):
            return "numbers must be finite"
    elif spec.kind in ("string", "enum"):
        if not isinstance(v, str):
            return f"expected a string, got {v!r}"
        if len(v) > MAX_STRING:
            return f"strings are limited to {MAX_STRING} characters"
        if spec.kind == "enum" and v not in spec.choices:
            return f"{v!r} is not one of {', '.join(spec.choices)}"
    elif spec.kind == "bool" and not isinstance(v, bool):
        return f"expected true or false, got {v!r}"
    return None


def _items(sel: _Y, sname: str, target: RuleTarget, problems: list[RuleProblem]) -> tuple[Item, ...]:
    fields = FIELDS[target]
    out = []
    for key, y in sel.v.items():
        path = f"detection.{sname}.{key}"
        fld, sep, mod = key.partition("|")
        mod = mod if sep else "eq"
        if "|" in mod:
            problems.append(RuleProblem(y.line, path, "only one modifier is allowed"))
            continue
        if references_ground_truth(fld):
            problems.append(RuleProblem(y.line, path, f"field {fld!r} references a Ground_Truth_Store field; "
                                                      "Detection_Rules have no access to the Ground_Truth_Store"))
            continue
        spec = fields.get(fld)
        if spec is None:
            roots = sorted({f.partition('.')[0] + '.*' for f in fields})
            problems.append(RuleProblem(y.line, path, f"unknown field {fld!r} for target {target.value} "
                                                      f"(allowed: {', '.join(roots)}; see the rule schema)"))
            continue
        if mod not in ALL_MODIFIERS or mod not in MODIFIERS[spec.kind]:
            problems.append(RuleProblem(y.line, path, f"modifier {mod!r} is not allowed for a {spec.kind} field "
                                                      f"(allowed: {', '.join(sorted(MODIFIERS[spec.kind] - {'eq'}))})"))
            continue
        raw = y.v
        if mod == "exists":
            if not isinstance(raw, bool):
                problems.append(RuleProblem(y.line, path, "exists takes true or false"))
                continue
            out.append(Item(fld, mod, (raw,)))
            continue
        vals = [z.v for z in raw] if isinstance(raw, list) else [raw]
        if not vals:
            problems.append(RuleProblem(y.line, path, "value list must not be empty"))
            continue
        if len(vals) > MAX_VALUES:
            problems.append(RuleProblem(y.line, path, f"at most {MAX_VALUES} values"))
            continue
        bad = next((m for v in vals if (m := _check_value(v, spec, mod)) is not None), None)
        if bad is not None:
            problems.append(RuleProblem(y.line, path, bad))
            continue
        out.append(Item(fld, mod, tuple(float(v) if isinstance(v, float) else v for v in vals)))
    return tuple(out)


def _rule_from_y(root: _Y, catalogue: Catalogue) -> DetectionRule:
    problems: list[RuleProblem] = []

    def bad(line: Optional[int], path: str, msg: str) -> None:
        problems.append(RuleProblem(line, path, msg))

    if not isinstance(root.v, dict):
        raise RuleValidationError([RuleProblem(root.line, "", "a Detection_Rule must be a YAML mapping")])
    d: dict[str, _Y] = root.v
    for k, y in d.items():
        if k not in TOP_KEYS:
            bad(y.line, k, f"unknown key (allowed: {', '.join(TOP_KEYS)})")
    for k in REQUIRED_KEYS:
        if k not in d:
            bad(root.line, k, "required key is missing")

    def text(k: str, limit: int, required: bool) -> Optional[str]:
        y = d.get(k)
        if y is None:
            return None
        if y.v is None and not required:
            return None
        if not isinstance(y.v, str) or (required and not y.v.strip()):
            bad(y.line, k, "must be a non-empty string" if required else "must be a string")
            return None
        if len(y.v) > limit:
            bad(y.line, k, f"longer than {limit} characters")
            return None
        return y.v

    rule_id = text("id", 64, True)
    if rule_id is not None and not RULE_ID.match(rule_id):
        bad(d["id"].line, "id", "must match [A-Za-z0-9][A-Za-z0-9_.-]{0,63}")
        rule_id = None
    title = text("title", MAX_TITLE, True)
    description = text("description", MAX_DESCRIPTION, False)

    version = None
    if "version" in d and d["version"].v is not None:
        v = d["version"].v
        if not (isinstance(v, int) and not isinstance(v, bool) and v >= 1):
            bad(d["version"].line, "version", "must be an integer >= 1")
        else:
            version = v

    def enum(k: str, cls: Any, default: Any) -> Any:
        y = d.get(k)
        if y is None:
            return default
        try:
            return cls(y.v)
        except ValueError:
            bad(y.line, k, f"{y.v!r} is not one of {', '.join(m.value for m in cls)}")
            return default

    status = enum("status", RuleStatus, RuleStatus.EXPERIMENTAL)
    target = enum("target", RuleTarget, RuleTarget.TRANSACTION)

    techniques: tuple[str, ...] = ()
    if "techniques" in d:
        y = d["techniques"]
        if not isinstance(y.v, list) or not y.v:
            bad(y.line, "techniques", "must be a non-empty list of HELIX_Technique ids")
        else:
            seen: list[str] = []
            for n, t in enumerate(y.v):
                p = f"techniques[{n}]"
                if not is_technique_id(t.v):
                    bad(t.line, p, f"{t.v!r} is not a HELIX_Technique id (HX-T followed by four digits)")
                elif t.v not in catalogue:
                    bad(t.line, p, f"unknown HELIX_Technique id {t.v!r} (catalogue {catalogue.version})")
                elif t.v in seen:
                    bad(t.line, p, f"duplicate technique {t.v!r}")
                else:
                    seen.append(t.v)
            techniques = tuple(seen)

    selections: list[Selection] = []
    condition: Optional[str] = None
    cond_line = None
    if "detection" in d:
        y = d["detection"]
        if not isinstance(y.v, dict):
            bad(y.line, "detection", "must be a mapping of selections plus a condition")
        else:
            for name, s in y.v.items():
                if name == "condition":
                    if not isinstance(s.v, str):
                        bad(s.line, "detection.condition", "must be a string expression")
                    else:
                        condition, cond_line = s.v, s.line
                    continue
                p = f"detection.{name}"
                if not SELECTION_NAME.match(name) or name in RESERVED:
                    bad(s.line, p, "selection names must match [A-Za-z_][A-Za-z0-9_]* and not be and/or/not")
                    continue
                if not isinstance(s.v, dict) or not s.v:
                    bad(s.line, p, "a selection must be a non-empty mapping of field: value")
                    continue
                items = _items(s, name, target, problems)
                if items and len(items) == len(s.v):
                    selections.append(Selection(name, items))
            if "condition" not in y.v:
                bad(y.line, "detection.condition", "required key is missing")
            if not any(k != "condition" for k in y.v):
                bad(y.line, "detection", "needs at least one selection")
    ast = None
    if condition is not None:
        names = {k for k in d["detection"].v if k != "condition"}
        try:
            ast = parse_condition(condition, names)
        except ConditionError as e:
            bad(cond_line, "detection.condition", str(e))

    mass_f = mass_l = 0.0
    if "mass" in d:
        y = d["mass"]
        if not isinstance(y.v, dict):
            bad(y.line, "mass", "must be a mapping with fraud (and optional legit)")
        else:
            for k, z in y.v.items():
                if k not in ("fraud", "legit"):
                    bad(z.line, f"mass.{k}", "unknown key (allowed: fraud, legit)")
            vals = {}
            for k in ("fraud", "legit"):
                z = y.v.get(k)
                if z is None:
                    if k == "fraud":
                        bad(y.line, "mass.fraud", "required key is missing")
                    vals[k] = 0.0
                    continue
                if not _num(z.v) or not math.isfinite(float(z.v)) or not 0.0 <= float(z.v) <= 1.0:
                    bad(z.line, f"mass.{k}", f"must be a number in [0, 1], got {z.v!r}")
                    vals[k] = 0.0
                    continue
                vals[k] = float(z.v)
            mass_f, mass_l = vals["fraud"], vals["legit"]
            if mass_f + mass_l > 1.0 + TOL:
                bad(y.line, "mass", f"fraud + legit must be <= 1, got {mass_f + mass_l!r}")
            elif "fraud" in y.v and mass_f + mass_l <= 0.0:
                bad(y.line, "mass", "the Mass_Function must not be vacuous (fraud + legit > 0)")

    if problems:
        raise RuleValidationError(sorted(problems, key=lambda p: (p.line or 0, p.path)))
    assert rule_id is not None and title is not None and condition is not None
    return DetectionRule(rule_id=rule_id, title=title, techniques=techniques, selections=tuple(selections),
                         condition=condition, mass_fraud=mass_f, mass_legit=mass_l, status=status, target=target,
                         description=description, version=version, _ast=ast)


def parse_rule(text: Union[str, bytes], catalogue: Optional[Catalogue] = None) -> DetectionRule:
    """Parse and validate one Detection_Rule YAML document (Req 31.1, 31.2, 31.7, 31.9).
    Raises ``RuleValidationError`` with line-numbered problems."""
    return _rule_from_y(_compose(text), catalogue or default_catalogue())


def rule_to_document(rule: DetectionRule) -> dict[str, Any]:
    doc: dict[str, Any] = {"id": rule.rule_id}
    if rule.version is not None:
        doc["version"] = rule.version
    doc["title"] = rule.title
    if rule.description is not None:
        doc["description"] = rule.description
    doc["status"] = rule.status.value
    doc["target"] = rule.target.value
    doc["techniques"] = list(rule.techniques)
    det: dict[str, Any] = {}
    for s in rule.selections:
        det[s.name] = {i.key: (i.values[0] if len(i.values) == 1 else list(i.values)) for i in s.items}
    det["condition"] = rule.condition
    doc["detection"] = det
    doc["mass"] = {"fraud": rule.mass_fraud, "legit": rule.mass_legit}
    return doc


def format_rule(rule: DetectionRule) -> str:
    """Canonical YAML document of a Detection_Rule (Req 31.8)."""
    return yaml.safe_dump(rule_to_document(rule), sort_keys=False, allow_unicode=True,
                          default_flow_style=False, width=10_000)


# --- backtest ----------------------------------------------------------------------------------------


class BacktestSource(Protocol):
    def now(self) -> datetime: ...

    def transactions(self, start: datetime, end: datetime) -> Sequence[Any]: ...

    def auth_events(self, start: datetime, end: datetime) -> Sequence[tuple[Any, Optional[bool]]]: ...

    def confirmed_fraud_ids(self) -> set[str]: ...


@dataclass
class StaticBacktestSource:
    """In-memory events (tests, offline evaluation)."""

    txs: Sequence[Any] = ()
    auths: Sequence[tuple[Any, Optional[bool]]] = ()
    confirmed: set[str] = field(default_factory=set)
    at: Optional[datetime] = None
    notes: tuple[str, ...] = ()

    def now(self) -> datetime:
        if self.at is not None:
            return self.at
        ts = [e.ts for e in self.txs] + [a.ts for a, _ in self.auths]
        return max(ts) if ts else datetime.now(timezone.utc)

    def transactions(self, start: datetime, end: datetime) -> Sequence[Any]:
        return [e for e in self.txs if start <= e.ts <= end]

    def auth_events(self, start: datetime, end: datetime) -> Sequence[tuple[Any, Optional[bool]]]:
        return [(a, nd) for a, nd in self.auths if start <= a.ts <= end]

    def confirmed_fraud_ids(self) -> set[str]:
        return set(self.confirmed)


class HelixBacktestSource:
    """Stored events of a running pipeline. Transaction_Events come from Storage when present
    (flushed first), otherwise from the pipeline's in-memory event cache (Graph_Window and Case
    members only). Auth_Events come from the in-memory auth history (bounded by
    ``soc.auth_retention_s``). CONFIRMED_FRAUD ids are the tx ids of Cases with that label plus
    the Auth_Event ids recorded in their Evidence_Chains."""

    def __init__(self, helix: Any, storage: Any = None) -> None:
        self.helix = helix
        self.storage = storage if storage is not None else getattr(helix, "storage", None)

    @property
    def notes(self) -> tuple[str, ...]:
        out = []
        if self.storage is None:
            out.append("no Storage: Transaction_Events limited to the in-memory Graph_Window and Case members")
        if getattr(self.helix, "auth", None) is None:
            out.append("cyber fusion disabled: no Auth_Events recorded")
        else:
            out.append("Auth_Events limited to the in-memory auth history (soc.auth_retention_s)")
        return tuple(out)

    def now(self) -> datetime:
        return self.helix.clock.now()

    def transactions(self, start: datetime, end: datetime) -> Sequence[Any]:
        st = self.storage
        if st is not None:
            try:
                st.flush()
            except Exception:  # noqa: BLE001 - a failed flush only hides the newest rows
                log.exception("rule backtest: storage flush failed")
            return [se.event for se in st.events(start, end)]
        infos: dict[str, Any] = {}
        for src in (getattr(self.helix, "_recent", {}), getattr(self.helix, "_pinned", {})):
            for tx, i in list(src.items()):
                if start <= i.ev.ts <= end:
                    infos[tx] = i.ev
        return sorted(infos.values(), key=lambda e: (e.ts, e.tx_id))

    def auth_events(self, start: datetime, end: datetime) -> Sequence[tuple[Any, Optional[bool]]]:
        hist = getattr(self.helix, "auth", None)
        if hist is None:
            return []
        out = [(r.event, r.new_device) for a in hist.accounts() for r in hist.events(a) if start <= r.event.ts <= end]
        return sorted(out, key=lambda p: (p[0].ts, p[0].event_id))

    def confirmed_fraud_ids(self) -> set[str]:
        ids: set[str] = set()
        chains = getattr(self.helix, "chains", {})
        for case in list(self.helix.cases.cases.values()):
            lab = getattr(case.label, "value", case.label)
            if lab != "CONFIRMED_FRAUD":
                continue
            ids |= set(case.tx_ids)
            chain = chains.get(case.case_id)
            for e in chain or ():
                if str(getattr(e.kind, "value", e.kind)) == "AUTH_EVENT" and e.detail.get("event_id"):
                    ids.add(str(e.detail["event_id"]))
        return ids


@dataclass(frozen=True, slots=True)
class BacktestReport:
    rule_id: str
    version: int
    window_start: datetime
    window_end: datetime
    transactions_scanned: int
    auth_events_scanned: int
    hits: int
    estimated_false_positives: int  # hits on events outside CONFIRMED_FRAUD Cases
    sample_hits: tuple[str, ...]
    ran_at: datetime
    notes: tuple[str, ...] = ()

    @property
    def window_s(self) -> float:
        return (self.window_end - self.window_start).total_seconds()

    def to_dict(self) -> dict[str, Any]:
        return {"rule_id": self.rule_id, "version": self.version,
                "window_start": self.window_start.isoformat(), "window_end": self.window_end.isoformat(),
                "window_s": self.window_s, "transactions_scanned": self.transactions_scanned,
                "auth_events_scanned": self.auth_events_scanned, "hits": self.hits,
                "estimated_false_positives": self.estimated_false_positives,
                "sample_hits": list(self.sample_hits), "ran_at": self.ran_at.isoformat(), "notes": list(self.notes)}


def backtest_rule(rule: DetectionRule, source: BacktestSource, window_s: float = DEFAULT_BACKTEST_WINDOW_S, *,
                  half_life_days: float = 30.0, ran_at: Optional[datetime] = None) -> BacktestReport:
    """Replay ``rule`` over the source's events in ``[now - window, now]`` (Req 31.4)."""
    if not (math.isfinite(window_s) and window_s > 0):
        raise ValueError(f"backtest window must be a positive number of seconds, got {window_s!r}")
    end = source.now()
    start = end - timedelta(seconds=float(window_s))
    confirmed = source.confirmed_fraud_ids()
    hits: list[str] = []
    n_tx = n_auth = 0
    notes = list(getattr(source, "notes", ()) or ())
    if rule.target is RuleTarget.TRANSACTION:
        from helix.memory import ProfileStore

        txs = sorted(source.transactions(start, end), key=lambda e: (e.ts, e.tx_id))
        n_tx = len(txs)
        profiles = ProfileStore(half_life_days=half_life_days)
        uses_dna = any(f.startswith("dna.") for f in rule.fields)
        for ev in txs:
            if rule.matches(tx_getter(ev, lambda ev=ev: profiles.view(ev.src))):
                hits.append(ev.tx_id)
            if uses_dna:
                profiles.apply(ev)
        if uses_dna:
            notes.append("dna.* fields use a Behavioural_DNA rebuilt from the backtest window only")
    else:
        auths = source.auth_events(start, end)
        n_auth = len(auths)
        for a, nd in auths:
            if rule.matches(auth_getter(a, nd)):
                hits.append(a.event_id)
    fp = sum(1 for h in hits if h not in confirmed)
    return BacktestReport(rule.rule_id, int(rule.version or 0), start, end, n_tx, n_auth, len(hits), fp,
                          tuple(hits[:MAX_LISTED]), ran_at or datetime.now(timezone.utc), tuple(notes))


# --- engine ----------------------------------------------------------------------------------------


@dataclass
class RuleVersion:
    rule: DetectionRule  # version set
    text: str  # the document as submitted
    source: str  # "api" or a file path
    accepted_at: datetime
    backtest: Optional[BacktestReport] = None
    activated_at: Optional[datetime] = None
    activated_by: Optional[str] = None

    @property
    def version(self) -> int:
        return int(self.rule.version or 0)

    def to_dict(self, full: bool = True) -> dict[str, Any]:
        d: dict[str, Any] = {
            **self.rule.summary(), "source": self.source, "accepted_at": self.accepted_at.isoformat(),
            "backtested": self.backtest is not None,
            "activated_at": None if self.activated_at is None else self.activated_at.isoformat(),
            "activated_by": self.activated_by,
        }
        if full:
            d["yaml"] = format_rule(self.rule)
            d["backtest"] = None if self.backtest is None else self.backtest.to_dict()
        return d


@dataclass(frozen=True, slots=True)
class RuleHit:
    rule: DetectionRule
    matched: tuple[str, ...]  # tx id (transaction rule) or matching Auth_Event ids (auth rule)

    def contribution(self, weight: float = 1.0) -> Contribution:
        return Contribution(self.rule.contributor_id, ContributorType.RULE, self.rule.mass, weight)

    def result(self, tx_id: str) -> SensorResult:
        r = self.rule
        feats = {"rule_id": r.rule_id, "version": int(r.version or 0), "title": r.title,
                 "techniques": "|".join(r.techniques), "target": r.target.value,
                 "matched": "|".join(self.matched[:MAX_LISTED])}
        return SensorResult(tx_id, r.contributor_id, SensorOutcome.SUCCESS, SensorOutput(r.mass, feats, None), 0.0)


@dataclass(frozen=True, slots=True)
class DirectoryResult:
    path: str
    status: str  # accepted | rejected | unchanged
    rule_id: Optional[str] = None
    version: Optional[int] = None
    problems: tuple[RuleProblem, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {"path": self.path, "status": self.status, "rule_id": self.rule_id, "version": self.version,
                "errors": [p.to_dict() for p in self.problems]}


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class RuleEngine:
    """Validates, versions, backtests, activates and evaluates Detection_Rules (Req 31)."""

    def __init__(
        self,
        catalogue: Optional[Catalogue] = None,
        *,
        source: Optional[BacktestSource] = None,
        backtest_window_s: float = DEFAULT_BACKTEST_WINDOW_S,
        half_life_days: float = 30.0,
        weights: Optional[Mapping[str, float]] = None,
        clock: Callable[[], datetime] = _utcnow,
    ) -> None:
        if not (math.isfinite(backtest_window_s) and backtest_window_s > 0):
            raise ValueError(f"backtest_window_s must be positive, got {backtest_window_s!r}")
        self.catalogue = catalogue or default_catalogue()
        self.source = source
        self.backtest_window_s = float(backtest_window_s)
        self.half_life_days = float(half_life_days)
        self.weights: Mapping[str, float] = weights if weights is not None else {}
        self.clock = clock
        self._versions: dict[str, list[RuleVersion]] = {}
        self._active: Mapping[str, RuleVersion] = MappingProxyType({})
        self._lock = threading.RLock()
        self._dir_hashes: dict[str, str] = {}
        #: Audit_Ledger (Req 35.1): RULE_CHANGE on submit / activate / deactivate. None = off.
        self.audit: Any = None
        self.eval_errors = 0
        self.hits = 0

    # -- authoring -------------------------------------------------------------------

    def parse(self, text: Union[str, bytes]) -> DetectionRule:
        return parse_rule(text, self.catalogue)

    def submit(self, text: Union[str, bytes], *, source: str = "api", backtest: bool = True) -> RuleVersion:
        """Validate and store ``text`` as the next version of its rule (Req 31.1-31.3), then
        backtest it when a source is configured (Req 31.4). Raises ``RuleValidationError``;
        nothing changes on failure."""
        rule = self.parse(text)
        raw = text.decode("utf-8") if isinstance(text, (bytes, bytearray)) else text
        with self._lock:
            versions = self._versions.setdefault(rule.rule_id, [])
            n = (versions[-1].version if versions else 0) + 1
            rv = RuleVersion(rule.with_version(n), raw, source, self.clock())
            versions.append(rv)
        self._audit(source, rule.rule_id, "submitted", n)
        if backtest and self.source is not None:
            self.backtest(rule.rule_id, n)
        return rv

    def _audit(self, actor: str, rule_id: str, action: str, version: Optional[int]) -> None:
        if self.audit is not None:
            from .ledger import audit_rule_change  # never raises (logged on failure)

            audit_rule_change(self.audit, str(actor or "system"), rule_id, action, version)

    def load_directory(self, path: Union[str, Path]) -> list[DirectoryResult]:
        """Submit every ``*.yaml`` / ``*.yml`` file in ``path`` whose content changed since the
        last scan (Req 31.1). Call again to pick up edits without a restart."""
        p = Path(path)
        if not p.is_dir():
            raise FileNotFoundError(f"rules directory not found: {p}")
        out = []
        for f in sorted([*p.glob("*.yaml"), *p.glob("*.yml")]):
            key = str(f.resolve())
            try:
                if f.stat().st_size > MAX_RULE_BYTES:
                    out.append(DirectoryResult(str(f), "rejected", problems=(
                        RuleProblem(None, "", f"document larger than {MAX_RULE_BYTES} bytes"),)))
                    continue
                data = f.read_bytes()
            except OSError as e:
                out.append(DirectoryResult(str(f), "rejected", problems=(RuleProblem(None, "", str(e)),)))
                continue
            h = hashlib.sha256(data).hexdigest()
            if self._dir_hashes.get(key) == h:
                out.append(DirectoryResult(str(f), "unchanged"))
                continue
            self._dir_hashes[key] = h
            try:
                rv = self.submit(data, source=str(f))
            except RuleValidationError as e:
                out.append(DirectoryResult(str(f), "rejected", problems=e.problems))
                continue
            out.append(DirectoryResult(str(f), "accepted", rv.rule.rule_id, rv.version))
        return out

    # -- queries -----------------------------------------------------------------------

    def rule_ids(self) -> list[str]:
        with self._lock:
            return sorted(self._versions)

    def versions(self, rule_id: str) -> list[RuleVersion]:
        with self._lock:
            if rule_id not in self._versions:
                raise RuleNotFound(rule_id)
            return list(self._versions[rule_id])

    def get(self, rule_id: str, version: Optional[int] = None) -> RuleVersion:
        vs = self.versions(rule_id)
        if version is None:
            return vs[-1]
        for v in vs:
            if v.version == version:
                return v
        raise RuleNotFound(f"{rule_id} version {version}")

    def active_version(self, rule_id: str) -> Optional[int]:
        rv = self._active.get(rule_id)
        return None if rv is None else rv.version

    def active_rules(self) -> list[DetectionRule]:
        active = self._active
        return [active[k].rule for k in sorted(active)]

    def summary(self) -> list[dict[str, Any]]:
        out = []
        for rid in self.rule_ids():
            vs = self.versions(rid)
            latest = vs[-1]
            out.append({**latest.rule.summary(), "latest_version": latest.version,
                        "active_version": self.active_version(rid),
                        "versions": [v.to_dict(full=False) for v in vs]})
        return out

    # -- lifecycle -----------------------------------------------------------------------

    def backtest(self, rule_id: str, version: Optional[int] = None, *, window_s: Optional[float] = None,
                 source: Optional[BacktestSource] = None) -> BacktestReport:
        rv = self.get(rule_id, version)
        src = source or self.source
        if src is None:
            raise RuleStateError("no backtest source configured")
        rep = backtest_rule(rv.rule, src, self.backtest_window_s if window_s is None else float(window_s),
                            half_life_days=self.half_life_days, ran_at=self.clock())
        with self._lock:
            rv.backtest = rep
        return rep

    def activate(self, rule_id: str, version: Optional[int] = None, *, by: str = "investigator") -> RuleVersion:
        """Make ``version`` (default latest) the active version of ``rule_id`` (Req 31.5).
        It applies from the next processed event. Requires a backtest (Req 31.4)."""
        with self._lock:
            rv = self.get(rule_id, version)
            if rv.backtest is None:
                raise RuleStateError(f"{rule_id} version {rv.version} has not been backtested")
            rv.activated_at, rv.activated_by = self.clock(), by
            self._active = MappingProxyType({**self._active, rule_id: rv})
        self._audit(by, rule_id, "activated", rv.version)
        return rv

    def deactivate(self, rule_id: str, *, by: str = "investigator") -> Optional[int]:
        with self._lock:
            self.versions(rule_id)  # RuleNotFound for an unknown id
            old = self._active.get(rule_id)
            if old is not None:
                self._active = MappingProxyType({k: v for k, v in self._active.items() if k != rule_id})
        if old is not None:
            self._audit(by, rule_id, "deactivated", old.version)
        return None if old is None else old.version

    # -- live evaluation (Req 31.5, 31.6) ---------------------------------------------------

    def evaluate(self, ev: Any, dna: Any = None, auth: Any = None,
                 auth_window: Optional[timedelta] = None) -> list[RuleHit]:
        """Active rules matching Transaction_Event ``ev`` (``dna``: DNAView or zero-argument
        callable; ``auth``: an ``AuthHistory`` or None), in rule-id order."""
        active = self._active
        if not active:
            return []
        hits = []
        get = tx_getter(ev, dna)
        recs: Optional[list[Any]] = None
        for rid in sorted(active):
            rule = active[rid].rule
            try:
                if rule.target is RuleTarget.TRANSACTION:
                    if rule.matches(get):
                        hits.append(RuleHit(rule, (ev.tx_id,)))
                elif auth is not None:
                    if recs is None:
                        recs = list(auth.in_window(ev.src, ev.ts, auth_window))
                    ids = tuple(r.event.event_id for r in recs if rule.matches(auth_getter(r.event, r.new_device)))
                    if ids:
                        hits.append(RuleHit(rule, ids))
            except Exception:  # noqa: BLE001 - one broken rule must not stop detection
                self.eval_errors += 1
                log.exception("rule %s failed on %s", rid, getattr(ev, "tx_id", "?"))
        self.hits += len(hits)
        return hits

    def weight(self, contributor_id: str) -> float:
        w = self.weights.get(contributor_id, 1.0)
        try:
            w = float(w)
        except (TypeError, ValueError):
            return 1.0
        return min(1.0, max(0.0, w)) if math.isfinite(w) else 1.0

    def extend_stage1(self, ev: Any, stage1: FusedAssessment, results: Sequence[SensorResult], *,
                      profiles: Any = None, auth: Any = None,
                      high_conflict: float = 0.95) -> tuple[FusedAssessment, list[SensorResult]]:
        """Stage-1 assessment with the matching active rules' masses fused in, plus one result
        per hit for the Evidence_Chain. Unchanged (same objects) when nothing matches."""
        if not self._active:
            return stage1, list(results)
        dna = (lambda: profiles.view(ev.src)) if profiles is not None else None
        hits = self.evaluate(ev, dna, auth)
        if not hits:
            return stage1, list(results)
        contribs = [h.contribution(self.weight(h.rule.contributor_id)) for h in hits]
        fa, _ = fuse([*stage1.contributions, *contribs], high_conflict, tx_id=stage1.tx_id, stage=1)
        return fa, [*results, *(h.result(ev.tx_id) for h in hits)]


def attach_rules(helix: Any, engine: Optional[RuleEngine] = None, *, storage: Any = None) -> RuleEngine:
    """Wire a ``RuleEngine`` into a ``Helix`` pipeline (needs ``soc.enabled``). Configures the
    backtest source, DNA half-life and learned weights. Idempotent for the same engine."""
    if not getattr(helix, "soc_enabled", False):
        raise ValueError("Detection_Rules need cyber fusion (soc.enabled = true)")
    eng = engine if engine is not None else rule_engine_for(helix, storage)
    if eng.source is None:
        eng.source = HelixBacktestSource(helix, storage)
    eng.half_life_days = float(helix.cfg.memory.half_life_days)
    other = getattr(getattr(helix, "learner", None), "other_weights", None)
    if other is not None:
        eng.weights = other
    helix.rules = eng
    return eng


def rule_engine_for(helix: Any, storage: Any = None, **kw: Any) -> RuleEngine:
    """A ``RuleEngine`` with a backtest source over ``helix`` (not attached)."""
    eng = RuleEngine(source=HelixBacktestSource(helix, storage), **kw)
    eng.half_life_days = float(helix.cfg.memory.half_life_days)
    return eng


__all__ = [
    "AUTH_FIELDS",
    "CONTRIBUTOR_PREFIX",
    "DEFAULT_BACKTEST_WINDOW_S",
    "DNA_FIELDS",
    "FIELDS",
    "GT_FIELD_NAMES",
    "MAX_RULE_BYTES",
    "MODIFIERS",
    "TX_FIELDS",
    "BacktestReport",
    "BacktestSource",
    "ConditionError",
    "DetectionRule",
    "DirectoryResult",
    "HelixBacktestSource",
    "Item",
    "RuleEngine",
    "RuleHit",
    "RuleNotFound",
    "RuleProblem",
    "RuleStateError",
    "RuleStatus",
    "RuleTarget",
    "RuleValidationError",
    "RuleVersion",
    "Selection",
    "StaticBacktestSource",
    "attach_rules",
    "auth_getter",
    "backtest_rule",
    "eval_condition",
    "format_rule",
    "match_item",
    "parse_condition",
    "parse_rule",
    "references_ground_truth",
    "rule_engine_for",
    "rule_to_document",
    "tx_getter",
]
