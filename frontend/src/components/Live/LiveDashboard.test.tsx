import { act, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";
import { axe } from "vitest-axe";
import type { CaseDetail, SuggestedLabel } from "../../api/types";
import { dashboard } from "../../store/store";
import { caseMsg, caseSummary, feedItem, jsonResponse, routeFetch } from "../../test/fixtures";
import { aliceDetail, aliceEvidence } from "../../test/sampleRun";
import { aliceTimeline } from "../../test/vizFixtures";
import { LiveDashboard } from "./LiveDashboard";

// The WebGL views are replaced by stand-ins that count their mounts (their own behaviour is
// covered by Network3D.test and GeoMap.test, with the WebGL libraries mocked in setup.ts).
// ``geoFails`` makes the map throw, to check that its error boundary isolates it.
const viz = vi.hoisted(() => ({ network: 0, geo: 0, geoFails: false }));
vi.mock("../Network3D/Network3D", async () => {
  const { Panel } = await import("../common/Panel");
  return {
    Network3D: () => {
      viz.network += 1;
      return (
        <Panel title="Network" area="network">
          <p data-testid="mock-network">network view</p>
        </Panel>
      );
    },
  };
});
vi.mock("../GeoMap/GeoMap", async () => {
  const { Panel } = await import("../common/Panel");
  return {
    GeoMap: () => {
      viz.geo += 1;
      if (viz.geoFails) throw new Error("WebGL context lost");
      return (
        <Panel title="Map" area="geo">
          <p data-testid="mock-geo">map view</p>
        </Panel>
      );
    },
  };
});

const AXE_OPTIONS = { rules: { "color-contrast": { enabled: false } } };
// The whole dashboard (with axe runs) is heavy; under a parallel full-suite run the default
// 5 s per test is too tight on a loaded machine.
vi.setConfig({ testTimeout: 20_000 });

const SUGGESTION: SuggestedLabel = { label: "CONFIRMED_FRAUD", confidence: 0.949, reason: "committed evidence supports fraud" };
const alice: CaseDetail = { ...aliceDetail, suggested_label: SUGGESTION };

type Call = { path: string; body: unknown };

/** Server for Alice's Case C-1 (evidence, timeline, open/feedback) and /metrics. */
function serveAlice(calls: Call[] = [], metrics: unknown = { latency_breach: { active: false, count: 0, threshold_ms: 50, p95_ms: 3.2 } }) {
  const post = (status: string, extra: Partial<CaseDetail> = {}) => (init: RequestInit | undefined, url: string) => {
    calls.push({ path: url.replace(/^.*\/api\/v1/, ""), body: init?.body ? JSON.parse(String(init.body)) : undefined });
    return jsonResponse({ ...alice, status, assignee: "alice", updated_at: "2024-06-05T11:30:00+00:00", ...extra });
  };
  return routeFetch({
    "/cases/C-1": alice,
    "/cases/C-1/evidence": aliceEvidence,
    "/cases/C-1/timeline": aliceTimeline,
    "/cases/C-1/open": post("IN_REVIEW"),
    "/cases/C-1/feedback": post("CLOSED", { label: "CONFIRMED_FRAUD", placement: "NONE", closure_reason: "LABELLED" }),
    "/metrics": metrics,
  });
}

function seedAlice() {
  act(() => {
    const s = dashboard.getState();
    s.applyEvents([feedItem(1), feedItem(2)]);
    s.applyCaseMessage(caseMsg(alice, 1, 4));
    s.applyCaseMessage(caseMsg(caseSummary("C-2", { tier: "LOW", priority: "10.00", accounts: ["A-1", "A-2", "A-3", "A-4", "A-5"] }), 2, 4));
    s.setConnection("open");
    s.selectCase("C-1");
  });
}

/** Stand-in for POST /events (see DemoScenario.test): field validation answers 422 and a
 *  repeated tx_id is a duplicate. */
function ingestRoutes() {
  const seen = new Set<string>();
  const bodies: Array<Record<string, string>> = [];
  return {
    bodies,
    routes: {
      "/events": (init: RequestInit | undefined) => {
        const doc = JSON.parse(String(init?.body)) as Record<string, string>;
        bodies.push(doc);
        if (!(Number(doc.amount) >= 0.01) || !/^[A-Z]{3}$/.test(doc.currency ?? ""))
          return jsonResponse({ detail: "invalid Transaction_Event", errors: [{ field: "amount", reason: "too small" }] }, 422);
        const dup = seen.has(doc.tx_id!);
        seen.add(doc.tx_id!);
        return jsonResponse({ tx_id: doc.tx_id, status: dup ? "duplicate" : "accepted", duplicate: dup, backpressure: false }, 202);
      },
      "/cases": { items: [], page_size: 100, total: 0, next_cursor: null, backlog_count: 0 },
    },
  };
}

function resetViz() {
  viz.network = 0;
  viz.geo = 0;
  viz.geoFails = false;
}

describe("LiveDashboard KPI strip", () => {
  it("shows the store's counts and the server's loop metrics", async () => {
    serveAlice([], {
      latency_breach: { active: false, count: 0, threshold_ms: 50, p95_ms: 3.2 },
      evaluation: { precision: 0.5, recall: 0.25, alerts_per_hour: 2, latency_median_s: 30, latency_mean_s: 40 },
    });
    seedAlice();
    render(<LiveDashboard />);

    expect(screen.getByTestId("kpi-active")).toHaveTextContent("2");
    expect(screen.getByTestId("kpi-backlog")).toHaveTextContent("4");
    expect(screen.getByTestId("kpi-connection")).toHaveTextContent("Live");
    await waitFor(() => expect(screen.getByTestId("kpi-p95")).toHaveTextContent("3.20 ms"));
    expect(screen.getByTestId("kpi-p95-chip")).toHaveTextContent("within target");
    expect(screen.getByTestId("kpi-eval")).toHaveTextContent("0.50 / 0.25");

    // End-to-end throughput comes from the END_TO_END counter across two frames.
    expect(screen.getByTestId("kpi-eps")).toHaveTextContent("n/a");
    const frame = (seq: number, total: number) => ({
      channel: "metrics" as const,
      type: "metrics.latency",
      seq,
      stages: { END_TO_END: { count: 1, mean_ms: 2, total } },
    });
    act(() => dashboard.getState().setMetrics(frame(1, 100)));
    await new Promise((r) => setTimeout(r, 30));
    act(() => dashboard.getState().setMetrics(frame(2, 160)));
    await waitFor(() => expect(Number(screen.getByTestId("kpi-eps").textContent)).toBeGreaterThan(0));
  });

  it("shows n/a for everything the server does not report", async () => {
    const fetchFn = routeFetch({ "/metrics": { evaluation: null } });
    render(<LiveDashboard />);
    await waitFor(() => expect(fetchFn.mock.calls.some(([u]) => String(u).endsWith("/metrics"))).toBe(true));
    await waitFor(() => expect(screen.getByTestId("loop-eval")).toHaveTextContent("Not applicable"));
    expect(screen.getByTestId("kpi-active")).toHaveTextContent("0");
    expect(screen.getByTestId("kpi-backlog")).toHaveTextContent("0");
    expect(screen.getByTestId("kpi-eps")).toHaveTextContent("n/a");
    expect(screen.getByTestId("kpi-p95")).toHaveTextContent("n/a");
    expect(screen.queryByTestId("kpi-p95-chip")).toBeNull();
    expect(screen.getByTestId("kpi-eval")).toHaveTextContent("n/a");
    expect(screen.getByTestId("kpi-connection")).toHaveTextContent("Connecting…");
  });
});

describe("LiveDashboard layout", () => {
  it("orders the sections KPI strip → queue → workspace → feed and loop, inside #main", () => {
    serveAlice();
    seedAlice();
    const { container } = render(<LiveDashboard />);
    const main = container.querySelector("main#main");
    expect(main).not.toBeNull();
    expect(main).toHaveAttribute("tabindex", "-1");
    const headings = screen.getAllByRole("heading", { level: 2 }).map((h) => h.textContent);
    expect(headings).toEqual(["Key metrics", "Case queue", "Case workspace", "Evidence", "Live feed", "HELIX loop"]);
    expect(screen.getByRole("heading", { name: "Live feed", level: 2 })).toBeInTheDocument();
    const feed = screen.getByRole("region", { name: "Live transaction feed, newest first" });
    expect(feed).toHaveTextContent("T-2");
    // Compact rows: the clock to the second with another day's date beneath; the full
    // millisecond timestamp stays in the title. Tx id and route share one cell.
    const row = within(feed).getAllByRole("row")[1]!;
    const time = row.querySelector("td.feed__time")!;
    expect(time).toHaveAttribute("title", "2025-03-14 10:22:05.120");
    expect(time).toHaveTextContent(/^10:22:052025-03-14$/);
    expect(row.querySelector("td.feed__tx")).toHaveTextContent("T-2A-1 →to A-2");
  });

  it("shows a friendly empty state with a demo action when no Case is selected", () => {
    routeFetch({});
    render(<LiveDashboard />);
    const empty = screen.getByTestId("ws-empty");
    expect(empty).toHaveTextContent("No case selected");
    expect(empty).toHaveTextContent("Load the demo scenario or pick a Case from the queue");
    expect(within(empty).getByRole("button", { name: "Run the demo scenario" })).toBeEnabled();
    expect(screen.queryByRole("tablist", { name: "Case views" })).toBeNull();
    // The investigator name stays available (bulk labelling uses it).
    expect(screen.getByLabelText("Investigator name")).toBeInTheDocument();
  });

  it("shows the selected Case's header with its suggestion marked as a suggestion", async () => {
    serveAlice();
    seedAlice();
    render(<LiveDashboard />);
    const header = screen.getByTestId("ws-header");
    expect(within(header).getByRole("heading", { level: 3 })).toHaveTextContent("C-1");
    expect(header).toHaveTextContent("CRITICAL");
    expect(screen.getByTestId("ws-risk")).toHaveTextContent("0.97");
    expect(header).toHaveTextContent("EUR 4,800.00");
    expect(screen.getByTestId("ws-suggestion")).toHaveTextContent("Confirmed fraud · 95%");
    expect(screen.getByTestId("ws-suggestion")).toHaveTextContent("Suggestion only: the investigator decides");
    await screen.findByTestId("ev-summary");
  });
});

describe("LiveDashboard workspace sub-tabs", () => {
  it("switches views with click and arrow keys, mounting only the selected one", async () => {
    resetViz();
    serveAlice();
    seedAlice();
    const user = userEvent.setup();
    render(<LiveDashboard />);
    const tabs = within(screen.getByRole("tablist", { name: "Case views" })).getAllByRole("tab");
    expect(tabs.map((t) => t.textContent)).toEqual(["Evidence", "Network", "Map", "Replay"]);
    expect(tabs[0]).toHaveAttribute("aria-selected", "true");
    expect(tabs.filter((t) => t.tabIndex === 0)).toEqual([tabs[0]]);
    await screen.findByTestId("ev-summary");
    expect(viz.network).toBe(0);
    expect(viz.geo).toBe(0);

    await user.click(tabs[1]!);
    expect(tabs[1]).toHaveAttribute("aria-selected", "true");
    expect(screen.getByTestId("mock-network")).toBeInTheDocument();
    expect(screen.queryByTestId("ev-summary")).toBeNull();
    expect(screen.getByRole("tabpanel")).toHaveAttribute("aria-labelledby", tabs[1]!.id);

    await user.keyboard("{ArrowRight}");
    expect(tabs[2]).toHaveFocus();
    expect(tabs[2]).toHaveAttribute("aria-selected", "true");
    expect(screen.getByTestId("mock-geo")).toBeInTheDocument();
    expect(screen.queryByTestId("mock-network")).toBeNull();

    await user.keyboard("{End}");
    expect(tabs[3]).toHaveAttribute("aria-selected", "true");
    expect(await screen.findByRole("slider", { name: "Replay time" })).toBeInTheDocument();
    expect(screen.queryByTestId("mock-geo")).toBeNull();

    await user.keyboard("{ArrowRight}"); // wraps
    expect(tabs[0]).toHaveFocus();
    expect(await screen.findByTestId("ev-summary")).toBeInTheDocument();
    await user.keyboard("{Home}{ArrowLeft}");
    expect(tabs[3]).toHaveAttribute("aria-selected", "true");
  });

  it("keeps the rest of the dashboard working when a WebGL view fails", async () => {
    resetViz();
    viz.geoFails = true;
    vi.spyOn(console, "error").mockImplementation(() => undefined);
    serveAlice();
    seedAlice();
    const user = userEvent.setup();
    render(<LiveDashboard />);
    await user.click(screen.getByRole("tab", { name: "Map" }));
    expect(await screen.findByTestId("panel-error-geo")).toHaveTextContent("The map view failed: WebGL context lost");
    expect(screen.getAllByRole("option")).toHaveLength(2);
    expect(screen.getByRole("button", { name: "Confirm fraud" })).toBeInTheDocument();
    await user.click(screen.getByRole("tab", { name: "Evidence" }));
    expect(await screen.findByTestId("ev-summary")).toBeInTheDocument();
    vi.mocked(console.error).mockRestore();
  });
});

describe("LiveDashboard case actions", () => {
  it("mounts one CaseActions, and the F shortcut runs its Confirm fraud path", async () => {
    const calls: Call[] = [];
    serveAlice(calls);
    seedAlice();
    const user = userEvent.setup();
    render(<LiveDashboard />);
    await screen.findByTestId("ev-summary");
    expect(screen.getAllByTestId("case-actions")).toHaveLength(1);
    for (const name of ["Confirm fraud", "False positive", "Escalate"]) {
      expect(screen.getAllByRole("button", { name })).toHaveLength(1);
    }
    expect(screen.getByRole("button", { name: "Confirm fraud" })).toHaveAttribute("aria-keyshortcuts", "F");

    const evidence = screen.getByRole("region", { name: "Evidence" });
    act(() => evidence.focus());
    await user.keyboard("f");
    expect(screen.getByRole("alert")).toHaveTextContent("Enter your investigator name first.");
    expect(calls).toEqual([]);

    await user.type(screen.getByLabelText("Investigator name"), "alice");
    act(() => evidence.focus());
    await user.keyboard("F");
    await waitFor(() => expect(screen.getByText(/C-1 is CLOSED with label CONFIRMED_FRAUD/)).toBeInTheDocument());
    expect(calls).toEqual([
      { path: "/cases/C-1/open", body: { investigator: "alice" } },
      { path: "/cases/C-1/feedback", body: { by: "alice", label: "CONFIRMED_FRAUD", note: "" } },
    ]);
  });
});

describe("LiveDashboard Controls drawer", () => {
  it("opens as a modal dialog, traps focus, closes on Escape and returns focus to the trigger", async () => {
    routeFetch({});
    const user = userEvent.setup();
    render(<LiveDashboard />);
    const trigger = screen.getByRole("button", { name: "Controls" });
    expect(trigger).toHaveAttribute("aria-expanded", "false");
    expect(screen.queryByRole("dialog")).toBeNull();

    await user.click(trigger);
    const dialog = screen.getByRole("dialog", { name: "Controls" });
    expect(dialog).toHaveAttribute("aria-modal", "true");
    expect(trigger).toHaveAttribute("aria-expanded", "true");
    expect(trigger).toHaveAttribute("aria-controls", dialog.id);
    const close = within(dialog).getByRole("button", { name: "Close controls" });
    expect(close).toHaveFocus();
    expect(within(dialog).getByLabelText("Alert budget")).toBeInTheDocument();
    expect(within(dialog).getByLabelText("Investigation cost")).toBeInTheDocument();
    expect(within(dialog).getByRole("button", { name: "Start generator" })).toBeInTheDocument();
    expect(within(dialog).getByRole("button", { name: "Stop generator" })).toBeInTheDocument();
    expect(dialog).toHaveTextContent("Sends the 15 lines of docs/sample_transactions.jsonl");

    // Focus trap: Shift+Tab from the first element wraps to the last, Tab from the last to the first.
    const last = within(dialog).getByRole("button", { name: "Stop generator" });
    await user.tab({ shift: true });
    expect(last).toHaveFocus();
    await user.tab();
    expect(close).toHaveFocus();
    for (let i = 0; i < 12; i++) {
      await user.tab();
      expect(dialog.contains(document.activeElement)).toBe(true);
    }

    await user.keyboard("{Escape}");
    expect(screen.queryByRole("dialog")).toBeNull();
    expect(trigger).toHaveFocus();
    expect(trigger).toHaveAttribute("aria-expanded", "false");

    // Escape still closes it when focus has dropped out of the drawer to <body> (as after
    // the focused generator button turns disabled).
    await user.click(trigger);
    (document.activeElement as HTMLElement).blur();
    expect(document.body).toHaveFocus();
    await user.keyboard("{Escape}");
    expect(screen.queryByRole("dialog")).toBeNull();
    expect(trigger).toHaveFocus();

    // The close button and the backdrop close it too.
    await user.click(trigger);
    await user.click(screen.getByRole("button", { name: "Close controls" }));
    expect(screen.queryByRole("dialog")).toBeNull();
    expect(trigger).toHaveFocus();
    await user.click(trigger);
    fireEvent.click(screen.getByTestId("drawer-backdrop"));
    expect(screen.queryByRole("dialog")).toBeNull();
  });
});

describe("LiveDashboard demo scenario", () => {
  it("keeps one visible Load demo scenario button that submits the run and shows its result under the strip", async () => {
    const { routes, bodies } = ingestRoutes();
    routeFetch(routes);
    const user = userEvent.setup();
    render(<LiveDashboard />);
    // Playwright matches names by substring: only one button may contain the phrase.
    expect(screen.getAllByRole("button", { name: /load demo scenario/i })).toHaveLength(1);
    const button = screen.getByRole("button", { name: "Load demo scenario" });
    expect(button).toBeVisible();

    await user.click(button);
    const line = screen.getByTestId("demo-scenario");
    expect(await within(line).findByTestId("demo-result")).toHaveTextContent("13 accepted, 1 duplicate, 1 rejected by validation");

    const tag = String(bodies[0]!.tx_id).replace(/^T001/, "");
    act(() => dashboard.getState().applyCaseMessage(caseMsg(caseSummary("C-260", { accounts: [`ACC-ALICE${tag}`], tier: "CRITICAL" }))));
    expect(await within(line).findByRole("button", { name: "Show C-260" })).toBeInTheDocument();
    expect(within(line).getByRole("listitem")).toHaveTextContent("Alice");
    // The first scenario Case is selected automatically and opens in the workspace.
    await waitFor(() => expect(screen.getByTestId("ws-header")).toHaveTextContent("C-260"));

    await user.click(screen.getByRole("button", { name: "Dismiss demo result" }));
    expect(screen.queryByTestId("demo-result")).toBeNull();
  });

  it("the empty state's action runs the same scenario", async () => {
    const { routes, bodies } = ingestRoutes();
    routeFetch(routes);
    const user = userEvent.setup();
    render(<LiveDashboard />);
    await user.click(screen.getByRole("button", { name: "Run the demo scenario" }));
    expect(await screen.findByTestId("demo-result")).toHaveTextContent("13 accepted");
    expect(bodies).toHaveLength(15);
  });
});

describe("LiveDashboard accessibility", () => {
  it("has no axe violations when empty", async () => {
    routeFetch({});
    const { container } = render(<LiveDashboard />);
    await screen.findByTestId("dataset-absent");
    const results = await axe(container, AXE_OPTIONS);
    expect(results.violations.map((v) => `${v.id}: ${v.help}`)).toEqual([]);
  });

  it("has no axe violations with a selected Case, loaded evidence, and with the drawer open", async () => {
    serveAlice();
    seedAlice();
    const user = userEvent.setup();
    const { container } = render(<LiveDashboard />);
    await screen.findByTestId("ev-summary");
    await waitFor(() => expect(screen.getByTestId("loop-breach")).toHaveTextContent("Within latency target"));
    let results = await axe(container, AXE_OPTIONS);
    expect(results.violations.map((v) => `${v.id}: ${v.help}`)).toEqual([]);

    await user.click(screen.getByRole("button", { name: "Controls" }));
    results = await axe(container, AXE_OPTIONS);
    expect(results.violations.map((v) => `${v.id}: ${v.help}`)).toEqual([]);
  });
});
