import { act, render, screen, waitFor, within } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { axe } from "vitest-axe";
import { dashboard } from "../../store/store";
import { jsonResponse, routeFetch } from "../../test/fixtures";
import { sampleMetrics } from "../../test/sampleRun";
import { simAnalystOf } from "../../api/live";
import { LoopPanel, SIM_ANALYST_HELP, WEIGHTS_AT_START_TEXT } from "./LoopPanel";
import { attackerRunning, normaliseMetrics, NOT_APPLICABLE, poolSize, pushSample, stageRows, throughput, type Sample } from "./loop";

const AXE_OPTIONS = { rules: { "color-contrast": { enabled: false } } };

const cell = (stage: string, col: number) => within(screen.getByTestId(`stage-${stage}`)).getAllByRole("cell")[col]!;

// Latencies in the captured run are wall-clock measurements, so expectations are derived
// from the fixture rather than hard-coded.
const fixtureStages = sampleMetrics.stages as Record<string, { mean_ms: number }>;
const fixtureP95 = (sampleMetrics.latency_breach as { p95_ms: number }).p95_ms;

describe("LoopPanel", () => {
  it("shows n/a for everything the server does not expose (sample-run /metrics, no /immune, no /attacker)", async () => {
    const fetchFn = routeFetch({ "/metrics": sampleMetrics }); // /immune and /attacker → 404
    render(<LoopPanel />);
    await waitFor(() =>
      expect(screen.getByTestId("loop-breach")).toHaveTextContent(`Within latency target: p95 ${fixtureP95.toFixed(2)} ms ≤ 50 ms`),
    );

    const paths = fetchFn.mock.calls.map(([u]) => String(u));
    expect(paths).toEqual(expect.arrayContaining(["/api/v1/metrics", "/api/v1/immune", "/api/v1/attacker"]));

    // All six loop stages plus end to end.
    const stages = within(screen.getByTestId("loop-stages")).getAllByRole("rowheader").map((h) => h.firstChild?.textContent);
    expect(stages).toEqual(["SENSE", "PERCEIVE", "REMEMBER", "REASON", "ACT", "LEARN", "End to end"]);
    expect(cell("SENSE", 0)).toHaveTextContent("0.0"); // ingest.rate_eps
    expect(cell("SENSE", 1)).toHaveTextContent("n/a");
    expect(cell("PERCEIVE", 1)).toHaveTextContent(fixtureStages.PERCEIVE!.mean_ms.toFixed(3));
    expect(cell("END_TO_END", 1)).toHaveTextContent(fixtureStages.END_TO_END!.mean_ms.toFixed(3));
    expect(cell("PERCEIVE", 0)).toHaveTextContent("n/a"); // one sample so far: no rate yet
    expect(cell("LEARN", 0)).toHaveTextContent("n/a");
    expect(cell("LEARN", 1)).toHaveTextContent("n/a");

    expect(screen.getByTestId("loop-weights")).toHaveTextContent("n/a (current weights are not exposed by GET /metrics)");
    expect(screen.getByTestId("loop-pool")).toHaveTextContent("n/a (no /immune endpoint)");
    expect(screen.getByTestId("loop-attacker")).toHaveTextContent("n/a (no /attacker endpoint)");
    expect(screen.getByTestId("loop-eval")).toHaveTextContent("Not applicable: no Evaluation_Monitor");
  });

  it("shows weights, pool size, attacker state, evaluation and a latency breach when reported", async () => {
    routeFetch({
      "/metrics": {
        ...sampleMetrics,
        weights: { amount: 0.9, geo: 1 },
        latency_breach: { active: true, count: 2, threshold_ms: 50, p95_ms: 72.5 },
        evaluation: { precision: 0.5, recall: NOT_APPLICABLE, alerts_per_hour: 2.25, latency_median_s: 30, latency_mean_s: 45.5 },
      },
      "/immune": { pool_size: 12, max_pool: 200 },
      "/attacker": { running: true },
    });
    render(<LoopPanel />);
    await waitFor(() => expect(screen.getByTestId("loop-pool")).toHaveTextContent("12 / 200"));
    expect(screen.getByTestId("loop-breach")).toHaveTextContent("Latency breach: p95 72.50 ms > 50 ms target · 2 breaches so far");
    expect(screen.getByTestId("loop-weights")).toHaveTextContent("amount 0.90geo 1.00");
    expect(screen.getByTestId("loop-attacker")).toHaveTextContent("running");
    const ev = screen.getByTestId("loop-eval");
    expect(ev).toHaveTextContent("Precision0.500");
    expect(ev).toHaveTextContent("Recallnot applicable");
    expect(ev).toHaveTextContent("Alerts per hour2.25");
    expect(ev).toHaveTextContent("Detection latencymedian 30.0 s, mean 45.5 s");
  });

  it("shows the investigator workload numbers and the dataset status block", async () => {
    routeFetch({
      "/metrics": {
        ...sampleMetrics,
        workload: {
          labelled_last_hour: 7,
          labelled_last_hour_by: { bob: 2, alice: 5 },
          labelled_total: 31,
          auto_cleared_total: 12,
          auto_cleared_last_hour: 3,
          bulk_requests_total: 4,
        },
      },
      "/dataset": { status: "DISABLED" },
    });
    const { container } = render(<LoopPanel />);
    await waitFor(() => expect(screen.getByTestId("loop-workload")).toHaveTextContent("Labels, last 60 min7 (alice 5, bob 2)"));
    const wl = screen.getByTestId("loop-workload");
    expect(wl).toHaveTextContent("Labels, total31");
    expect(wl).toHaveTextContent("Auto-cleared (not labels)12 total · 3 in the last 60 min");
    expect(wl).toHaveTextContent("Bulk label requests4");
    expect(await screen.findByTestId("dataset-disabled")).toHaveTextContent(
      "Dataset stream off (synthetic demo). Start with -Dataset amlworld to stream IBM AMLworld.",
    );
    // Compact: one line, no "Dataset stream" heading block.
    expect(within(screen.getByTestId("dataset")).queryByRole("heading")).toBeNull();
    const results = await axe(container, AXE_OPTIONS);
    expect(results.violations.map((v) => `${v.id}: ${v.help}`)).toEqual([]);
  });

  it("explains weights at the 1.00 ceiling, an empty evaluation and zero labels", async () => {
    routeFetch({
      "/metrics": {
        ...sampleMetrics,
        weights: { amount: 1, geo: 1 },
        evaluation: { precision: 0, recall: NOT_APPLICABLE, alerts_per_hour: 0, latency_median_s: null, latency_mean_s: null },
        workload: { labelled_last_hour: 0, labelled_total: 0, auto_cleared_total: 0, auto_cleared_last_hour: 0, bulk_requests_total: 0 },
      },
    });
    render(<LoopPanel />);
    expect(await screen.findByTestId("loop-weights-note")).toHaveTextContent(WEIGHTS_AT_START_TEXT);
    expect(screen.getByTestId("loop-weights")).toHaveTextContent("amount 1.00geo 1.00");
    expect(screen.getByTestId("loop-eval-note")).toHaveTextContent("No fraud detected yet in this run");
    expect(screen.getByTestId("loop-workload-note")).toHaveTextContent("No labels yet.");
  });

  it("shows per-sensor weight changes once labels moved them, without the ceiling note", async () => {
    routeFetch({ "/metrics": { ...sampleMetrics, weights: { amount: 0.85, geo: 1 } } });
    render(<LoopPanel />);
    expect(await screen.findByTestId("loop-weights-delta")).toHaveTextContent("amount −0.15");
    expect(screen.getByTestId("loop-weights-delta")).not.toHaveTextContent("geo");
    expect(screen.queryByTestId("loop-weights-note")).toBeNull();
  });

  it("shows the simulated analyst chip only while it is enabled", async () => {
    routeFetch({ "/metrics": sampleMetrics });
    render(<LoopPanel />);
    await waitFor(() => expect(screen.getByTestId("loop-breach")).toHaveTextContent("Within latency target"));
    expect(screen.queryByTestId("sim-analyst")).toBeNull(); // not reported

    const status = { status: "ok", sim_investigator: { enabled: true, labels_total: 12, confirmed: 9, false_positive: 3, error_rate: 0.05, interval_s: 20 } };
    act(() => dashboard.getState().setSimAnalyst(simAnalystOf(status)));
    const chip = screen.getByTestId("sim-analyst");
    expect(chip).toHaveTextContent("Simulated analyst on · 12 labels (9 fraud, 3 false positive)");
    expect(chip).toHaveTextContent(SIM_ANALYST_HELP);

    act(() => dashboard.getState().setSimAnalyst(simAnalystOf({ ...status, sim_investigator: { enabled: false } })));
    expect(screen.queryByTestId("sim-analyst")).toBeNull();
    expect(simAnalystOf({ status: "ok" })).toBeNull(); // older server
  });

  it("shows n/a for workload when GET /metrics does not report it", async () => {
    routeFetch({ "/metrics": sampleMetrics });
    render(<LoopPanel />);
    await waitFor(() => expect(screen.getByTestId("loop-breach")).toHaveTextContent("Within latency target"));
    expect(screen.getByTestId("loop-workload")).toHaveTextContent("n/a");
  });

  it("refreshes by polling and derives per-stage throughput from counters over time", async () => {
    let n = 0;
    const fetchFn = routeFetch({
      "/metrics": () => {
        n += 1;
        const total = 13 + n * 10;
        const stages = Object.fromEntries(
          Object.entries(sampleMetrics.stages as Record<string, object>).map(([k, s]) => [k, { ...s, total }]),
        );
        return jsonResponse({ ...sampleMetrics, stages });
      },
    });
    render(<LoopPanel pollMs={40} />);
    await waitFor(() => expect(fetchFn.mock.calls.filter(([u]) => String(u).endsWith("/metrics")).length).toBeGreaterThanOrEqual(3));
    await waitFor(() => expect(cell("PERCEIVE", 0).textContent).not.toBe("n/a"));
    expect(Number(cell("PERCEIVE", 0).textContent)).toBeGreaterThan(0);
  });

  it("applies /ws/metrics frames from the store between polls, keeping REST-only fields", async () => {
    routeFetch({ "/metrics": sampleMetrics });
    render(<LoopPanel />);
    await waitFor(() => expect(screen.getByTestId("loop-eval")).toHaveTextContent("Not applicable"));
    act(() =>
      dashboard.getState().setMetrics({
        channel: "metrics",
        type: "metrics.latency",
        seq: 9,
        stages: { PERCEIVE: { count: 3, mean_ms: 2.5, total: 40 } },
        ingest: { rate_eps: 12.5, queue_length: 0 },
        latency_breach: { active: false, count: 0, threshold_ms: 50, p95_ms: 3 },
      }),
    );
    expect(cell("PERCEIVE", 1)).toHaveTextContent("2.500");
    expect(cell("SENSE", 0)).toHaveTextContent("12.5");
    expect(screen.getByTestId("loop-eval")).toHaveTextContent("Not applicable"); // not in WS frames
  });
});

describe("loop view-model", () => {
  it("computes events/s between the oldest and newest sample and resets on counter decrease", () => {
    let s: Sample[] = [];
    s = pushSample(s, { at: 0, totals: { ACT: 100 } });
    expect(throughput(s, "ACT")).toBeNull();
    s = pushSample(s, { at: 2_000, totals: { ACT: 140 } });
    expect(throughput(s, "ACT")).toBeCloseTo(20, 9);
    s = pushSample(s, { at: 2_000, totals: { ACT: 150 } }); // not newer: ignored
    expect(s).toHaveLength(2);
    s = pushSample(s, { at: 3_000, totals: { ACT: 5 } }); // server restarted
    expect(s).toHaveLength(1);
    expect(throughput(s, "ACT")).toBeNull();
  });

  it("drops samples older than the horizon", () => {
    let s: Sample[] = [];
    for (let t = 0; t <= 20_000; t += 1_000) s = pushSample(s, { at: t, totals: { ACT: t / 100 } });
    expect(s[0]!.at).toBeGreaterThanOrEqual(10_000);
    expect(throughput(s, "ACT")).toBeCloseTo(10, 9);
  });

  it("normalises unavailable latency and missing evaluation without inventing values", () => {
    const v = normaliseMetrics({ stages: { ACT: { count: 0, mean_ms: "unavailable", total: 0 } }, latency_breach: { active: false, p95_ms: "unavailable" } });
    expect(v.stages?.ACT).toEqual({ meanMs: null, total: 0 });
    expect(v.breach?.p95Ms).toBeNull();
    expect("evaluation" in v).toBe(false);
    expect("weights" in v).toBe(false);
    expect(normaliseMetrics({ evaluation: null }).evaluation).toBe(NOT_APPLICABLE);
    expect(stageRows(v, []).find((r) => r.stage === "ACT")).toMatchObject({ meanMs: null, throughput: null });
  });

  it("reads the Antibody_Pool size from several plausible /immune shapes, else null", () => {
    expect(poolSize({ pool_size: 3 })).toEqual({ size: 3, max: null });
    expect(poolSize({ pool: { size: 4, max: 200 } })).toEqual({ size: 4, max: 200 });
    expect(poolSize({ pool: [1, 2] })).toEqual({ size: 2, max: null });
    expect(poolSize({ active: [1, 2, 3] })).toEqual({ size: 3, max: null });
    expect(poolSize({ enabled: false })).toBeNull();
    expect(poolSize(null)).toBeNull();
    expect(attackerRunning({ running: false })).toBe(false);
    expect(attackerRunning({ state: "RUNNING" })).toBe(true);
    expect(attackerRunning({})).toBeNull();
  });
});
