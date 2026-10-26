import { act, render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";
import { axe } from "vitest-axe";
import { dashboard } from "../../store/store";
import { enableWebGL } from "../../test/vizFixtures";
import { STORY_STEPS } from "./storyScript";
import type { WorkflowSceneProps } from "./WorkflowScene";
import { LIVE_UNAVAILABLE, SCENE_UNAVAILABLE, WorkflowView } from "./WorkflowView";

const AXE_OPTIONS = { rules: { "color-contrast": { enabled: false } } };

// jsdom has no WebGL: the lazy scene module is replaced by a recorder.
const scene = vi.hoisted(() => ({ props: [] as unknown[], fail: false }));
vi.mock("./WorkflowScene", () => ({
  default: (props: unknown) => {
    if (scene.fail) throw new Error("WebGL context lost");
    scene.props.push(props);
    return <div data-testid="mock-workflow-scene" />;
  },
}));

const lastScene = () => scene.props[scene.props.length - 1] as WorkflowSceneProps;

function reducedMotion(on: boolean) {
  vi.stubGlobal("matchMedia", (q: string) => ({
    matches: on && q.includes("reduce"),
    media: q,
    onchange: null,
    addEventListener: () => undefined,
    removeEventListener: () => undefined,
    addListener: () => undefined,
    removeListener: () => undefined,
    dispatchEvent: () => false,
  }));
}

const btn = (name: string | RegExp) => screen.getByRole("button", { name });
const announce = () => screen.getByTestId("wf-announce");

function setup() {
  scene.props.length = 0;
  scene.fail = false;
}

describe("WorkflowView", () => {
  it("draws the lazy 3D scene in a labelled role=img stage, in Story_Mode, playing", async () => {
    setup();
    enableWebGL();
    render(<WorkflowView />);
    const stage = screen.getByRole("img");
    expect(stage).toHaveAttribute("aria-label", expect.stringMatching(/six stage rings/));
    expect(await within(stage).findByTestId("mock-workflow-scene")).toBeInTheDocument();
    expect(btn("Story")).toHaveAttribute("aria-pressed", "true");
    expect(btn("Live")).toHaveAttribute("aria-pressed", "false");
    expect(btn("Pause")).toBeInTheDocument();
    expect(announce()).toHaveAttribute("aria-live", "polite");
    expect(announce()).toHaveTextContent(`Step 1 of ${STORY_STEPS.length}, SENSE: ${STORY_STEPS[0]!.title}`);
    expect(lastScene()).toMatchObject({ overlays: { redBlue: false, soc: false }, camera: { preset: "overview" }, reducedMotion: false, highlight: 0 });
    await userEvent.setup().click(btn("Pause"));
    expect(btn("Play")).toBeInTheDocument();
  });

  it("advances the story on the simulation clock", () => {
    setup();
    reducedMotion(false);
    vi.useFakeTimers({ toFake: ["requestAnimationFrame", "cancelAnimationFrame", "performance", "setTimeout", "clearTimeout", "Date"] });
    render(<WorkflowView />);
    act(() => vi.advanceTimersByTime(11_000));
    expect(announce()).toHaveTextContent("SENSE: Alice's T007 arrives");
    expect(screen.getByTestId("wf-stage-name")).toHaveTextContent("SENSE");
    act(() => btn("2×").click());
    act(() => vi.advanceTimersByTime(2_000)); // 4 s of story at 2x
    expect(announce()).toHaveTextContent("PERCEIVE: The sensors flag T007");
    expect(screen.getByTestId("wf-stage-name")).toHaveTextContent("PERCEIVE");
  });

  it("operates every control: speed, mode, overlays, camera, steps and restart", async () => {
    setup();
    enableWebGL();
    reducedMotion(true);
    render(<WorkflowView />);
    await screen.findByTestId("mock-workflow-scene");
    const user = userEvent.setup();

    for (const s of ["0.5×", "2×", "1×"]) {
      await user.click(btn(s));
      expect(btn(s)).toHaveAttribute("aria-pressed", "true");
    }
    expect(btn("2×")).toHaveAttribute("aria-pressed", "false");

    await user.click(btn("Red vs Blue"));
    await user.click(btn("SOC"));
    expect(btn("Red vs Blue")).toHaveAttribute("aria-pressed", "true");
    expect(lastScene().overlays).toEqual({ redBlue: true, soc: true });

    await user.click(btn("Investigator"));
    expect(btn("Investigator")).toHaveAttribute("aria-pressed", "true");
    expect(btn("Overview")).toHaveAttribute("aria-pressed", "false");
    expect(lastScene().camera.preset).toBe("investigator");
    await user.click(btn("HELIX core"));
    expect(lastScene().camera.preset).toBe("core");

    expect(btn("Previous step")).toBeDisabled();
    await user.click(btn("Next step"));
    await user.click(btn("Next step"));
    expect(announce()).toHaveTextContent("Step 3 of");
    expect(announce()).toHaveTextContent("Alice's T007 arrives");
    expect(lastScene().highlight).toBe(0);
    await user.click(btn("Next step"));
    expect(screen.getByTestId("wf-stage-name")).toHaveTextContent("PERCEIVE");
    expect(lastScene().highlight).toBe(1);
    await user.click(btn("Previous step"));
    expect(announce()).toHaveTextContent("Step 3 of");
    await user.click(btn("Restart"));
    expect(announce()).toHaveTextContent("Step 1 of");
  });

  it("opens paused under prefers-reduced-motion with step buttons", async () => {
    setup();
    enableWebGL();
    reducedMotion(true);
    render(<WorkflowView />);
    expect(btn("Play")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Pause" })).toBeNull();
    expect(btn("Next step")).toBeEnabled();
    expect(btn("Previous step")).toBeInTheDocument();
    expect((await screen.findByTestId("mock-workflow-scene")) && lastScene().reducedMotion).toBe(true);
  });

  it("Text view lists every step as an ordered list with the current step marked", async () => {
    setup();
    enableWebGL();
    reducedMotion(true);
    render(<WorkflowView />);
    const user = userEvent.setup();
    await user.click(btn("Text view"));
    expect(btn("Text view")).toHaveAttribute("aria-pressed", "true");
    expect(screen.queryByRole("img")).toBeNull();
    const list = screen.getByRole("list", { name: /The story, step by step/ });
    const items = within(list).getAllByRole("listitem");
    expect(items).toHaveLength(STORY_STEPS.length);
    STORY_STEPS.forEach((s, i) => {
      expect(items[i]).toHaveTextContent(s.title);
      expect(items[i]).toHaveTextContent(s.text);
    });
    expect(items[0]).toHaveAttribute("aria-current", "step");
    expect(items[0]).toHaveTextContent("(current step)");
    await user.click(btn("Next step"));
    expect(within(list).getAllByRole("listitem")[1]).toHaveAttribute("aria-current", "step");
    await user.click(btn("Text view"));
    expect(screen.getByRole("img")).toBeInTheDocument();
  });

  it("falls back to the text view with a note when WebGL is unavailable", () => {
    setup();
    reducedMotion(true);
    render(<WorkflowView />);
    expect(screen.getByTestId("wf-scene-unavailable")).toHaveTextContent(SCENE_UNAVAILABLE);
    expect(screen.getByTestId("wf-textview")).toBeInTheDocument();
    expect(screen.queryByRole("img")).toBeNull();
    expect(scene.props).toHaveLength(0);
    expect(btn("Text view")).toBeDisabled();
  });

  it("falls back to the text view when the scene throws, without crashing", async () => {
    setup();
    scene.fail = true;
    enableWebGL();
    reducedMotion(true);
    const err = vi.spyOn(console, "error").mockImplementation(() => undefined);
    render(<WorkflowView />);
    expect(await screen.findByTestId("wf-scene-unavailable")).toHaveTextContent(SCENE_UNAVAILABLE);
    expect(screen.getByTestId("wf-textview")).toBeInTheDocument();
    err.mockRestore();
  });

  it("Live_Mode falls back to the story while disconnected and follows the store when connected", async () => {
    setup();
    enableWebGL();
    reducedMotion(true);
    render(<WorkflowView />);
    const user = userEvent.setup();
    await user.click(btn("Live"));
    expect(btn("Live")).toHaveAttribute("aria-pressed", "true");
    expect(screen.getByTestId("wf-live-unavailable")).toHaveTextContent(LIVE_UNAVAILABLE);
    expect(btn("Next step")).toBeInTheDocument();
    act(() => dashboard.getState().setConnection("open"));
    expect(screen.queryByTestId("wf-live-unavailable")).toBeNull();
    expect(screen.queryByRole("button", { name: "Next step" })).toBeNull();
    expect(screen.getByTestId("wf-live-stats")).toHaveTextContent("0 in flight");
    expect(announce()).toHaveTextContent("Live: waiting for transactions.");
    expect(lastScene().highlight).toBeNull();
  });

  it("has no axe violations in the 3D and text views", async () => {
    setup();
    enableWebGL();
    reducedMotion(true);
    const { container } = render(<WorkflowView />);
    await screen.findByTestId("mock-workflow-scene");
    let results = await axe(container, AXE_OPTIONS);
    expect(results.violations.map((v) => `${v.id}: ${v.help}`)).toEqual([]);
    await userEvent.setup().click(btn("Text view"));
    results = await axe(container, AXE_OPTIONS);
    expect(results.violations.map((v) => `${v.id}: ${v.help}`)).toEqual([]);
  });
});
