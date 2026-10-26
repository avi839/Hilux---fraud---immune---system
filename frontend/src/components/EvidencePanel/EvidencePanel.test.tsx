import { act, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it } from "vitest";
import type { EvidenceDoc } from "../../api/types";
import { dashboard } from "../../store/store";
import { deferredResponse, jsonResponse, routeFetch } from "../../test/fixtures";
import { aliceDetail, aliceEvidence, bobDetail, bobEvidence } from "../../test/sampleRun";
import { EvidencePanel } from "./EvidencePanel";

function seedCases() {
  const s = dashboard.getState();
  s.upsertCase(aliceDetail);
  s.upsertCase(bobDetail);
}

const routes = {
  "/cases/C-1": aliceDetail,
  "/cases/C-1/evidence": aliceEvidence,
  "/cases/C-2": bobDetail,
  "/cases/C-2/evidence": bobEvidence,
};

/** Segment widths (percent) of one stacked bar. */
function widths(bar: HTMLElement): { f: number; l: number; t: number } {
  const w = (seg: string) => parseFloat((bar.querySelector(`[data-seg="${seg}"]`) as HTMLElement).style.width);
  return { f: w("f"), l: w("l"), t: w("t") };
}

describe("EvidencePanel", () => {
  it("shows Alice's T007 explanation, chain, bars and history within 2 s of selection", async () => {
    routeFetch(routes);
    seedCases();
    render(<EvidencePanel />);
    expect(screen.getByText(/select a case/i)).toBeInTheDocument();

    const t0 = performance.now();
    act(() => dashboard.getState().selectCase("C-1"));
    const summary = await screen.findByTestId("ev-summary");
    expect(performance.now() - t0).toBeLessThan(2_000);

    expect(summary).toHaveTextContent("CRITICAL | risk 0.973 | Bel 0.949 Pl 0.998 U 0.049 K 0.039 | at risk 4800.00 EUR");
    expect(screen.queryByTestId("ev-message")).not.toBeInTheDocument();

    // Reasons: numbered, in the engine's order, each with comparison, weight, F'/L' and tx ids.
    const reasons = screen.getByRole("heading", { name: /^Reasons/ }).nextElementSibling as HTMLElement;
    expect(reasons.tagName).toBe("OL");
    const items = within(reasons).getAllByRole("listitem");
    expect(items.map((li) => li.querySelector(".ev-item__label")?.textContent)).toEqual(["Amount", "Geo", "Counterparty", "Timing"]);
    expect(items[0]).toHaveTextContent("Amount: amount 4800.00 vs baseline mean 37.69 (sd 13.74, z 346.64)");
    expect(items[0]).toHaveTextContent("weight 1.00 · F′ 0.700 vs L′ 0.000 · tx T007");
    expect(items[1]).toHaveTextContent("5196.1 km in 1.17 h = 4454 km/h vs plausible 900 km/h");

    const mitigating = screen.getByRole("heading", { name: /^Mitigating/ }).nextElementSibling as HTMLElement;
    expect(within(mitigating).getAllByRole("listitem")).toHaveLength(1);
    expect(mitigating).toHaveTextContent("Velocity: events 1h 2 vs expected 1.00");
    expect(mitigating).toHaveTextContent("F′ 0.000 vs L′ 0.041");
    const insufficient = screen.getByRole("heading", { name: /^Insufficient evidence/ }).nextElementSibling as HTMLElement;
    expect(insufficient).toHaveTextContent("Drift: no decisive evidence (F' = L')");

    const records = screen.getByRole("heading", { name: /^Records/ }).nextElementSibling as HTMLElement;
    expect(records).toHaveTextContent("T007: change point ABRUPT_BREAK");

    // Uncertainty to 2 dp; T007 had every field and enough history.
    expect(screen.getByTestId("ev-uncertainty")).toHaveTextContent("0.05");
    expect(screen.getByTestId("ev-conditions-none")).toHaveTextContent("none contributed");

    // One bar per contributor (6 sensors), each spanning exactly 100%.
    const bars = screen.getAllByRole("img");
    expect(bars).toHaveLength(6);
    for (const b of bars) {
      const { f, l, t } = widths(b);
      expect(f + l + t).toBeCloseTo(100, 9);
    }
    const amount = screen.getByTestId("bar-amount");
    const amountBar = within(amount).getByRole("img");
    expect(amountBar).toHaveAccessibleName("Amount, tx T007: fraud 0.700, legit 0.000, uncertainty 0.300");
    expect(widths(amountBar).f).toBeCloseTo(70, 9);
    expect(widths(amountBar).t).toBeCloseTo(30, 9);
    expect(within(screen.getByTestId("bar-velocity")).getByRole("img")).toHaveAccessibleName(/legit 0\.041, uncertainty 0\.959/);

    // Collapsible chain and history.
    const user = userEvent.setup();
    const chainToggle = screen.getByText("Evidence chain (8 entries)");
    await user.click(chainToggle);
    expect(screen.getByRole("region", { name: "Evidence chain entries" })).toHaveTextContent("~fused");
    const history = screen.getByText("Case history (2)");
    expect(history.closest("details")).not.toHaveAttribute("open");
    await user.click(history);
    expect(history.closest("details")).toHaveAttribute("open");
    expect(screen.getByRole("list", { name: /case history/i })).toHaveTextContent("placement NONE → ACTIVE by system");
  });

  it("lists missing-field and insufficient-history conditions with their sensors (Bob, T010-T012)", async () => {
    routeFetch(routes);
    seedCases();
    render(<EvidencePanel />);
    act(() => dashboard.getState().selectCase("C-2"));
    await screen.findByTestId("ev-summary");

    expect(screen.getByTestId("ev-uncertainty")).toHaveTextContent("0.40");
    const list = screen.getByTestId("ev-conditions");
    const rows = within(list).getAllByRole("listitem");
    expect(rows.map((r) => r.querySelector("strong")?.textContent)).toEqual(["Amount", "Counterparty", "Drift", "Geo", "Timing", "Velocity"]);
    const geo = rows[3]!;
    expect(geo).toHaveTextContent("Geo: missing field lat, lon (tx T010, T011, T012)");
    expect(rows[0]).toHaveTextContent(
      "Amount: insufficient history (0 prior events) (tx T010); insufficient history (1 prior events) (tx T011); insufficient history (2 prior events) (tx T012)",
    );

    // The motif bar is the stage-2 contributor; a sensor seen on 3 txs shows its representative.
    const motif = within(screen.getByTestId("bar-STRUCTURING:3e24d8282e37dec5")).getByRole("img");
    expect(motif).toHaveAccessibleName("Structuring, tx T012: fraud 0.600, legit 0.000, uncertainty 0.400");
    expect(widths(motif).f + widths(motif).l + widths(motif).t).toBeCloseTo(100, 9);
    expect(within(screen.getByTestId("bar-amount")).getByRole("img")).toHaveAccessibleName(
      "Amount, tx T010, representative of 3: fraud 0.000, legit 0.000, uncertainty 1.000",
    );
  });

  it("shows loading, then an error with a working Retry", async () => {
    let fail = true;
    const pending = deferredResponse({ detail: "boom" }, 500);
    routeFetch({
      "/cases/C-1": aliceDetail,
      "/cases/C-1/evidence": (init: RequestInit | undefined, url: string) =>
        fail ? pending.route(init, url) : jsonResponse(aliceEvidence),
    });
    seedCases();
    render(<EvidencePanel />);
    act(() => dashboard.getState().selectCase("C-1"));
    expect(screen.getByTestId("ev-loading")).toHaveTextContent("Loading evidence for C-1");
    expect(screen.getByRole("region", { name: "Evidence" })).toHaveAttribute("aria-busy", "true");

    act(() => pending.release());
    const err = await screen.findByTestId("ev-error");
    expect(err).toHaveAttribute("role", "alert");
    expect(err).toHaveTextContent("Could not load evidence for C-1: Server rejected the request (500): boom");

    fail = false;
    await userEvent.setup().click(within(err).getByRole("button", { name: "Retry" }));
    expect(await screen.findByTestId("ev-summary")).toHaveTextContent(/^CRITICAL/);
  });

  it("aborts the stale request when the selection changes", async () => {
    const slow = deferredResponse(aliceEvidence);
    routeFetch({ ...routes, "/cases/C-1/evidence": slow.route });
    seedCases();
    render(<EvidencePanel />);
    act(() => dashboard.getState().selectCase("C-1"));
    expect(screen.getByTestId("ev-loading")).toBeInTheDocument();
    act(() => dashboard.getState().selectCase("C-2"));
    expect(await screen.findByTestId("ev-summary")).toHaveTextContent(/^HIGH \| risk 0\.800/);
    expect(slow.aborted()).toBe(true);
    // A late answer for C-1 changes nothing.
    act(() => slow.release());
    await waitFor(() => expect(screen.getByTestId("ev-summary")).toHaveTextContent(/^HIGH/));
  });

  it('renders the summary with "no contributing evidence" when the chain is absent', async () => {
    const empty: EvidenceDoc = {
      case_id: "C-1",
      chain: null,
      explanation: {
        case_id: "C-1",
        summary: "CRITICAL | risk 0.973 | Bel 0.949 Pl 0.998 U 0.049 K 0.039 | at risk 4800.00 EUR",
        reasons: [],
        mitigating: [],
        insufficient: [],
        notes: [],
        message: "no contributing evidence",
      },
    };
    routeFetch({ "/cases/C-1": aliceDetail, "/cases/C-1/evidence": empty });
    seedCases();
    render(<EvidencePanel />);
    act(() => dashboard.getState().selectCase("C-1"));
    expect(await screen.findByTestId("ev-message")).toHaveTextContent("no contributing evidence");
    expect(screen.getByTestId("ev-summary")).toHaveTextContent(/^CRITICAL/);
    expect(screen.getByTestId("ev-conditions-none")).toBeInTheDocument();
    expect(screen.getByText("No contributors.")).toBeInTheDocument();
    expect(screen.queryAllByRole("img")).toHaveLength(0);
  });

  it("still shows the evidence when only the case detail fails", async () => {
    routeFetch({ "/cases/C-1/evidence": aliceEvidence }); // GET /cases/C-1 → 404
    seedCases();
    render(<EvidencePanel />);
    act(() => dashboard.getState().selectCase("C-1"));
    await screen.findByTestId("ev-summary");
    expect(screen.getByText(/Case detail unavailable/)).toBeInTheDocument();
    // Uncertainty falls back to the queue's Case summary.
    expect(screen.getByTestId("ev-uncertainty")).toHaveTextContent("0.05");
  });
});
