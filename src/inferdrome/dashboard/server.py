"""Local server entry point for the packaged dashboard."""

import threading
import webbrowser
from pathlib import Path
from typing import Any

from inferdrome.dashboard.index import DashboardIndex
from inferdrome.errors import DashboardError

_LOOPBACK_HOST = "127.0.0.1"


def run_dashboard(
    runs_root: Path,
    *,
    trial_sets_root: Path | None = None,
    comparison_plans_root: Path | None = None,
    comparison_results_root: Path | None = None,
    routing_campaigns_root: Path | None = None,
    routing_qualifications_root: Path | None = None,
    expected_routing_qualification_digest: str | None = None,
    port: int = 8787,
    open_browser: bool = False,
    keyring_path: Path | None = None,
) -> None:
    if isinstance(port, bool) or port < 1 or port > 65_535:
        raise DashboardError("dashboard port must be between 1 and 65535")
    try:
        import uvicorn

        from inferdrome.dashboard.api import create_app
    except ImportError:
        raise DashboardError(
            "dashboard dependencies are unavailable; install inferdrome[dashboard]"
        ) from None

    app = create_app(
        DashboardIndex(
            runs_root,
            trial_sets_root=trial_sets_root,
            comparison_plans_root=comparison_plans_root,
            comparison_results_root=comparison_results_root,
        ),
        routing_campaigns_root=routing_campaigns_root,
        routing_qualifications_root=routing_qualifications_root,
        expected_routing_qualification_digest=expected_routing_qualification_digest,
        keyring_path=keyring_path,
    )
    url = f"http://{_LOOPBACK_HOST}:{port}"
    if open_browser:
        timer = threading.Timer(0.75, webbrowser.open, args=(url,))
        timer.daemon = True
        timer.start()

    options: dict[str, Any] = {
        "host": _LOOPBACK_HOST,
        "port": port,
        "access_log": False,
    }
    uvicorn.run(app, **options)
