import "@testing-library/jest-dom/vitest";
import { cleanup } from "@testing-library/react";
import { afterEach, vi } from "vitest";
import { dashboard } from "../store/store";
import { clearCaseDataCache } from "../viz/caseData";
import { vizCalls } from "./vizMocks";

// WebGL libraries cannot run in jsdom; every test uses the recording stand-ins instead.
vi.mock("react-force-graph-3d", async () => ({ default: (await import("./vizMocks")).MockForceGraph }));
vi.mock("deck.gl", async () => {
  const m = await import("./vizMocks");
  return {
    DeckGL: m.MockDeckGL,
    default: m.MockDeckGL,
    ScatterplotLayer: m.MockScatterplotLayer,
    LineLayer: m.MockLineLayer,
    WebMercatorViewport: m.MockWebMercatorViewport,
  };
});

afterEach(() => {
  cleanup();
  dashboard.getState().reset();
  clearCaseDataCache();
  vizCalls.reset();
  vi.useRealTimers();
  vi.unstubAllGlobals();
});
