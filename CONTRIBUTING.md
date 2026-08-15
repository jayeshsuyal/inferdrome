# Contributing to Inferdrome

Inferdrome treats measurement integrity, provenance, eligibility, and customer
acceptance as separate facts. Contributions must preserve those boundaries and
fail closed when evidence is missing, malformed, or unsupported.

## Development environment

Use Python 3.12 and a current Node.js 24 runtime. From the repository root:

```bash
python3.12 -m venv .venv
.venv/bin/python -m pip install --editable ".[dev,dashboard]"
npm ci --prefix frontend
npx --prefix frontend playwright install chromium
```

On a clean Linux host, install Chromium's system dependencies as part of the
Playwright step:

```bash
npx --prefix frontend playwright install --with-deps chromium
```

## Required checks

Run both repository gates before requesting review:

```bash
INFERDROME_PYTHON=.venv/bin/python ./scripts/engineering_gate.sh
INFERDROME_PYTHON=.venv/bin/python ./scripts/dashboard_gate.sh
```

The engineering gate checks generated schemas and goldens, static real-GPU
assets, shell and Python script syntax, Ruff, strict mypy, and the complete
Python test suite. The dashboard gate checks TypeScript, frontend unit tests,
the populated Playwright route journey, dashboard backend tests, production
assets, and an sdist-to-wheel installed-package smoke test.

GitHub Actions runs the same gates on pull requests and on `main`. A pull
request must not substitute a narrower command for either required gate.

## Evidence and security boundaries

- Never edit a sealed bundle to make a demonstration pass.
- Never represent a configured, inferred, or unavailable value as an observed
  value. Unsupported observations remain unavailable rather than becoming zero.
- Keep synthetic evidence visibly `SYNTHETIC_ONLY` and outside customer-evidence
  flows.
- Do not commit credentials, secret-bearing endpoint URLs, private prompts, or
  unreviewed generated GPU captures.
- Do not claim a genuine GPU receipt from macOS, fixtures, static checks, or a
  host that does not satisfy the documented Linux/NVIDIA provenance gate.
- Inferdrome produces measurements. ExitSpec or another independent consumer
  owns customer acceptance outcomes.

Report a suspected evidence-integrity or secret-exposure issue privately to the
repository owner before opening a public issue containing sensitive details.

## Change discipline

- Keep product-contract changes separate from implementation convenience.
- Add adversarial coverage for every new parser, archive reader, digest boundary,
  immutable publication path, or capability declaration.
- Regenerate committed schemas, goldens, and dashboard assets through their
  checked-in scripts; do not edit generated output by hand.
- Document claim boundaries and unavailable capabilities alongside new output.
- Keep pull requests draft until both required checks pass and all failure
  artifacts have been reviewed.

The release candidate is tracked in
[`docs/V0_1_RELEASE_CHECKLIST.md`](docs/V0_1_RELEASE_CHECKLIST.md).
