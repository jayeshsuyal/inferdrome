"""Loopback-only, read-only HTTP surface for dashboard projections."""

from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Annotated

from fastapi import FastAPI, HTTPException, Query, Request, Response
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.trustedhost import TrustedHostMiddleware

from inferdrome.dashboard.index import DashboardIndex
from inferdrome.dashboard.models import (
    ComparisonResponse,
    ControlledComparisonDetail,
    ControlledComparisonIndexResponse,
    RunDetail,
    RunIndexResponse,
    TrialSetDetail,
    TrialSetIndexResponse,
)
from inferdrome.errors import (
    DashboardControlledComparisonNotFound,
    DashboardPaginationError,
    DashboardRunNotFound,
    DashboardTrialSetNotFound,
)

_SECURITY_HEADERS = {
    "Content-Security-Policy": (
        "default-src 'self'; base-uri 'none'; frame-ancestors 'none'; "
        "form-action 'none'; img-src 'self' data:; font-src 'self'; "
        "style-src 'self' 'unsafe-inline'; script-src 'self'; connect-src 'self'"
    ),
    "Referrer-Policy": "no-referrer",
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
}


def create_app(
    index: DashboardIndex | None = None,
    *,
    runs_root: Path | None = None,
    static_dir: Path | None = None,
) -> FastAPI:
    if index is None:
        if runs_root is None:
            raise ValueError("create_app requires an index or runs root")
        index = DashboardIndex(runs_root)
    elif runs_root is not None:
        raise ValueError("create_app accepts either an index or runs root, not both")

    app = FastAPI(
        title="Inferdrome dashboard",
        version="1.0.0",
        docs_url=None,
        redoc_url=None,
        openapi_url="/api/v1/openapi.json",
    )
    app.add_middleware(
        TrustedHostMiddleware,
        allowed_hosts=["127.0.0.1", "localhost", "testserver"],
    )

    @app.middleware("http")
    async def secure_local_response(
        request: Request,
        call_next: Callable[[Request], Awaitable[Response]],
    ) -> Response:
        response = await call_next(request)
        for name, value in _SECURITY_HEADERS.items():
            response.headers[name] = value
        if request.url.path.startswith("/api/"):
            response.headers["Cache-Control"] = "no-store"
        return response

    @app.get("/api/v1/health")
    def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/api/v1/runs", response_model=RunIndexResponse)
    def list_runs(
        cursor: Annotated[str | None, Query(max_length=128)] = None,
        limit: Annotated[int, Query(ge=1, le=200)] = 100,
    ) -> RunIndexResponse:
        try:
            return index.refresh(cursor=cursor, limit=limit)
        except DashboardPaginationError:
            raise HTTPException(
                status_code=400,
                detail="invalid dashboard pagination cursor",
            ) from None

    @app.get("/api/v1/runs/{run_id}", response_model=RunDetail)
    def get_run(run_id: str) -> RunDetail:
        try:
            return index.get_run(run_id)
        except DashboardRunNotFound:
            raise HTTPException(status_code=404, detail="run not found") from None

    @app.get("/api/v1/compare", response_model=ComparisonResponse)
    def compare(
        baseline_run_id: str,
        candidate_run_id: str,
    ) -> ComparisonResponse:
        try:
            return index.compare(baseline_run_id, candidate_run_id)
        except DashboardRunNotFound:
            raise HTTPException(status_code=404, detail="run not found") from None

    @app.get("/api/v1/trial-sets", response_model=TrialSetIndexResponse)
    def list_trial_sets(
        cursor: Annotated[str | None, Query(max_length=128)] = None,
        limit: Annotated[int, Query(ge=1, le=200)] = 100,
    ) -> TrialSetIndexResponse:
        try:
            return index.list_trial_sets(cursor=cursor, limit=limit)
        except DashboardPaginationError:
            raise HTTPException(
                status_code=400,
                detail="invalid trial-set pagination cursor",
            ) from None

    @app.get(
        "/api/v1/trial-sets/{trial_set_id}",
        response_model=TrialSetDetail,
    )
    def get_trial_set(trial_set_id: str) -> TrialSetDetail:
        try:
            return index.get_trial_set(trial_set_id)
        except DashboardTrialSetNotFound:
            raise HTTPException(
                status_code=404,
                detail="trial set not found",
            ) from None

    @app.get(
        "/api/v1/controlled-comparisons",
        response_model=ControlledComparisonIndexResponse,
    )
    def list_controlled_comparisons(
        cursor: Annotated[str | None, Query(max_length=128)] = None,
        limit: Annotated[int, Query(ge=1, le=200)] = 100,
    ) -> ControlledComparisonIndexResponse:
        try:
            return index.list_controlled_comparisons(
                cursor=cursor,
                limit=limit,
            )
        except DashboardPaginationError:
            raise HTTPException(
                status_code=400,
                detail="invalid controlled-comparison pagination cursor",
            ) from None

    @app.get(
        "/api/v1/controlled-comparisons/{comparison_plan_id}",
        response_model=ControlledComparisonDetail,
    )
    def get_controlled_comparison(
        comparison_plan_id: str,
    ) -> ControlledComparisonDetail:
        try:
            return index.get_controlled_comparison(comparison_plan_id)
        except DashboardControlledComparisonNotFound:
            raise HTTPException(
                status_code=404,
                detail="controlled comparison not found",
            ) from None

    selected_static = static_dir or Path(__file__).with_name("static")
    if (selected_static / "index.html").is_file():
        assets = selected_static / "assets"
        if assets.is_dir():
            app.mount(
                "/assets",
                StaticFiles(directory=assets, check_dir=True),
                name="dashboard-assets",
            )

        @app.get("/", include_in_schema=False)
        def dashboard_root() -> FileResponse:
            return FileResponse(selected_static / "index.html")

        @app.get("/{spa_path:path}", include_in_schema=False)
        def dashboard_route(spa_path: str) -> FileResponse:
            if spa_path == "api" or spa_path.startswith("api/"):
                raise HTTPException(status_code=404, detail="not found")
            return FileResponse(selected_static / "index.html")
    else:

        @app.get("/")
        def api_root() -> dict[str, str]:
            return {
                "name": "Inferdrome dashboard API",
                "status": "frontend assets unavailable",
            }

    return app
