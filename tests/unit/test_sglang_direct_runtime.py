"""CPU/fake provenance gates; all host/process observations are injected."""

from __future__ import annotations

import io
import os
import zipfile
from pathlib import Path
from types import SimpleNamespace

import pytest

from inferdrome.evaluation import sglang_direct_runtime as runtime
from inferdrome.evaluation.contracts import EvaluationError
from inferdrome.routing_execution.canonical import sha256_digest
from tests.sglang_direct_support import synthetic_runtime


def test_runtime_roundtrip_and_environment_ignores_ambient(monkeypatch, tmp_path):
    manifest = synthetic_runtime()
    assert (
        runtime.load_runtime_manifest(runtime.runtime_manifest_bytes(manifest))
        == manifest
    )
    for key in (
        "LD_LIBRARY_PATH",
        "PYTHONPATH",
        "CUDA_PATH",
        "PATH",
        "TRITON_PTXAS_PATH",
    ):
        monkeypatch.setenv(key, "/wrong/compiler")
    env = runtime.runtime_environment(manifest, cache=tmp_path, gpu_index=1)
    assert all("/wrong" not in v for v in env.values())
    assert "PYTHONPATH" not in env and "CUDA_PATH" not in env
    assert env["CUDA_VISIBLE_DEVICES"] == "1"
    assert env["PATH"].startswith(manifest.site_packages + "/nvidia/cu13/bin:")
    assert env["CUDA_HOME"] == manifest.cuda_home
    assert env["PYTORCH_NVCC"] == manifest.roles["host_nvcc"]
    assert env["TRITON_PTXAS_PATH"] == manifest.roles["triton_ptxas"]
    with pytest.raises(EvaluationError):
        runtime.runtime_environment(manifest, cache=tmp_path, gpu_index=True)


@pytest.mark.parametrize(
    "change",
    [
        "wheel",
        "package",
        "compiler",
        "missing_role",
        "source",
        "driver",
        "python",
        "image",
    ],
)
def test_manifest_cannot_weaken_release_identity(change):
    manifest = synthetic_runtime()
    value = manifest.model_dump(mode="python")
    if change == "wheel":
        value["wheels"][0]["sha256"] = "sha256:" + "f" * 64
    elif change == "package":
        value["packages"]["torch"] = "2.11.0"
    elif change == "compiler":
        value["roles"]["host_nvcc"] = value["roles"]["cutile_ptxas"]
    elif change == "missing_role":
        del value["roles"]["cutile_nvvm"]
    elif change == "source":
        value["source_commit"] = "1" * 40
    elif change == "driver":
        value["driver_version"] = "580.65.05"
    elif change == "python":
        value["python_version"] = "3.13.1"
    else:
        value["image_reference"] = "old-image"
    from inferdrome.routing_execution.canonical import canonical_json_bytes

    with pytest.raises(EvaluationError):
        runtime.load_runtime_manifest(canonical_json_bytes(value) + b"\n")
    if change != "image":
        with pytest.raises(EvaluationError):
            runtime.runtime_manifest_bytes(manifest.model_construct(**value))


@pytest.mark.parametrize(
    "driver",
    ["580.65.06", "580.159.03", "580.178.04", "595.84"],
)
def test_cuda_13_compatible_driver_lane_retains_exact_observation(driver):
    manifest = synthetic_runtime().model_copy(update={"driver_version": driver})
    content = runtime.runtime_manifest_bytes(manifest)
    loaded = runtime.load_runtime_manifest(content)
    assert loaded.driver_version == driver
    assert b'"driver_version":"' + driver.encode() + b'"' in content


@pytest.mark.parametrize(
    "driver",
    [
        "580.65.05",
        "579.999.999",
        "580",
        "580.65.06.1",
        "580.65-beta",
        "580.65.06 ",
        " 580.65.06",
        "",
    ],
)
def test_cuda_13_driver_lane_rejects_old_or_malformed_inventory(driver):
    value = synthetic_runtime().model_dump(mode="python")
    value["driver_version"] = driver
    from inferdrome.routing_execution.canonical import canonical_json_bytes

    with pytest.raises(EvaluationError):
        runtime.load_runtime_manifest(canonical_json_bytes(value) + b"\n")


def test_real_file_hash_mutation_alias_and_access_time(tmp_path):
    path = tmp_path / "runtime"
    path.write_bytes(b"first")
    os.utime(path, (1, 1))
    assert runtime._hash_file(str(path))[0] == sha256_digest(b"first")
    path.write_bytes(b"second")
    assert runtime._hash_file(str(path))[0] == sha256_digest(b"second")
    alias = tmp_path / "alias"
    alias.symlink_to(path)
    with pytest.raises(EvaluationError):
        runtime._hash_file(str(alias))
    empty = tmp_path / "empty"
    empty.touch()
    assert runtime._hash_file(str(empty))[0] == sha256_digest(b"")


def test_installed_python_must_match_wheel_and_cover_extra_files(tmp_path):
    site = tmp_path / "site"
    site.mkdir()
    module = site / "module.py"
    module.write_bytes(b"original")
    wheel = tmp_path / "example.whl"
    with zipfile.ZipFile(wheel, "w") as out:
        out.writestr("module.py", b"original")
    base = synthetic_runtime()
    manifest = base.model_copy(
        update={
            "site_packages": str(site),
            "files": (
                runtime.RuntimeFile(
                    path=str(module), sha256=sha256_digest(b"original")
                ),
            ),
            "wheels": (
                runtime.RuntimeWheel(
                    path=str(wheel),
                    sha256=sha256_digest(wheel.read_bytes()),
                    name="example",
                    version="1",
                ),
            ),
        }
    )
    runtime._verify_installation(manifest)
    module.write_bytes(b"modified")
    changed = manifest.model_copy(
        update={
            "files": (
                runtime.RuntimeFile(
                    path=str(module), sha256=sha256_digest(b"modified")
                ),
            )
        }
    )
    with pytest.raises(ValueError):
        runtime._verify_installation(changed)
    extra = site / "untracked.py"
    extra.touch()
    with pytest.raises(ValueError):
        runtime._verify_installation(manifest)
    extra.unlink()
    pth = site / "inject.pth"
    pth.write_bytes(b"import os")
    expanded = manifest.model_copy(
        update={
            "files": (
                *manifest.files,
                runtime.RuntimeFile(
                    path=str(pth), sha256=sha256_digest(pth.read_bytes())
                ),
            )
        }
    )
    with pytest.raises(ValueError):
        runtime._verify_installation(expanded)


def test_exact_installed_inventory_and_file_changes(monkeypatch):
    manifest = synthetic_runtime()
    all_files = {f.path: f for f in (*manifest.files, *manifest.wheels)}
    monkeypatch.setattr(runtime, "_hash_file", lambda p: (all_files[p].sha256, None))
    monkeypatch.setattr(runtime, "_verify_installation", lambda m: None)
    packages = [
        SimpleNamespace(metadata={"Name": n}, version=v)
        for n, v in manifest.packages.items()
    ]
    monkeypatch.setattr(
        runtime.importlib.metadata, "distributions", lambda **kw: packages
    )
    runtime.verify_runtime_files(manifest)
    packages.append(SimpleNamespace(metadata={"Name": "unreviewed"}, version="1"))
    with pytest.raises(EvaluationError):
        runtime.verify_runtime_files(manifest)
    packages.pop()
    monkeypatch.setattr(runtime, "_hash_file", lambda p: ("sha256:" + "f" * 64, None))
    with pytest.raises(EvaluationError):
        runtime.verify_runtime_files(manifest)


@pytest.mark.parametrize(
    "mode", ["valid", "unknown", "deleted", "unstable", "no_engine", "no_cuda"]
)
def test_loaded_owned_group_provenance(monkeypatch, tmp_path, mode):
    manifest = synthetic_runtime()
    dummy = tmp_path / "lib"
    dummy.write_bytes(b"dummy")
    info = dummy.stat()
    groups = {101: {101: "1", 102: "2"}, 201: {201: "3", 202: "4"}}
    seen = {}

    def members(pgid):
        seen[pgid] = seen.get(pgid, 0) + 1
        value = dict(groups[pgid])
        if mode == "unstable" and seen[pgid] > 1:
            value[999] = "9"
        if mode == "no_engine":
            value = {pgid: value[pgid]}
        return value

    monkeypatch.setattr(runtime, "_group_members", members)
    paths = [manifest.roles[r] for r in ("cuda_driver", "torch_cuda", "sglang_kernel")]
    if mode == "unknown":
        paths.append("/unreviewed/libcuda.so")
    if mode == "deleted":
        paths[0] += " (deleted)"
    if mode == "no_cuda":
        paths = paths[1:]
    device = f"{os.major(info.st_dev):02x}:{os.minor(info.st_dev):02x}"
    maps = "\n".join(
        f"1-2 r-xp 0000 {device} {info.st_ino} {p}" for p in paths
    ).encode()
    original_open, original_stat = Path.open, Path.stat

    def open_path(p, *a, **kw):
        return (
            io.BytesIO(maps)
            if str(p).startswith("/proc/")
            else original_open(p, *a, **kw)
        )

    def stat_path(p, *a, **kw):
        return info if str(p) in paths else original_stat(p, *a, **kw)

    monkeypatch.setattr(Path, "open", open_path)
    monkeypatch.setattr(Path, "stat", stat_path)
    calls = []

    def hashed(p):
        calls.append(p)
        return "sha256:" + "0" * 64, info

    monkeypatch.setattr(runtime, "_hash_file", hashed)
    if mode == "valid":
        assert runtime.verify_loaded_runtime(
            manifest, process_groups=(101, 201)
        ).startswith("sha256:")
        assert len(calls) == 3
    else:
        with pytest.raises(EvaluationError):
            runtime.verify_loaded_runtime(manifest, process_groups=(101, 201))


def test_only_reviewed_setuptools_wheel_can_supply_pth(tmp_path):
    site = tmp_path / "site"
    site.mkdir()
    pth = site / "distutils-precedence.pth"
    pth.write_bytes(b"synthetic upstream pth fixture")
    wheel = tmp_path / "setuptools.whl"
    with zipfile.ZipFile(wheel, "w") as out:
        out.writestr(pth.name, pth.read_bytes())
    version, digest = runtime.WHEEL_IDENTITIES["setuptools"]
    selected = runtime.RuntimeWheel(
        path=str(wheel), name="setuptools", version=version, sha256=digest
    )
    manifest = synthetic_runtime().model_copy(
        update={
            "site_packages": str(site),
            "files": (
                runtime.RuntimeFile(
                    path=str(pth), sha256=sha256_digest(pth.read_bytes())
                ),
            ),
            "wheels": (selected,),
        }
    )
    # Only the isolated member-coverage algorithm is tested with this fake wheel;
    # the public gate separately hashes the archive against the fixed upstream pin.
    runtime._verify_installation(manifest)
    with pytest.raises(ValueError):
        runtime._verify_installation(
            manifest.model_copy(
                update={"wheels": (selected.model_copy(update={"name": "unreviewed"}),)}
            )
        )


@pytest.mark.parametrize("bad_device", [False, True])
def test_cuda_character_device_maps_are_not_hashed(monkeypatch, tmp_path, bad_device):
    import stat

    manifest = synthetic_runtime()
    dummy = tmp_path / "library"
    dummy.touch()
    info = dummy.stat()
    monkeypatch.setattr(
        runtime, "_group_members", lambda pgid: {pgid: "1", pgid + 1: "2"}
    )
    libraries = [
        manifest.roles[r] for r in ("cuda_driver", "torch_cuda", "sglang_kernel")
    ]
    d = f"{os.major(info.st_dev):02x}:{os.minor(info.st_dev):02x}"
    old_open, old_stat = Path.open, Path.stat

    def open_maps(p, *args, **kwargs):
        if not str(p).startswith("/proc/"):
            return old_open(p, *args, **kwargs)
        index = 0 if str(p).split("/")[2] in ("101", "102") else 1
        dev = f"/dev/nvidia{1 - index if bad_device else index}"
        return io.BytesIO(
            "\n".join(
                f"1-2 rw-s 0000 {d} {info.st_ino} {path}" for path in (*libraries, dev)
            ).encode()
        )

    monkeypatch.setattr(Path, "open", open_maps)
    monkeypatch.setattr(
        Path,
        "stat",
        lambda p, *a, **kw: info if str(p) in libraries else old_stat(p, *a, **kw),
    )
    monkeypatch.setattr(
        Path,
        "lstat",
        lambda p: SimpleNamespace(
            st_mode=stat.S_IFCHR,
            st_rdev=os.makedev(195, int(str(p)[-1])),
            st_ino=info.st_ino,
            st_dev=info.st_dev,
        ),
    )
    hashed = []

    def hash_file(p):
        hashed.append(p)
        return "sha256:" + "0" * 64, info

    monkeypatch.setattr(runtime, "_hash_file", hash_file)
    if bad_device:
        with pytest.raises(EvaluationError):
            runtime.verify_loaded_runtime(manifest, process_groups=(101, 201))
    else:
        runtime.verify_loaded_runtime(manifest, process_groups=(101, 201))
        assert set(hashed) == set(libraries)
