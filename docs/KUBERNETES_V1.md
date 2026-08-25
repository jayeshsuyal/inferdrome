# Minimal Kubernetes Job v1

Status: **static contract and local orchestration simulation; no cluster gate has
been executed in this environment**

This slice adds one `batch/v1` Job shape for Kubernetes 1.33 or newer. The
serving engine is a Kubernetes native sidecar (`initContainers` with
`restartPolicy: Always`) and the one-shot Inferdrome process is the regular Job
container. They share the Pod network namespace and use
`http://127.0.0.1:8000`; Inferdrome measures and vLLM/mock serves.

A separate Service was intentionally rejected. It would introduce a second
network boundary and weaken the frozen runner/serving colocation contract. No
Deployment, Ingress, operator, Helm chart, autoscaler, RBAC controller, or
public Service is part of this contract.

## Static contract

The strict validator and currentness artifact are additive and outside the
frozen evidence schemas:

```bash
.venv/bin/python scripts/generate_kubernetes_contract.py --check
.venv/bin/python -m inferdrome.kubernetes validate \
  --profile mock --manifest kubernetes/inferdrome-benchmark-mock-job.yaml
.venv/bin/python -m inferdrome.kubernetes validate \
  --profile gpu --template \
  --manifest kubernetes/inferdrome-benchmark-gpu-job.template.yaml
```

The YAML parser rejects duplicate keys, multiple documents, unknown contract
fields, unsafe volume sources, public networking, shell construction, mutable
GPU images, GPU assignment to the runner, missing probes/security controls,
and unbound model/runtime/benchmark arguments. It performs no Kubernetes API
call and has bounded, non-disclosing errors.

## Synthetic local mock

The default Job uses the deterministic local mock engine and the explicitly
named `inferdrome-runner-probe`. It is synthetic only:

```bash
INFERDROME_ALLOW_LOCAL_KUBERNETES=1 \
INFERDROME_KUBERNETES_OUTPUT_DIR="$PWD/.inferdrome-kubernetes/evidence" \
  scripts/run_kubernetes_mock_e2e.sh mock --confirm-local-cluster
```

The wrapper is guarded by an explicit confirmation and requires `kind`,
`kubectl`, and prebuilt development images. It creates one unique local kind
cluster and namespace, applies only the checked-in Job, waits for completion,
captures exactly one bounded `kubectl logs` payload from the completed runner,
validates the canonical output as `synthetic_only=true` and
`evidence_eligible=false`, and publishes it locally with no replacement.
It does not use `kubectl cp` or `kubectl exec`: a completed container cannot be
exec'd safely, and the Pod's `emptyDir` is explicitly disposable. The output
is published outside the cluster before the wrapper deletes its exact
namespace and cluster. A cleanup failure returns status 70 and never removes
the already-published local artifact.

The fake `kind`/`kubectl` process tests exercise apply, wait, logs, publication,
failure, cleanup, and interrupt paths. They are orchestration simulations, not
Kubernetes execution or evidence. On a host without Docker/kind/kubectl, the
real local-cluster gate is `UNRUN`, not a successful smoke.

## GPU/vLLM template

`kubernetes/inferdrome-benchmark-gpu-job.template.yaml` is a non-executable
template. It binds the existing Qwen3-8B BF16 campaign identity and vLLM
0.26.0 image digest from the repository source of truth. The vLLM engine is
the only container with exactly one `nvidia.com/gpu`; the runner requests no
GPU and uses the canonical `inferdrome run` command. The endpoint remains
Pod-local and no public address or host namespace is allowed.

The template requires an immutable runner image digest at render time. Model
and experiment PVCs are read-only. Real evidence must use an operator-provided
writable evidence PVC; the template's `REQUIRED_EVIDENCE_PVC` placeholder is
deliberately not a persistence claim or an executable default. The template
does not launch a GPU, run vLLM, retrieve a bundle, issue a receipt, or make an
evidence-eligibility claim. A future operator must separately verify the PVC,
image digests, input snapshot, experiment binding, cluster security policy,
and retrieval/verification path before any authorized execution.

## Cleanup and claim boundary

Both profiles disable Job retries, set bounded deadlines and termination grace,
disable service-account-token automounting, run non-root with RuntimeDefault
seccomp, drop all capabilities, and reject privileged/host-mounted/public
resources. The mock wrapper owns cleanup of only the unique resources it
created. `emptyDir` and Kubernetes logs are not durable evidence storage for a
real run; only the mock's locally published log-derived synthetic artifact is
preserved by this slice. Inferdrome remains a measurement tool, not a
Kubernetes platform or an execution attestor.
