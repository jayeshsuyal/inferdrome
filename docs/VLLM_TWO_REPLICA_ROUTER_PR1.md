# Two-replica vLLM streaming router (PR1)

This local serving path accepts streaming OpenAI chat completions and forwards
each request to one of two **independent** vLLM servers. Start each replica with
the same pinned model and `--tensor-parallel-size 1`, one server per GPU. The
router does not start or manage GPUs. It accepts literal loopback origins only,
so use local ports or separately managed tunnels to both servers.

```sh
python -m inferdrome.vllm_router \
  --replica-a http://127.0.0.1:8001 \
  --replica-b http://127.0.0.1:8002 \
  --ledger ./router-results.jsonl \
  --policy round_robin --max-active 32 --max-queue 32
```

The listener defaults to `127.0.0.1:8090`. Send `POST /v1/chat/completions`
with JSON and `"stream": true`; response bytes are forwarded as SSE. Use
`--policy least_busy` for the other baseline. Least-busy observes the router's
own active request count, not vLLM's scheduler or queue telemetry. A request
waiting for admission has not yet been assigned a replica.

`GET /router/stats` reports offered, terminal, in-flight, queue, active
per-replica, and outcome counts. The append-only JSONL ledger records one
terminal row per offered request, including rejections and disconnects. Rows
include a random request ID, selected replica, monotonic arrival-to-first-byte
and terminal times, queued time, HTTP status, byte count, and outcome. The
router does not log prompts, generated text, or raw upstream errors. Protect
the ledger as experiment data. A full disk or ledger write failure is fatal to
accounting and must invalidate a run.

Admission is bounded by `max-active + max-queue`; waiting requests time out
after five seconds by default. The proxy reads at most 16 KiB per upstream
operation and awaits downstream writes, so a slow client applies backpressure.
A closed downstream socket closes the upstream response, cancelling generation.
Requests have a 120-second upstream deadline; input and stream byte limits are
256 KiB and 16 MiB. The router checks successful SSE framing using the existing
evaluation parser. A failure after HTTP 200 begins is recorded in the ledger;
HTTP status alone cannot describe a partial stream.

Local socket tests use fake SSE replicas and cover byte-for-byte forwarding,
both policies, capacity and queue rejection, disconnect propagation, and
one-row-per-offered accounting. This PR establishes a runnable software path,
not a GPU latency result. PR2 will add cache policies, changing-hotspot trace,
and frozen evaluation rules; PR3 will run balanced, repeated real-GPU blocks.
The earlier one-A100 capacity sweep remains separate evidence and cannot
establish two-replica routing performance.
