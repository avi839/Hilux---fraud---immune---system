"""Unit tests for the API WebSocket hub, page tokens and runtime-config checks (task 11.1)."""

from __future__ import annotations

import asyncio

import pytest

from helix.api.app import check_runtime_patch
from helix.api.hub import WSHub
from helix.api.serialize import decode_cursor, encode_cursor, jsonable
from helix.config import HelixConfig


def test_publish_without_clients_is_a_noop():
    hub = WSHub(4)
    hub.publish("events", {"seq": 1})
    assert hub.published == 0 and hub.subscribers("events") == 0


def test_slow_client_drops_oldest_and_publisher_never_blocks():
    async def run():
        hub = WSHub(3)
        hub.bind()
        c = hub.register("events")
        other = hub.register("cases")
        for i in range(1, 8):
            hub.publish("events", {"seq": i})
        got = [c.queue.get_nowait()["seq"] for _ in range(c.queue.qsize())]
        return got, c.dropped, other.queue.qsize(), hub.stats()

    got, dropped, other_size, stats = asyncio.run(run())
    assert got == [5, 6, 7]  # newest kept, oldest dropped
    assert dropped == 4 and other_size == 0
    assert stats["dropped"] == 4 and stats["clients"] == {"cases": 1, "events": 1}


def test_unregister_stops_delivery():
    async def run():
        hub = WSHub(2)
        c = hub.register("metrics")
        hub.unregister(c)
        hub.publish("metrics", {"seq": 1})
        return c.queue.qsize(), hub.subscribers("metrics")

    assert asyncio.run(run()) == (0, 0)


def test_queue_size_must_be_positive():
    with pytest.raises(ValueError):
        WSHub(0)


@pytest.mark.parametrize("n", [0, 1, 17, 10**9])
def test_cursor_round_trip(n):
    assert decode_cursor(encode_cursor(n)) == n


@pytest.mark.parametrize("token", ["", "!!", "eyJhZnRlciI6LTF9", "eyJhZnRlciI6dHJ1ZX0", "WzFd", "bnVsbA"])
def test_foreign_cursors_are_rejected(token):
    assert decode_cursor(token) is None


def test_jsonable_converts_helix_values():
    from datetime import datetime, timezone
    from decimal import Decimal

    from helix.cases import CaseStatus

    out = jsonable({"a": Decimal("1.50"), "s": CaseStatus.OPEN, "t": datetime(2024, 1, 1, tzinfo=timezone.utc),
                    "f": float("nan"), "set": {"b", "a"}})
    assert out == {"a": "1.50", "s": "OPEN", "t": "2024-01-01T00:00:00+00:00", "f": None, "set": ["a", "b"]}


def test_runtime_patch_checks():
    cfg = HelixConfig()
    upd, errs = check_runtime_patch(cfg, {"prioritiser": {"alert_budget": 30.0, "investigation_cost": 0}})
    assert errs == [] and upd == {"prioritiser.alert_budget": 30, "prioritiser.investigation_cost": 0}
    _, errs = check_runtime_patch(cfg, {"prioritiser.investigation_cost": -1, "api.port": 1})
    assert {e["field"]: e["rule"] for e in errs} == {
        "prioritiser.investigation_cost": "out_of_range", "api.port": "not_runtime"}
    assert errs[1]["permitted"]["rule"] == "must be in [0, 1000000000]"
