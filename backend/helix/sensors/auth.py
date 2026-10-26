"""Auth history, Suspicious_Auth_Activity, Auth_Event linkage and the Auth_Sensor (Req 28.1, 28.7, 28.8).

Design decisions:
- ``AuthHistory`` is the per-account authentication history the pipeline records every
  delivered Auth_Event into. It is bounded three ways: per-account event count
  (``auth_history_max_events``), time (events older than ``auth_retention_s`` behind the
  newest auth timestamp seen are dropped; retention is never shorter than the correlation
  window) and number of accounts (least recently recorded account evicted first).
- Known devices are the devices of earlier LOGIN_SUCCESS events (bounded LRU per account,
  kept independently of event retention). A LOGIN_SUCCESS is a *new-device* login iff the
  account already has a known device and this device is not one of them; the first login
  ever recorded has no baseline and is not evidence (cold start, like Minimum_History).
  The flag is fixed when the event is recorded, so it depends on arrival order.
- Suspicious_Auth_Activity relative to a Transaction_Event at ``ts`` is evaluated over the
  account's events in ``[ts - window, ts]``: at least ``failed_login_count`` LOGIN_FAILUREs
  inside any ``failed_login_window_s`` span, any PASSWORD_RESET, MFA_FAILURE or
  NEW_PAYEE_ADDED, and any new-device LOGIN_SUCCESS (glossary).
- Linkage (Req 28.7): events in the window with the same account and the same session or
  device as the transaction. ``AuthHistory.links`` returns linked and suspicious events
  together; the pipeline writes one AUTH_EVENT Evidence_Chain entry per event.
- ``AuthSensor`` is a Sensor_Agent named ``auth``. It reads the shared history (it holds a
  read reference, never mutates it), so it plugs into the unchanged sensor runner and any
  stage-1 engine wrapper. It is vacuous (``history:0``) when the account has no auth
  history. With suspicious activity it puts ``1 - prod(1 - m_signal)`` (capped) on {FRAUD}
  and names the suspicious event ids in its features; a benign linked login from a known
  device gives a small LEGIT mass; otherwise it is vacuous with no missing-data reason.
"""

from __future__ import annotations

import bisect
import math
from collections import OrderedDict
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Iterable, Optional

from helix.codec.auth import AuthEvent, AuthEventType
from helix.config.schema import SocConfig
from helix.memory import DNAView

from .base import SensorContext, SensorOutput, mass_of, missing_output, vacuous

#: Signal names in kill-chain order (used for feature strings and stage keys).
SIGNALS: tuple[str, ...] = ("failed_logins", "mfa_failure", "password_reset", "new_device", "new_payee")
MAX_LISTED_IDS = 50  # suspicious ids written into the sensor features

_SIGNAL_OF_TYPE = {
    AuthEventType.PASSWORD_RESET: "password_reset",
    AuthEventType.MFA_FAILURE: "mfa_failure",
    AuthEventType.NEW_PAYEE_ADDED: "new_payee",
}


@dataclass(frozen=True, slots=True)
class AuthRecord:
    event: AuthEvent
    new_device: bool  # LOGIN_SUCCESS from a device not previously seen (fixed at record time)
    seq: int  # recording order (tie-break for equal timestamps)

    @property
    def sort_key(self) -> tuple[datetime, int]:
        return (self.event.ts, self.seq)


@dataclass(frozen=True, slots=True)
class AuthLink:
    """One Auth_Event correlated with a Transaction_Event."""

    record: AuthRecord
    linked_by: str  # "session", "device", "session+device" or "" (suspicious only)
    signal: Optional[str]  # Suspicious_Auth_Activity signal, None when benign

    @property
    def event(self) -> AuthEvent:
        return self.record.event

    @property
    def suspicious(self) -> bool:
        return self.signal is not None


@dataclass(frozen=True, slots=True)
class SuspiciousActivity:
    """Suspicious_Auth_Activity of one account in a window, in time order."""

    items: tuple[tuple[AuthRecord, str], ...] = ()
    failed_logins: int = 0  # failures inside qualifying bursts

    @property
    def signals(self) -> frozenset[str]:
        return frozenset(s for _, s in self.items)

    @property
    def event_ids(self) -> tuple[str, ...]:
        return tuple(r.event.event_id for r, _ in self.items)

    def __bool__(self) -> bool:
        return bool(self.items)


@dataclass(slots=True)
class _Account:
    records: list[AuthRecord] = field(default_factory=list)  # sorted by (ts, seq)
    keys: list[tuple[datetime, int]] = field(default_factory=list)
    ids: set[str] = field(default_factory=set)
    devices: OrderedDict = field(default_factory=OrderedDict)  # known device -> None (LRU)


class AuthHistory:
    def __init__(self, cfg: Optional[SocConfig] = None) -> None:
        self.cfg = cfg if cfg is not None else SocConfig()
        self._acc: OrderedDict[str, _Account] = OrderedDict()
        self._seq = 0
        self._newest: Optional[datetime] = None
        self.recorded = 0
        self.duplicates = 0
        self.evicted = 0

    # ----------------------------------------------------------------- config

    @property
    def window(self) -> timedelta:
        return timedelta(seconds=float(self.cfg.auth_correlation_window_s))

    @property
    def retention(self) -> timedelta:
        return timedelta(seconds=max(float(self.cfg.auth_retention_s), float(self.cfg.auth_correlation_window_s)))

    # ---------------------------------------------------------------- writing

    def record(self, ev: AuthEvent) -> Optional[AuthRecord]:
        """Record one Auth_Event (Req 28.1). Returns None for an id already in the history."""
        acc = self._acc.get(ev.account_id)
        if acc is None:
            acc = self._acc[ev.account_id] = _Account()
            while len(self._acc) > int(self.cfg.auth_max_accounts):
                _, old = self._acc.popitem(last=False)
                self.evicted += len(old.records)
        else:
            self._acc.move_to_end(ev.account_id)
        if ev.event_id in acc.ids:
            self.duplicates += 1
            return None

        new_device = False
        if ev.event_type is AuthEventType.LOGIN_SUCCESS and ev.device_id is not None:
            new_device = bool(acc.devices) and ev.device_id not in acc.devices
            acc.devices[ev.device_id] = None
            acc.devices.move_to_end(ev.device_id)
            while len(acc.devices) > int(self.cfg.known_devices_max):
                acc.devices.popitem(last=False)

        self._seq += 1
        rec = AuthRecord(ev, new_device, self._seq)
        i = bisect.bisect_right(acc.keys, rec.sort_key)
        acc.keys.insert(i, rec.sort_key)
        acc.records.insert(i, rec)
        acc.ids.add(ev.event_id)
        self.recorded += 1
        if self._newest is None or ev.ts > self._newest:
            self._newest = ev.ts
        self._trim(acc)
        return rec

    def _trim(self, acc: _Account) -> None:
        cap = int(self.cfg.auth_history_max_events)
        drop = max(0, len(acc.records) - cap)
        if self._newest is not None:
            cutoff = (self._newest - self.retention, -1)
            drop = max(drop, bisect.bisect_left(acc.keys, cutoff))
        if drop:
            for r in acc.records[:drop]:
                acc.ids.discard(r.event.event_id)
            del acc.records[:drop]
            del acc.keys[:drop]
            self.evicted += drop

    # ---------------------------------------------------------------- reading

    def accounts(self) -> list[str]:
        return list(self._acc)

    def has_history(self, account: str) -> bool:
        acc = self._acc.get(account)
        return acc is not None and bool(acc.records)

    def events(self, account: str) -> list[AuthRecord]:
        acc = self._acc.get(account)
        return list(acc.records) if acc is not None else []

    def known_devices(self, account: str) -> tuple[str, ...]:
        acc = self._acc.get(account)
        return tuple(acc.devices) if acc is not None else ()

    def __len__(self) -> int:
        return sum(len(a.records) for a in self._acc.values())

    def in_window(self, account: str, ts: datetime, window: Optional[timedelta] = None) -> list[AuthRecord]:
        """Records of ``account`` with ``ts - window <= event.ts <= ts`` in time order."""
        acc = self._acc.get(account)
        if acc is None:
            return []
        w = self.window if window is None else window
        lo = bisect.bisect_left(acc.keys, (ts - w, -1))
        hi = bisect.bisect_right(acc.keys, (ts, math.inf))  # type: ignore[arg-type]
        return acc.records[lo:hi]

    def suspicious(self, account: str, ts: datetime, records: Optional[list[AuthRecord]] = None) -> SuspiciousActivity:
        """Suspicious_Auth_Activity of ``account`` in the correlation window ending at ``ts``."""
        recs = self.in_window(account, ts) if records is None else records
        burst = _failed_login_bursts(recs, int(self.cfg.failed_login_count),
                                     timedelta(seconds=float(self.cfg.failed_login_window_s)))
        items: list[tuple[AuthRecord, str]] = []
        for r in recs:
            t = r.event.event_type
            if t is AuthEventType.LOGIN_FAILURE:
                if r.seq in burst:
                    items.append((r, "failed_logins"))
            elif t is AuthEventType.LOGIN_SUCCESS:
                if r.new_device:
                    items.append((r, "new_device"))
            elif t in _SIGNAL_OF_TYPE:
                items.append((r, _SIGNAL_OF_TYPE[t]))
        return SuspiciousActivity(tuple(items), len(burst))

    def links(self, ev: Any) -> tuple[AuthLink, ...]:
        """Auth_Events correlated with Transaction_Event ``ev`` (Req 28.7, 28.8): every
        event in the window with the same account and session or device, plus every
        suspicious event of the source account. Time order."""
        recs = self.in_window(ev.src, ev.ts)
        if not recs:
            return ()
        sig = {r.seq: s for r, s in self.suspicious(ev.src, ev.ts, recs).items}
        out = []
        for r in recs:
            by = _linked_by(r.event, ev)
            s = sig.get(r.seq)
            if by or s is not None:
                out.append(AuthLink(r, by, s))
        return tuple(out)


def _linked_by(a: AuthEvent, ev: Any) -> str:
    parts = []
    sid, dev = getattr(ev, "session_id", None), getattr(ev, "device_id", None)
    if sid is not None and a.session_id == sid:
        parts.append("session")
    if dev is not None and a.device_id is not None and a.device_id == dev:
        parts.append("device")
    return "+".join(parts)


def _failed_login_bursts(recs: Iterable[AuthRecord], n: int, span: timedelta) -> set[int]:
    """Seqs of LOGIN_FAILUREs inside some ``span`` holding at least ``n`` failures."""
    fails = [r for r in recs if r.event.event_type is AuthEventType.LOGIN_FAILURE]
    out: set[int] = set()
    i = 0
    for j, r in enumerate(fails):
        while fails[i].event.ts < r.event.ts - span:
            i += 1
        if j - i + 1 >= n:
            out.update(f.seq for f in fails[i: j + 1])
    return out


def signal_mass(signals: Iterable[str], cfg: SocConfig, payee_match: bool = False) -> float:
    """``1 - prod(1 - m_s)`` over distinct signals, capped at ``max_fraud_mass``."""
    per = {
        "failed_logins": cfg.m_failed_logins,
        "mfa_failure": cfg.m_mfa_failure,
        "password_reset": cfg.m_password_reset,
        "new_device": cfg.m_new_device,
        "new_payee": cfg.m_payee_match if payee_match else cfg.m_new_payee,
    }
    keep = 1.0
    for s in sorted(set(signals)):
        keep *= 1.0 - float(per.get(s, 0.0))
    return min(float(cfg.max_fraud_mass), max(0.0, 1.0 - keep))


class AuthSensor:
    name = "auth"

    def __init__(self, history: AuthHistory, cfg: Optional[SocConfig] = None) -> None:
        self.history = history
        self.cfg = cfg if cfg is not None else history.cfg

    def __repr__(self) -> str:
        return f"AuthSensor(window_s={self.cfg.auth_correlation_window_s})"

    def evaluate(self, ev: Any, dna_before: DNAView, ctx: SensorContext) -> SensorOutput:
        if getattr(ev, "src", None) in (None, "") or getattr(ev, "ts", None) is None:
            return missing_output([f for f in ("src", "ts") if getattr(ev, f, None) in (None, "")])
        h = self.history
        if not h.has_history(ev.src):
            return vacuous("history:0", {"auth_events": 0.0})
        recs = h.in_window(ev.src, ev.ts)
        act = h.suspicious(ev.src, ev.ts, recs)
        linked = [r for r in recs if _linked_by(r.event, ev)]
        feats: dict[str, float | str | None] = {
            "window_s": float(self.cfg.auth_correlation_window_s),
            "auth_events": float(len(recs)),
            "linked": float(len(linked)),
        }
        if act:
            payees = [r.event.payee_account_id for r, s in act.items if s == "new_payee"]
            match = getattr(ev, "dst", None) is not None and ev.dst in payees
            devices = [r.event.device_id for r, s in act.items if s == "new_device"]
            ids = act.event_ids
            feats.update({
                "signals": "|".join(s for s in SIGNALS if s in act.signals),
                "suspicious": float(len(ids)),
                "suspicious_ids": "|".join(ids[:MAX_LISTED_IDS]),
                "failed_logins": float(act.failed_logins),
                "new_device": devices[-1] if devices else None,
                "new_payee": next((p for p in reversed(payees) if p is not None), None),
                "payee_match": 1.0 if match else 0.0,
                "first_ts": act.items[0][0].event.ts.isoformat(),
            })
            return SensorOutput(mass_of(signal_mass(act.signals, self.cfg, match), 0.0), feats, None)
        benign = [r for r in linked
                  if r.event.event_type is AuthEventType.LOGIN_SUCCESS and not r.new_device]
        if benign and self.cfg.benign_link_legit_mass > 0:
            feats["signals"] = ""
            return SensorOutput(mass_of(0.0, float(self.cfg.benign_link_legit_mass)), feats, None)
        return vacuous(None, feats)  # history exists, nothing correlated: no evidence


__all__ = [
    "SIGNALS",
    "AuthHistory",
    "AuthLink",
    "AuthRecord",
    "AuthSensor",
    "SuspiciousActivity",
    "signal_mass",
]
