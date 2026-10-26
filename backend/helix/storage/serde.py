"""Serialisation helpers for Storage.

* ``encode_json`` / ``decode_json``: a tagged JSON codec so payload dicts round-trip
  exactly, including ``datetime``, ``date``, ``timedelta``, ``Decimal``, ``tuple``,
  ``set``/``frozenset`` and non-finite floats. Enums, dataclasses, Pydantic models and
  numpy values are encoded one-way (as their value / field dict / Python value).
* ``profile_store_to_dict`` / ``profile_store_from_dict``: ProfileStore snapshots.
  ``helix.memory.ProfileStore`` has no public serialisation, so this reads its internal
  fields (``_accounts``, ``_seq``, ``half_life_s``, ``horizon``) without modifying
  ``helix.memory``. The per-account withholding log is included, so exact withholding,
  release and exclusion keep working after a restore.
"""

from __future__ import annotations

import dataclasses
import json
import math
from collections import deque
from datetime import date, datetime, timedelta
from decimal import Decimal
from enum import Enum
from typing import Any, Callable, Mapping

import numpy as np
from pydantic import BaseModel, ValidationError
from sortedcontainers import SortedList

from helix.codec import TransactionEvent
from helix.memory.dna import BehaviouralDNA, DecayedStats
from helix.memory.store import ProfileStore, _Account, _Entry

PROFILE_STORE_FORMAT = 1

_TAGS = ("$dt", "$date", "$td", "$dec", "$tuple", "$set", "$f", "$map")


# ---- tagged JSON ----------------------------------------------------------------------
def _tag(o: Any) -> Any:
    if o is None or isinstance(o, bool):
        return o
    if isinstance(o, Enum):
        return _tag(o.value)
    if isinstance(o, (str, int)):
        return o
    if isinstance(o, float):
        return o if math.isfinite(o) else {"$f": repr(o)}
    if isinstance(o, datetime):
        return {"$dt": o.isoformat()}
    if isinstance(o, date):
        return {"$date": o.isoformat()}
    if isinstance(o, timedelta):
        return {"$td": [o.days, o.seconds, o.microseconds]}
    if isinstance(o, Decimal):
        return {"$dec": str(o)}
    if isinstance(o, dict) or isinstance(o, Mapping):
        out: dict[str, Any] = {}
        for k, v in o.items():
            if isinstance(k, Enum):
                k = k.value
            if not isinstance(k, str):
                raise TypeError(f"payload dict keys must be str, got {type(k).__name__}")
            out[k] = _tag(v)
        return {"$map": out} if len(out) == 1 and next(iter(out)) in _TAGS else out
    if isinstance(o, list):
        return [_tag(v) for v in o]
    if isinstance(o, tuple):
        return {"$tuple": [_tag(v) for v in o]}
    if isinstance(o, (set, frozenset)):
        items = [_tag(v) for v in o]
        items.sort(key=lambda v: json.dumps(v, sort_keys=True))
        return {"$set": items}
    if isinstance(o, np.generic):
        return _tag(o.item())
    if isinstance(o, np.ndarray):
        return _tag(o.tolist())
    if isinstance(o, BaseModel):
        return _tag(o.model_dump(mode="python"))
    if dataclasses.is_dataclass(o) and not isinstance(o, type):
        return _tag({f.name: getattr(o, f.name) for f in dataclasses.fields(o)})
    raise TypeError(f"cannot encode {type(o).__name__} in a Storage payload")


def _untag(o: Any) -> Any:
    """Top-down decode (an ``object_hook`` would run bottom-up and break ``$map``)."""
    if isinstance(o, list):
        return [_untag(v) for v in o]
    if not isinstance(o, dict):
        return o
    if len(o) == 1:
        (k, v), = o.items()
        if k == "$dt":
            return datetime.fromisoformat(v)
        if k == "$date":
            return date.fromisoformat(v)
        if k == "$td":
            return timedelta(days=v[0], seconds=v[1], microseconds=v[2])
        if k == "$dec":
            return Decimal(v)
        if k == "$tuple":
            return tuple(_untag(x) for x in v)
        if k == "$set":
            return {_untag(x) for x in v}
        if k == "$f":
            return float(v)
        if k == "$map":
            return {kk: _untag(vv) for kk, vv in v.items()}
    return {k: _untag(v) for k, v in o.items()}


def encode_json(obj: Any) -> str:
    """Strict JSON text (no NaN/Infinity literals). Raises TypeError on unsupported values."""
    return json.dumps(_tag(obj), separators=(",", ":"), allow_nan=False)


def decode_json(text: str | bytes) -> Any:
    return _untag(json.loads(text))


# ---- events ---------------------------------------------------------------------------
_EVENT_FIELDS = (
    "tx_id", "ts", "src", "dst", "amount", "currency",
    "channel", "mcc", "lat", "lon", "device_id", "session_id",
)  # fmt: skip


@dataclasses.dataclass(frozen=True, slots=True)
class LooseEvent:
    """Fallback for restored log events that are not valid TransactionEvents
    (e.g. float amounts with more than 2 decimals). Satisfies ``EventLike``."""

    tx_id: str
    ts: datetime
    src: str
    dst: str
    amount: Decimal | float
    currency: str | None = None
    channel: str | None = None
    mcc: str | None = None
    lat: float | None = None
    lon: float | None = None
    device_id: str | None = None
    session_id: str | None = None


def event_to_dict(ev: Any) -> dict[str, Any]:
    return {k: getattr(ev, k, None) for k in _EVENT_FIELDS}


def event_from_dict(d: Mapping[str, Any]) -> Any:
    if d.get("currency") is not None:
        try:
            return TransactionEvent(**{k: v for k, v in d.items() if v is not None})
        except ValidationError:
            pass
    return LooseEvent(**{k: d.get(k) for k in _EVENT_FIELDS})


# ---- Behavioural_DNA / ProfileStore ---------------------------------------------------
def _stats_to(s: DecayedStats) -> list[float]:
    return [s.w, s.off, s.m2, s.shift]


def _stats_from(v: list[float]) -> DecayedStats:
    return DecayedStats(float(v[0]), float(v[1]), float(v[2]), float(v[3]))


def dna_to_dict(dna: BehaviouralDNA) -> dict[str, Any]:
    return {
        "account": dna.account,
        "count": dna.count,
        "t_last": dna.t_last,
        "amount": _stats_to(dna.amount),
        "iat": _stats_to(dna.iat),
        "hour_hist": [float(x) for x in dna.hour_hist],
        "weekday_hist": [float(x) for x in dna.weekday_hist],
        "channel_counts": dict(dna.channel_counts),
        "channel_t": dna.channel_t,
        "last_location": None if dna.last_location is None else list(dna.last_location),
        "counterparties": sorted(dna.counterparties),
        "recent_ts": list(dna.recent_ts),
    }


def dna_from_dict(d: Mapping[str, Any]) -> BehaviouralDNA:
    loc = d["last_location"]
    return BehaviouralDNA(
        account=d["account"],
        count=int(d["count"]),
        t_last=d["t_last"],
        amount=_stats_from(d["amount"]),
        iat=_stats_from(d["iat"]),
        hour_hist=np.asarray(d["hour_hist"], dtype=float),
        weekday_hist=np.asarray(d["weekday_hist"], dtype=float),
        channel_counts={str(k): float(v) for k, v in d["channel_counts"].items()},
        channel_t=d["channel_t"],
        last_location=None if loc is None else (float(loc[0]), float(loc[1]), loc[2]),
        counterparties=set(d["counterparties"]),
        recent_ts=SortedList(d["recent_ts"]),
    )


def _entry_to(e: _Entry) -> list[Any]:
    return [e.seq, event_to_dict(e.ev)]


def _entry_from(v: list[Any]) -> _Entry:
    return _Entry(int(v[0]), event_from_dict(v[1]))


def profile_store_to_dict(ps: ProfileStore) -> dict[str, Any]:
    """Full ProfileStore state (DNA, base checkpoints, withholding log, holds, exclusions).

    Values may contain datetimes and Decimals; encode with :func:`encode_json`.
    """
    return {
        "format": PROFILE_STORE_FORMAT,
        "half_life_s": ps.half_life_s,
        "horizon": ps.horizon,
        "seq": ps._seq,
        "accounts": {
            acc: {
                "base": dna_to_dict(st.base),
                "dna": dna_to_dict(st.dna),
                "log": [_entry_to(e) for e in st.log],
                "withheld": {tx: _entry_to(e) for tx, e in st.withheld.items()},
                "hold": sorted(st.hold),
                "excluded": sorted(st.excluded),
            }
            for acc, st in ps._accounts.items()
        },
    }


def profile_store_from_dict(
    d: Mapping[str, Any], *, is_canary: Callable[[str], bool] | None = None
) -> ProfileStore:
    """Rebuild a ProfileStore equal in state to the one passed to ``profile_store_to_dict``.

    ``is_canary`` is a callable and cannot be stored; pass the live one.
    """
    if d.get("format") != PROFILE_STORE_FORMAT:
        raise ValueError(f"unsupported ProfileStore snapshot format {d.get('format')!r}")
    horizon: timedelta = d["horizon"]
    half_life_s = float(d["half_life_s"])
    ps = ProfileStore(
        half_life_days=half_life_s / 86_400.0,
        horizon_s=horizon.total_seconds(),
        is_canary=is_canary,
    )
    ps.half_life_s = half_life_s  # exact, not via the days round-trip
    ps.horizon = horizon
    ps._seq = int(d["seq"])
    ps._accounts = {
        acc: _Account(
            base=dna_from_dict(a["base"]),
            dna=dna_from_dict(a["dna"]),
            log=deque(_entry_from(e) for e in a["log"]),
            withheld={tx: _entry_from(e) for tx, e in a["withheld"].items()},
            hold=set(a["hold"]),
            excluded=set(a["excluded"]),
        )
        for acc, a in d["accounts"].items()
    }
    return ps
