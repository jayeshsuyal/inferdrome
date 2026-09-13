import { expect, test, type Page } from "@playwright/test";
import { spawn, spawnSync, type ChildProcess } from "node:child_process";
import { once } from "node:events";
import { chmodSync, existsSync, lstatSync, mkdtempSync, readdirSync, readFileSync, realpathSync, rmSync } from "node:fs";
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
  readonly routingQualifications: string;
  readonly routingQualificationDigest: string;
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
    readonly routing_qualification: {
      readonly retained_digest: string;
    };
  };
  if (summary.claim_boundary !== "SYNTHETIC_ONLY") {
    throw new Error("authenticated dashboard fixture was not synthetic-only");
  }
  if (
    !summary.roots.routing_campaign
    || !summary.roots.routing_qualification
    || !summary.routing_qualification?.retained_digest
  ) {
    throw new Error("authenticated qualification fixture is incomplete");
  }
  return {
    root,
    runs: summary.roots.runs,
    trialSets: summary.roots.trial_sets,
    comparisonPlans: summary.roots.comparison_plans,
    comparisonResults: summary.roots.comparison_results,
    routingCampaigns: summary.roots.routing_campaign,
    routingQualifications: summary.roots.routing_qualification,
    routingQualificationDigest: summary.routing_qualification.retained_digest,
  };
}

function createKeyring(root: string): { readonly path: string; readonly token: string; readonly keyId: string } {
  const path = join(root, "dashboard-keyring.json");
  const result = spawnSync(
    pythonExecutable(),
    ["-m", "inferdrome", "dashboard-keyring", "create", "--keyring", path, "--label", "playwright"],
    { cwd: REPOSITORY_ROOT, encoding: "utf8", env: environment(), timeout: 20_000 },
  );
  if (result.status !== 0 || result.error) throw new Error("dashboard keyring creation failed");
  const output = JSON.parse(result.stdout) as { readonly token: string; readonly key_id: string };
  if (!output.token || !output.key_id) throw new Error("dashboard keyring creation returned no token or key identity");
  return { path, token: output.token, keyId: output.key_id };
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
      "--routing-qualification-root", paths.routingQualifications,
      "--routing-qualification-digest", paths.routingQualificationDigest,
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

test("revoked descriptor download relocks immediately and a replacement key recovers exact bytes", async ({ page }) => {
  const root = realpathSync(mkdtempSync(join(tmpdir(), FIXTURE_PREFIX)));
  let server: ChildProcess | undefined;
  try {
    const paths = prepareFixture(root);
    const keyring = createKeyring(root);
    const started = await startServer(paths, keyring.path);
    server = started.process;
    const detailPath = "/routing-qualifications/stale-telemetry-qualification-v1";
    const evidencePath = `/api/v1${detailPath}/evidence`;
    const verifiedResponse = await page.request.get(`${started.url}${evidencePath}`, {
      headers: { Authorization: `Bearer ${keyring.token}` },
    });
    expect(verifiedResponse.status()).toBe(200);
    const verifiedBytes = await verifiedResponse.body();
    expect(verifiedResponse.headers()["x-inferdrome-evidence-digest"]).toBe(paths.routingQualificationDigest);
    expect(verifiedBytes.equals(readFileSync(join(paths.routingQualifications, "stale-telemetry-qualification-v1", "qualification.json")))).toBe(true);

    await page.goto(`${started.url}${detailPath}`);
    await expect(page.getByRole("heading", { name: "Unlock evidence views" })).toBeVisible();
    await page.getByLabel("Bearer token").fill(keyring.token);
    await page.getByRole("button", { name: "Unlock dashboard" }).click();
    const downloadButton = page.getByRole("button", { name: "Download verified descriptor" });
    await expect(downloadButton).toBeVisible();

    await page.route(`**${evidencePath}`, (route) => route.fulfill({
      status: 500, json: { detail: "Synthetic descriptor service failure" },
    }));
    await downloadButton.click();
    await expect(page.getByText("Synthetic descriptor service failure", { exact: true })).toBeVisible();
    await expect(page.getByRole("heading", { name: "Unlock evidence views" })).toHaveCount(0);
    await page.unrouteAll();
    const initialDownload = page.waitForEvent("download");
    await downloadButton.click();
    const initialPath = await (await initialDownload).path();
    if (!initialPath) throw new Error("The verified descriptor was not downloaded");
    expect(readFileSync(initialPath).equals(verifiedBytes)).toBe(true);

    const rotated = spawnSync(pythonExecutable(), [
      "-m", "inferdrome", "dashboard-keyring", "rotate", "--keyring", keyring.path,
      "--revoke-key-id", keyring.keyId, "--label", "playwright-replacement",
    ], { cwd: REPOSITORY_ROOT, env: environment(), encoding: "utf8", timeout: 20_000 });
    if (rotated.status !== 0 || rotated.error) throw new Error("Synthetic dashboard key rotation failed");
    const replacement = JSON.parse(rotated.stdout) as { readonly token: string };
    if (!replacement.token) throw new Error("Synthetic key rotation returned no replacement token");
    const requestsAfterRotation: string[] = [];
    page.on("request", (request) => {
      if (new URL(request.url()).pathname.startsWith("/api/")) requestsAfterRotation.push(new URL(request.url()).pathname);
    });
    const revokedResponse = page.waitForResponse((response) =>
      new URL(response.url()).pathname === evidencePath && response.status() === 401);
    await downloadButton.click();
    await revokedResponse;
    await expect(page.getByRole("heading", { name: "Unlock evidence views" })).toBeVisible();
    await expect(downloadButton).toHaveCount(0);
    expect(requestsAfterRotation).toEqual([evidencePath]);
    await expect(page).toHaveURL(`${started.url}${detailPath}`);

    await page.getByLabel("Bearer token").fill(replacement.token);
    await page.getByRole("button", { name: "Unlock dashboard" }).click();
    await expect(downloadButton).toBeVisible();
    const recoveredDownload = page.waitForEvent("download");
    await downloadButton.click();
    const downloaded = await recoveredDownload;
    expect(downloaded.suggestedFilename()).toBe("stale-telemetry-qualification-v1.json");
    const recoveredPath = await downloaded.path();
    if (!recoveredPath) throw new Error("The replacement-key descriptor was not downloaded");
    expect(readFileSync(recoveredPath).equals(verifiedBytes)).toBe(true);
    const persisted = await page.evaluate(() => `${document.cookie}\n${JSON.stringify(localStorage)}\n${JSON.stringify(sessionStorage)}\n${location.href}`);
    expect([keyring.token, replacement.token].some((token) => persisted.includes(token))).toBe(false);
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
    const qualificationPaths = [
      "/api/v1/routing-qualifications",
      "/api/v1/routing-qualifications/stale-telemetry-qualification-v1",
      "/api/v1/routing-qualifications/stale-telemetry-qualification-v1/evidence",
    ];
    const unauthenticated = await page.request.get(`${started.url}/api/v1/runs`);
    const unauthenticatedCampaigns = await page.request.get(`${started.url}/api/v1/routing-campaigns`);
    const unauthenticatedQualifications = [];
    for (const path of qualificationPaths) {
      unauthenticatedQualifications.push(await page.request.get(`${started.url}${path}`));
    }
    const health = await page.request.get(`${started.url}/api/v1/health`);
    const authenticated = await page.request.get(`${started.url}/api/v1/runs`, {
      headers: { Authorization: `Bearer ${keyring.token}` },
    });
    const authenticatedCampaigns = await page.request.get(`${started.url}/api/v1/routing-campaigns`, {
      headers: { Authorization: `Bearer ${keyring.token}` },
    });
    const authenticatedQualifications = [];
    for (const path of qualificationPaths) {
      authenticatedQualifications.push(await page.request.get(`${started.url}${path}`, {
        headers: { Authorization: `Bearer ${keyring.token}` },
      }));
    }
    expect(unauthenticated.status()).toBe(401);
    expect(unauthenticatedCampaigns.status()).toBe(401);
    expect(unauthenticatedQualifications.every((response) => response.status() === 401)).toBe(true);
    expect(health.status()).toBe(200);
    expect(authenticated.status()).toBe(200);
    expect(authenticatedCampaigns.status()).toBe(200);
    expect(authenticatedQualifications.every((response) => response.status() === 200)).toBe(true);

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
    // The initial locked-page read is deliberately unauthenticated so the
    // application can offer its unlock control. Assert the subsequent causal
    // navigation/download traffic separately, after that expected 401 path.
    protectedRequests.splice(0, protectedRequests.length);

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
    await page.getByRole("link", { name: "Causal qualification", exact: true }).click();
    await expect(page.getByRole("heading", { name: "Verified qualification descriptors", level: 2 })).toBeVisible();
    const qualificationHref = await page.locator("a.routing-qualification-link:visible").first().getAttribute("href");
    expect(qualificationHref).toBe("/routing-qualifications/stale-telemetry-qualification-v1");
    await page.locator(`a[href="${qualificationHref}"]`).first().click();
    await expect(page.getByRole("heading", { name: "What the router knew at the focal request", level: 2 })).toBeVisible();
    const downloadPromise = page.waitForEvent("download");
    await page.getByRole("button", { name: "Download verified descriptor" }).click();
    const download = await downloadPromise;
    expect(download.suggestedFilename()).toBe("stale-telemetry-qualification-v1.json");
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
    expect(protectedRequests.length).toBeGreaterThan(0);
    expect(protectedRequests.every(
      (request) => request.authorization === `Bearer ${keyring.token}`,
    )).toBe(true);
    const evidenceRequests = protectedRequests.filter(
      (request) => new URL(request.url).pathname === qualificationPaths[2],
    );
    expect(evidenceRequests.length).toBeGreaterThan(0);
    expect(evidenceRequests.every((request) => new URL(request.url).search === "")).toBe(true);
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
