import { act, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it } from "vitest";
import { axe } from "vitest-axe";
import { dashboard } from "../../store/store";
import { routeFetch } from "../../test/fixtures";
import { AppHeader, SkipLink } from "./Header";
import { ShellPanels, ShellProvider } from "./TabbedDashboard";
import type { ShellTab } from "./tabs";

const AXE_OPTIONS = { rules: { "color-contrast": { enabled: false } } };

const HOME: ShellTab = { id: "home", label: "Home", View: () => <p>home body</p> };
const TABS: ShellTab[] = [{ id: "a", label: "Alpha", View: () => <p>alpha body</p> }];

function renderHeader() {
  return render(
    <ShellProvider leadingTabs={[HOME]} tabs={TABS}>
      <SkipLink />
      <AppHeader />
      <ShellPanels>
        <main id="main" tabIndex={-1}>
          <p>live grid</p>
        </main>
      </ShellPanels>
    </ShellProvider>,
  );
}

const selectedTab = () => screen.getAllByRole("tab").find((t) => t.getAttribute("aria-selected") === "true")?.textContent;

beforeEach(() => routeFetch({}));

describe("App header", () => {
  it("shows the wordmark, the view tabs, the connection chip and the primary action", async () => {
    renderHeader();
    const header = screen.getByRole("banner");
    expect(header).toHaveTextContent("HELIX");
    expect(header).toContainElement(screen.getByRole("tablist", { name: "Dashboard views" }));
    expect(header).toContainElement(screen.getByRole("button", { name: "Open dashboard" }));
    expect(screen.getByTestId("connection")).toHaveTextContent("Connecting…");
    // The mark next to the wordmark is decorative.
    expect(screen.getByTestId("helix-logo")).toHaveAttribute("aria-hidden", "true");
    expect(selectedTab()).toBe("Home");
    // Home brings its own h1 (the hero); on every other view the header wordmark is the h1.
    expect(screen.queryByRole("heading", { level: 1 })).toBeNull();
    await userEvent.setup().click(screen.getByRole("tab", { name: "Alpha" }));
    expect(header).toContainElement(screen.getByRole("heading", { level: 1, name: "HELIX" }));
  });

  it("keeps the full disconnected text for assistive tech while showing a compact chip", () => {
    renderHeader();
    act(() => dashboard.getState().setConnection("down"));
    const chip = screen.getByTestId("connection");
    expect(chip).toHaveTextContent("Disconnected · reconnecting, showing last received data");
    expect(chip).toHaveAttribute("title", "Disconnected · reconnecting, showing last received data");
    act(() => dashboard.getState().setConnection("open"));
    expect(chip).toHaveTextContent(/^Live$/);
  });

  it("Open dashboard selects the Dashboard tab and moves focus to the grid", async () => {
    renderHeader();
    const user = userEvent.setup();
    await user.click(screen.getByRole("button", { name: "Open dashboard" }));
    expect(selectedTab()).toBe("Dashboard");
    expect(screen.getByText("live grid")).toBeVisible();
    await act(() => new Promise((r) => setTimeout(r, 0)));
    expect(document.activeElement).toBe(document.getElementById("main"));
  });

  it("the skip link reaches the grid from another tab", async () => {
    renderHeader();
    const user = userEvent.setup();
    await user.tab();
    expect(document.activeElement).toBe(screen.getByRole("link", { name: "Skip to dashboard" }));
    await user.keyboard("{Enter}");
    expect(selectedTab()).toBe("Dashboard");
    await act(() => new Promise((r) => setTimeout(r, 0)));
    expect(document.activeElement).toBe(document.getElementById("main"));
  });

  it("has no axe violations", async () => {
    const { container } = renderHeader();
    act(() => dashboard.getState().setConnection("down"));
    const results = await axe(container, AXE_OPTIONS);
    expect(results.violations.map((v) => `${v.id}: ${v.help}`)).toEqual([]);
  });
});
