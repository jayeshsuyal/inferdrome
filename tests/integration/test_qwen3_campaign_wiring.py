"""The frozen Qwen3 workload traverses a sealed synthetic end-to-end run."""

from pathlib import Path

from inferdrome.bundle import verify_bundle
from inferdrome.domain.states import EvidenceEligibility, RunState
from inferdrome.execution.orchestrator import run_experiment
from inferdrome.qwen3_campaign import qwen3_workload_sha256

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
SOURCE = REPOSITORY_ROOT / "campaigns" / "v1" / "qwen3-workload-synthetic-smoke.yaml"
RUN_ID = "run-aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"


def test_qwen3_campaign_workload_seals_but_stays_synthetic(
    tmp_path: Path,
) -> None:
    result = run_experiment(
        SOURCE,
        runs_root=tmp_path / "runs",
        run_id=RUN_ID,
    )
    report = verify_bundle(
        result.sealed_bundle.path,
        expected_bundle_digest=result.sealed_bundle.bundle_digest,
    )

    assert report.descriptor.evidence_eligibility is EvidenceEligibility.SYNTHETIC_ONLY
    assert result.workspace.current_state().state is RunState.COMPLETE
    assert result.resolution.resolved_spec.workload.sha256 == qwen3_workload_sha256()
    assert len(result.resolution.request_plan.requests) == 96
    assert (
        result.sealed_bundle.path / "records" / "requests.jsonl"
    ).read_bytes().count(b"\n") == 96
