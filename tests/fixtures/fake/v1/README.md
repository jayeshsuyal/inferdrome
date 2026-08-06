# Fake adapter v1 golden fixture

These artifacts are generated from `examples/fake-smoke.yaml` with a fixed run
ID, UTC start time, one successful measured request, and one failed measured
request. They are synthetic and cannot be used as customer evidence.

Regenerate or verify them from the repository root:

```bash
PYTHONPATH=src python scripts/generate_fake_golden.py
PYTHONPATH=src python scripts/generate_fake_golden.py --check
```

`MANIFEST.sha256` protects the committed fixture from accidental drift. It is
not the v0.1 evidence-bundle manifest implemented in PR 4.
