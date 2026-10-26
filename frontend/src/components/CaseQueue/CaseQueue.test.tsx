import { act, render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it } from "vitest";
import { dashboard } from "../../store/store";
import { caseMsg, caseSummary } from "../../test/fixtures";
import { CaseQueue } from "./CaseQueue";

function seed() {
  const s = dashboard.getState();
  s.applyCaseMessage(caseMsg(caseSummary("C-1", { tier: "CRITICAL", priority: "900.00", risk: 0.93 }), 1, 5));
  s.applyCaseMessage(caseMsg(caseSummary("C-2", { tier: "MEDIUM", priority: "500.00", uncertainty: null }), 2, 5));
  s.applyCaseMessage(caseMsg(caseSummary("C-3", { tier: "LOW", priority: "100.00" }), 3, 37));
}

describe("CaseQueue", () => {
  it("shows tier as text, risk, uncertainty, accounts, amount at risk and backlog count", () => {
    seed();
    render(<CaseQueue />);
    const options = screen.getAllByRole("option");
    expect(options.map((o) => within(o).getByText(/^C-\d$/).textContent)).toEqual(["C-1", "C-2", "C-3"]);
    expect(within(options[0]!).getByText("CRITICAL")).toBeInTheDocument();
    expect(within(options[1]!).getByText("MEDIUM")).toBeInTheDocument();
    expect(within(options[2]!).getByText("LOW")).toBeInTheDocument();
    expect(options[0]).toHaveTextContent("Risk 0.93");
    expect(options[0]).toHaveTextContent("U 0.21");
    expect(options[1]).toHaveTextContent("U n/a");
    expect(options[0]).toHaveTextContent("A-1042, A-7781, A-3310");
    expect(options[0]).toHaveTextContent("GBP 2,696.80");
    expect(screen.getByTestId("backlog-count")).toHaveTextContent("37");
  });

  it("is keyboard navigable: Tab enters, arrows move, Enter selects", async () => {
    seed();
    const user = userEvent.setup();
    render(<CaseQueue />);
    const [first, second, third] = screen.getAllByRole("option");

    await user.tab();
    expect(first).toHaveFocus();
    // Roving tabindex: exactly one option is in the tab order.
    expect(screen.getAllByRole("option").filter((o) => o.tabIndex === 0)).toHaveLength(1);

    await user.keyboard("{ArrowDown}");
    expect(second).toHaveFocus();
    await user.keyboard("{ArrowDown}{ArrowDown}"); // clamps at the end
    expect(third).toHaveFocus();
    await user.keyboard("{Home}");
    expect(first).toHaveFocus();
    await user.keyboard("{End}{ArrowUp}");
    expect(second).toHaveFocus();

    await user.keyboard("{Enter}");
    expect(dashboard.getState().selectedCaseId).toBe("C-2");
    expect(second).toHaveAttribute("aria-selected", "true");
    expect(first).toHaveAttribute("aria-selected", "false");

    await user.keyboard("{ArrowUp} ");
    expect(dashboard.getState().selectedCaseId).toBe("C-1");
  });

  it("selects on click and shows an empty state", async () => {
    const user = userEvent.setup();
    const { rerender } = render(<CaseQueue />);
    expect(screen.getByText("No active cases.")).toBeInTheDocument();
    act(() => seed());
    rerender(<CaseQueue />);
    await user.click(screen.getAllByRole("option")[2]!);
    expect(dashboard.getState().selectedCaseId).toBe("C-3");
  });
});
