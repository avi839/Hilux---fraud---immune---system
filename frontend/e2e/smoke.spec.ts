import { expect, test, type Page } from "@playwright/test";

// Smoke test of the real dashboard in a real browser against the running backend: nothing
// is mocked. Run with `npm run e2e` while backend (:8000) and Vite (:5173) are up.

function watch(page: Page) {
  const pageErrors: string[] = [];
  const consoleErrors: string[] = [];
  const httpErrors: string[] = [];
  const sockets = new Map<string, { frames: number; closed: boolean }>();
  page.on("pageerror", (e) => pageErrors.push(`${e.name}: ${e.message}`));
  // HTTP errors are checked per request below; the browser's matching "Failed to load
  // resource" console line is skipped here so it is not counted twice.
  page.on("console", (m) => {
    if (m.type() === "error" && !m.text().startsWith("Failed to load resource")) consoleErrors.push(m.text());
  });
  page.on("response", (r) => {
    const path = new URL(r.url()).pathname;
    // Expected: the optional /immune and /attacker endpoints may not exist yet (404), and
    // the demo file's deliberately invalid line (T009) is rejected by validation (422).
    const expected = (r.status() === 404 && /\/api\/v1\/(immune|attacker)$/.test(path)) || (r.status() === 422 && path === "/api/v1/events");
    if (r.status() >= 400 && !expected) httpErrors.push(`${r.status()} ${r.request().method()} ${path}`);
  });
  page.on("websocket", (ws) => {
    const s = { frames: 0, closed: false };
    sockets.set(new URL(ws.url()).pathname, s);
    ws.on("framereceived", () => (s.frames += 1));
    ws.on("close", () => (s.closed = true));
  });
  return { pageErrors, consoleErrors, httpErrors, sockets };
}

test("dashboard shows live HELIX activity and explains a case", async ({ page }) => {
  const seen = watch(page);
  await page.goto("/");

  // Shell renders and all three WebSocket channels connect.
  await expect(page.getByRole("heading", { name: "HELIX", exact: true })).toBeVisible();
  await expect(page.getByTestId("connection")).toHaveText("Live");
  for (const ch of ["/ws/events", "/ws/cases", "/ws/metrics"]) expect(seen.sockets.has(ch), `${ch} opened`).toBe(true);
  // The app opens on Home; the live grid is the Dashboard tab.
  await page.getByRole("tablist", { name: "Dashboard views" }).getByRole("tab", { name: "Dashboard", exact: true }).click();

  // Drive real traffic through the backend: the generator plus the demo scenario.
  // The generator lives in the Controls drawer (a modal dialog); Escape closes it again.
  const controls = page.getByRole("button", { name: "Controls", exact: true });
  await controls.click();
  const drawer = page.getByRole("dialog", { name: "Controls" });
  await drawer.getByRole("button", { name: "Start generator" }).click();
  await expect(drawer.getByTestId("confirmed-generator")).toHaveText("running");
  await page.keyboard.press("Escape");
  await expect(drawer).toBeHidden();
  await expect(controls).toBeFocused();
  await page.getByRole("button", { name: "Load demo scenario" }).click();
  await expect(page.getByTestId("demo-result")).toContainText("13 accepted, 1 duplicate, 1 rejected by validation");

  // The live feed fills with scored events.
  const feedRows = page.getByRole("region", { name: "Live transaction feed, newest first" }).locator("tbody tr:not(:has(.empty))");
  await expect.poll(() => feedRows.count(), { timeout: 15_000 }).toBeGreaterThan(0);
  expect(seen.sockets.get("/ws/events")!.frames).toBeGreaterThan(0);

  // Cases: the queue has at least one, and the scenario produced its own.
  const queueItems = page.getByRole("listbox", { name: "Active queue, highest priority first" }).getByRole("option");
  await expect.poll(() => queueItems.count(), { timeout: 15_000 }).toBeGreaterThan(0);
  const showButtons = page.getByTestId("demo-scenario").getByRole("button", { name: /^Show C-\d+$/ });
  await expect.poll(() => showButtons.count(), { timeout: 15_000 }).toBeGreaterThanOrEqual(1);

  // Clicking a queued Case shows its tier, risk and explanation.
  const first = queueItems.first();
  const caseId = (await first.locator(".queue__id").textContent())!.trim();
  await first.click();
  await expect(first).toHaveAttribute("aria-selected", "true");
  const evidence = page.getByRole("region", { name: "Evidence", exact: true });
  await expect(evidence.getByTestId("ev-summary")).toContainText(/(LOW|MEDIUM|HIGH|CRITICAL) \| risk \d/);
  await expect(evidence.getByRole("heading", { name: /^Reasons/ })).toBeVisible();
  await expect(evidence.locator(".panel__case")).toContainText(caseId);

  // Then a scenario Case (Alice first: Berlin -> Lagos), with explanation, network and map.
  const alice = page.getByTestId("demo-scenario").getByRole("listitem").filter({ hasText: "Alice" }).getByRole("button");
  await alice.click();
  await expect(evidence.getByTestId("ev-summary")).toContainText(/risk \d/);
  await expect(evidence.getByText(/Geo/).first()).toBeVisible();
  // Network and Map are sub-tabs of the Case workspace; only the selected one is mounted.
  const views = page.getByRole("tablist", { name: "Case views" });
  await views.getByRole("tab", { name: "Network", exact: true }).click();
  await expect(page.getByTestId("net-summary")).toContainText(/accounts/);
  await views.getByRole("tab", { name: "Map", exact: true }).click();
  // Located events come from the Storage-backed timeline, written in batches; allow a retry.
  await expect(page.getByTestId("geo-summary")).toContainText(/^[1-9]\d* located event/, { timeout: 30_000 });
  await views.getByRole("tab", { name: "Network", exact: true }).click();
  // Switching back remounts the view, which fetches again; wait so the screenshot shows the graph.
  await expect(page.getByTestId("net-summary")).toContainText(/accounts/, { timeout: 30_000 });

  // The HELIX loop panel shows per-stage throughput.
  await expect(page.getByTestId("stage-PERCEIVE")).toBeVisible();

  await page.waitForTimeout(3_000); // let the 3D layout settle for the screenshot
  // The sticky header would be painted mid-page in a full-page capture; unstick it for the shot.
  await page.addStyleTag({ content: ".topbar { position: static !important; }" });
  await page.screenshot({ path: "../docs/dashboard.png", fullPage: true });

  for (const ch of ["/ws/events", "/ws/cases", "/ws/metrics"]) expect(seen.sockets.get(ch)!.closed, `${ch} still open`).toBe(false);
  expect(seen.pageErrors, "uncaught page errors").toEqual([]);
  expect(seen.consoleErrors, "console errors").toEqual([]);
  expect(seen.httpErrors, "unexpected HTTP errors").toEqual([]);
});

// Every tab of the shell, in order: [tab name, heading inside its panel, optional screenshot].
// Run against a backend started with --soc --attacker --immune so no view is in its
// "feature not enabled" state.
const TABS: ReadonlyArray<readonly [string, RegExp, string?]> = [
  ["Home", /^HELIX$/, "../docs/home.png"],
  ["Dashboard", /^Live feed$/],
  ["Workflow", /^Workflow$/, "../docs/workflow.png"],
  ["SOC console", /^Kill chain$/, "../docs/soc.png"],
  ["Red vs Blue", /^Red vs Blue arena$/, "../docs/arena.png"],
  ["Benchmark", /^HELIX vs ML baselines$/, "../docs/benchmark.png"],
  ["Trace", /^Transaction trace$/],
  ["Live log", /^Live log$/],
  ["Pricing", /^Plans for every fraud team$/, "../docs/pricing.png"],
];
const SOC_SECTIONS = [
  "SLA timers and handoff",
  "Kill chain",
  "Response playbooks",
  "Detection rules",
  "Coverage matrix",
  "Threat intel",
  "Audit ledger",
  "Resilience mode",
  "Purple team",
];

test("every dashboard tab renders its view without an error boundary", async ({ page }) => {
  const seen = watch(page);
  await page.goto("/");
  await expect(page.getByTestId("connection")).toHaveText("Live");
  await page.getByRole("tab", { name: "Dashboard", exact: true }).click();
  // Some traffic so the views have data: the scenario opens Cases (and SOC playbook runs).
  await page.getByRole("button", { name: "Load demo scenario" }).click();
  await expect(page.getByTestId("demo-result")).toContainText("13 accepted");

  const tablist = page.getByRole("tablist", { name: "Dashboard views" });
  for (const [name, heading, shot] of TABS) {
    const tab = tablist.getByRole("tab", { name, exact: true });
    await tab.click();
    await expect(tab).toHaveAttribute("aria-selected", "true");
    const panel = page.locator(`#${await tab.getAttribute("aria-controls")}`);
    await expect(panel).toBeVisible();
    await expect(panel.getByRole("heading", { name: heading }).first()).toBeVisible();
    // No tab- or panel-level error boundary fired, and no "feature not enabled" notice.
    await expect(page.locator('[data-testid^="tab-error-"], [data-testid^="panel-error-"]')).toHaveCount(0);
    await expect(panel.locator('[data-testid^="soc-absent-"], [data-testid^="soc-disabled-"]')).toHaveCount(0);

    if (name === "SOC console") {
      for (const s of SOC_SECTIONS) await expect(panel.getByRole("heading", { name: s, exact: true })).toBeVisible();
      // Verify the audit hash chain of this run: it must be intact.
      await panel.getByRole("button", { name: "Verify" }).click();
      // Verify walks the whole chain server-side; on a long-running demo it holds 1,000+
      // entries while the generator keeps the backend busy, so allow it more time.
      await expect(panel.getByTestId("audit-verify")).toContainText("Chain verified", { timeout: 60_000 });
    }
    if (name === "Workflow") {
      // The lazy 3D scene mounts a WebGL canvas (no "scene unavailable" fallback), and the
      // story plays: the screen-reader caption moves on to the next step within seconds.
      await expect(panel.getByTestId("wf-stage").locator("canvas")).toHaveCount(1);
      await expect(panel.getByTestId("wf-scene-unavailable")).toHaveCount(0);
      const announce = panel.getByTestId("wf-announce");
      await expect(announce).not.toBeEmpty();
      const before = await announce.textContent();
      await expect.poll(() => announce.textContent(), { timeout: 10_000 }).not.toBe(before);
    }
    if (shot) {
      // Polled sections finish loading; the Workflow scene gets time to fill with particles.
      await page.waitForTimeout(name === "Workflow" ? 4_000 : 2_000);
      if (name === "Home") {
        // Home's sections reveal on scroll; a full-page capture does not scroll, so walk
        // the page once to reveal them all, then return to the top.
        // (String expressions: the e2e tsconfig has no DOM lib.)
        const height = (await page.evaluate("document.documentElement.scrollHeight")) as number;
        for (let y = 0; y < height; y += 600) {
          await page.evaluate(`window.scrollTo(0, ${y})`);
          await page.waitForTimeout(150);
        }
        await page.evaluate("window.scrollTo(0, 0)");
        await page.waitForTimeout(900); // the 700 ms reveal transition
      }
      await page.addStyleTag({ content: ".topbar { position: static !important; }" });
      await page.screenshot({ path: shot, fullPage: true });
    }
  }

  expect(seen.pageErrors, "uncaught page errors").toEqual([]);
  expect(seen.consoleErrors, "console errors").toEqual([]);
  expect(seen.httpErrors, "unexpected HTTP errors").toEqual([]);
});
