"""SYNTHETIC_ONLY 0.5.15 declarations; no installed runtime or GPU evidence."""

from __future__ import annotations

from inferdrome.evaluation.contracts import EndpointId
from inferdrome.evaluation.sglang_direct_runtime import (
    CUTLASS_COMPILER_SHA256,
    WHEEL_IDENTITIES,
    RuntimeDevice,
    RuntimeFile,
    RuntimeWheel,
    SglangDirectRuntimeManifest,
)
from inferdrome.evaluation.sglang_profile import SglangServingConfig
from inferdrome.evaluation.study_config import StudyConfig
from inferdrome.qwen3_campaign import (
    QWEN3_8B_MODEL_ID,
    QWEN3_8B_REVISION,
    qwen3_expected_snapshot_sha256,
)
from tests.unit.test_evaluation_engine_binding import profiles, study
from tests.unit.test_evaluation_study_config import load


def direct_study(scenario: str = "HEALTHY") -> StudyConfig:
    value = study(scenario).model_dump(mode="json")
    value["preparation"]["serving_image_reference"] = None
    for block in value["blocks"]:
        block["foreground"]["model"] = QWEN3_8B_MODEL_ID
        if block.get("background") is not None:
            block["background"]["model"] = QWEN3_8B_MODEL_ID
    return load(value)


def direct_profiles() -> dict[EndpointId, SglangServingConfig]:
    return {
        key: profile.model_copy(
            update={
                "served_model_name": QWEN3_8B_MODEL_ID,
                "model_revision": QWEN3_8B_REVISION,
                "tokenizer_revision": QWEN3_8B_REVISION,
                "model_snapshot_sha256": qwen3_expected_snapshot_sha256(),
                "tokenizer_snapshot_sha256": qwen3_expected_snapshot_sha256(),
            }
        )
        for key, profile in profiles().items()
    }


def synthetic_runtime() -> SglangDirectRuntimeManifest:
    site = "/opt/sglang015/lib/python3.12/site-packages"
    home = "/opt/cuda-13.1"
    roles = {
        "python": "/opt/sglang015/bin/python3.12",
        "cxx": "/usr/bin/g++-13",
        "host_nvcc": home + "/bin/nvcc",
        "host_ptxas": home + "/bin/ptxas",
        "host_nvvm": home + "/nvvm/lib64/libnvvm.so.4",
        "host_cudart": home + "/targets/x86_64-linux/lib/libcudart.so.13.1.80",
        "cutile_tileiras": site + "/nvidia/cu13/bin/tileiras",
        "cutile_ptxas": site + "/nvidia/cu13/bin/ptxas",
        "cutile_nvvm": site + "/nvidia/cu13/lib/libnvvm.so.4",
        "triton_ptxas": site + "/triton/backends/nvidia/bin/ptxas",
        "cutlass_compiler": site + "/cutlass/compiler.so",
        "torch_cuda": site + "/torch/lib/libtorch_cuda.so",
        "torch_cudart": site + "/nvidia/cu13/lib/libcudart.so.13.0.96",
        "sglang_kernel": site + "/sgl_kernel/common_ops.abi3.so",
        "nvjitlink": site + "/nvidia/cu13/lib/libnvJitLink.so.13",
        "cuda_driver": "/usr/lib/x86_64-linux-gnu/libcuda.so.595.84",
    }
    return SglangDirectRuntimeManifest(
        gpu_uuids=(
            "GPU-00000000-0000-0000-0000-000000000001",
            "GPU-00000000-0000-0000-0000-000000000002",
        ),
        devices=(
            RuntimeDevice(path="/dev/nvidia0", major=195, minor=0),
            RuntimeDevice(path="/dev/nvidia1", major=195, minor=1),
            RuntimeDevice(path="/dev/nvidiactl", major=195, minor=255),
            RuntimeDevice(path="/dev/nvidia-uvm", major=511, minor=0),
        ),
        python_version="3.12.13",
        cuda_home=home,
        site_packages=site,
        packages={name: version for name, (version, _) in WHEEL_IDENTITIES.items()},
        roles=roles,
        files=tuple(
            RuntimeFile(
                path=path,
                sha256=CUTLASS_COMPILER_SHA256
                if role == "cutlass_compiler"
                else "sha256:" + "0" * 64,
            )
            for role, path in sorted(roles.items())
        ),
        wheels=tuple(
            RuntimeWheel(
                name=name,
                version=version,
                sha256=digest,
                path=f"/opt/wheels/{name}.whl",
            )
            for name, (version, digest) in sorted(WHEEL_IDENTITIES.items())
        ),
    )


def synthetic_native(trial):
    """Run the real controller against CPU fake populations and selected labels."""
    import asyncio
    from dataclasses import replace

    from inferdrome.evaluation.fault_config import RoutingFaultConfig
    from inferdrome.evaluation.faults import RoutingFaultResult, run_routing_fault
    from inferdrome.evaluation.healthy import run_routing_healthy
    from inferdrome.evaluation.observations import ProbeResponse
    from tests.unit.test_evaluation_faults import Probe, Stream, finish
    from tests.unit.test_evaluation_runner import ManualClock

    async def run():
        clock = ManualClock()

        class SelectedProbe(Probe):
            async def get(self, origin, path):
                response = await super().get(origin, path)
                return ProbeResponse(
                    response.status,
                    response.body.replace(
                        b"private-model", trial.config.foreground.model.encode()
                    ),
                )

        background = Stream(clock, block=True)
        owned = (
            Stream(clock),
            background,
            SelectedProbe(clock, background),
            SelectedProbe(clock, background),
        )
        if isinstance(trial.config, RoutingFaultConfig):
            task = asyncio.create_task(
                run_routing_fault(trial.config, *owned, clock=clock)
            )
        else:
            task = asyncio.create_task(
                run_routing_healthy(
                    trial.config, owned[0], owned[2], owned[3], clock=clock
                )
            )
        await finish(clock)
        result = await task
        updates = dict(
            evidence_class="SYNTHETIC_ONLY",
            foreground=replace(result.foreground, evidence_class="SYNTHETIC_ONLY"),
        )
        if isinstance(result, RoutingFaultResult):
            updates["background"] = replace(
                result.background, evidence_class="SYNTHETIC_ONLY"
            )
        return replace(result, **updates)

    return asyncio.run(run())
