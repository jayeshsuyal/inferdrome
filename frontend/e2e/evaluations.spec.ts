import { expect, test, type Page } from "@playwright/test";
import { spawn, spawnSync, type ChildProcess } from "node:child_process";
import { once } from "node:events";
import { chmodSync, existsSync, lstatSync, mkdirSync, mkdtempSync, readFileSync, readdirSync, realpathSync, renameSync, rmSync, writeFileSync } from "node:fs";
import { createServer } from "node:net";
import { tmpdir } from "node:os";
import { delimiter, dirname, isAbsolute, join, resolve, sep } from "node:path";
import { fileURLToPath } from "node:url";

const ROOT = resolve(dirname(fileURLToPath(import.meta.url)), "../..");
const TRUST = "Pinned report; source inputs not replayed";

interface ReportFixture {
  readonly kind: "STUDY" | "PREFIX_CACHE";
  readonly report_path: string;
  readonly expected_sha256: string;
}

interface Fixture {
  readonly root: string;
  readonly catalog: string;
  readonly reports: Record<string, ReportFixture>;
}

interface Summary {
  readonly report_id: string;
  readonly report_sha256: string;
  readonly label: string;
  readonly kind: "STUDY" | "PREFIX_CACHE";
  readonly returned_records: number;
}

interface Server {
  readonly process: ChildProcess;
  readonly url: string;
}

function python(): string {
  const configured = process.env.INFERDROME_PYTHON?.trim();
  if (configured) return configured.includes(sep) && !isAbsolute(configured) ? resolve(ROOT, configured) : configured;
  const local = join(ROOT, ".venv", "bin", "python");
  return existsSync(local) ? local : "python3";
}

function environment(): NodeJS.ProcessEnv {
  return { ...process.env, PYTHONPATH: [join(ROOT, "src"), ROOT, process.env.PYTHONPATH].filter(Boolean).join(delimiter) };
}

function prepare(studyVariant = "complete", cacheBlockCount = 4): Fixture {
  const root = realpathSync(mkdtempSync(join(tmpdir(), "inferdrome-evaluation-ui-")));
  const result = spawnSync(python(), ["-m", "tests.evaluation_dashboard_support", "--root", root, "--study-variant", studyVariant, "--cache-block-count", String(cacheBlockCount)], {
    cwd: ROOT, encoding: "utf8", env: environment(), timeout: 60_000, maxBuffer: 1024 * 1024,
  });
  if (result.error || result.status !== 0) throw new Error(`Synthetic evaluation preparation failed: ${result.stderr}`);
  const fixture = JSON.parse(result.stdout) as Omit<Fixture, "root"> & { fixture_provenance: string };
  if (fixture.fixture_provenance !== "SYNTHETIC_ONLY") throw new Error("Expected explicitly synthetic evaluation fixtures");
  mkdirSync(join(root, "runs"), { mode: 0o700 });
  return { ...fixture, root };
}

async function port(): Promise<number> {
  return await new Promise((resolvePort, reject) => {
    const socket = createServer();
    socket.unref();
    socket.once("error", reject);
    socket.listen(0, "127.0.0.1", () => {
      const address = socket.address();
      if (!address || typeof address === "string") { socket.close(); reject(new Error("No loopback port")); return; }
      socket.close((error) => error ? reject(error) : resolvePort(address.port));
    });
  });
}

async function start(fixture: Fixture, keyring?: string): Promise<Server> {
  const number = await port();
  const child = spawn(python(), [
    "-m", "inferdrome", "dashboard", "--runs-root", join(fixture.root, "runs"),
    "--evaluation-reports-catalog", fixture.catalog, "--port", String(number),
    ...(keyring ? ["--keyring", keyring] : []),
  ], { cwd: ROOT, env: environment(), stdio: ["ignore", "ignore", "pipe"] });
  let stderr = "";
  child.stderr?.on("data", (chunk: Buffer) => { stderr = `${stderr}${chunk.toString("utf8")}`.slice(-2000); });
  const url = `http://127.0.0.1:${number}`;
  const deadline = Date.now() + 20_000;
  while (Date.now() < deadline) {
    if (child.exitCode !== null || child.signalCode !== null) throw new Error(`Evaluation dashboard exited: ${stderr}`);
    try { if ((await fetch(`${url}/api/v1/health`)).ok) return { process: child, url }; } catch { /* loopback startup */ }
    await new Promise((resolveDelay) => setTimeout(resolveDelay, 100));
  }
  child.kill("SIGTERM");
  throw new Error(`Evaluation dashboard did not start: ${stderr}`);
}

async function stop(server?: Server): Promise<void> {
  if (!server || server.process.exitCode !== null || server.process.signalCode !== null) return;
  server.process.kill("SIGTERM");
  await Promise.race([once(server.process, "exit"), new Promise((resolveDelay) => setTimeout(resolveDelay, 5000))]);
  if (server.process.exitCode === null && server.process.signalCode === null) {
    server.process.kill("SIGKILL");
    await Promise.race([once(server.process, "exit"), new Promise((resolveDelay) => setTimeout(resolveDelay, 5000))]);
  }
}

function remove(path: string): void {
  const metadata = lstatSync(path);
  if (metadata.isSymbolicLink()) return;
  chmodSync(path, metadata.isDirectory() ? 0o700 : 0o600);
  if (metadata.isDirectory()) for (const entry of readdirSync(path)) remove(join(path, entry));
}

async function summaries(page: Page, url: string, token?: string): Promise<Summary[]> {
  const response = await page.request.get(`${url}/api/v1/evaluation-reports`, token ? { headers: { Authorization: `Bearer ${token}` } } : {});
  expect(response.status()).toBe(200);
  expect(response.headers()["cache-control"]).toBe("no-store");
  const body = await response.json() as { reports: Summary[]; rejected: unknown[] };
  expect(body.rejected).toEqual([]);
  expect(body.reports).toHaveLength(8);
  return body.reports;
}

function lookup(fixture: Fixture, list: Summary[], name: string): Summary {
  const found = list.find((item) => item.report_sha256 === fixture.reports[name]?.expected_sha256);
  if (!found) throw new Error(`Missing synthetic fixture ${name}`);
  return found;
}

async function openReport(page: Page, server: Server, report: Summary): Promise<void> {
  await page.goto(`${server.url}/evaluations/${report.report_id}`);
  await expect(page.getByRole("heading", { name: report.label, exact: true })).toBeVisible();
  await expect(page.getByText(TRUST, { exact: true })).toBeVisible();
}

async function noOverflow(page: Page): Promise<void> {
  const sizes = await page.evaluate(() => ({
    body: document.body.scrollWidth, document: document.documentElement.scrollWidth, width: innerWidth,
    overflow: Array.from(document.querySelectorAll("main *")).filter((element) => element.getBoundingClientRect().right > innerWidth).slice(0, 20).map((element) => ({ tag: element.tagName, class: element.className, width: element.getBoundingClientRect().width, right: element.getBoundingClientRect().right })),
  }));
  expect(sizes.body, JSON.stringify(sizes.overflow)).toBeLessThanOrEqual(sizes.width);
  expect(sizes.document, JSON.stringify(sizes.overflow)).toBeLessThanOrEqual(sizes.width);
}

test("real reports index, filter, trial and block selectors preserve authoritative values", async ({ page }) => {
  const fixture = prepare();
  let server: Server | undefined;
  try {
    server = await start(fixture);
    const list = await summaries(page, server.url);
    const study = lookup(fixture, list, "study-complete");
    const cache = lookup(fixture, list, "cache-complete");
    await page.goto(`${server.url}/evaluations`);
    await expect(page.getByRole("heading", { name: "Evaluations", exact: true })).toBeVisible();
    await page.getByLabel("Report kind").selectOption("STUDY");
    await expect(page.getByRole("link", { name: study.label, exact: true })).toBeVisible();
    await expect(page.getByRole("link", { name: cache.label, exact: true })).toHaveCount(0);
    await page.getByRole("link", { name: study.label, exact: true }).click();
    await expect(page.getByText(TRUST, { exact: true })).toBeVisible();
    await expect(page.getByRole("main")).toContainText("33.333333");
    await expect(page.getByRole("main")).toContainText("27.777778");
    await page.getByLabel("Trial", { exact: true }).selectOption({ index: 3 });
    await expect(page.getByRole("main")).toContainText("0.833333");
    await page.getByLabel("Population", { exact: true }).selectOption("background");
    await expect(page.getByLabel("Population", { exact: true })).toHaveValue("background");
    await page.reload();
    await expect(page.getByText(TRUST, { exact: true })).toBeVisible();
    await page.goBack();
    await expect(page.getByRole("heading", { name: "Evaluations", exact: true })).toBeVisible();
    await page.getByLabel("Report kind").selectOption("PREFIX_CACHE");
    await page.getByRole("link", { name: cache.label, exact: true }).click();
    await expect(page.getByRole("main")).toContainText("33.333333");
    await expect(page.getByRole("main")).toContainText("11.111111");
    await expect(page.getByRole("main")).toContainText("22.222222");
    await page.getByLabel("Block", { exact: true }).selectOption({ index: 2 });
    await page.getByLabel("Cell", { exact: true }).selectOption("S1");
    await expect(page.getByRole("main")).toContainText("44.444444");
    await expect(page.getByRole("main")).toContainText("Synthetic only");
    await noOverflow(page);
    const text = await page.locator("body").innerText();
    for (const secret of [fixture.root, "127.0.0.1:8000", "private foreground", "case-0000", "preparation-0"]) expect(text).not.toContain(secret);
  } finally { await stop(server); remove(fixture.root); rmSync(fixture.root, { recursive: true, force: true }); }
});

test("mobile report journeys remain legible, keyboard reachable and theme safe at 390 and 320", async ({ page }) => {
  const fixture = prepare();
  let server: Server | undefined;
  try {
    server = await start(fixture);
    const list = await summaries(page, server.url);
    for (const width of [390, 320]) {
      await page.setViewportSize({ width, height: 844 });
      for (const name of ["study-complete", "cache-complete"]) {
        await openReport(page, server, lookup(fixture, list, name));
        await noOverflow(page);
        const selector = page.getByLabel(name.startsWith("study") ? "Trial" : "Block", { exact: true });
        await selector.focus();
        await expect(selector).toBeFocused();
        await page.keyboard.press("ArrowDown");
        await page.keyboard.press("Enter");
        await expect(page.getByRole("main")).toContainText("33.333333");
        await page.getByRole("button", { name: /Switch to .* theme/ }).click();
        await noOverflow(page);
      }
      await page.goto(`${server.url}/evaluations`);
      await expect(page.getByRole("link", { name: lookup(fixture, list, "study-complete").label, exact: true })).toBeVisible();
      const navigation = page.getByRole("navigation").getByRole("link", { name: "Evaluations", exact: true });
      await navigation.focus();
      await expect(navigation).toBeFocused();
      await noOverflow(page);
    }
  } finally { await stop(server); remove(fixture.root); rmSync(fixture.root, { recursive: true, force: true }); }
});

test("eight complete cache blocks display exact descriptive intervals and select block 8", async ({ page }) => {
  const fixture = prepare("complete", 8);
  let server: Server | undefined;
  try {
    const source = JSON.parse(readFileSync(fixture.reports["cache-complete"].report_path, "utf8")) as {
      coverage: { complete_blocks: number; returned_records: number };
      contrasts: { interval_status: string; interval_rps: { lower: { decimal: string }; upper: { decimal: string } }; mean_goodput_difference_rps: { decimal: string } }[];
    };
    expect(source.coverage.complete_blocks).toBe(8);
    expect(source.coverage.returned_records).toBe(128);
    const exactValues = ["33.333333", "11.111111", "22.222222"];
    expect(source.contrasts.map((row) => [row.mean_goodput_difference_rps.decimal, row.interval_rps.lower.decimal, row.interval_rps.upper.decimal, row.interval_status])).toEqual(
      exactValues.map((value) => [value, value, value, "DESCRIPTIVE_ONLY"]),
    );
    server = await start(fixture);
    const report = lookup(fixture, await summaries(page, server.url), "cache-complete");
    const response = await page.request.get(`${server.url}/api/v1/evaluation-reports/${report.report_id}`);
    expect(response.status()).toBe(200);
    const projected = await response.json() as { blocks: unknown[]; contrasts: { label: string; mean_rps: string; lower_rps: string; upper_rps: string; interval_status: string }[] };
    expect(projected.blocks).toHaveLength(8);
    expect(projected.contrasts.map((row) => [row.mean_rps, row.lower_rps, row.upper_rps, row.interval_status])).toEqual(
      exactValues.map((value) => [value, value, value, "DESCRIPTIVE_ONLY"]),
    );
    await openReport(page, server, report);
    const table = page.getByRole("table", { name: "Authoritative descriptive contrasts in requests/s", exact: true });
    for (const contrast of projected.contrasts) {
      const row = table.getByRole("row").filter({ has: page.getByRole("rowheader", { name: contrast.label, exact: true }) });
      await expect(row).toContainText(`[${contrast.lower_rps}, ${contrast.upper_rps}] requests/s`);
      await expect(row).toContainText(/descriptive only/i);
    }
    await page.getByLabel("Block", { exact: true }).selectOption({ index: 7 });
    await expect(page.getByRole("table", { name: "Block 8: all four cache conditions", exact: true })).toBeVisible();
    await page.getByLabel("Cell", { exact: true }).selectOption("S1");
    await expect(page.getByRole("heading", { name: "S1 · Cell 32 details", exact: true })).toBeVisible();
    await expect(page.getByRole("main")).toContainText("44.444444");
    await expect(page.getByRole("main")).toContainText("Synthetic only");
    await expect(page.getByRole("main")).toContainText("Runtime unverified");
    await expect(page.getByText("Low-replication rehearsal / pilot", { exact: true })).toHaveCount(0);
    await page.reload();
    await expect(page.getByRole("table", { name: "Authoritative descriptive contrasts in requests/s", exact: true })).toContainText("[33.333333, 33.333333] requests/s");
  } finally { await stop(server); remove(fixture.root); rmSync(fixture.root, { recursive: true, force: true }); }
});

test("cancelled, invalid-cell and partial reports keep suppression distinct from measured zero", async ({ page }) => {
  const fixture = prepare();
  let server: Server | undefined;
  try {
    server = await start(fixture);
    const list = await summaries(page, server.url);
    for (const name of ["study-partial", "study-cancelled", "cache-cancelled", "cache-invalid"]) {
      await openReport(page, server, lookup(fixture, list, name));
      await expect(page.getByRole("main")).toContainText(/SUPPRESSED|Suppressed|suppressed/);
      if (name.includes("cancelled")) await expect(page.getByRole("main")).toContainText(/CANCELLED|Cancelled|cancelled/);
      if (name === "cache-invalid") await expect(page.getByRole("main")).toContainText(/INVALID|Invalid|invalid/);
    }
    await openReport(page, server, lookup(fixture, list, "cache-zero"));
    await expect(page.getByRole("main")).toContainText("0.000000");
    await expect(page.getByRole("main")).toContainText(/HTTP_ERROR|HTTP error|Http error/);
    await expect(page.getByText("No returned measurements", { exact: true })).toHaveCount(0);
    await openReport(page, server, lookup(fixture, list, "study-empty"));
    await expect(page.getByText("No returned measurements", { exact: true })).toBeVisible();
    await expect(page.getByRole("main")).toContainText(/Unavailable|unavailable/);
    await expect(page.getByLabel("Trial", { exact: true })).toHaveCount(0);
  } finally { await stop(server); remove(fixture.root); rmSync(fixture.root, { recursive: true, force: true }); }
});

test("source removal and changed pinned bytes invalidate stale detail and recover after restoration", async ({ page }) => {
  const fixture = prepare();
  let server: Server | undefined;
  try {
    server = await start(fixture);
    const report = lookup(fixture, await summaries(page, server.url), "cache-complete");
    await openReport(page, server, report);
    const path = fixture.reports["cache-complete"].report_path;
    const bytes = readFileSync(path);
    renameSync(path, `${path}.removed`);
    const refreshResponse = page.waitForResponse((response) => new URL(response.url()).pathname === `/api/v1/evaluation-reports/${report.report_id}`);
    await page.getByRole("button", { name: "Refresh report", exact: true }).click();
    expect((await refreshResponse).status()).toBe(404);
    await expect(page.getByRole("heading", { name: report.label, exact: true })).toHaveCount(0);
    await expect(page.getByText(TRUST, { exact: true })).toHaveCount(0);
    const unavailable = await page.request.get(`${server.url}/api/v1/evaluation-reports/${report.report_id}`);
    expect(unavailable.status()).toBe(404);
    expect(await unavailable.text()).not.toContain(fixture.root);
    renameSync(`${path}.removed`, path);
    chmodSync(path, 0o600);
    writeFileSync(path, Buffer.concat([bytes, Buffer.from(" ")]));
    const changedResponse = page.waitForResponse((response) => new URL(response.url()).pathname === `/api/v1/evaluation-reports/${report.report_id}`);
    await page.reload();
    expect((await changedResponse).status()).toBe(404);
    await expect(page.getByText(TRUST, { exact: true })).toHaveCount(0);
    await page.goto(`${server.url}/evaluations`);
    await expect(page.getByRole("main")).toContainText(/withheld/i);
    await expect(page.getByRole("link", { name: report.label, exact: true })).toHaveCount(0);
    writeFileSync(path, bytes);
    await page.getByRole("button", { name: "Refresh reports", exact: true }).click();
    await page.getByRole("link", { name: report.label, exact: true }).click();
    await expect(page.getByText(TRUST, { exact: true })).toBeVisible();
  } finally { await stop(server); remove(fixture.root); rmSync(fixture.root, { recursive: true, force: true }); }
});

test("actual publication at zero remains distinct from censored decision and dispatch recovery", async ({ page }) => {
  const fixture = prepare("censored");
  let server: Server | undefined;
  try {
    server = await start(fixture);
    await openReport(page, server, lookup(fixture, await summaries(page, server.url), "study-complete"));
    await page.getByLabel("Trial", { exact: true }).selectOption({ index: 1 });
    const table = page.getByRole("table", { name: "Publication, decision and dispatch recovery from actual restoration" });
    const publication = table.getByRole("row").filter({ has: page.getByRole("rowheader", { name: "Publication", exact: true }) });
    await expect(publication).toContainText(/observed/i);
    await expect(publication).toContainText("0 ns");
    for (const name of ["Decision", "Dispatch"]) {
      const row = table.getByRole("row").filter({ has: page.getByRole("rowheader", { name, exact: true }) });
      await expect(row).toContainText(/unobserved or censored/i);
      await expect(row).toContainText("Unavailable");
      await expect(row).toContainText("60000000 ns");
    }
  } finally { await stop(server); remove(fixture.root); rmSync(fixture.root, { recursive: true, force: true }); }
});

test("an empty catalog stays empty and malformed configuration recovers through a real retry", async ({ page }) => {
  const fixture = prepare();
  let server: Server | undefined;
  try {
    const original = readFileSync(fixture.catalog);
    writeFileSync(fixture.catalog, JSON.stringify({ entries: [], schema_version: "inferdrome.dashboard-evaluation-reports-catalog.v1" }) + "\n");
    server = await start(fixture);
    await page.goto(`${server.url}/evaluations`);
    await expect(page.getByText("No available evaluation reports", { exact: true })).toBeVisible();
    writeFileSync(fixture.catalog, '{"private-configuration-secret":');
    await page.getByRole("button", { name: "Refresh reports", exact: true }).click();
    await expect(page.getByText("The evaluation-report index is unavailable", { exact: true })).toBeVisible();
    await expect(page.getByRole("main")).not.toContainText("private-configuration-secret");
    await expect(page.getByRole("main")).not.toContainText(fixture.root);
    const response = await page.request.get(`${server.url}/api/v1/evaluation-reports`);
    expect(response.status()).toBe(503);
    expect(response.headers()["cache-control"]).toBe("no-store");
    writeFileSync(fixture.catalog, original);
    await page.getByRole("button", { name: "Try again", exact: true }).click();
    await expect(page.getByRole("link", { name: "Study report 1", exact: true })).toBeVisible();
  } finally { await stop(server); remove(fixture.root); rmSync(fixture.root, { recursive: true, force: true }); }
});

test("an old real-backend detail response cannot replace a newly selected report", async ({ page }) => {
  const fixture = prepare();
  let server: Server | undefined;
  try {
    server = await start(fixture);
    const list = await summaries(page, server.url);
    const study = lookup(fixture, list, "study-complete");
    const cache = lookup(fixture, list, "cache-complete");
    let release: (() => void) | undefined;
    let fetched: (() => void) | undefined;
    const held = new Promise<void>((resolveHeld) => { release = resolveHeld; });
    const started = new Promise<void>((resolveStarted) => { fetched = resolveStarted; });
    await page.route(`**/api/v1/evaluation-reports/${study.report_id}`, async (route) => {
      const response = await route.fetch();
      fetched?.();
      await held;
      await route.fulfill({ response }).catch(() => undefined);
    });
    await page.goto(`${server.url}/evaluations/${study.report_id}`);
    await started;
    await page.getByRole("navigation").getByRole("link", { name: "Evaluations", exact: true }).click();
    await page.getByRole("link", { name: cache.label, exact: true }).click();
    await expect(page.getByRole("heading", { name: cache.label, exact: true })).toBeVisible();
    release?.();
    await expect(page.getByRole("heading", { name: study.label, exact: true })).toHaveCount(0);
    await expect(page.getByLabel("Block", { exact: true })).toBeVisible();
  } finally { await page.unrouteAll({ behavior: "ignoreErrors" }); await stop(server); remove(fixture.root); rmSync(fixture.root, { recursive: true, force: true }); }
});

test("protected report views relock on revocation and recover with a replacement key", async ({ page }) => {
  const fixture = prepare();
  let server: Server | undefined;
  try {
    const keyring = join(fixture.root, "dashboard-keyring.json");
    const keyCommand = (...args: string[]) => {
      const result = spawnSync(python(), ["-m", "inferdrome", "dashboard-keyring", ...args, "--keyring", keyring], {
        cwd: ROOT, env: environment(), encoding: "utf8", timeout: 20_000,
      });
      if (result.error || result.status !== 0) throw new Error("Evaluation dashboard keyring operation failed");
      return JSON.parse(result.stdout) as { token: string; key_id: string };
    };
    const initial = keyCommand("create", "--label", "evaluation-playwright");
    server = await start(fixture, keyring);
    const report = lookup(fixture, await summaries(page, server.url, initial.token), "study-complete");
    for (const path of ["/api/v1/evaluation-reports", `/api/v1/evaluation-reports/${report.report_id}`]) {
      const response = await page.request.get(`${server.url}${path}`);
      expect(response.status()).toBe(401);
      expect(response.headers()["cache-control"]).toBe("no-store");
    }
    await page.goto(`${server.url}/evaluations/${report.report_id}`);
    await expect(page.getByRole("heading", { name: "Unlock evidence views" })).toBeVisible();
    await page.getByLabel("Bearer token").fill(initial.token);
    await page.getByRole("button", { name: "Unlock dashboard", exact: true }).click();
    await expect(page.getByText(TRUST, { exact: true })).toBeVisible();
    const replacement = keyCommand("rotate", "--revoke-key-id", initial.key_id, "--label", "evaluation-replacement");
    await page.getByRole("button", { name: "Refresh report", exact: true }).click();
    await expect(page.getByRole("heading", { name: "Unlock evidence views" })).toBeVisible();
    await expect(page.getByText(TRUST, { exact: true })).toHaveCount(0);
    await expect(page.getByRole("heading", { name: report.label, exact: true })).toHaveCount(0);
    await page.getByLabel("Bearer token").fill(replacement.token);
    await page.getByRole("button", { name: "Unlock dashboard", exact: true }).click();
    await expect(page.getByText(TRUST, { exact: true })).toBeVisible();
    const persisted = await page.evaluate(() => JSON.stringify({ local: localStorage, session: sessionStorage, cookie: document.cookie, url: location.href, body: document.body.innerText }));
    for (const secret of [initial.token, replacement.token, fixture.root]) expect(persisted).not.toContain(secret);
  } finally { await stop(server); remove(fixture.root); rmSync(fixture.root, { recursive: true, force: true }); }
});
