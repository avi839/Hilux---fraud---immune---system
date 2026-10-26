import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it } from "vitest";
import { axe } from "vitest-axe";
import { BenchmarkPage } from "./BenchmarkPage";
import { SUMMARY, badges, int, pct, systemsOf, type BenchmarkSummary } from "./benchmarkModel";

// jsdom does no layout, so colour-contrast is checked separately (src/a11y).
const AXE_OPTIONS = { rules: { "color-contrast": { enabled: false } } };

const SYSTEM_IDS = ["helix", "helix-backlog", "rule", "iforest", "xgboost"];
const keyOf = (id: string) => systemsOf(SUMMARY).find((s) => s.id === id)!.key;
const cell = (id: string, metric: string) => screen.getByTestId(`bench-cell-${id}-${metric}`);
const badgeOf = (id: string, metric: string) => cell(id, metric).querySelector("[data-badge]")?.textContent ?? null;

/** A copy of the real results in which the rule baseline has the best alert precision. */
function ruleWinsPrecision(): BenchmarkSummary {
  const s = JSON.parse(JSON.stringify(SUMMARY)) as BenchmarkSummary;
  s.systems[keyOf("rule")]!.by_capacity["25"]!.alert_precision = 0.9;
  return s;
}

describe("BenchmarkPage", () => {
  it("renders one page title and a row for every system in the results file", () => {
    render(<BenchmarkPage />);
    expect(screen.getAllByRole("heading", { level: 2 }).map((h) => h.textContent)).toEqual(["HELIX vs ML baselines"]);
    const table = screen.getByRole("table", { name: /Results at N = 25 alerts per hour/ });
    for (const id of SYSTEM_IDS) expect(within(table).getByTestId(`bench-row-${id}`)).toBeInTheDocument();
    expect(Object.keys(SUMMARY.systems)).toHaveLength(SYSTEM_IDS.length);
    expect(screen.getByTestId("bench-read-first")).toHaveTextContent("Read this first");
  });

  it("switches the numbers with the N = 25 | 50 | 100 radio group, by click and arrow keys", async () => {
    render(<BenchmarkPage />);
    const user = userEvent.setup();
    const xgb = SUMMARY.systems[keyOf("xgboost")]!.by_capacity;
    const group = screen.getByRole("radiogroup", { name: /alerts per hour/ });
    const [r25, r50, r100] = within(group).getAllByRole("radio");
    expect(r25).toHaveAttribute("aria-checked", "true");
    expect(cell("xgboost", "alerts")).toHaveTextContent(int(xgb["25"]!.alerts));

    await user.click(r50!);
    expect(r50).toHaveAttribute("aria-checked", "true");
    expect(cell("xgboost", "alerts")).toHaveTextContent(int(xgb["50"]!.alerts));
    expect(cell("xgboost", "alert_precision")).toHaveTextContent(pct(xgb["50"]!.alert_precision));
    expect(screen.getByRole("table", { name: /Results at N = 50/ })).toBeInTheDocument();

    await user.keyboard("{ArrowRight}");
    expect(r100).toHaveAttribute("aria-checked", "true");
    expect(document.activeElement).toBe(r100);
    expect([r25!.tabIndex, r50!.tabIndex, r100!.tabIndex]).toEqual([-1, -1, 0]);
    expect(cell("xgboost", "alerts")).toHaveTextContent(int(xgb["100"]!.alerts));
  });

  it("computes winner badges from the data on the real results", () => {
    render(<BenchmarkPage />);
    // Supervised XGBoost has the best alert precision; HELIX is best among the label-free systems.
    expect(badgeOf("xgboost", "alert_precision")).toBe("Best overall");
    expect(badgeOf("helix", "alert_precision")).toBe("Best among label-free");
    expect(badgeOf("rule", "alert_precision")).toBeNull();
    // Alerts are not ranked.
    for (const id of SYSTEM_IDS) expect(badgeOf(id, "alerts")).toBeNull();
    // KPI tiles compare HELIX with the best label-free baseline, from the data.
    expect(screen.getByTestId("kpi-alert_precision-cmp")).toHaveTextContent("Better than best label-free baseline (Isolation Forest");
  });

  it("moves the badges when a baseline wins in the data (nothing is hard-coded)", () => {
    render(<BenchmarkPage summary={ruleWinsPrecision()} />);
    expect(badgeOf("rule", "alert_precision")).toBe("Best overall");
    expect(badgeOf("xgboost", "alert_precision")).toBeNull();
    // A label-free system is best overall, so no separate label-free badge is given.
    expect(badgeOf("helix", "alert_precision")).toBeNull();
    expect(screen.getByTestId("kpi-alert_precision-cmp")).toHaveTextContent("Worse than best label-free baseline (Rule baseline: 90.0%)");
  });

  it("gives every tied system the badge and skips systems without a value", () => {
    const systems = systemsOf(SUMMARY);
    const m = badges(systems, (s) => (s.id === "xgboost" ? null : s.id === "rule" || s.id === "iforest" ? 5 : 1), "higher");
    expect(m.get("rule")).toBe("overall");
    expect(m.get("iforest")).toBe("overall");
    expect(m.has("xgboost")).toBe(false);
    expect(m.has("helix")).toBe(false);
  });

  it("shows Labels needed: Yes only for XGBoost", () => {
    render(<BenchmarkPage />);
    expect(within(screen.getByTestId("bench-table")).getByRole("columnheader", { name: "Labels needed" })).toBeInTheDocument();
    for (const id of SYSTEM_IDS) expect(screen.getByTestId(`bench-labels-${id}`)).toHaveTextContent(id === "xgboost" ? "Yes" : "No");
  });

  it("gives the bar chart a title, a text summary and a data table", () => {
    render(<BenchmarkPage />);
    const chart = screen.getByRole("img", { name: /Capped value per investigator-hour, N = 25/ });
    expect(chart.querySelector("title")).toHaveTextContent("Capped value per investigator-hour, N = 25");
    expect(chart.querySelector("desc")?.textContent).toMatch(/highest first: XGBoost \(supervised\)/);
    const table = screen.getByTestId("bench-chart-table");
    expect(table.closest("details")).not.toBeNull();
    expect(within(table).getAllByRole("row")).toHaveLength(SYSTEM_IDS.length + 1);
  });

  it("shows the random PR-AUC baseline from the row counts", () => {
    render(<BenchmarkPage />);
    expect(screen.getByTestId("bench-random-pr")).toHaveTextContent((SUMMARY.rows.test_laundering / SUMMARY.rows.test).toFixed(5));
  });

  // Two axe passes over a large page: allow for a busy machine in the full parallel run.
  it("has no axe violations at every capacity", { timeout: 30_000 }, async () => {
    const { container } = render(<BenchmarkPage />);
    const user = userEvent.setup();
    for (const name of ["N = 25", "N = 100"]) {
      await user.click(screen.getByRole("radio", { name }));
      const r = await axe(container, AXE_OPTIONS);
      expect(r.violations.map((v) => `${v.id}: ${v.help}`)).toEqual([]);
    }
  });
});
