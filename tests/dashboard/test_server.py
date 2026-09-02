"""Server bootstrap contracts that do not open sockets or browsers."""

from pathlib import Path
from typing import Any

import pytest
import uvicorn
from fastapi.testclient import TestClient

from inferdrome.dashboard.server import run_dashboard
from inferdrome.errors import DashboardError
from inferdrome.routing_campaign import run_campaign

_ROOT = Path(__file__).resolve().parents[2]
_INPUTS = _ROOT / "campaigns" / "routing-campaign-v1"


def test_server_is_hard_bound_to_loopback_and_does_not_open_browser_by_default(
    tmp_path: Path,
    monkeypatch: Any,
) -> None:
    calls: list[tuple[Any, dict[str, object]]] = []

    def capture_run(app: object, **options: object) -> None:
        calls.append((app, options))

    def reject_browser_open(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("default dashboard startup must not open a browser")

    monkeypatch.setattr(uvicorn, "run", capture_run)
    monkeypatch.setattr("webbrowser.open", reject_browser_open)

    run_dashboard(tmp_path / "runs", port=9123)

    assert len(calls) == 1
    app, options = calls[0]
    assert app.title == "Inferdrome dashboard"
    assert options == {
        "host": "127.0.0.1",
        "port": 9123,
        "access_log": False,
    }


def test_server_wires_explicit_routing_campaign_root(
    tmp_path: Path,
    monkeypatch: Any,
) -> None:
    sealed = run_campaign(
        _INPUTS / "stale-load-fresh-health.plan.json",
        _INPUTS / "stale-load-fresh-health.trace.jsonl",
        _INPUTS / "stale-load-fresh-health.fault-schedule.json",
        _INPUTS / "trial-plan.json",
        tmp_path / "routing-campaign",
    )
    calls: list[object] = []

    def capture_run(app: object, **_options: object) -> None:
        calls.append(app)

    monkeypatch.setattr(uvicorn, "run", capture_run)
    run_dashboard(
        tmp_path / "runs",
        routing_campaigns_root=sealed.path,
        port=9124,
    )

    assert len(calls) == 1
    with TestClient(calls[0]) as client:
        response = client.get("/api/v1/routing-campaigns/routing-campaign-v1")
    assert response.status_code == 200
    assert response.json()["summary"]["retained_digest"] == sealed.retained_digest


@pytest.mark.parametrize("port", [True, 0, 65_536])
def test_server_rejects_invalid_ports_before_starting(
    port: int,
    monkeypatch: Any,
) -> None:
    def reject_run(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("invalid ports must not reach the ASGI server")

    monkeypatch.setattr(uvicorn, "run", reject_run)

    with pytest.raises(DashboardError, match="port must be between 1 and 65535"):
        run_dashboard(Path("unused"), port=port)
