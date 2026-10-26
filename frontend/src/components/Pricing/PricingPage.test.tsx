import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it } from "vitest";
import { axe } from "vitest-axe";
import { TabbedDashboard } from "../Shell/TabbedDashboard";
import type { ShellTab } from "../Shell/tabs";
import { PricingPage, annualPerMonth } from "./PricingPage";

// jsdom does no layout, so colour-contrast is checked separately (src/a11y).
const AXE_OPTIONS = { rules: { "color-contrast": { enabled: false } } };

const TABS: ShellTab[] = [
  { id: "workflow", label: "Workflow", View: () => <p>workflow body</p> },
  { id: "pricing", label: "Pricing", View: PricingPage },
];

/** Pricing inside a real shell, so the CTAs switch real tabs. */
function renderPricing() {
  return render(
    <TabbedDashboard tabs={TABS} leadingTabs={[]} initialTab="pricing">
      <p>live grid</p>
    </TabbedDashboard>,
  );
}

const selectedTab = () => screen.getAllByRole("tab").find((t) => t.getAttribute("aria-selected") === "true")?.textContent;
const plan = (id: string) => screen.getByTestId(`plan-${id}`);
const price = (id: string) => screen.getByTestId(`price-${id}`).textContent;
const note = (id: string) => screen.getByTestId(`price-note-${id}`).textContent;

describe("PricingPage", () => {
  it("renders one page title and the four plans, with Growth marked as most popular in text", () => {
    renderPricing();
    expect(screen.getAllByRole("heading", { level: 2 }).map((h) => h.textContent)).toEqual(["Plans for every fraud team"]);
    for (const name of ["Pilot", "Starter", "Growth", "Enterprise"]) {
      expect(screen.getByRole("heading", { level: 3, name })).toBeInTheDocument();
    }
    expect(within(plan("growth")).getByText("Most popular")).toBeInTheDocument();
    expect(within(plan("starter")).queryByText("Most popular")).toBeNull();
    expect(price("pilot")).toBe("$0/ month");
    expect(price("enterprise")).toBe("Custom");
    // Large-bank items that do not exist yet are labelled as roadmap.
    expect(within(plan("enterprise")).getAllByText("Roadmap")).toHaveLength(4);
  });

  it("rounds the 20% annual discount to the nearest 10 USD", () => {
    expect(annualPerMonth(1490)).toBe(1190);
    expect(annualPerMonth(4990)).toBe(3990);
    expect(annualPerMonth(0)).toBe(0);
  });

  it("switches the displayed prices with a click on the billing radio group", async () => {
    renderPricing();
    const user = userEvent.setup();
    const group = screen.getByRole("radiogroup", { name: "Billing period" });
    const monthly = within(group).getByRole("radio", { name: "Monthly" });
    const annual = within(group).getByRole("radio", { name: "Annual" });
    expect(monthly).toHaveAttribute("aria-checked", "true");
    expect(annual).toHaveAttribute("aria-checked", "false");
    expect(price("starter")).toBe("$1,490/ month");
    expect(note("starter")).toBe("Billed monthly");

    await user.click(annual);
    expect(annual).toHaveAttribute("aria-checked", "true");
    expect(monthly).toHaveAttribute("aria-checked", "false");
    expect(price("starter")).toBe("$1,190/ month");
    expect(note("starter")).toBe("$14,280 billed annually");
    expect(price("growth")).toBe("$3,990/ month");
    expect(note("growth")).toBe("$47,880 billed annually");
    expect(screen.getByRole("status")).toHaveTextContent("Showing annual billing");

    await user.click(monthly);
    expect(price("starter")).toBe("$1,490/ month");
  });

  it("moves the billing selection and focus with the arrow keys, keeping one radio in the tab order", async () => {
    renderPricing();
    const user = userEvent.setup();
    const monthly = screen.getByRole("radio", { name: "Monthly" });
    const annual = screen.getByRole("radio", { name: "Annual" });
    expect([monthly.tabIndex, annual.tabIndex]).toEqual([0, -1]);
    monthly.focus();

    await user.keyboard("{ArrowRight}");
    expect(annual).toHaveAttribute("aria-checked", "true");
    expect(document.activeElement).toBe(annual);
    expect([monthly.tabIndex, annual.tabIndex]).toEqual([-1, 0]);
    expect(price("starter")).toBe("$1,190/ month");
    expect(note("starter")).toBe("$14,280 billed annually");

    await user.keyboard("{ArrowLeft}");
    expect(monthly).toHaveAttribute("aria-checked", "true");
    expect(document.activeElement).toBe(monthly);
    expect(price("starter")).toBe("$1,490/ month");
  });

  it("CTAs open the Dashboard (pilot) or the Workflow tab", async () => {
    renderPricing();
    const user = userEvent.setup();
    const back = () => user.click(screen.getByRole("tab", { name: "Pricing" }));

    await user.click(screen.getByRole("button", { name: "Start pilot for Pilot" }));
    expect(selectedTab()).toBe("Dashboard");
    expect(screen.getByText("live grid")).toBeVisible();

    for (const name of ["Book a demo for Starter", "Book a demo for Growth", "Contact sales for Enterprise"]) {
      await back();
      await user.click(screen.getByRole("button", { name }));
      expect(selectedTab()).toBe("Workflow");
      expect(screen.getByText("workflow body")).toBeVisible();
    }
  });

  it("has a captioned comparison table in a focusable, labelled scroll region", () => {
    renderPricing();
    const table = screen.getByRole("table", { name: "Compare plans" });
    expect(table.querySelector("caption")).toHaveTextContent("Compare plans");
    const region = screen.getByRole("region", { name: "Compare plans" });
    expect(region).toHaveAttribute("tabindex", "0");
    expect(region).toContainElement(table);
    expect(within(table).getAllByRole("columnheader").map((h) => h.textContent)).toEqual(["Feature", "Pilot", "Starter", "Growth", "Enterprise"]);
    const soc = within(table).getByRole("rowheader", { name: "SOC layer" }).closest("tr")!;
    expect([...soc.querySelectorAll("td")].map((td) => td.textContent)).toEqual(["—Not included", "—Not included", "Included", "Included"]);
  });

  it("has no axe violations in either billing mode", async () => {
    const { container } = renderPricing();
    const first = await axe(container, AXE_OPTIONS);
    expect(first.violations.map((v) => `${v.id}: ${v.help}`)).toEqual([]);
    await userEvent.setup().click(screen.getByRole("radio", { name: "Annual" }));
    const second = await axe(container, AXE_OPTIONS);
    expect(second.violations.map((v) => `${v.id}: ${v.help}`)).toEqual([]);
  });
});
