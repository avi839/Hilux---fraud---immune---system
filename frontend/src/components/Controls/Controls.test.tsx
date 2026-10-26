import { act, fireEvent, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";
import { dashboard } from "../../store/store";
import { caseMsg, caseSummary, jsonResponse } from "../../test/fixtures";
import { Controls } from "./Controls";
import { checkAlertBudget, checkInvestigationCost } from "./validation";

type Handler = (url: string, init: RequestInit) => Promise<Response>;

function stubFetch(handler: Handler) {
  const fn = vi.fn(handler);
  vi.stubGlobal("fetch", fn);
  return fn;
}

function calls(fn: ReturnType<typeof stubFetch>) {
  return fn.mock.calls.map(([url, init]) => ({
    url,
    method: init?.method,
    body: init?.body ? (JSON.parse(String(init.body)) as unknown) : undefined,
  }));
}

function confirmBudget(n: number) {
  dashboard.getState().setConfirmed({ alertBudget: n, investigationCost: 50, generatorRunning: false });
}

describe("validation", () => {
  it("alert budget: integers 1-1000 only", () => {
    expect(checkAlertBudget("1")).toEqual({ ok: true, value: 1 });
    expect(checkAlertBudget(" 1000 ")).toEqual({ ok: true, value: 1000 });
    for (const bad of ["0", "1001", "12.5", "-3", "", "abc", "1e2"]) expect(checkAlertBudget(bad).ok).toBe(false);
  });

  it("investigation cost: 0-1e9", () => {
    expect(checkInvestigationCost("0")).toEqual({ ok: true, value: 0 });
    expect(checkInvestigationCost("1000000000")).toEqual({ ok: true, value: 1e9 });
    expect(checkInvestigationCost("12.75")).toEqual({ ok: true, value: 12.75 });
    for (const bad of ["-1", "1000000000.01", "", "x", "Infinity"]) expect(checkInvestigationCost(bad).ok).toBe(false);
  });
});

describe("Controls", () => {
  it("rejects an out-of-range budget before any request and keeps the confirmed value", async () => {
    const fetchFn = stubFetch(async () => jsonResponse({}));
    confirmBudget(20);
    const user = userEvent.setup();
    render(<Controls />);
    await user.type(screen.getByLabelText("Alert budget"), "1001");
    await user.click(screen.getAllByRole("button", { name: "Apply" })[0]!);
    expect(screen.getByRole("alert")).toHaveTextContent("from 1 to 1,000");
    expect(fetchFn).not.toHaveBeenCalled();
    expect(screen.getByTestId("confirmed-alertBudget")).toHaveTextContent("20");
  });

  it("rejects an out-of-range investigation cost before any request", async () => {
    const fetchFn = stubFetch(async () => jsonResponse({}));
    confirmBudget(20);
    const user = userEvent.setup();
    render(<Controls />);
    await user.type(screen.getByLabelText("Investigation cost"), "-5");
    await user.click(screen.getAllByRole("button", { name: "Apply" })[1]!);
    expect(screen.getByRole("alert")).toHaveTextContent("from 0 to 1,000,000,000");
    expect(fetchFn).not.toHaveBeenCalled();
    expect(screen.getByTestId("confirmed-investigationCost")).toHaveTextContent("50");
  });

  it("shows the server-confirmed budget after PATCH /config", async () => {
    const fetchFn = stubFetch(async () =>
      jsonResponse({ accepted: { "prioritiser.alert_budget": 30 }, applies_from_period: 4, pending: { "prioritiser.alert_budget": 30 } }),
    );
    confirmBudget(20);
    const user = userEvent.setup();
    render(<Controls />);
    await user.type(screen.getByLabelText("Alert budget"), "30");
    await user.click(screen.getAllByRole("button", { name: "Apply" })[0]!);
    expect(await screen.findByRole("status")).toHaveTextContent("applies from budget period 4");
    expect(screen.getByTestId("confirmed-alertBudget")).toHaveTextContent("30");
    expect(calls(fetchFn)).toEqual([{ url: "/api/v1/config", method: "PATCH", body: { "prioritiser.alert_budget": 30 } }]);
  });

  it("shows the server's error and keeps the confirmed value when the server rejects", async () => {
    stubFetch(async () =>
      jsonResponse(
        {
          detail: "validation failed",
          errors: [
            {
              field: "prioritiser.alert_budget",
              reason: "must be in [1, 1000]",
              rule: "out_of_range",
              permitted: { lo: 1, hi: 1000, lo_open: false, rule: "must be in [1, 1000]" },
            },
          ],
        },
        422,
      ),
    );
    confirmBudget(20);
    const user = userEvent.setup();
    render(<Controls />);
    await user.type(screen.getByLabelText("Alert budget"), "25");
    await user.click(screen.getAllByRole("button", { name: "Apply" })[0]!);
    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent("422");
    expect(alert).toHaveTextContent("permitted: must be in [1, 1000]");
    expect(screen.getByTestId("confirmed-alertBudget")).toHaveTextContent("20");
  });

  it("shows an error when no confirmation arrives within 5 s and keeps the confirmed state", async () => {
    vi.useFakeTimers();
    stubFetch(
      (_url, init) =>
        new Promise<Response>((_resolve, reject) => {
          init.signal?.addEventListener("abort", () => reject(new DOMException("aborted", "AbortError")));
        }),
    );
    confirmBudget(20);
    render(<Controls />);
    fireEvent.click(screen.getByRole("button", { name: "Start generator" }));
    expect(screen.getByText("Waiting for confirmation…")).toBeInTheDocument();

    await act(async () => {
      await vi.advanceTimersByTimeAsync(4_999);
    });
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
    await act(async () => {
      await vi.advanceTimersByTimeAsync(1);
    });
    expect(screen.getByRole("alert")).toHaveTextContent("No confirmation from the server within 5 s");
    expect(screen.getByTestId("confirmed-generator")).toHaveTextContent("stopped");
  });

  it("feedback opens the case with the investigator name, then labels it", async () => {
    const detail = (status: string, extra = {}) => ({
      ...caseSummary("C-5"),
      status,
      assignee: "alice",
      seq_no: 5,
      tx_ids: [],
      created_at: "2025-03-14T10:00:00+00:00",
      history: [],
      updated_at: status === "CLOSED" ? "2025-03-14T10:40:00+00:00" : "2025-03-14T10:30:00+00:00",
      ...extra,
    });
    const fetchFn = stubFetch(async (url) =>
      jsonResponse(url.endsWith("/open") ? detail("IN_REVIEW") : detail("CLOSED", { label: "CONFIRMED_FRAUD", placement: "NONE" })),
    );
    act(() => {
      dashboard.getState().applyCaseMessage(caseMsg(caseSummary("C-5")));
      dashboard.getState().selectCase("C-5");
    });
    const user = userEvent.setup();
    render(<Controls />);

    // Without a name nothing is sent.
    await user.click(screen.getByRole("button", { name: "Confirm fraud" }));
    expect(screen.getByRole("alert")).toHaveTextContent("investigator name");
    expect(fetchFn).not.toHaveBeenCalled();

    await user.type(screen.getByLabelText("Investigator name"), "alice");
    await user.click(screen.getByRole("button", { name: "Confirm fraud" }));
    expect(await screen.findByRole("status")).toHaveTextContent("C-5 is CLOSED with label CONFIRMED_FRAUD");
    expect(calls(fetchFn)).toEqual([
      { url: "/api/v1/cases/C-5/open", method: "POST", body: { investigator: "alice" } },
      { url: "/api/v1/cases/C-5/feedback", method: "POST", body: { by: "alice", label: "CONFIRMED_FRAUD", note: "" } },
    ]);
    expect(dashboard.getState().cases["C-5"]?.status).toBe("CLOSED");
    expect(dashboard.getState().queue).toEqual([]);
  });

  it("escalate reports a 409 conflict and leaves the case unchanged", async () => {
    stubFetch(async () => jsonResponse({ detail: "case is CLOSED and cannot be escalated", case_id: "C-5" }, 409));
    act(() => {
      dashboard.getState().applyCaseMessage(caseMsg(caseSummary("C-5", { tier: "HIGH" })));
      dashboard.getState().selectCase("C-5");
    });
    const user = userEvent.setup();
    render(<Controls />);
    await user.type(screen.getByLabelText("Investigator name"), "bob");
    await user.click(screen.getByRole("button", { name: "Escalate" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("409");
    expect(dashboard.getState().cases["C-5"]?.tier).toBe("HIGH");
  });

  it("disables case actions until a case is selected", () => {
    render(<Controls />);
    for (const name of ["Confirm fraud", "False positive", "Escalate"]) {
      expect(screen.getByRole("button", { name })).toBeDisabled();
    }
  });
});
