"""Shared harness for the Immune_System end-to-end loop (E2E test and Property 69).

``make_stream`` builds deterministic background traffic from the Synthetic_Generator merged
with one Attacker_Agent campaign (a Pattern_Genome generated with a fixed seed). ``run_loop``
streams it through a real ``Helix`` pipeline with the Immune_System attached. A
``MissDetector`` (fed by an in-memory ground-truth channel, as the API does) checks every
attack event once its detection deadline has passed; "detected" here means *placed in a
Case* (Req 21.14 counts events in Cases within the deadline).

Determinism: the generator, the campaign and the Immune_System are seeded, and sensor timing
uses a frozen clock so no sensor can time out. Two runs over the same stream differ only in
the Immune_System state they start from.

Size: the defaults (400 accounts, every 20th generator event, 2 h warm-up) keep one run to
roughly 1-3 s. Denser background traffic makes Cases grow large and the pipeline's
Evidence_Chain rebuild per event dominates the run time.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any, Optional

import numpy as np

from helix.attacker import CampaignContext, PatternGenome, TargetPool, generate
from helix.config import HelixConfig
from helix.config.schema import GeneratorConfig, ImmuneConfig
from helix.core import Helix
from helix.evaluation import InMemoryGroundTruthReader
from helix.immune import ImmuneSystem, attach_immune
from helix.immune.miss_detector import MissDetector
from helix.sim import InMemoryGroundTruth, ManualClock, SyntheticGenerator
from helix.sim.ground_truth import GroundTruthRecord, GTSource

T0 = datetime(2024, 3, 4, 0, 0, tzinfo=timezone.utc)


def FROZEN() -> float:  # noqa: N802 - sensor clock: no sensor ever exceeds its budget
    return 0.0


@dataclass
class Stream:
    background: list[Any]
    attack: list[Any]
    campaign_id: str

    @property
    def merged(self) -> list[Any]:
        # Stable merge by ts: background first among equal timestamps (deterministic).
        tagged = [(e.ts, 0, i, e) for i, e in enumerate(self.background)]
        tagged += [(e.ts, 1, i, e) for i, e in enumerate(self.attack)]
        return [e for *_, e in sorted(tagged, key=lambda t: t[:3])]


def make_stream(genome: PatternGenome, seed: int, *, accounts: int = 400, every: int = 20,
                warmup_s: float = 2 * 3_600, tail_s: float = 1_800, max_events: int = 60) -> Stream:
    """Background traffic (1 event/s from the generator, every ``every``-th kept) for
    ``warmup_s`` + campaign duration + ``tail_s``; the campaign starts after the warm-up.
    The same (genome, seed) always gives the same stream."""
    gen = SyntheticGenerator(GeneratorConfig(accounts=accounts, rate_eps=1, change_fraction=0.0),
                             ManualClock(T0), InMemoryGroundTruth(), seed=1)
    pool = [p.account_id for p in gen.profiles]
    ctx = CampaignContext(rng=np.random.default_rng(seed), start=T0 + timedelta(seconds=warmup_s),
                          targets=TargetPool(pool), id_prefix=f"replay:{genome.genome_id}:{seed}",
                          reporting_threshold=Decimal("10000"), max_events=max_events)
    attack = [e for e in generate(genome, ctx) if hasattr(e, "tx_id")]
    end = max(e.ts for e in attack) + timedelta(seconds=tail_s)
    background = list(gen.events_until(end))[::every]
    return Stream(background, attack, f"C-{genome.genome_id}-{seed}")


@dataclass
class LoopResult:
    helix: Helix
    immune: ImmuneSystem
    detector: MissDetector
    attack_ids: list[str]
    detected: set[str] = field(default_factory=set)  # attack events in a Case by their deadline

    @property
    def missed(self) -> list[str]:
        return [m.event_id for m in self.detector.misses]


def immune_cfg(**kw: Any) -> ImmuneConfig:
    base: dict[str, Any] = dict(enabled=True, self_set_min=150, detection_deadline_s=600)
    base.update(kw)
    return ImmuneConfig(**base)


def run_loop(stream: Stream, immune: Optional[ImmuneSystem] = None, *, cfg: Optional[ImmuneConfig] = None,
             learn_from_misses: bool = True, seed: int = 0) -> LoopResult:
    icfg = cfg if cfg is not None else (immune.cfg if immune is not None else immune_cfg())
    hcfg = HelixConfig(immune=icfg)
    clock = ManualClock(T0)
    helix = Helix(hcfg, clock, sensor_clock=FROZEN)
    if immune is None:
        immune = ImmuneSystem(icfg, clock, seed=seed)
    else:
        immune.clock = clock
    attach_immune(helix, immune)

    sink = InMemoryGroundTruth()
    for ev in stream.attack:  # label first, as the Attacker_Agent does (Req 20.10)
        sink.write(GroundTruthRecord(event_id=ev.tx_id, label="FRAUD", source=GTSource.ATTACKER,
                                     written_at=T0, campaign_id=stream.campaign_id, genome_id=None,
                                     parent_genome_id=None))

    def in_case(tx: str) -> bool:
        return helix.cases.case_of(tx) is not None

    det = MissDetector(InMemoryGroundTruthReader(sink), in_case, immune.on_miss if learn_from_misses else None)
    deadline = timedelta(seconds=float(icfg.detection_deadline_s))

    def check(at: datetime) -> None:
        det.record_misses(at, now=clock.now())
        immune.tick()  # negative selection of the batch's candidates

    for ev in stream.merged:
        helix.process(ev)
        det.observe(ev.tx_id, ev.ts)
        check(clock.now() - deadline)
    check(clock.now())  # end of stream: nothing more can be detected
    return LoopResult(helix, immune, det, [e.tx_id for e in stream.attack], set(det.settled))
