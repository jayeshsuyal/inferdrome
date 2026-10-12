# vLLM HTTP connection lifetimes

Inferdrome explicitly bounds idle connections between the router and its two
independent vLLM replicas. These settings reduce exposure to idle connection
closure; they do not establish the cause of a historical transport failure.

## Settings

| Setting | Default | Configuration |
| --- | --- | --- |
| Router upstream pool idle limit | 1 second | `Router(..., upstream_keepalive_s=1.0)` or `--upstream-keepalive-s 1.0` |
| Upstream connection reuse | Enabled | `reuse_upstream_connections=False` or `--no-upstream-reuse` disables reuse |
| Owned vLLM server idle timeout | 30 seconds | Engine launchers set `VLLM_HTTP_TIMEOUT_KEEP_ALIVE=30` |

For an independently launched server, set that environment variable when
starting each vLLM process. The standalone router cannot configure its servers.
Keep the router idle limit below the effective server idle timeout.

```bash
python -m inferdrome.vllm_router \
  --replica-a http://127.0.0.1:8001 \
  --replica-b http://127.0.0.1:8002 \
  --ledger ./router-results.jsonl \
  --policy round_robin --upstream-keepalive-s 1.0
```

Add `--no-upstream-reuse` for the diagnostic control that opens a fresh upstream
connection per POST. This changes connection overhead. `/router/stats` reports
`upstream_connection_reuse` and `upstream_keepalive_s`; the latter is `null` when
reuse is disabled.

The idle limit starts when a completed response returns its connection to the
pool. It does not limit the duration of an active SSE response. Existing request
deadlines and cancellation still apply. In fresh mode, the connection closes
after its response instead of returning to the pool.

## Why make this explicit?

vLLM 0.26.0 defaults `VLLM_HTTP_TIMEOUT_KEEP_ALIVE` to 5 seconds and passes it to
its HTTP server. aiohttp 3.13.5 defaults pooled idle retention to 15 seconds;
it checks connection age again when acquiring a pooled connection. A shorter
client limit leaves a margin before the configured server idle timeout.
Neither that margin nor fresh connections prevent every peer closure or race.
See the pinned [vLLM environment definition](https://github.com/vllm-project/vllm/blob/v0.26.0/vllm/envs.py),
[vLLM server configuration](https://github.com/vllm-project/vllm/blob/v0.26.0/vllm/entrypoints/openai/api_server.py),
and [aiohttp connector implementation](https://docs.aiohttp.org/en/v3.13.5/_modules/aiohttp/connector.html).

Inference POSTs are **never retried** by this change. aiohttp's internal
persistent-connection retry allowlist excludes POST, and the router adds no
retry or fallback POST. A transport failure retains its ledger outcome,
diagnostics and study eligibility consequences.
See [aiohttp 3.13.5 request handling](https://github.com/aio-libs/aiohttp/blob/v3.13.5/aiohttp/client.py).

## Configuration is not runtime proof

Study/session `http_transport` metadata records the requested client and server
settings with provenance `CONFIGURED_NOT_RUNTIME_ATTESTED`. Router stats expose
its configured client behavior; neither record independently attests the
engine's effective timeout.

A future execution must retain the source revision, package versions and a
separate observation of the selected engine environment value. Capture only
that value, not the whole environment. The upstream default is a source fact;
it does not prove which value an earlier process inherited. A diagnostic reuse
event also does not prove a stale connection, remote acceptance of a POST or
the server's reason for closing it.

## CPU checks and evidence boundary

Real loopback TCP fixtures use fake streaming replicas, complete HTTP/SSE
framing, controlled idle gaps and server-side closure. They count accepted
connections and consumed POSTs, then check router outcomes and diagnostics.
The [connection fixtures](../tests/integration/test_vllm_router_connections.py)
check reuse within the idle limit, retirement before the peer timeout, fresh
mode and one POST on failure, with diagnostics enabled and disabled. A test-only
trace hook orders a real peer FIN after pool selection to exercise that race;
it does not inject an exception or reproduce the historical event. The
[transport fixtures](../tests/integration/test_vllm_router_transport.py) also
cover closure before headers and truncated streams.

```bash
PYTHONPATH=src python -m pytest \
  tests/integration/test_vllm_router_connections.py \
  tests/integration/test_vllm_router_transport.py \
  tests/unit/test_vllm_http_transport.py
```

These checks establish local transport behavior. They do not reproduce GPU
scheduling, prove a historical root cause or measure a serving-performance gain.
No new GPU result is claimed here.

A full study after this change needs a fresh source revision, preparation bundle
and execution approval bound to the new pins. Apply the same transport settings
throughout all 32 windows and retain the existing 23,040-request population,
separate warmups and strict comparison gates. Preserve historical releases,
sealed preparations and raw archives; failed earlier windows cannot be combined
into a completed new study.

See [Breakpoint pilot methodology and transport diagnostics](BREAKPOINT_PILOT.md).
