# MCP sample evidence (synthetic)

A tiny, committed, **synthetic** evidence root so the read-only MCP evidence
adapter can be demonstrated from a clean clone, without the real
`gpu-proof-retrieved/` archives (which are git-ignored, large, and
`EXTERNAL_ONLY`).

**This is not real evidence.** Every record here is labelled
`SAMPLE_SYNTHETIC`/`SAMPLE_SYNTHETIC_NOT_EVIDENCE`; the archives contain a short
text placeholder, not a captured run. It exists only to exercise the adapter's
shape and guards.

## Demo

```bash
PYTHONPATH=src python -m inferdrome.mcp list-runs \
  --evidence-root examples/mcp-sample-evidence

PYTHONPATH=src python -m inferdrome.mcp verify-evidence \
  --evidence-root examples/mcp-sample-evidence --run-id sample-a10-0001

PYTHONPATH=src python -m inferdrome.mcp compare-runs \
  --evidence-root examples/mcp-sample-evidence \
  --baseline sample-a10-0001 --candidate sample-a100-0002
```

`verify-evidence` recomputes the archive SHA-256 and reports `VERIFIED`.
`compare-runs` reports `INCOMPARABLE` because the two runs differ on
`gpu_model` (A10 vs A100), while still returning the paired metric deltas — the
adapter reports differences and defers the verdict, it never crowns a winner.

Point `--evidence-root` at a real retrieved store to run the same commands on
real bundles.
