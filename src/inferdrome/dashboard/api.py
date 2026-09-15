"""Loopback-only, read-only HTTP surface for dashboard projections."""

import re
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Annotated

from fastapi import Depends, FastAPI, HTTPException, Query, Request, Response, Security
from fastapi.responses import FileResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from fastapi.staticfiles import StaticFiles
from starlette.middleware.trustedhost import TrustedHostMiddleware

from inferdrome.dashboard.auth import DashboardKeyringStore, validate_token_shape
from inferdrome.dashboard.evaluation_report_models import (
    EvaluationReportDetail,
    EvaluationReportIndex,
)
from inferdrome.dashboard.evaluation_reports import (
    EvaluationReportNotFound,
    EvaluationReportsIndex,
)
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
from inferdrome.dashboard.routing_campaign import RoutingCampaignDashboardIndex
from inferdrome.dashboard.routing_campaign_models import (
    RoutingCampaignDetail,
    RoutingCampaignIndexResponse,
)
from inferdrome.dashboard.routing_execution import RoutingExecutionDashboardIndex
from inferdrome.dashboard.routing_execution_models import (
    RoutingExecutionDetail,
    RoutingExecutionIndexResponse,
)
from inferdrome.dashboard.routing_qualification import (
    RoutingQualificationDashboardIndex,
)
from inferdrome.dashboard.routing_qualification_models import (
    RoutingQualificationDetail,
    RoutingQualificationIndexResponse,
)
from inferdrome.errors import (
    DashboardAuthError,
    DashboardControlledComparisonNotFound,
    DashboardError,
    DashboardPaginationError,
    DashboardRoutingCampaignNotFound,
    DashboardRoutingExecutionNotFound,
    DashboardRoutingQualificationNotFound,
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
_BEARER = HTTPBearer(auto_error=False, scheme_name="DashboardBearer")
_AUTH_HEADER_RE = re.compile(r"^Bearer ([A-Za-z0-9._-]+)$")
_AUTH_FAILURE = "dashboard authentication failed"
_AUTH_UNAVAILABLE = "dashboard authentication data unavailable"
_ROUTING_QUALIFICATION_UNAVAILABLE = (
    "routing-qualification verification work is temporarily unavailable"
)
_ROUTING_EXECUTION_UNAVAILABLE = (
    "routing-execution verification work is temporarily unavailable"
)


def _auth_error(status_code: int, detail: str) -> HTTPException:
    headers = {"WWW-Authenticate": "Bearer"} if status_code == 401 else None
    return HTTPException(status_code=status_code, detail=detail, headers=headers)


def _authorization_headers(request: Request) -> tuple[str, ...]:
    values: list[str] = []
    for name, value in request.scope.get("headers", []):
        if name.lower() != b"authorization":
            continue
        try:
            values.append(value.decode("ascii"))
        except UnicodeDecodeError:
            return ()
    return tuple(values)


def _require_dashboard_read(
    request: Request,
    credentials: Annotated[HTTPAuthorizationCredentials | None, Security(_BEARER)],
    *,
    store: DashboardKeyringStore,
) -> None:
    headers = _authorization_headers(request)
    if len(headers) != 1:
        raise _auth_error(401, _AUTH_FAILURE)
    match = _AUTH_HEADER_RE.fullmatch(headers[0])
    if match is None or credentials is None:
        raise _auth_error(401, _AUTH_FAILURE)
    token = match.group(1)
    if credentials.scheme != "Bearer" or credentials.credentials != token:
        raise _auth_error(401, _AUTH_FAILURE)
    if not validate_token_shape(token):
        raise _auth_error(401, _AUTH_FAILURE)
    try:
        valid = store.verify(token)
    except DashboardAuthError:
        raise _auth_error(503, _AUTH_UNAVAILABLE) from None
    if not valid:
        raise _auth_error(401, _AUTH_FAILURE)


def create_app(
    index: DashboardIndex | None = None,
    *,
    runs_root: Path | None = None,
    evaluation_report_index: EvaluationReportsIndex | None = None,
    evaluation_reports_catalog: Path | None = None,
    routing_campaign_index: RoutingCampaignDashboardIndex | None = None,
    routing_campaigns_root: Path | None = None,
    routing_qualification_index: RoutingQualificationDashboardIndex | None = None,
    routing_qualifications_root: Path | None = None,
    expected_routing_qualification_digest: str | None = None,
    routing_execution_index: RoutingExecutionDashboardIndex | None = None,
    routing_execution_root: Path | None = None,
    expected_routing_execution_digest: str | None = None,
    static_dir: Path | None = None,
    keyring_path: Path | None = None,
) -> FastAPI:
    if index is None:
        if runs_root is None:
            raise ValueError("create_app requires an index or runs root")
        index = DashboardIndex(runs_root)
    elif runs_root is not None:
        raise ValueError("create_app accepts either an index or runs root, not both")
    if evaluation_report_index is not None and evaluation_reports_catalog is not None:
        raise ValueError("create_app accepts either an evaluation index or catalog")
    if evaluation_report_index is None:
        evaluation_report_index = EvaluationReportsIndex(evaluation_reports_catalog)
    if routing_campaign_index is not None and routing_campaigns_root is not None:
        raise ValueError(
            "create_app accepts either a routing campaign index or root, not both"
        )
    if routing_campaign_index is None:
        routing_campaign_index = RoutingCampaignDashboardIndex(routing_campaigns_root)
    if routing_qualification_index is not None and (
        routing_qualifications_root is not None
        or expected_routing_qualification_digest is not None
    ):
        raise ValueError(
            "create_app accepts either a routing qualification index or "
            "configuration, not both"
        )
    if routing_qualification_index is None:
        routing_qualification_index = RoutingQualificationDashboardIndex(
            routing_campaigns_root=routing_campaigns_root,
            routing_qualifications_root=routing_qualifications_root,
            expected_qualification_digest=expected_routing_qualification_digest,
        )
    if routing_execution_index is not None and (
        routing_execution_root is not None
        or expected_routing_execution_digest is not None
    ):
        raise ValueError(
            "create_app accepts either a routing execution index or configuration, "
            "not both"
        )
    if routing_execution_index is None:
        routing_execution_index = RoutingExecutionDashboardIndex(
            routing_execution_root=routing_execution_root,
            expected_execution_digest=expected_routing_execution_digest,
        )

    auth_store = (
        DashboardKeyringStore(keyring_path) if keyring_path is not None else None
    )
    if auth_store is not None:
        auth_store.assert_usable()

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

    def require_dashboard_read(
        request: Request,
        credentials: Annotated[HTTPAuthorizationCredentials | None, Security(_BEARER)],
    ) -> None:
        if auth_store is None:
            return
        _require_dashboard_read(request, credentials, store=auth_store)

    protected_dependencies = (
        [Depends(require_dashboard_read)] if auth_store is not None else []
    )

    @app.get(
        "/api/v1/evaluation-reports",
        response_model=EvaluationReportIndex,
        dependencies=protected_dependencies,
    )
    def list_evaluation_reports() -> EvaluationReportIndex:
        try:
            return evaluation_report_index.refresh()
        except DashboardError:
            raise HTTPException(
                503, detail="Evaluation reports are temporarily unavailable."
            ) from None

    @app.get(
        "/api/v1/evaluation-reports/{report_id}",
        response_model=EvaluationReportDetail,
        dependencies=protected_dependencies,
    )
    def get_evaluation_report(report_id: str) -> EvaluationReportDetail:
        try:
            return evaluation_report_index.get_report(report_id)
        except EvaluationReportNotFound:
            raise HTTPException(404, detail="Evaluation report not found.") from None
        except DashboardError:
            raise HTTPException(
                503, detail="Evaluation reports are temporarily unavailable."
            ) from None

    @app.get(
        "/api/v1/runs",
        response_model=RunIndexResponse,
        dependencies=protected_dependencies,
    )
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
        except DashboardError:
            raise HTTPException(
                status_code=503,
                detail="dashboard verification work is temporarily unavailable",
            ) from None

    @app.get(
        "/api/v1/runs/{run_id}",
        response_model=RunDetail,
        dependencies=protected_dependencies,
    )
    def get_run(run_id: str) -> RunDetail:
        try:
            return index.get_run(run_id)
        except DashboardRunNotFound:
            raise HTTPException(status_code=404, detail="run not found") from None
        except DashboardError:
            raise HTTPException(
                status_code=503,
                detail="dashboard verification work is temporarily unavailable",
            ) from None

    @app.get(
        "/api/v1/compare",
        response_model=ComparisonResponse,
        dependencies=protected_dependencies,
    )
    def compare(
        baseline_run_id: str,
        candidate_run_id: str,
    ) -> ComparisonResponse:
        try:
            return index.compare(baseline_run_id, candidate_run_id)
        except DashboardRunNotFound:
            raise HTTPException(status_code=404, detail="run not found") from None
        except DashboardError:
            raise HTTPException(
                status_code=503,
                detail="dashboard verification work is temporarily unavailable",
            ) from None

    @app.get(
        "/api/v1/trial-sets",
        response_model=TrialSetIndexResponse,
        dependencies=protected_dependencies,
    )
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
        except DashboardError:
            raise HTTPException(
                status_code=503,
                detail="dashboard verification work is temporarily unavailable",
            ) from None

    @app.get(
        "/api/v1/trial-sets/{trial_set_id}",
        response_model=TrialSetDetail,
        dependencies=protected_dependencies,
    )
    def get_trial_set(trial_set_id: str) -> TrialSetDetail:
        try:
            return index.get_trial_set(trial_set_id)
        except DashboardTrialSetNotFound:
            raise HTTPException(
                status_code=404,
                detail="trial set not found",
            ) from None
        except DashboardError:
            raise HTTPException(
                status_code=503,
                detail="dashboard verification work is temporarily unavailable",
            ) from None

    @app.get(
        "/api/v1/controlled-comparisons",
        response_model=ControlledComparisonIndexResponse,
        dependencies=protected_dependencies,
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
        except DashboardError:
            raise HTTPException(
                status_code=503,
                detail="dashboard verification work is temporarily unavailable",
            ) from None

    @app.get(
        "/api/v1/controlled-comparisons/{comparison_plan_id}",
        response_model=ControlledComparisonDetail,
        dependencies=protected_dependencies,
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
        except DashboardError:
            raise HTTPException(
                status_code=503,
                detail="dashboard verification work is temporarily unavailable",
            ) from None

    @app.get(
        "/api/v1/routing-campaigns",
        response_model=RoutingCampaignIndexResponse,
        dependencies=protected_dependencies,
    )
    def list_routing_campaigns(
        cursor: Annotated[str | None, Query(max_length=128)] = None,
        limit: Annotated[int, Query(ge=1, le=25)] = 25,
    ) -> RoutingCampaignIndexResponse:
        try:
            return routing_campaign_index.refresh(cursor=cursor, limit=limit)
        except DashboardPaginationError:
            raise HTTPException(
                status_code=400,
                detail="invalid routing-campaign pagination cursor",
            ) from None
        except DashboardError:
            raise HTTPException(
                status_code=503,
                detail="routing-campaign verification work is temporarily unavailable",
            ) from None

    @app.get(
        "/api/v1/routing-campaigns/{campaign_id}",
        response_model=RoutingCampaignDetail,
        dependencies=protected_dependencies,
    )
    def get_routing_campaign(campaign_id: str) -> RoutingCampaignDetail:
        try:
            return routing_campaign_index.get_campaign(campaign_id)
        except DashboardRoutingCampaignNotFound:
            raise HTTPException(
                status_code=404,
                detail="routing campaign not found",
            ) from None
        except DashboardError:
            raise HTTPException(
                status_code=503,
                detail="routing-campaign verification work is temporarily unavailable",
            ) from None

    @app.get(
        "/api/v1/routing-executions",
        response_model=RoutingExecutionIndexResponse,
        dependencies=protected_dependencies,
    )
    def list_routing_executions(
        limit: Annotated[int, Query(ge=1, le=25)] = 25,
    ) -> RoutingExecutionIndexResponse:
        try:
            return routing_execution_index.refresh(limit=limit)
        except DashboardPaginationError:
            raise HTTPException(
                status_code=400,
                detail="invalid routing-execution page limit",
            ) from None
        except DashboardError:
            raise HTTPException(
                status_code=503,
                detail=_ROUTING_EXECUTION_UNAVAILABLE,
            ) from None

    @app.get(
        "/api/v1/routing-executions/{execution_id}",
        response_model=RoutingExecutionDetail,
        dependencies=protected_dependencies,
    )
    def get_routing_execution(execution_id: str) -> RoutingExecutionDetail:
        try:
            return routing_execution_index.get_execution(execution_id)
        except DashboardRoutingExecutionNotFound:
            raise HTTPException(
                status_code=404,
                detail="routing execution not found",
            ) from None
        except DashboardError:
            raise HTTPException(
                status_code=503,
                detail=_ROUTING_EXECUTION_UNAVAILABLE,
            ) from None

    @app.get(
        "/api/v1/routing-qualifications",
        response_model=RoutingQualificationIndexResponse,
        dependencies=protected_dependencies,
    )
    def list_routing_qualifications(
        limit: Annotated[int, Query(ge=1, le=25)] = 25,
    ) -> RoutingQualificationIndexResponse:
        try:
            return routing_qualification_index.refresh(limit=limit)
        except DashboardPaginationError:
            raise HTTPException(
                status_code=400,
                detail="invalid routing-qualification pagination cursor",
            ) from None
        except DashboardError:
            raise HTTPException(
                status_code=503,
                detail=_ROUTING_QUALIFICATION_UNAVAILABLE,
            ) from None

    @app.get(
        "/api/v1/routing-qualifications/{qualification_id}",
        response_model=RoutingQualificationDetail,
        dependencies=protected_dependencies,
    )
    def get_routing_qualification(qualification_id: str) -> RoutingQualificationDetail:
        try:
            return routing_qualification_index.get_qualification(qualification_id)
        except DashboardRoutingQualificationNotFound:
            raise HTTPException(
                status_code=404,
                detail="routing qualification not found",
            ) from None
        except DashboardError:
            raise HTTPException(
                status_code=503,
                detail=_ROUTING_QUALIFICATION_UNAVAILABLE,
            ) from None

    @app.get(
        "/api/v1/routing-qualifications/{qualification_id}/evidence",
        dependencies=protected_dependencies,
    )
    def get_routing_qualification_evidence(qualification_id: str) -> Response:
        try:
            content, digest = routing_qualification_index.get_evidence(qualification_id)
        except DashboardRoutingQualificationNotFound:
            raise HTTPException(
                status_code=404,
                detail="routing qualification not found",
            ) from None
        except DashboardError:
            raise HTTPException(
                status_code=503,
                detail=_ROUTING_QUALIFICATION_UNAVAILABLE,
            ) from None
        return Response(
            content=content,
            media_type="application/json",
            headers={
                "Content-Disposition": (
                    'attachment; filename="stale-telemetry-qualification-v1.json"'
                ),
                "X-Inferdrome-Evidence-Digest": digest,
            },
        )

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
