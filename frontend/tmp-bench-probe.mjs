import { chromium } from "@playwright/test";

const browser = await chromium.launch({ channel: "msedge" });
const page = await browser.newPage({ viewport: { width: 1440, height: 900 } });
const errors = [];
page.on("pageerror", (e) => errors.push(String(e)));
page.on("console", (m) => m.type() === "error" && errors.push(m.text()));
page.on("requestfailed", (r) => errors.push(`failed ${r.url()}`));
await page.goto("http://localhost:5173/", { waitUntil: "commit", timeout: 60000 });
await page.waitForTimeout(20000);
const tabs = await page.getByRole("tab").allTextContents().catch(() => []);
console.log("tabs:", JSON.stringify(tabs));
console.log("body:", (await page.locator("body").innerText()).slice(0, 400));
console.log("errors:", JSON.stringify(errors.slice(0, 10)));
await page.screenshot({ path: "../docs/benchmark-probe.tmp.png" });
await browser.close();
