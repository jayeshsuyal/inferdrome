#!/usr/bin/env python3
"""Smoke-test dashboard resources from the imported Inferdrome installation."""

import argparse
import re
import tempfile
from importlib.resources import files
from pathlib import Path

from fastapi.testclient import TestClient

import inferdrome
from inferdrome.dashboard.api import create_app

_ASSET_REFERENCE = re.compile(r'(?:src|href)="(/assets/[^"?]+)')
_CSS_ASSET_REFERENCE = re.compile(r"url\((/assets/[^)?]+)")


def verify_install(expected_package_root: Path | None = None) -> None:
    package_file = Path(inferdrome.__file__).resolve()
    if expected_package_root is not None and not package_file.is_relative_to(
        expected_package_root.resolve()
    ):
        raise AssertionError(
            f"Inferdrome was imported from {package_file}, not the wheel install"
        )

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
        stylesheet = static_root.joinpath(
            *stylesheet_path.removeprefix("/").split("/")
        )
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
            health = client.get("/api/v1/health")
            assets = [client.get(asset_path) for asset_path in sorted(asset_paths)]

    if dashboard.status_code != 200 or "Inferdrome" not in dashboard.text:
        raise AssertionError("installed dashboard HTML is not served")
    if deep_link.status_code != 200 or deep_link.content != dashboard.content:
        raise AssertionError("installed dashboard deep links are not served")
    if health.status_code != 200 or health.json() != {"status": "ok"}:
        raise AssertionError("installed dashboard API health route failed")
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
