# Lambda manual-host campaign preparation v1

This is a local preparation bridge for the existing Dockerized Qwen3-8B
two-engine routing campaign. It does not provision a host, grant execution
authority, read credentials, query capacity or prices, or satisfy the automated
prelaunch-watchdog boundary. No Lambda/GPU execution is claimed by this PR.

## One local preparation command

From the reviewed checkout, using Python 3.12 and the `uv.lock` dependencies:

```sh
python -m inferdrome.deployment.manual_host prepare \
  --input operator-host.json --output prepared-manual-host
```

Start the input from
`deployments/manual-host-v1/lambda-two-a100-pcie.input-template.json`, or inspect
it with `python -m inferdrome.deployment.manual_host template`. Every unknown
real value is `null`, intentionally invalid. There are no sample approved
quotes, credentials, invented image digests, host observations, or valid-looking
approval records. All required declarations must be supplied before preparation
succeeds. Errors are sanitized; the output directory is private and create-only.
An interrupted partial preparation is not success and is never overwritten.

The output is eight files: `operator-input.json`, `deployment-config.json`,
`selected-workload.jsonl`, `compose.manual-host.json`, `cleanup-handoff.json`,
`plan.json`, `startup.sh`, and `preparation-integrity.json`. They are invocation
inputs and a deterministic startup plan, **not execution evidence**. They include
private operator/host paths and IDs; do not publish them as redacted evidence.
Preparation reads local input, the existing Compose template, and its Git blob;
it does not inspect the local machine as if a Mac were the target Linux host.

## Exact inputs and source binding

Supply the exact Lambda instance ID (32 lowercase hexadecimal characters,
without UUID hyphens), region, API instance type (including underscores), source
commit, and two distinct NVIDIA GPU UUIDs (`GPU-` followed by a hyphenated UUID).
Instance ID and cleanup target must match exactly. IDs are operator declarations;
this tool does not establish provider ownership from a list difference.

Supply distinct immutable serving and CPU-runner OCI role images built with the
existing `Dockerfile.vllm-benchmark-runner`, exact source-revision labels, Linux x86_64,
non-root UID/GID, three non-overlapping absolute host directories (model,
preparation, evidence), unused Compose project, dedicated non-conflicting RFC1918
container subnet and two fixed container IPs, and request timeout. The host needs
the same reviewed source installed with locked Python 3.12 dependencies, Docker
Compose, and NVIDIA Container Toolkit. All images and model files must already
be loaded; startup never builds or pulls them. Host UID must equal the runner
UID so the existing owner-controlled evidence seal remains usable offline.

The input retains the frozen Qwen3 model/tokenizer revision, model snapshot and
manifest digests, vLLM 0.26.0 / adapter identity, full workload identity, fixed
selected-six identity, and four frozen R1 input digests. Missing or mismatched
pins fail closed. The exact consumed `compose.gpu.yaml` bytes must equal the Git
blob at the supplied source commit, which must exist locally. The plan also
records their SHA-256. This verifies a template binding, not image build
provenance; source/image labels remain separately checked operator inputs.

## Distinct hardware profile and compatible evidence

| Profile | Declared device per engine | Allocation |
| --- | --- | --- |
| New `lambda-manual-two-a100-pcie-40gb-v1` | A100 PCIe 40 GB | One UUID per engine, two distinct GPUs, TP=1 |
| Existing GCP `a2-highgpu-2g` | A100 SXM4 40 GB | Unchanged existing profile |

The documented Lambda match is one VM with **2 × A100 PCIe 40 GB**. It is not
SXM4 and not host-failure independence. Neither current availability nor price
has been observed. The profile records both observations as null.

New `routing-execution-config.v2` and `routing-executed-manifest.v2` admit only
`LAMBDA_MANUAL_HOST`. Old v1 literals, schemas, packages, and verification remain
unchanged; v1 still rejects manual-host/PCIe. V2 retains actual provisioning as
`LAMBDA` / `OPERATOR_SUPPLIED_VM`, with hashed declaration/host/GPU bindings and
`OPERATOR_DECLARED_NOT_OBSERVED`. Runtime driver/container checks are local
observations, not a provider attestation, and do not rewrite those declarations
into provider proof. Raw instance IDs, GPU UUIDs and operator identity do not go
into the execution package.

The runner command is still `python -m inferdrome.routing_execution run`, with
three fixed policies, six requests each, 18 terminal receipts, no retries,
independent real `/health` and `vllm:num_requests_running` collection, the
existing fault schedule, evidence sealing, and the existing offline verifier.
GPU/DCGM and KV/cache telemetry remain `UNAVAILABLE`; inspecting GPU allocation
does not invent a GPU telemetry stream. Reset remains runner connection and
telemetry only: `NOT_ASSERTED_SEPARATE_SERVING_ENGINE`. No serving engine or KV
cache reset is claimed. Live outcomes never enter the synthetic-only
`stale-telemetry-qualification-v1` descriptor.

## Separately authorized manual startup

Generating source or artifacts grants no launch authority. Before a manual run,
the user must explicitly choose this operating mode with its unresolved lifecycle
risk, separately authorize the exact plan, and ensure the accountable operator
can perform exact-ID termination/readback even if the host or runner fails.

Review and externally retain the printed `plan_sha256` for `plan.json`. On the host,
place the preparation at its exact declared path, make the preloaded model and
empty evidence directories accessible to the declared UID/GID, and run
`bash startup.sh` with all five arguments: `--execute-plan`, that exact digest,
`--operator`, the exact accountable-operator ID from the input, and
`--accept-manual-cleanup-risk`. Missing/wrong arguments stop before host commands.
This is an explicit intent guard, not an approval-signature or authorization
service. Do not run it merely because preparation succeeded.

The script verifies plan/input hashes, checks the deadline, Linux/Python/UID,
two exact GPU UUIDs, PCIe device names and 40 GB-class usable driver memory,
MIG disabled, distinct preloaded image contents and role/source/version/platform
labels, unused Compose project, bind access, and the existing frozen snapshot
and tokenizer verification. It starts two read-only, capability-dropped engines
on an internal network with no published ports, waits for `/health`, then checks
running container IDs, image contents, exact UUID allocations and TP=1 before
the CPU-only runner starts. No Docker socket or GPU is mounted into the runner.

The existing bridge records readiness/telemetry/transport failures faithfully;
valid sealed failure populations are evidence, not a successful-inference claim.
A startup/capture/seal/verification failure must never be relabeled success.
After capture, the same offline `verify` command checks the sealed package. Keep
the runner's printed retained package digest externally; later use `verify`
with `--expected-digest` to compare it. Do not add preparation files to the
closed four-file evidence package.

## Cleanup is an operator handoff, not a solved watchdog

The input requires the exact same instance ID, accountable operator, exact UTC
termination deadline, and `OPERATOR_EXACT_ID_TERMINATION_AND_READBACK` handoff.
An expired deadline fails host preflight. The deadline is **not an enforced TTL**.

Every startup exit after container launch attempts scoped Compose cleanup and
prints the operator handoff. Container cleanup failure returns nonzero. Even a
preflight failure leaves the already-provided VM with its operator cleanup duty.
The trap is best-effort guest cleanup, not an independent controller; host loss,
SIGKILL, lost operator access, or process death can defeat it.

Lambda public documentation does not establish provider-enforced TTL or create
idempotency. API keys have all-operation access, and this workflow accepts no
key. Guest shutdown and container stop do **not** end VM billing. The operator
must request termination of the exact ID and independently verify termination;
requested, pending, timeout, unknown, inaccessible, or stopped is not verified.
Escalate failed/ambiguous readback or an unavailable cleanup operator immediately;
billing may continue. This PR does not implement provider creates, blind retries,
list-difference ownership inference, automatic termination, or an approval system.

## Local validation

```sh
python -m inferdrome.deployment.manual_host_schema
python -m pytest tests/unit/test_manual_host.py tests/unit/test_manual_host_preflight.py
./scripts/engineering_gate.sh
```

Tests use injected command output, fake shell executables, and existing local
HTTP/fake transports. They cover exact input/source binding, deterministic plans,
wrong/duplicate UUIDs, PCIe/SXM4 mismatch, missing pins, readiness/capture failures,
v1 compatibility, v2 sealing/replay, intent/tamper gates, and cleanup failures.
Mac tests are not CUDA, Docker-daemon, billing, availability, or performance proof.
