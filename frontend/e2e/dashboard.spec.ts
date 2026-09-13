import { expect, test, type Page } from "@playwright/test";
import { spawn, spawnSync, type ChildProcess } from "node:child_process";
import { once } from "node:events";
import {
  chmodSync,
  existsSync,
  lstatSync,
  mkdtempSync,
  readFileSync,
  readdirSync,
  realpathSync,
  renameSync,
  rmSync,
  writeFileSync,
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
import type { RunDetail, RunSummary, RunsPageResponse, TrialSetDetail, TrialSetPageResponse } from "../src/lib/types";

const REPOSITORY_ROOT = resolve(dirname(fileURLToPath(import.meta.url)), "../..");
const SOURCE_ROOT = join(REPOSITORY_ROOT, "src");
const FIXTURE_PREFIX = "inferdrome-dashboard-e2e-";
const PLAN_ID = "comparison-plan-eeeeeeeeeeeeeeeeeeeeeeeeeeeeeeee";
const ROUTING_CAMPAIGN_ID = "routing-campaign-v1";
const ROUTING_QUALIFICATION_ID = "stale-telemetry-qualification-v1";
const EXECUTION_PROFILES = [
  {
    label: "A100 v2",
    profileId: "lambda-manual-two-a100-pcie-40gb-v1",
    acceleratorModel: "NVIDIA A100-PCIE-40GB",
    manifestVersion: "inferdrome.routing-executed-manifest.v2",
  },
  {
    label: "H100 v3",
    profileId: "lambda-manual-two-h100-sxm5-80gb-v1",
    acceleratorModel: "NVIDIA H100-SXM5-80GB",
    manifestVersion: "inferdrome.routing-executed-manifest.v3",
  },
] as const;

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

function prepareRoutingExecutionFixture(root: string, profileId?: string): RoutingExecutionFixtureSummary {
  const result = spawnSync(
    pythonExecutable(),
    [
      join(REPOSITORY_ROOT, "scripts", "create_routing_execution_dashboard_fixture.py"),
      "--output-parent",
      root,
      ...(profileId ? ["--profile", profileId] : []),
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
    const execution = JSON.parse(result.stdout) as RoutingExecutionFixtureSummary;
    const provenance = JSON.parse(readFileSync(join(root, "fixture-provenance.json"), "utf8")) as Record<string, unknown>;
    if (
      execution.fixture_provenance !== "FIXTURE_GENERATED_LOCAL_NOT_PROVIDER_EVIDENCE"
      || resolve(execution.root) !== resolve(root, "routing-execution-package")
      || !/^sha256:[0-9a-f]{64}$/.test(execution.retained_digest)
      || provenance.fixture_provenance !== execution.fixture_provenance
      || provenance.package_retained_digest !== execution.retained_digest
      || provenance.provider_action !== "NONE"
      || provenance.docker_action !== "NONE"
      || provenance.gpu_action !== "NONE"
    ) {
      throw new Error("The routing-execution fixture did not retain local-only provenance.");
    }
    return execution;
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

  test("keeps a nonlatest selected run through trailing-slash navigation and reload", async ({ page }) => {
    const index = await (await page.request.get(`${baseUrl}/api/v1/runs`)).json() as RunsPageResponse;
    const run = [...index.runs].sort((a, b) => Date.parse(b.started_at) - Date.parse(a.started_at))[1];
    await page.goto(`${baseUrl}/runs/${run.run_id}/`);
    await expect(page.getByRole("heading", { name: run.run_id, exact: true })).toBeVisible();
    await dashboardNavigation(page).getByRole("link", { name: "Evidence", exact: true }).click();
    await expectPath(page, `/evidence/${run.run_id}`);
    await expect(page.getByRole("heading", { name: "Offline verification" })).toBeVisible();
    await page.goto(`${baseUrl}/evidence/${run.run_id}/`);
    await page.reload();
    await dashboardNavigation(page).getByRole("link", { name: "Run detail", exact: true }).click();
    await expectPath(page, `/runs/${run.run_id}`);
    await expect(page.getByRole("heading", { name: run.run_id, exact: true })).toBeVisible();
  });

  test("recovers from nested run faults and rejects a different valid run response", async ({ page }) => {
    const index = await (await page.request.get(`${baseUrl}/api/v1/runs`)).json() as RunsPageResponse;
    const [selected, other] = index.runs;
    const alternate = await (await page.request.get(`${baseUrl}/api/v1/runs/${other.run_id}`)).json() as RunDetail;
    const browserErrors: string[] = [];
    page.on("pageerror", (error) => browserErrors.push(error.message));
    await page.route("**/api/v1/runs?*", (route) => route.fulfill({ json: {
      ...index, runs: index.runs.map((run) => ({ ...run, headline_metrics: [null] })),
    } }));
    await page.goto(`${baseUrl}/runs`);
    await expect(page.getByText(/headline_metrics\[0\] must be an object/)).toBeVisible();
    await expect(dashboardNavigation(page)).toBeVisible();
    await page.unrouteAll();
    await page.getByRole("button", { name: "Try again" }).click();
    await expect(page.locator(".runs-table tbody tr")).toHaveCount(4);

    const pattern = `**/api/v1/runs/${selected.run_id}`;
    await page.route(pattern, (route) => route.fulfill({ json: alternate }));
    await page.goto(`${baseUrl}/runs/${selected.run_id}`);
    await expect(page.getByText(/identity does not match the requested run/)).toBeVisible();
    await expect(page.getByRole("heading", { name: other.run_id, exact: true })).toHaveCount(0);
    await page.unrouteAll();
    await page.getByRole("button", { name: "Try again" }).click();
    await expect(page.getByRole("heading", { name: selected.run_id, exact: true })).toBeVisible();
    expect(browserErrors).toEqual([]);
  });

  for (const view of ["runs", "evidence", "compare"] as const) {
    test(`refresh invalidates removed ${view} evidence and one retry restores it`, async ({ page }) => {
      if (!fixture) throw new Error("The owned fixture is unavailable");
      const index = await (await page.request.get(`${baseUrl}/api/v1/runs`)).json() as RunsPageResponse;
      const run = [...index.runs].sort((a, b) => Date.parse(b.started_at) - Date.parse(a.started_at))[0];
      const path = view === "compare" ? "/compare" : `/${view}/${run.run_id}`;
      const content = view === "compare" ? page.locator(".comparison-result")
        : view === "evidence" ? page.getByRole("heading", { name: "Offline verification" })
          : page.getByRole("heading", { name: "Observed distributions" });
      await page.goto(`${baseUrl}${path}`);
      await expect(content).toBeVisible();
      await expect(page.getByRole("button", { name: "Refresh runs" })).toBeVisible();
      const original = join(fixture.runs, run.run_id);
      const moved = join(fixture.root, `removed-${run.run_id}`);
      renameSync(original, moved);
      let releaseIndex: (() => void) | undefined;
      try {
        const indexGate = new Promise<void>((resolveGate) => { releaseIndex = resolveGate; });
        await page.route("**/api/v1/runs?*", async (route) => { await indexGate; await route.continue(); });
        await page.getByRole("button", { name: "Refresh runs" }).click();
        await expect(page.getByRole("button", { name: "Refresh runs" })).toBeDisabled();
        await expect(content).toHaveCount(0);
        releaseIndex!();
        const unavailable = view === "compare"
          ? page.getByText("A selected run is unavailable", { exact: true })
          : page.getByText(/This run is not in the latest verified run snapshot/);
        await expect(unavailable).toBeVisible();
        await expect(page.getByText("3 verified · 0 rejected", { exact: true })).toBeVisible();
        await expectPath(page, path);
        if (view === "compare") {
          await expect(page.locator(".compare-controls select").nth(1)).toHaveValue(run.run_id);
        } else {
          // The initial detail read must also wait for a new index on a deep reload.
          await page.unrouteAll();
          await page.reload();
          await expect(unavailable).toBeVisible();
          await expect(content).toHaveCount(0);
        }
        renameSync(moved, original);
        await page.unrouteAll();
        await page.getByRole("button", { name: "Refresh runs" }).click();
        await expect(content).toBeVisible();
        await dashboardNavigation(page).getByRole("link", { name: "Runs", exact: true }).click();
        await expect(page.locator(".runs-table tbody tr")).toHaveCount(4);
      } finally {
        releaseIndex?.();
        if (existsSync(moved)) renameSync(moved, original);
        await page.unrouteAll({ behavior: "ignoreErrors" });
      }
    });
  }

  test("refresh binds a replacement bundle to the same selected run identity", async ({ page }) => {
    if (!fixture) throw new Error("The owned fixture is unavailable");
    const index = await (await page.request.get(`${baseUrl}/api/v1/runs`)).json() as RunsPageResponse;
    const selected: RunSummary = index.runs[0];
    const replacementRoot = join(fixture.root, "replacement-runs");
    const prepared = spawnSync(pythonExecutable(), [
      "-m", "inferdrome", "run", join(REPOSITORY_ROOT, "examples", "controlled-concurrency-2.yaml"),
      "--runs-root", replacementRoot, "--run-id", selected.run_id,
    ], { cwd: REPOSITORY_ROOT, env: inferdromeEnvironment(), encoding: "utf8", timeout: 30_000 });
    if (prepared.error || prepared.status !== 0) throw new Error("Synthetic replacement fixture creation failed");
    await page.goto(`${baseUrl}/evidence/${selected.run_id}`);
    await expect(page.locator(`.verification-stats dd[title="${selected.bundle_digest}"]`)).toBeVisible();
    await expect(page.getByRole("button", { name: "Refresh runs" })).toBeVisible();
    const original = join(fixture.runs, selected.run_id);
    const saved = join(fixture.root, `original-${selected.run_id}`);
    const replacement = join(replacementRoot, selected.run_id);
    try {
      renameSync(original, saved);
      renameSync(replacement, original);
      await page.getByRole("button", { name: "Refresh runs" }).click();
      await expect(page.locator(`.verification-stats dd[title="${selected.bundle_digest}"]`)).toHaveCount(0);
      await expect(page.getByRole("heading", { name: "Offline verification" })).toBeVisible();
      const refreshed = await (await page.request.get(`${baseUrl}/api/v1/runs/${selected.run_id}`)).json() as RunDetail;
      expect(refreshed.summary.run_id).toBe(selected.run_id);
      expect(refreshed.verification.bundle_digest).not.toBe(selected.bundle_digest);
      await expect(page.locator(`.verification-stats dd[title="${refreshed.verification.bundle_digest}"]`)).toBeVisible();
      await page.reload();
      await expect(page.locator(`.verification-stats dd[title="${refreshed.verification.bundle_digest}"]`)).toBeVisible();
      // Corrupt only this newly generated replacement, never the retained original.
      const measurements = join(original, "bundle", "derived", "measurements.json");
      const verifiedBytes = readFileSync(measurements);
      chmodSync(measurements, 0o600);
      writeFileSync(measurements, Buffer.concat([verifiedBytes, Buffer.from(" ")]));
      chmodSync(measurements, 0o400);
      await page.getByRole("button", { name: "Refresh runs" }).click();
      await expect(page.getByText(/This run is not in the latest verified run snapshot/)).toBeVisible();
      await expect(page.getByText("3 verified · 1 rejected", { exact: true })).toBeVisible();
      await expect(page.getByRole("heading", { name: "Offline verification" })).toHaveCount(0);
      chmodSync(measurements, 0o600);
      writeFileSync(measurements, verifiedBytes);
      chmodSync(measurements, 0o400);
      await page.getByRole("button", { name: "Refresh runs" }).click();
      await expect(page.locator(`.verification-stats dd[title="${refreshed.verification.bundle_digest}"]`)).toBeVisible();
    } finally {
      if (existsSync(saved)) {
        if (existsSync(original)) renameSync(original, replacement);
        renameSync(saved, original);
      }
      await page.request.get(`${baseUrl}/api/v1/runs`);
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

  for (const profile of EXECUTION_PROFILES) {
    test(`packaged ${profile.label} execution supports list, detail, back, and reload`, async ({ page }) => {
      if (!fixture) throw new Error("The populated dashboard fixture is unavailable.");
      let executionRoot = fixture.routingExecutionRoot;
      let retainedDigest = fixture.routingExecutionDigest;
      if (profile.profileId !== EXECUTION_PROFILES[0].profileId) {
        // Keep both closed packages beneath the one owned fixture tree and
        // reuse the same server lifecycle; no provider or serving process runs.
        const root = realpathSync(mkdtempSync(join(fixture.root, "h100-")));
        const execution = prepareRoutingExecutionFixture(root, profile.profileId);
        executionRoot = execution.root;
        retainedDigest = execution.retained_digest;
        await stopDashboardServer();
        baseUrl = await startDashboardServer({
          ...fixture,
          routingExecutionRoot: executionRoot,
          routingExecutionDigest: retainedDigest,
        });
      }
      const manifest = JSON.parse(readFileSync(join(executionRoot, "executed-manifest.json"), "utf8")) as {
        readonly schema_version: string;
        readonly topology: { readonly profile_id: string };
      };
      expect(manifest.schema_version).toBe(profile.manifestVersion);
      expect(manifest.topology.profile_id).toBe(profile.profileId);

      const browserErrors: string[] = [];
      const failedResponses: string[] = [];
      const unsafeApiRequests: string[] = [];
      page.on("console", (message) => { if (message.type() === "error") browserErrors.push(message.text()); });
      page.on("pageerror", (error) => browserErrors.push(error.message));
      page.on("response", (response) => {
        if (new URL(response.url()).origin === baseUrl && response.status() >= 400) {
          failedResponses.push(`${response.status()} ${response.url()}`);
        }
      });
      page.on("request", (request) => {
        const url = new URL(request.url());
        if (url.origin === baseUrl && url.pathname.startsWith("/api/") && request.method() !== "GET") {
          unsafeApiRequests.push(`${request.method()} ${url.pathname}`);
        }
      });

      const indexResponse = await page.request.get(`${baseUrl}/api/v1/routing-executions`);
      const detailPath = "/routing-executions/routing-execution-v1";
      const detailResponse = await page.request.get(`${baseUrl}/api/v1${detailPath}`);
      expect(indexResponse.status()).toBe(200);
      expect(detailResponse.status()).toBe(200);
      const index = await indexResponse.json();
      const detail = await detailResponse.json();
      expect(index.rejected).toEqual([]);
      expect(index.routing_executions).toHaveLength(1);
      expect(index.routing_executions[0]).toEqual(detail.summary);
      expect(detail.summary).toMatchObject({
        execution_id: "routing-execution-v1",
        retained_digest: retainedDigest,
        mode: "LAMBDA_MANUAL_HOST",
        topology: {
          accelerator_model: profile.acceleratorModel,
          accelerator_count: 2,
          identity_assertion: "OPERATOR_DECLARED_NOT_OBSERVED",
          lifecycle_protection: "UNRESOLVED_PRELAUNCH_WATCHDOG_BOUNDARY",
        },
        terminal_denominator: 18,
        verified_by_offline_replay: true,
      });

      const shell = await page.goto(`${baseUrl}/routing-executions`);
      expect(shell?.status()).toBe(200);
      await expect(page.getByText(`2 × ${profile.acceleratorModel}`, { exact: true })).toBeVisible();
      await expect(page.getByText("Manual host declaration · not provider proof", { exact: true }).first()).toBeVisible();
      await page.getByRole("link", { name: "routing-execution-v1", exact: true }).first().click();
      await expectPath(page, detailPath);

      const expectExecutionDetail = async () => {
        await expect(page.getByRole("heading", { name: "routing-execution-v1", level: 1 })).toBeVisible();
        await expect(page.locator(".execution-trial-section")).toHaveCount(3);
        await expect(page.locator(".execution-request-table tbody tr")).toHaveCount(18);
        await expect(page.getByText("Manual-host facts are operator-declared.", { exact: false })).toBeVisible();
        await expect(page.getByText("GPU/DCGM and KV/cache retained as unavailable", { exact: true })).toBeVisible();
      };
      await expectExecutionDetail();
      await page.locator(".execution-back-link").getByRole("link", { name: "Routing executions", exact: true }).click();
      await expectPath(page, "/routing-executions");
      await expect(page.getByText(`2 × ${profile.acceleratorModel}`, { exact: true })).toBeVisible();
      const deepLink = await page.goto(`${baseUrl}${detailPath}`);
      expect(deepLink?.status()).toBe(200);
      await expectExecutionDetail();
      const reload = await page.reload();
      expect(reload?.status()).toBe(200);
      await expectExecutionDetail();
      expect(browserErrors).toEqual([]);
      expect(failedResponses).toEqual([]);
      expect(unsafeApiRequests).toEqual([]);
    });
  }

  test.describe("navigation and metric clarity", () => {
    for (const width of [390, 470, 600, 760, 1024, 1440]) {
      test(`keeps every navigation link keyboard reachable at ${width}px`, async ({ page }) => {
        if (width === 390) await page.emulateMedia({ reducedMotion: "reduce" });
        await page.setViewportSize({ width, height: 900 });
        const response = await page.goto(`${baseUrl}/runs`);
        expect(response?.status()).toBe(200);
        await expect(page.locator(".runs-table tbody tr")).toHaveCount(4);

        const names = [
          "Runs", "Trial sets", "Routing campaigns", "Routing executions",
          "Causal qualification", "Run detail", "Comparisons", "Evidence",
        ];
        const navigation = dashboardNavigation(page);
        await expect(navigation.getByRole("link")).toHaveCount(names.length);
        await page.keyboard.press("Tab");
        await expect(page.getByRole("link", { name: "Skip to dashboard content" })).toBeFocused();
        await page.keyboard.press("Tab");
        await expect(page.getByRole("link", { name: "Inferdrome runs", exact: true })).toBeFocused();

        for (const name of names) {
          await page.keyboard.press("Tab");
          const link = navigation.getByRole("link", { name, exact: true });
          await expect(link).toBeFocused();
          await expect(link.locator("span")).toBeInViewport({ ratio: 1 });
          const visibility = await link.evaluate((element) => {
            const bounds = element.querySelector("span")!.getBoundingClientRect();
            const nav = element.closest("nav")!;
            const navBounds = nav.getBoundingClientRect();
            const clipsHorizontally = ["auto", "scroll", "hidden", "clip"].includes(getComputedStyle(nav).overflowX);
            const style = getComputedStyle(element);
            return {
              left: bounds.left,
              right: bounds.right,
              visibleLeft: clipsHorizontally ? Math.max(0, navBounds.left + nav.clientLeft) : 0,
              visibleRight: clipsHorizontally
                ? Math.min(innerWidth, navBounds.left + nav.clientLeft + nav.clientWidth)
                : innerWidth,
              focusVisible: element.matches(":focus-visible"),
              outlineStyle: style.outlineStyle,
              outlineWidth: Number.parseFloat(style.outlineWidth),
              scrollWidth: document.documentElement.scrollWidth,
              clientWidth: document.documentElement.clientWidth,
            };
          });
          expect(visibility.left, `${name} label left edge at ${width}px`).toBeGreaterThanOrEqual(visibility.visibleLeft - 1);
          expect(visibility.right, `${name} label right edge at ${width}px`).toBeLessThanOrEqual(visibility.visibleRight + 1);
          expect(visibility.focusVisible, `${name} keyboard focus at ${width}px`).toBe(true);
          expect(visibility.outlineStyle).not.toBe("none");
          expect(visibility.outlineWidth).toBeGreaterThan(0);
          expect(visibility.scrollWidth).toBeLessThanOrEqual(visibility.clientWidth);
        }
        await page.keyboard.press("Enter");
        await expect(page.getByRole("heading", { name: "Offline verification" })).toBeVisible();
        await navigation.getByRole("link", { name: "Runs", exact: true }).click();
        await expect(page.locator(".runs-table tbody tr")).toHaveCount(4);
      });
    }

    for (const width of [1440, 390]) {
      test(`shows exact backend Trial Set values and request samples at ${width}px`, async ({ page }) => {
        const indexResponse = await page.request.get(`${baseUrl}/api/v1/trial-sets?limit=100`);
        expect(indexResponse.status()).toBe(200);
        const index = await indexResponse.json() as TrialSetPageResponse;
        expect(index.rejected).toEqual([]);
        expect(index.trial_sets).toHaveLength(2);
        const trialSetId = index.trial_sets[0].trial_set_id;
        const detailResponse = await page.request.get(`${baseUrl}/api/v1/trial-sets/${encodeURIComponent(trialSetId)}`);
        expect(detailResponse.status()).toBe(200);
        const detail = await detailResponse.json() as TrialSetDetail;
        expect(detail.summary.trial_set_id).toBe(trialSetId);
        expect(detail.summary.evidence_eligibilities).toEqual(["SYNTHETIC_ONLY"]);
        expect(detail.request_population_policy).toBe("separate_per_run_v1");

        await page.setViewportSize({ width, height: 900 });
        const response = await page.goto(`${baseUrl}/trial-sets/${encodeURIComponent(trialSetId)}`);
        expect(response?.status()).toBe(200);
        await expect(page.getByRole("heading", { name: "Run-to-run variation", level: 2 })).toBeVisible();
        const metricSelect = page.getByRole("combobox", { name: "Metric", exact: true });

        for (const key of ["ttft_ns:p50", "error_rate:ratio"]) {
          const variation = detail.variations.find((item) => item.key === key);
          expect(variation, `${key} remains part of the real fixture`).toBeDefined();
          if (!variation) throw new Error(`Fixture is missing ${key}`);
          expect(variation.weighting).toBe("equal_per_run");
          await metricSelect.selectOption(key);
          await expect(page.locator(".trial-summary-grid dd")).toHaveText([
            variation.minimum_display_value ?? "Unavailable",
            variation.median_display_value ?? "Unavailable",
            variation.maximum_display_value ?? "Unavailable",
            variation.span_display_value ?? "Unavailable",
            variation.mean_display_value ?? "Unavailable",
            variation.sample_standard_deviation_display_value ?? "Unavailable",
          ]);

          for (const point of variation.points) {
            expect(point.value).not.toBeNull();
            expect(point.sample_count).not.toBeNull();
            const runLink = page.locator(`a[href="/runs/${encodeURIComponent(point.run_id)}"]`);
            const pointRow = page.locator(".trial-point-row").filter({ has: runLink });
            const member = page.locator(width === 390 ? ".trial-member-card" : ".trial-member-table tbody tr")
              .filter({ has: runLink });
            for (const row of [pointRow, member]) {
              await expect(row).toBeVisible();
              await expect(row.getByText(point.display_value!, { exact: true })).toBeVisible();
              await expect(row.getByText(`Exact: ${point.value} ${variation.unit}`, { exact: true })).toBeVisible();
              await expect(row.getByText(`Request samples: ${point.sample_count!.toLocaleString()}`, { exact: true })).toBeVisible();
            }
          }
          await expect(page.getByText(/Each point is one verified run-level scalar with equal run weighting/)).toBeVisible();
          await expect(page.getByText(/Request populations remain separate/)).toBeVisible();
        }
        const dimensions = await page.evaluate(() => ({
          scrollWidth: document.documentElement.scrollWidth,
          clientWidth: document.documentElement.clientWidth,
        }));
        expect(dimensions.scrollWidth).toBeLessThanOrEqual(dimensions.clientWidth);
      });

      test(`distinguishes missing TTFT from a measured zero at ${width}px`, async ({ page }) => {
        const response = await page.request.get(`${baseUrl}/api/v1/runs?limit=200`);
        expect(response.status()).toBe(200);
        const index = await response.json() as RunsPageResponse;
        expect(index.rejected).toEqual([]);
        expect(index.runs).toHaveLength(4);
        const run = index.runs.find((item) => item.headline_metrics.some((metric) => (
          metric.key === "error_rate:ratio" && /^0(?:\.0+)?$/.test(metric.value)
        )));
        expect(run, "real fixture includes an observed zero error rate").toBeDefined();
        if (!run) throw new Error("Fixture is missing a measured zero error rate");
        const zero = run.headline_metrics.find((metric) => metric.key === "error_rate:ratio")!;
        expect(run.headline_metrics.some((metric) => metric.metric === "ttft_ns")).toBe(true);
        expect(run.evidence_eligibility).toBe("SYNTHETIC_ONLY");

        // Retain the real response and zero measurement; only withhold TTFT.
        const compatibleIndex = {
          ...index,
          runs: index.runs.map((item) => item.run_id === run.run_id ? {
            ...item,
            ttft_p50_ns: null,
            ttft_p95_ns: null,
            headline_metrics: item.headline_metrics.filter((metric) => metric.metric !== "ttft_ns"),
          } : item),
        };
        await page.route("**/api/v1/runs?**", (route) => route.fulfill({
          status: response.status(),
          contentType: "application/json",
          json: compatibleIndex,
        }));
        await page.setViewportSize({ width, height: 900 });
        await page.goto(`${baseUrl}/runs`);
        await expect(page.getByRole("heading", { name: "Recent runs", level: 2 })).toBeVisible();
        const runLink = page.locator(`a[href="/runs/${encodeURIComponent(run.run_id)}"]`);
        const row = page.locator(width === 390 ? ".run-mobile-card" : ".runs-table tbody tr")
          .filter({ has: runLink });
        const metrics = row.locator(width === 390 ? ".run-mobile-metrics dd" : "td.number-cell");
        const missing = metrics.nth(0);
        const measuredZero = metrics.nth(2);
        await expect(missing).toHaveText("Not reported");
        await expect(measuredZero).not.toHaveText("Not reported");
        expect((await measuredZero.innerText()).replace(/\s/g, "")).toBe(zero.display_value.replace(/\s/g, ""));
        await missing.scrollIntoViewIfNeeded();
        await expect(missing).toBeInViewport({ ratio: 1 });
        const dimensions = await missing.evaluate((element) => ({
          textWidth: element.scrollWidth,
          availableWidth: element.clientWidth,
          scrollWidth: document.documentElement.scrollWidth,
          clientWidth: document.documentElement.clientWidth,
        }));
        expect(dimensions.textWidth).toBeLessThanOrEqual(dimensions.availableWidth);
        expect(dimensions.scrollWidth).toBeLessThanOrEqual(dimensions.clientWidth);
      });
    }

    test("labels the configured local evidence source without a placeholder path", async ({ page }) => {
      expect(fixture).toBeDefined();
      expect(isAbsolute(fixture!.runs)).toBe(true);
      expect(fixture!.runs).not.toBe("./runs");
      await page.setViewportSize({ width: 1440, height: 900 });
      await page.goto(`${baseUrl}/runs`);
      await expect(page.locator(".runs-table tbody tr")).toHaveCount(4);
      const source = page.locator(".workspace-label");
      await expect(source).toBeVisible();
      await expect(source).toContainText("Evidence source");
      await expect(source).toContainText("Local bundles");
      await expect(source).not.toContainText("./runs");
      await expect(source).not.toContainText(fixture!.runs);
    });
  });
});
