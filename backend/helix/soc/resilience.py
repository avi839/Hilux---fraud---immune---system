"""Resilience_Mode (Req 37.5-37.8): inject sensor failures, delayed events and traffic bursts.

Design decisions:
- Installed around the existing components, without editing ``helix.sensors`` or the pipeline:

  * ``FaultySensor`` wraps one Sensor_Agent (same ``name``). While the mode is enabled each call
    fails with probability ``sensor_failure`` (or per an explicit ``fail_decider(sensor, tx_id)``)
    by raising ``InjectedSensorFailure``. The unchanged sensor runner (``run_sensors``) records
    an ERROR outcome and substitutes the Vacuous_Mass_Function, so processing continues and the
    failed sensor is neutral in fusion (Req 37.6). ``install_sensors(helix)`` wraps
    ``helix.stage1.sensors`` *in place* (the list is shared with an ``ImmuneStage1Engine``).
  * ``ResilientIngestor`` is a proxy for the Stream_Ingestor (every other attribute, including
    assignments such as ``config``, is forwarded). Its ``submit`` runs the event through the
    mode's ``EventShaper``: with probability ``delay`` an event is held back until
    ``delay_events`` later events were submitted (it then arrives out of order and the
    ingestor classifies its lateness as usual); with probability ``burst`` an event starts a
    burst: it and the next ``burst_size - 1`` events are gathered and submitted back to back.
    Nothing is invented or dropped: the released events are a permutation of the submitted ones
    (Property "shaper conservation" in the tests). A held event is acknowledged immediately as
    ``accepted`` (``IngestResult`` with the event's id when readable) and admitted on release;
    the release outcome is counted (``released_rejected`` / ``released_duplicate``). Auth_Events
    (``submit_auth``) pass through unshaped.
  * ``TeeLatency`` wraps ``helix.latency``: every sample still goes to the pipeline's recorder,
    and samples taken while the mode is enabled also go to the mode's own ``LatencyRecorder``,
    so the status reports the per-stage latency percentiles *of the degraded period*.
- The mode starts disabled (Req 37.5). ``enable(rates)`` resets the counters, the RNG (seeded,
  so a run is reproducible for a deterministic pipeline) and the latency recorder; ``disable()``
  releases every held event (through each installed proxy) and turns injection off; wrappers
  stay installed and pass through.
- Counters (Req 37.7): sensor calls and injected failures (overall and per sensor), delayed and
  released events, bursts and burst events, inbound queue length (current, held, peak while
  enabled). Honest note: Req 37.7 names the Evaluation_Monitor as the recorder; the counters live
  on this object so the monitor stays untouched, and the integrator can expose them next to it.
- Invariant (Req 37.8): fusion is unchanged, so every Fused_Assessment stays a valid mass
  function under any failure pattern; the property test drives the real pipeline with
  arbitrary per-(event, sensor) failure patterns and checks it.
"""

from __future__ import annotations

import json
import logging
import math
import random
import threading
from collections import Counter
from collections.abc import Callable, Iterable, Iterator
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any, Optional

from helix.core.pipeline import LatencyRecorder

log = logging.getLogger(__name__)

MAX_DELAY_EVENTS = 1_000
MAX_BURST_SIZE = 1_000


class InjectedSensorFailure(RuntimeError):
    """Raised by a ``FaultySensor`` for an injected failure."""


def _rate(name: str, v: Any, problems: list[str]) -> None:
    if isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v) or not 0.0 <= v <= 1.0:
        problems.append(f"{name} must be a number in [0, 1], got {v!r}")


def _int(name: str, v: Any, lo: int, hi: int, problems: list[str]) -> None:
    if isinstance(v, bool) or not isinstance(v, int) or not lo <= v <= hi:
        problems.append(f"{name} must be an integer in [{lo}, {hi}], got {v!r}")


@dataclass(frozen=True, slots=True)
class ResilienceRates:
    sensor_failure: float = 0.0  # probability that one Sensor_Agent call fails
    delay: float = 0.0  # probability that one submitted event is delayed
    burst: float = 0.0  # probability that one submitted event starts a burst
    delay_events: int = 5  # a delayed event is released after this many later submits
    burst_size: int = 10  # events gathered into one burst (including the first)
    seed: int = 0

    def problems(self) -> list[str]:
        out: list[str] = []
        _rate("sensor_failure_rate", self.sensor_failure, out)
        _rate("delay_rate", self.delay, out)
        _rate("burst_rate", self.burst, out)
        _int("delay_events", self.delay_events, 1, MAX_DELAY_EVENTS, out)
        _int("burst_size", self.burst_size, 2, MAX_BURST_SIZE, out)
        _int("seed", self.seed, 0, 2**32 - 1, out)
        return out

    def validated(self) -> ResilienceRates:
        p = self.problems()
        if p:
            raise ValueError("; ".join(p))
        return self

    def to_dict(self) -> dict[str, Any]:
        return {"sensor_failure_rate": self.sensor_failure, "delay_rate": self.delay, "burst_rate": self.burst,
                "delay_events": self.delay_events, "burst_size": self.burst_size, "seed": self.seed}


@dataclass(slots=True)
class ResilienceCounters:
    sensor_calls: int = 0
    sensor_failures: int = 0
    events_shaped: int = 0
    delayed_events: int = 0
    released_delayed: int = 0
    bursts: int = 0
    burst_events: int = 0
    deferred_acks: int = 0  # submits acknowledged before admission (held events)
    released_rejected: int = 0
    released_duplicate: int = 0
    failures_by_sensor: Counter = field(default_factory=Counter)

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["failures_by_sensor"] = dict(sorted(self.failures_by_sensor.items()))
        return d


class ResilienceMode:
    """Fault-injection state shared by the installed wrappers."""

    def __init__(self, *, fail_decider: Optional[Callable[[str, str], bool]] = None,
                 wall_clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc)) -> None:
        self.enabled = False
        self.rates = ResilienceRates()
        #: explicit failure pattern ``(sensor name, tx_id) -> fail?`` (tests); overrides the rate
        self.fail_decider = fail_decider
        self.counters = ResilienceCounters()
        self.latency = LatencyRecorder()
        self.peak_queue_length = 0
        self.enabled_at: Optional[datetime] = None
        self.disabled_at: Optional[datetime] = None
        self._wall = wall_clock
        self._rng = random.Random(0)
        self._lock = threading.RLock()
        self._proxies: list[ResilientIngestor] = []
        self._engines: list[Any] = []
        self._latency_hosts: list[Any] = []

    # --- lifecycle ------------------------------------------------------------------------

    def enable(self, rates: Optional[ResilienceRates] = None) -> None:
        r = (rates or self.rates).validated()
        with self._lock:
            self.rates = r
            self.counters = ResilienceCounters()
            self.latency = LatencyRecorder()
            self.peak_queue_length = 0
            self._rng = random.Random(r.seed)
            self.enabled = True
            self.enabled_at, self.disabled_at = self._wall(), None

    def disable(self) -> int:
        """Turn injection off and release every held event; returns the number released."""
        with self._lock:
            self.enabled = False
            self.disabled_at = self._wall()
        return sum(p.flush() for p in list(self._proxies))

    # --- decisions --------------------------------------------------------------------------

    def should_fail(self, sensor: str, tx_id: str) -> bool:
        if not self.enabled:
            return False
        with self._lock:
            self.counters.sensor_calls += 1
            fail = bool(self.fail_decider(sensor, tx_id)) if self.fail_decider is not None \
                else self._rng.random() < self.rates.sensor_failure
            if fail:
                self.counters.sensor_failures += 1
                self.counters.failures_by_sensor[sensor] += 1
            return fail

    def shaper(self) -> EventShaper:
        return EventShaper(self)

    def sample_queue(self, n: int) -> None:
        if self.enabled and n > self.peak_queue_length:
            self.peak_queue_length = n

    # --- installation -----------------------------------------------------------------------

    def install_sensors(self, target: Any) -> int:
        """Wrap the Sensor_Agents of ``target`` (a ``Helix`` or a stage-1 engine) in place.
        Idempotent; returns the number of wrapped sensors."""
        eng = getattr(target, "stage1", target)
        sensors = eng.sensors
        sensors[:] = [s if isinstance(s, FaultySensor) else FaultySensor(s, self) for s in sensors]
        if eng not in self._engines:
            self._engines.append(eng)
        return len(sensors)

    def uninstall_sensors(self) -> None:
        for eng in self._engines:
            eng.sensors[:] = [s.inner if isinstance(s, FaultySensor) else s for s in eng.sensors]
        self._engines.clear()

    def install_latency(self, helix: Any) -> None:
        if not isinstance(helix.latency, TeeLatency):
            helix.latency = TeeLatency(helix.latency, self)
        if helix not in self._latency_hosts:
            self._latency_hosts.append(helix)

    def wrap_ingestor(self, ingestor: Any) -> ResilientIngestor:
        if isinstance(ingestor, ResilientIngestor):
            return ingestor
        p = ResilientIngestor(ingestor, self)
        self._proxies.append(p)
        return p

    def install(self, state: Any) -> ResilienceMode:
        """Install every wrapper on an API ``AppState`` (``helix`` sensors and latency,
        ``ingestor`` proxy) and expose the mode as ``state.resilience``. Idempotent."""
        self.install_sensors(state.helix)
        self.install_latency(state.helix)
        state.ingestor = self.wrap_ingestor(state.ingestor)
        state.resilience = self
        return self

    @property
    def held(self) -> int:
        return sum(p.shaper.held for p in self._proxies)

    # --- batch use ----------------------------------------------------------------------------

    def perturb(self, events: Iterable[Any]) -> Iterator[Any]:
        """The shaped order of ``events`` (delays and bursts applied, everything released at the
        end): for batch replays and tests."""
        sh = EventShaper(self)
        for ev in events:
            yield from sh.push(ev)
        yield from sh.drain()

    # --- status ----------------------------------------------------------------------------------

    def status(self, queue_length: Optional[int] = None) -> dict[str, Any]:
        q = queue_length
        if q is None and self._proxies:
            q = len(self._proxies[0])
        return {
            "enabled": self.enabled,
            "rates": self.rates.to_dict(),
            "enabled_at": None if self.enabled_at is None else self.enabled_at.isoformat(),
            "disabled_at": None if self.disabled_at is None else self.disabled_at.isoformat(),
            "counters": self.counters.to_dict(),
            "queue": {"length": q, "held": self.held, "peak_while_enabled": self.peak_queue_length},
            "latency_ms": self.latency.report(),
            "installed": {"sensor_engines": len(self._engines), "ingestors": len(self._proxies),
                          "latency": bool(self._latency_hosts)},
        }


class EventShaper:
    """Delays and bursts over a stream of items; conserves the items (a permutation)."""

    def __init__(self, mode: ResilienceMode) -> None:
        self.mode = mode
        self._held: list[list[Any]] = []  # [remaining submits, item]
        self._burst: Optional[list[Any]] = None

    @property
    def held(self) -> int:
        return len(self._held) + (len(self._burst) if self._burst is not None else 0)

    def drain(self) -> list[Any]:
        """Release everything held (a partial burst first, then delayed events in hold order)."""
        out = list(self._burst or []) + [it for _, it in self._held]
        c = self.mode.counters
        c.burst_events += len(self._burst or [])
        c.released_delayed += len(self._held)
        self._burst = None
        self._held = []
        return out

    def push(self, item: Any) -> list[Any]:
        """Items to submit now, in order. When the mode is disabled everything held is released
        first and ``item`` passes through."""
        m = self.mode
        if not m.enabled:
            return [*self.drain(), item]
        with m._lock:
            r = m.rates
            c = m.counters
            c.events_shaped += 1
            out: list[Any] = []
            new_hold = None
            if self._burst is not None:
                self._burst.append(item)
                if len(self._burst) >= r.burst_size:
                    out += self._burst
                    c.burst_events += len(self._burst)
                    self._burst = None
            elif m._rng.random() < r.delay:
                new_hold = [r.delay_events, item]
                c.delayed_events += 1
            elif m._rng.random() < r.burst:
                c.bursts += 1
                self._burst = [item]
            else:
                out.append(item)
            due: list[Any] = []
            keep: list[list[Any]] = []
            for h in self._held:
                h[0] -= 1
                (due if h[0] <= 0 else keep).append(h)
            if new_hold is not None:
                keep.append(new_hold)
            self._held = keep
            c.released_delayed += len(due)
            return out + [it for _, it in due]


class FaultySensor:
    """A Sensor_Agent wrapper that fails on demand of the ``ResilienceMode``."""

    def __init__(self, inner: Any, mode: ResilienceMode) -> None:
        self.inner = inner
        self.mode = mode
        self.name = str(inner.name)

    def evaluate(self, ev: Any, dna_before: Any, ctx: Any) -> Any:
        if self.mode.should_fail(self.name, str(getattr(ev, "tx_id", ""))):
            raise InjectedSensorFailure(f"injected sensor failure ({self.name})")
        return self.inner.evaluate(ev, dna_before, ctx)

    def __getattr__(self, name: str) -> Any:
        if name.startswith("__") or "inner" not in self.__dict__:
            raise AttributeError(name)
        return getattr(self.__dict__["inner"], name)


def _event_id(raw: Any) -> Optional[str]:
    tx = getattr(raw, "tx_id", None)
    if isinstance(tx, str):
        return tx
    if isinstance(raw, (str, bytes, bytearray)):
        try:
            doc = json.loads(raw)
        except (ValueError, UnicodeDecodeError):
            return None
        v = doc.get("tx_id") if isinstance(doc, dict) else None
        return v if isinstance(v, str) else None
    return None


class ResilientIngestor:
    """Stream_Ingestor proxy applying the mode's delays and bursts to ``submit``."""

    _OWN = frozenset({"inner", "mode", "shaper", "_seq"})

    def __init__(self, inner: Any, mode: ResilienceMode) -> None:
        object.__setattr__(self, "inner", inner)
        object.__setattr__(self, "mode", mode)
        object.__setattr__(self, "shaper", EventShaper(mode))
        object.__setattr__(self, "_seq", 0)

    def __getattr__(self, name: str) -> Any:
        if name.startswith("__") or "inner" not in self.__dict__:
            raise AttributeError(name)
        return getattr(self.__dict__["inner"], name)

    def __setattr__(self, name: str, value: Any) -> None:
        if name in self._OWN:
            object.__setattr__(self, name, value)
        else:
            setattr(self.inner, name, value)

    def __len__(self) -> int:
        return len(self.inner)

    def _release(self, raw: Any) -> Any:
        res = self.inner.submit(raw)
        st = str(getattr(getattr(res, "status", None), "value", ""))
        c = self.mode.counters
        if st == "rejected":
            c.released_rejected += 1
        elif st == "duplicate":
            c.released_duplicate += 1
        return res

    def submit(self, raw: Any) -> Any:
        if not self.mode.enabled and not self.shaper.held:
            return self.inner.submit(raw)
        object.__setattr__(self, "_seq", self._seq + 1)
        token = self._seq
        mine = None
        for tok, item in self.shaper.push((token, raw)):
            if tok == token:
                mine = self.inner.submit(item)
            else:
                self._release(item)
        self.mode.sample_queue(len(self.inner) + self.shaper.held)
        if mine is not None:
            return mine
        from helix.ingest import IngestResult, IngestStatus

        self.mode.counters.deferred_acks += 1
        return IngestResult(IngestStatus.ACCEPTED, _event_id(raw), backpressure=bool(self.inner.backpressure))

    def flush(self) -> int:
        items = self.shaper.drain()
        for _, raw in items:
            self._release(raw)
        return len(items)


class TeeLatency:
    """``LatencyRecorder`` proxy: samples also go to the mode's recorder while it is enabled."""

    def __init__(self, inner: Any, mode: ResilienceMode) -> None:
        self.inner = inner
        self.mode = mode

    def record(self, stage: Any, ms: float, at: Optional[float] = None) -> None:
        self.inner.record(stage, ms, at)
        if self.mode.enabled:
            self.mode.latency.record(stage, ms)

    def __getattr__(self, name: str) -> Any:
        if name.startswith("__") or "inner" not in self.__dict__:
            raise AttributeError(name)
        return getattr(self.__dict__["inner"], name)


def rates_from_doc(doc: dict[str, Any], base: Optional[ResilienceRates] = None) -> ResilienceRates:
    """``ResilienceRates`` from the API document keys (``sensor_failure_rate``, ``delay_rate``,
    ``burst_rate``, ``delay_events``, ``burst_size``, ``seed``); missing keys keep ``base``.
    Raises ValueError naming every bad key."""
    b = base or ResilienceRates()
    keys = {"sensor_failure_rate": "sensor_failure", "delay_rate": "delay", "burst_rate": "burst",
            "delay_events": "delay_events", "burst_size": "burst_size", "seed": "seed"}
    kw = {attr: doc.get(k, getattr(b, attr)) for k, attr in keys.items()}
    return ResilienceRates(**kw).validated()


__all__ = [
    "EventShaper",
    "FaultySensor",
    "InjectedSensorFailure",
    "ResilienceCounters",
    "ResilienceMode",
    "ResilienceRates",
    "ResilientIngestor",
    "TeeLatency",
    "rates_from_doc",
]
