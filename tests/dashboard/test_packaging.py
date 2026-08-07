"""Packaging contract for the dashboard's production frontend."""

from scripts.verify_dashboard_install import verify_install


def test_source_package_contains_and_serves_production_dashboard() -> None:
    verify_install()
