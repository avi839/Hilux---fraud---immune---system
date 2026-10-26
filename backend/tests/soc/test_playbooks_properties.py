"""Hypothesis properties of the Playbook_Runner: idempotence (Req 32.8) and dry-run (Req 32.6)."""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from helix.config import TIER_ORDER
from helix.soc.playbooks import ActionStatus, Playbook, PlaybookActionType, PlaybookRunner
from helix.soc.techniques import default_catalogue

pytestmark = pytest.mark.property

T0 = datetime(2024, 6, 1, tzinfo=timezone.utc)
TIERS = [t.value for t in TIER_ORDER]
TECHS = sorted(default_catalogue().ids)
ACTIONS = [t.value for t in PlaybookActionType]
SETTINGS = settings(max_examples=60, deadline=None, derandomize=True, database=None)


class Ledger:
    def __init__(self) -> None:
        self.payloads: list[dict] = []

    def append(self, category: str, actor: str, payload: Any) -> None:
        self.payloads.append(dict(payload))


@dataclass
class Case:
    case_id: str
    tier: str
    accounts: set = field(default_factory=set)
    tx_ids: set = field(default_factory=set)


@st.composite
def playbook_docs(draw: Any, pid: str) -> dict:
    trig: dict[str, Any] = {}
    tiers = draw(st.lists(st.sampled_from(TIERS), max_size=2, unique=True))
    techs = draw(st.lists(st.sampled_from(TECHS[:6]), max_size=2, unique=True))
    if tiers:
        trig["tiers"] = tiers
    if techs:
        trig["techniques"] = techs
    if draw(st.booleans()) or not trig:
        trig["min_tier"] = draw(st.sampled_from(TIERS))
    trig["match"] = draw(st.sampled_from(["any", "all"]))
    acts = [{"type": t} for t in draw(st.lists(st.sampled_from(ACTIONS), min_size=1, max_size=4))]
    return {"id": pid, "version": draw(st.integers(1, 3)), "title": pid, "trigger": trig, "actions": acts}


# One Case update: (case index, tier, technique tags, new account, new tx id)
updates = st.lists(
    st.tuples(st.integers(0, 3), st.sampled_from(TIERS), st.sets(st.sampled_from(TECHS[:6]), max_size=3),
              st.integers(0, 5), st.integers(0, 20)),
    min_size=1, max_size=25,
)


def apply_updates(r: PlaybookRunner, ups, decide: list[bool] | None = None) -> None:
    cases: dict[int, Case] = {}
    for n, (ci, tier, techs, acc, tx) in enumerate(ups):
        c = cases.setdefault(ci, Case(f"C-{ci}", tier))
        c.tier = tier
        c.accounts.add(f"ACC{acc:06d}")
        c.tx_ids.add(f"TX{tx:04d}")
        r.on_case_update(c, techniques=techs)
        if decide is not None:  # approve / reject whatever is pending, alternating
            for k, a in enumerate(r.actions(status=ActionStatus.PENDING)):
                (r.approve if (n + k) % 2 == 0 else r.reject)(a.action_id, by="inv")


@SETTINGS
@given(docs=st.tuples(playbook_docs("PB-A"), playbook_docs("PB-B"), playbook_docs("PB-C")), ups=updates,
       decide=st.booleans())
def test_each_playbook_version_is_performed_at_most_once_per_case(docs, ups, decide):
    """Idempotence: whatever the update sequence, each (Case, playbook, version) is scheduled at
    most once, with exactly its defined steps in order, and performed at most once.

    **Validates: Requirements 32.8, 32.2**
    """
    pbs = [Playbook.from_dict(d) for d in docs]
    led = Ledger()
    r = PlaybookRunner(pbs, clock=lambda: T0, audit=led)
    apply_updates(r, ups, [True] if decide else None)
    apply_updates(r, ups)  # replaying every update again schedules nothing new
    acts = r.actions()
    groups = Counter((a.case_id, a.playbook_id, a.playbook_version) for a in acts)
    by_key = {p.key: p for p in pbs}
    for cid, pid, ver in groups:
        steps = [a for a in acts if (a.case_id, a.playbook_id, a.playbook_version) == (cid, pid, ver)]
        assert [a.step for a in steps] == list(range(len(by_key[(pid, ver)].actions)))
        assert [a.type for a in steps] == [s.type for s in by_key[(pid, ver)].actions]
    performed = Counter((p["case_id"], p["playbook_id"], p["playbook_version"], p["step"])
                        for p in led.payloads if p["event"] == "performed")
    assert all(n == 1 for n in performed.values())
    assert len(r.runs()) == len(groups) == len(set(r.runs()))
    replay = PlaybookRunner(pbs, clock=lambda: T0)
    apply_updates(replay, ups + ups)
    assert len(replay.actions()) == len(acts)  # decisions never change what gets scheduled


@SETTINGS
@given(docs=st.tuples(playbook_docs("PB-A"), playbook_docs("PB-B")), ups=updates,
       pre=st.lists(st.sampled_from(TIERS), max_size=3))
def test_dry_run_never_changes_the_simulated_state(docs, ups, pre):
    """With dry-run on, scheduling and approving actions leaves the simulated state unchanged
    and every new action ends as DRY_RUN.

    **Validates: Requirements 32.6**
    """
    r = PlaybookRunner([Playbook.from_dict(d) for d in docs], clock=lambda: T0)
    for i, tier in enumerate(pre):  # some real effects before dry-run is switched on
        r.on_case_update(Case(f"P-{i}", tier, {f"ACC9{i:05d}"}, {f"TX9{i:03d}"}))
    pending = [a.action_id for a in r.actions(status=ActionStatus.PENDING)]
    before = r.state.snapshot()
    n_before = len(r.actions())
    r.set_dry_run(True, by="lead")
    for aid in pending:  # approved under dry-run: recorded, not performed
        assert r.approve(aid, by="inv").status is ActionStatus.DRY_RUN
    apply_updates(r, ups, decide=[True])
    assert r.state.snapshot() == before
    assert all(a.status is ActionStatus.DRY_RUN for a in r.actions()[n_before:])
