import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it } from "vitest";
import { axe } from "vitest-axe";
import { dashboard } from "../../store/store";
import { jsonResponse, routeFetch } from "../../test/fixtures";
import { aliceDetail, aliceEvidence } from "../../test/sampleRun";
import { aliceTimeline, NO_STORAGE } from "../../test/vizFixtures";
import { toMs } from "../../viz/time";
import { EvidencePanel } from "../EvidencePanel/EvidencePanel";
import { GeoMap } from "../GeoMap/GeoMap";
import { ReplaySlider } from "./ReplaySlider";

const AXE_OPTIONS = { rules: { "color-contrast": { enabled: false } } };
const ts = (tx: string) => toMs(aliceTimeline.entries.find((e) => e.event.tx_id === tx)!.event.ts);
const replayT = () => dashboard.getState().replayT;

function select() {
  act(() => {
    dashboard.getState().upsertCase(aliceDetail);
    dashboard.getState().selectCase("C-1");
  });
}

describe("ReplaySlider", () => {
  it('says "replay needs the server started with --storage" on 503', async () => {
    routeFetch({ "/cases/C-1/timeline": () => jsonResponse(NO_STORAGE, 503) });
    const { container } = render(<ReplaySlider />);
    expect(screen.getByText(/select a case/i)).toBeInTheDocument();
    select();
    const msg = await screen.findByTestId("replay-no-storage");
    expect(msg.textContent?.toLowerCase()).toContain("replay needs the server started with --storage");
    expect(screen.queryByRole("slider")).not.toBeInTheDocument();
    const results = await axe(container, AXE_OPTIONS);
    expect(results.violations.map((v) => `${v.id}: ${v.help}`)).toEqual([]);
  });

  it("is a labelled slider operable from the keyboard: arrows step events, Home/End jump", async () => {
    routeFetch({ "/cases/C-1/timeline": aliceTimeline });
    const { container } = render(<ReplaySlider />);
    select();
    const slider = await screen.findByRole("slider", { name: "Replay time" });
    expect(slider).toHaveAttribute("aria-valuetext", expect.stringMatching(/^2024-06-05 11:20:00 UTC, 4 of 4 events$/));
    expect(screen.getByTestId("replay-now")).toHaveTextContent("Current state");

    const user = userEvent.setup();
    await user.tab();
    expect(slider).toHaveFocus();
    await user.keyboard("{Home}");
    expect(replayT()).toBe(toMs(aliceTimeline.start));
    expect(slider).toHaveValue("0");
    await user.keyboard("{ArrowRight}");
    expect(replayT()).toBe(ts("T004"));
    await user.keyboard("{ArrowRight}{ArrowRight}");
    expect(replayT()).toBe(ts("T006"));
    expect(screen.getByTestId("replay-now")).toHaveTextContent("T = 2024-06-05 10:40:00 UTC (event) · 3 of 4 events at or before T");
    expect(slider).toHaveAttribute("aria-valuetext", "2024-06-05 10:40:00 UTC, 3 of 4 events");
    await user.keyboard("{ArrowLeft}");
    expect(replayT()).toBe(ts("T005"));
    await user.keyboard("{End}");
    expect(replayT()).toBe(toMs(aliceTimeline.end));

    // Pointer / native changes map the slider's seconds back to time.
    fireEvent.change(slider, { target: { value: "3600" } });
    expect(replayT()).toBe(toMs(aliceTimeline.start) + 3_600_000);

    // Buttons, by keyboard.
    screen.getByRole("button", { name: "Next event" }).focus();
    await user.keyboard("{Enter}");
    expect(replayT()).toBe(ts("T004"));
    await user.keyboard(" ");
    expect(replayT()).toBe(ts("T005"));
    await user.click(screen.getByRole("button", { name: "Previous event" }));
    expect(replayT()).toBe(ts("T004"));
    await user.click(screen.getByRole("button", { name: "Show current state" }));
    expect(replayT()).toBeNull();
    expect(screen.getByRole("button", { name: "Show current state" })).toBeDisabled();

    const results = await axe(container, AXE_OPTIONS);
    expect(results.violations.map((v) => `${v.id}: ${v.help}`)).toEqual([]);
  });

  it("plays through the events and pauses", async () => {
    routeFetch({ "/cases/C-1/timeline": aliceTimeline });
    render(<ReplaySlider />);
    select();
    await screen.findByRole("slider");
    const user = userEvent.setup();
    const play = screen.getByRole("button", { name: "Play" });
    await user.click(play);
    expect(replayT()).toBe(toMs(aliceTimeline.start));
    expect(play).toHaveAttribute("aria-pressed", "true");
    expect(play).toHaveTextContent("Pause");
    await waitFor(() => expect(replayT()).toBe(ts("T004")), { timeout: 2_000 });
    await user.click(screen.getByRole("button", { name: "Pause" }));
    const paused = replayT();
    await new Promise((r) => setTimeout(r, 900));
    expect(replayT()).toBe(paused);
  });

  it("filters the map and the evidence to data at or before T within 1 s", async () => {
    routeFetch({ "/cases/C-1/timeline": aliceTimeline, "/cases/C-1/evidence": aliceEvidence, "/cases/C-1": aliceDetail });
    render(
      <>
        <ReplaySlider />
        <EvidencePanel />
        <GeoMap />
      </>,
    );
    select();
    const slider = await screen.findByRole("slider");
    await screen.findByTestId("ev-summary");
    await screen.findByTestId("geo-row-T007");
    expect(screen.getAllByRole("img")).toHaveLength(6); // Alice's six contributor bars

    const t0 = performance.now();
    fireEvent.keyDown(slider, { key: "ArrowLeft" }); // end → T006 (10:40), before T007
    await waitFor(() => expect(screen.getByTestId("ev-replay")).toHaveTextContent("showing 0 of 8 chain entries"));
    expect(screen.queryByTestId("geo-row-T007")).not.toBeInTheDocument();
    expect(screen.getByTestId("geo-omitted")).toHaveTextContent("1 of 3 events omitted");
    expect(screen.queryAllByRole("img")).toHaveLength(0);
    expect(performance.now() - t0).toBeLessThan(1_000);
  });
});
