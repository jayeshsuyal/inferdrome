# Inferdrome v0.3 development capability boundary

Inferdrome v0.3 remains a measurement and evidence plane for changes to
open-weight inference systems. The current local implementation links supplied
configuration and observation identity to a routing decision, selected endpoint,
and endpoint outcome without becoming a production router, a policy verdict,
or a promotion authority.

The active machine-readable contract is available through:

```sh
python -m inferdrome capabilities
```

Its claims are deliberately narrow:

| Capability | Current status | Honest boundary |
| --- | --- | --- |
| Local two-endpoint routing execution | `PROVEN_LOCAL_SOCKET_LEVEL` | Loopback/socket proof only; no GPU, Docker, cloud, or production-router claim. |
| Attached external-router evidence | `LOCAL_FIXTURE_VALIDATED` | `llm-d-attached-v1` validates local supplied facts; it does not embed llm-d or claim a native llm-d API, live cluster, or observed router. |
| Stale-telemetry qualification | `LOCAL_DETERMINISTIC_VALIDATED` | One virtual-time, six-request fresh-health/stale-load vector is sealed and replayed locally; it is not a model run, live router observation, or a general routing conclusion. |
| Historical Qwen3-8B A10 evidence | `PRESERVED_EXTERNAL_ONLY` | Raw archive remains `EXTERNAL_ONLY`; it is not a v0.3 campaign or acceptance decision. |
| Two-A100 multi-endpoint campaign | `UNEXECUTED` | No real two-A100 campaign evidence exists. |
| GCP operation | `LOCAL_FAKE_VALIDATED` | No provider operation, invoice fact, GPU campaign, or production execution is claimed. |
| Kubernetes operation | `NOT_CLAIMED` | Static/local-synthetic boundaries only; no cluster operation is claimed. |

The contract never issues `PASS`, `FAIL`, `NOT_PROVEN`, a routing winner, or a
promotion recommendation. Content in this repository never self-authorizes a
tag, release, provider action, or live evidence campaign.
