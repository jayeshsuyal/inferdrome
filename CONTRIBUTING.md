# Contributing to Inferdrome

Inferdrome treats measurement integrity, provenance, eligibility, and customer
acceptance as separate facts. Contributions must preserve those boundaries and
fail closed when evidence is missing, malformed, or unsupported.

## Development environment

Use Python 3.12, uv 0.8.17, and a current Node.js 24 runtime. From the
repository root:

```bash
uv lock --check
uv sync --frozen --extra dev --extra dashboard
npm ci --prefix frontend
npx --prefix frontend playwright install chromium
```

The committed `uv.lock` is the Python environment authority. A normal install
must not rewrite it or replace it with a ranged editable pip resolution. To
change Python dependencies intentionally, edit `pyproject.toml`, run
`uv lock` with uv 0.8.17, review both files, then repeat `uv lock --check` and
the frozen sync above. CI checksum-verifies the same exact uv version, checks
lock freshness, and syncs dependencies with `--frozen --no-install-project`
before using `.venv/bin/python` with the repository `src` tree. That locked
environment intentionally has no pip. CI hands the same checksum-verified uv
binary to `.venv/bin/uv`; local dashboard packaging uses uv 0.8.17 from that
sibling path or from `INFERDROME_UV`/`PATH` to build and install artifacts
offline without adding pip to the environment.

On a clean Linux host, install Chromium's system dependencies as part of the
Playwright step:

```bash
npx --prefix frontend playwright install --with-deps chromium
```

## Required checks

Run all three repository gates before requesting review:

```bash
INFERDROME_PYTHON=.venv/bin/python ./scripts/engineering_gate.sh
INFERDROME_PYTHON=.venv/bin/python ./scripts/dashboard_gate.sh
INFERDROME_PYTHON=.venv/bin/python ./scripts/deployment_qualification_gate.sh
```

The engineering gate checks generated schemas and goldens, static real-GPU
assets, shell and Python script syntax, Ruff, strict mypy, and the complete
Python test suite. The dashboard gate checks TypeScript, frontend unit tests,
the populated Playwright route journey, dashboard backend tests, production
assets, and an sdist-to-wheel installed-package smoke test. The deployment
qualification gate checks the local synthetic Docker Compose boundary; it does
not prove cloud, GPU, serving-engine, or customer-acceptance behavior.

GitHub Actions runs the same gates on pull requests and on `main`. A pull
request must not substitute a narrower command for any required gate.

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
- Keep pull requests draft until all three required checks pass and all failure
  artifacts have been reviewed.

The release candidate is tracked in
[`docs/V0_1_RELEASE_CHECKLIST.md`](docs/V0_1_RELEASE_CHECKLIST.md).

## Contribution licensing

Unless explicitly stated otherwise, contributions accepted into Inferdrome
are licensed under Apache-2.0. Do not submit material you lack permission to
license. Third-party assets must retain their notices and license texts; see
[`THIRD_PARTY_NOTICES.md`](THIRD_PARTY_NOTICES.md). Inferdrome's license does
not cover models, workloads, serving engines, generated output, raw evidence
archives, or other externally supplied material.
