"""Campaign generators: ``GENERATORS[ptype](genome, ctx) -> list[TransactionEvent]`` (design §17).

Every event is an ordinary ``TransactionEvent``: only schema fields, no campaign, genome or
label marker. Ids are opaque hashes (``TX`` + 24 hex) and fresh accounts look like the
generator's external accounts (``EXT`` / ``MER`` + digits), so neither the schema nor the
id shape says "attack". Events are returned sorted by ``ts`` (non-decreasing).

Account selection draws victims and payees from a ``TargetPool``: a Canary-free list of the
Synthetic_Generator's account ids plus fresh mule ids minted by the pool itself. The pool
accepts no Canary information at all (Req 20.16, 33).

Account takeover: the transaction leg (new device, far-away location, large transfers to
fresh payees). With ``CampaignContext.auth`` (set by the agent when ``soc.enabled``, Req 28.6)
it is preceded by an Auth_Event leg in one of four sequences (drawn per campaign, or forced
with ``CampaignContext.auth_variant``):

- ``account_takeover``: password guessing, i.e. a burst of LOGIN_FAILUREs from the attacker's
  device in one session, then a LOGIN_SUCCESS from that (new) device;
- ``credential_stuffing``: a burst of LOGIN_FAILUREs from rotating IPs, then a LOGIN_SUCCESS
  from the attacker's device;
- ``sim_swap``: PASSWORD_RESET, MFA_SUCCESS and LOGIN_SUCCESS from the attacker's device;
- ``device_takeover``: one or two MFA_FAILUREs, MFA_SUCCESS, LOGIN_SUCCESS on a new device;

each followed by NEW_PAYEE_ADDED for the first payee. The transaction leg then runs in the
attacker's login session, shifted to start after the auth leg. Auth draws use a separate RNG
derived from the campaign id, so the transaction leg's random draws (amounts, payees,
locations) are identical with and without the auth leg. Events are still sorted by ``ts``
and capped at ``max_events`` (the auth leg is trimmed from its start if needed, so the
campaign always keeps at least one Transaction_Event).
"""

from __future__ import annotations

import hashlib
import math
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any, Callable, Optional, Sequence

import numpy as np

from helix.codec import OPTIONAL_FIELDS, AuthEvent, AuthEventType, TransactionEvent
from helix.codec.models import AMOUNT_MAX
from helix.sim.generator import CITIES, MCC_BY_CHANNEL

from .genome import PatternGenome, PatternType

CURRENCY = "USD"
OMIT_FRAC = 0.10  # same optional-field omission rate as the generator default
TIMING_JITTER = 0.2  # every gap is scaled by U(1 - j, 1 + j)
_CENT = Decimal("0.01")
_KM_PER_DEG = 111.32


class TargetPool:
    """Victim / payee selection: generator accounts plus fresh mule ids. No Canary input."""

    def __init__(self, accounts: Sequence[str] = ()) -> None:
        # Sorted and de-duplicated so selection does not depend on caller ordering.
        self.accounts: tuple[str, ...] = tuple(sorted(set(accounts)))
        self._fresh = 0
        self._merchants = 0
        self._devices = 0

    def fresh(self) -> str:
        self._fresh += 1
        return f"EXT{self._fresh:09d}"

    def merchant(self) -> str:
        self._merchants += 1
        return f"MER{self._merchants:09d}"

    def device(self) -> str:
        self._devices += 1
        return f"DEV{self._devices:09d}-0"

    def pick(self, k: int, fresh_share: float, rng: np.random.Generator) -> list[str]:
        """``k`` distinct accounts; each slot is fresh with probability ``fresh_share``."""
        fresh_slots = rng.random(k) < fresh_share
        n_pool = min(int((~fresh_slots).sum()), len(self.accounts))
        pool_ids = iter(
            [self.accounts[int(i)] for i in rng.choice(len(self.accounts), size=n_pool, replace=False)]
            if n_pool
            else []
        )
        out: list[str] = []
        for is_fresh in fresh_slots:
            nxt = None if is_fresh else next(pool_ids, None)
            out.append(nxt if nxt is not None else self.fresh())
        return out


@dataclass
class CampaignContext:
    rng: np.random.Generator
    start: datetime
    targets: TargetPool
    id_prefix: str  # unique per campaign; hashed into opaque tx ids
    reporting_threshold: Decimal
    max_events: int
    auth: bool = False  # emit the Auth_Event leg of account-takeover campaigns (soc.enabled)
    auth_variant: Optional[str] = None  # force one of AUTH_VARIANTS; None = drawn per campaign


# ---------------------------------------------------------------------------- helpers


class _Builder:
    def __init__(self, ctx: CampaignContext) -> None:
        self.ctx = ctx
        self.events: list[TransactionEvent] = []

    def gap(self, seconds: float) -> timedelta:
        j = float(self.ctx.rng.uniform(1 - TIMING_JITTER, 1 + TIMING_JITTER))
        return timedelta(milliseconds=round(seconds * j * 1000))

    def amount(self, x: float) -> Decimal:
        x = min(max(x, 0.01), float(AMOUNT_MAX))
        return max(Decimal(f"{x:.2f}"), _CENT)

    def location(self, city: Optional[int] = None) -> tuple[float, float]:
        rng = self.ctx.rng
        c = int(rng.integers(0, len(CITIES))) if city is None else city
        _, lat, lon = CITIES[c]
        km, th = 25.0 * math.sqrt(rng.random()), float(rng.uniform(0, 2 * math.pi))
        dlon = km * math.sin(th) / (_KM_PER_DEG * max(math.cos(math.radians(lat)), 1e-6))
        return round(lat + km * math.cos(th) / _KM_PER_DEG, 6), round(lon + dlon, 6)

    def add(
        self,
        ts: datetime,
        src: str,
        dst: str,
        amount: float | Decimal,
        channel: str,
        device: Optional[str] = None,
        loc: Optional[tuple[float, float]] = None,
    ) -> None:
        rng = self.ctx.rng
        i = len(self.events)
        digest = hashlib.sha1(f"{self.ctx.id_prefix}:{i}".encode()).hexdigest()[:24]
        mccs = MCC_BY_CHANNEL[channel]
        lat, lon = loc if loc is not None else self.location()
        values = {
            "channel": channel,
            "mcc": mccs[int(rng.integers(0, len(mccs)))],
            "lat": lat,
            "lon": lon,
            "device_id": device if device is not None else self.ctx.targets.device(),
            "session_id": f"S{int(rng.integers(0, 1 << 48)):012x}",
        }
        omit = rng.random(len(OPTIONAL_FIELDS)) < OMIT_FRAC
        optional = {f: (None if o else values[f]) for f, o in zip(OPTIONAL_FIELDS, omit)}
        amt = amount if isinstance(amount, Decimal) else self.amount(amount)
        self.events.append(
            TransactionEvent(tx_id=f"TX{digest}", ts=ts, src=src, dst=dst, amount=amt, currency=CURRENCY, **optional)
        )

    def done(self) -> list[TransactionEvent]:
        evs = sorted(self.events, key=lambda e: e.ts)  # stable: equal ts keep build order
        return evs[: self.ctx.max_events]


# ---------------------------------------------------------------------------- generators


def gen_round_trip(g: PatternGenome, ctx: CampaignContext) -> list[TransactionEvent]:
    p, b = g.params, _Builder(ctx)
    ring = ctx.targets.pick(int(p["cycle_len"]), float(p["fresh_share"]), ctx.rng)
    t, amt = ctx.start, float(p["amount"])
    for _ in range(int(p["repeats"])):
        for i, src in enumerate(ring):
            b.add(t, src, ring[(i + 1) % len(ring)], amt, "transfer")
            amt *= float(p["decay"])
            t += b.gap(float(p["gap_s"]))  # > 0: strictly increasing hops
    return b.done()


def _fan(g: PatternGenome, ctx: CampaignContext, outgoing: bool) -> list[TransactionEvent]:
    p, b, rng = g.params, _Builder(ctx), ctx.rng
    accts = ctx.targets.pick(int(p["n_counterparties"]) + 1, float(p["fresh_share"]), rng)
    hub, cps = accts[0], accts[1:]
    t, base, jit = ctx.start, float(p["amount"]), float(p["amount_jitter"])
    for cp in cps:
        amt = base * float(rng.uniform(1 - jit, 1 + jit))
        src, dst = (hub, cp) if outgoing else (cp, hub)
        b.add(t, src, dst, amt, "transfer")
        t += b.gap(float(p["gap_s"]))
    return b.done()


def gen_fan_out(g: PatternGenome, ctx: CampaignContext) -> list[TransactionEvent]:
    return _fan(g, ctx, True)


def gen_fan_in(g: PatternGenome, ctx: CampaignContext) -> list[TransactionEvent]:
    return _fan(g, ctx, False)


def gen_structuring(g: PatternGenome, ctx: CampaignContext) -> list[TransactionEvent]:
    p, b, rng = g.params, _Builder(ctx), ctx.rng
    accts = ctx.targets.pick(int(p["n_dst"]) + 1, float(p["fresh_share"]), rng)
    src, dsts = accts[0], accts[1:]
    rt = float(ctx.reporting_threshold)
    top, jit = float(p["band_frac"]), float(p["band_jitter"])
    t = ctx.start
    for i in range(int(p["n_tx"])):
        amt = b.amount(rt * top * (1 - jit * float(rng.random())))
        b.add(t, src, dsts[i % len(dsts)], amt, "transfer")
        t += b.gap(float(p["gap_s"]))
    return b.done()


def gen_mule_chain(g: PatternGenome, ctx: CampaignContext) -> list[TransactionEvent]:
    p, b = g.params, _Builder(ctx)
    chain = ctx.targets.pick(int(p["hops"]) + 1, float(p["fresh_share"]), ctx.rng)
    t, amt = ctx.start, b.amount(float(p["amount"]))
    for i in range(len(chain) - 1):
        b.add(t, chain[i], chain[i + 1], amt, "transfer")
        amt = b.amount(float(amt) * float(p["forward_ratio"]))
        t += b.gap(float(p["hop_delay_s"]))
    return b.done()


AUTH_VARIANTS: tuple[str, ...] = ("account_takeover", "credential_stuffing", "sim_swap", "device_takeover")
AUTH_LEG_GAP_S = 60.0  # pause between the last auth event and the first transfer


def _auth_rng(ctx: CampaignContext) -> np.random.Generator:
    digest = hashlib.sha1(f"{ctx.id_prefix}:auth".encode()).digest()
    return np.random.default_rng(int.from_bytes(digest[:8], "big"))


def _ip(rng: np.random.Generator) -> str:
    return "{}.{}.{}.{}".format(*(int(x) for x in rng.integers(1, 255, 4)))


def auth_leg(
    ctx: CampaignContext, victim: str, device: str, payee: str, variant: Optional[str] = None
) -> tuple[list[AuthEvent], str, timedelta]:
    """Auth_Event leg of a takeover: ``(events, attacker session id, duration)``."""
    rng = _auth_rng(ctx)
    v = variant or ctx.auth_variant or AUTH_VARIANTS[int(rng.integers(0, len(AUTH_VARIANTS)))]
    if v not in AUTH_VARIANTS:
        raise ValueError(f"unknown auth variant {v!r}; expected one of {AUTH_VARIANTS}")
    session = f"S{int(rng.integers(0, 1 << 48)):012x}"
    ip = _ip(rng)
    out: list[AuthEvent] = []
    t = ctx.start

    def add(kind: AuthEventType, gap_s: float, **kw: object) -> None:
        nonlocal t
        i = len(out)
        digest = hashlib.sha1(f"{ctx.id_prefix}:auth:{i}".encode()).hexdigest()[:24]
        kw.setdefault("session_id", session)
        out.append(AuthEvent(event_id=f"AU{digest}", ts=t, account_id=victim, event_type=kind, **kw))  # type: ignore[arg-type]
        t += timedelta(milliseconds=round(gap_s * 1000 * float(rng.uniform(0.8, 1.2))))

    if v == "account_takeover":
        for _ in range(int(rng.integers(5, 9))):  # >= the default burst count of 5, one session
            add(AuthEventType.LOGIN_FAILURE, 20.0, device_id=device, ip=ip)
        add(AuthEventType.LOGIN_SUCCESS, 30.0, device_id=device, ip=ip)
    elif v == "credential_stuffing":
        for _ in range(int(rng.integers(6, 13))):  # >= the default burst count of 5
            add(AuthEventType.LOGIN_FAILURE, 15.0, session_id=f"S{int(rng.integers(0, 1 << 48)):012x}",
                device_id=device, ip=_ip(rng))
        add(AuthEventType.LOGIN_SUCCESS, 30.0, device_id=device, ip=ip)
    elif v == "sim_swap":
        add(AuthEventType.PASSWORD_RESET, 45.0, device_id=device, ip=ip)
        add(AuthEventType.MFA_SUCCESS, 10.0, device_id=device, ip=ip)
        add(AuthEventType.LOGIN_SUCCESS, 30.0, device_id=device, ip=ip)
    else:
        for _ in range(int(rng.integers(1, 3))):
            add(AuthEventType.MFA_FAILURE, 20.0, device_id=device, ip=ip)
        add(AuthEventType.MFA_SUCCESS, 10.0, device_id=device, ip=ip)
        add(AuthEventType.LOGIN_SUCCESS, 30.0, device_id=device, ip=ip)
    add(AuthEventType.NEW_PAYEE_ADDED, 0.0, device_id=device, ip=ip, payee_account_id=payee)
    return out, session, (t - ctx.start) + timedelta(seconds=AUTH_LEG_GAP_S)


def gen_account_takeover(g: PatternGenome, ctx: CampaignContext) -> list[Any]:
    """Transaction leg; with ``ctx.auth`` preceded by an Auth_Event leg (Req 20.2, 28.6)."""
    p, b, rng, tp = g.params, _Builder(ctx), ctx.rng, ctx.targets
    victim = tp.pick(1, 0.0, rng)[0]
    payees = [a for a in tp.pick(int(p["n_payees"]) + 1, float(p["fresh_share"]), rng) if a != victim]
    payees = payees[: int(p["n_payees"])] or [tp.fresh()]
    device = tp.device()  # the attacker's device, never seen on the victim before
    loc = b.location()
    channels = ("online", "mobile")
    t = ctx.start
    for i in range(int(p["n_tx"])):
        amt = float(p["amount"]) * float(rng.uniform(0.8, 1.2))
        b.add(t, victim, payees[i % len(payees)], amt, channels[i % 2], device=device, loc=loc)
        t += b.gap(float(p["gap_s"]))
    txs = b.done()
    if not ctx.auth:
        return txs
    auth, session, shift = auth_leg(ctx, victim, device, payees[0])
    txs = [e.model_copy(update={"ts": e.ts + shift,
                                "session_id": session if e.session_id is not None else None}) for e in txs]
    room = max(0, ctx.max_events - len(txs))
    if room < len(auth):
        auth = auth[len(auth) - room:] if room else []  # keep the latest auth steps
    return sorted([*auth, *txs], key=lambda e: e.ts)


def gen_card_testing(g: PatternGenome, ctx: CampaignContext) -> list[TransactionEvent]:
    """Many small tests on a stolen card at new merchants, then one large purchase."""
    p, b, rng, tp = g.params, _Builder(ctx), ctx.rng, ctx.targets
    card = tp.pick(1, float(p["fresh_share"]), rng)[0]
    merchants = [tp.merchant() for _ in range(int(p["n_merchants"]))]
    device = tp.device()
    t = ctx.start
    for i in range(int(p["n_tests"])):
        amt = float(p["test_amount"]) * float(rng.uniform(0.5, 1.5))
        b.add(t, card, merchants[i % len(merchants)], amt, "online", device=device)
        t += b.gap(float(p["gap_s"]))
    b.add(t, card, tp.merchant(), float(p["final_amount"]), "card", device=device)
    return b.done()


Generator = Callable[[PatternGenome, CampaignContext], list[TransactionEvent]]

GENERATORS: dict[PatternType, Generator] = {
    PatternType.ROUND_TRIP_CYCLE: gen_round_trip,
    PatternType.FAN_OUT: gen_fan_out,
    PatternType.FAN_IN: gen_fan_in,
    PatternType.STRUCTURING: gen_structuring,
    PatternType.MULE_CHAIN: gen_mule_chain,
    PatternType.ACCOUNT_TAKEOVER: gen_account_takeover,
    PatternType.CARD_TESTING: gen_card_testing,
}


def generate(g: PatternGenome, ctx: CampaignContext) -> list[TransactionEvent]:
    return GENERATORS[g.ptype](g, ctx)


__all__ = ["AUTH_VARIANTS", "GENERATORS", "CampaignContext", "TargetPool", "auth_leg", "generate"]
