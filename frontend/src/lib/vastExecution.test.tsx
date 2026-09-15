import { render, screen, within } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { RoutingExecutionDetailView } from "../views/RoutingExecutionDetailView";
import { RoutingExecutionsView } from "../views/RoutingExecutionsView";
import fixture from "../test/vastExecutionFixture.json";
import { api } from "./api";
import { MemoryRouter } from "./router";

// Captured from the actual JSON detail endpoint over seal_vast_fixture(), with
// source_commit='a'*40, StaticEndpointTransport, and ManualMonotonicClock.
// This is synthetic local evidence, not a Vast allocation or GPU execution.
// tests/dashboard/test_vast_execution_dashboard.py regenerates and verifies
// the sealed v4 package before exercising the actual index/detail endpoints.
function executionIndex(summary: unknown = fixture.summary) {
  return {
    projection_version: fixture.projection_version,
    routing_executions: [summary],
    rejected: [],
    page: { limit: 25, returned: 1, total: 1, has_more: false, next_cursor: null },
  };
}

function jsonResponse(payload: unknown) {
  return new Response(JSON.stringify(payload), {
    status: 200,
    headers: { "Content-Type": "application/json", "Cache-Control": "no-store" },
  });
}

function serve(payload: unknown) {
  const fetchMock = vi.fn(async () => jsonResponse(payload));
  vi.stubGlobal("fetch", fetchMock);
  return fetchMock;
}

afterEach(() => vi.unstubAllGlobals());

describe("Vast v4 execution import", () => {
  it("accepts the actual sealed synthetic v4 index and detail without inventing legacy images", async () => {
    const fetchMock = vi.fn()
      .mockResolvedValueOnce(jsonResponse(executionIndex()))
      .mockResolvedValueOnce(jsonResponse(fixture));
    vi.stubGlobal("fetch", fetchMock);

    const index = await api.listRoutingExecutions();
    const detail = await api.getRoutingExecution(fixture.summary.execution_id);

    expect(index.rejected).toEqual([]);
    expect(index.routing_executions).toEqual([detail.summary]);
    expect(detail).toEqual(fixture);
    expect(detail.summary.mode).toBe("VAST_MANUAL_CONTAINER");
    expect(detail.evidence).not.toHaveProperty("runner_image");
    expect(detail.evidence).not.toHaveProperty("serving_image");
    expect(detail.summary.topology).not.toHaveProperty("runner_separate_from_serving");
    expect(fetchMock.mock.calls.map(([url]) => url)).toEqual([
      "/api/v1/routing-executions?limit=25",
      "/api/v1/routing-executions/routing-execution-v1",
    ]);
  });

  it.each([
    ["profile_id", "manual-host-two-h100-sxm5-80gb-v1"],
    ["accelerator_model", "NVIDIA A100-PCIE-40GB"],
    ["accelerator_count", 1],
    ["container_count", 2],
    ["serving_engine_count", 1],
    ["one_engine_per_endpoint", false],
    ["tensor_parallel_size", 2],
    ["declared_provider", "LAMBDA"],
    ["declared_provisioning", "OPERATOR_SUPPLIED_VM"],
    ["identity_assertion", "PROVIDER_ATTESTED"],
    ["lifecycle_protection", "CLEANUP_VERIFIED"],
    ["isolation_boundary", "SEPARATE_CONTAINERS"],
    ["observer_gpu_isolation", "HARDWARE_ENFORCED"],
    ["runner_separate_from_serving", true],
  ])("rejects a Vast topology with %s=%s", async (key, value) => {
    serve(executionIndex({
      ...fixture.summary,
      topology: { ...fixture.summary.topology, [key]: value },
    }));
    await expect(api.listRoutingExecutions()).rejects.toMatchObject({ status: 502 });
  });

  it.each(["LOCAL_LOOPBACK", "GCP_PRIVATE", "LAMBDA_MANUAL_HOST"])(
    "does not admit the Vast shape as legacy mode %s",
    async (mode) => {
      serve(executionIndex({ ...fixture.summary, mode }));
      await expect(api.listRoutingExecutions()).rejects.toMatchObject({ status: 502 });
    },
  );

  it.each([
    ["container_image_assertion", "PROVIDER_ATTESTED"],
    ["source_commit", "b".repeat(40)],
    ["observer_artifact_sha256", "sha256:invalid"],
    ["supervisor_artifact_sha256", null],
    ["model_manifest_sha256", ""],
    ["model_snapshot_sha256", 42],
    ["runtime_observation_sha256", "a".repeat(64)],
    ["runtime_assertion", "PROVIDER_ATTESTED"],
    ["provider_attestation", true],
  ])("rejects an invalid or overstated artifact provenance %s", async (key, value) => {
    serve({
      ...fixture,
      evidence: {
        ...fixture.evidence,
        artifact_provenance: { ...fixture.evidence.artifact_provenance, [key]: value },
      },
    });
    await expect(api.getRoutingExecution(fixture.summary.execution_id)).rejects.toMatchObject({ status: 502 });
  });

  it.each([
    { runner_image: fixture.evidence.container_image },
    { serving_image: fixture.evidence.container_image },
    { container_image: "example.invalid/synthetic-vast:latest" },
    { container_image: undefined },
    { artifact_provenance: null },
    { artifact_provenance: [] },
  ])("rejects an invalid single-container evidence shape %j", async (changes) => {
    serve({ ...fixture, evidence: { ...fixture.evidence, ...changes } });
    await expect(api.getRoutingExecution(fixture.summary.execution_id)).rejects.toMatchObject({ status: 502 });
  });
});

describe("Vast execution presentation", () => {
  it("shows the shared-container and environment-only boundary in both list presentations", async () => {
    serve(executionIndex());
    const { container } = render(<MemoryRouter><RoutingExecutionsView /></MemoryRouter>);

    const table = await screen.findByRole("table", { name: "Verified execution ledger" });
    const card = container.querySelector(".execution-card") as HTMLElement;
    for (const scope of [table, card]) {
      expect(within(scope).getByText("VAST_MANUAL_CONTAINER")).toBeInTheDocument();
      expect(within(scope).getByText(/2 serving processes share 1 container/)).toHaveTextContent(
        "observer GPU isolation is environment only",
      );
    }
  });

  it("renders one image, declared process topology, and bounded artifact provenance", async () => {
    serve(fixture);
    render(
      <MemoryRouter initialEntries={["/routing-executions/routing-execution-v1"]}>
        <RoutingExecutionDetailView />
      </MemoryRouter>,
    );

    await screen.findByRole("heading", { name: "Declared process topology" });
    expect(screen.getByText("Container image")).toBeInTheDocument();
    expect(screen.getByTitle(fixture.evidence.container_image)).toBeInTheDocument();
    expect(screen.queryByText("Runner image")).not.toBeInTheDocument();
    expect(screen.queryByText("Serving image")).not.toBeInTheDocument();
    expect(screen.getByText("1 container / 2 serving processes")).toBeInTheDocument();
    expect(screen.getByText("1 per serving engine")).toBeInTheDocument();
    expect(screen.getByText(fixture.summary.topology.profile_id)).toBeInTheDocument();
    expect(screen.getByText("SEPARATE_PROCESSES_SHARED_CONTAINER")).toBeInTheDocument();
    expect(screen.getByText("ENVIRONMENT_ONLY_NOT_HARDWARE_ENFORCED")).toBeInTheDocument();
    expect(screen.getByText("Unverified; watchdog boundary unresolved")).toBeInTheDocument();
    expect(screen.getByText(/Two serving processes share one container/)).toHaveTextContent(
      "This record does not prove provider allocation, image attestation, or cleanup.",
    );
    expect(screen.getByText("OPERATOR_DECLARED_NOT_OBSERVED")).toBeInTheDocument();
    expect(screen.getByText("LOCAL_PROCESS_OBSERVATIONS_NOT_PROVIDER_ATTESTATION")).toBeInTheDocument();
    for (const [field, value] of Object.entries(fixture.evidence.artifact_provenance)) {
      if (field.endsWith("_sha256")) expect(screen.getByTitle(value)).toBeInTheDocument();
    }
    expect(screen.getByRole("link", { name: "Routing executions" })).toHaveAttribute(
      "href", "/routing-executions",
    );
  });
});
