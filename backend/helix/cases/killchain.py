"""Kill-chain reconstruction (Req 29): stage per Evidence_Chain entry, per-Case progression.

Design decisions:
- Stages are ``KillChainStage`` (``helix.config``), ordered by declaration (Req 29.1).
- Each entry gets exactly one stage (Req 29.2) from *stage keys* looked up in the configured
  mapping (``soc.kill_chain_mapping``):

  ====================================  =====================================================
  entry                                 stage key(s)
  ====================================  =====================================================
  SENSOR contributor, ``w·m(F) > 0``    ``sensor.<name>``; the ``auth`` sensor uses
                                        ``auth.<signal>`` for each signal in its features
  SENSOR contributor, no FRAUD mass     ``evidence.non_indicative``
  MOTIF                                 ``motif.<MotifType>``
  other contributor types               ``<ctype>.<contributor_id>`` (antibody, rule, ...)
  AUTH_EVENT, suspicious                ``auth.<signal>`` (failed_logins, new_device, ...)
  AUTH_EVENT, linked only               ``auth.linked``
  any other record kind                 ``record.<KIND>``
  ====================================  =====================================================

  With several keys the most advanced mapped stage wins. If no key is mapped the entry gets
  ``soc.kill_chain_default_stage`` and is marked unmapped (Req 29.3).
- Because stages are a pure function of the entry and the mapping, re-deriving them from a
  stored chain gives the same answer, and a Case's most advanced stage is by construction
  the maximum over its entries (Req 29.5, the kill-chain maximum property).
- ``KillChain.stages`` lists the stages present in stage order, each with the earliest entry
  timestamp at that stage (Req 29.4). AUTH_EVENT entries carry the Auth_Event's own
  timestamp, so credential access shows when it happened, not when money moved.
- The Kill_Chain_Multiplier (``soc.kill_chain_multiplier``, at least 1) applies to
  Expected_Loss when the most advanced stage is CASH_OUT or LAYERING (Req 29.6). Expected_Loss
  is non-negative and Case_Priority is Expected_Loss minus a constant cost, so the multiplied
  priority is never lower (Req 29.7). Multipliers below 1 or non-finite are rejected.
"""

from __future__ import annotations

import math
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import datetime
from decimal import ROUND_HALF_EVEN, Decimal
from typing import Any, Optional, Union

from helix.config.schema import DEFAULT_KILL_CHAIN_MAPPING, KILL_CHAIN_ORDER, KillChainStage

_RANK: dict[KillChainStage, int] = {s: i for i, s in enumerate(KILL_CHAIN_ORDER)}
MULTIPLIED_STAGES: frozenset[KillChainStage] = frozenset({KillChainStage.CASH_OUT, KillChainStage.LAYERING})
DEFAULT_STAGE = KillChainStage.ACCOUNT_TAKEOVER
_CENT = Decimal("0.01")
_CONTRIBUTOR_KINDS = ("CONTRIBUTOR", "MOTIF")


def stage_rank(s: Union[KillChainStage, str]) -> int:
    return _RANK[KillChainStage(s)]


def max_stage(stages: Iterable[Union[KillChainStage, str]]) -> Optional[KillChainStage]:
    """Most advanced stage, None for an empty iterable."""
    return max((KillChainStage(s) for s in stages), key=stage_rank, default=None)


@dataclass(frozen=True, slots=True)
class StageAssignment:
    stage: KillChainStage
    key: str  # the stage key that decided the stage (first key when unmapped)
    mapped: bool  # False: default stage, entry marked unmapped (Req 29.3)


def _kind(e: Any) -> str:
    k = getattr(e, "kind", "")
    return str(getattr(k, "value", k))


def stage_keys(e: Any) -> tuple[str, ...]:
    """Stage keys of an Evidence_Chain entry (see module docstring)."""
    kind = _kind(e)
    ctype = getattr(e, "ctype", None)
    feats: Mapping[str, Any] = getattr(e, "features", {}) or {}
    detail: Mapping[str, Any] = getattr(e, "detail", {}) or {}
    if kind in _CONTRIBUTOR_KINDS:
        if ctype == "MOTIF" or kind == "MOTIF":
            return (f"motif.{feats.get('motif_type') or e.contributor_id}",)
        if ctype == "SENSOR":
            m, w = getattr(e, "mass", None), getattr(e, "weight", None)
            indicative = m is not None and w is not None and float(w) * float(m.f) > 0.0
            if not indicative:
                return ("evidence.non_indicative",)
            if e.contributor_id == "auth":
                sig = [s for s in str(feats.get("signals") or "").split("|") if s]
                return tuple(f"auth.{s}" for s in sig) or ("sensor.auth",)
            return (f"sensor.{e.contributor_id}",)
        return (f"{str(ctype).lower()}.{e.contributor_id}",)
    if kind == "AUTH_EVENT":
        sig = detail.get("signal")
        return (f"auth.{sig}",) if sig else ("auth.linked",)
    return (f"record.{kind}",)


def assign_stage(
    e: Any,
    mapping: Optional[Mapping[str, Union[KillChainStage, str]]] = None,
    default: Union[KillChainStage, str] = DEFAULT_STAGE,
) -> StageAssignment:
    """Exactly one stage for an entry (Req 29.2, 29.3)."""
    mp = DEFAULT_KILL_CHAIN_MAPPING if mapping is None else mapping
    keys = stage_keys(e)
    best: Optional[StageAssignment] = None
    for k in keys:
        s = mp.get(k)
        if s is None:
            continue
        s = KillChainStage(s)
        if best is None or stage_rank(s) > stage_rank(best.stage):
            best = StageAssignment(s, k, True)
    return best if best is not None else StageAssignment(KillChainStage(default), keys[0], False)


@dataclass(frozen=True, slots=True)
class StageSpan:
    stage: KillChainStage
    first_ts: datetime  # earliest entry timestamp at this stage
    entries: int
    tx_ids: tuple[str, ...]  # chain order
    keys: tuple[str, ...]  # stage keys seen at this stage, sorted

    def to_dict(self) -> dict[str, Any]:
        return {"stage": self.stage.value, "first_ts": self.first_ts.isoformat(), "entries": self.entries,
                "tx_ids": list(self.tx_ids), "keys": list(self.keys)}


@dataclass(frozen=True, slots=True)
class KillChain:
    case_id: str
    stages: tuple[StageSpan, ...]  # stage order (Req 29.4)
    most_advanced: Optional[KillChainStage]  # None only for an empty chain
    assignments: tuple[tuple[tuple[str, str], StageAssignment], ...]  # (entry key, assignment), chain order
    unmapped: tuple[tuple[str, str], ...]  # entry keys given the default stage

    @property
    def multiplied(self) -> bool:
        return self.most_advanced in MULTIPLIED_STAGES

    def to_dict(self) -> dict[str, Any]:
        return {
            "case_id": self.case_id,
            "most_advanced": None if self.most_advanced is None else self.most_advanced.value,
            "stages": [s.to_dict() for s in self.stages],
            "unmapped": [{"contributor_id": c, "tx_id": t} for c, t in self.unmapped],
        }


def reconstruct(
    chain: Any,
    mapping: Optional[Mapping[str, Union[KillChainStage, str]]] = None,
    default: Union[KillChainStage, str] = DEFAULT_STAGE,
) -> KillChain:
    """Kill chain of a Case from its Evidence_Chain (iterated in canonical order)."""
    spans: dict[KillChainStage, list[Any]] = {}
    assigned = []
    unmapped = []
    for e in chain:
        a = assign_stage(e, mapping, default)
        assigned.append(((e.contributor_id, e.tx_id), a))
        if not a.mapped:
            unmapped.append((e.contributor_id, e.tx_id))
        spans.setdefault(a.stage, []).append((e, a.key))
    stages = []
    for s in KILL_CHAIN_ORDER:
        items = spans.get(s)
        if not items:
            continue
        stages.append(StageSpan(
            stage=s,
            first_ts=min(e.ts for e, _ in items),
            entries=len(items),
            tx_ids=tuple(dict.fromkeys(e.tx_id for e, _ in items)),
            keys=tuple(sorted({k for _, k in items})),
        ))
    return KillChain(
        case_id=str(getattr(chain, "case_id", "")),
        stages=tuple(stages),
        most_advanced=max_stage(a.stage for _, a in assigned),
        assignments=tuple(assigned),
        unmapped=tuple(unmapped),
    )


def check_multiplier(m: object) -> float:
    """Validated Kill_Chain_Multiplier (Req 29.8): a finite number >= 1, else ValueError."""
    if isinstance(m, bool) or not isinstance(m, (int, float, Decimal)):
        raise ValueError(f"kill_chain_multiplier must be a number >= 1, got {m!r}")
    x = float(m)
    if not (math.isfinite(x) and x >= 1.0):
        raise ValueError(f"kill_chain_multiplier must be a finite number >= 1, got {m!r}")
    return x


def multiplier_for(stage: Optional[Union[KillChainStage, str]], multiplier: float) -> Decimal:
    """Factor applied to Expected_Loss: the multiplier at CASH_OUT / LAYERING, else 1."""
    if stage is not None and KillChainStage(stage) in MULTIPLIED_STAGES:
        return Decimal(repr(check_multiplier(multiplier)))
    return Decimal(1)


def apply_multiplier(expected_loss: Decimal, stage: Optional[Union[KillChainStage, str]],
                     multiplier: float) -> Decimal:
    """Expected_Loss after the Kill_Chain_Multiplier (Req 29.6), quantised to cents."""
    f = multiplier_for(stage, multiplier)
    if f == 1:
        return expected_loss
    # max(): rounding a sub-cent input must not undercut it (never lowers priority, Req 29.7).
    return max(expected_loss, (expected_loss * f).quantize(_CENT, rounding=ROUND_HALF_EVEN))


__all__ = [
    "DEFAULT_STAGE",
    "MULTIPLIED_STAGES",
    "KillChain",
    "StageAssignment",
    "StageSpan",
    "apply_multiplier",
    "assign_stage",
    "check_multiplier",
    "max_stage",
    "multiplier_for",
    "reconstruct",
    "stage_keys",
    "stage_rank",
]
