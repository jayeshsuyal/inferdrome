import { expect, test, type Page } from "@playwright/test";
import { spawn, spawnSync, type ChildProcess } from "node:child_process";
import { once } from "node:events";
import {
  chmodSync,
  existsSync,
  lstatSync,
  mkdtempSync,
  readdirSync,
  rmSync,
} from "node:fs";
import { createServer } from "node:net";
import { tmpdir } from "node:os";
import {
  delimiter,
  dirname,
  isAbsolute,
  join,
  resolve,
  sep,
} from "node:path";
import { fileURLToPath } from "node:url";

const REPOSITORY_ROOT = resolve(dirname(fileURLToPath(import.meta.url)), "../..");
const SOURCE_ROOT = join(REPOSITORY_ROOT, "src");
const FIXTURE_PREFIX = "inferdrome-dashboard-e2e-";
const PLAN_ID = "comparison-plan-eeeeeeeeeeeeeeeeeeeeeeeeeeeeeeee";
const BASELINE_TRIAL_SET_ID = "trial-set-bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb";
const CANDIDATE_TRIAL_SET_ID = "trial-set-cccccccccccccccccccccccccccccccc";

interface CreatedPlan {
  readonly comparison_plan_digest: string;
  readonly path: string;
}

interface ExecutedPlan {
  readonly executed_run_ids: readonly string[];
  readonly planned_run_count: number;
}

interface FixturePaths {
  readonly root: string;
  readonly runs: string;
  readonly trialSets: string;
  readonly comparisonPlans: string;
  readonly comparisonResults: string;
}

let fixture: FixturePaths | undefined;
let dashboardServer: ChildProcess | undefined;
let dashboardServerLog = "";
let baseUrl = "";

function pythonExecutable(): string {
  const configured = process.env.INFERDROME_PYTHON?.trim();
  if (configured) {
    return configured.includes(sep) && !isAbsolute(configured)
      ? resolve(REPOSITORY_ROOT, configured)
      : configured;
  }
  const projectPython = join(REPOSITORY_ROOT, ".venv", "bin", "python");
  return existsSync(projectPython) ? projectPython : "python3";
}

function inferdromeEnvironment(): NodeJS.ProcessEnv {
  return {
    ...process.env,
    PYTHONPATH: [SOURCE_ROOT, process.env.PYTHONPATH].filter(Boolean).join(delimiter),
  };
}

function runInferdromeJson<T>(arguments_: readonly string[]): T {
  const result = spawnSync(
    pythonExecutable(),
    ["-m", "inferdrome", ...arguments_],
    {
      cwd: REPOSITORY_ROOT,
      encoding: "utf8",
      env: inferdromeEnvironment(),
      maxBuffer: 10 * 1024 * 1024,
      timeout: 90_000,
    },
  );
  if (result.error) throw result.error;
  if (result.status !== 0) {
    throw new Error(
      `Inferdrome command failed (${arguments_.slice(0, 2).join(" ")}):\n${result.stderr || result.stdout}`,
    );
  }
  try {
    return JSON.parse(result.stdout) as T;
  } catch (error) {
    throw new Error(
      `Inferdrome command did not return JSON:\n${result.stdout}`,
      { cause: error },
    );
  }
}

function createPopulatedFixture(): FixturePaths {
  const root = mkdtempSync(join(tmpdir(), FIXTURE_PREFIX));
  const paths: FixturePaths = {
    root,
    runs: join(root, "runs"),
    trialSets: join(root, "trial-sets"),
    comparisonPlans: join(root, "comparison-plans"),
    comparisonResults: join(root, "comparison-results"),
  };
  const baselineSource = join(
    REPOSITORY_ROOT,
    "examples",
    "controlled-concurrency-2.yaml",
  );
  const candidateSource = join(
    REPOSITORY_ROOT,
    "examples",
    "controlled-concurrency-4.yaml",
  );
  try {
    const created = runInferdromeJson<CreatedPlan>([
      "comparison-plan",
      "create",
      "--baseline-source",
      baselineSource,
      "--candidate-source",
      candidateSource,
      "--title",
      "Browser release comparison",
      "--hypothesis",
      "Concurrency may change attempted throughput.",
      "--repetitions",
      "2",
      "--primary-outcome",
      "attempted_request_throughput_per_s:rate",
      "--comparison-plan-id",
      PLAN_ID,
      "--baseline-trial-set-id",
      BASELINE_TRIAL_SET_ID,
      "--candidate-trial-set-id",
      CANDIDATE_TRIAL_SET_ID,
      "--schedule-seed",
      "0".repeat(64),
      "--runs-root",
      paths.runs,
      "--comparison-plans-root",
      paths.comparisonPlans,
    ]);
    const executed = runInferdromeJson<ExecutedPlan>([
      "comparison-plan",
      "execute",
      created.path,
      "--expected-digest",
      created.comparison_plan_digest,
      "--baseline-source",
      baselineSource,
      "--candidate-source",
      candidateSource,
      "--runs-root",
      paths.runs,
      "--trial-sets-root",
      paths.trialSets,
      "--comparison-results-root",
      paths.comparisonResults,
    ]);
    if (executed.planned_run_count !== 4 || executed.executed_run_ids.length !== 4) {
      throw new Error("The populated dashboard fixture did not execute all four planned runs.");
    }
    return paths;
  } catch (error) {
    removeFixture(paths);
    throw error;
  }
}

async function availablePort(): Promise<number> {
  return await new Promise((resolvePort, reject) => {
    const server = createServer();
    server.unref();
    server.once("error", reject);
    server.listen(0, "127.0.0.1", () => {
      const address = server.address();
      if (!address || typeof address === "string") {
        server.close();
        reject(new Error("Could not allocate a loopback port for the dashboard."));
        return;
      }
      server.close((error) => {
        if (error) reject(error);
        else resolvePort(address.port);
      });
    });
  });
}

function appendServerLog(chunk: Buffer): void {
  dashboardServerLog = `${dashboardServerLog}${chunk.toString("utf8")}`.slice(-20_000);
}

async function startDashboardServer(paths: FixturePaths): Promise<string> {
  const port = await availablePort();
  dashboardServerLog = "";
  let spawnError: Error | undefined;
  dashboardServer = spawn(
    pythonExecutable(),
    [
      "-m",
      "inferdrome",
      "dashboard",
      "--runs-root",
      paths.runs,
      "--trial-sets-root",
      paths.trialSets,
      "--comparison-plans-root",
      paths.comparisonPlans,
      "--comparison-results-root",
      paths.comparisonResults,
      "--port",
      String(port),
    ],
    {
      cwd: REPOSITORY_ROOT,
      env: inferdromeEnvironment(),
      stdio: ["ignore", "pipe", "pipe"],
    },
  );
  dashboardServer.once("error", (error) => {
    spawnError = error;
  });
  dashboardServer.stdout?.on("data", appendServerLog);
  dashboardServer.stderr?.on("data", appendServerLog);

  const url = `http://127.0.0.1:${port}`;
  const deadline = Date.now() + 20_000;
  while (Date.now() < deadline) {
    if (spawnError) throw spawnError;
    if (dashboardServer.exitCode !== null || dashboardServer.signalCode !== null) {
      throw new Error(`The dashboard server exited before startup.\n${dashboardServerLog}`);
    }
    try {
      const response = await fetch(`${url}/api/v1/health`);
      if (response.ok) return url;
    } catch {
      // The loopback socket is expected to refuse connections while uvicorn starts.
    }
    await new Promise((resolveDelay) => setTimeout(resolveDelay, 100));
  }
  throw new Error(`The dashboard server did not become ready.\n${dashboardServerLog}`);
}

async function stopDashboardServer(): Promise<void> {
  const server = dashboardServer;
  dashboardServer = undefined;
  if (!server || server.exitCode !== null || server.signalCode !== null) return;
  server.kill("SIGTERM");
  await Promise.race([
    once(server, "exit"),
    new Promise((resolveDelay) => setTimeout(resolveDelay, 5_000)),
  ]);
  if (server.exitCode === null && server.signalCode === null) {
    const forcedExit = once(server, "exit");
    server.kill("SIGKILL");
    await Promise.race([
      forcedExit,
      new Promise((resolveDelay) => setTimeout(resolveDelay, 5_000)),
    ]);
  }
  if (server.exitCode === null && server.signalCode === null) {
    throw new Error("The dashboard server did not stop after SIGKILL.");
  }
}

function makeTreeWritable(path: string): void {
  if (!existsSync(path)) return;
  const stat = lstatSync(path);
  if (stat.isSymbolicLink()) return;
  if (stat.isDirectory()) {
    chmodSync(path, 0o700);
    for (const entry of readdirSync(path)) makeTreeWritable(join(path, entry));
    return;
  }
  chmodSync(path, 0o600);
}

function removeFixture(paths: FixturePaths | undefined): void {
  if (!paths) return;
  const resolvedRoot = resolve(paths.root);
  const resolvedTemporaryRoot = `${resolve(tmpdir())}${sep}`;
  if (
    !resolvedRoot.startsWith(resolvedTemporaryRoot)
    || !resolvedRoot.split(sep).at(-1)?.startsWith(FIXTURE_PREFIX)
  ) {
    throw new Error(`Refusing to remove unexpected E2E fixture path: ${resolvedRoot}`);
  }
  makeTreeWritable(resolvedRoot);
  rmSync(resolvedRoot, { force: true, recursive: true });
}

function dashboardNavigation(page: Page) {
  return page.getByRole("navigation", { name: "Dashboard views" });
}

async function expectPath(page: Page, path: string): Promise<void> {
  await expect(page).toHaveURL(`${baseUrl}${path}`);
}

test.describe("populated dashboard", () => {
  test.beforeAll(async () => {
    try {
      fixture = createPopulatedFixture();
      baseUrl = await startDashboardServer(fixture);
    } catch (error) {
      await stopDashboardServer();
      removeFixture(fixture);
      fixture = undefined;
      throw error;
    }
  });

  test.afterAll(async () => {
    try {
      await stopDashboardServer();
    } finally {
      removeFixture(fixture);
      fixture = undefined;
    }
  });

  test("boots the real server and traverses every dashboard route", async ({ page }) => {
    const browserErrors: string[] = [];
    const failedResponses: string[] = [];
    const failedRequests: string[] = [];
    const unsafeApiRequests: string[] = [];
    const visitedByClick = new Set<string>();

    page.on("console", (message) => {
      if (message.type() === "error") browserErrors.push(message.text());
    });
    page.on("pageerror", (error) => browserErrors.push(error.message));
    page.on("response", (response) => {
      const url = new URL(response.url());
      if (url.origin === baseUrl && response.status() >= 400) {
        failedResponses.push(`${response.status()} ${url.pathname}`);
      }
    });
    page.on("requestfailed", (request) => {
      const errorText = request.failure()?.errorText ?? "unknown failure";
      if (errorText !== "net::ERR_ABORTED") {
        failedRequests.push(`${request.method()} ${request.url()} (${errorText})`);
      }
    });
    page.on("request", (request) => {
      const url = new URL(request.url());
      if (
        url.origin === baseUrl
        && url.pathname.startsWith("/api/")
        && request.method() !== "GET"
      ) {
        unsafeApiRequests.push(`${request.method()} ${url.pathname}`);
      }
    });

    const rootResponse = await page.goto(`${baseUrl}/`);
    expect(rootResponse?.status()).toBe(200);
    await expectPath(page, "/runs");
    await expect(page.getByRole("heading", { name: "Runs", level: 1 })).toBeVisible();
    await expect(page.getByRole("heading", { name: "Recent runs", level: 2 })).toBeVisible();
    await expect(page.locator(".runs-table tbody tr")).toHaveCount(4);

    const runLink = page.locator("a.latest-run-title");
    const runHref = await runLink.getAttribute("href");
    expect(runHref).toMatch(/^\/runs\/run-[0-9a-f]{32}$/);
    const runPath = runHref as string;
    const runId = decodeURIComponent(runPath.slice("/runs/".length));
    await runLink.click();
    await expectPath(page, runPath);
    visitedByClick.add(runPath);
    await expect(page.getByRole("heading", { name: runId, level: 1 })).toBeVisible();
    await expect(page.getByRole("heading", { name: "Observed distributions", level: 2 })).toBeVisible();

    await dashboardNavigation(page).getByRole("link", { name: "Evidence", exact: true }).click();
    const evidencePath = `/evidence/${runId}`;
    await expectPath(page, evidencePath);
    visitedByClick.add(evidencePath);
    await expect(page.getByRole("heading", { name: "Evidence", level: 1 })).toBeVisible();
    await expect(page.getByRole("heading", { name: "Offline verification", level: 2 })).toBeVisible();

    await dashboardNavigation(page).getByRole("link", { name: "Runs", exact: true }).click();
    await expectPath(page, "/runs");
    visitedByClick.add("/runs");
    await expect(page.locator(".runs-table tbody tr")).toHaveCount(4);

    await dashboardNavigation(page).getByRole("link", { name: "Trial sets", exact: true }).click();
    await expectPath(page, "/trial-sets");
    visitedByClick.add("/trial-sets");
    await expect(page.getByRole("heading", { name: "Verified trial sets", level: 2 })).toBeVisible();
    await expect(page.locator(".trial-set-table tbody tr")).toHaveCount(2);

    const trialSetLink = page.locator("a.trial-set-link:visible").first();
    const trialSetHref = await trialSetLink.getAttribute("href");
    expect(trialSetHref).toMatch(/^\/trial-sets\/trial-set-[0-9a-f]{32}$/);
    const trialSetPath = trialSetHref as string;
    await trialSetLink.click();
    await expectPath(page, trialSetPath);
    visitedByClick.add(trialSetPath);
    await expect(page.getByRole("heading", { name: "Run-to-run variation", level: 2 })).toBeVisible();
    await expect(page.getByRole("heading", { name: "Member runs", level: 2 })).toBeVisible();

    await dashboardNavigation(page).getByRole("link", { name: "Comparisons", exact: true }).click();
    await expectPath(page, "/comparisons");
    visitedByClick.add("/comparisons");
    await expect(page.getByRole("heading", { name: "Comparison plans", level: 2 })).toBeVisible();
    await expect(page.locator(".controlled-comparison-table tbody tr")).toHaveCount(1);

    const comparisonLink = page.locator("a.comparison-plan-link:visible").first();
    const comparisonHref = await comparisonLink.getAttribute("href");
    expect(comparisonHref).toBe(`/comparisons/${PLAN_ID}`);
    const comparisonPath = comparisonHref as string;
    await comparisonLink.click();
    await expectPath(page, comparisonPath);
    visitedByClick.add(comparisonPath);
    await expect(page.getByRole("heading", { name: "Browser release comparison", level: 1 })).toBeVisible();
    await expect(page.getByRole("heading", { name: "Comparability result", level: 2 })).toBeVisible();
    await expect(page.getByRole("heading", { name: "Arm evidence", level: 2 })).toBeVisible();

    await page.getByRole("link", { name: "Controlled comparisons", exact: true }).click();
    await expectPath(page, "/comparisons");
    await page.getByRole("link", { name: "Compare two runs", exact: true }).first().click();
    await expectPath(page, "/compare");
    visitedByClick.add("/compare");
    await expect(page.getByRole("heading", { name: "Compare runs", level: 1 })).toBeVisible();
    await expect(page.locator(".comparison-result")).toBeVisible();
    await expect(page.getByRole("heading", { name: "Configuration diff", level: 2 })).toBeVisible();

    const baseline = page.locator(".compare-controls select").nth(0);
    const candidate = page.locator(".compare-controls select").nth(1);
    await expect(baseline).toBeVisible();
    await expect(candidate).toBeVisible();
    const baselineBefore = await baseline.inputValue();
    const candidateBefore = await candidate.inputValue();
    await page.getByRole("button", { name: "Swap baseline and candidate" }).click();
    await expect(baseline).toHaveValue(candidateBefore);
    await expect(candidate).toHaveValue(baselineBefore);

    expect(visitedByClick).toEqual(new Set([
      "/runs",
      runPath,
      "/trial-sets",
      trialSetPath,
      "/comparisons",
      comparisonPath,
      "/compare",
      evidencePath,
    ]));

    const deepLinks = [
      ["/runs", "Runs"],
      [runPath, runId],
      ["/trial-sets", "Trial sets"],
      [trialSetPath, null],
      ["/comparisons", "Controlled comparisons"],
      [comparisonPath, "Browser release comparison"],
      ["/compare", "Compare runs"],
      ["/evidence", "Evidence"],
      [evidencePath, "Evidence"],
    ] as const;
    for (const [path, heading] of deepLinks) {
      const response = await page.goto(`${baseUrl}${path}`);
      expect(response?.status(), `deep link ${path}`).toBe(200);
      await expectPath(page, path);
      if (heading) {
        await expect(page.getByRole("heading", { name: heading, level: 1 })).toBeVisible();
      } else {
        await expect(page.getByRole("heading", { name: "Run-to-run variation", level: 2 })).toBeVisible();
      }
    }

    expect(browserErrors).toEqual([]);
    expect(failedResponses).toEqual([]);
    expect(failedRequests).toEqual([]);
    expect(unsafeApiRequests).toEqual([]);
  });
});
