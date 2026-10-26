import { defineConfig } from "@playwright/test";

// Browser smoke tests against servers that are already running (scripts/demo.ps1, or
// backend `python -m helix.api --generator` + `npm run dev`). Uses the installed Microsoft
// Edge, so no Playwright browser download is needed.
export default defineConfig({
  testDir: "./e2e",
  timeout: 90_000,
  expect: { timeout: 20_000 },
  retries: 0,
  workers: 1,
  reporter: [["list"]],
  use: {
    baseURL: "http://localhost:5173",
    channel: "msedge",
    headless: true,
    viewport: { width: 1600, height: 1000 },
    // Software WebGL so the 3D network and the map also render on machines without a GPU.
    launchOptions: { args: ["--enable-unsafe-swiftshader"] },
  },
});
