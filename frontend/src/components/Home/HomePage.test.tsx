import { act, render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { axe } from "vitest-axe";
import { dashboard } from "../../store/store";
import { caseSummary, feedItem } from "../../test/fixtures";
import { CARDS_SUMMARY } from "./HeroCards";
import { HomePage } from "./HomePage";

const AXE_OPTIONS = { rules: { "color-contrast": { enabled: false } } };

// The page navigates through the shell's context; record the ids it asks for.
const select = vi.hoisted(() => vi.fn());
vi.mock("../Shell/TabbedDashboard", () => ({
  useShellTab: () => ({ active: "home", select, all: [], views: [], leadingCount: 0 }),
}));

beforeEach(() => select.mockClear());

const H2 = [
  "Live now",
  "How HELIX works",
  "One system, every signal",
  "See the whole loop as a live 3D simulation",
  "From pilot to production",
  "Built and tested",
  "Ready to see it catch fraud?",
];

describe("HomePage", () => {
  it("is a region labelled Home with one h1 and every section heading", () => {
    render(<HomePage />);
    const region = screen.getByRole("region", { name: "Home" });
    expect(within(region).getAllByRole("heading", { level: 1 }).map((h) => h.textContent)).toEqual(["HELIX"]);
    expect(within(region).getAllByRole("heading", { level: 2 }).map((h) => h.textContent)).toEqual(H2);
    for (const stage of ["SENSE", "PERCEIVE", "REMEMBER", "REASON", "ACT", "LEARN"]) {
      expect(screen.getByRole("heading", { level: 3, name: stage })).toBeInTheDocument();
    }
    for (const phase of ["Shadow", "Assisted", "Active"]) {
      expect(screen.getByRole("heading", { level: 3, name: phase })).toBeInTheDocument();
    }
    expect(screen.getByRole("heading", { level: 3, name: "Tamper-evident audit ledger" })).toBeInTheDocument();
    expect(screen.getByTestId("home-footer")).toHaveTextContent("runs locally, no authentication");
    expect(screen.getByTestId("home-built-aml")).toHaveTextContent("recall 0.045, precision 0.002");
  });

  it("hides the decorative product cards and gives them a text summary", () => {
    render(<HomePage />);
    const cards = screen.getByTestId("home-cards");
    expect(cards).toHaveAttribute("aria-hidden", "true");
    expect(cards).toHaveTextContent("Case C-1");
    expect(cards).toHaveTextContent("CRITICAL");
    expect(cards).toHaveTextContent("0.973");
    expect(cards).toHaveTextContent("Mule chain detected");
    // The inert "buttons" on the miniature are not real controls.
    expect(within(cards).queryAllByRole("button", { hidden: true })).toHaveLength(0);
    expect(screen.getByText(CARDS_SUMMARY)).toHaveClass("sr-only");
  });

  it("CTAs and feature links select the right tabs", async () => {
    render(<HomePage />);
    const user = userEvent.setup();

    const [heroOpen, finalOpen] = screen.getAllByRole("button", { name: /^Open dashboard/ });
    await user.click(heroOpen!);
    await user.click(finalOpen!);
    for (const b of screen.getAllByRole("button", { name: "Watch it work" })) await user.click(b);
    await user.click(screen.getByRole("button", { name: /^Open Workflow/ }));
    expect(select.mock.calls.map((c) => c[0])).toEqual(["live", "live", "workflow", "workflow", "workflow"]);

    select.mockClear();
    await user.click(screen.getByRole("button", { name: /^View Red vs Blue.* in the Red vs Blue tab$/ }));
    await user.click(screen.getByRole("button", { name: /^View SOC fusion.* in the SOC console tab$/ }));
    await user.click(screen.getByRole("button", { name: /^View Evidence fusion.* in the Trace tab$/ }));
    await user.click(screen.getByRole("button", { name: /^View Ranked cases.* in the Dashboard tab$/ }));
    expect(select.mock.calls.map((c) => c[0])).toEqual(["arena", "soc", "trace", "live"]);
    expect(screen.getAllByRole("button", { name: /^View / })).toHaveLength(9);
  });

  it("live strip shows a neutral state until the API server is connected and reporting", () => {
    render(<HomePage />);
    expect(screen.getByTestId("home-kpi-connection")).toHaveTextContent("Connecting…");
    expect(screen.getByTestId("home-kpi-empty")).toHaveTextContent("Connect the API server to see live numbers.");
    expect(screen.queryByTestId("home-kpi-active")).toBeNull();

    // Disconnected with stale data still shows no numbers.
    act(() => {
      dashboard.getState().setConnection("down");
      dashboard.getState().applyEvents([feedItem(1)]);
    });
    expect(screen.getByTestId("home-kpi-connection")).toHaveTextContent("Disconnected");
    expect(screen.getByTestId("home-kpi-empty")).toHaveTextContent("Connect the API server");

    // Connected but nothing received yet: say so instead of showing zeros.
    act(() => {
      dashboard.getState().reset();
      dashboard.getState().setConnection("open");
    });
    expect(screen.getByTestId("home-kpi-empty")).toHaveTextContent("Connected. Numbers appear as soon as the first events arrive.");
  });

  it("live strip shows the store's real values once connected", () => {
    render(<HomePage />);
    act(() => {
      const s = dashboard.getState();
      s.setConnection("open");
      s.setMetrics({ channel: "metrics", stages: { END_TO_END: { count: 10, mean_ms: 0.8, total: 1234 } } });
      s.replaceCases([caseSummary("C-1"), caseSummary("C-2"), caseSummary("C-3", { placement: "BACKLOG" })], 7);
    });
    expect(screen.getByTestId("home-kpi-connection")).toHaveTextContent("Connected");
    expect(screen.queryByTestId("home-kpi-empty")).toBeNull();
    expect(screen.getByTestId("home-kpi-events")).toHaveTextContent("1,234");
    expect(screen.getByTestId("home-kpi-active")).toHaveTextContent(String(dashboard.getState().queue.length));
    expect(screen.getByTestId("home-kpi-backlog")).toHaveTextContent("7");

    // Metrics without counters: "n/a", never a made-up number.
    act(() => dashboard.getState().setMetrics({ channel: "metrics" }));
    expect(screen.getByTestId("home-kpi-events")).toHaveTextContent("n/a");
  });

  it("shows all content at once without IntersectionObserver (jsdom)", () => {
    expect(window.IntersectionObserver).toBeUndefined();
    const { container } = render(<HomePage />);
    const reveals = container.querySelectorAll(".hx-reveal");
    expect(reveals.length).toBeGreaterThan(10);
    reveals.forEach((el) => expect(el).toHaveClass("is-in"));
  });

  it("shows all content at once under prefers-reduced-motion, and never observes", () => {
    const observe = vi.fn();
    vi.stubGlobal(
      "IntersectionObserver",
      vi.fn(() => ({ observe, disconnect: vi.fn(), unobserve: vi.fn() })),
    );
    vi.stubGlobal(
      "matchMedia",
      vi.fn((q: string) => ({ matches: q.includes("reduce"), media: q, addEventListener: vi.fn(), removeEventListener: vi.fn() })),
    );
    const { container } = render(<HomePage />);
    container.querySelectorAll(".hx-reveal").forEach((el) => expect(el).toHaveClass("is-in"));
    expect(observe).not.toHaveBeenCalled();
    expect(screen.getByTestId("home-cards").dataset.parallax).toBeUndefined();
  });

  it("reveals sections as they scroll into view when motion is allowed", () => {
    const callbacks: ((entries: { isIntersecting: boolean }[]) => void)[] = [];
    vi.stubGlobal(
      "IntersectionObserver",
      vi.fn((cb: (entries: { isIntersecting: boolean }[]) => void) => {
        callbacks.push(cb);
        return { observe: vi.fn(), disconnect: vi.fn(), unobserve: vi.fn() };
      }),
    );
    const { container } = render(<HomePage />);
    const reveals = container.querySelectorAll(".hx-reveal");
    expect(reveals.length).toBeGreaterThan(10);
    reveals.forEach((el) => expect(el).not.toHaveClass("is-in"));
    act(() => callbacks.forEach((cb) => cb([{ isIntersecting: true }])));
    reveals.forEach((el) => expect(el).toHaveClass("is-in"));
  });

  it("has no axe violations", async () => {
    const { container } = render(<HomePage />);
    const results = await axe(container, AXE_OPTIONS);
    expect(results.violations.map((v) => `${v.id}: ${v.help}`)).toEqual([]);
  });
});
