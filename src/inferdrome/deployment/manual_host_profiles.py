"""Closed, documented hardware acceptance catalog for manual routing hosts.

These are configuration profiles, not host attestations.  Preflight compares
the finite documented values below with later local runtime observations and
fails closed on a mismatch.  They do not establish provider ownership,
capacity, price, driver provenance, or a real GPU result.
"""

from __future__ import annotations

from dataclasses import dataclass
from types import MappingProxyType


@dataclass(frozen=True)
class ManualHostProfile:
    """One supported two-physical-GPU, one-engine-per-GPU profile."""

    profile_id: str
    input_schema_version: str
    config_schema_version: str
    manifest_schema_version: str
    accelerator_model: str
    runtime_gpu_name: str
    minimum_memory_mib: int
    maximum_memory_mib: int
    preflight_requirement: str


A100_PCIE_40GB_PROFILE = ManualHostProfile(
    profile_id="lambda-manual-two-a100-pcie-40gb-v1",
    input_schema_version="inferdrome.manual-host-input.v1",
    config_schema_version="inferdrome.routing-execution-config.v2",
    manifest_schema_version="inferdrome.routing-executed-manifest.v2",
    accelerator_model="NVIDIA A100-PCIE-40GB",
    runtime_gpu_name="NVIDIA A100-PCIE-40GB",
    minimum_memory_mib=39_000,
    maximum_memory_mib=41_000,
    preflight_requirement=(
        "Exactly two A100 PCIe 40 GB; distinct UUIDs and MIG disabled."
    ),
)

# NVIDIA documents H100-SXM5 as an 80 GB product and its MIG guide's
# `nvidia-smi -L` example reports this product string.  The same NVIDIA
# published H100 output reports 81,559 MiB.  This is a finite compatibility
# contract for the manual profile, not an assertion about a future host.
H100_SXM5_80GB_PROFILE = ManualHostProfile(
    profile_id="lambda-manual-two-h100-sxm5-80gb-v1",
    input_schema_version="inferdrome.manual-host-input.v2",
    config_schema_version="inferdrome.routing-execution-config.v3",
    manifest_schema_version="inferdrome.routing-executed-manifest.v3",
    accelerator_model="NVIDIA H100-SXM5-80GB",
    runtime_gpu_name="NVIDIA H100 80GB HBM3",
    minimum_memory_mib=81_559,
    maximum_memory_mib=81_559,
    preflight_requirement=(
        "Exactly two H100 SXM5 80 GB physical GPUs; distinct UUIDs and MIG disabled."
    ),
)

_PROFILES = {
    A100_PCIE_40GB_PROFILE.profile_id: A100_PCIE_40GB_PROFILE,
    H100_SXM5_80GB_PROFILE.profile_id: H100_SXM5_80GB_PROFILE,
}
SUPPORTED_MANUAL_HOST_PROFILES = MappingProxyType(_PROFILES)


def manual_host_profile(profile_id: str) -> ManualHostProfile:
    """Return only a closed supported profile; never infer a replacement."""

    try:
        return SUPPORTED_MANUAL_HOST_PROFILES[profile_id]
    except KeyError as error:
        raise ValueError("manual host profile is unsupported") from error
