import { chromium } from "@playwright/test";

const browser = await chromium.launch({ channel: "msedge" });
const page = await browser.newPage({ viewport: { width: 1440, height: 900 } });
const errors = [];
page.on("pageerror", (e) => errors.push(String(e)));
page.on("console", (m) => m.type() === "error" && errors.push(m.text()));
await page.goto("http://localhost:5173/", { waitUntil: "domcontentloaded", timeout: 90000 });
await page.getByRole("tab", { name: "Benchmark", exact: true }).click();
await page.getByRole("heading", { name: "HELIX vs ML baselines" }).waitFor();
await page.waitForTimeout(800);
await page.addStyleTag({ content: ".topbar { position: static !important; }" });
await page.screenshot({ path: "../docs/benchmark.png", fullPage: true });
await page.screenshot({ path: "../docs/benchmark-top.tmp.png" });
console.log("errors:", JSON.stringify(errors));
await browser.close();
