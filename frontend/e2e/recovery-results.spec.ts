import { expect, test } from "@playwright/test";
import { spawn, type ChildProcess } from "node:child_process";
import { once } from "node:events";
import { readFileSync } from "node:fs";
import { join, resolve, dirname } from "node:path";
import { fileURLToPath } from "node:url";

const FRONTEND_ROOT = resolve(dirname(fileURLToPath(import.meta.url)), "..");
const REPOSITORY_ROOT = resolve(FRONTEND_ROOT, "..");
let preview: ChildProcess | undefined;
let baseUrl = "";

test.beforeAll(async () => {
  preview = spawn(process.execPath, [join(FRONTEND_ROOT, "node_modules/vite/bin/vite.js"), "preview", "--host", "127.0.0.1", "--port", "0"], {
    cwd: FRONTEND_ROOT,
    env: { ...process.env, NO_COLOR: "1" },
    stdio: ["ignore", "pipe", "pipe"],
  });
  const server = preview;
  await new Promise<void>((accept, reject) => {
    let output = "";
    const timeout = setTimeout(() => reject(new Error(`Dashboard preview did not start: ${output}`)), 15_000);
    server.once("error", (error) => { clearTimeout(timeout); reject(error); });
    server.once("exit", (code) => { clearTimeout(timeout); reject(new Error(`Dashboard preview exited ${code}: ${output}`)); });
    server.stderr?.on("data", (chunk) => { output += chunk.toString(); });
    server.stdout?.on("data", (chunk) => {
      output += chunk.toString();
      const address = output.match(/http:\/\/127\.0\.0\.1:\d+/);
      if (address) { baseUrl = address[0]; clearTimeout(timeout); accept(); }
    });
  });
});

test.afterAll(async () => {
  const server = preview;
  if (!server || server.exitCode !== null || server.signalCode !== null) return;
  const closed = once(server, "exit");
  server.kill("SIGTERM");
  await closed;
});

test.beforeEach(async ({ page }) => {
  // The recorded research view needs no local bundle index. Only that unrelated API is stubbed.
  await page.route("**/api/v1/runs?*", (route) => route.fulfill({ json: {
    projection_version: "inferdrome.dashboard.v1",
    generated_at: "2026-11-01T12:00:00Z",
    runs: [], rejected: [],
    page: { limit: 200, returned: 0, total: 0, has_more: false, next_cursor: null },
  } }));
});

test("shows published recovery results with order filters and an exact aggregate download", async ({ page }, testInfo) => {
  const browserErrors: string[] = [];
  page.on("pageerror", (error) => browserErrors.push(error.message));
  await page.setViewportSize({ width: 1440, height: 1000 });
  await page.goto(`${baseUrl}/runs`);
  await page.getByRole("navigation", { name: "Dashboard views" }).getByRole("link", { name: "Recovery results", exact: true }).click();
  await expect(page).toHaveURL(`${baseUrl}/recovery-results`);
  await expect(page.getByRole("heading", { name: "12.3% fewer SLO misses" })).toBeVisible();
  await expect(page.getByText("Published research aggregate")).toBeVisible();
  await expect(page.locator(".recovery-pair-table tbody tr")).toHaveCount(12);
  await expect(page.getByRole("button", { name: "Refresh runs" })).toHaveCount(0);
  await page.screenshot({ path: testInfo.outputPath("recovery-desktop-light.png"), fullPage: true });

  await page.getByRole("button", { name: "400 ms first", exact: true }).click();
  await expect(page.locator(".recovery-pair-table tbody tr")).toHaveCount(6);
  await expect(page.getByText("400 → 500 ms", { exact: true })).toHaveCount(6);
  await expect(page.getByText("3,420", { exact: true })).toBeVisible();
  await page.getByRole("button", { name: "500 ms first", exact: true }).click();
  await expect(page.getByText("500 → 400 ms", { exact: true })).toHaveCount(6);
  await page.getByRole("button", { name: "All 12", exact: true }).click();

  const downloadPromise = page.waitForEvent("download");
  await page.getByRole("link", { name: "Download results" }).click();
  const download = await downloadPromise;
  expect(download.suggestedFilename()).toBe("recovery-headroom-2026-10-19.json");
  const downloadPath = await download.path();
  if (!downloadPath) throw new Error("Published aggregate download is unavailable.");
  expect(readFileSync(downloadPath, "utf8")).toBe(readFileSync(join(REPOSITORY_ROOT, "docs/results/recovery-headroom-2026-10-19.json"), "utf8"));

  await page.goto(`${baseUrl}/recovery-results/`);
  await page.reload();
  await expect(page.locator(".recovery-pair-table tbody tr")).toHaveCount(12);
  await page.getByRole("button", { name: "Switch to dark theme" }).click();
  await page.screenshot({ path: testInfo.outputPath("recovery-desktop-dark.png"), fullPage: true });
  await page.setViewportSize({ width: 375, height: 812 });
  await expect(page.getByRole("columnheader", { name: "400 ms misses" })).toBeVisible();
  const reductionCell = page.locator(".recovery-pair-table tbody tr").first().locator("td").last();
  const cellBounds = await reductionCell.boundingBox();
  expect(cellBounds && cellBounds.x + cellBounds.width <= 375).toBeTruthy();
  await page.screenshot({ path: testInfo.outputPath("recovery-mobile-dark.png"), fullPage: true });
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(true);
  await expect(page.getByRole("link", { name: "Download results" })).toBeVisible();
  expect(browserErrors).toEqual([]);
});
