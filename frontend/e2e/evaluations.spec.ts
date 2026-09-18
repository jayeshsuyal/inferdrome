import { expect, test, type Page, type TestInfo } from "@playwright/test";
import { spawn, spawnSync, type ChildProcess } from "node:child_process";
import { createHash } from "node:crypto";
import { once } from "node:events";
import { chmodSync, existsSync, lstatSync, mkdirSync, mkdtempSync, readFileSync, readdirSync, realpathSync, renameSync, rmSync, writeFileSync } from "node:fs";
import { createServer } from "node:net";
import { tmpdir } from "node:os";
import { delimiter, dirname, isAbsolute, join, resolve, sep } from "node:path";
import { fileURLToPath } from "node:url";

const ROOT = resolve(dirname(fileURLToPath(import.meta.url)), "../..");
const TRUST = "Pinned report; source inputs not replayed";

interface ReportFixture {
  readonly kind: "STUDY" | "PREFIX_CACHE" | "SGLANG_STUDY";
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
  readonly evidence_class: "SYNTHETIC_ONLY" | "LOCAL_MEASUREMENT_ONLY" | null;
  readonly returned_records: number;
  readonly engine_identity?: {
    readonly engine: "sglang";
    readonly engine_binding_sha256: string;
    readonly engine_choice_sha256: string;
  };
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

function prepareEngineCatalog(): Fixture {
  const root = realpathSync(mkdtempSync(join(tmpdir(), "inferdrome-engine-evaluation-ui-")));
  const result = spawnSync(python(), ["-m", "tests.sglang_dashboard_support", "--root", root], {
    cwd: ROOT, encoding: "utf8", env: environment(), timeout: 60_000, maxBuffer: 1024 * 1024,
  });
  if (result.error || result.status !== 0) throw new Error(`Native fake SGLang preparation failed: ${result.stderr}`);
  const fixture = JSON.parse(result.stdout) as Omit<Fixture, "root"> & { fixture_provenance: string };
  if (fixture.fixture_provenance !== "SYNTHETIC_ONLY") throw new Error("Expected synthetic native SGLang fixtures");
  mkdirSync(join(root, "runs"), { mode: 0o700 });
  return { ...fixture, root };
}

function prepareCpuLoopbackRehearsal(engine: "vllm" | "sglang" = "vllm"): Fixture {
  const root = realpathSync(mkdtempSync(join(tmpdir(), "inferdrome-load-rehearsal-ui-")));
  const result = spawnSync(python(), ["-m", "tests.load_calibration_rehearsal_dashboard_support", "--root", root, "--engine", engine], {
    cwd: ROOT, encoding: "utf8", env: environment(), timeout: 90_000, maxBuffer: 1024 * 1024,
  });
  if (result.error || result.status !== 0) throw new Error(`CPU loopback rehearsal preparation failed: ${result.stderr}`);
  const fixture = JSON.parse(result.stdout) as {
    fixture_provenance: string;
    catalog: string;
    report_sha256: string;
  };
  if (fixture.fixture_provenance !== "CPU_LOOPBACK_REHEARSAL_ONLY") throw new Error("Expected a CPU-loopback-only rehearsal fixture");
  return {
    root,
    catalog: fixture.catalog,
    reports: {
      rehearsal: {
        kind: engine === "sglang" ? "SGLANG_STUDY" : "STUDY",
        report_path: "",
        expected_sha256: fixture.report_sha256,
      },
    },
  };
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

async function engineSummaries(page: Page, url: string): Promise<Summary[]> {
  const response = await page.request.get(`${url}/api/v1/evaluation-reports`);
  expect(response.status()).toBe(200);
  expect(response.headers()["cache-control"]).toBe("no-store");
  const body = await response.json() as { projection_version: string; reports: Summary[]; rejected: unknown[] };
  expect(body.projection_version).toBe("inferdrome.evaluation-dashboard.v2");
  expect(body.rejected).toEqual([]);
  expect(body.reports).toHaveLength(2);
  return body.reports;
}

function canonicalBytes(value: unknown): Buffer {
  const ordered = (item: unknown): unknown => {
    if (Array.isArray(item)) return item.map(ordered);
    if (item !== null && typeof item === "object") return Object.fromEntries(Object.entries(item).sort(([a], [b]) => a < b ? -1 : a > b ? 1 : 0).map(([key, child]) => [key, ordered(child)]));
    return item;
  };
  return Buffer.from(`${JSON.stringify(ordered(value))}\n`);
}

function digest(content: Buffer): string {
  return `sha256:${createHash("sha256").update(content).digest("hex")}`;
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

interface RaceObservation {
  readonly responses: { status: number; path: string; busy: string | null; retryAfter: string | null }[];
  readonly pageErrors: string[];
  readonly visibleErrors: () => Promise<string[]>;
}

async function observeRefreshRace(page: Page): Promise<RaceObservation> {
  const responses: { status: number; path: string; busy: string | null; retryAfter: string | null }[] = [];
  const pageErrors: string[] = [];
  page.on("response", (response) => {
    const path = new URL(response.url()).pathname;
    const headers = response.headers();
    if (path.startsWith("/api/v1/evaluation-reports")) responses.push({ status: response.status(), path, busy: headers["x-inferdrome-evaluation-busy"] ?? null, retryAfter: headers["retry-after"] ?? null });
  });
  page.on("pageerror", (error) => pageErrors.push(error.message));
  await page.evaluate(() => {
    const state = window as unknown as { evaluationRaceErrors: string[] };
    state.evaluationRaceErrors = [];
    const observer = new MutationObserver(() => {
      for (const title of ["The evaluation-report index is unavailable", "This evaluation report could not be opened"]) {
        if (document.body.innerText.includes(title) && !state.evaluationRaceErrors.includes(title)) state.evaluationRaceErrors.push(title);
      }
      if (location.pathname === "/evaluations" && document.querySelector('select[aria-label="Block"], select[aria-label="Trial"]')) {
        if (!state.evaluationRaceErrors.includes("Stale detail visible on index route")) state.evaluationRaceErrors.push("Stale detail visible on index route");
      }
    });
    observer.observe(document.body, { childList: true, subtree: true });
  });
  return { responses, pageErrors, visibleErrors: () => page.evaluate(() => (window as unknown as { evaluationRaceErrors: string[] }).evaluationRaceErrors) };
}

async function attachRace(testInfo: TestInfo, observations: readonly (RaceObservation | undefined)[]): Promise<void> {
  const values = [];
  for (const observation of observations) {
    if (observation) values.push({ responses: observation.responses, pageErrors: observation.pageErrors, visibleErrors: await observation.visibleErrors() });
  }
  await testInfo.attach("real-refresh-race-observations", { body: JSON.stringify(values, null, 2), contentType: "application/json" });
}

for (const engine of ["vllm", "sglang"] as const) test(`CPU loopback rehearsal: ${engine} native confirmation reaches the read-only browser`, async ({ page }) => {
  const fixture = prepareCpuLoopbackRehearsal(engine);
  let server: Server | undefined;
  try {
    server = await start(fixture);
    const response = await page.request.get(`${server.url}/api/v1/evaluation-reports`);
    expect(response.status()).toBe(200);
    const index = await response.json() as { reports: Summary[]; rejected: unknown[] };
    expect(index.rejected).toEqual([]);
    expect(index.reports).toHaveLength(1);
    const report = index.reports[0]!;
    expect(report.kind).toBe("STUDY");
    expect(report.evidence_class).toBe("SYNTHETIC_ONLY");
    expect(report.report_sha256).toBe(fixture.reports.rehearsal.expected_sha256);
    await page.goto(`${server.url}/evaluations/${report.report_id}`);
    await expect(page.getByRole("heading", { name: report.label, exact: true })).toBeVisible();
    await expect(page.getByText(TRUST, { exact: true })).toBeVisible();
    await expect(page.getByRole("main")).toContainText("Synthetic only");
    await expect(page.getByRole("main")).not.toContainText("127.0.0.1");
    if (engine === "sglang") {
      expect(report.engine_identity?.engine).toBe("sglang");
      await expect(page.getByRole("main")).toContainText("SGLang 0.5.18");
      await expect(page.getByTitle(report.engine_identity!.engine_binding_sha256, { exact: true })).toBeVisible();
      await page.reload();
      await expect(page.getByText(TRUST, { exact: true })).toBeVisible();
    } else expect(report.engine_identity).toBeUndefined();
  } finally {
    await stop(server);
    remove(fixture.root);
    rmSync(fixture.root, { recursive: true, force: true });
  }
});

test("engine-bound SGLang and legacy reports retain identity, values and navigation on desktop and mobile", async ({ page }, testInfo) => {
  const fixture = prepareEngineCatalog();
  let server: Server | undefined;
  try {
    server = await start(fixture);
    const list = await engineSummaries(page, server.url);
    const sglang = lookup(fixture, list, "sglang-study");
    const legacy = lookup(fixture, list, "legacy-study");
    expect(sglang.engine_identity?.engine).toBe("sglang");
    expect(legacy.engine_identity).toBeUndefined();
    expect(sglang.returned_records).toBe(24);
    const identity = sglang.engine_identity!;
    for (const width of [1280, 390, 320]) {
      await page.setViewportSize({ width, height: 844 });
      await page.goto(`${server.url}/evaluations`);
      await page.getByLabel("Report kind").selectOption("STUDY");
      for (const report of [sglang, legacy]) await expect(page.getByRole("link", { name: report.label, exact: true })).toBeVisible();
      await expect(page.getByRole("main")).toContainText("SGLang 0.5.18");
      await noOverflow(page);
      await page.getByRole("link", { name: sglang.label, exact: true }).click();
      await expect(page.getByText(TRUST, { exact: true })).toBeVisible();
      for (const text of ["SGLang 0.5.18", "Synthetic only", "Runtime unverified", "33.333333", "27.777778"]) await expect(page.getByRole("main")).toContainText(text);
      for (const value of [identity.engine_binding_sha256, identity.engine_choice_sha256]) await expect(page.getByTitle(value, { exact: true })).toBeVisible();
      await expect(page.getByRole("main")).toContainText(/scheduler-state age is unavailable/i);
      if (width === 1280 || width === 320) await page.screenshot({ path: testInfo.outputPath(`sglang-detail-${width}.png`), fullPage: true });
      const trial = page.getByLabel("Trial", { exact: true });
      await trial.focus();
      await expect(trial).toBeFocused();
      await trial.selectOption({ index: 3 });
      await expect(page.getByRole("main")).toContainText("0.833333");
      await page.getByLabel("Population", { exact: true }).selectOption("background");
      await expect(page.getByLabel("Population", { exact: true })).toHaveValue("background");
      await noOverflow(page);
      await page.reload();
      await expect(page.getByTitle(identity.engine_binding_sha256, { exact: true })).toBeVisible();
      await page.goBack();
      await page.getByRole("link", { name: legacy.label, exact: true }).click();
      await expect(page.getByText(TRUST, { exact: true })).toBeVisible();
      await expect(page.getByRole("main")).toContainText("33.333333");
      await expect(page.getByRole("main")).not.toContainText("SGLang 0.5.18");
      await expect(page.getByTitle(identity.engine_binding_sha256, { exact: true })).toHaveCount(0);
      await page.getByRole("button", { name: /Switch to .* theme/ }).click();
      await noOverflow(page);
    }
    const text = await page.getByRole("main").innerText();
    for (const privateValue of [fixture.root, "127.0.0.1", "private-model", "private foreground"]) expect(text).not.toContain(privateValue);
  } finally { await stop(server); remove(fixture.root); rmSync(fixture.root, { recursive: true, force: true }); }
});

test("engine-bound missing, mismatched, tampered and unsupported reports stay withheld and recover from exact bytes", async ({ page }) => {
  const fixture = prepareEngineCatalog();
  let server: Server | undefined;
  try {
    server = await start(fixture);
    const list = await engineSummaries(page, server.url);
    const sglang = lookup(fixture, list, "sglang-study");
    const legacy = lookup(fixture, list, "legacy-study");
    const path = fixture.reports["sglang-study"].report_path;
    const original = readFileSync(path);
    const originalCatalog = readFileSync(fixture.catalog);
    const cases = ["missing-file", "changed-pin", "binding-mismatch", "statistics-tamper", "missing-binding", "unsupported-version", "historical-marker"] as const;
    for (const variant of cases) {
      await openReport(page, server, sglang);
      chmodSync(path, 0o600);
      if (variant === "missing-file") renameSync(path, `${path}.removed`);
      else if (variant === "changed-pin") writeFileSync(path, Buffer.concat([original, Buffer.from(" ")]));
      else {
        const altered = JSON.parse(original.toString("utf8")) as Record<string, any>;
        if (variant === "binding-mismatch") {
          altered.engine_binding.config_sha256 = `sha256:${"f".repeat(64)}`;
          altered.engine_binding_sha256 = digest(canonicalBytes(altered.engine_binding));
        } else if (variant === "statistics-tamper") {
          altered.statistical_report.coverage.foreground.returned_records += 1;
          altered.statistical_report_sha256 = digest(canonicalBytes(altered.statistical_report));
        } else if (variant === "missing-binding") delete altered.engine_binding;
        else if (variant === "unsupported-version") altered.schema_version = "inferdrome.evaluation-study-report.v999";
        else altered.dashboard_projection = "UNSUPPORTED_ENGINE_BINDING";
        const bytes = canonicalBytes(altered);
        writeFileSync(path, bytes);
        const catalog = JSON.parse(originalCatalog.toString("utf8")) as { entries: { expected_sha256: string }[] };
        catalog.entries[1]!.expected_sha256 = digest(bytes);
        writeFileSync(fixture.catalog, canonicalBytes(catalog));
      }
      const response = page.waitForResponse((item) => new URL(item.url()).pathname === `/api/v1/evaluation-reports/${sglang.report_id}`);
      await page.getByRole("button", { name: "Refresh report", exact: true }).click();
      expect((await response).status()).toBe(404);
      await expect(page.getByRole("heading", { name: sglang.label, exact: true })).toHaveCount(0);
      await expect(page.getByText(TRUST, { exact: true })).toHaveCount(0);
      await expect(page.getByLabel("Trial", { exact: true })).toHaveCount(0);
      await expect(page.getByTitle(sglang.engine_identity!.engine_binding_sha256, { exact: true })).toHaveCount(0);
      const indexResponse = await page.request.get(`${server.url}/api/v1/evaluation-reports`);
      expect(indexResponse.status()).toBe(200);
      expect(indexResponse.headers()["cache-control"]).toBe("no-store");
      const index = await indexResponse.json() as { reports: Summary[]; rejected: { entry: number; code: string }[] };
      expect(index.reports.map((report) => report.report_id)).toEqual([legacy.report_id]);
      expect(index.rejected).toEqual([{ entry: 2, code: variant === "missing-file" ? "REPORT_UNAVAILABLE" : variant === "changed-pin" ? "DIGEST_MISMATCH" : "REPORT_INVALID", message: "Configured report was withheld." }]);
      await page.goto(`${server.url}/evaluations`);
      await expect(page.getByRole("main")).toContainText(/withheld/i);
      await expect(page.getByRole("link", { name: legacy.label, exact: true })).toBeVisible();
      await expect(page.getByRole("link", { name: sglang.label, exact: true })).toHaveCount(0);
      await expect(page.getByRole("main")).not.toContainText(fixture.root);
      if (variant === "missing-file") renameSync(`${path}.removed`, path);
      writeFileSync(path, original);
      writeFileSync(fixture.catalog, originalCatalog);
      await page.getByRole("button", { name: "Refresh reports", exact: true }).click();
      await page.getByRole("link", { name: sglang.label, exact: true }).click();
      await expect(page.getByText(TRUST, { exact: true })).toBeVisible();
      await expect(page.getByTitle(sglang.engine_identity!.engine_binding_sha256, { exact: true })).toBeVisible();
    }
  } finally { await stop(server); remove(fixture.root); rmSync(fixture.root, { recursive: true, force: true }); }
});

test("refresh recovery: rapid 25 ms double-click keeps the real index available", async ({ page }, testInfo) => {
  const fixture = prepare("complete", 8);
  let server: Server | undefined;
  let observed: RaceObservation | undefined;
  try {
    server = await start(fixture);
    await page.goto(`${server.url}/evaluations`);
    await expect(page.getByRole("link", { name: "Prefix-cache report 5", exact: true })).toBeVisible();
    observed = await observeRefreshRace(page);
    await page.getByRole("button", { name: "Refresh reports", exact: true }).dblclick({ delay: 25 });
    await expect(page.getByRole("link", { name: "Prefix-cache report 5", exact: true })).toBeVisible();
    await expect(page.getByRole("main")).toContainText("128 returned records");
    await expect(page.getByRole("button", { name: "Refresh reports", exact: true })).toBeEnabled();
    expect(await observed.visibleErrors()).toEqual([]);
    expect(observed.pageErrors).toEqual([]);
    expect(observed.responses.some((response) => response.status === 200)).toBe(true);
    expect(observed.responses.filter((response) => response.status === 503)).toEqual([]);
  } finally { await attachRace(testInfo, [observed]); await stop(server); remove(fixture.root); rmSync(fixture.root, { recursive: true, force: true }); }
});

test("refresh recovery: detail refresh then immediate navigation reaches the real index", async ({ page }, testInfo) => {
  const fixture = prepare("complete", 8);
  let server: Server | undefined;
  let observed: RaceObservation | undefined;
  try {
    server = await start(fixture);
    const report = lookup(fixture, await summaries(page, server.url), "cache-complete");
    await openReport(page, server, report);
    observed = await observeRefreshRace(page);
    await page.getByRole("button", { name: "Refresh report", exact: true }).click();
    await page.getByRole("navigation").getByRole("link", { name: "Evaluations", exact: true }).click();
    await expect(page).toHaveURL(`${server.url}/evaluations`);
    await expect(page.getByRole("link", { name: report.label, exact: true })).toBeVisible();
    await expect(page.getByRole("main")).toContainText("128 returned records");
    await expect(page.getByLabel("Block", { exact: true })).toHaveCount(0);
    expect(await observed.visibleErrors()).toEqual([]);
    expect(observed.pageErrors).toEqual([]);
    expect(observed.responses.some((response) => response.status === 200 && response.path === "/api/v1/evaluation-reports")).toBe(true);
  } finally { await attachRace(testInfo, [observed]); await stop(server); remove(fixture.root); rmSync(fixture.root, { recursive: true, force: true }); }
});

test("refresh recovery: overlapping browser tabs recover an actual busy response", async ({ page, context }, testInfo) => {
  const fixture = prepare("complete", 8);
  const other = await context.newPage();
  let server: Server | undefined;
  let first: RaceObservation | undefined;
  let second: RaceObservation | undefined;
  try {
    server = await start(fixture);
    const report = lookup(fixture, await summaries(page, server.url), "cache-complete");
    await page.goto(`${server.url}/evaluations`);
    await expect(page.getByRole("link", { name: report.label, exact: true })).toBeVisible();
    await openReport(other, server, report);
    first = await observeRefreshRace(page);
    second = await observeRefreshRace(other);
    await Promise.all([
      page.getByRole("button", { name: "Refresh reports", exact: true }).click(),
      other.getByRole("button", { name: "Refresh report", exact: true }).click(),
    ]);
    await expect(page.getByRole("link", { name: report.label, exact: true })).toBeVisible();
    await expect(other.getByRole("heading", { name: report.label, exact: true })).toBeVisible();
    await expect(page.getByRole("main")).toContainText("128 returned records");
    await expect(other.getByLabel("Block", { exact: true })).toBeVisible();
    expect([...first.responses, ...second.responses].some((response) => response.status === 503 && response.busy === "1" && response.retryAfter === "1")).toBe(true);
    expect(await first.visibleErrors()).toEqual([]);
    expect(await second.visibleErrors()).toEqual([]);
    expect([...first.pageErrors, ...second.pageErrors]).toEqual([]);
  } finally { await attachRace(testInfo, [first, second]); await other.close(); await stop(server); remove(fixture.root); rmSync(fixture.root, { recursive: true, force: true }); }
});

test("refresh recovery: a changed pin during an actual busy response stays withheld", async ({ page, context }, testInfo) => {
  const fixture = prepare("complete", 8);
  const other = await context.newPage();
  let server: Server | undefined;
  let first: RaceObservation | undefined;
  let second: RaceObservation | undefined;
  let deadline: ReturnType<typeof setTimeout> | undefined;
  try {
    server = await start(fixture);
    const report = lookup(fixture, await summaries(page, server.url), "cache-complete");
    await openReport(page, server, report);
    await openReport(other, server, report);
    first = await observeRefreshRace(page);
    second = await observeRefreshRace(other);
    const sourcePath = fixture.reports["cache-complete"].report_path;
    const originalBytes = readFileSync(sourcePath);
    let changed = false;
    const busyPage = new Promise<Page>((resolveBusy, reject) => {
      deadline = setTimeout(() => reject(new Error("The real overlapping scans did not return a busy response")), 10_000);
      for (const candidate of [page, other]) candidate.on("response", (response) => {
        if (!changed && response.status() === 503 && new URL(response.url()).pathname === `/api/v1/evaluation-reports/${report.report_id}`) {
          changed = true;
          chmodSync(sourcePath, 0o600);
          writeFileSync(sourcePath, Buffer.concat([originalBytes, Buffer.from(" ")]));
          if (deadline) clearTimeout(deadline);
          resolveBusy(candidate);
        }
      });
    });
    await Promise.all([
      page.getByRole("button", { name: "Refresh report", exact: true }).click(),
      other.getByRole("button", { name: "Refresh report", exact: true }).click(),
    ]);
    const withheld = await busyPage;
    await expect(withheld.getByText("This evaluation report could not be opened", { exact: true })).toBeVisible();
    await expect(withheld.getByText(TRUST, { exact: true })).toHaveCount(0);
    await expect(withheld.getByLabel("Block", { exact: true })).toHaveCount(0);
    await expect(withheld.getByRole("main")).not.toContainText(fixture.root);
    const observed = withheld === page ? first : second;
    expect(observed.responses.some((response) => response.status === 503 && response.busy === "1" && response.retryAfter === "1")).toBe(true);
    expect(observed.responses.some((response) => response.status === 404)).toBe(true);
    expect(observed.pageErrors).toEqual([]);
  } finally { if (deadline) clearTimeout(deadline); await attachRace(testInfo, [first, second]); await other.close(); await stop(server); remove(fixture.root); rmSync(fixture.root, { recursive: true, force: true }); }
});

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
