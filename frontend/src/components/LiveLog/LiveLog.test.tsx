import { act, render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it } from "vitest";
import { axe } from "vitest-axe";
import { dashboard } from "../../store/store";
import { caseMsg, caseSummary, feedItem, routeFetch } from "../../test/fixtures";
import { LiveLog } from "./LiveLog";

const AXE_OPTIONS = { rules: { "color-contrast": { enabled: false } } };

/** Let the first REST poll settle inside act(). */
const settle = () => act(() => new Promise<void>((r) => setTimeout(r, 0)));

const lines = () => within(screen.getByRole("log", { name: "Live log entries" })).queryAllByRole("listitem");

describe("LiveLog", () => {
  it("is a polite log seeded from the feed that follows WebSocket events and Cases", async () => {
    routeFetch({ "/immune": { recent_antibodies: [] }, "/soc/playbook-actions": { actions: [], count: 0 } });
    dashboard.getState().applyEvents([feedItem(1)]);
    render(<LiveLog />);
    await settle();
    const log = screen.getByRole("log", { name: "Live log entries" });
    expect(log).toHaveAttribute("aria-live", "polite");
    expect(lines()).toHaveLength(1);
    expect(lines()[0]).toHaveTextContent("INGESTED T-1: A-1 → A-2 949.00 GBP");

    act(() => {
      dashboard.getState().applyEvents([feedItem(2)]);
      dashboard.getState().applyCaseMessage(caseMsg(caseSummary("C-1")));
    });
    expect(lines().map((l) => l.textContent)).toEqual([
      expect.stringContaining("INGESTED T-1"),
      expect.stringContaining("INGESTED T-2"),
      expect.stringContaining("CASE OPENED C-1: HIGH"), // updated_at is 60 ms after the events
    ]);
  });

  it("holds new lines while paused and shows them on resume", async () => {
    routeFetch({});
    render(<LiveLog />);
    await settle();
    const user = userEvent.setup();
    const pause = screen.getByRole("button", { name: "Pause" });
    await user.click(pause);
    expect(screen.getByRole("button", { name: "Resume" })).toHaveAttribute("aria-pressed", "true");
    act(() => dashboard.getState().applyEvents([feedItem(1), feedItem(2)]));
    expect(lines()).toHaveLength(0);
    expect(screen.getByTestId("livelog-meta")).toHaveTextContent("Paused: 2 new entries held.");
    await user.click(screen.getByRole("button", { name: "Resume" }));
    expect(lines()).toHaveLength(2);
  });

  it("keeps at most cap lines, dropping the oldest", async () => {
    routeFetch({});
    render(<LiveLog cap={3} />);
    await settle();
    act(() => dashboard.getState().applyEvents([1, 2, 3, 4, 5].map((i) => feedItem(i))));
    expect(lines().map((l) => l.textContent?.match(/T-\d/)?.[0])).toEqual(["T-3", "T-4", "T-5"]);
    expect(screen.getByTestId("livelog-meta")).toHaveTextContent("3 lines (newest 3 kept)");
  });

  it("adds Antibodies from the REST API and notes features that are not enabled", async () => {
    routeFetch({
      "/immune": { recent_antibodies: [{ ab_id: "AB-1", created_at: "2025-03-14T10:12:00+00:00", origin: "miss:T-9" }] },
    });
    render(<LiveLog />);
    expect(await screen.findByText("AB-1 created from miss:T-9")).toBeInTheDocument();
    expect(await screen.findByTestId("livelog-unavailable")).toHaveTextContent("SOC playbooks are not enabled");
  });

  it("has no axe violations", async () => {
    routeFetch({});
    dashboard.getState().applyEvents([feedItem(1)]);
    const { container } = render(<LiveLog />);
    await screen.findAllByTestId("livelog-unavailable");
    const results = await axe(container, AXE_OPTIONS);
    expect(results.violations.map((v) => `${v.id}: ${v.help}`)).toEqual([]);
  });
});
