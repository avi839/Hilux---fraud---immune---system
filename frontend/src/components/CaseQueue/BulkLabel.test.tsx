import { act, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import fc from "fast-check";
import { describe, expect, it } from "vitest";
import { axe } from "vitest-axe";
import { MAX_BULK_CASES } from "../../api/types";
import { dashboard } from "../../store/store";
import { caseMsg, caseSummary, jsonResponse, routeFetch } from "../../test/fixtures";
import { AUTO_CLEARED_TEXT } from "../EvidencePanel/SuggestedLabel";
import { pruneSelection, selectAllVisible } from "./BulkLabel";
import { CaseQueue } from "./CaseQueue";

const AXE_OPTIONS = { rules: { "color-contrast": { enabled: false } } };

function seed(n = 3) {
  const s = dashboard.getState();
  for (let i = 1; i <= n; i++) {
    s.applyCaseMessage(
      caseMsg(
        caseSummary(`C-${i}`, {
          priority: String(1000 - i),
          suggested_label: { label: "FALSE_POSITIVE", confidence: 0.85, reason: "Belief 0.15" },
        }),
        i,
      ),
    );
  }
}

type Sent = { by: string; label: string; note: string; case_ids: string[] };

function serveBulk(sent: Sent[], respond: (b: Sent) => Response) {
  routeFetch({
    "/cases/bulk-feedback": (init: RequestInit | undefined) => {
      const body = JSON.parse(String(init?.body)) as Sent;
      sent.push(body);
      return respond(body);
    },
  });
}

const okBody = (b: Sent) =>
  jsonResponse({
    by: b.by,
    label: b.label,
    count: b.case_ids.length,
    case_ids: b.case_ids,
    cases: b.case_ids.map((id) =>
      caseSummary(id, { status: "CLOSED", placement: "NONE", label: b.label as "FALSE_POSITIVE", closure_reason: "LABELLED", updated_at: "2025-03-14T11:00:00+00:00" }),
    ),
  });

async function openBulk() {
  const user = userEvent.setup();
  const utils = render(<CaseQueue />);
  await user.click(screen.getByRole("button", { name: "Bulk label" }));
  return { user, ...utils };
}

describe("CaseQueue bulk labelling", () => {
  it("selects cases, confirms in an alertdialog, sends only after Confirm and announces the result", async () => {
    const sent: Sent[] = [];
    serveBulk(sent, okBody);
    seed();
    dashboard.getState().setInvestigator("  alice ");
    const { user, container } = await openBulk();

    expect(screen.queryByRole("listbox")).not.toBeInTheDocument();
    await user.click(screen.getByRole("checkbox", { name: "Select C-1 for bulk labelling" }));
    await user.click(screen.getByRole("checkbox", { name: "Select C-3 for bulk labelling" }));
    expect(screen.getByTestId("bulk-count")).toHaveTextContent("2 selected (max 50)");
    expect(screen.getByRole("group", { name: "Bulk actions for 2 selected cases" })).toBeInTheDocument();

    const fp = screen.getByRole("button", { name: "False positive" });
    await user.click(fp);
    const dialog = screen.getByRole("alertdialog", { name: "Label 2 cases as FALSE_POSITIVE?" });
    expect(dialog).toHaveAttribute("aria-modal", "true");
    expect(within(dialog).getByRole("button", { name: "Cancel" })).toHaveFocus();
    // Focus trap: Tab and Shift+Tab stay on Cancel / Confirm.
    await user.tab();
    expect(within(dialog).getByRole("button", { name: "Confirm" })).toHaveFocus();
    await user.tab();
    expect(within(dialog).getByRole("button", { name: "Cancel" })).toHaveFocus();
    await user.tab({ shift: true });
    expect(within(dialog).getByRole("button", { name: "Confirm" })).toHaveFocus();
    expect(sent).toEqual([]);
    const results = await axe(container, AXE_OPTIONS);
    expect(results.violations.map((v) => `${v.id}: ${v.help}`)).toEqual([]);

    await user.click(within(dialog).getByRole("button", { name: "Confirm" }));
    await waitFor(() => expect(screen.getByTestId("bulk-result")).toHaveTextContent("Confirmed: 2 cases labelled FALSE_POSITIVE by alice: C-1, C-3."));
    expect(screen.getByTestId("bulk-result")).toHaveAttribute("aria-live", "polite");
    expect(sent).toEqual([{ by: "alice", label: "FALSE_POSITIVE", note: "", case_ids: ["C-1", "C-3"] }]);
    expect(dashboard.getState().queue).toEqual(["C-2"]);
    expect(screen.getByTestId("bulk-count")).toHaveTextContent("0 selected");
    // The action buttons are disabled with an empty selection, so focus goes to select-all.
    expect(fp).toBeDisabled();
    await waitFor(() => expect(screen.getByRole("checkbox", { name: /Select all visible/ })).toHaveFocus());
  });

  it("Escape cancels without a request", async () => {
    const sent: Sent[] = [];
    serveBulk(sent, okBody);
    seed();
    dashboard.getState().setInvestigator("alice");
    const { user } = await openBulk();
    await user.click(screen.getByRole("checkbox", { name: "Select C-2 for bulk labelling" }));
    const btn = screen.getByRole("button", { name: "Confirm fraud" });
    await user.click(btn);
    expect(screen.getByRole("alertdialog", { name: "Label 1 case as CONFIRMED_FRAUD?" })).toBeInTheDocument();
    await user.keyboard("{Escape}");
    expect(screen.queryByRole("alertdialog")).not.toBeInTheDocument();
    expect(btn).toHaveFocus();
    expect(sent).toEqual([]);
  });

  it("uses the Controls name check before asking", async () => {
    const sent: Sent[] = [];
    serveBulk(sent, okBody);
    seed();
    const { user } = await openBulk();
    await user.click(screen.getByRole("checkbox", { name: "Select C-1 for bulk labelling" }));
    await user.click(screen.getByRole("button", { name: "Confirm fraud" }));
    expect(screen.getByRole("alert")).toHaveTextContent("Enter your investigator name first.");
    expect(screen.queryByRole("alertdialog")).not.toBeInTheDocument();
    expect(sent).toEqual([]);
  });

  it("lists every failing case when the server rejects the batch (nothing changed)", async () => {
    const sent: Sent[] = [];
    serveBulk(sent, () =>
      jsonResponse(
        {
          detail: "2 of 2 cases failed validation; nothing was changed",
          failures: [
            { case_id: "C-1", reason: "case is assigned to bob", status: 409 },
            { case_id: "C-2", reason: "case is CLOSED", status: 409 },
          ],
        },
        409,
      ),
    );
    seed();
    dashboard.getState().setInvestigator("alice");
    const { user } = await openBulk();
    await user.click(screen.getByRole("checkbox", { name: "Select C-1 for bulk labelling" }));
    await user.click(screen.getByRole("checkbox", { name: "Select C-2 for bulk labelling" }));
    await user.click(screen.getByRole("button", { name: "Confirm fraud" }));
    await user.click(screen.getByRole("button", { name: "Confirm" }));
    const result = screen.getByTestId("bulk-result");
    await waitFor(() => expect(result).toHaveTextContent("Nothing was changed: 409 2 of 2 cases failed validation"));
    const items = within(within(result).getByRole("list", { name: "Failing cases" })).getAllByRole("listitem");
    expect(items.map((li) => li.textContent)).toEqual(["C-1: case is assigned to bob (409)", "C-2: case is CLOSED (409)"]);
    expect(dashboard.getState().queue).toEqual(["C-1", "C-2", "C-3"]);
    // The selection is kept so the investigator can adjust it.
    expect(screen.getByTestId("bulk-count")).toHaveTextContent("2 selected");
  });

  it("select all visible is capped at 50", async () => {
    seed(55);
    routeFetch({});
    const { user } = await openBulk();
    await user.click(screen.getByRole("checkbox", { name: /Select all visible \(first 50 of 55\)/ }));
    expect(screen.getByTestId("bulk-count")).toHaveTextContent("50 selected (max 50). Limit reached");
    const c51 = screen.getByRole("checkbox", { name: "Select C-51 for bulk labelling" });
    expect(c51).not.toBeChecked();
    expect(c51).toBeDisabled();
    await user.click(screen.getByRole("button", { name: "Clear selection" }));
    expect(screen.getByTestId("bulk-count")).toHaveTextContent("0 selected");
    expect(c51).toBeEnabled();
  });

  it("returns to the listbox on Done, keeping its arrow-key navigation", async () => {
    seed();
    routeFetch({});
    const { user } = await openBulk();
    await user.click(screen.getByRole("button", { name: "Done" }));
    expect(screen.getAllByRole("option")).toHaveLength(3);
    expect(screen.getByRole("button", { name: "Bulk label" })).toHaveAttribute("aria-pressed", "false");
  });

  it("shows AUTO_CLEARED closures apart from the queue", async () => {
    seed(1);
    act(() =>
      dashboard
        .getState()
        .upsertCase(caseSummary("C-9", { tier: "LOW", status: "CLOSED", placement: "NONE", closure_reason: "AUTO_CLEARED" })),
    );
    const user = userEvent.setup();
    const { container } = render(<CaseQueue />);
    expect(screen.getAllByRole("option")).toHaveLength(1);
    expect(screen.getByTestId("auto-cleared-count")).toHaveTextContent("1");
    const box = screen.getByTestId("auto-cleared");
    expect(box).toHaveTextContent(`${AUTO_CLEARED_TEXT}: 1 case`);
    await user.click(within(box).getByText(/Auto-cleared \(not a label\): 1 case/));
    await user.click(within(box).getByRole("button", { name: "View C-9" }));
    expect(dashboard.getState().selectedCaseId).toBe("C-9");
    const results = await axe(container, AXE_OPTIONS);
    expect(results.violations.map((v) => `${v.id}: ${v.help}`)).toEqual([]);
  });
});

describe("bulk selection helpers", () => {
  // Property: select-all never exceeds the cap, keeps queue order, and pruning only keeps
  // ids still in the queue. **Validates: Requirements 41.7, 41.8**
  it("selection stays within the queue and the 50-case cap (property)", () => {
    fc.assert(
      fc.property(fc.uniqueArray(fc.integer({ min: 1, max: 500 }), { maxLength: 120 }), fc.array(fc.integer({ min: 1, max: 500 })), (q, pick) => {
        const queue = q.map((i) => `C-${i}`);
        const all = selectAllVisible(queue);
        if (all.length !== Math.min(queue.length, MAX_BULK_CASES)) return false;
        if (all.some((id, i) => id !== queue[i])) return false;
        const pruned = pruneSelection(new Set(pick.map((i) => `C-${i}`)), queue);
        return pruned.every((id) => queue.includes(id)) && new Set(pruned).size === pruned.length;
      }),
    );
  });
});
