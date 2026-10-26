"""Pattern_Genome: a bounded parameter vector for one attack pattern type (design §17).

Each pattern type declares its parameters with bounds and a dimension tag (Req 20.7:
amounts, timing, hop count, topology, account selection). The *default* genome of each
type sits inside the matching HELIX motif definition, so it is detectable:

- Round_Trip_Cycle: 4 accounts, hops ~1 h apart (cycle of 2-6 accounts within 72 h);
- Fan_Out / Fan_In: 12 distinct counterparties ~10 min apart (>= 8 within 24 h);
- Structuring: 5 transfers in [0.931, 0.98] x RT, ~1 h apart (>= 3 in [0.9 RT, RT) within 48 h);
- Mule_Chain: 5 accounts, forwarding ratio 0.92, ~15 min hops (>= 3 accounts, >= 0.8, <= 60 min).

Mutation may move a child outside those definitions (longer cycles, slower hops, fewer
counterparties, amounts out of band). That is the evasion search space, by design.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field, replace
from enum import Enum
from types import MappingProxyType
from typing import Literal, Mapping, Optional

import numpy as np

Dimension = Literal["amounts", "timing", "hop_count", "topology", "account_selection"]


class PatternType(str, Enum):
    ROUND_TRIP_CYCLE = "ROUND_TRIP_CYCLE"
    ROUND_TRIP = "ROUND_TRIP_CYCLE"  # alias: design data-model name
    FAN_OUT = "FAN_OUT"
    FAN_IN = "FAN_IN"
    STRUCTURING = "STRUCTURING"
    MULE_CHAIN = "MULE_CHAIN"
    ACCOUNT_TAKEOVER = "ACCOUNT_TAKEOVER"
    CARD_TESTING = "CARD_TESTING"

    def __str__(self) -> str:
        return self.value


ALL_TYPES: tuple[PatternType, ...] = tuple(PatternType)  # aliases are not iterated: 7 members


@dataclass(frozen=True, slots=True)
class ParamSpec:
    lo: float
    hi: float
    kind: Literal["int", "float"]
    dim: Dimension
    default: float

    def clip(self, v: float) -> float | int:
        v = min(max(v, self.lo), self.hi)
        return int(round(v)) if self.kind == "int" else float(v)

    def contains(self, v: float | int) -> bool:
        if self.kind == "int" and (isinstance(v, bool) or not isinstance(v, int)):
            return False
        return isinstance(v, (int, float)) and math.isfinite(v) and self.lo <= v <= self.hi


def _i(lo: int, hi: int, dim: Dimension, default: int) -> ParamSpec:
    return ParamSpec(lo, hi, "int", dim, default)


def _f(lo: float, hi: float, dim: Dimension, default: float) -> ParamSpec:
    return ParamSpec(lo, hi, "float", dim, default)


# Per-type parameter bounds. The ordering of keys is the (deterministic) mutation order.
BOUNDS: Mapping[PatternType, Mapping[str, ParamSpec]] = MappingProxyType({
    PatternType.ROUND_TRIP_CYCLE: MappingProxyType({
        "cycle_len": _i(2, 9, "hop_count", 4),           # motif: 2-6 accounts
        "repeats": _i(1, 4, "topology", 1),               # laps around the same ring
        "amount": _f(200.0, 50_000.0, "amounts", 5_000.0),
        "decay": _f(0.80, 1.0, "amounts", 0.97),          # kept per hop (fees)
        "gap_s": _f(60.0, 172_800.0, "timing", 3_600.0),  # motif: whole cycle <= 72 h
        "fresh_share": _f(0.0, 1.0, "account_selection", 0.5),
    }),
    PatternType.FAN_OUT: MappingProxyType({
        "n_counterparties": _i(2, 40, "topology", 12),    # motif: >= 8 distinct
        "amount": _f(10.0, 20_000.0, "amounts", 900.0),
        "amount_jitter": _f(0.0, 0.5, "amounts", 0.1),
        "gap_s": _f(10.0, 14_400.0, "timing", 600.0),     # motif: within 24 h
        "fresh_share": _f(0.0, 1.0, "account_selection", 0.5),
    }),
    PatternType.FAN_IN: MappingProxyType({
        "n_counterparties": _i(2, 40, "topology", 12),
        "amount": _f(10.0, 20_000.0, "amounts", 900.0),
        "amount_jitter": _f(0.0, 0.5, "amounts", 0.1),
        "gap_s": _f(10.0, 14_400.0, "timing", 600.0),
        "fresh_share": _f(0.0, 1.0, "account_selection", 0.5),
    }),
    PatternType.STRUCTURING: MappingProxyType({
        "n_tx": _i(1, 30, "topology", 5),                 # motif: >= 3
        "n_dst": _i(1, 10, "topology", 3),
        "band_frac": _f(0.5, 0.999, "amounts", 0.98),     # top of the band, as a fraction of RT
        "band_jitter": _f(0.0, 0.3, "amounts", 0.05),     # amount = RT*band_frac*(1 - U*jitter)
        "gap_s": _f(60.0, 86_400.0, "timing", 3_600.0),   # motif: within 48 h
        "fresh_share": _f(0.0, 1.0, "account_selection", 0.5),
    }),
    PatternType.MULE_CHAIN: MappingProxyType({
        "hops": _i(2, 10, "hop_count", 4),                # motif: >= 3 accounts (>= 2 hops)
        "amount": _f(500.0, 100_000.0, "amounts", 8_000.0),
        "forward_ratio": _f(0.5, 1.0, "amounts", 0.92),   # motif: >= 0.8
        "hop_delay_s": _f(30.0, 14_400.0, "timing", 900.0),  # motif: <= 60 min
        "fresh_share": _f(0.0, 1.0, "account_selection", 0.6),
    }),
    PatternType.ACCOUNT_TAKEOVER: MappingProxyType({
        "n_tx": _i(1, 30, "topology", 4),
        "n_payees": _i(1, 10, "topology", 2),
        "amount": _f(50.0, 50_000.0, "amounts", 3_000.0),
        "gap_s": _f(5.0, 7_200.0, "timing", 180.0),
        "fresh_share": _f(0.0, 1.0, "account_selection", 0.9),  # payees: fresh mules vs pool
    }),
    PatternType.CARD_TESTING: MappingProxyType({
        "n_tests": _i(1, 60, "topology", 12),
        "n_merchants": _i(1, 60, "topology", 12),
        "test_amount": _f(0.5, 25.0, "amounts", 2.0),
        "final_amount": _f(100.0, 20_000.0, "amounts", 2_500.0),
        "gap_s": _f(1.0, 900.0, "timing", 30.0),
        "fresh_share": _f(0.0, 1.0, "account_selection", 0.0),  # card holder: pool vs fresh
    }),
})

# The parameter the topology tweak moves by +/-1 (hop count, cycle length or fan width).
TOPOLOGY_PARAM: Mapping[PatternType, str] = MappingProxyType({
    PatternType.ROUND_TRIP_CYCLE: "cycle_len",
    PatternType.FAN_OUT: "n_counterparties",
    PatternType.FAN_IN: "n_counterparties",
    PatternType.STRUCTURING: "n_tx",
    PatternType.MULE_CHAIN: "hops",
    PatternType.ACCOUNT_TAKEOVER: "n_payees",
    PatternType.CARD_TESTING: "n_merchants",
})

P_PARAM = 0.5  # chance that each parameter is jittered
P_TOPO = 0.3  # chance of the extra +/-1 topology tweak
MAX_RESAMPLES = 5  # then a forced integer step (design §17 step 3)


@dataclass(frozen=True, slots=True)
class PatternGenome:
    genome_id: str
    ptype: PatternType
    params: Mapping[str, float | int]  # each within BOUNDS[ptype]
    parent_id: Optional[str]
    generation: int = 0
    evasion: Optional[float] = None  # mean evasion of this genome's scored campaigns
    retired: bool = False
    scored_campaigns: int = field(default=0)

    def __post_init__(self) -> None:
        spec = BOUNDS[self.ptype]
        if set(self.params) != set(spec):
            raise ValueError(f"{self.ptype}: params must be exactly {sorted(spec)}")
        for k, v in self.params.items():
            if not spec[k].contains(v):
                raise ValueError(f"{self.ptype}.{k}={v!r} outside [{spec[k].lo}, {spec[k].hi}]")
        if not isinstance(self.params, MappingProxyType):
            object.__setattr__(self, "params", MappingProxyType(dict(self.params)))

    def differs_from(self, other: PatternGenome) -> tuple[Dimension, ...]:
        """Dimensions (Req 20.7) in which the two genomes' parameters differ."""
        spec = BOUNDS[self.ptype]
        return tuple(sorted({spec[k].dim for k in spec if self.params[k] != other.params.get(k)}))


def default_params(ptype: PatternType) -> dict[str, float | int]:
    return {k: s.clip(s.default) for k, s in BOUNDS[ptype].items()}


def default_genome(ptype: PatternType, genome_id: str) -> PatternGenome:
    return PatternGenome(genome_id, ptype, default_params(ptype), None, 0)


def _int_step(rng: np.random.Generator) -> int:
    return int((-2, -1, 1, 2)[int(rng.integers(0, 4))])


def _mutate_once(params: Mapping[str, float | int], ptype: PatternType, rng: np.random.Generator) -> dict:
    spec = BOUNDS[ptype]
    out = dict(params)
    for k, s in spec.items():
        if rng.random() >= P_PARAM:
            continue
        if s.kind == "int":
            out[k] = s.clip(out[k] + _int_step(rng))
        else:
            out[k] = s.clip(out[k] + float(rng.normal(0.0, 0.1 * (s.hi - s.lo))))
    if rng.random() < P_TOPO:
        k = TOPOLOGY_PARAM[ptype]
        out[k] = spec[k].clip(out[k] + (1 if rng.random() < 0.5 else -1))
    return out


def mutate(
    parent: PatternGenome, rng: np.random.Generator, child_id: str
) -> PatternGenome:
    """Child of ``parent``: jittered, clipped to bounds, guaranteed to differ in >= 1 dimension."""
    spec = BOUNDS[parent.ptype]
    params = dict(parent.params)
    for _ in range(MAX_RESAMPLES):
        params = _mutate_once(parent.params, parent.ptype, rng)
        if params != dict(parent.params):
            break
    else:
        # Forced integer step: the topology parameter moves one step away from its bound.
        k = TOPOLOGY_PARAM[parent.ptype]
        s = spec[k]
        v = parent.params[k]
        params = dict(parent.params)
        params[k] = v + 1 if v < s.hi else v - 1  # lo < hi for every topology param
    return PatternGenome(child_id, parent.ptype, params, parent.genome_id, parent.generation + 1)


def with_score(g: PatternGenome, evasion: float) -> PatternGenome:
    n = g.scored_campaigns
    mean = evasion if g.evasion is None else (g.evasion * n + evasion) / (n + 1)
    return replace(g, evasion=mean, scored_campaigns=n + 1)


__all__ = [
    "ALL_TYPES",
    "BOUNDS",
    "Dimension",
    "P_TOPO",
    "ParamSpec",
    "PatternGenome",
    "PatternType",
    "TOPOLOGY_PARAM",
    "default_genome",
    "default_params",
    "mutate",
    "with_score",
]
