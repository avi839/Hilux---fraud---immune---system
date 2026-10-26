"""Deception layer (helix.soc.canaries, Req 33)."""

from __future__ import annotations

import random
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import numpy as np
import pytest

from helix.codec import AuthEvent, TransactionEvent
from helix.codec.auth import AuthEventType
from helix.config.schema import GeneratorConfig, Range
from helix.fusion import Contribution, ContributorType, Mass, fuse, is_valid
from helix.learn.learner import NOT_LEARNED
from helix.soc.canaries import (
    CANARY_CONTRIBUTOR_ID,
    CANARY_ID_FLOOR,
    CANARY_TYPE,
    GENERATOR_MAX_ACCOUNTS,
    ID_FORMATS,
    CanaryConfigError,
    CanaryCounts,
    CanaryKind,
    CanaryRegistry,
    is_canary_contributor,
)

T0 = datetime(2024, 6, 1, tzinfo=timezone.utc)
REG = CanaryRegistry.generate(42)


def tx(tx_id="T1", src="ACC000001", dst="ACC000002", device=None, ts=T0) -> TransactionEvent:
    return TransactionEvent(tx_id=tx_id, ts=ts, src=src, dst=dst, amount=Decimal("10.00"), currency="USD",
                            device_id=device)


def auth(event_id="E1", account="ACC000001", device=None, payee=None, ts=T0) -> AuthEvent:
    et = AuthEventType.NEW_PAYEE_ADDED if payee else AuthEventType.LOGIN_SUCCESS
    return AuthEvent(event_id=event_id, ts=ts, account_id=account, session_id="S1", event_type=et,
                     device_id=device, payee_account_id=payee)


def test_default_generation_is_ten_of_each_kind_in_generator_id_formats():
    assert REG.counts() == {"ACCOUNT": 10, "PAYEE": 10, "DEVICE": 10} and len(REG) == 30
    for k in CanaryKind:
        ids = REG.ids(k)
        assert all(ID_FORMATS[k].match(i) and int(i[3:9]) >= CANARY_ID_FLOOR for i in ids)
        assert all(REG.kind_of(i) is k and REG.is_canary(i) for i in ids)
    assert len(set(REG.ids())) == 30
    assert CanaryRegistry.generate(42).ids() == REG.ids() and CanaryRegistry.generate(43).ids() != REG.ids()


def test_generator_ids_can_never_be_canaries():
    """Req 33.5: generator ids are numbered by account index < generator.accounts upper bound."""
    hi = next(m for m in GeneratorConfig.model_fields["accounts"].metadata if isinstance(m, Range)).hi
    assert hi <= GENERATOR_MAX_ACCOUNTS == CANARY_ID_FLOOR
    reg = CanaryRegistry.generate(7, CanaryCounts(200, 200, 200))
    gen = {f"ACC{a:06d}" for a in range(hi)} | {f"EXT{a:06d}" for a in range(hi)}
    gen |= {f"DEV{a:06d}-{d}" for a in range(hi) for d in range(3)}
    gen |= {f"EXT{n:09d}" for n in range(1, 1000)} | {f"DEV{n:09d}-0" for n in range(1, 1000)}  # attacker
    assert not gen & set(reg.ids())


def test_reserved_ids_are_never_drawn_and_bad_settings_are_rejected():
    ids = set(REG.ids())
    reg = CanaryRegistry.generate(42, reserved=ids)
    assert not ids & set(reg.ids()) and len(reg) == 30
    for bad in (dict(seed=-1), dict(seed=True), dict(seed=2**32)):
        with pytest.raises(CanaryConfigError):
            CanaryRegistry.generate(**bad)
    with pytest.raises(CanaryConfigError):
        CanaryCounts(accounts=-1)
    with pytest.raises(CanaryConfigError):
        CanaryRegistry.generate(1, fraud_mass=0.9)
    with pytest.raises(CanaryConfigError):
        CanaryRegistry(accounts=["CANARY-1"])  # must look like a generator id
    assert len(CanaryRegistry.generate(1, CanaryCounts(0, 0, 0))) == 0


def test_references_cover_account_payee_destination_and_device_fields():
    acc, pay, dev = REG.ids(CanaryKind.ACCOUNT)[0], REG.ids(CanaryKind.PAYEE)[0], REG.ids(CanaryKind.DEVICE)[0]
    assert REG.references(tx()) == () and not REG.touches(auth())
    assert [(r.canary_id, r.field) for r in REG.references(tx(src=acc))] == [(acc, "src")]
    assert [(r.canary_id, r.field) for r in REG.references(tx(dst=pay))] == [(pay, "dst")]
    assert [(r.field, r.kind) for r in REG.references(tx(device=dev, dst=acc))] == [
        ("dst", CanaryKind.ACCOUNT), ("device_id", CanaryKind.DEVICE)]
    assert [r.field for r in REG.references(auth(account=acc, device=dev, payee=pay))] == [
        "account_id", "payee_account_id", "device_id"]


def test_contribution_is_near_certain_fraud_with_fixed_weight_and_not_learned():
    acc = REG.ids(CanaryKind.ACCOUNT)[0]
    assert REG.contribution(tx()) is None
    c = REG.contribution(tx(dst=acc))
    assert isinstance(c, Contribution) and c.contributor_id == CANARY_CONTRIBUTOR_ID
    assert c.mass == Mass(0.99, 0.0, c.mass.t) and is_valid(c.mass) and c.weight == 1.0
    assert str(c.kind) == "CANARY" and str(CANARY_TYPE) in NOT_LEARNED
    assert getattr(ContributorType, "CANARY", CANARY_TYPE) is CANARY_TYPE
    assert is_canary_contributor("canary") and is_canary_contributor("canary:ACCOUNT")
    assert not is_canary_contributor("amount")
    benign = Contribution("amount", ContributorType.SENSOR, Mass(0.0, 0.6, 0.4), 0.8)
    fa, errs = fuse([benign, c], 0.95, tx_id="T1")
    assert errs == [] and fa.belief > 0.95


def test_observe_records_hits_once_with_first_reference_time():
    reg = CanaryRegistry.generate(5)
    acc, dev = reg.ids(CanaryKind.ACCOUNT)[0], reg.ids(CanaryKind.DEVICE)[0]
    assert reg.observe(tx()) == ()
    later, earlier = T0 + timedelta(minutes=5), T0
    assert len(reg.observe(tx("T2", src=acc, device=dev, ts=later))) == 2
    assert reg.observe(tx("T2", src=acc, device=dev, ts=later)) == ()  # same event again
    reg.observe(auth("E9", account=acc, ts=earlier))
    assert reg.hit_count == 3 and reg.first_reference(acc) == earlier and reg.first_reference(dev) == later
    rows = reg.touched()
    assert [(r["canary_id"], r["hits"]) for r in rows] == [(acc, 2), (dev, 1)]
    assert {h.event_type for h in reg.hits()} == {"TRANSACTION", "AUTH"}
    capped = CanaryRegistry.generate(5, max_hits=1)
    capped.observe(tx("T2", src=acc, device=dev))
    assert len(capped.hits()) == 1 and capped.dropped_hits == 1 and capped.hit_count == 2


def test_campaign_report_lists_campaign_canaries_and_first_reference():
    reg = CanaryRegistry.generate(5)
    a1, a2 = reg.ids(CanaryKind.ACCOUNT)[:2]
    reg.observe(tx("X1", src=a1, ts=T0 + timedelta(seconds=30)))
    reg.observe(tx("X2", dst=a2, ts=T0 + timedelta(seconds=10)))
    reg.observe(tx("Y1", dst=a1, ts=T0 + timedelta(seconds=20)))
    reg.observe(tx("Z1", dst=a1, ts=T0))  # no campaign
    rep = reg.campaign_report({"X1": "camp-x", "X2": "camp-x", "Y1": "camp-y"})
    assert [c.campaign_id for c in rep.campaigns] == ["camp-x", "camp-y"]
    x = rep.campaigns[0]
    assert x.first_reference == T0 + timedelta(seconds=10) and x.events == 2
    assert [(c, t) for c, _, t in x.canaries] == [(a2, T0 + timedelta(seconds=10)), (a1, T0 + timedelta(seconds=30))]
    assert rep.unattributed_events == 1 and rep.total_hits == 4
    assert rep.to_dict()["campaigns"][1]["canaries"][0]["canary_id"] == a1


def test_population_mixes_canaries_in_shuffled_without_marking_them():
    base = [f"ACC{a:06d}" for a in range(50)]
    pop = REG.population(base, random.Random(1))
    assert sorted(pop) == sorted(base + list(REG.ids(CanaryKind.ACCOUNT)) + list(REG.ids(CanaryKind.PAYEE)))
    assert all(isinstance(p, str) for p in pop) and pop != base + list(REG.ids(CanaryKind.ACCOUNT))
    assert pop == REG.population(base, random.Random(1))  # deterministic for a seeded rng
    np_pop = REG.population(base, np.random.default_rng(3))
    assert sorted(np_pop) == sorted(pop)
    devs = REG.device_population(["DEV000001-0"], random.Random(2))
    assert sorted(devs) == sorted(["DEV000001-0", *REG.ids(CanaryKind.DEVICE)])
