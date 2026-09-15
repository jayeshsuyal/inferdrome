import { expect, test, type Page } from "@playwright/test";
import { spawn, spawnSync, type ChildProcess } from "node:child_process";
import { once } from "node:events";
import {
  chmodSync, existsSync, lstatSync, mkdirSync, mkdtempSync, readFileSync,
  readdirSync, realpathSync, rmSync, writeFileSync,
} from "node:fs";
import { createServer } from "node:net";
import { tmpdir } from "node:os";
import { delimiter, dirname, isAbsolute, join, resolve, sep } from "node:path";
import { fileURLToPath } from "node:url";
import type { RoutingExecutionDetail } from "../src/lib/types";

const REPOSITORY_ROOT = resolve(dirname(fileURLToPath(import.meta.url)), "../..");
const FIXTURE_PREFIX = "inferdrome-vast-dashboard-e2e-";
const EXECUTION_PATH = "/routing-executions/routing-execution-v1";
const FIXTURE_PROVENANCE = "FIXTURE_GENERATED_LOCAL_NOT_PROVIDER_EVIDENCE";

interface Fixture {
  readonly root: string;
  readonly runs: string;
  readonly package: string;
  readonly digest: string;
}

let fixture: Fixture | undefined;
let dashboardServer: ChildProcess | undefined;
let serverLog = "";
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

function pythonEnvironment(): NodeJS.ProcessEnv {
  return {
    ...process.env,
    PYTHONDONTWRITEBYTECODE: "1",
    PYTHONPATH: [join(REPOSITORY_ROOT, "src"), process.env.PYTHONPATH].filter(Boolean).join(delimiter),
  };
}

function makeTreeWritable(path: string): void {
  if (!existsSync(path)) return;
  const metadata = lstatSync(path);
  if (metadata.isSymbolicLink()) return;
  if (metadata.isDirectory()) {
    chmodSync(path, 0o700);
    for (const entry of readdirSync(path)) makeTreeWritable(join(path, entry));
  } else {
    chmodSync(path, 0o600);
  }
}

function removeFixture(root: string): void {
  const resolved = resolve(root);
  if (
    !resolved.startsWith(`${realpathSync(tmpdir())}${sep}`)
    || !resolved.split(sep).at(-1)?.startsWith(FIXTURE_PREFIX)
    || lstatSync(resolved).isSymbolicLink()
  ) throw new Error("Refusing to remove an unexpected Vast E2E fixture path.");
  makeTreeWritable(resolved);
  rmSync(resolved, { recursive: true, force: true });
}

function prepareFixture(): Fixture {
  // Only this owned tree is mutable; the generated package is sealed before
  // the actual dashboard reader sees it. No GPU, model, or provider is used.
  const root = realpathSync(mkdtempSync(join(tmpdir(), FIXTURE_PREFIX)));
  try {
    const runs = join(root, "runs");
    const packagePath = join(root, "routing-execution-package");
    mkdirSync(runs, { mode: 0o700 });
    const result = spawnSync(pythonExecutable(), ["-c", [
      "import json, sys",
      "from pathlib import Path",
      "from tests.unit.test_vast_execution import seal_vast_fixture",
      "from inferdrome.routing_execution.verifier import verify_execution_package",
      "package = seal_vast_fixture(Path(sys.argv[1]), source_commit='a' * 40)",
      "verified = verify_execution_package(package)",
      "print(json.dumps({'package': str(package), 'retained_digest': verified.report.retained_digest}))",
    ].join("\n"), packagePath], {
      cwd: REPOSITORY_ROOT,
      env: pythonEnvironment(),
      encoding: "utf8",
      timeout: 30_000,
      maxBuffer: 1024 * 1024,
    });
    if (result.error) throw result.error;
    if (result.status !== 0) throw new Error(`Synthetic Vast fixture failed: ${result.stderr || result.stdout}`);
    const prepared = JSON.parse(result.stdout) as { package: string; retained_digest: string };
    if (prepared.package !== packagePath || !/^sha256:[0-9a-f]{64}$/.test(prepared.retained_digest)) {
      throw new Error("Synthetic Vast fixture returned an unexpected package identity.");
    }
    // Provenance stays outside the closed package, whose schema is unmodified.
    writeFileSync(join(root, "fixture-provenance.json"), JSON.stringify({
      fixture_provenance: FIXTURE_PROVENANCE,
      source_commit: "a".repeat(40),
      package_retained_digest: prepared.retained_digest,
      transport: "StaticEndpointTransport",
      clock: "ManualMonotonicClock",
      provider_action: "NONE", docker_action: "NONE", gpu_action: "NONE",
    }), { mode: 0o600, flag: "wx" });
    return { root, runs, package: packagePath, digest: prepared.retained_digest };
  } catch (error) {
    removeFixture(root);
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
        reject(new Error("Could not allocate a Vast dashboard loopback port."));
        return;
      }
      server.close((error) => error ? reject(error) : resolvePort(address.port));
    });
  });
}

async function startServer(paths: Fixture): Promise<string> {
  const port = await availablePort();
  let spawnError: Error | undefined;
  serverLog = "";
  dashboardServer = spawn(pythonExecutable(), [
    "-m", "inferdrome", "dashboard", "--runs-root", paths.runs,
    "--trial-sets-root", join(paths.root, "trial-sets"),
    "--comparison-plans-root", join(paths.root, "comparison-plans"),
    "--comparison-results-root", join(paths.root, "comparison-results"),
    "--routing-execution-root", paths.package,
    "--routing-execution-digest", paths.digest, "--port", String(port),
  ], { cwd: REPOSITORY_ROOT, env: pythonEnvironment(), stdio: ["ignore", "pipe", "pipe"] });
  dashboardServer.once("error", (error) => { spawnError = error; });
  const appendLog = (chunk: Buffer) => { serverLog = `${serverLog}${chunk.toString("utf8")}`.slice(-20_000); };
  dashboardServer.stdout?.on("data", appendLog);
  dashboardServer.stderr?.on("data", appendLog);
  const url = `http://127.0.0.1:${port}`;
  const deadline = Date.now() + 20_000;
  while (Date.now() < deadline) {
    if (spawnError) throw spawnError;
    if (dashboardServer.exitCode !== null || dashboardServer.signalCode !== null) {
      throw new Error(`Vast dashboard exited during startup.\n${serverLog}`);
    }
    try {
      if ((await fetch(`${url}/api/v1/health`)).ok) return url;
    } catch {
      // A refused loopback socket is expected before uvicorn is ready.
    }
    await new Promise((resolveDelay) => setTimeout(resolveDelay, 100));
  }
  throw new Error(`Vast dashboard did not become ready.\n${serverLog}`);
}

async function stopServer(): Promise<void> {
  const server = dashboardServer;
  dashboardServer = undefined;
  if (!server || server.exitCode !== null || server.signalCode !== null) return;
  const gracefulExit = once(server, "exit");
  server.kill("SIGTERM");
  await Promise.race([gracefulExit, new Promise((done) => setTimeout(done, 5_000))]);
  if (server.exitCode === null && server.signalCode === null) {
    const forcedExit = once(server, "exit");
    server.kill("SIGKILL");
    await Promise.race([forcedExit, new Promise((done) => setTimeout(done, 5_000))]);
  }
  if (server.exitCode === null && server.signalCode === null) {
    throw new Error("Vast dashboard did not stop after SIGKILL.");
  }
}

async function expectNoPageOverflow(page: Page): Promise<void> {
  expect(await page.evaluate(() => document.documentElement.scrollWidth)).toBeLessThanOrEqual(
    await page.evaluate(() => document.documentElement.clientWidth),
  );
}

test.describe("packaged synthetic Vast execution", () => {
  test.beforeAll(async () => {
    try {
      fixture = prepareFixture();
      baseUrl = await startServer(fixture);
    } catch (error) {
      try { await stopServer(); } finally {
        if (fixture) removeFixture(fixture.root);
        fixture = undefined;
      }
      throw error;
    }
  });

  test.afterAll(async () => {
    try { await stopServer(); } finally {
      if (fixture) removeFixture(fixture.root);
      fixture = undefined;
    }
  });

  for (const width of [1440, 390]) {
    test(`imports the shared-container v4 ledger at ${width}px through detail and reload`, async ({ page }) => {
      if (!fixture) throw new Error("The owned Vast fixture is unavailable.");
      await page.setViewportSize({ width, height: 900 });
      const errors: string[] = [];
      const failedResponses: string[] = [];
      const unexpectedRequests: string[] = [];
      page.on("console", (message) => { if (message.type() === "error") errors.push(message.text()); });
      page.on("pageerror", (error) => errors.push(error.message));
      page.on("requestfailed", (request) => {
        if (request.failure()?.errorText !== "net::ERR_ABORTED") errors.push(`${request.url()}: ${request.failure()?.errorText}`);
      });
      page.on("response", (response) => {
        if (response.status() >= 400) failedResponses.push(`${response.status()} ${response.url()}`);
      });
      page.on("request", (request) => {
        if (new URL(request.url()).origin !== baseUrl || request.method() !== "GET") {
          unexpectedRequests.push(`${request.method()} ${request.url()}`);
        }
      });

      const provenance = JSON.parse(readFileSync(join(fixture.root, "fixture-provenance.json"), "utf8"));
      expect(provenance).toMatchObject({
        fixture_provenance: FIXTURE_PROVENANCE, package_retained_digest: fixture.digest,
        transport: "StaticEndpointTransport", clock: "ManualMonotonicClock",
        provider_action: "NONE", docker_action: "NONE", gpu_action: "NONE",
      });
      const manifest = JSON.parse(readFileSync(join(fixture.package, "executed-manifest.json"), "utf8"));
      expect(manifest.schema_version).toBe("inferdrome.routing-executed-manifest.v4");
      const indexResponse = await page.request.get(`${baseUrl}/api/v1/routing-executions`);
      const detailResponse = await page.request.get(`${baseUrl}/api/v1${EXECUTION_PATH}`);
      expect(indexResponse.status()).toBe(200);
      expect(detailResponse.status()).toBe(200);
      const index = await indexResponse.json();
      const detail = await detailResponse.json() as RoutingExecutionDetail;
      expect(index.rejected).toEqual([]);
      expect(index.routing_executions).toEqual([detail.summary]);
      expect(detail.summary).toMatchObject({
        retained_digest: fixture.digest, mode: "VAST_MANUAL_CONTAINER",
        source_commit: "a".repeat(40), terminal_denominator: 18,
        topology: {
          profile_id: "vast-container-two-h100-sxm5-80gb-v1",
          accelerator_model: "NVIDIA H100-SXM5-80GB", accelerator_count: 2,
          container_count: 1, serving_engine_count: 2, tensor_parallel_size: 1,
          isolation_boundary: "SEPARATE_PROCESSES_SHARED_CONTAINER",
          observer_gpu_isolation: "ENVIRONMENT_ONLY_NOT_HARDWARE_ENFORCED",
        },
      });
      expect(detail.evidence.container_image).toBe(manifest.container_image.reference);
      expect(detail.evidence.artifact_provenance).toEqual(manifest.artifact_provenance);
      expect(detail.evidence).not.toHaveProperty("runner_image");
      expect(detail.evidence).not.toHaveProperty("serving_image");
      expect(detail.summary.topology).not.toHaveProperty("runner_separate_from_serving");
      expect(detail.trials.map((trial) => trial.terminal_population_total)).toEqual([6, 6, 6]);
      const serialized = JSON.stringify(detail);
      for (const privateValue of [fixture.root, REPOSITORY_ROOT, "127.0.0.1:8000", "127.0.0.1:8001", "instance_identity_sha256", "gpu_uuid_sha256", "origin_sha256", "request_body"]) {
        expect(serialized).not.toContain(privateValue);
      }

      const expectIndex = async () => {
        await expect(page.getByRole("heading", { name: "Routing executions", level: 1 })).toBeVisible();
        const presentation = page.locator(width < 600 ? ".execution-card" : ".execution-table tbody tr");
        await expect(presentation).toBeVisible();
        await expect(presentation.getByText("VAST_MANUAL_CONTAINER", { exact: true })).toBeVisible();
        await expect(presentation.getByText(/2 serving processes share 1 container/)).toBeVisible();
        await expect(presentation.getByText(/observer GPU isolation is environment only/)).toBeVisible();
        await expectNoPageOverflow(page);
        return presentation;
      };
      const expectDetail = async () => {
        await expect(page.getByRole("heading", { name: "routing-execution-v1", level: 1 })).toBeVisible();
        await expect(page.getByText("Container image", { exact: true })).toBeVisible();
        await expect(page.getByTitle(manifest.container_image.reference, { exact: true })).toBeVisible();
        await expect(page.getByText("Runner image", { exact: true })).toHaveCount(0);
        await expect(page.getByText("Serving image", { exact: true })).toHaveCount(0);
        await expect(page.getByText("1 container / 2 serving processes", { exact: true })).toBeVisible();
        await expect(page.getByText("1 per serving engine", { exact: true })).toBeVisible();
        await expect(page.getByText("SEPARATE_PROCESSES_SHARED_CONTAINER", { exact: true })).toBeVisible();
        await expect(page.getByText("ENVIRONMENT_ONLY_NOT_HARDWARE_ENFORCED", { exact: true })).toBeVisible();
        await expect(page.getByText("Unverified; watchdog boundary unresolved", { exact: true })).toBeVisible();
        await expect(page.getByText(/This record does not prove provider allocation, image attestation, or cleanup/)).toBeVisible();
        await expect(page.getByText("OPERATOR_DECLARED_NOT_OBSERVED", { exact: true })).toBeVisible();
        await expect(page.getByText("LOCAL_PROCESS_OBSERVATIONS_NOT_PROVIDER_ATTESTATION", { exact: true })).toBeVisible();
        for (const [field, value] of Object.entries(manifest.artifact_provenance)) {
          if (field.endsWith("_sha256")) await expect(page.getByTitle(value as string, { exact: true })).toBeVisible();
        }
        await expect(page.getByText("18 / 18", { exact: true })).toBeVisible();
        await expect(page.locator(".execution-trial-section")).toHaveCount(3);
        await expect(page.locator(".execution-request-table tbody tr")).toHaveCount(18);
        await expect(page.locator(".execution-population-total strong")).toHaveText(["6", "6", "6"]);
        await expect(page.getByText("GPU/DCGM and KV/cache retained as unavailable", { exact: true })).toBeVisible();
        const html = await page.locator("body").innerHTML();
        for (const privateValue of [fixture!.root, REPOSITORY_ROOT, "127.0.0.1:8000", "127.0.0.1:8001"]) {
          expect(html).not.toContain(privateValue);
        }
        await expectNoPageOverflow(page);
      };

      expect((await page.goto(`${baseUrl}/routing-executions`))?.status()).toBe(200);
      const presentation = await expectIndex();
      await presentation.getByRole("link", { name: "routing-execution-v1", exact: true }).click();
      await expect(page).toHaveURL(`${baseUrl}${EXECUTION_PATH}`);
      await expectDetail();
      await page.locator(".execution-back-link").getByRole("link", { name: "Routing executions", exact: true }).click();
      await expect(page).toHaveURL(`${baseUrl}/routing-executions`);
      await expectIndex();
      expect((await page.goto(`${baseUrl}${EXECUTION_PATH}`))?.status()).toBe(200);
      await expectDetail();
      expect((await page.reload())?.status()).toBe(200);
      await expectDetail();
      expect(errors).toEqual([]);
      expect(failedResponses).toEqual([]);
      expect(unexpectedRequests).toEqual([]);
    });
  }
});
