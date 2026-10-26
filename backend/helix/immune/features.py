"""Immune_System feature space (design §18): ``f(event) in [0, 1]^d`` with NaN for missing.

Every dimension is built from the event itself or from a *successful* SensorResult's
features (the sensors already read the pre-event Behavioural_DNA), so the vector depends
only on what stage 1 saw:

======================  ===============================================  ==================
dimension               value                                            missing when
======================  ===============================================  ==================
amount_z                ``min(z / 10, 1)`` (Amount sensor ``z``)         sensor vacuous / failed
log_amount              ``log(max(amount, 1)) / log(1e9)``, clipped      never (required field)
hour_sin, hour_cos      ``(sin h + 1) / 2``, ``(cos h + 1) / 2`` with    never (required field)
                        ``h = 2 pi (hour + minute / 60) / 24`` (UTC)
velocity_ratio          ``min(r / 10, 1)`` (Velocity sensor ``ratio``)   sensor vacuous / failed
counterparty_unknown    ``1 - known`` (Counterparty sensor)              sensor vacuous / failed
geo_speed_ratio         ``min(v / vmax / 10, 1)`` (Geo sensor)           no location / failed
motif_<TYPE> (x5)       1 if the vector describes a Motif instance of    never (0 for plain
                        that type                                        events)
======================  ===============================================  ==================

Missing inputs are NaN. An Antibody never matches on a NaN dimension (``match`` treats a
NaN in any of the antibody's dimensions as no match), so missing data never adds evidence
(design §5 note, Req 7.5).

Stage-1 events are plain events, so their motif flags are 0. The flags exist so that Motif
instance vectors share the space; Motif-instance matching is not wired into stage-2 fusion
in this increment (it would need changes inside the pipeline's REASON stage).
"""

from __future__ import annotations

import math
from datetime import timezone
from typing import Any, Iterable, Mapping, Optional, Sequence

import numpy as np

from helix.motifs import MotifType

MOTIF_DIMS: tuple[str, ...] = tuple(f"motif_{t.value}" for t in MotifType)
DIMS: tuple[str, ...] = (
    "amount_z",
    "log_amount",
    "hour_sin",
    "hour_cos",
    "velocity_ratio",
    "counterparty_unknown",
    "geo_speed_ratio",
    *MOTIF_DIMS,
)
D = len(DIMS)
INDEX: Mapping[str, int] = {n: i for i, n in enumerate(DIMS)}
_LOG_MAX = math.log(1e9)

TECHNIQUE_PREFIX = "HX-T"
#: Tag for an Antibody whose origin has no known technique (a miss whose campaign type is not
#: known to the tagger, or a Case without Motifs). Not an ``HX-Tnnnn`` id, so it never
#: collides with a catalogue entry.
UNATTRIBUTED = "UNATTRIBUTED"
#: Provisional HELIX_Technique ids (glossary form ``HX-Tnnnn``) for the Pattern_Genome / Motif
#: types. The versioned HELIX_Technique_Catalogue arrives with Increment 4 (Req 34); until
#: then this table is the single source of the ids the Immune_System tags antibodies with.
TECHNIQUES: Mapping[str, str] = {
    "ROUND_TRIP_CYCLE": "HX-T0001",
    "FAN_OUT": "HX-T0002",
    "FAN_IN": "HX-T0003",
    "STRUCTURING": "HX-T0004",
    "MULE_CHAIN": "HX-T0005",
    "ACCOUNT_TAKEOVER": "HX-T0006",
    "CARD_TESTING": "HX-T0007",
}


def technique_tag(kind: Any) -> Optional[str]:
    """HELIX_Technique id for a Pattern_Genome type or Motif type; an ``HX-Tnnnn`` string is
    passed through; None for an unknown kind."""
    v = str(getattr(kind, "value", kind)).upper()
    if v == "ROUND_TRIP":
        v = "ROUND_TRIP_CYCLE"
    if v.startswith(TECHNIQUE_PREFIX) and v[len(TECHNIQUE_PREFIX):].isdigit():
        return v
    return TECHNIQUES.get(v)


def technique_tags(kinds: Iterable[Any]) -> tuple[str, ...]:
    """Sorted, de-duplicated technique ids of ``kinds`` (unknown kinds are dropped)."""
    return tuple(sorted({t for k in kinds if (t := technique_tag(k)) is not None}))


def _num(v: Any) -> Optional[float]:
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        return None
    x = float(v)
    return None if math.isnan(x) else x


def _unit(x: float) -> float:
    return min(1.0, max(0.0, x))


def _ok_features(results: Iterable[Any]) -> dict[str, Mapping[str, Any]]:
    out: dict[str, Mapping[str, Any]] = {}
    for r in results:
        ok = getattr(r, "ok", None)
        out_ = getattr(r, "output", None)
        if ok is True and out_ is not None:  # vacuous outputs carry none of the keys read below
            out[str(r.name)] = out_.features or {}
    return out


def feature_vector(ev: Any, results: Sequence[Any] = (), motif_types: Iterable[Any] = ()) -> np.ndarray:
    """The event's normalised feature vector (float64, length ``D``, NaN = missing).

    ``results`` are the event's SensorResults; only SUCCESS results contribute, and a
    vacuous output (missing field, short history) has none of the keys read here, so a
    timed-out or history-starved sensor leaves its dimension missing. The Geo speed is kept
    under short history (the sensor halves its mass, but the speed itself is measured)."""
    f = np.full(D, np.nan)
    feats = _ok_features(results)

    amt = feats.get("amount", {})
    z = _num(amt.get("z"))
    if z is not None:
        f[INDEX["amount_z"]] = _unit(z / 10.0)

    a = getattr(ev, "amount", None)
    if a is not None:
        x = float(a)
        if math.isfinite(x):
            f[INDEX["log_amount"]] = _unit(math.log(max(x, 1.0)) / _LOG_MAX)

    ts = getattr(ev, "ts", None)
    if ts is not None:
        u = ts.astimezone(timezone.utc)
        th = 2.0 * math.pi * (u.hour + u.minute / 60.0 + u.second / 3600.0) / 24.0
        f[INDEX["hour_sin"]] = (math.sin(th) + 1.0) / 2.0
        f[INDEX["hour_cos"]] = (math.cos(th) + 1.0) / 2.0

    r = _num(feats.get("velocity", {}).get("ratio"))
    if r is not None:
        f[INDEX["velocity_ratio"]] = _unit(r / 10.0)

    known = _num(feats.get("counterparty", {}).get("known"))
    if known is not None:
        f[INDEX["counterparty_unknown"]] = _unit(1.0 - known)

    geo = feats.get("geo", {})
    v, vmax = _num(geo.get("speed_kmh")), _num(geo.get("vmax_kmh"))
    if v is not None and vmax is not None and vmax > 0:
        f[INDEX["geo_speed_ratio"]] = _unit(v / vmax / 10.0)

    types = {str(getattr(t, "value", t)) for t in motif_types}
    for t in MotifType:
        f[INDEX[f"motif_{t.value}"]] = 1.0 if t.value in types else 0.0
    return f


def present(fv: np.ndarray) -> np.ndarray:
    """Boolean mask of the non-missing dimensions."""
    return ~np.isnan(fv)


__all__ = ["D", "DIMS", "INDEX", "MOTIF_DIMS", "TECHNIQUES", "TECHNIQUE_PREFIX", "UNATTRIBUTED", "feature_vector",
           "present", "technique_tag", "technique_tags"]
