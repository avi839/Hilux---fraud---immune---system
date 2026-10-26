import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";
import { PanelBoundary } from "./ErrorBoundary";

let fail = true;
function Flaky() {
  if (fail) throw new Error("WebGL context lost");
  return <p>recovered</p>;
}

describe("PanelBoundary", () => {
  it("contains a failing view to its own panel and retries it", async () => {
    vi.spyOn(console, "error").mockImplementation(() => undefined); // React logs caught errors
    fail = true;
    render(
      <>
        <PanelBoundary title="Network" area="network">
          <Flaky />
        </PanelBoundary>
        <PanelBoundary title="Live feed" area="feed">
          <p>feed still here</p>
        </PanelBoundary>
      </>,
    );
    expect(screen.getByTestId("panel-error-network")).toHaveTextContent("The network view failed: WebGL context lost");
    expect(screen.getByText("feed still here")).toBeInTheDocument();
    expect(screen.getByRole("heading", { name: "Network" })).toBeInTheDocument();

    fail = false;
    await userEvent.click(screen.getByRole("button", { name: "Retry" }));
    expect(screen.getByText("recovered")).toBeInTheDocument();
  });
});
