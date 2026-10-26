import { act, render, screen, within } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { axe } from "vitest-axe";
import { dashboard } from "../../store/store";
import { jsonResponse, routeFetch } from "../../test/fixtures";
import { aliceDetail, aliceEvidence, bobDetail, bobEvidence } from "../../test/sampleRun";
import { vizCalls } from "../../test/vizMocks";
import { aliceTimeline, enableWebGL, NO_STORAGE } from "../../test/vizFixtures";
import { toMs } from "../../viz/time";
import { GeoMap } from "./GeoMap";

const AXE_OPTIONS = { rules: { "color-contrast": { enabled: false } } };

function select(id: "C-1" | "C-2") {
  act(() => {
    dashboard.getState().upsertCase(id === "C-1" ? aliceDetail : bobDetail);
    dashboard.getState().selectCase(id);
  });
}

describe("GeoMap", () => {
  it("lists located events with distance and implied speed per hop, and the omitted count", async () => {
    routeFetch({ "/cases/C-1/timeline": aliceTimeline, "/cases/C-1/evidence": aliceEvidence });
    const { container } = render(<GeoMap />);
    const t0 = performance.now();
    select("C-1");
    const list = await screen.findByTestId("geo-list");
    expect(performance.now() - t0).toBeLessThan(3_000);
    expect(screen.getByTestId("geo-omitted")).toHaveTextContent("1 of 4 events omitted: no location data");
    expect(screen.getByTestId("geo-summary")).toHaveTextContent("3 located events · 1 account path");
    expect(within(list).getByTestId("geo-row-T004")).toHaveTextContent("first located");
    const t007 = within(list).getByTestId("geo-row-T007");
    expect(t007).toHaveTextContent("5196.1 km");
    expect(t007).toHaveTextContent("1.17 h");
    expect(t007).toHaveTextContent("4454 km/h");
    // Context events (not a Case transaction) are marked; T007 is the Case's.
    expect(within(list).getByTestId("geo-row-T005")).toHaveTextContent("(context)");
    expect(t007).not.toHaveTextContent("(context)");
    expect(screen.getByRole("button", { name: "List view" })).toBeDisabled();

    const results = await axe(container, AXE_OPTIONS);
    expect(results.violations.map((v) => `${v.id}: ${v.help}`)).toEqual([]);
  });

  it("follows the replay position", async () => {
    routeFetch({ "/cases/C-1/timeline": aliceTimeline, "/cases/C-1/evidence": aliceEvidence });
    render(<GeoMap />);
    select("C-1");
    await screen.findByTestId("geo-list");
    act(() => dashboard.getState().setReplayT(toMs("2024-06-05T10:10:00+00:00")));
    expect(screen.getByTestId("geo-omitted")).toHaveTextContent("0 of 2 events omitted");
    expect(screen.queryByTestId("geo-row-T007")).not.toBeInTheDocument();
    act(() => dashboard.getState().setReplayT(null));
    expect(screen.getByTestId("geo-row-T007")).toBeInTheDocument();
  });

  it("falls back to Evidence_Chain features when the timeline needs Storage (503)", async () => {
    routeFetch({ "/cases/C-2/timeline": () => jsonResponse(NO_STORAGE, 503), "/cases/C-2/evidence": bobEvidence });
    const { container } = render(<GeoMap />);
    select("C-2");
    expect(await screen.findByTestId("geo-fallback")).toHaveTextContent("server started without --storage");
    expect(screen.getByTestId("geo-omitted")).toHaveTextContent("3 of 3 events omitted: no location data");
    expect(screen.getByText("No located events.")).toBeInTheDocument();
    const results = await axe(container, AXE_OPTIONS);
    expect(results.violations.map((v) => `${v.id}: ${v.help}`)).toEqual([]);
  });

  it("draws events and per-hop lines with deck.gl, with no basemap and no network fetch", async () => {
    enableWebGL();
    const fetch = routeFetch({ "/cases/C-1/timeline": aliceTimeline, "/cases/C-1/evidence": aliceEvidence });
    render(<GeoMap />);
    select("C-1");
    expect(await screen.findByTestId("mock-deck", {}, { timeout: 5_000 })).toBeInTheDocument();
    const deck = vizCalls.lastDeck()!;
    const layers = deck.layers as Array<{ id: string; kind: string; props: { data: unknown[] } }>;
    expect(layers.map((l) => [l.id, l.kind])).toEqual([
      ["graticule", "LineLayer"],
      ["hops", "LineLayer"],
      ["events", "ScatterplotLayer"],
    ]);
    // Alice: T004 → T005 → T007 (T006 has no location): two hop lines, three points.
    const hops = layers[1]!.props.data as Array<{ fromTx: string; toTx: string; from: number[]; to: number[] }>;
    expect(hops.map((h) => [h.fromTx, h.toTx])).toEqual([
      ["T004", "T005"],
      ["T005", "T007"],
    ]);
    expect(hops[1]).toMatchObject({ from: [13.4, 52.52], to: [3.38, 6.52] });
    expect(layers[2]!.props.data).toHaveLength(3);
    expect((deck.getTooltip as (i: { object?: unknown }) => string | null)({ object: hops[1] })).toContain("4454 km/h");
    // No basemap child and only the two API requests: nothing goes to a tile server.
    expect(deck.children).toBeUndefined();
    expect(fetch.mock.calls.every(([url]) => String(url).startsWith("/api/v1/"))).toBe(true);
    expect(screen.getByRole("img", { name: /Map of 3 located events with 1 account paths/ })).toBeInTheDocument();
  });

  it("shows an error with Retry for other timeline failures", async () => {
    routeFetch({ "/cases/C-1/timeline": () => jsonResponse({ detail: "boom" }, 500), "/cases/C-1/evidence": aliceEvidence });
    render(<GeoMap />);
    select("C-1");
    expect(await screen.findByTestId("geo-error")).toHaveTextContent("Could not load the timeline for C-1");
  });
});
