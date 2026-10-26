"""Synthetic_Generator: seeded legitimate traffic on a simulated clock (Req 3.1-3.12).

Every random draw comes from one `numpy.random.Generator`, and draws happen in
a fixed order per tick (jitter, then event content), so a seed and config give
the same event sequence however `events_until` calls are chunked.

Benign Auth_Events (Req 28.5), only with ``soc=SocConfig(enabled=True)``:

- every Transaction_Event is one simulated session; ``events_until`` yields a LOGIN_SUCCESS
  for that session (same session and device, a few seconds earlier) right before it, and
  for ``soc.login_failure_frac`` of sessions one or two LOGIN_FAILUREs first; an account's
  benign failures within any ``soc.failed_login_window_s`` stay below
  ``soc.failed_login_count``, so benign traffic is never a failed-login burst;
- before the first event, a back-dated LOGIN_SUCCESS per account device (``ENROL_LEAD``
  before ``t0``, outside any correlation window) establishes the known devices, so the
  account's own devices are not new-device logins;
- auth draws use a second RNG seeded from the same seed, so the Transaction_Event sequence
  is identical with and without auth traffic.
"""

from __future__ import annotations

import math
from collections import deque
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Iterator, Optional

import numpy as np

from helix.codec import OPTIONAL_FIELDS, AuthEvent, AuthEventType, TransactionEvent
from helix.config import ConfigError, ConfigValidationError, walk_ranges
from helix.config.schema import GeneratorConfig, SocConfig
from helix.config.validation import check

from .clock import SimClock
from .ground_truth import ChangeKind, GroundTruthRecord, GroundTruthWriter, GTSource

SEED_MAX = 4_294_967_295

# (name, lat, lon): real city centres used as home / relocation / travel anchors.
CITIES: tuple[tuple[str, float, float], ...] = (
    ("London", 51.5074, -0.1278),
    ("New York", 40.7128, -74.0060),
    ("Singapore", 1.3521, 103.8198),
    ("Mumbai", 19.0760, 72.8777),
    ("Sydney", -33.8688, 151.2093),
    ("Sao Paulo", -23.5505, -46.6333),
    ("Lagos", 6.5244, 3.3792),
    ("Tokyo", 35.6762, 139.6503),
    ("Berlin", 52.5200, 13.4050),
    ("Toronto", 43.6532, -79.3832),
)
CHANNELS: tuple[str, ...] = ("card", "online", "mobile", "transfer", "atm")
MCC_BY_CHANNEL: dict[str, tuple[str, ...]] = {
    "card": ("5411", "5812", "5541", "5311"),
    "online": ("5734", "5942", "4899"),
    "mobile": ("4814", "5815", "5814"),
    "transfer": ("4829", "6012"),
    "atm": ("6011",),
}
CURRENCY = "USD"

ACTIVE_ACCOUNT_P = 0.97  # per tick: pick an account active at this hour (design §3)
OFF_HOURS_BUDGET = 0.03  # cap on an account's off-hours share while no change is in effect
HOME_JITTER_KM = 10.0
EVENT_RADIUS_KM = 25.0  # in-profile events stay within this of the current location (< 50 km, Req 3.2)
NEW_PAYEE_SHARE = 0.3
CHANGE_START_MIN_S = 60.0
CHANGE_START_MAX_S = 3_600.0
AMOUNT_CAP = 1_000_000.0
_KM_PER_DEG = 111.32
_CENT = Decimal("0.01")
ENROL_LEAD = timedelta(days=1)  # back-dated device enrolment logins (before t0)
LOGIN_LEAD_S = (2.0, 20.0)  # LOGIN_SUCCESS this many seconds before its session's event
AUTH_SEED_SALT = 0xA17E  # second RNG stream for auth draws


@dataclass(frozen=True)
class AccountProfile:
    account_id: str
    mu: float
    sigma: float
    active_start: int  # UTC hour where the contiguous active block starts
    active_len: int  # hours, 6-14 (longer only where needed to cover every hour)
    active_mask: int  # 24-bit mask, bit h set iff hour h is active
    home_city: int
    home_lat: float
    home_lon: float
    channel_mix: tuple[float, ...]  # Dirichlet weights over CHANNELS
    counterparties: tuple[str, ...]  # 1-50 ids, never the account itself
    devices: tuple[str, ...]

    def is_active(self, hour: int) -> bool:
        return bool((self.active_mask >> hour) & 1)


@dataclass(frozen=True)
class BehaviourChange:
    account: int
    account_id: str
    kind: ChangeKind
    start: datetime
    end: Optional[datetime]  # None = permanent; travel reverts at `end`
    lat: Optional[float] = None  # relocation / travel destination
    lon: Optional[float] = None
    salary_factor: Optional[float] = None
    new_payee: Optional[str] = None

    def in_effect(self, t: datetime) -> bool:
        return self.start <= t and (self.end is None or t < self.end)


def validate_generator_config(cfg: GeneratorConfig, seed: Optional[int] = None) -> list[ConfigError]:
    """Every out-of-range generator setting (and the seed, if given). Empty = valid."""
    errs = [e for p, v, r in walk_ranges(cfg, "generator.") if (e := check(p, v, r)) is not None]
    if seed is not None and (isinstance(seed, bool) or not isinstance(seed, int) or not 0 <= seed <= SEED_MAX):
        errs.append(ConfigError("seed", seed, f"must be an integer in [0, {SEED_MAX}]"))
    return errs


def _mask(start: int, length: int) -> int:
    m = 0
    for k in range(min(length, 24)):
        m |= 1 << ((start + k) % 24)
    return m


def _offset(lat: float, lon: float, km: float, theta: float) -> tuple[float, float]:
    """Point `km` from (lat, lon) in direction `theta` (small-distance approximation)."""
    dlat = km * math.cos(theta) / _KM_PER_DEG
    dlon = km * math.sin(theta) / (_KM_PER_DEG * max(math.cos(math.radians(lat)), 1e-6))
    return round(lat + dlat, 6), round(lon + dlon, 6)


class SyntheticGenerator:
    def __init__(
        self,
        cfg: GeneratorConfig,
        clock: SimClock,
        gt: GroundTruthWriter,
        seed: Optional[int] = None,
        *,
        soc: Optional[SocConfig] = None,
    ):
        errors = validate_generator_config(cfg, seed)
        if errors:
            raise ConfigValidationError(errors)  # refuse to start, naming every bad setting
        self.cfg = cfg
        self.clock = clock
        self.seed = cfg.default_seed if seed is None else seed
        self._rng = np.random.default_rng(self.seed)
        self.t0 = clock.now()

        rate = float(cfg.rate_eps)
        self._gap = 1.0 / rate
        # |jitter| <= (1 - 1/rate) / 2 keeps every 10 s window within ±10% of 10*rate;
        # <= gap/4 keeps timestamps strictly increasing.
        self._jitter = min(0.25 * self._gap, 0.45 * (1.0 - self._gap))
        self._i = 0
        self._pending_ts: Optional[datetime] = None

        self.profiles: list[AccountProfile] = self._build_profiles(cfg.accounts)
        n = len(self.profiles)
        self._active_by_hour = [
            np.array([a for a, p in enumerate(self.profiles) if p.is_active(h)], dtype=np.int64)
            for h in range(24)
        ]
        self._channel_cdf = [np.cumsum(p.channel_mix) for p in self.profiles]
        self.changes: dict[int, BehaviourChange] = self._build_changes()
        # In-profile bookkeeping: events / off-hours events while no change is in effect.
        self._inprofile_total = np.zeros(n, dtype=np.int64)
        self._inprofile_off = np.zeros(n, dtype=np.int64)

        # Benign auth traffic (Req 28.5): off unless soc.enabled.
        self.soc = soc
        self.auth_enabled = bool(soc is not None and soc.enabled)
        self._auth_rng = np.random.default_rng([self.seed, AUTH_SEED_SALT])
        self._auth_n = 0
        self._enrolled = not self.auth_enabled
        self._recent_fails: dict[str, deque[datetime]] = {}  # account -> benign LOGIN_FAILURE times

        written_at = clock.now()
        for ch in self.changes.values():  # all before the first event (Req 3.8)
            gt.write(
                GroundTruthRecord(
                    event_id=ch.account_id,
                    label="LEGIT_CHANGE",
                    source=GTSource.GENERATOR,
                    written_at=written_at,
                    change_kind=ch.kind,
                    effective_at=ch.start,
                    effective_until=ch.end,
                )
            )

    # ------------------------------------------------------------------ setup

    def _build_profiles(self, n: int) -> list[AccountProfile]:
        rng = self._rng
        ids = [f"ACC{a:06d}" for a in range(n)]
        mus = rng.normal(math.log(60.0), 0.8, n)
        sigmas = rng.uniform(0.3, 0.8, n)
        starts = rng.integers(0, 24, n)
        lens = rng.integers(6, 15, n)
        cities = rng.integers(0, len(CITIES), n)
        jit_km = HOME_JITTER_KM * np.sqrt(rng.random(n))
        jit_theta = rng.uniform(0.0, 2 * math.pi, n)
        mixes = rng.dirichlet(np.ones(len(CHANNELS)), n)
        n_cp = rng.integers(1, 51, n)
        n_dev = rng.integers(1, 4, n)

        starts_l = [int(s) for s in starts]
        lens_l = [int(x) for x in lens]
        self._cover_all_hours(starts_l, lens_l)

        profiles: list[AccountProfile] = []
        for a in range(n):
            if n == 1:
                cps: tuple[str, ...] = ("EXT000000",)  # no other simulated account exists
            else:
                k = min(int(n_cp[a]), n - 1)
                picks = rng.choice(n - 1, size=k, replace=False)
                cps = tuple(ids[int(j) + (1 if j >= a else 0)] for j in picks)  # skip self
            _, clat, clon = CITIES[int(cities[a])]
            lat, lon = _offset(clat, clon, float(jit_km[a]), float(jit_theta[a]))
            profiles.append(
                AccountProfile(
                    account_id=ids[a],
                    mu=float(mus[a]),
                    sigma=float(sigmas[a]),
                    active_start=starts_l[a],
                    active_len=lens_l[a],
                    active_mask=_mask(starts_l[a], lens_l[a]),
                    home_city=int(cities[a]),
                    home_lat=lat,
                    home_lon=lon,
                    channel_mix=tuple(float(x) for x in mixes[a]),
                    counterparties=cps,
                    devices=tuple(f"DEV{a:06d}-{d}" for d in range(int(n_dev[a]))),
                )
            )
        return profiles

    @staticmethod
    def _cover_all_hours(starts: list[int], lens: list[int]) -> None:
        """Extend blocks so every hour has an active account (needed for small N)."""
        while True:
            covered = 0
            for s, length in zip(starts, lens):
                covered |= _mask(s, length)
            missing = [h for h in range(24) if not (covered >> h) & 1]
            if not missing:
                return
            h = missing[0]
            # The block whose end is closest before h grows to include h.
            a = min(range(len(starts)), key=lambda i: ((h - (starts[i] + lens[i])) % 24, i))
            lens[a] = min(24, (h - starts[a]) % 24 + 1)

    def _build_changes(self) -> dict[int, BehaviourChange]:
        rng, n = self._rng, len(self.profiles)
        count = math.floor(self.cfg.change_fraction * n + 0.5)  # halves round up (Req 3.6)
        accounts = rng.choice(n, size=count, replace=False) if count else np.empty(0, dtype=np.int64)
        kinds: tuple[ChangeKind, ...] = ("relocation", "salary", "new_payee", "travel")
        travel = timedelta(hours=float(self.cfg.travel_duration_h))
        out: dict[int, BehaviourChange] = {}
        for a in (int(x) for x in accounts):
            p = self.profiles[a]
            kind = kinds[int(rng.integers(0, 4))]
            start = self.t0 + timedelta(seconds=float(rng.uniform(CHANGE_START_MIN_S, CHANGE_START_MAX_S)))
            kw: dict = {}
            end = None
            if kind in ("relocation", "travel"):
                city = (p.home_city + int(rng.integers(1, len(CITIES)))) % len(CITIES)
                _, clat, clon = CITIES[city]
                kw["lat"], kw["lon"] = _offset(
                    clat, clon, HOME_JITTER_KM * math.sqrt(rng.random()), float(rng.uniform(0, 2 * math.pi))
                )
                if kind == "travel":
                    end = start + travel
            elif kind == "salary":
                kw["salary_factor"] = float(rng.uniform(1.5, 3.0))
            else:
                kw["new_payee"] = self._new_payee(a)
            out[a] = BehaviourChange(a, p.account_id, kind, start, end, **kw)
        return out

    def _new_payee(self, a: int) -> str:
        p, n = self.profiles[a], len(self.profiles)
        taken = set(p.counterparties) | {p.account_id}
        if n - len(taken) > 0:
            while True:
                cand = f"ACC{int(self._rng.integers(0, n)):06d}"
                if cand not in taken:
                    return cand
        return f"EXT{a:06d}"  # every simulated account is already a payee

    # --------------------------------------------------------------- emission

    def __iter__(self) -> Iterator[TransactionEvent]:
        return self

    def __next__(self) -> TransactionEvent:
        ts = self._peek_ts()
        self._pending_ts = None
        self._i += 1
        return self._make_event(ts)

    def events_until(self, t: Optional[datetime] = None) -> Iterator[TransactionEvent]:
        """Yield every not-yet-emitted event with ts <= t (default: clock.now()).

        With auth traffic enabled the stream also carries the benign Auth_Events: device
        enrolments first, then each event's session logins immediately before it."""
        limit = self.clock.now() if t is None else t
        if not self._enrolled:
            self._enrolled = True
            yield from self.enrolment_events()  # type: ignore[misc]
        while self._peek_ts() <= limit:
            ev = next(self)
            if self.auth_enabled:
                yield from self.session_auth_events(ev)  # type: ignore[misc]
            yield ev

    # ------------------------------------------------------------ auth traffic

    def _auth_id(self) -> str:
        self._auth_n += 1
        return f"AUTH-{self.seed}-{self._auth_n - 1:012d}"

    def enrolment_events(self) -> list[AuthEvent]:
        """One back-dated LOGIN_SUCCESS per account device (known-device baseline)."""
        out = []
        base = self.t0 - ENROL_LEAD
        k = 0
        for p in self.profiles:
            for d in p.devices:
                k += 1
                out.append(AuthEvent(
                    event_id=self._auth_id(), ts=base + timedelta(milliseconds=k), account_id=p.account_id,
                    session_id=f"ENROL-{p.account_id}-{d}", event_type=AuthEventType.LOGIN_SUCCESS,
                    device_id=d, lat=p.home_lat, lon=p.home_lon,
                ))
        return out

    def session_auth_events(self, ev: TransactionEvent) -> list[AuthEvent]:
        """Benign logins of the session that carries ``ev`` (Req 28.5)."""
        rng = self._auth_rng
        p = self.profiles[int(ev.src[3:])] if ev.src.startswith("ACC") else None
        devices = p.devices if p is not None else ("DEV-UNKNOWN",)
        device = ev.device_id if ev.device_id is not None else devices[int(rng.integers(0, len(devices)))]
        session = ev.session_id if ev.session_id is not None else f"S{int(rng.integers(0, 1 << 48)):012x}"
        lead = float(rng.uniform(*LOGIN_LEAD_S))
        login_ts = ev.ts - timedelta(seconds=lead)
        out = []
        frac = float(self.soc.login_failure_frac) if self.soc is not None else 0.0
        if rng.random() < frac:
            fails = 1 + int(rng.integers(0, 2))  # 1-2: a mistyped password
            # Never a burst: the account's benign failures inside any failed-login window stay
            # below the Suspicious_Auth_Activity count (draws above are made either way).
            assert self.soc is not None
            span = timedelta(seconds=float(self.soc.failed_login_window_s))
            recent = self._recent_fails.setdefault(ev.src, deque())
            while recent and recent[0] < login_ts - span - timedelta(seconds=10):
                recent.popleft()
            fails = min(fails, max(0, int(self.soc.failed_login_count) - 1 - len(recent)))
            for k in range(fails, 0, -1):
                recent.append(login_ts - timedelta(seconds=5 * k))
                out.append(AuthEvent(
                    event_id=self._auth_id(), ts=login_ts - timedelta(seconds=5 * k), account_id=ev.src,
                    session_id=session, event_type=AuthEventType.LOGIN_FAILURE, device_id=device,
                ))
        out.append(AuthEvent(
            event_id=self._auth_id(), ts=login_ts, account_id=ev.src, session_id=session,
            event_type=AuthEventType.LOGIN_SUCCESS, device_id=device, lat=ev.lat, lon=ev.lon,
        ))
        return out

    def _peek_ts(self) -> datetime:
        if self._pending_ts is None:
            off = (self._i + 0.5) * self._gap
            if self._jitter > 0:
                off += float(self._rng.uniform(-self._jitter, self._jitter))
            self._pending_ts = self.t0 + timedelta(microseconds=round(off * 1e6))
        return self._pending_ts

    def _pick_active(self, hour: int) -> int:
        arr = self._active_by_hour[hour]
        return int(arr[int(self._rng.integers(0, len(arr)))])

    def _make_event(self, ts: datetime) -> TransactionEvent:
        rng = self._rng
        hour = ts.hour
        if rng.random() < ACTIVE_ACCOUNT_P:
            a = self._pick_active(hour)
        else:
            a = int(rng.integers(0, len(self.profiles)))
            ch0 = self.changes.get(a)
            if (
                not self.profiles[a].is_active(hour)
                and not (ch0 is not None and ch0.in_effect(ts))
                and self._inprofile_off[a] + 1 > OFF_HOURS_BUDGET * (self._inprofile_total[a] + 1)
            ):
                a = self._pick_active(hour)  # off-hours budget spent: keep Req 3.2's 95%
        p = self.profiles[a]
        ch = self.changes.get(a)
        change = ch if ch is not None and ch.in_effect(ts) else None
        if change is None:
            self._inprofile_total[a] += 1
            self._inprofile_off[a] += 0 if p.is_active(hour) else 1

        # Destination: listed counterparty, or the new regular payee once that change starts.
        if change is not None and change.kind == "new_payee" and rng.random() < NEW_PAYEE_SHARE:
            dst = change.new_payee
        else:
            dst = p.counterparties[int(rng.integers(0, len(p.counterparties)))]

        mu = p.mu + (math.log(change.salary_factor) if change is not None and change.kind == "salary" else 0.0)
        x = min(max(math.exp(float(rng.normal(mu, p.sigma))), 0.01), AMOUNT_CAP)
        amount = max(Decimal(f"{x:.2f}"), _CENT)

        if change is not None and change.kind in ("relocation", "travel"):
            base_lat, base_lon = change.lat, change.lon
        else:
            base_lat, base_lon = p.home_lat, p.home_lon
        lat, lon = _offset(
            base_lat, base_lon, EVENT_RADIUS_KM * math.sqrt(rng.random()), float(rng.uniform(0, 2 * math.pi))
        )

        cdf = self._channel_cdf[a]
        ci = min(int(np.searchsorted(cdf, rng.random(), side="right")), len(CHANNELS) - 1)
        channel = CHANNELS[ci]
        mccs = MCC_BY_CHANNEL[channel]
        mcc = mccs[int(rng.integers(0, len(mccs)))]
        device = p.devices[int(rng.integers(0, len(p.devices)))]
        session = f"S{int(rng.integers(0, 1 << 48)):012x}"

        values = {"channel": channel, "mcc": mcc, "lat": lat, "lon": lon, "device_id": device, "session_id": session}
        omit = rng.random(len(OPTIONAL_FIELDS)) < self.cfg.omit_frac  # independent per field (Req 3.9)
        optional = {f: (None if o else values[f]) for f, o in zip(OPTIONAL_FIELDS, omit)}
        return TransactionEvent(
            tx_id=f"GEN-{self.seed}-{self._i - 1:012d}",
            ts=ts,
            src=p.account_id,
            dst=dst,
            amount=amount,
            currency=CURRENCY,
            **optional,
        )
