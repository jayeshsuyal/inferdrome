"""Core repeated-trial grouping and variation invariants."""

import os
from decimal import localcontext
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest

import inferdrome.trials.service as trial_service
from inferdrome.domain.metrics import (
    Aggregation,
    DefinitionId,
    Measurement,
    MetricId,
    Population,
    RoundingPolicy,
    Unit,
)
from inferdrome.domain.states import EvidenceEligibility
from inferdrome.errors import TrialSetError, WorkLimitError
from inferdrome.limits import WorkBudget
from inferdrome.trials import (
    create_trial_set,
    trial_metric_variations,
    verify_trial_set,
)


def _make_tree_writable(root: Path) -> None:
    if not root.exists():
        return
    for directory, directory_names, filenames in os.walk(root, topdown=False):
        current = Path(directory)
        for filename in filenames:
            (current / filename).chmod(0o600)
        for directory_name in directory_names:
            (current / directory_name).chmod(0o700)
        current.chmod(0o700)


def test_create_verify_and_summarize_same_configuration(
    tmp_path: Path,
    run_fake_bundle: Any,
) -> None:
    runs_root = tmp_path / "runs"
    trial_sets_root = tmp_path / "trial-sets"
    first_id = "run-11111111111111111111111111111111"
    second_id = "run-22222222222222222222222222222222"
    run_fake_bundle(runs_root, first_id)
    run_fake_bundle(runs_root, second_id)

    try:
        created = create_trial_set(
            runs_root=runs_root,
            trial_sets_root=trial_sets_root,
            run_ids=(first_id, second_id),
            title="Two deterministic repetitions",
            hypothesis="The configured treatment is repeatable.",
            trial_set_id="trial-set-11111111111111111111111111111111",
        )
        reloaded = verify_trial_set(
            created.path,
            runs_root=runs_root,
            expected_trial_set_digest=created.trial_set_digest,
        )

        assert reloaded.descriptor.members[0].run_id == first_id
        assert reloaded.descriptor.members[1].run_id == second_id
        assert reloaded.descriptor.reducer_version == "1.0.0"
        assert reloaded.trial_set_digest.startswith("sha256:")
        assert reloaded.comparison_authority.scope == (
            "DESCRIPTIVE_ONLY_NON_AUTHORITATIVE"
        )
        assert set(reloaded.comparison_authority.issues) == {
            "EVIDENCE_NOT_CUSTOMER_ELIGIBLE",
            "EXITSPEC_CONTRACT_IDENTITY_MISSING",
        }
        variations = trial_metric_variations(reloaded)
        assert variations
        assert all(item.available_run_count == 2 for item in variations)
        assert all(item.span == "0" for item in variations)
    finally:
        _make_tree_writable(trial_sets_root)


def test_creation_reads_back_descriptor_without_third_member_recalculation(
    tmp_path: Path,
    run_fake_bundle: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runs_root = tmp_path / "runs"
    trial_sets_root = tmp_path / "trial-sets"
    run_ids = (
        "run-11111111111111111111111111111111",
        "run-22222222222222222222222222222222",
    )
    for run_id in run_ids:
        run_fake_bundle(runs_root, run_id)

    recalculated_paths: list[Path] = []
    recalculate_bundle = trial_service.recalculate_bundle
    publish_immutable_directory = trial_service.publish_immutable_directory
    recalculations_at_publication: int | None = None

    def tracked_recalculation(path: Path, **kwargs: Any) -> Any:
        recalculated_paths.append(path)
        return recalculate_bundle(path, **kwargs)

    def tracked_publication(**kwargs: Any) -> Path:
        nonlocal recalculations_at_publication
        recalculations_at_publication = len(recalculated_paths)
        return publish_immutable_directory(**kwargs)

    def unexpected_reverification(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("creation reverified after publication")

    monkeypatch.setattr(
        trial_service,
        "recalculate_bundle",
        tracked_recalculation,
    )
    monkeypatch.setattr(
        trial_service,
        "publish_immutable_directory",
        tracked_publication,
    )
    monkeypatch.setattr(
        trial_service,
        "verify_trial_set",
        unexpected_reverification,
    )
    try:
        created = create_trial_set(
            runs_root=runs_root,
            trial_sets_root=trial_sets_root,
            run_ids=run_ids,
            title="Prepublication verification",
        )

        assert created.path.is_dir()
        assert (created.path / "trial-set.json").is_file()
        assert len(created.members) == 2
        assert recalculations_at_publication == 2 * len(run_ids)
        assert len(recalculated_paths) == 2 * len(run_ids)
        assert recalculated_paths[:2] == recalculated_paths[2:]
        persisted_descriptor, persisted_content = trial_service._load_descriptor(
            created.path
        )
        assert created.descriptor == persisted_descriptor
        assert created.trial_set_digest == trial_service.digest_bytes(
            trial_service.DigestDomain.TRIAL_SET,
            persisted_content,
        )
    finally:
        _make_tree_writable(trial_sets_root)


def test_creation_rejects_substituted_published_descriptor(
    tmp_path: Path,
    run_fake_bundle: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runs_root = tmp_path / "runs"
    trial_sets_root = tmp_path / "trial-sets"
    run_ids = (
        "run-11111111111111111111111111111111",
        "run-22222222222222222222222222222222",
    )
    for run_id in run_ids:
        run_fake_bundle(runs_root, run_id)

    publish_immutable_directory = trial_service.publish_immutable_directory
    substituted = False

    def publish_then_substitute(**kwargs: Any) -> Path:
        nonlocal substituted
        destination = publish_immutable_directory(**kwargs)
        expected = trial_service.TrialSet.model_validate_json(kwargs["content"])
        replacement = expected.model_copy(
            update={"title": "Substituted after immutable publication"}
        )
        descriptor_path = destination / "trial-set.json"
        destination.chmod(0o700)
        descriptor_path.chmod(0o600)
        descriptor_path.write_bytes(
            trial_service._canonical_trial_set_bytes(replacement)
        )
        descriptor_path.chmod(0o400)
        destination.chmod(0o500)
        substituted = True
        return destination

    monkeypatch.setattr(
        trial_service,
        "publish_immutable_directory",
        publish_then_substitute,
    )
    try:
        with pytest.raises(
            TrialSetError,
            match="publication readback failed closed",
        ):
            create_trial_set(
                runs_root=runs_root,
                trial_sets_root=trial_sets_root,
                run_ids=run_ids,
                title="Expected publication",
            )
        assert substituted
    finally:
        _make_tree_writable(trial_sets_root)


def test_creation_rejects_substituted_published_path(
    tmp_path: Path,
    run_fake_bundle: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runs_root = tmp_path / "runs"
    trial_sets_root = tmp_path / "trial-sets"
    run_ids = (
        "run-11111111111111111111111111111111",
        "run-22222222222222222222222222222222",
    )
    for run_id in run_ids:
        run_fake_bundle(runs_root, run_id)

    publish_immutable_directory = trial_service.publish_immutable_directory
    substituted = False

    def publish_then_substitute_path(**kwargs: Any) -> Path:
        nonlocal substituted
        destination = publish_immutable_directory(**kwargs)
        displaced = destination.with_name(f"{destination.name}.displaced")
        destination.rename(displaced)
        destination.symlink_to(displaced.name, target_is_directory=True)
        substituted = True
        return destination

    monkeypatch.setattr(
        trial_service,
        "publish_immutable_directory",
        publish_then_substitute_path,
    )
    try:
        with pytest.raises(
            TrialSetError,
            match="publication readback failed closed",
        ):
            create_trial_set(
                runs_root=runs_root,
                trial_sets_root=trial_sets_root,
                run_ids=run_ids,
                title="Expected path",
            )
        assert substituted
    finally:
        _make_tree_writable(trial_sets_root)


def test_creation_rejects_substituted_publication_return_path(
    tmp_path: Path,
    run_fake_bundle: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runs_root = tmp_path / "runs"
    trial_sets_root = tmp_path / "trial-sets"
    run_ids = (
        "run-11111111111111111111111111111111",
        "run-22222222222222222222222222222222",
    )
    for run_id in run_ids:
        run_fake_bundle(runs_root, run_id)

    publish_immutable_directory = trial_service.publish_immutable_directory
    substituted_path: Path | None = None

    def publish_then_return_substitute(**kwargs: Any) -> Path:
        nonlocal substituted_path
        destination = publish_immutable_directory(**kwargs)
        substituted_path = destination.with_name(
            f"{destination.name}.returned-substitute"
        )
        return substituted_path

    monkeypatch.setattr(
        trial_service,
        "publish_immutable_directory",
        publish_then_return_substitute,
    )
    try:
        with pytest.raises(
            TrialSetError,
            match="publication readback failed closed",
        ):
            create_trial_set(
                runs_root=runs_root,
                trial_sets_root=trial_sets_root,
                run_ids=run_ids,
                title="Expected return path",
            )
        assert substituted_path is not None
        assert not substituted_path.exists()
    finally:
        _make_tree_writable(trial_sets_root)


def test_creation_fails_when_publication_readback_budget_is_exhausted(
    tmp_path: Path,
    run_fake_bundle: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runs_root = tmp_path / "runs"
    trial_sets_root = tmp_path / "trial-sets"
    run_ids = (
        "run-11111111111111111111111111111111",
        "run-22222222222222222222222222222222",
    )
    for run_id in run_ids:
        run_fake_bundle(runs_root, run_id)

    observed_limits: list[Any] = []
    reservations: list[tuple[int, int]] = []
    published_size: int | None = None
    publish_immutable_directory = trial_service.publish_immutable_directory

    class ExhaustedReadbackBudget:
        def reserve(self, *, units: int = 0, bytes_: int = 0) -> None:
            reservations.append((units, bytes_))
            if bytes_:
                raise WorkLimitError("readback byte budget sentinel")

        def checkpoint(self) -> None:
            raise AssertionError("exhausted readback reached its checkpoint")

    def tracked_publication(**kwargs: Any) -> Path:
        nonlocal published_size
        published_size = len(kwargs["content"])
        return publish_immutable_directory(**kwargs)

    def budget_factory(limits: Any) -> Any:
        observed_limits.append(limits)
        if limits == trial_service.TRIAL_SET_PUBLICATION_READBACK_WORK:
            return ExhaustedReadbackBudget()
        return WorkBudget(limits)

    monkeypatch.setattr(
        trial_service,
        "publish_immutable_directory",
        tracked_publication,
    )
    monkeypatch.setattr(trial_service, "WorkBudget", budget_factory)
    try:
        with pytest.raises(
            TrialSetError,
            match="publication readback exceeded its work limits",
        ):
            create_trial_set(
                runs_root=runs_root,
                trial_sets_root=trial_sets_root,
                run_ids=run_ids,
                title="Bounded readback",
            )
        assert observed_limits == [
            trial_service.TRIAL_SET_CREATION_WORK,
            trial_service.TRIAL_SET_PUBLICATION_READBACK_WORK,
        ]
        assert published_size is not None
        assert reservations == [(1, 0), (0, published_size)]
        assert trial_service.TRIAL_SET_PUBLICATION_READBACK_WORK.max_units == 1
        assert (
            trial_service.TRIAL_SET_PUBLICATION_READBACK_WORK.max_bytes
            == trial_service._MAX_TRIAL_SET_BYTES
        )
        assert trial_service.TRIAL_SET_PUBLICATION_READBACK_WORK.max_seconds == 5.0
    finally:
        _make_tree_writable(trial_sets_root)


def test_creation_checks_deadline_after_publication_readback_comparison(
    tmp_path: Path,
    run_fake_bundle: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runs_root = tmp_path / "runs"
    trial_sets_root = tmp_path / "trial-sets"
    run_ids = (
        "run-11111111111111111111111111111111",
        "run-22222222222222222222222222222222",
    )
    for run_id in run_ids:
        run_fake_bundle(runs_root, run_id)

    publication_finished = False
    comparison_finished = False
    publish_immutable_directory = trial_service.publish_immutable_directory
    compare_digest = trial_service.hmac.compare_digest

    def tracked_publication(**kwargs: Any) -> Path:
        nonlocal publication_finished
        destination = publish_immutable_directory(**kwargs)
        publication_finished = True
        return destination

    def expiring_comparison(first: Any, second: Any) -> bool:
        nonlocal comparison_finished
        result = compare_digest(first, second)
        if publication_finished:
            comparison_finished = True
        return result

    def budget_factory(limits: Any) -> WorkBudget:
        if limits == trial_service.TRIAL_SET_PUBLICATION_READBACK_WORK:
            return WorkBudget(
                limits,
                clock=lambda: 6.0 if comparison_finished else 0.0,
            )
        return WorkBudget(limits)

    monkeypatch.setattr(
        trial_service,
        "publish_immutable_directory",
        tracked_publication,
    )
    monkeypatch.setattr(trial_service.hmac, "compare_digest", expiring_comparison)
    monkeypatch.setattr(trial_service, "WorkBudget", budget_factory)
    try:
        with pytest.raises(
            TrialSetError,
            match="publication readback exceeded its work limits",
        ):
            create_trial_set(
                runs_root=runs_root,
                trial_sets_root=trial_sets_root,
                run_ids=run_ids,
                title="Post-comparison deadline",
            )
        assert comparison_finished
    finally:
        _make_tree_writable(trial_sets_root)


def test_creation_rejects_oversized_descriptor_before_publication(
    tmp_path: Path,
    run_fake_bundle: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runs_root = tmp_path / "runs"
    trial_sets_root = tmp_path / "trial-sets"
    run_ids = (
        "run-11111111111111111111111111111111",
        "run-22222222222222222222222222222222",
    )
    for run_id in run_ids:
        run_fake_bundle(runs_root, run_id)

    canonical_trial_set_bytes = trial_service._canonical_trial_set_bytes

    def oversized_trial_set_bytes(descriptor: Any) -> bytes:
        content = canonical_trial_set_bytes(descriptor)
        return content.ljust(trial_service._MAX_TRIAL_SET_BYTES + 1, b" ")

    def unexpected_publication(**kwargs: Any) -> Path:
        raise AssertionError("oversized descriptor reached publication")

    monkeypatch.setattr(
        trial_service,
        "_canonical_trial_set_bytes",
        oversized_trial_set_bytes,
    )
    monkeypatch.setattr(
        trial_service,
        "publish_immutable_directory",
        unexpected_publication,
    )

    with pytest.raises(TrialSetError, match="descriptor exceeds its byte limit"):
        create_trial_set(
            runs_root=runs_root,
            trial_sets_root=trial_sets_root,
            run_ids=run_ids,
            title="Oversized descriptor sentinel",
        )
    assert not trial_sets_root.exists()


def test_creation_checks_deadline_immediately_before_publication(
    tmp_path: Path,
    run_fake_bundle: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runs_root = tmp_path / "runs"
    trial_sets_root = tmp_path / "trial-sets"
    run_ids = (
        "run-11111111111111111111111111111111",
        "run-22222222222222222222222222222222",
    )
    for run_id in run_ids:
        run_fake_bundle(runs_root, run_id)

    expired = False
    delegate = WorkBudget(trial_service.TRIAL_SET_CREATION_WORK)

    class ExpiringBudget:
        def reserve(self, *, units: int = 0, bytes_: int = 0) -> None:
            delegate.reserve(units=units, bytes_=bytes_)

        def checkpoint(self) -> None:
            if expired:
                raise WorkLimitError("test deadline crossed")
            delegate.checkpoint()

    monkeypatch.setattr(trial_service, "WorkBudget", lambda _limits: ExpiringBudget())
    canonical_trial_set_bytes = trial_service._canonical_trial_set_bytes

    def expire_after_canonicalization(descriptor: Any) -> bytes:
        nonlocal expired
        content = canonical_trial_set_bytes(descriptor)
        expired = True
        return content

    def unexpected_publication(**kwargs: Any) -> Any:
        raise AssertionError("expired creation reached immutable publication")

    monkeypatch.setattr(
        trial_service,
        "_canonical_trial_set_bytes",
        expire_after_canonicalization,
    )
    monkeypatch.setattr(
        trial_service,
        "publish_immutable_directory",
        unexpected_publication,
    )

    with pytest.raises(TrialSetError, match="exceeded its work limits"):
        create_trial_set(
            runs_root=runs_root,
            trial_sets_root=trial_sets_root,
            run_ids=run_ids,
            title="Deadline sentinel",
        )
    assert not trial_sets_root.exists()


def test_creation_rejects_fingerprint_drift(
    tmp_path: Path,
    run_fake_bundle: Any,
) -> None:
    runs_root = tmp_path / "runs"
    first_id = "run-11111111111111111111111111111111"
    second_id = "run-22222222222222222222222222222222"
    run_fake_bundle(runs_root, first_id)
    run_fake_bundle(runs_root, second_id, max_runtime_seconds=61)

    with pytest.raises(
        TrialSetError,
        match="share one execution fingerprint",
    ):
        create_trial_set(
            runs_root=runs_root,
            trial_sets_root=tmp_path / "trial-sets",
            run_ids=(first_id, second_id),
            title="Invalid mixed treatment",
        )


def test_creation_rejects_duplicate_members(
    tmp_path: Path,
    run_fake_bundle: Any,
) -> None:
    runs_root = tmp_path / "runs"
    run_id = "run-11111111111111111111111111111111"
    run_fake_bundle(runs_root, run_id)

    with pytest.raises(TrialSetError, match="must be unique"):
        create_trial_set(
            runs_root=runs_root,
            trial_sets_root=tmp_path / "trial-sets",
            run_ids=(run_id, run_id),
            title="Duplicate run",
        )


def test_creation_rejects_different_experiment_identity(
    tmp_path: Path,
    run_fake_bundle: Any,
) -> None:
    runs_root = tmp_path / "runs"
    first_id = "run-11111111111111111111111111111111"
    second_id = "run-22222222222222222222222222222222"
    run_fake_bundle(runs_root, first_id)
    run_fake_bundle(runs_root, second_id, experiment_id="other-experiment")

    with pytest.raises(TrialSetError, match="one experiment ID"):
        create_trial_set(
            runs_root=runs_root,
            trial_sets_root=tmp_path / "trial-sets",
            run_ids=(first_id, second_id),
            title="Mixed experiment identity",
        )


def test_cross_contract_trial_set_remains_explicitly_non_authoritative(
    tmp_path: Path,
    run_fake_bundle: Any,
) -> None:
    runs_root = tmp_path / "runs"
    trial_sets_root = tmp_path / "trial-sets"
    first_id = "run-11111111111111111111111111111111"
    second_id = "run-22222222222222222222222222222222"
    run_fake_bundle(
        runs_root,
        first_id,
        exitspec_contract_digest=f"sha256:{'a' * 64}",
    )
    run_fake_bundle(
        runs_root,
        second_id,
        exitspec_contract_digest=f"sha256:{'b' * 64}",
    )

    try:
        created = create_trial_set(
            runs_root=runs_root,
            trial_sets_root=trial_sets_root,
            run_ids=(first_id, second_id),
            title="Cross-contract descriptive exploration",
        )
        reloaded = verify_trial_set(created.path, runs_root=runs_root)

        assert reloaded.comparison_authority.scope == (
            "DESCRIPTIVE_ONLY_NON_AUTHORITATIVE"
        )
        assert "EXITSPEC_CONTRACT_IDENTITY_MISMATCH" in (
            reloaded.comparison_authority.issues
        )
        assert reloaded.comparison_authority.exitspec_contract_digest is None
    finally:
        _make_tree_writable(trial_sets_root)


def test_customer_eligible_matching_contracts_pass_trial_authority_gate() -> None:
    contract_digest = f"sha256:{'c' * 64}"
    analyses = tuple(
        SimpleNamespace(
            verification=SimpleNamespace(
                descriptor=SimpleNamespace(
                    evidence_eligibility=EvidenceEligibility.CUSTOMER_ELIGIBLE,
                    digests=SimpleNamespace(
                        exitspec_contract_digest=contract_digest
                    ),
                )
            )
        )
        for _ in range(2)
    )

    authority = trial_service._comparison_authority(cast(Any, analyses))

    assert authority.scope == "CONTROLLED_OUTCOME_ELIGIBLE"
    assert authority.exitspec_contract_digest == contract_digest
    assert authority.issues == ()


def test_creation_wraps_invalid_public_metadata(
    tmp_path: Path,
    run_fake_bundle: Any,
) -> None:
    runs_root, run_ids = (
        tmp_path / "runs",
        (
            "run-11111111111111111111111111111111",
            "run-22222222222222222222222222222222",
        ),
    )
    for run_id in run_ids:
        run_fake_bundle(runs_root, run_id)

    with pytest.raises(TrialSetError, match="metadata failed"):
        create_trial_set(
            runs_root=runs_root,
            trial_sets_root=tmp_path / "trial-sets",
            run_ids=run_ids,
            title="",
        )


def test_verification_detects_descriptor_mutation(
    tmp_path: Path,
    run_fake_bundle: Any,
) -> None:
    runs_root = tmp_path / "runs"
    trial_sets_root = tmp_path / "trial-sets"
    first_id = "run-11111111111111111111111111111111"
    second_id = "run-22222222222222222222222222222222"
    run_fake_bundle(runs_root, first_id)
    run_fake_bundle(runs_root, second_id)

    try:
        created = create_trial_set(
            runs_root=runs_root,
            trial_sets_root=trial_sets_root,
            run_ids=(first_id, second_id),
            title="Mutation target",
        )
        descriptor = created.path / "trial-set.json"
        created.path.chmod(0o700)
        descriptor.chmod(0o600)
        descriptor.write_bytes(descriptor.read_bytes() + b" ")
        descriptor.chmod(0o400)
        created.path.chmod(0o500)

        with pytest.raises(TrialSetError, match="not canonical"):
            verify_trial_set(created.path, runs_root=runs_root)
    finally:
        _make_tree_writable(trial_sets_root)


def test_verification_requires_retained_trial_set_digest(
    tmp_path: Path,
    run_fake_bundle: Any,
) -> None:
    runs_root = tmp_path / "runs"
    trial_sets_root = tmp_path / "trial-sets"
    run_ids = (
        "run-11111111111111111111111111111111",
        "run-22222222222222222222222222222222",
    )
    for run_id in run_ids:
        run_fake_bundle(runs_root, run_id)

    try:
        created = create_trial_set(
            runs_root=runs_root,
            trial_sets_root=trial_sets_root,
            run_ids=run_ids,
            title="Digest-pinned trial",
        )
        with pytest.raises(TrialSetError, match="does not match"):
            verify_trial_set(
                created.path,
                runs_root=runs_root,
                expected_trial_set_digest=f"sha256:{'0' * 64}",
            )
    finally:
        _make_tree_writable(trial_sets_root)


def test_verification_requires_immutable_trial_set_directory(
    tmp_path: Path,
    run_fake_bundle: Any,
) -> None:
    runs_root = tmp_path / "runs"
    trial_sets_root = tmp_path / "trial-sets"
    run_ids = (
        "run-11111111111111111111111111111111",
        "run-22222222222222222222222222222222",
    )
    for run_id in run_ids:
        run_fake_bundle(runs_root, run_id)

    try:
        created = create_trial_set(
            runs_root=runs_root,
            trial_sets_root=trial_sets_root,
            run_ids=run_ids,
            title="Immutable trial",
        )
        created.path.chmod(0o700)

        with pytest.raises(TrialSetError, match="directory must be read-only"):
            verify_trial_set(created.path, runs_root=runs_root)
    finally:
        _make_tree_writable(trial_sets_root)


def test_verification_bounds_undeclared_trial_set_directory_entries(
    tmp_path: Path,
    run_fake_bundle: Any,
) -> None:
    runs_root = tmp_path / "runs"
    trial_sets_root = tmp_path / "trial-sets"
    run_ids = (
        "run-11111111111111111111111111111111",
        "run-22222222222222222222222222222222",
    )
    for run_id in run_ids:
        run_fake_bundle(runs_root, run_id)

    try:
        created = create_trial_set(
            runs_root=runs_root,
            trial_sets_root=trial_sets_root,
            run_ids=run_ids,
            title="Closed descriptor directory",
        )
        created.path.chmod(0o700)
        (created.path / "undeclared").write_bytes(b"ignored")
        (created.path / "undeclared").chmod(0o400)
        created.path.chmod(0o500)

        with pytest.raises(TrialSetError, match="undeclared entries"):
            verify_trial_set(created.path, runs_root=runs_root)
    finally:
        _make_tree_writable(trial_sets_root)


def _measurement(value: str, sample_count: int) -> Measurement:
    return Measurement(
        metric=MetricId.OUTPUT_TOKEN_THROUGHPUT,
        aggregation=Aggregation.RATE,
        value=value,
        unit=Unit.TOKENS_PER_SECOND,
        sample_count=sample_count,
        population=Population.SUCCESSFUL_MEASURED_REQUESTS,
        definition_id=DefinitionId.SUCCESSFUL_OUTPUT_TOKENS_PER_WINDOW_SECOND_V1,
        quantile_method=None,
        rounding_policy=RoundingPolicy.DECIMAL_HALF_EVEN_6_V1,
    )


def _fake_verified_measurements(
    entries: tuple[tuple[str, int], ...],
) -> Any:
    member_ids = tuple(f"run-{index}" for index in range(len(entries)))
    return SimpleNamespace(
        descriptor=SimpleNamespace(
            members=tuple(SimpleNamespace(run_id=run_id) for run_id in member_ids)
        ),
        members=tuple(
            SimpleNamespace(
                reduction=SimpleNamespace(
                    measurements=SimpleNamespace(
                        measurements=(_measurement(value, sample_count),)
                    )
                )
            )
            for value, sample_count in entries
        ),
    )


def test_variation_weights_runs_equally_not_request_counts() -> None:
    fake_verified = _fake_verified_measurements(
        (("10.000000", 1), ("100.000000", 9))
    )

    variation = trial_metric_variations(cast(Any, fake_verified))[0]

    assert variation.mean == "55"
    assert variation.median == "55"
    assert variation.minimum == "10"
    assert variation.maximum == "100"
    assert variation.sample_standard_deviation == "63.63961"
    assert [item.sample_count for item in variation.values] == [1, 9]


def test_variation_is_independent_of_ambient_decimal_precision() -> None:
    fake_verified = _fake_verified_measurements(
        (("123456789.123456", 1), ("987654321.654321", 1))
    )
    expected = trial_metric_variations(cast(Any, fake_verified))

    with localcontext() as context:
        context.prec = 6
        observed = trial_metric_variations(cast(Any, fake_verified))

    assert observed == expected
