import { act, render, screen } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { SPLASH_DONE_AT, SPLASH_FADE_AT, SPLASH_KEY, Splash } from "./Splash";

function stubReducedMotion(reduce: boolean) {
  vi.stubGlobal(
    "matchMedia",
    vi.fn((query: string) => ({
      matches: reduce && query.includes("prefers-reduced-motion: reduce"),
      media: query,
      onchange: null,
      addEventListener: vi.fn(),
      removeEventListener: vi.fn(),
      addListener: vi.fn(),
      removeListener: vi.fn(),
      dispatchEvent: vi.fn(),
    })),
  );
}

beforeEach(() => {
  window.sessionStorage.clear();
});

describe("Splash", () => {
  it("never appears under Vitest by default", () => {
    render(<Splash />);
    expect(screen.queryByTestId("splash")).toBeNull();
    expect(window.sessionStorage.getItem(SPLASH_KEY)).toBeNull();
  });

  it("is skipped entirely under prefers-reduced-motion", () => {
    stubReducedMotion(true);
    render(<Splash enabled />);
    expect(screen.queryByTestId("splash")).toBeNull();
  });

  it("plays once per session: draws, fades without blocking, then leaves the DOM", () => {
    vi.useFakeTimers();
    stubReducedMotion(false);
    const first = render(<Splash enabled />);
    const overlay = screen.getByTestId("splash");
    // Decorative: hidden from assistive tech and nothing in it can take focus.
    expect(overlay).toHaveAttribute("aria-hidden", "true");
    expect(overlay.querySelectorAll("a, button, input, [tabindex]")).toHaveLength(0);
    expect(overlay.querySelector(".hx-logo--animated")).not.toBeNull();
    expect(overlay).toHaveTextContent("HELIX");
    expect(overlay).not.toHaveClass("hx-splash--out");
    expect(window.sessionStorage.getItem(SPLASH_KEY)).toBe("1");

    act(() => vi.advanceTimersByTime(SPLASH_FADE_AT));
    expect(overlay).toHaveClass("hx-splash--out"); // pointer-events: none from here
    act(() => vi.advanceTimersByTime(SPLASH_DONE_AT - SPLASH_FADE_AT));
    expect(screen.queryByTestId("splash")).toBeNull();
    first.unmount();

    // Same session: not again.
    render(<Splash enabled />);
    expect(screen.queryByTestId("splash")).toBeNull();
  });

  it("runs for under two seconds in total", () => {
    expect(SPLASH_FADE_AT).toBeLessThan(SPLASH_DONE_AT);
    expect(SPLASH_DONE_AT).toBeGreaterThanOrEqual(1600);
    expect(SPLASH_DONE_AT).toBeLessThanOrEqual(2000);
  });
});
