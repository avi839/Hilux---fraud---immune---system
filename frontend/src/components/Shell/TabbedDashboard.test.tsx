import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { useState } from "react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { axe } from "vitest-axe";
import { routeFetch } from "../../test/fixtures";
import { TabbedDashboard, useShellTab } from "./TabbedDashboard";
import { LEADING_TABS, LIVE_TAB, SHOWCASE_TABS, type ShellTab } from "./tabs";

const AXE_OPTIONS = { rules: { "color-contrast": { enabled: false } } };

const mounts = { a: 0, b: 0, live: 0 };

function ViewA() {
  mounts.a++;
  return <p>view A body</p>;
}
function ViewB() {
  mounts.b++;
  const { select } = useShellTab();
  return (
    <button type="button" onClick={() => select("live")}>
      back to live
    </button>
  );
}
function LiveGrid() {
  // Counts mounts (not renders): the Dashboard content must never remount on tab switches.
  useState(() => {
    mounts.live++;
    return null;
  });
  return <p>live grid</p>;
}
function Broken(): never {
  throw new Error("boom");
}

const HOME: ShellTab = { id: "home", label: "Home", View: () => <p>home body</p> };
const TABS: ShellTab[] = [
  { id: "a", label: "Alpha", View: ViewA },
  { id: "b", label: "Beta", View: ViewB },
  { id: "c", label: "Gamma", View: () => <p>view C body</p> },
];

function renderShell(tabs: readonly ShellTab[] = TABS, initialTab?: string) {
  return render(
    <TabbedDashboard tabs={tabs} leadingTabs={[HOME]} initialTab={initialTab}>
      <LiveGrid />
    </TabbedDashboard>,
  );
}

const selected = () => screen.getAllByRole("tab").filter((t) => t.getAttribute("aria-selected") === "true");

beforeEach(() => {
  mounts.a = 0;
  mounts.b = 0;
  mounts.live = 0;
  routeFetch({});
});

describe("TabbedDashboard", () => {
  it("exposes a labelled tablist, opens on the first tab and wires tabs to their panels", () => {
    renderShell();
    const list = screen.getByRole("tablist", { name: "Dashboard views" });
    const tabs = within(list).getAllByRole("tab");
    expect(tabs.map((t) => t.textContent)).toEqual(["Home", "Dashboard", "Alpha", "Beta", "Gamma"]);
    for (const t of tabs) {
      const panel = document.getElementById(t.getAttribute("aria-controls")!);
      expect(panel).toHaveAttribute("role", "tabpanel");
      expect(panel).toHaveAttribute("aria-labelledby", t.id);
    }
    expect(selected()).toEqual([tabs[0]]);
    expect(tabs.map((t) => t.tabIndex)).toEqual([0, -1, -1, -1, -1]);
    expect(screen.getByRole("tabpanel", { name: "Home" })).toHaveTextContent("home body");
    // The Dashboard tab keeps its id "live" (Home and Pricing select it by id).
    expect(screen.getByRole("tab", { name: "Dashboard" })).toHaveAttribute("id", `tab-${LIVE_TAB.id}`);
  });

  it("moves selection and focus with Arrow keys (wrapping), Home and End", async () => {
    renderShell();
    const user = userEvent.setup();
    const tabs = screen.getAllByRole("tab");
    tabs[0]!.focus();
    const expectAt = (i: number) => {
      expect(selected()).toEqual([tabs[i]]);
      expect(document.activeElement).toBe(tabs[i]);
      expect(tabs.filter((t) => t.tabIndex === 0)).toEqual([tabs[i]]);
    };
    await user.keyboard("{ArrowRight}");
    expectAt(1);
    await user.keyboard("{ArrowRight}{ArrowRight}{ArrowRight}{ArrowRight}");
    expectAt(0); // wrapped
    await user.keyboard("{ArrowLeft}");
    expectAt(4);
    await user.keyboard("{Home}");
    expectAt(0);
    await user.keyboard("{End}");
    expectAt(4);
  });

  it("keeps the Dashboard mounted while hidden and mounts other views only while selected", async () => {
    renderShell();
    const user = userEvent.setup();
    expect(mounts.live).toBe(1); // mounted from the start, behind Home
    expect(screen.getByText("live grid")).not.toBeVisible();
    expect(mounts.a).toBe(0);
    await user.click(screen.getByRole("tab", { name: "Alpha" }));
    expect(screen.getByText("view A body")).toBeVisible();
    expect(screen.queryByText("home body")).toBeNull();
    await user.click(screen.getByRole("tab", { name: "Beta" }));
    expect(screen.queryByText("view A body")).toBeNull();
    // A view can switch tabs through useShellTab (as the Home and Pricing CTAs do).
    await user.click(screen.getByRole("button", { name: "back to live" }));
    expect(selected()[0]).toHaveTextContent("Dashboard");
    expect(screen.getByText("live grid")).toBeVisible();
    await user.click(screen.getByRole("tab", { name: "Home" }));
    await user.click(screen.getByRole("tab", { name: "Dashboard" }));
    expect(mounts.live).toBe(1);
  });

  it("honours initialTab", () => {
    renderShell(TABS, "live");
    expect(selected()[0]).toHaveTextContent("Dashboard");
    expect(screen.getByText("live grid")).toBeVisible();
  });

  it("contains a failing view to its own tab", async () => {
    const spy = vi.spyOn(console, "error").mockImplementation(() => undefined);
    // jsdom also reports the thrown render error as an uncaught window error; swallow it here.
    const swallow = (e: ErrorEvent) => e.preventDefault();
    window.addEventListener("error", swallow);
    renderShell([...TABS, { id: "x", label: "Broken", View: Broken }]);
    const user = userEvent.setup();
    await user.click(screen.getByRole("tab", { name: "Broken" }));
    expect(screen.getByTestId("tab-error-x")).toHaveTextContent("The Broken view failed: boom");
    await user.click(screen.getByRole("tab", { name: "Alpha" }));
    expect(screen.getByText("view A body")).toBeVisible();
    window.removeEventListener("error", swallow);
    spy.mockRestore();
  });

  it("registers every view of Req 38.1 in order, with Home first", () => {
    render(
      <TabbedDashboard initialTab="live">
        <p>live grid</p>
      </TabbedDashboard>,
    );
    expect(LEADING_TABS.map((t) => t.id)).toEqual(["home"]);
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
  });

  it("opens the real SOC console, which reports missing routes instead of failing", async () => {
    renderShell(SHOWCASE_TABS);
    const user = userEvent.setup();
    await user.click(screen.getByRole("tab", { name: "SOC console" }));
    const panel = screen.getByRole("tabpanel", { name: "SOC console" });
    expect(await within(panel).findByTestId("soc-absent-soc-sla")).toHaveTextContent("SOC routes not available on this server");
    expect(within(panel).getByRole("region", { name: "Kill chain" })).toBeInTheDocument();
  });

  it("slides the selected-tab pill to the measured tab, and paints it on the tab until measured", async () => {
    const { container, unmount } = renderShell();
    // jsdom has no layout: before measurement the selected tab draws its own pill.
    expect(container.querySelector(".hx-tabs")).not.toHaveClass("hx-tabs--measured");
    unmount();

    const order = ["home", "live", "a", "b", "c"];
    vi.spyOn(HTMLElement.prototype, "offsetWidth", "get").mockReturnValue(80);
    vi.spyOn(HTMLElement.prototype, "offsetLeft", "get").mockImplementation(function (this: HTMLElement) {
      return Math.max(0, order.indexOf(this.id.replace(/^tab-/, ""))) * 90;
    });
    const view = renderShell();
    const wrap = view.container.querySelector<HTMLElement>(".hx-tabs")!;
    expect(wrap).toHaveClass("hx-tabs--measured");
    expect(wrap.style.getPropertyValue("--pill-x")).toBe("0px");
    expect(wrap.style.getPropertyValue("--pill-w")).toBe("80px");
    await userEvent.setup().click(screen.getByRole("tab", { name: "Beta" }));
    expect(wrap.style.getPropertyValue("--pill-x")).toBe("270px");
    // The pill is decorative.
    expect(wrap.querySelector(".hx-tabs__pill")).toHaveAttribute("aria-hidden", "true");
  });

  it("has no axe violations", async () => {
    const { container } = renderShell();
    await userEvent.setup().click(screen.getByRole("tab", { name: "Alpha" }));
    const results = await axe(container, AXE_OPTIONS);
    expect(results.violations.map((v) => `${v.id}: ${v.help}`)).toEqual([]);
  });
});
