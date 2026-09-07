import { expect, test, type Page } from "@playwright/test";
import { spawn, spawnSync, type ChildProcess } from "node:child_process";
import { once } from "node:events";
import {
  chmodSync,
  existsSync,
  lstatSync,
  mkdtempSync,
  readdirSync,
  realpathSync,
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
const ROUTING_CAMPAIGN_ID = "routing-campaign-v1";
const ROUTING_QUALIFICATION_ID = "stale-telemetry-qualification-v1";

interface LocalDemoSummary {
  readonly claim_boundary: string;
  readonly comparison: {
    readonly executed_run_ids: readonly string[];
    readonly planned_run_count: number;
    readonly reused_run_ids: readonly string[];
    readonly status: string;
  };
  readonly roots: {
    readonly comparison_plans: string;
    readonly comparison_results: string;
    readonly routing_campaign: string;
    readonly routing_qualification: string;
    readonly runs: string;
    readonly trial_sets: string;
  };
  readonly routing_campaign: {
    readonly campaign_id: string;
    readonly execution_mode: string;
    readonly retained_digest: string;
    readonly verified_by_replay: boolean;
  };
  readonly routing_qualification: {
    readonly qualification_id: string;
    readonly retained_digest: string;
    readonly source_package_retained_digest: string;
    readonly verified_by_source_replay: boolean;
    readonly verified_descriptor_binding: boolean;
  };
}

interface FixturePaths {
  readonly root: string;
  readonly runs: string;
  readonly trialSets: string;
  readonly comparisonPlans: string;
  readonly comparisonResults: string;
  readonly routingCampaigns: string;
  readonly routingQualifications: string;
  readonly routingQualificationDigest: string;
  readonly routingExecutionRoot: string;
  readonly routingExecutionDigest: string;
}

interface RoutingExecutionFixtureSummary {
  readonly root: string;
  readonly retained_digest: string;
  readonly fixture_provenance: "FIXTURE_GENERATED_LOCAL_NOT_PROVIDER_EVIDENCE";
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

function prepareLocalDemoFixture(root: string): LocalDemoSummary {
  const result = spawnSync(
    pythonExecutable(),
    [
      join(REPOSITORY_ROOT, "scripts", "run_local_demo.py"),
      "--workspace",
      root,
      "--prepare-only",
      "--json",
    ],
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
      `Inferdrome local demo preparation failed:\n${result.stderr || result.stdout}`,
    );
  }
  try {
    return JSON.parse(result.stdout) as LocalDemoSummary;
  } catch (error) {
    throw new Error(
      `Inferdrome local demo preparation did not return JSON:\n${result.stdout}`,
      { cause: error },
    );
  }
}

function prepareRoutingExecutionFixture(root: string): RoutingExecutionFixtureSummary {
  const result = spawnSync(
    pythonExecutable(),
    [
      join(REPOSITORY_ROOT, "scripts", "create_routing_execution_dashboard_fixture.py"),
      "--output-parent",
      root,
    ],
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
      `Routing-execution fixture preparation failed:\n${result.stderr || result.stdout}`,
    );
  }
  try {
    return JSON.parse(result.stdout) as RoutingExecutionFixtureSummary;
  } catch (error) {
    throw new Error(
      `Routing-execution fixture preparation did not return JSON:\n${result.stdout}`,
      { cause: error },
    );
  }
}

function createPopulatedFixture(): FixturePaths {
  // Qualification publication walks no-follow descriptors from `/`. Resolve
  // macOS's `/var` compatibility symlink before handing a temporary path to
  // that strict reader, just as the authenticated E2E fixture does.
  const root = realpathSync(mkdtempSync(join(tmpdir(), FIXTURE_PREFIX)));
  const paths: FixturePaths = {
    root,
    runs: join(root, "runs"),
    trialSets: join(root, "trial-sets"),
    comparisonPlans: join(root, "comparison-plans"),
    comparisonResults: join(root, "comparison-results"),
    routingCampaigns: join(root, "routing-campaign-v1"),
    routingQualifications: join(root, "stale-telemetry-qualification"),
    routingQualificationDigest: "",
    routingExecutionRoot: "",
    routingExecutionDigest: "",
  };
  try {
    const prepared = prepareLocalDemoFixture(root);
    if (
      prepared.claim_boundary !== "SYNTHETIC_ONLY"
      || prepared.comparison.status !== "INCOMPARABLE"
      || prepared.comparison.planned_run_count !== 4
      || prepared.comparison.executed_run_ids.length !== 4
      || prepared.comparison.reused_run_ids.length !== 0
      || prepared.routing_campaign.campaign_id !== ROUTING_CAMPAIGN_ID
      || prepared.routing_campaign.execution_mode !== "SYNTHETIC_CPU_ONLY"
      || !prepared.routing_campaign.retained_digest
      || prepared.routing_campaign.verified_by_replay !== true
      || prepared.routing_qualification.qualification_id !== ROUTING_QUALIFICATION_ID
      || !prepared.routing_qualification.retained_digest
      || prepared.routing_qualification.source_package_retained_digest !== prepared.routing_campaign.retained_digest
      || prepared.routing_qualification.verified_by_source_replay !== true
      || prepared.routing_qualification.verified_descriptor_binding !== true
    ) {
      throw new Error("The populated dashboard fixture did not execute all four planned runs.");
    }
    const preparedRoots = prepared.roots;
    if (
      resolve(preparedRoots.runs) !== resolve(paths.runs)
      || resolve(preparedRoots.trial_sets) !== resolve(paths.trialSets)
      || resolve(preparedRoots.comparison_plans) !== resolve(paths.comparisonPlans)
      || resolve(preparedRoots.comparison_results) !== resolve(paths.comparisonResults)
      || resolve(preparedRoots.routing_campaign) !== resolve(paths.routingCampaigns)
      || resolve(preparedRoots.routing_qualification) !== resolve(paths.routingQualifications)
    ) {
      throw new Error("The local demo prepared dashboard roots outside its fixture.");
    }
    const execution = prepareRoutingExecutionFixture(root);
    const expectedExecutionRoot = resolve(root, "routing-execution-package");
    if (
      execution.fixture_provenance !== "FIXTURE_GENERATED_LOCAL_NOT_PROVIDER_EVIDENCE"
      || resolve(execution.root) !== expectedExecutionRoot
      || !/^sha256:[0-9a-f]{64}$/.test(execution.retained_digest)
      || !existsSync(join(root, "fixture-provenance.json"))
    ) {
      throw new Error("The routing-execution fixture did not retain local-only provenance.");
    }
    return {
      ...paths,
      routingQualificationDigest: prepared.routing_qualification.retained_digest,
      routingExecutionRoot: execution.root,
      routingExecutionDigest: execution.retained_digest,
    };
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
      "--routing-campaigns-root",
      paths.routingCampaigns,
      "--routing-qualification-root",
      paths.routingQualifications,
      "--routing-qualification-digest",
      paths.routingQualificationDigest,
      "--routing-execution-root",
      paths.routingExecutionRoot,
      "--routing-execution-digest",
      paths.routingExecutionDigest,
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
  const resolvedTemporaryRoot = `${realpathSync(tmpdir())}${sep}`;
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
    await expect(page.getByText("Synthetic only", { exact: true }).first()).toBeVisible();

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
    await expect(page.getByRole("heading", { name: "Inferdrome local product demo", level: 1 })).toBeVisible();
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

    await dashboardNavigation(page).getByRole("link", { name: "Routing campaigns", exact: true }).click();
    await expectPath(page, "/routing-campaigns");
    visitedByClick.add("/routing-campaigns");
    await expect(page.getByRole("heading", { name: "Verified routing campaigns", level: 2 })).toBeVisible();
    const routingCampaignLink = page.locator("a.routing-campaign-link:visible").first();
    const routingCampaignHref = await routingCampaignLink.getAttribute("href");
    expect(routingCampaignHref).toBe(`/routing-campaigns/${ROUTING_CAMPAIGN_ID}`);
    const routingCampaignPath = routingCampaignHref as string;
    await routingCampaignLink.click();
    await expectPath(page, routingCampaignPath);
    visitedByClick.add(routingCampaignPath);
    await expect(page.getByRole("heading", { name: "Fault timeline", level: 2 })).toBeVisible();
    await expect(page.getByRole("heading", { name: "Cold reset receipt", level: 2 }).first()).toBeVisible();
    await expect(page.getByRole("heading", { name: "Complete terminal population", level: 2 }).first()).toBeVisible();
    await expect(page.getByText("Inadmissible", { exact: true }).first()).toBeVisible();
    await expect(page.getByText("REQUIRED_LOAD_STALE", { exact: true }).first()).toBeVisible();
    await expect(page.getByText("NO SAFE ROUTE", { exact: true }).first()).toBeVisible();
    await expect(page.getByText("endpoint-b", { exact: true }).first()).toBeVisible();

    await dashboardNavigation(page).getByRole("link", { name: "Routing executions", exact: true }).click();
    await expectPath(page, "/routing-executions");
    visitedByClick.add("/routing-executions");
    await expect(page.getByRole("heading", { name: "Routing executions", level: 1 })).toBeVisible();
    await expect(page.getByText("3 policy trials · 6 each", { exact: true })).toBeVisible();
    await expect(page.getByText("Manual host declaration · not provider proof", { exact: true }).first()).toBeVisible();
    const executionLink = page.getByRole("link", { name: "routing-execution-v1" }).first();
    await executionLink.click();
    const executionPath = "/routing-executions/routing-execution-v1";
    await expectPath(page, executionPath);
    visitedByClick.add(executionPath);
    await expect(page.getByRole("heading", { name: "routing-execution-v1", level: 1 })).toBeVisible();
    await expect(page.getByText("Manual-host facts are operator-declared.", { exact: false })).toBeVisible();
    await expect(page.getByRole("heading", { name: "Observed admission conditions", level: 2 })).toBeVisible();
    await expect(page.getByText("GPU/DCGM and KV/cache retained as unavailable", { exact: true })).toBeVisible();
    await expect(page.getByText("REQUIRED_LOAD_STALE", { exact: true }).first()).toBeVisible();
    await expect(page.getByText("NOT_ASSERTED_SEPARATE_SERVING_ENGINE", { exact: true }).first()).toBeVisible();

    await dashboardNavigation(page).getByRole("link", { name: "Causal qualification", exact: true }).click();
    await expectPath(page, "/routing-qualifications");
    visitedByClick.add("/routing-qualifications");
    await expect(page.getByRole("heading", { name: "Verified qualification descriptors", level: 2 })).toBeVisible();
    const qualificationLink = page.locator("a.routing-qualification-link:visible").first();
    const qualificationHref = await qualificationLink.getAttribute("href");
    expect(qualificationHref).toBe(`/routing-qualifications/${ROUTING_QUALIFICATION_ID}`);
    const qualificationPath = qualificationHref as string;
    await qualificationLink.click();
    await expectPath(page, qualificationPath);
    visitedByClick.add(qualificationPath);
    await expect(page.getByRole("heading", { name: "What the router knew at the focal request", level: 2 })).toBeVisible();
    await expect(page.getByRole("heading", { name: "Separate terminal populations", level: 2 })).toBeVisible();
    await expect(page.getByText("REQUIRED_LOAD_STALE", { exact: true }).first()).toBeVisible();
    await expect(page.getByText("STALE_LOAD_FAIL_OPEN", { exact: true }).first()).toBeVisible();
    await expect(page.getByText("HEALTH_ONLY_TIE_BREAK", { exact: true }).first()).toBeVisible();
    await expect(page.getByText("Canonical digest matched", { exact: true })).toBeVisible();
    const downloadPromise = page.waitForEvent("download");
    await page.getByRole("button", { name: "Download verified descriptor" }).click();
    const download = await downloadPromise;
    expect(download.suggestedFilename()).toBe("stale-telemetry-qualification-v1.json");
    await page.getByRole("link", { name: "Open source receipts" }).click();
    await expectPath(page, `/routing-campaigns/${ROUTING_CAMPAIGN_ID}`);

    await page.setViewportSize({ width: 390, height: 844 });
    await page.goto(`${baseUrl}${executionPath}`);
    await expect(page.getByRole("heading", { name: "routing-execution-v1", level: 1 })).toBeVisible();
    const responsive = await page.evaluate(() => ({
      scrollWidth: document.documentElement.scrollWidth,
      clientWidth: document.documentElement.clientWidth,
    }));
    expect(responsive.scrollWidth).toBeLessThanOrEqual(responsive.clientWidth);

    expect(visitedByClick).toEqual(new Set([
      "/runs",
      runPath,
      "/trial-sets",
      trialSetPath,
      "/comparisons",
      comparisonPath,
      "/compare",
      evidencePath,
      "/routing-campaigns",
      routingCampaignPath,
      "/routing-executions",
      executionPath,
      "/routing-qualifications",
      qualificationPath,
    ]));

    const deepLinks = [
      ["/runs", "Runs"],
      [runPath, runId],
      ["/trial-sets", "Trial sets"],
      [trialSetPath, null],
      ["/comparisons", "Controlled comparisons"],
      [comparisonPath, "Inferdrome local product demo"],
      ["/compare", "Compare runs"],
      ["/routing-campaigns", "Routing campaigns"],
      [routingCampaignPath, ROUTING_CAMPAIGN_ID],
      ["/routing-executions", "Routing executions"],
      [executionPath, "routing-execution-v1"],
      ["/routing-qualifications", "Causal qualification"],
      [qualificationPath, ROUTING_QUALIFICATION_ID],
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
