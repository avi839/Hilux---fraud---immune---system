import { describe, expect, it } from "vitest";
import { caseMsg, caseSummary, feedItem } from "../test/fixtures";
import { activeQueue, announcementFor, FEED_CAP, pushEvents, upsertCase, type FeedItem } from "./reducers";
import { createDashboardStore } from "./store";

describe("pushEvents (live feed ring)", () => {
  it("keeps newest first", () => {
    const feed = pushEvents(pushEvents([], [feedItem(1), feedItem(2)]), [feedItem(3)]);
    expect(feed.map((e) => e.seq)).toEqual([3, 2, 1]);
  });

  it("caps the feed at the 200 most recent events", () => {
    let feed: FeedItem[] = [];
    for (let i = 1; i <= 250; i++) feed = pushEvents(feed, [feedItem(i)]);
    expect(feed).toHaveLength(FEED_CAP);
    expect(feed[0]?.seq).toBe(250);
    expect(feed[FEED_CAP - 1]?.seq).toBe(51);
  });

  it("caps a single oversized batch too", () => {
    const batch = Array.from({ length: 450 }, (_, i) => feedItem(i + 1));
    const feed = pushEvents([feedItem(0)], batch);
    expect(feed).toHaveLength(200);
    expect(feed[0]?.seq).toBe(450);
    expect(feed[199]?.seq).toBe(251);
  });

  it("randomised batches: always the last min(n, 200) events, newest first", () => {
    // Seeded LCG so the run is reproducible.
    let seed = 42;
    const rand = () => ((seed = (seed * 1103515245 + 12345) % 2 ** 31) / 2 ** 31);
    for (let run = 0; run < 50; run++) {
      let feed: FeedItem[] = [];
      let n = 0;
      const batches = 1 + Math.floor(rand() * 20);
      for (let b = 0; b < batches; b++) {
        const size = Math.floor(rand() * 60);
        feed = pushEvents(feed, Array.from({ length: size }, () => feedItem(++n)));
      }
      const expected = Array.from({ length: Math.min(n, 200) }, (_, i) => n - i);
      expect(feed.map((e) => e.seq)).toEqual(expected);
    }
  });
});

describe("activeQueue", () => {
  it("orders by priority descending, then risk, then id", () => {
    let cases = {};
    cases = upsertCase(cases, caseSummary("C-1", { priority: "50.00" }));
    cases = upsertCase(cases, caseSummary("C-2", { priority: "2140.55" }));
    cases = upsertCase(cases, caseSummary("C-3", { priority: "300.10", risk: 0.5 }));
    cases = upsertCase(cases, caseSummary("C-4", { priority: "300.10", risk: 0.9 }));
    expect(activeQueue(cases)).toEqual(["C-2", "C-4", "C-3", "C-1"]);
  });

  it("excludes backlog, closed and merged cases", () => {
    let cases = {};
    cases = upsertCase(cases, caseSummary("C-1"));
    cases = upsertCase(cases, caseSummary("C-2", { placement: "BACKLOG" }));
    cases = upsertCase(cases, caseSummary("C-3", { status: "CLOSED" }));
    cases = upsertCase(cases, caseSummary("C-4", { merged_into: "C-1" }));
    expect(activeQueue(cases)).toEqual(["C-1"]);
  });

  it("ignores an update older than the held one", () => {
    const newer = caseSummary("C-1", { risk: 0.9, updated_at: "2025-03-14T10:30:00+00:00" });
    const older = caseSummary("C-1", { risk: 0.1, updated_at: "2025-03-14T10:00:00+00:00" });
    const cases = upsertCase(upsertCase({}, newer), older);
    expect(cases["C-1"]?.risk).toBe(0.9);
  });
});

describe("announcementFor", () => {
  it("announces new HIGH politely and CRITICAL assertively", () => {
    expect(announcementFor(undefined, caseSummary("C-1", { tier: "HIGH" }), 1)?.politeness).toBe("polite");
    expect(announcementFor(undefined, caseSummary("C-1", { tier: "CRITICAL" }), 1)?.politeness).toBe("assertive");
  });

  it("stays quiet for LOW/MEDIUM and repeat updates", () => {
    expect(announcementFor(undefined, caseSummary("C-1", { tier: "MEDIUM" }), 1)).toBeNull();
    const c = caseSummary("C-1", { tier: "HIGH" });
    expect(announcementFor(c, { ...c, risk: 0.85 }, 2)).toBeNull();
  });

  it("announces an upgrade to CRITICAL", () => {
    const c = caseSummary("C-1", { tier: "HIGH" });
    expect(announcementFor(c, { ...c, tier: "CRITICAL" }, 2)?.text).toMatch(/now CRITICAL/);
  });
});

describe("dashboard store", () => {
  it("applies case messages: queue order, backlog count and announcement", () => {
    const store = createDashboardStore();
    const s = () => store.getState();
    s().applyCaseMessage(caseMsg(caseSummary("C-1", { priority: "10.00", tier: "MEDIUM" }), 1, 3));
    s().applyCaseMessage(caseMsg(caseSummary("C-2", { priority: "90.00", tier: "CRITICAL" }), 2, 4));
    expect(s().queue).toEqual(["C-2", "C-1"]);
    expect(s().backlogCount).toBe(4);
    expect(s().announcement).toMatchObject({ politeness: "assertive" });
    expect(s().announcement?.text).toContain("C-2");

    // C-1 is re-prioritised above C-2.
    s().applyCaseMessage(caseMsg(caseSummary("C-1", { priority: "200.00", tier: "MEDIUM" }), 3, 4));
    expect(s().queue).toEqual(["C-1", "C-2"]);
  });

  it("caps the feed through applyEvents and replaceCases keeps selection", () => {
    const store = createDashboardStore();
    const s = () => store.getState();
    s().applyEvents(Array.from({ length: 205 }, (_, i) => feedItem(i + 1)));
    expect(s().feed).toHaveLength(200);
    expect(s().feed[0]?.seq).toBe(205);
    s().selectCase("C-9");
    s().replaceCases([caseSummary("C-9")], 7);
    expect(s().selectedCaseId).toBe("C-9");
    expect(s().queue).toEqual(["C-9"]);
    expect(s().backlogCount).toBe(7);
  });
});
