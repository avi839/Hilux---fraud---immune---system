"""Profile_Store (design §7): Behavioural_DNA per source account with exact withholding.

Each account keeps a base checkpoint plus an applied-event log covering the withholding
horizon. Removing events (withhold / exclude) and rebasing rebuild the DNA by replaying
base + log in original application order, which is exact by construction.

CPD state is owned by the Change_Point_Detector (task 4.2). The optional ``on_rebuild`` /
``on_apply`` hooks let it rebuild or step its own state alongside the DNA.
"""
from __future__ import annotations

import logging
from collections import deque
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any, Callable, Iterable, Sequence

from .dna import DAY_S, BehaviouralDNA, DNAView, EventLike, apply_event

log = logging.getLogger(__name__)

HALF_LIFE_MIN_DAYS = 1.0
HALF_LIFE_MAX_DAYS = 365.0
DEFAULT_HORIZON_S = 7 * DAY_S

#: Called after a rebuild with the account and its log events (application order) that
#: are now applied on top of the base checkpoint.
RebuildHook = Callable[[str, Sequence[EventLike]], None]
#: Called once per event newly applied by ``release``, in application order.
ApplyHook = Callable[[str, EventLike], None]


class ProfileStoreConfigError(ValueError):
    """Raised when a Profile_Store setting is out of range (Req 8.8)."""

    def __init__(self, setting: str, value: Any, lo: float, hi: float) -> None:
        super().__init__(f"{setting}={value!r} is outside the permitted range [{lo}, {hi}]")
        self.setting = setting


@dataclass(slots=True)
class _Entry:
    seq: int  # global application/arrival order
    ev: EventLike


@dataclass(slots=True)
class _Account:
    base: BehaviouralDNA
    dna: BehaviouralDNA
    log: deque[_Entry] = field(default_factory=deque)  # applied since base, app order
    withheld: dict[str, _Entry] = field(default_factory=dict)  # arrived, not applied
    hold: set[str] = field(default_factory=set)  # withhold on arrival
    excluded: set[str] = field(default_factory=set)  # never apply


class ProfileStore:
    def __init__(
        self,
        half_life_days: float = 30.0,
        horizon_s: float = DEFAULT_HORIZON_S,
        is_canary: Callable[[str], bool] | None = None,
    ) -> None:
        if not (HALF_LIFE_MIN_DAYS <= half_life_days <= HALF_LIFE_MAX_DAYS):
            raise ProfileStoreConfigError(
                "memory.half_life_days", half_life_days, HALF_LIFE_MIN_DAYS, HALF_LIFE_MAX_DAYS
            )
        if not horizon_s > 0:
            raise ProfileStoreConfigError("memory.horizon_s", horizon_s, 0, float("inf"))
        self.half_life_s = float(half_life_days) * DAY_S
        self.horizon = timedelta(seconds=horizon_s)
        self._is_canary = is_canary
        self._accounts: dict[str, _Account] = {}
        self._seq = 0

    @classmethod
    def from_config(cls, cfg: Any, **kw: Any) -> ProfileStore:
        """Build from a ``HelixConfig`` (uses ``cfg.memory.half_life_days``)."""
        return cls(half_life_days=cfg.memory.half_life_days, **kw)

    # ---- read ---------------------------------------------------------------------
    def view(self, account: str) -> DNAView:
        st = self._accounts.get(account)
        return DNAView.empty(account) if st is None else DNAView.of(st.dna)

    def withheld(self, account: str) -> frozenset[str]:
        st = self._accounts.get(account)
        return frozenset() if st is None else frozenset(st.withheld)

    def accounts(self) -> frozenset[str]:
        return frozenset(self._accounts)

    # ---- write --------------------------------------------------------------------
    def apply(self, ev: EventLike, *, withhold: bool = False) -> bool:
        """Apply ``ev`` to its source account's DNA. Returns False if withheld/excluded.

        ``withhold=True`` withholds up front (open HIGH/CRITICAL Case and stage-1 risk at
        or above the alerting threshold), which avoids a later rebuild.
        """
        if self._references_canary(ev):  # Req 33.6: Canary events never reach Behavioural_DNA
            return False
        st = self._state(ev.src)
        entry = _Entry(self._next_seq(), ev)
        if ev.tx_id in st.excluded:
            return False
        if withhold or ev.tx_id in st.hold:
            st.hold.discard(ev.tx_id)
            st.withheld[ev.tx_id] = entry
            return False
        self._apply_entry(st, entry)
        return True

    def withhold(
        self, account: str, tx_ids: Iterable[str], *, on_rebuild: RebuildHook | None = None
    ) -> set[str]:
        """Withhold ``tx_ids``: remove applied ones exactly, hold future arrivals.

        Returns the tx_ids whose applied contributions were removed.
        """
        ids = set(tx_ids)
        st = self._state(account)
        removed = self._remove_from_log(st, ids, keep=True)
        for tx in ids - removed - set(st.withheld) - st.excluded:
            st.hold.add(tx)
        if removed:
            self._rebuild(st, on_rebuild)
        return removed

    def release(
        self, account: str, tx_ids: Iterable[str], *, on_apply: ApplyHook | None = None
    ) -> list[EventLike]:
        """FALSE_POSITIVE: apply withheld events once each in timestamp order.

        Canary-referencing events are dropped instead of applied (Req 19.7). Returns the
        events applied, in application order.
        """
        ids = set(tx_ids)
        st = self._accounts.get(account)
        if st is None:
            return []
        st.hold -= ids
        entries = sorted(
            (st.withheld.pop(tx) for tx in ids if tx in st.withheld),
            key=lambda e: (e.ev.ts, e.seq),
        )
        applied: list[EventLike] = []
        for e in entries:
            if self._references_canary(e.ev):
                continue
            self._apply_entry(st, _Entry(self._next_seq(), e.ev))
            applied.append(e.ev)
            if on_apply is not None:
                on_apply(account, e.ev)
        return applied

    def exclude(
        self, account: str, tx_ids: Iterable[str], *, on_rebuild: RebuildHook | None = None
    ) -> set[str]:
        """CONFIRMED_FRAUD: drop ``tx_ids`` permanently (withheld, applied or future).

        Returns the tx_ids whose applied contributions were removed.
        """
        ids = set(tx_ids)
        st = self._state(account)
        removed = self._remove_from_log(st, ids, keep=False)
        for tx in ids:
            st.withheld.pop(tx, None)
        st.hold -= ids
        st.excluded |= ids
        if removed:
            self._rebuild(st, on_rebuild)
        return removed

    def rebase(
        self, account: str, since_tx: str, *, on_rebuild: RebuildHook | None = None
    ) -> bool:
        """Confirmed Regime_Shift: rebuild amount, inter-arrival, hour/weekday, location and
        channel stats from the log events applied since ``since_tx`` (inclusive), keeping
        the counterparty set, count, running max timestamp and recent timestamps.

        Returns False (and rebases on the whole log) if ``since_tx`` is not in the log.
        """
        st = self._accounts.get(account)
        if st is None:
            return False
        entries = list(st.log)
        idx = next((i for i, e in enumerate(entries) if e.ev.tx_id == since_tx), None)
        found = idx is not None
        if idx is None:
            log.warning(
                "rebase %s: change point %s not in withholding log; using whole log",
                account,
                since_tx,
            )
            idx = 0
        pre = st.base.copy()
        for e in entries[:idx]:
            apply_event(pre, e.ev, self.half_life_s)
        pre.reset_regime_stats()
        st.base = pre
        st.log = deque(entries[idx:])
        self._rebuild(st, on_rebuild)
        return found

    # ---- internals ----------------------------------------------------------------
    def _state(self, account: str) -> _Account:
        st = self._accounts.get(account)
        if st is None:
            st = _Account(base=BehaviouralDNA(account), dna=BehaviouralDNA(account))
            self._accounts[account] = st
        return st

    def _next_seq(self) -> int:
        self._seq += 1
        return self._seq

    def _references_canary(self, ev: EventLike) -> bool:
        f = self._is_canary
        if f is None:
            return False
        dev = getattr(ev, "device_id", None)
        return bool(f(ev.src) or f(ev.dst) or (isinstance(dev, str) and f(dev)))

    def set_canary_check(self, is_canary: Callable[[str], bool] | None) -> None:
        """Install (or clear) the Canary hook (Req 33.6); used by the SOC wiring."""
        self._is_canary = is_canary

    def _apply_entry(self, st: _Account, entry: _Entry) -> None:
        apply_event(st.dna, entry.ev, self.half_life_s)
        st.log.append(entry)
        # Fold entries older than the horizon into the base checkpoint. Folding is batched:
        # it only starts once the oldest entry is two horizons old, so most updates do no
        # folding at all (keeps the median update cost flat, Req 8.2) while every event
        # within one horizon of the latest timestamp stays removable.
        lg = st.log
        t_last = st.dna.t_last
        if lg and lg[0].ev.ts < t_last - 2 * self.horizon:
            cutoff = t_last - self.horizon
            while lg and lg[0].ev.ts < cutoff:
                apply_event(st.base, lg.popleft().ev, self.half_life_s)

    def _remove_from_log(self, st: _Account, ids: set[str], *, keep: bool) -> set[str]:
        if not ids:
            return set()
        kept: deque[_Entry] = deque()
        removed: set[str] = set()
        for e in st.log:
            if e.ev.tx_id in ids:
                removed.add(e.ev.tx_id)
                if keep:
                    st.withheld[e.ev.tx_id] = e
            else:
                kept.append(e)
        if removed:
            st.log = kept
        missing = ids - removed - set(st.withheld)
        if missing:
            # Either not yet arrived or already folded into the base (older than the
            # horizon); folded contributions cannot be removed exactly.
            log.info(
                "%s: %d tx not in withholding log (future or beyond horizon): %s",
                st.dna.account,
                len(missing),
                sorted(missing)[:10],
            )
        return removed

    def _rebuild(self, st: _Account, hook: RebuildHook | None) -> None:
        dna = st.base.copy()
        for e in st.log:
            apply_event(dna, e.ev, self.half_life_s)
        st.dna = dna
        if hook is not None:
            hook(dna.account, [e.ev for e in st.log])
