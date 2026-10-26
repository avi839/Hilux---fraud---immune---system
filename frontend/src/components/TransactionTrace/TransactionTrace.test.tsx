import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it } from "vitest";
import { axe } from "vitest-axe";
import { dashboard } from "../../store/store";
import { feedItem, jsonResponse, routeFetch } from "../../test/fixtures";
import { TransactionTrace } from "./TransactionTrace";
import { STAGE_ORDER, type TraceDoc } from "./traceApi";

const AXE_OPTIONS = { rules: { "color-contrast": { enabled: false } } };

// Same keys as get_transaction_trace in helix/api/app.py (pipeline source).
const TRACE: TraceDoc = {
  tx_id: "T-1",
  timestamp: "2025-03-14T10:22:05.120+00:00",
  source: "pipeline",
  event: { src: "A-1", dst: "A-2", amount: "949.00", currency: "GBP", channel: "card" },
  stages: {
    SENSE: { received_at: "2025-03-14T10:22:05.200+00:00", lateness: { kind: "in_order", seconds: 0 } },
    PERCEIVE: {
      sensor_results: [
        { name: "amount", outcome: "SUCCESS", mass: { fraud: 0.6, legit: 0.1, uncertainty: 0.3 }, features: { amount_z: 4.2 } },
        { name: "geo", outcome: "SUCCESS", mass: { fraud: 0, legit: 0, uncertainty: 1 }, features: {}, vacuous_reason: "missing:lat,lon" },
      ],
      stage1_assessment: { belief: 0.55, plausibility: 0.8, uncertainty: 0.25, conflict: 0.1, high_conflict: false },
    },
    REMEMBER: { dna_comparison: { amount: { amount_z: 4.2 } }, change_point: { kind: "ABRUPT_BREAK", short_run_mass: 0.7 } },
    REASON: {
      motifs: [{ contributor_id: "motif:MULE_CHAIN", mass: { fraud: 0.5, legit: 0, uncertainty: 0.5 }, weight: 1 }],
      truncated_motif_types: [],
      stage2_assessment: { belief: 0.7, plausibility: 0.9, uncertainty: 0.2, conflict: 0.12, high_conflict: true },
    },
    ACT: { risk_score: 0.8, severity_tier: "HIGH", case_id: "C-7" },
    LEARN: { label: null, note: "Feedback feeds the learner." },
  },
};

describe("TransactionTrace", () => {
  it("traces through the API client on Enter and renders every stage in pipeline order", async () => {
    const fetch = routeFetch({ "/trace/T-1": TRACE });
    render(<TransactionTrace />);
    const user = userEvent.setup();
    await user.type(screen.getByLabelText("Transaction ID"), "T-1{Enter}");

    const result = await screen.findByRole("article", { name: "Trace of T-1" });
    expect(fetch).toHaveBeenCalledTimes(1);
    const [url, init] = fetch.mock.calls[0]!;
    expect(String(url)).toBe("/api/v1/trace/T-1");
    expect(init?.signal).toBeInstanceOf(AbortSignal); // request() deadline/cancellation

    const stages = within(result).getAllByTestId(/^trace-stage-/);
    expect(stages.map((s) => s.dataset.testid)).toEqual(STAGE_ORDER.map((n) => `trace-stage-${n}`));
    const perceive = screen.getByRole("region", { name: "PERCEIVE" });
    expect(within(perceive).getByRole("rowheader", { name: "amount" })).toBeInTheDocument();
    expect(within(perceive).getByText("vacuous: missing:lat,lon")).toBeInTheDocument();
    expect(screen.getByRole("region", { name: "REMEMBER" })).toHaveTextContent("amount_z = 4.2000");
    expect(screen.getByRole("region", { name: "REASON" })).toHaveTextContent("motif:MULE_CHAIN");
    expect(screen.getByRole("region", { name: "REASON" })).toHaveTextContent("(high conflict)");
    const act = screen.getByRole("region", { name: "ACT" });
    expect(act).toHaveTextContent("0.8000");
    expect(within(act).getByText("HIGH")).toHaveClass("tier--high");

    await user.click(within(act).getByRole("button", { name: "Open C-7 in the Live view" }));
    expect(dashboard.getState().selectedCaseId).toBe("C-7");
  });

  it("picks a recent transaction from the live feed", async () => {
    routeFetch({ "/trace/T-3": { ...TRACE, tx_id: "T-3" } });
    dashboard.getState().applyEvents([feedItem(3)]);
    render(<TransactionTrace />);
    await userEvent.setup().click(screen.getByRole("button", { name: "T-3" }));
    expect(await screen.findByRole("article", { name: "Trace of T-3" })).toBeInTheDocument();
    expect(screen.getByLabelText("Transaction ID")).toHaveValue("T-3");
  });

  it("reports an unknown transaction with the server's message", async () => {
    routeFetch({ "/trace/NOPE": () => jsonResponse({ detail: "unknown Transaction_Event 'NOPE'" }, 404) });
    render(<TransactionTrace />);
    await userEvent.setup().type(screen.getByLabelText("Transaction ID"), "NOPE{Enter}");
    expect(await screen.findByTestId("trace-error")).toHaveTextContent("Transaction NOPE not found: unknown Transaction_Event 'NOPE'");
    expect(screen.queryByRole("article")).toBeNull();
  });

  it("says the feature is unavailable when the server has no trace route", async () => {
    routeFetch({});
    render(<TransactionTrace />);
    await userEvent.setup().type(screen.getByLabelText("Transaction ID"), "T-1{Enter}");
    expect(await screen.findByTestId("trace-disabled")).toHaveTextContent("not available on this server");
  });

  it("asks for an id instead of calling the server with an empty one", async () => {
    const fetch = routeFetch({});
    render(<TransactionTrace />);
    await userEvent.setup().click(screen.getByRole("button", { name: "Trace" }));
    expect(screen.getByRole("alert")).toHaveTextContent("Enter a transaction ID.");
    expect(fetch).not.toHaveBeenCalled();
  });

  it("has no axe violations with a rendered trace", async () => {
    routeFetch({ "/trace/T-1": TRACE });
    dashboard.getState().applyEvents([feedItem(1)]);
    const { container } = render(<TransactionTrace />);
    await userEvent.setup().type(screen.getByLabelText("Transaction ID"), "T-1{Enter}");
    await screen.findByRole("article");
    const results = await axe(container, AXE_OPTIONS);
    expect(results.violations.map((v) => `${v.id}: ${v.help}`)).toEqual([]);
  });
});
