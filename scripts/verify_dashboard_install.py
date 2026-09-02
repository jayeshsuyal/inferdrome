#!/usr/bin/env python3
"""Smoke-test dashboard resources from the imported Inferdrome installation."""

import argparse
import hashlib
import re
import tempfile
from importlib.metadata import distribution
from importlib.resources import files
from pathlib import Path

_ASSET_REFERENCE = re.compile(r'(?:src|href)="(/assets/[^"?]+)')
_CSS_ASSET_REFERENCE = re.compile(r"url\((/assets/[^)?]+)")
_LICENSE_EXPRESSION = "Apache-2.0"
_REPOSITORY_PROJECT_URL = "Repository, https://github.com/jayeshsuyal/inferdrome"
_LICENSE_FILE_SHA256 = {
    "LICENSE": "cfc7749b96f63bd31c3c42b5c471bf756814053e847c10f3eb003417bc523d30",
    "THIRD_PARTY_NOTICES.md": (
        "6ed4ff6fddd948ac839347f873f3cbaf2ca0c57ff7a1c8cc2548a7f0968400f4"
    ),
    "LICENSES/ISC-Lucide.txt": (
        "1e7290b35280a048667bbf0ebabac1c7fd52a75300e8b2946ac165715997f2bc"
    ),
    "LICENSES/MIT-React.txt": (
        "da6d3703ed11cbe42bd212c725957c98da23cbff1998c05fa4b3d976d1a58e93"
    ),
    "LICENSES/MIT-Vite.txt": (
        "e373c2e74cd342b9e74d0b8813a8fddf3c9788515669a79c2ac238195673d38b"
    ),
    "LICENSES/OFL-1.1-IBM-Plex-Mono.txt": (
        "23b0a9d0c6d3f140a0b77e483c5cfa6bba574325ef5cb189ed9f2fec4884533f"
    ),
    "LICENSES/OFL-1.1-Instrument-Sans.txt": (
        "c27a3c53c3beed7f5c26853afa15991478ff7145d3754a36b0382f84e10c0d03"
    ),
}


def _verify_distribution_metadata(expected_package_root: Path) -> None:
    installed = distribution("inferdrome")
    metadata = installed.metadata
    if metadata.get("License-Expression") != _LICENSE_EXPRESSION:
        raise AssertionError("installed Inferdrome license expression is incorrect")
    project_urls = set(metadata.get_all("Project-URL") or ())
    if _REPOSITORY_PROJECT_URL not in project_urls:
        raise AssertionError("installed Inferdrome repository URL is incorrect")

    expected_license_files = set(_LICENSE_FILE_SHA256)
    metadata_license_files = set(metadata.get_all("License-File") or ())
    if metadata_license_files != expected_license_files:
        raise AssertionError(
            "installed Inferdrome license-file metadata disagrees; "
            f"expected={sorted(expected_license_files)}, "
            f"observed={sorted(metadata_license_files)}"
        )

    installed_files = installed.files
    if installed_files is None:
        raise AssertionError("installed Inferdrome distribution has no file inventory")
    package_root = expected_package_root.resolve()
    for relative_path, expected_sha256 in _LICENSE_FILE_SHA256.items():
        suffix = f".dist-info/licenses/{relative_path}"
        candidates = [
            entry for entry in installed_files if entry.as_posix().endswith(suffix)
        ]
        if len(candidates) != 1:
            raise AssertionError(
                f"installed Inferdrome license file is missing: {relative_path}"
            )
        installed_path = Path(installed.locate_file(candidates[0])).resolve()
        if not installed_path.is_relative_to(package_root):
            raise AssertionError(
                f"Inferdrome license file came from outside the wheel install: "
                f"{relative_path}"
            )
        if not installed_path.is_file():
            raise AssertionError(
                f"installed Inferdrome license path is not a file: {relative_path}"
            )
        actual_sha256 = hashlib.sha256(installed_path.read_bytes()).hexdigest()
        if actual_sha256 != expected_sha256:
            raise AssertionError(
                f"installed Inferdrome license file is stale: {relative_path}"
            )


def verify_install(expected_package_root: Path | None = None) -> None:
    from fastapi.testclient import TestClient

    import inferdrome
    from inferdrome.dashboard.api import create_app
    from inferdrome.domain.trial_set import TrialSet
    from inferdrome.trials import verify_trial_set

    if TrialSet.__name__ != "TrialSet" or not callable(verify_trial_set):
        raise AssertionError("installed trial-set runtime is unavailable")
    package_file = Path(inferdrome.__file__).resolve()
    if expected_package_root is not None and not package_file.is_relative_to(
        expected_package_root.resolve()
    ):
        raise AssertionError(
            f"Inferdrome was imported from {package_file}, not the wheel install"
        )
    if expected_package_root is not None:
        _verify_distribution_metadata(expected_package_root)

    static_root = files("inferdrome.dashboard").joinpath("static")
    index = static_root.joinpath("index.html")
    if not index.is_file():
        raise AssertionError("installed dashboard index.html is missing")
    html = index.read_text(encoding="utf-8")
    html_assets = set(_ASSET_REFERENCE.findall(html))
    if not html_assets:
        raise AssertionError("installed dashboard index has no local assets")
    asset_paths = set(html_assets)
    for stylesheet_path in html_assets:
        if not stylesheet_path.endswith(".css"):
            continue
        stylesheet = static_root.joinpath(*stylesheet_path.removeprefix("/").split("/"))
        if not stylesheet.is_file():
            raise AssertionError(
                f"installed dashboard asset is missing: {stylesheet_path}"
            )
        asset_paths.update(
            _CSS_ASSET_REFERENCE.findall(stylesheet.read_text(encoding="utf-8"))
        )
    for asset_path in asset_paths:
        asset = static_root.joinpath(*asset_path.removeprefix("/").split("/"))
        if not asset.is_file():
            raise AssertionError(f"installed dashboard asset is missing: {asset_path}")
    actual_assets = {
        f"/assets/{asset.name}"
        for asset in static_root.joinpath("assets").iterdir()
        if asset.is_file()
    }
    if actual_assets != asset_paths:
        stale = sorted(actual_assets - asset_paths)
        missing = sorted(asset_paths - actual_assets)
        raise AssertionError(
            f"installed dashboard asset set disagrees; stale={stale}, missing={missing}"
        )

    with tempfile.TemporaryDirectory(prefix="inferdrome-installed-dashboard-") as root:
        app = create_app(runs_root=Path(root) / "runs")
        with TestClient(app) as client:
            dashboard = client.get("/")
            deep_link = client.get("/compare")
            trial_deep_link = client.get("/trial-sets")
            routing_campaign_deep_link = client.get(
                "/routing-campaigns/routing-campaign-v1"
            )
            health = client.get("/api/v1/health")
            trial_sets = client.get("/api/v1/trial-sets?limit=200")
            routing_campaigns = client.get("/api/v1/routing-campaigns?limit=25")
            assets = [client.get(asset_path) for asset_path in sorted(asset_paths)]

    if dashboard.status_code != 200 or "Inferdrome" not in dashboard.text:
        raise AssertionError("installed dashboard HTML is not served")
    if deep_link.status_code != 200 or deep_link.content != dashboard.content:
        raise AssertionError("installed dashboard deep links are not served")
    if (
        trial_deep_link.status_code != 200
        or trial_deep_link.content != dashboard.content
    ):
        raise AssertionError("installed trial-set deep link is not served")
    if (
        routing_campaign_deep_link.status_code != 200
        or routing_campaign_deep_link.content != dashboard.content
    ):
        raise AssertionError("installed routing-campaign deep link is not served")
    if health.status_code != 200 or health.json() != {"status": "ok"}:
        raise AssertionError("installed dashboard API health route failed")
    if trial_sets.status_code != 200 or trial_sets.json().get("trial_sets") != []:
        raise AssertionError("installed trial-set API route failed")
    if (
        routing_campaigns.status_code != 200
        or routing_campaigns.json().get("routing_campaigns") != []
    ):
        raise AssertionError("installed routing-campaign API route failed")
    if any(response.status_code != 200 for response in assets):
        raise AssertionError("one or more installed dashboard assets are not served")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--expected-package-root", type=Path)
    arguments = parser.parse_args()
    verify_install(arguments.expected_package_root)
    print("dashboard installed-wheel smoke: ok")


if __name__ == "__main__":
    main()
