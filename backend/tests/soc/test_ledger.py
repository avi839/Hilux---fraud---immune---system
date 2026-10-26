"""Unit tests for the Audit_Ledger (helix.soc.ledger, Req 35, 18.9)."""

from __future__ import annotations

import dataclasses
import hashlib
import json
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from enum import Enum
from types import SimpleNamespace

import duckdb
import pytest

from helix.soc.ledger import (
    EMPTY_HEAD,
    GENESIS_HASH,
    NOTICE,
    AuditCategory,
    AuditLedger,
    DuckDBLedgerBackend,
    HeadRecord,
    LedgerError,
    attach_audit,
    audit_config_change,
    audit_decision,
    audit_escalation,
    audit_rule_change,
    audit_weight_record,
    canonical_json,
    normalize,
    verify_chain,
)

T0 = datetime(2024, 6, 1, 12, 0, tzinfo=timezone.utc)


class Tick:
    """Deterministic clock: T0, T0 + 1 s, ..."""

    def __init__(self) -> None:
        self.n = 0

    def __call__(self) -> datetime:
        self.n += 1
        return T0 + timedelta(seconds=self.n - 1)


def ledger(**kw) -> AuditLedger:
    return AuditLedger(clock=Tick(), **kw)


def test_first_entry_links_to_genesis_and_hash_covers_canonical_form():
    lg = ledger()
    e = lg.append("CONFIG_CHANGE", "admin", {"name": "risk.alert_threshold", "old": 0.7, "new": 0.8})
    assert e.index == 0 and e.prev_hash == GENESIS_HASH == "0" * 64
    assert e.ts == "2024-06-01T12:00:00.000000Z" and e.category == "CONFIG_CHANGE"
    doc = {"index": 0, "ts": e.ts, "category": "CONFIG_CHANGE", "actor": "admin",
           "payload": {"name": "risk.alert_threshold", "old": 0.7, "new": 0.8}, "prev_hash": GENESIS_HASH}
    text = json.dumps(doc, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    assert e.hash == hashlib.sha256(text.encode()).hexdigest()
    e2 = lg.append(AuditCategory.ESCALATION, "inv", {"case_id": "C-1"})
    assert e2.index == 1 and e2.prev_hash == e.hash
    assert lg.head() == HeadRecord(1, e2.hash)
    assert lg.verify().valid and lg.verify().first_broken_index is None


def test_empty_ledger_is_valid_with_empty_head():
    lg = ledger()
    assert lg.head() == EMPTY_HEAD == HeadRecord(-1, GENESIS_HASH)
    res = lg.verify()
    assert res.valid and res.entries == 0 and res.to_dict()["notice"] == NOTICE


def test_every_required_category_is_accepted_case_insensitively():
    lg = ledger()
    for c in AuditCategory:
        lg.append(c.value.lower(), "x", {})
    assert [e.category for e in lg.entries()] == [c.value for c in AuditCategory]
    assert {c.value for c in AuditCategory} == {
        "CONFIG_CHANGE", "FEEDBACK_LABEL", "SENSOR_WEIGHT_CHANGE", "RULE_CHANGE", "PLAYBOOK_ACTION",
        "ESCALATION", "WEIGHT_ROLLBACK"}


@pytest.mark.parametrize("category,actor,payload", [
    ("NOT_A_CATEGORY", "x", {}),
    ("bad category", "x", {}),
    ("CONFIG_CHANGE", "", {}),
    ("CONFIG_CHANGE", "x" * 129, {}),
    ("CONFIG_CHANGE", "x", ["not", "a", "mapping"]),
    ("CONFIG_CHANGE", "x", {"v": float("nan")}),
    ("CONFIG_CHANGE", "x", {"v": float("inf")}),
    ("CONFIG_CHANGE", "x", {"v": object()}),
    ("CONFIG_CHANGE", "x", {1: "a", "1": "b"}),
    ("CONFIG_CHANGE", "x", {"v": datetime(2024, 1, 1, tzinfo=timezone.utc), (1, 2): 3}),
])
def test_invalid_appends_are_rejected_and_change_nothing(category, actor, payload):
    lg = ledger()
    lg.append("CONFIG_CHANGE", "x", {"ok": True})
    head = lg.head()
    with pytest.raises(LedgerError):
        lg.append(category, actor, payload)
    assert len(lg) == 1 and lg.head() == head and lg.verify().valid


def test_extra_categories_can_be_registered():
    lg = ledger(categories=["indicator_import"])
    assert lg.append("INDICATOR_IMPORT", "api", {"n": 3}).category == "INDICATOR_IMPORT"


class Colour(str, Enum):
    RED = "RED"


@dataclasses.dataclass
class Rec:
    a: int
    b: Decimal


def test_normalize_handles_helix_value_types():
    v = normalize({"d": Decimal("1.50"), "t": T0, "e": Colour.RED, "dc": Rec(1, Decimal("2")),
                   "tup": (1, 2), "s": {"b", "a"}, "td": timedelta(minutes=1), Colour.RED: 1, 7: "k"})
    assert v == {"d": "1.50", "t": "2024-06-01T12:00:00.000000Z", "e": "RED", "dc": {"a": 1, "b": "2"},
                 "tup": [1, 2], "s": ["a", "b"], "td": 60.0, "RED": 1, "7": "k"}
    assert canonical_json({"b": 1, "a": "ü"}) == '{"a":"ü","b":1}'


def test_entries_are_immutable_and_payload_is_a_copy():
    lg = ledger()
    e = lg.append("RULE_CHANGE", "inv", {"rule_id": "R-1", "list": [1]})
    with pytest.raises(dataclasses.FrozenInstanceError):
        e.actor = "mallory"  # type: ignore[misc]
    e.payload["list"].append(2)
    assert lg.entries()[0].payload == {"rule_id": "R-1", "list": [1]}
    assert not hasattr(lg, "update") and not hasattr(lg, "delete")


def test_verify_reports_content_edit_at_its_index():
    lg = ledger()
    for i in range(5):
        lg.append("PLAYBOOK_ACTION", "playbook_runner", {"n": i})
    es = list(lg.entries())
    es[2] = dataclasses.replace(es[2], payload_json='{"n":99}')
    res = verify_chain(es, lg.head())
    assert not res.valid and res.first_broken_index == 2 and "content" in res.reason


def test_verify_rehashed_edit_and_deletions_and_head():
    lg = ledger()
    for i in range(5):
        lg.append("PLAYBOOK_ACTION", "p", {"n": i})
    es = list(lg.entries())
    e = dataclasses.replace(es[2], actor="mallory")
    es2 = es[:2] + [dataclasses.replace(e, hash=e.recompute_hash())] + es[3:]
    assert verify_chain(es2, lg.head()).first_broken_index == 2  # link 2 -> 3 broken, reported at 2
    assert verify_chain(es[:2] + es[3:], lg.head()).first_broken_index == 2  # deletion
    assert verify_chain(es[:4], lg.head()).first_broken_index == 0  # truncation: not localisable
    assert verify_chain(es, HeadRecord(2, es[2].hash)).first_broken_index == 3  # unanchored tail
    assert verify_chain(es, None).first_broken_index == 0
    swapped = [es[0], es[3], es[2], es[1], es[4]]
    assert verify_chain(swapped, lg.head()).first_broken_index == 1


def test_duckdb_backend_persists_reloads_and_detects_sql_tampering():
    con = duckdb.connect(":memory:")
    lg = ledger(backend=DuckDBLedgerBackend(con))
    for i in range(4):
        lg.append("FEEDBACK_LABEL", "inv", {"case_id": f"C-{i}", "label": "CONFIRMED_FRAUD"})
    assert con.execute("SELECT COUNT(*) FROM audit_ledger").fetchone() == (4,)
    assert con.execute("SELECT idx, hash FROM audit_head").fetchone() == (3, lg.head().hash)
    again = AuditLedger(clock=Tick(), storage=con)  # reload continues the chain
    assert again.entries() == lg.entries() and again.head() == lg.head()
    nxt = again.append("ESCALATION", "inv", {"case_id": "C-9"})
    assert nxt.index == 4 and nxt.prev_hash == lg.head().hash and again.verify().valid
    con.execute("UPDATE audit_ledger SET payload = '{\"case_id\":\"C-1\",\"label\":\"FALSE_POSITIVE\"}' WHERE idx = 1")
    res = again.verify()  # re-reads the stored rows
    assert not res.valid and res.first_broken_index == 1
    with pytest.raises(duckdb.Error):  # the primary key refuses an overwrite through INSERT
        again.backend.write(again.entries()[0], again.head())


def test_backend_failure_leaves_ledger_unchanged():
    class Broken:
        def load(self):
            return [], None

        def write(self, entry, head):
            raise OSError("disk full")

    lg = ledger(backend=Broken())
    with pytest.raises(OSError):
        lg.append("CONFIG_CHANGE", "x", {})
    assert len(lg) == 0 and lg.head() == EMPTY_HEAD
    assert audit_config_change(lg, "x", "a", 1, 2) is None and lg.append_errors == 1
    with pytest.raises(OSError):
        audit_config_change(lg, "x", "a", 1, 2, strict=True)


def test_ledger_on_helix_storage(tmp_path):
    from helix.storage import Storage

    p = tmp_path / "h.duckdb"
    st = Storage(p).open()
    lg = AuditLedger(clock=Tick(), storage=st)
    lg.append("CONFIG_CHANGE", "admin", {"name": "x", "old": 1, "new": 2})
    st.close()
    st = Storage(p).open()  # the extra tables do not trip the Storage schema check
    try:
        again = AuditLedger(storage=st)
        assert again.head() == lg.head() and again.entries() == lg.entries() and again.verify().valid
    finally:
        st.close()


def test_page_filters_by_category():
    lg = ledger()
    for i in range(6):
        lg.append("ESCALATION" if i % 2 else "CONFIG_CHANGE", "x", {"i": i})
    total, rows = lg.page(1, 2, "escalation")
    assert total == 3 and [r.payload["i"] for r in rows] == [3, 5]
    with pytest.raises(ValueError):
        lg.page(0, 0)


def test_helpers_record_each_category():
    lg = ledger()
    case = SimpleNamespace(case_id="C-3", assignee="inv-1", label_note="mule", tier="CRITICAL")
    rec = SimpleNamespace(contributor_id="amount", kind="SENSOR", old=1.0, new=0.95, ts=T0, case_id="C-3",
                          rollback_to=None)
    rb = SimpleNamespace(**{**vars(rec), "rollback_to": T0})
    audit_config_change(lg, "admin", "prioritiser.alert_budget", 10, 20)
    audit_escalation(lg, case, "inv-1")
    audit_weight_record(lg, rec)
    audit_weight_record(lg, rb)
    audit_rule_change(lg, "inv-1", "R-1", "activated", 2)
    audit_decision(lg, "PLAYBOOK_ACTION", "playbook_runner", {"event": "performed"})
    assert audit_decision(None, "CONFIG_CHANGE", "x") is None
    cats = [e.category for e in lg.entries()]
    assert cats == ["CONFIG_CHANGE", "ESCALATION", "SENSOR_WEIGHT_CHANGE", "WEIGHT_ROLLBACK", "RULE_CHANGE",
                    "PLAYBOOK_ACTION"]
    assert lg.entries()[3].payload["rollback_to"] == "2024-06-01T12:00:00.000000Z"
    assert lg.verify().valid


def test_attach_audit_records_feedback_labels_once():
    helix = SimpleNamespace(feedback_hooks=[])
    lg = ledger()
    attach_audit(helix, lg)
    attach_audit(helix, lg)  # idempotent
    assert len(helix.feedback_hooks) == 1 and helix.ledger is lg
    from helix.cases import Label

    helix.feedback_hooks[0](SimpleNamespace(case_id="C-1", assignee="inv", label_note="n"), Label.CONFIRMED_FRAUD)
    [e] = lg.entries()
    assert (e.category, e.actor, e.payload) == ("FEEDBACK_LABEL", "inv",
                                                {"case_id": "C-1", "label": "CONFIRMED_FRAUD", "note": "n"})


def test_ledger_satisfies_the_playbook_audit_sink_signature():
    lg = ledger()
    e = lg.append(category="PLAYBOOK_ACTION", actor="playbook_runner",
                  payload={"event": "scheduled", "targets": ["A"], "simulated": True})
    assert e.payload["targets"] == ["A"] and lg.verify().valid
