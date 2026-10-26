import { act, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it } from "vitest";
import { axe } from "vitest-axe";
import { App } from "./App";
import { dashboard } from "./store/store";
import { caseMsg, caseSummary, feedItem, routeFetch } from "./test/fixtures";
import { aliceDetail, aliceEvidence, sampleMetrics } from "./test/sampleRun";
import { aliceSubgraph, aliceTimeline } from "./test/vizFixtures";

// Automated axe checks catch only a subset of WCAG issues and do not prove conformance;
// manual keyboard and screen-reader testing is still required. jsdom does no layout, so
// colour-contrast cannot be evaluated here and is disabled; the palette is checked
// separately by src/a11y/contrast.test.ts.
const AXE_OPTIONS = { rules: { "color-contrast": { enabled: false } } };

function seed() {
  const s = dashboard.getState();
  s.applyEvents([feedItem(1), feedItem(2)]);
  s.applyCaseMessage(caseMsg(caseSummary("C-1", { tier: "CRITICAL", priority: "900.00" }), 1, 3));
  s.applyCaseMessage(caseMsg(caseSummary("C-2", { tier: "LOW", priority: "10.00" }), 2, 3));
  s.selectCase("C-1");
  s.setConfirmed({ alertBudget: 20, investigationCost: 50, generatorRunning: true });
}

beforeEach(() => {
  // /immune and /attacker are absent (404), as on a server without the Immune_System API.
  routeFetch({
    "/cases/C-1": aliceDetail,
    "/cases/C-1/evidence": aliceEvidence,
    "/cases/C-1/subgraph": aliceSubgraph,
    "/cases/C-1/timeline": aliceTimeline,
    "/metrics": sampleMetrics,
  });
});

/** The app opens on Home; the live panels are on the Dashboard tab. */
async function openDashboard() {
  await userEvent.setup().click(screen.getByRole("tab", { name: "Dashboard" }));
}

async function loaded() {
  await screen.findByTestId("ev-summary");
  await waitFor(() => expect(screen.getByTestId("loop-breach")).toHaveTextContent("Within latency target"));
}

// Whole-app integration tests (Home plus the live grid): allow for a loaded test machine.
describe("App shell", { timeout: 20_000 }, () => {
  it("opens on Home with the live grid mounted behind it, and Open dashboard shows the grid", async () => {
    render(<App />);
    const selected = screen.getAllByRole("tab").filter((t) => t.getAttribute("aria-selected") === "true");
    expect(selected.map((t) => t.textContent)).toEqual(["Home"]);
    expect(screen.getAllByRole("tab").map((t) => t.textContent)).toEqual([
      "Home",
      "Dashboard",
      "Workflow",
      "SOC console",
      "Red vs Blue",
      "Benchmark",
      "Trace",
      "Live log",
      "Pricing",
    ]);
    // The grid holds the WebSocket-driven state, so it is rendered (hidden) from the start.
    const main = document.getElementById("main")!;
    expect(main).not.toBeVisible();
    await waitFor(() => expect(screen.getByTestId("loop-breach")).toHaveTextContent("Within latency target"));
    await userEvent.setup().click(within(screen.getByTestId("app-header")).getByRole("button", { name: "Open dashboard" }));
    expect(main).toBeVisible();
    expect(document.getElementById("main")).toBe(main); // same element: not remounted
    // No splash overlay in tests.
    expect(screen.queryByTestId("splash")).toBeNull();
  });

  it("has no axe violations when empty, on Home and on the Dashboard", async () => {
    const { container } = render(<App />);
    await waitFor(() => expect(screen.getByTestId("loop-breach")).toHaveTextContent("Within latency target"));
    const home = await axe(container, AXE_OPTIONS);
    expect(home.violations.map((v) => `${v.id}: ${v.help}`)).toEqual([]);
    await openDashboard();
    const results = await axe(container, AXE_OPTIONS);
    expect(results.violations.map((v) => `${v.id}: ${v.help}`)).toEqual([]);
  });

  it("has no axe violations with live data, loaded evidence and loop metrics", async () => {
    act(() => seed());
    const { container } = render(<App />);
    await openDashboard();
    await loaded();
    expect(screen.getAllByRole("img").length).toBeGreaterThan(0); // mass bars rendered
    expect(screen.getByRole("heading", { level: 1, name: "HELIX" })).toBeInTheDocument();
    const results = await axe(container, AXE_OPTIONS);
    expect(results.violations.map((v) => `${v.id}: ${v.help}`)).toEqual([]);
  });

  it("shows the disconnected indicator while keeping data, and announces new CRITICAL cases", async () => {
    render(<App />);
    act(() => seed());
    expect(screen.getByTestId("announce-assertive")).toHaveTextContent("New CRITICAL case C-1");
    act(() => dashboard.getState().setConnection("down"));
    expect(screen.getByTestId("connection")).toHaveTextContent("Disconnected");
    await openDashboard();
    expect(screen.getAllByRole("option")).toHaveLength(2);
    expect(screen.getByRole("region", { name: /live transaction feed/i })).toHaveTextContent("T-2");
    act(() => dashboard.getState().setConnection("open"));
    expect(screen.getByTestId("connection")).toHaveTextContent("Live");
    await loaded();
  });

  it("puts the header (skip link, view tabs, primary action) before the dashboard in focus order", async () => {
    render(<App />);
    await openDashboard();
    const user = userEvent.setup();
    (document.activeElement as HTMLElement | null)?.blur();
    await user.tab();
    expect(document.activeElement).toBe(screen.getByRole("link", { name: "Skip to dashboard" }));
    await user.tab();
    expect(document.activeElement).toBe(screen.getByRole("tab", { name: "Dashboard" }));
    await user.tab();
    expect(document.activeElement).toBe(within(screen.getByTestId("app-header")).getByRole("button", { name: "Open dashboard" }));
    await user.tab();
    expect(document.getElementById("main")!.contains(document.activeElement)).toBe(true);
    await waitFor(() => expect(screen.getByTestId("loop-breach")).toHaveTextContent("Within latency target"));
  });

  it("makes every panel a region labelled by its own heading", async () => {
    act(() => seed());
    const { container } = render(<App />);
    await openDashboard();
    await loaded();
    const panels = [...container.querySelectorAll("#main section.panel")];
    expect(panels.length).toBeGreaterThanOrEqual(3);
    for (const p of panels) {
      const heading = document.getElementById(p.getAttribute("aria-labelledby") ?? "");
      expect(heading?.tagName).toBe("H2");
      expect(p.contains(heading)).toBe(true);
      expect(screen.getByRole("region", { name: heading!.textContent! })).toBe(p);
    }
  });

  it("reaches every enabled control and focusable region with the keyboard", async () => {
    act(() => seed());
    const { container } = render(<App />);
    await openDashboard();
    await loaded();
    const user = userEvent.setup();
    // <summary> is natively focusable in browsers but user-event's Tab does not model it, so
    // summaries are checked separately: they must not be removed from the tab order.
    const summaries = [...container.querySelectorAll("summary")];
    expect(summaries.length).toBeGreaterThan(0);
    for (const s of summaries) {
      expect(s.hasAttribute("tabindex")).toBe(false);
      expect(s.parentElement?.firstElementChild).toBe(s);
    }
    const focusable = [
      ...container.querySelectorAll<HTMLElement>("a[href], button:not(:disabled), input:not(:disabled), [tabindex]"),
    ].filter((el) => {
      // Content of a closed <details> is not focusable in browsers.
      const d = el.closest("details");
      return el.tabIndex >= 0 && el.id !== "main" && (!d || d.open);
    });
    expect(focusable.length).toBeGreaterThan(10);
    const seen = new Set<Element>();
    for (let i = 0; i < focusable.length * 2; i++) {
      await user.tab();
      if (document.activeElement) seen.add(document.activeElement);
    }
    const missed = focusable.filter((el) => !seen.has(el)).map((el) => el.outerHTML.slice(0, 80));
    expect(missed).toEqual([]);
  });
});
