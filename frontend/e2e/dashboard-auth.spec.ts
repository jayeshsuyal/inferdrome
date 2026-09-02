import { expect, test, type Page } from "@playwright/test";
import { spawn, spawnSync, type ChildProcess } from "node:child_process";
import { once } from "node:events";
import { chmodSync, existsSync, lstatSync, mkdtempSync, readdirSync, realpathSync, rmSync } from "node:fs";
import { createServer } from "node:net";
import { tmpdir } from "node:os";
import { delimiter, dirname, isAbsolute, join, resolve, sep } from "node:path";
import { fileURLToPath } from "node:url";

const REPOSITORY_ROOT = resolve(dirname(fileURLToPath(import.meta.url)), "../..");
const SOURCE_ROOT = join(REPOSITORY_ROOT, "src");
const FIXTURE_PREFIX = "inferdrome-dashboard-auth-e2e-";

interface DashboardRoots {
  readonly root: string;
  readonly runs: string;
  readonly trialSets: string;
  readonly comparisonPlans: string;
  readonly comparisonResults: string;
  readonly routingCampaigns: string;
}

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

function environment(): NodeJS.ProcessEnv {
  return {
    ...process.env,
    PYTHONPATH: [SOURCE_ROOT, process.env.PYTHONPATH].filter(Boolean).join(delimiter),
  };
}

function prepareFixture(root: string): DashboardRoots {
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
      env: environment(),
      maxBuffer: 10 * 1024 * 1024,
      timeout: 90_000,
    },
  );
  if (result.status !== 0 || result.error) throw new Error("local dashboard fixture preparation failed");
  const summary = JSON.parse(result.stdout) as {
    readonly claim_boundary: string;
    readonly roots: Record<string, string>;
  };
  if (summary.claim_boundary !== "SYNTHETIC_ONLY") {
    throw new Error("authenticated dashboard fixture was not synthetic-only");
  }
  const routingCampaigns = join(root, "routing-campaign-package");
  const campaignRoot = join(REPOSITORY_ROOT, "campaigns", "routing-campaign-v1");
  const campaign = spawnSync(
    pythonExecutable(),
    [
      "-m", "inferdrome.routing_campaign", "run",
      "--campaign-plan", join(campaignRoot, "stale-load-fresh-health.plan.json"),
      "--request-trace", join(campaignRoot, "stale-load-fresh-health.trace.jsonl"),
      "--fault-schedule", join(campaignRoot, "stale-load-fresh-health.fault-schedule.json"),
      "--trial-plan", join(campaignRoot, "trial-plan.json"),
      "--output", routingCampaigns,
    ],
    {
      cwd: REPOSITORY_ROOT,
      encoding: "utf8",
      env: environment(),
      maxBuffer: 10 * 1024 * 1024,
      timeout: 30_000,
    },
  );
  if (campaign.status !== 0 || campaign.error || !existsSync(routingCampaigns)) {
    throw new Error(`authenticated routing-campaign fixture failed:\n${campaign.stderr || campaign.stdout}`);
  }
  return {
    root,
    runs: summary.roots.runs,
    trialSets: summary.roots.trial_sets,
    comparisonPlans: summary.roots.comparison_plans,
    comparisonResults: summary.roots.comparison_results,
    routingCampaigns,
  };
}

function createKeyring(root: string): { readonly path: string; readonly token: string } {
  const path = join(root, "dashboard-keyring.json");
  const result = spawnSync(
    pythonExecutable(),
    ["-m", "inferdrome", "dashboard-keyring", "create", "--keyring", path, "--label", "playwright"],
    { cwd: REPOSITORY_ROOT, encoding: "utf8", env: environment(), timeout: 20_000 },
  );
  if (result.status !== 0 || result.error) throw new Error("dashboard keyring creation failed");
  const output = JSON.parse(result.stdout) as { readonly token: string };
  if (!output.token) throw new Error("dashboard keyring creation returned no token");
  return { path, token: output.token };
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
        reject(new Error("could not allocate a loopback port"));
        return;
      }
      server.close((error) => (error ? reject(error) : resolvePort(address.port)));
    });
  });
}

async function startServer(paths: DashboardRoots, keyring: string): Promise<{ readonly url: string; readonly process: ChildProcess }> {
  const port = await availablePort();
  const child = spawn(
    pythonExecutable(),
    [
      "-m", "inferdrome", "dashboard", "--keyring", keyring,
      "--runs-root", paths.runs, "--trial-sets-root", paths.trialSets,
      "--comparison-plans-root", paths.comparisonPlans,
      "--comparison-results-root", paths.comparisonResults,
      "--routing-campaigns-root", paths.routingCampaigns,
      "--port", String(port),
    ],
    { cwd: REPOSITORY_ROOT, env: environment(), stdio: ["ignore", "ignore", "pipe"] },
  );
  let stderr = "";
  child.stderr?.on("data", (chunk: Buffer) => { stderr = `${stderr}${chunk.toString("utf8")}`.slice(-2_000); });
  const url = `http://127.0.0.1:${port}`;
  const deadline = Date.now() + 20_000;
  while (Date.now() < deadline) {
    if (child.exitCode !== null || child.signalCode !== null) throw new Error(`authenticated dashboard exited: ${stderr}`);
    try {
      if ((await fetch(`${url}/api/v1/health`)).ok) return { url, process: child };
    } catch {
      // The loopback socket may refuse connections while uvicorn starts.
    }
    await new Promise((resolveDelay) => setTimeout(resolveDelay, 100));
  }
  throw new Error(`authenticated dashboard did not start: ${stderr}`);
}

async function stopServer(child: ChildProcess): Promise<void> {
  if (child.exitCode !== null || child.signalCode !== null) return;
  child.kill("SIGTERM");
  await Promise.race([once(child, "exit"), new Promise((resolveDelay) => setTimeout(resolveDelay, 5_000))]);
  if (child.exitCode === null && child.signalCode === null) {
    child.kill("SIGKILL");
    await Promise.race([once(child, "exit"), new Promise((resolveDelay) => setTimeout(resolveDelay, 5_000))]);
  }
}

function makeTreeWritable(path: string): void {
  const metadata = lstatSync(path);
  if (metadata.isSymbolicLink()) return;
  chmodSync(path, metadata.isDirectory() ? 0o700 : 0o600);
  if (metadata.isDirectory()) {
    for (const entry of readdirSync(path)) makeTreeWritable(join(path, entry));
  }
}

test("authenticated local server blocks evidence, unlocks, and traverses every route", async ({ page }) => {
  // Dashboard keyring paths reject symlinked ancestors; use the strict path
  // explicitly rather than relying on a production-side path rewrite.
  const root = realpathSync(mkdtempSync(join(tmpdir(), FIXTURE_PREFIX)));
  let server: ChildProcess | undefined;
  try {
    const paths = prepareFixture(root);
    const keyring = createKeyring(root);
    const started = await startServer(paths, keyring.path);
    server = started.process;
    const unauthenticated = await page.request.get(`${started.url}/api/v1/runs`);
    const unauthenticatedCampaigns = await page.request.get(`${started.url}/api/v1/routing-campaigns`);
    const health = await page.request.get(`${started.url}/api/v1/health`);
    const authenticated = await page.request.get(`${started.url}/api/v1/runs`, {
      headers: { Authorization: `Bearer ${keyring.token}` },
    });
    const authenticatedCampaigns = await page.request.get(`${started.url}/api/v1/routing-campaigns`, {
      headers: { Authorization: `Bearer ${keyring.token}` },
    });
    expect(unauthenticated.status()).toBe(401);
    expect(unauthenticatedCampaigns.status()).toBe(401);
    expect(health.status()).toBe(200);
    expect(authenticated.status()).toBe(200);
    expect(authenticatedCampaigns.status()).toBe(200);

    const protectedRequests: Array<{ readonly url: string; readonly authorization: string | undefined }> = [];
    const browserErrors: string[] = [];
    page.on("request", (request) => {
      if (new URL(request.url()).pathname.startsWith("/api/") && !request.url().endsWith("/health")) {
        protectedRequests.push({ url: request.url(), authorization: request.headers().authorization });
      }
    });
    page.on("console", (message) => { if (message.type() === "error") browserErrors.push(message.text()); });

    await page.goto(`${started.url}/`);
    await expect(page.getByRole("heading", { name: "Unlock evidence views" })).toBeVisible();
    await page.getByLabel("Bearer token").fill(keyring.token);
    await page.getByRole("button", { name: "Unlock dashboard" }).click();
    await expect(page.getByRole("heading", { name: "Runs", level: 1 })).toBeVisible();
    await expect(page.locator(".runs-table tbody tr")).toHaveCount(4);

    const runHref = await page.locator("a.latest-run-title").getAttribute("href");
    expect(runHref).toMatch(/^\/runs\/run-[0-9a-f]{32}$/);
    const runId = decodeURIComponent(runHref!.slice("/runs/".length));
    await page.locator("a.latest-run-title").click();
    await expect(page.getByRole("heading", { name: runId, level: 1 })).toBeVisible();
    await page.getByRole("link", { name: "Evidence", exact: true }).click();
    await expect(page).toHaveURL(`${started.url}/evidence/${runId}`);
    await page.getByRole("link", { name: "Runs", exact: true }).click();
    await page.getByRole("link", { name: "Trial sets", exact: true }).click();
    await expect(page.getByRole("heading", { name: "Verified trial sets", level: 2 })).toBeVisible();
    const trialHref = await page.locator("a.trial-set-link:visible").first().getAttribute("href");
    await page.locator(`a[href="${trialHref}"]`).first().click();
    await expect(page.getByRole("heading", { name: "Run-to-run variation", level: 2 })).toBeVisible();
    await page.getByRole("link", { name: "Runs", exact: true }).click();
    await page.getByRole("link", { name: "Comparisons", exact: true }).click();
    await expect(page.getByRole("heading", { name: "Comparison plans", level: 2 })).toBeVisible();
    const comparisonHref = await page.locator("a.comparison-plan-link:visible").first().getAttribute("href");
    await page.locator(`a[href="${comparisonHref}"]`).first().click();
    await expect(page.getByRole("heading", { name: "Inferdrome local product demo", level: 1 })).toBeVisible();
    await page.getByRole("link", { name: "Controlled comparisons", exact: true }).click();
    await page.getByRole("link", { name: "Compare two runs", exact: true }).first().click();
    await expect(page.getByRole("heading", { name: "Compare runs", level: 1 })).toBeVisible();
    await page.getByRole("link", { name: "Routing campaigns", exact: true }).click();
    await expect(page.getByRole("heading", { name: "Verified routing campaigns", level: 2 })).toBeVisible();
    const routingHref = await page.locator("a.routing-campaign-link:visible").first().getAttribute("href");
    expect(routingHref).toBe("/routing-campaigns/routing-campaign-v1");
    await page.locator(`a[href="${routingHref}"]`).first().click();
    await expect(page.getByRole("heading", { name: "Fault timeline", level: 2 })).toBeVisible();
    await expect(page.getByRole("heading", { name: "Complete terminal population", level: 2 }).first()).toBeVisible();
    await page.getByRole("button", { name: "Lock dashboard" }).click();
    await expect(page.getByRole("heading", { name: "Unlock evidence views" })).toBeVisible();

    const browserState = await page.evaluate(() => ({
      cookie: document.cookie,
      local: JSON.stringify(localStorage),
      session: JSON.stringify(sessionStorage),
      url: window.location.href,
    }));
    expect(browserState.cookie).not.toContain(keyring.token);
    expect(browserState.local).not.toContain(keyring.token);
    expect(browserState.session).not.toContain(keyring.token);
    expect(browserState.url).not.toContain(keyring.token);
    expect(protectedRequests.filter((request) => request.authorization).every(
      (request) => request.authorization === `Bearer ${keyring.token}`,
    )).toBe(true);
    expect(browserErrors.every((message) => !message.includes(keyring.token))).toBe(true);
  } finally {
    if (server) await stopServer(server);
    const resolvedRoot = realpathSync(root);
    if (!resolvedRoot.startsWith(`${realpathSync(tmpdir())}${sep}${FIXTURE_PREFIX}`)) {
      throw new Error("refusing to remove unexpected authenticated E2E root");
    }
    makeTreeWritable(resolvedRoot);
    rmSync(resolvedRoot, { force: true, maxRetries: 5, recursive: true, retryDelay: 100 });
  }
});
