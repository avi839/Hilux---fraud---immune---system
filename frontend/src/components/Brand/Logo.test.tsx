import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { axe } from "vitest-axe";
import { HelixLogo } from "./Logo";

describe("HelixLogo", () => {
  it("is a named image when given a title", () => {
    render(<HelixLogo title="HELIX" size={40} />);
    const img = screen.getByRole("img", { name: "HELIX" });
    expect(img).toHaveAttribute("width", "40");
    expect(img).not.toHaveAttribute("aria-hidden");
  });

  it("is hidden from assistive tech when decorative", () => {
    render(<HelixLogo />);
    expect(screen.queryByRole("img")).toBeNull();
    const svg = screen.getByTestId("helix-logo");
    expect(svg).toHaveAttribute("aria-hidden", "true");
    expect(svg).toHaveAttribute("width", "28");
  });

  it("draws two interlaced strands and opts into the drawing animation", () => {
    const { container } = render(<HelixLogo animated />);
    const svg = container.querySelector("svg")!;
    expect(svg).toHaveClass("hx-logo--animated");
    // Two strands, each split at the middle crossing; two of the halves carry an underlay gap.
    expect(svg.querySelectorAll(".hx-logo__strand")).toHaveLength(4);
    expect(svg.querySelectorAll(".hx-logo__gap")).toHaveLength(2);
    for (const p of svg.querySelectorAll(".hx-logo__strand")) expect(p).toHaveAttribute("pathLength", "1");
  });

  it("gives every instance its own gradient ids", () => {
    const { container } = render(
      <>
        <HelixLogo />
        <HelixLogo />
      </>,
    );
    const ids = [...container.querySelectorAll("linearGradient")].map((g) => g.id);
    expect(ids).toHaveLength(4);
    expect(new Set(ids).size).toBe(4);
    for (const id of ids) expect(id).toMatch(/^[\w-]+$/);
    // Every gradient reference resolves inside its own svg.
    for (const svg of container.querySelectorAll("svg")) {
      for (const el of svg.querySelectorAll("[stroke^='url(#']")) {
        const ref = el.getAttribute("stroke")!.slice(5, -1);
        expect(svg.querySelector(`#${ref}`)).not.toBeNull();
      }
    }
  });

  it("has no axe violations", async () => {
    const { container } = render(
      <>
        <HelixLogo title="HELIX" />
        <HelixLogo animated />
      </>,
    );
    expect((await axe(container)).violations).toEqual([]);
  });
});
