import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it } from "vitest";
import { dashboard } from "../../store/store";
import { caseMsg, caseSummary, jsonResponse, routeFetch } from "../../test/fixtures";
import { DemoScenario } from "./DemoScenario";

/** Stand-in for POST /events with the server's observable behaviour on this file: field
 *  validation (amount >= 0.01, ISO currency code) answers 422, a repeated tx_id is a duplicate. */
function ingestServer() {
  const seen = new Set<string>();
  const bodies: Array<Record<string, unknown>> = [];
  const fetchFn = routeFetch({
    "/events": (init: RequestInit | undefined) => {
      const doc = JSON.parse(String(init?.body)) as Record<string, string>;
      bodies.push(doc);
      const errors = [];
      if (!(Number(doc.amount) >= 0.01)) errors.push({ field: "amount", reason: "Input should be greater than or equal to 0.01" });
      if (!/^[A-Z]{3}$/.test(doc.currency ?? "")) errors.push({ field: "currency", reason: "String should match pattern" });
      if (errors.length) return jsonResponse({ detail: "invalid Transaction_Event", errors }, 422);
      const dup = seen.has(doc.tx_id!);
      seen.add(doc.tx_id!);
      return jsonResponse({ tx_id: doc.tx_id, status: dup ? "duplicate" : "accepted", duplicate: dup, backpressure: false }, 202);
    },
    "/cases": { items: [], page_size: 100, total: 0, next_cursor: null, backlog_count: 0 },
  });
  return { fetchFn, bodies };
}

describe("DemoScenario", () => {
  it("submits every line through POST /events, reports outcomes, and lists and selects the Cases the backend opens", async () => {
    const { fetchFn, bodies } = ingestServer();
    render(<DemoScenario />);
    await userEvent.click(screen.getByRole("button", { name: "Load demo scenario" }));

    const result = await screen.findByTestId("demo-result");
    expect(result).toHaveTextContent("13 accepted, 1 duplicate, 1 rejected by validation");
    expect(screen.getByText(/Line 9 \(T009-D\w{4}\): amount: Input should be greater than or equal to 0.01; currency/)).toBeInTheDocument();
    const posts = fetchFn.mock.calls.filter(([, init]) => init?.method === "POST");
    expect(posts).toHaveLength(15);
    expect(posts.every(([u]) => String(u) === "/api/v1/events")).toBe(true);

    // The backend opens Cases for this run's accounts; they arrive on /ws/cases.
    const tag = String(bodies[0]!.tx_id).replace(/^T001/, "");
    dashboard.getState().applyCaseMessage(caseMsg(caseSummary("C-261", { accounts: [`ACC-BOB${tag}`], tier: "HIGH" })));
    dashboard.getState().applyCaseMessage(caseMsg(caseSummary("C-260", { accounts: [`ACC-ALICE${tag}`], tier: "CRITICAL", placement: "BACKLOG" })));

    expect(await screen.findByRole("button", { name: "Show C-260" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Show C-261" })).toBeInTheDocument();
    expect(screen.getByText(/backlog: over the alert budget/)).toBeInTheDocument();
    await waitFor(() => expect(dashboard.getState().selectedCaseId).not.toBeNull());

    await userEvent.click(screen.getByRole("button", { name: "Show C-261" }));
    expect(dashboard.getState().selectedCaseId).toBe("C-261");
  });
});
