"""Change_Point_Detector (design §8): per-account BOCPD with an Abrupt_Break /
Regime_Shift state machine.

Model (Adams & MacKay 2007):
- Features ``log(amount)`` and ``log1p(inter-arrival seconds)``, each with an
  independent Normal-Gamma prior and Student-t predictive (``ν = 2α``,
  ``scale² = β(κ+1)/(ακ)``). A missing iat (first event) contributes factor 1.
- Run-length slot ``r`` holds the sufficient statistics of the last ``r``
  observations; slot 0 holds the prior.
- Constant hazard ``H = 1/λ``; recursion in log space with logsumexp; run length
  truncated at ``R_max`` (overflow mass merged into the last slot); normalised.
- Detection signal: short-run mass ``s_t = Σ_{r ≤ r_small} P(r_t = r)``. A candidate
  is recorded on the rising edge ``s_t > θ and s_{t-1} ≤ θ`` once more than
  ``r_small + 1`` updates have been applied since the (re)start.
- Any non-finite value in the posterior (before or after a step) resets it to the
  prior with state NONE; history is kept.
"""
from __future__ import annotations

import math
from collections import deque
from dataclasses import dataclass, field, replace
from datetime import datetime
from enum import Enum
from typing import Any, Callable, Mapping

import numpy as np

from helix.fusion import VACUOUS, Mass

# ---- defaults (design §8) -------------------------------------------------------------
DEFAULT_LAMBDA = 100.0
DEFAULT_R_MAX = 300
#: Tuned up from the design's r_small = 2 (and κ0 0.01 → 0.1): on synthetic regime
#: switches with exponential inter-arrivals, Req 9.7 (Regime_Shift within N + 5) held
#: in ~40-45 % of small switches at (2, 0.01) and ~92-95 % at (4, 0.1); lognormal
#: features pass 100 % in both. See tests/unit/test_changepoint.py.
DEFAULT_R_SMALL = 4
#: Normal-Gamma (μ0, κ0, α0, β0) per feature: [log(amount), log1p(iat seconds)].
#: κ0 is small so the first observation dominates the run mean; β0/α0 is the prior
#: within-regime variance (σ ≈ 0.5 on log-amount, σ ≈ 1 on log-iat).
DEFAULT_PRIOR = ((math.log(100.0), 0.1, 1.0, 0.25), (math.log1p(3600.0), 0.1, 1.0, 1.0))

_LOG_PI = math.log(math.pi)
_lgamma = np.frompyfunc(math.lgamma, 1, 1)


class CPDKind(str, Enum):
    NONE = "NONE"
    ABRUPT_BREAK = "ABRUPT_BREAK"
    REGIME_SHIFT = "REGIME_SHIFT"

    def __str__(self) -> str:  # StrEnum-like behaviour on Python 3.10
        return self.value


@dataclass(frozen=True, slots=True)
class ChangePointRecord:
    account: str
    tx_id: str
    ts: datetime
    short_run_mass: float  # s_t at the rising edge
    est_start: datetime  # ts of the estimated first event of the new run
    est_start_tx: str  # tx_id of that event, passed to ProfileStore.rebase
    outcome: CPDKind  # ABRUPT_BREAK until confirmed REGIME_SHIFT or superseded

    def to_dict(self) -> dict[str, Any]:
        return {
            "account": self.account,
            "tx_id": self.tx_id,
            "ts": self.ts.isoformat(),
            "short_run_mass": self.short_run_mass,
            "est_start": self.est_start.isoformat(),
            "est_start_tx": self.est_start_tx,
            "outcome": self.outcome.value,
        }

    @classmethod
    def from_dict(cls, d: Mapping[str, Any]) -> ChangePointRecord:
        return cls(
            account=d["account"],
            tx_id=d["tx_id"],
            ts=datetime.fromisoformat(d["ts"]),
            short_run_mass=float(d["short_run_mass"]),
            est_start=datetime.fromisoformat(d["est_start"]),
            est_start_tx=d["est_start_tx"],
            outcome=CPDKind(d["outcome"]),
        )


@dataclass(slots=True)
class CPDState:
    account: str
    log_p: np.ndarray  # run-length log posterior, length ≤ R_max + 1
    suff: np.ndarray  # Normal-Gamma (μ, κ, α, β) per run length per feature, (R, 2, 4)
    kind: CPDKind = CPDKind.NONE
    candidate: ChangePointRecord | None = None
    quiet_count: int = 0  # consecutive non-triggering events since candidate
    s_prev: float = 0.0
    updates: int = 0  # updates since start / last reset (arming rule)
    history: list[ChangePointRecord] = field(default_factory=list)
    resets: int = 0
    t_last: datetime | None = None  # for iat in ``observe``
    recent: deque = field(default_factory=deque)  # (tx_id, ts), newest last, ≤ R_max + 1

    def probs(self) -> np.ndarray:
        return np.exp(self.log_p)

    def to_dict(self) -> dict[str, Any]:
        return {
            "account": self.account,
            "log_p": self.log_p.tolist(),
            "suff": self.suff.tolist(),
            "kind": self.kind.value,
            "candidate": None if self.candidate is None else self.candidate.to_dict(),
            "quiet_count": self.quiet_count,
            "s_prev": self.s_prev,
            "updates": self.updates,
            "history": [r.to_dict() for r in self.history],
            "resets": self.resets,
            "t_last": None if self.t_last is None else self.t_last.isoformat(),
            "recent": [[tx, ts.isoformat()] for tx, ts in self.recent],
        }

    @classmethod
    def from_dict(cls, d: Mapping[str, Any]) -> CPDState:
        return cls(
            account=d["account"],
            log_p=np.asarray(d["log_p"], dtype=float),
            suff=np.asarray(d["suff"], dtype=float).reshape(-1, 2, 4),
            kind=CPDKind(d["kind"]),
            candidate=None if d["candidate"] is None else ChangePointRecord.from_dict(d["candidate"]),
            quiet_count=int(d["quiet_count"]),
            s_prev=float(d["s_prev"]),
            updates=int(d["updates"]),
            history=[ChangePointRecord.from_dict(r) for r in d["history"]],
            resets=int(d.get("resets", 0)),
            t_last=None if d.get("t_last") is None else datetime.fromisoformat(d["t_last"]),
            recent=deque((tx, datetime.fromisoformat(ts)) for tx, ts in d.get("recent", [])),
        )


# ---- pure numerics ----------------------------------------------------------------------
def _logsumexp(a: np.ndarray) -> float:
    m = float(np.max(a))
    if not math.isfinite(m):
        return m
    return m + math.log(float(np.sum(np.exp(a - m))))


def student_t_logpdf(x: float, suff: np.ndarray) -> np.ndarray:
    """Normal-Gamma posterior predictive log density of ``x``; ``suff`` is (R, 4)."""
    mu, k, a, b = suff[:, 0], suff[:, 1], suff[:, 2], suff[:, 3]
    nu = 2.0 * a
    scale2 = b * (k + 1.0) / (a * k)
    lg = _lgamma(a + 0.5).astype(float) - _lgamma(a).astype(float)
    return lg - 0.5 * (np.log(nu * scale2) + _LOG_PI) - (a + 0.5) * np.log1p((x - mu) ** 2 / (nu * scale2))


def ng_update(suff: np.ndarray, x: float) -> np.ndarray:
    """Normal-Gamma posterior after one observation ``x``; ``suff`` is (R, 4)."""
    mu, k, a, b = suff[:, 0], suff[:, 1], suff[:, 2], suff[:, 3]
    out = np.empty_like(suff)
    out[:, 0] = (k * mu + x) / (k + 1.0)
    out[:, 1] = k + 1.0
    out[:, 2] = a + 0.5
    out[:, 3] = b + k * (x - mu) ** 2 / (2.0 * (k + 1.0))
    return out


def features(amount: float, iat_s: float | None) -> tuple[float, float | None]:
    """``(log(amount), log1p(iat))``; non-positive/non-finite amounts give non-finite
    features, which reset the posterior."""
    a = float(amount)
    xa = math.log(a) if a > 0 and math.isfinite(a) else float("nan")
    if iat_s is None:
        return xa, None
    i = float(iat_s)
    return xa, (math.log1p(max(i, 0.0)) if math.isfinite(i) else float("nan"))


def _finite(st: CPDState) -> bool:
    return bool(np.all(np.isfinite(st.log_p)) and np.all(np.isfinite(st.suff)))


def short_run_mass(log_p: np.ndarray, r_small: int) -> float:
    return float(np.sum(np.exp(log_p[: r_small + 1])))


def advance(st: CPDState, triggered: bool, record: ChangePointRecord | None, n_confirm: int) -> bool:
    """State machine step. Returns True when ABRUPT_BREAK is confirmed as REGIME_SHIFT.

    - candidate (from any state) → ABRUPT_BREAK, count restarted, record appended to
      history (earlier candidates stay in history);
    - ABRUPT_BREAK + n_confirm consecutive non-triggering events → REGIME_SHIFT;
    - REGIME_SHIFT and NONE hold otherwise.
    """
    if triggered:
        assert record is not None
        st.history.append(record)
        st.candidate = record
        st.kind = CPDKind.ABRUPT_BREAK
        st.quiet_count = 0
        return False
    if st.kind is CPDKind.ABRUPT_BREAK:
        st.quiet_count += 1
        if st.quiet_count >= n_confirm:
            assert st.candidate is not None
            confirmed = replace(st.candidate, outcome=CPDKind.REGIME_SHIFT)
            if st.history and st.history[-1] == st.candidate:
                st.history[-1] = confirmed
            st.candidate = confirmed
            st.kind = CPDKind.REGIME_SHIFT
            return True
    return False


def drift_mass(state: CPDState | None, cfg: Any, history_count: int, min_history: int) -> Mass:
    """Drift_Sensor mass (Req 5.11, 9.5, 9.6, 9.9, 9.10). ``cfg`` is a
    ``ChangePointConfig`` or a ``HelixConfig``."""
    cp = getattr(cfg, "changepoint", cfg)
    if state is None or history_count < min_history or state.candidate is None:
        return VACUOUS
    if state.kind is CPDKind.ABRUPT_BREAK:
        m = float(cp.abrupt_break_mass)
        return Mass(m, 0.0, 1.0 - m)
    if state.kind is CPDKind.REGIME_SHIFT:
        m = float(cp.regime_shift_mass)
        return Mass(0.0, m, 1.0 - m)
    return VACUOUS


# ---- detector ---------------------------------------------------------------------------
#: Called once when a candidate is confirmed as REGIME_SHIFT.
RegimeShiftHook = Callable[[str, ChangePointRecord], None]


def profile_store_rebaser(store: Any) -> RegimeShiftHook:
    """Hook that rebases ``store`` (a ``ProfileStore``) from the change point's
    estimated start, as design §7/§8 require on a confirmed Regime_Shift."""

    def _hook(account: str, rec: ChangePointRecord) -> None:
        store.rebase(account, rec.est_start_tx)

    return _hook


class ChangePointDetector:
    def __init__(
        self,
        threshold: float = 0.5,
        regime_confirmation_count: int = 10,
        *,
        hazard_lambda: float = DEFAULT_LAMBDA,
        r_max: int = DEFAULT_R_MAX,
        r_small: int = DEFAULT_R_SMALL,
        prior: tuple[tuple[float, float, float, float], ...] = DEFAULT_PRIOR,
        on_regime_shift: RegimeShiftHook | None = None,
    ) -> None:
        if not 0.0 <= threshold <= 1.0:
            raise ValueError(f"threshold={threshold!r} outside [0, 1]")
        if regime_confirmation_count < 1:
            raise ValueError("regime_confirmation_count must be >= 1")
        if hazard_lambda <= 1.0 or r_max < r_small + 1 or r_small < 0:
            raise ValueError("invalid hazard_lambda / r_max / r_small")
        self.threshold = float(threshold)
        self.n_confirm = int(regime_confirmation_count)
        self.r_max = int(r_max)
        self.r_small = int(r_small)
        self.log_h = math.log(1.0 / hazard_lambda)
        self.log_1mh = math.log1p(-1.0 / hazard_lambda)
        self.prior = np.asarray(prior, dtype=float).reshape(2, 4)
        self.on_regime_shift = on_regime_shift
        self._states: dict[str, CPDState] = {}

    @classmethod
    def from_config(cls, cfg: Any, **kw: Any) -> ChangePointDetector:
        cp = getattr(cfg, "changepoint", cfg)
        return cls(cp.threshold, cp.regime_confirmation_count, **kw)

    # ---- read -------------------------------------------------------------------------
    def state(self, account: str) -> CPDState:
        st = self._states.get(account)
        if st is None:
            st = self._fresh(account)
            self._states[account] = st
        return st

    def accounts(self) -> frozenset[str]:
        return frozenset(self._states)

    # ---- write ------------------------------------------------------------------------
    def observe(self, ev: Any) -> CPDState:
        """Step from an event (``src``, ``amount``, ``tx_id``, ``ts``), computing the
        iat from the previous observed event. Usable as ``ProfileStore.release``'s
        ``on_apply`` hook via ``lambda acc, ev: det.observe(ev)``."""
        st = self.state(ev.src)
        iat = None if st.t_last is None else (ev.ts - st.t_last).total_seconds()
        return self.update(ev.src, float(ev.amount), iat, ev.tx_id, ev.ts)

    def update(
        self, account: str, x_amount: float, x_iat: float | None, tx_id: str, ts: datetime
    ) -> CPDState:
        """One BOCPD step. ``x_amount`` is the raw amount (> 0) and ``x_iat`` the raw
        inter-arrival time in seconds (None for the first event)."""
        st = self.state(account)
        st.t_last = ts if st.t_last is None else max(st.t_last, ts)
        st.recent.append((tx_id, ts))
        while len(st.recent) > self.r_max + 1:
            st.recent.popleft()
        if not _finite(st):
            self._reset(st)
            return st
        xa, xi = features(x_amount, x_iat)
        log_p, suff = self._step(st.log_p, st.suff, xa, xi)
        st.log_p, st.suff = log_p, suff
        if not _finite(st):
            self._reset(st)
            return st
        st.updates += 1
        s = short_run_mass(log_p, self.r_small)
        armed = st.updates > self.r_small + 1
        triggered = armed and s > self.threshold and st.s_prev <= self.threshold
        st.s_prev = s
        rec = None
        if triggered:
            r_star = int(np.argmax(log_p))
            start_tx, start_ts = st.recent[-max(min(r_star, len(st.recent)), 1)]
            rec = ChangePointRecord(
                account, tx_id, ts, s, start_ts, start_tx, CPDKind.ABRUPT_BREAK
            )
        if advance(st, triggered, rec, self.n_confirm) and self.on_regime_shift is not None:
            assert st.candidate is not None
            self.on_regime_shift(account, st.candidate)
        return st

    # ---- persistence ------------------------------------------------------------------
    def to_dict(self) -> dict[str, Any]:
        return {"accounts": {a: s.to_dict() for a, s in self._states.items()}}

    def load_dict(self, d: Mapping[str, Any]) -> None:
        self._states = {a: CPDState.from_dict(s) for a, s in d["accounts"].items()}

    def snapshot(self, account: str) -> dict[str, Any]:
        return self.state(account).to_dict()

    def restore(self, d: Mapping[str, Any]) -> CPDState:
        st = CPDState.from_dict(d)
        self._states[st.account] = st
        return st

    # ---- internals --------------------------------------------------------------------
    def _fresh(self, account: str) -> CPDState:
        return CPDState(account, np.zeros(1), self.prior[None].copy())

    def _reset(self, st: CPDState) -> None:
        """Posterior back to the prior (P(r=0) = 1), no candidate, history kept."""
        st.log_p = np.zeros(1)
        st.suff = self.prior[None].copy()
        st.kind = CPDKind.NONE
        st.candidate = None
        st.quiet_count = 0
        st.s_prev = 1.0  # short-run mass of the prior
        st.updates = 0
        st.resets += 1

    def _step(
        self, log_p: np.ndarray, suff: np.ndarray, xa: float, xi: float | None
    ) -> tuple[np.ndarray, np.ndarray]:
        lj = log_p + student_t_logpdf(xa, suff[:, 0])
        new_suff = np.empty((len(log_p) + 1, 2, 4))
        new_suff[0] = self.prior
        new_suff[1:, 0] = ng_update(suff[:, 0], xa)
        if xi is None:
            new_suff[1:, 1] = suff[:, 1]
        else:
            lj = lj + student_t_logpdf(xi, suff[:, 1])
            new_suff[1:, 1] = ng_update(suff[:, 1], xi)
        new_lp = np.empty(len(log_p) + 1)
        new_lp[0] = _logsumexp(lj) + self.log_h
        new_lp[1:] = lj + self.log_1mh
        if len(new_lp) > self.r_max + 1:  # merge overflow into the R_max slot
            a, b = new_lp[-2], new_lp[-1]
            if b > a:
                new_suff[-2] = new_suff[-1]
            new_lp[-2] = np.logaddexp(a, b)
            new_lp, new_suff = new_lp[:-1], new_suff[:-1]
        z = _logsumexp(new_lp)
        return new_lp - z, new_suff
