"""Loopback HTTP API for registered assets and evidence."""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Annotated, Literal

from fastapi import Depends, FastAPI, Request
from fastapi import Path as PathParameter
from fastapi.exceptions import RequestValidationError
from pydantic import BaseModel, ConfigDict, StringConstraints, ValidationError
from starlette.concurrency import run_in_threadpool
from starlette.exceptions import HTTPException
from starlette.requests import ClientDisconnect
from starlette.responses import JSONResponse, Response
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from ira_native.application.service import EvidenceService
from ira_native.application.workflow import VerificationWorkflow
from ira_native.domain.models import (
    AssetDetail,
    AssetView,
    Evidence,
    IngestResult,
    ParseRun,
    PdfPageLocation,
    SearchResult,
    SourceKind,
)
from ira_native.errors import IraError
from ira_native.settings import Settings

_PREFIX = "/api/v1"
_ID_PATTERN = "^[0-9a-f]{64}$"
RegistryID = Annotated[str, PathParameter(pattern=_ID_PATTERN)]
BodyID = Annotated[str, StringConstraints(pattern=_ID_PATTERN)]
_RESPONSE_HEADERS = (
    (b"cache-control", b"no-store"),
    (b"x-content-type-options", b"nosniff"),
    (b"referrer-policy", b"no-referrer"),
    (b"content-security-policy", b"default-src 'none'; sandbox"),
)


def _error(error: IraError) -> JSONResponse:
    return JSONResponse(error.payload(), status_code=error.http_status)


class LocalHttp:
    """Apply local host/origin rules before reading request data."""

    def __init__(self, app: ASGIApp):
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        async def secured_send(message: Message) -> None:
            if message["type"] == "http.response.start":
                headers = message.get("headers", [])
                existing = {name.lower() for name, _ in headers}
                message["headers"] = [
                    *headers,
                    *(header for header in _RESPONSE_HEADERS if header[0] not in existing),
                ]
            await send(message)

        request = Request(scope)
        host = request.headers.get("host", "")
        hostname = host.rsplit(":", 1)[0] if ":" in host else host
        origin = request.headers.get("origin")
        if hostname not in ("localhost", "127.0.0.1") or (
            origin is not None and origin != f"http://{host}"
        ):
            await _error(IraError("permission_denied"))(scope, receive, secured_send)
            return
        await self.app(scope, receive, secured_send)


class RunInput(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    source_asset_id: BodyID
    artifact_asset_id: BodyID
    declared: bool = True


async def _read_limited(request: Request, media_type: str, limit: int) -> bytes:
    request_type = request.headers.get("content-type", "").partition(";")[0].strip().lower()
    if request_type != media_type:
        raise IraError("unsupported_format")
    length = request.headers.get("content-length")
    if length is not None:
        try:
            count = int(length)
        except ValueError:
            raise IraError("invalid_input") from None
        if count < 0:
            raise IraError("invalid_input")
        if count > limit:
            raise IraError("resource_limit")
    body = bytearray()
    try:
        async for chunk in request.stream():
            if len(body) + len(chunk) > limit:
                raise IraError("resource_limit")
            body.extend(chunk)
    except ClientDisconnect:
        raise IraError("invalid_input") from None
    return bytes(body)


def _get_service(request: Request) -> EvidenceService:
    return request.app.state.evidence_service


Service = Annotated[EvidenceService, Depends(_get_service)]


def create_app(settings: Settings | None = None) -> FastAPI:
    @asynccontextmanager
    async def lifespan(application: FastAPI) -> AsyncIterator[None]:
        with EvidenceService(settings if settings is not None else Settings.from_env()) as service:
            application.state.evidence_service = service
            application.state.workflow = VerificationWorkflow(service)
            yield

    app = FastAPI(
        title="IRA Native",
        version="0.1.0",
        lifespan=lifespan,
        docs_url=None,
        redoc_url=None,
        redirect_slashes=False,
    )
    app.add_middleware(LocalHttp)

    @app.exception_handler(IraError)
    async def ira_error(_request: Request, error: IraError) -> JSONResponse:
        return _error(error)

    @app.exception_handler(RequestValidationError)
    async def validation_error(_request: Request, error: RequestValidationError) -> JSONResponse:
        page_error = any(item["loc"][-1:] == ("physical_page",) for item in error.errors())
        return _error(IraError("invalid_location" if page_error else "invalid_input"))

    @app.exception_handler(HTTPException)
    async def http_error(_request: Request, error: HTTPException) -> JSONResponse:
        return _error(IraError("method_not_allowed" if error.status_code == 405 else "not_found"))

    @app.get(f"{_PREFIX}/doctor")
    def doctor(service: Service) -> dict:
        return service.doctor()

    @app.get(f"{_PREFIX}/assets/{{asset_id}}")
    def get_asset(asset_id: RegistryID, service: Service) -> AssetDetail:
        return service.get_asset(asset_id)

    @app.get(f"{_PREFIX}/assets/{{asset_id}}/pdf")
    def get_original_pdf(asset_id: RegistryID, service: Service) -> Response:
        return Response(
            service.get_pdf_bytes(asset_id),
            media_type="application/pdf",
            headers={"Content-Disposition": f'attachment; filename="{asset_id}.pdf"'},
        )

    @app.get(f"{_PREFIX}/assets/{{asset_id}}/pages/{{physical_page}}")
    def get_page(asset_id: RegistryID, physical_page: int, service: Service) -> PdfPageLocation:
        return service.get_pdf_page(asset_id, physical_page)

    @app.get(f"{_PREFIX}/assets/{{asset_id}}/pages/{{physical_page}}/image")
    def get_page_image(asset_id: RegistryID, physical_page: int, service: Service) -> Response:
        return Response(service.get_pdf_page_image(asset_id, physical_page), media_type="image/png")

    @app.get(f"{_PREFIX}/parse-runs/{{parse_run_id}}")
    def get_parse_run(parse_run_id: RegistryID, service: Service) -> ParseRun:
        return service.get_run(parse_run_id)

    @app.get(f"{_PREFIX}/evidence/{{evidence_id}}")
    def get_evidence(
        evidence_id: RegistryID,
        service: Service,
        q: str | None = None,
        mode: Literal["literal", "exact"] = "literal",
    ) -> Evidence:
        return service.get_evidence(evidence_id, query=q, mode=mode)

    @app.get(f"{_PREFIX}/evidence/{{evidence_id}}/page")
    def get_evidence_page(
        evidence_id: RegistryID,
        service: Service,
        q: str | None = None,
        mode: Literal["literal", "exact"] = "literal",
    ) -> PdfPageLocation:
        return service.get_evidence_page(evidence_id, query=q, mode=mode)

    @app.get(f"{_PREFIX}/search")
    def search(
        service: Service,
        q: str,
        source_asset_id: BodyID | None = None,
        parse_run_id: BodyID | None = None,
        page: int | None = None,
        kind: str | None = None,
        mode: Literal["literal", "exact"] = "literal",
        limit: int = 50,
    ) -> SearchResult:
        return service.search_evidence(
            q,
            source_asset_id=source_asset_id,
            parse_run_id=parse_run_id,
            physical_page=page,
            kind=kind,
            mode=mode,
            limit=limit,
        )

    @app.post(f"{_PREFIX}/assets/upload/{{source_kind}}", status_code=201)
    async def upload(source_kind: SourceKind, request: Request, service: Service) -> AssetView:
        if not service.settings.enable_uploads:
            raise IraError("permission_denied")
        data = await _read_limited(request, source_kind.media_type, service.byte_limit(source_kind))
        return await run_in_threadpool(service.ingest_uploaded_source, data, source_kind)

    @app.post(f"{_PREFIX}/parse-runs", status_code=201)
    async def register_run(request: Request, service: Service) -> IngestResult:
        if not service.settings.enable_uploads:
            raise IraError("permission_denied")
        body = await _read_limited(request, "application/json", 8192)
        try:
            values = RunInput.model_validate_json(body)
        except ValidationError:
            raise IraError("invalid_input") from None
        return await run_in_threadpool(
            service.ingest_parse_run,
            values.source_asset_id,
            values.artifact_asset_id,
            declared=values.declared,
        )

    from ira_native.interfaces.web import router

    app.include_router(router)
    return app
