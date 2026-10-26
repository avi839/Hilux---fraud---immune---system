import { describe, expect, it } from "vitest";
import type { MetricsMessage } from "../../api/types";
import { caseSummary, feedItem } from "../../test/fixtures";
import {
  antibodyEntries,
  mergeEntries,
  playbookEntries,
  transitionEntries,
  type LogEntry,
  type LogSource,
} from "./logModel";

const empty: LogSource = { feed: [], cases: {}, metrics: null };
const entry = (id: string, ts: string): LogEntry => ({ id, ts, kind: "ingested", text: id });
const metrics = (seq: number, malformed: number, duplicate: number): MetricsMessage => ({
  channel: "metrics",
  seq,
  ts: `2025-03-14T10:00:0${seq}+00:00`,
  ingest: { accepted: 10, malformed, duplicate },
});

describe("mergeEntries", () => {
  it("orders by timestamp, keeps arrival order on ties, drops repeats and keeps the newest cap", () => {
    const log = [entry("b", "2025-01-01T00:00:02Z"), entry("d", "2025-01-01T00:00:04Z")];
    const out = mergeEntries(log, [entry("a", "2025-01-01T00:00:01Z"), entry("c", "2025-01-01T00:00:02Z"), entry("b", "2025-01-01T00:00:02Z")]);
    expect(out.map((e) => e.id)).toEqual(["a", "b", "c", "d"]);
    expect(mergeEntries(out, [entry("e", "2025-01-01T00:00:05Z")], 3).map((e) => e.id)).toEqual(["c", "d", "e"]);
  });
});

describe("transitionEntries", () => {
  it("logs newly ingested events only", () => {
    const prev = { ...empty, feed: [feedItem(1)] };
    const next = { ...empty, feed: [feedItem(2), feedItem(1)] };
    const out = transitionEntries(prev, next);
    expect(out).toHaveLength(1);
    expect(out[0]).toMatchObject({ kind: "ingested", ts: feedItem(2).ts });
    expect(out[0]!.text).toContain("T-2: A-1 → A-2 949.00 GBP, risk 0.50");
  });

  it("logs a Case opened, updated and labelled, and ignores an unchanged refresh", () => {
    const c1 = caseSummary("C-1");
    const opened = transitionEntries(empty, { ...empty, cases: { "C-1": c1 } });
    expect(opened.map((e) => e.kind)).toEqual(["case-opened"]);
    expect(opened[0]!.text).toBe("C-1: HIGH, risk 0.81, 3 accounts, 4 transactions");

    const c2 = { ...c1, tier: "CRITICAL" as const, updated_at: "2025-03-14T10:23:00+00:00" };
    const upd = transitionEntries({ ...empty, cases: { "C-1": c1 } }, { ...empty, cases: { "C-1": c2 } });
    expect(upd[0]).toMatchObject({ kind: "case-updated", ts: c2.updated_at });
    expect(upd[0]!.text).toContain("tier HIGH → CRITICAL");

    const c3 = { ...c2, label: "CONFIRMED_FRAUD" as const, updated_at: "2025-03-14T10:24:00+00:00" };
    expect(transitionEntries({ ...empty, cases: { "C-1": c2 } }, { ...empty, cases: { "C-1": c3 } })).toEqual([
      { id: `label:C-1@${c3.updated_at}`, ts: c3.updated_at, kind: "label", text: "CONFIRMED_FRAUD recorded on C-1" },
    ]);

    expect(transitionEntries({ ...empty, cases: { "C-1": c1 } }, { ...empty, cases: { "C-1": { ...c1 } } })).toEqual([]);
  });

  it("turns increases of the ingest counters into rejected and duplicate entries", () => {
    expect(transitionEntries(empty, { ...empty, metrics: metrics(1, 3, 1) })).toEqual([]); // first frame = baseline
    const out = transitionEntries({ ...empty, metrics: metrics(1, 3, 1) }, { ...empty, metrics: metrics(2, 5, 2) });
    expect(out.map((e) => [e.kind, e.text])).toEqual([
      ["rejected", "2 events rejected as invalid"],
      ["duplicate", "1 duplicate event dropped"],
    ]);
  });
});

describe("REST sources", () => {
  it("logs Antibodies created and retired", () => {
    const out = antibodyEntries({
      recent_antibodies: [
        { ab_id: "AB-1", created_at: "2025-03-14T10:12:00+00:00", origin: "miss:T-9", retired_at: null },
        { ab_id: "AB-2", created_at: "2025-03-14T10:13:00+00:00", retired_at: "2025-03-14T10:20:00+00:00", retired_reason: "false positives" },
      ],
    });
    expect(out.map((e) => [e.kind, e.text])).toEqual([
      ["antibody-created", "AB-1 created from miss:T-9"],
      ["antibody-created", "AB-2 created"],
      ["antibody-retired", "AB-2 retired: false positives"],
    ]);
    expect(antibodyEntries(null)).toEqual([]);
  });

  it("logs executed Playbook_Actions only", () => {
    const out = playbookEntries({
      actions: [
        { action_id: "PA-1", case_id: "C-1", type: "FREEZE_ACCOUNT", status: "PERFORMED", targets: ["A-1"], completed_at: "2025-03-14T10:30:00+00:00" },
        { action_id: "PA-2", case_id: "C-1", type: "NOTIFY", status: "PENDING", targets: [], scheduled_at: "2025-03-14T10:31:00+00:00" },
        { action_id: "PA-3", case_id: "C-2", type: "NOTIFY", status: "DRY_RUN", targets: [], scheduled_at: "2025-03-14T10:32:00+00:00" },
      ],
    });
    expect(out.map((e) => [e.id, e.text])).toEqual([
      ["pb:PA-1", "FREEZE_ACCOUNT on A-1 for C-1"],
      ["pb:PA-3", "NOTIFY for C-2 (dry run)"],
    ]);
  });
});
