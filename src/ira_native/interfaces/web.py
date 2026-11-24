"""Server-rendered review flow for the local IRA workspace."""

from collections import defaultdict
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from pathlib import Path
from secrets import compare_digest, token_urlsafe
from urllib.parse import urlencode

from fastapi import APIRouter, Request
from starlette.concurrency import run_in_threadpool
from starlette.datastructures import FormData, UploadFile
from starlette.exceptions import HTTPException
from starlette.formparsers import FormParser, MultiPartException, MultiPartParser
from starlette.requests import ClientDisconnect
from starlette.responses import JSONResponse, RedirectResponse, Response
from starlette.staticfiles import StaticFiles
from starlette.templating import Jinja2Templates

from ira_native.application.workflow import _match_key, _related
from ira_native.errors import IraError

router = APIRouter(include_in_schema=False)
_ROOT = Path(__file__).parent
_templates = Jinja2Templates(directory=_ROOT / "templates")
_static = StaticFiles(directory=_ROOT / "static", check_dir=False)
_COOKIE = "ira_csrf"
_CSP = (
    "default-src 'none'; script-src 'self' 'wasm-unsafe-eval'; style-src 'self'; "
    "img-src 'self' data: blob:; font-src 'self' data: blob:; connect-src 'self'; "
    "worker-src 'self' blob:; form-action 'self'; frame-ancestors 'none'; base-uri 'none'"
)
_COLUMNS = (
    ("equipment_class", "设备类别"),
    ("manufacturer_model", "厂家型号"),
    ("configuration_variant", "配置变体"),
    ("scope", "工程范围"),
    ("quantity", "数量"),
    ("unit", "单位"),
    ("count_basis", "计数口径"),
    ("config", "配置原文"),
    ("property", "属性"),
    ("expr", "表达式"),
    ("site_group", "站点组"),
    ("condition", "适用条件"),
    ("source_revision", "来源修订"),
)
_DECISIONS = (
    ("accepted_with_scope", "限范围接受"),
    ("rejected_with_scope", "限范围否决"),
    ("needs_more_evidence", "需要补充证据"),
)


def _page(request: Request, name: str, *, status: int = 200, **context) -> Response:
    token = request.cookies.get(_COOKIE)
    issue_token = token is None or len(token) != 43 or not token.isascii()
    if issue_token:
        token = token_urlsafe(32)
    response = _templates.TemplateResponse(
        request,
        f"{name}.html",
        {"csrf": token, **context},
        status_code=status,
        headers={"Content-Security-Policy": _CSP, "Referrer-Policy": "same-origin"},
    )
    if issue_token:
        response.set_cookie(_COOKIE, token, httponly=True, samesite="strict")
    return response


def _error(request: Request, error: IraError) -> Response:
    return _page(request, "error", status=error.http_status, error=error.payload())


def _evidence(request: Request):
    return request.app.state.evidence_service


def _workflow(request: Request):
    return request.app.state.workflow


def _redirect(route: str, **query: str) -> RedirectResponse:
    return RedirectResponse(route + ("?" + urlencode(query) if query else ""), status_code=303)


def _id(value: str, length: int = 64) -> str:
    if len(value) != length or any(character not in "0123456789abcdef" for character in value):
        raise IraError("invalid_input")
    return value


def _str(form: FormData, key: str, size: int = 256, required: bool = True) -> str:
    value = form.get(key, "")
    if not isinstance(value, str) or len(value) > size or (required and not value.strip()):
        raise IraError("invalid_input")
    return value.strip()


@asynccontextmanager
async def _form(
    request: Request, limit: int = 32 * 1024, *, files: int = 0
) -> AsyncIterator[FormData]:
    if request.headers.get("sec-fetch-site") == "cross-site":
        raise IraError("permission_denied")
    cookie = request.cookies.get(_COOKIE)
    if not cookie or len(cookie) != 43 or not cookie.isascii():
        raise IraError("permission_denied")
    length = request.headers.get("content-length")
    if length is not None:
        if not length.isascii() or not length.isdecimal() or len(length) > 12:
            raise IraError("invalid_input")
        if int(length) > limit:
            raise IraError("resource_limit")
    too_large = False

    async def stream() -> AsyncIterator[bytes]:
        nonlocal too_large
        received = 0
        async for chunk in request.stream():
            received += len(chunk)
            if received > limit:
                too_large = True
                # The multipart parser closes temporary files on this exception.
                raise MultiPartException("resource_limit")
            yield chunk

    media_type = request.headers.get("content-type", "").split(";", 1)[0]
    if files and media_type == "multipart/form-data":
        parser = MultiPartParser(request.headers, stream(), max_files=files, max_fields=32)
    elif not files and media_type == "application/x-www-form-urlencoded":
        parser = FormParser(request.headers, stream())
    else:
        raise IraError("unsupported_format")
    try:
        form = await parser.parse()
    except (MultiPartException, ClientDisconnect, ValueError):
        raise IraError("resource_limit" if too_large else "invalid_input") from None
    try:
        sent = form.get("csrf")
        if not isinstance(sent, str) or not sent.isascii() or not compare_digest(cookie, sent):
            raise IraError("permission_denied")
        yield form
    finally:
        await form.close()


async def _upload(form: FormData, key: str, limit: int) -> tuple[str, bytes]:
    upload = form.get(key)
    if not isinstance(upload, UploadFile) or not upload.filename:
        raise IraError("invalid_input")
    if upload.size is not None and upload.size > limit:
        raise IraError("resource_limit")
    data = await upload.read(limit + 1)
    if len(data) > limit:
        raise IraError("resource_limit")
    if not data:
        raise IraError("invalid_input")
    return Path(upload.filename.replace("\\", "/")).name, data


async def _action(request: Request, handler: Callable[[], Awaitable[Response]]) -> Response:
    try:
        return await handler()
    except IraError as error:
        return _error(request, error)


@router.get("/", name="web_home")
def home(request: Request, parse_run_id: str = "", import_id: str = "") -> Response:
    return _page(
        request,
        "home",
        uploads=_evidence(request).settings.enable_uploads,
        columns=_COLUMNS,
        parse_run_id=parse_run_id,
        import_id=import_id,
    )


@router.get("/workbench/static/{resource:path}", name="web_static")
async def static(request: Request, resource: str) -> Response:
    try:
        response = await _static.get_response(resource, request.scope)
    except HTTPException as error:
        if error.status_code != 404:
            raise
        return _error(request, IraError("not_found"))
    response.headers["Content-Security-Policy"] = _CSP
    if Path(resource).suffix == ".mjs":
        response.headers["Content-Type"] = "text/javascript; charset=utf-8"
    return response


@router.post("/workbench/inputs/pdf")
async def upload_pdf(request: Request) -> Response:
    async def execute() -> Response:
        settings = _evidence(request).settings
        if not settings.enable_uploads:
            raise IraError("permission_denied")
        limit = settings.max_pdf_bytes + settings.max_json_bytes + 16 * 1024
        async with _form(request, limit, files=2) as form:
            _, pdf = await _upload(form, "pdf", settings.max_pdf_bytes)
            _, parsed = await _upload(form, "parsed", settings.max_json_bytes)
            import_id = _str(form, "import_id", 64, required=False)
            result = await run_in_threadpool(_workflow(request).ingest_pair_bytes, pdf, parsed)
            return _redirect("/", parse_run_id=result.parse_run.parse_run_id, import_id=import_id)

    return await _action(request, execute)


@router.post("/workbench/inputs/spreadsheet")
async def upload_spreadsheet(request: Request) -> Response:
    async def execute() -> Response:
        settings = _evidence(request).settings
        if not settings.enable_uploads:
            raise IraError("permission_denied")
        limit = settings.max_json_bytes
        async with _form(request, limit + 32 * 1024, files=1) as form:
            name, data = await _upload(form, "spreadsheet", limit)
            sheet = _str(form, "sheet", required=False) or None
            encoding = _str(form, "encoding", 32)
            parse_run_id = _str(form, "parse_run_id", 64, required=False)
            mapping = {}
            for key, _ in _COLUMNS:
                column = _str(form, f"column_{key}", required=False)
                if column:
                    mapping[key] = column
            result = await run_in_threadpool(
                _workflow(request).ingest_spreadsheet,
                data,
                name,
                mapping,
                sheet=sheet,
                encoding=encoding,
            )
            return _redirect("/", parse_run_id=parse_run_id, import_id=result["import_id"])

    return await _action(request, execute)


@router.post("/workbench/drafts")
async def prepare(request: Request) -> Response:
    async def execute() -> Response:
        async with _form(request) as form:
            result = await run_in_threadpool(
                _workflow(request).prepare,
                _id(_str(form, "parse_run_id", 64)),
                _id(_str(form, "import_id", 64)),
            )
            return _redirect(f"/workbench/drafts/{result['draft_id']}")

    return await _action(request, execute)


@router.get("/workbench/drafts/{draft_id}", name="web_draft")
def draft(request: Request, draft_id: str) -> Response:
    try:
        workflow = _workflow(request)
        item = workflow.get_draft(_id(draft_id))
        diagnostics = (
            None if item["requirements"] else workflow.diagnose_requirements(item["parse_run_id"])
        )
        declarations_by_key = defaultdict(list)
        for row in item["declarations"]:
            declarations_by_key[_match_key(row)].append(row)
        matches = {
            requirement["candidate_id"]: [
                row
                for row in declarations_by_key.get(_match_key(requirement), ())
                if _related(requirement, row)
            ]
            for requirement in item["requirements"]
        }
        return _page(request, "draft", draft=item, matches=matches, diagnostics=diagnostics)
    except IraError as error:
        return _error(request, error)


@router.post("/workbench/drafts/{draft_id}/verify")
async def verify(request: Request, draft_id: str) -> Response:
    async def execute() -> Response:
        async with _form(request, 256 * 1024) as form:
            confirmations = []
            checked = form.getlist("confirmed_id")
            if len(checked) > 1000 or len(set(checked)) != len(checked):
                raise IraError("invalid_input")
            for candidate in checked:
                if not isinstance(candidate, str):
                    raise IraError("invalid_input")
                candidate = _id(candidate)
                confirmations.append(
                    {
                        "requirement_id": candidate,
                        "declaration_id": _id(_str(form, f"declaration_{candidate}", 64)),
                        "fields_checked": form.get(f"fields_checked_{candidate}") == "on",
                        "same_version": form.get(f"same_version_{candidate}") == "on",
                    }
                )
            run = await run_in_threadpool(
                _workflow(request).verify, _id(draft_id), confirmations, _str(form, "reviewer", 80)
            )
            return _redirect(f"/workbench/runs/{run['run_id']}")

    return await _action(request, execute)


@router.get("/workbench/review")
def open_run(request: Request, run_id: str) -> Response:
    try:
        return _redirect(f"/workbench/runs/{_id(run_id, 32)}")
    except IraError as error:
        return _error(request, error)


@router.get("/workbench/search", name="web_search")
def search(
    request: Request, q: str = "", asset_id: str = "", physical_page: int | None = None
) -> Response:
    try:
        result = None
        if q:
            result = _evidence(request).search_evidence(
                q,
                source_asset_id=_id(asset_id) if asset_id else None,
                physical_page=physical_page,
            )
        return _page(
            request, "search", q=q, asset_id=asset_id, physical_page=physical_page, result=result
        )
    except IraError as error:
        return _error(request, error)


def _original(
    request: Request, evidence_id: str, query: str | None = None
) -> tuple[object | None, object | None, dict | None]:
    if not evidence_id:
        return None, None, None
    try:
        item = _evidence(request).get_evidence(_id(evidence_id), query=query)
    except IraError as error:
        return None, None, error.payload()
    try:
        return item, _evidence(request).get_evidence_page(evidence_id, query=query), None
    except IraError as error:
        return item, None, error.payload()


@router.get("/workbench/evidence/{evidence_id}", name="web_evidence")
def evidence(request: Request, evidence_id: str, q: str | None = None) -> Response:
    item, location, problem = _original(request, evidence_id, query=q)
    return _page(request, "evidence", item=item, location=location, problem=problem)


@router.get("/workbench/runs/{run_id}", name="web_run")
def inspect_run(request: Request, run_id: str, finding_id: str = "") -> Response:
    try:
        run = _workflow(request).get_run(_id(run_id, 32))
        findings = run["findings"]
        selected = next(
            (finding for finding in findings if finding["finding_id"] == finding_id), None
        )
        if finding_id and selected is None:
            raise IraError("no_evidence_found")
        if selected is None and findings:
            selected = findings[0]
        requirement = selected.get("requirement") if selected else None
        item, location, problem = _original(request, (requirement or {}).get("evidence_id", ""))
        return _page(
            request,
            "review",
            run=run,
            selected=selected,
            item=item,
            location=location,
            problem=problem,
            decisions=_DECISIONS,
        )
    except IraError as error:
        return _error(request, error)


@router.post("/workbench/runs/{run_id}/findings/{finding_id}/review")
async def review(request: Request, run_id: str, finding_id: str) -> Response:
    async def execute() -> Response:
        async with _form(request) as form:
            decision = _str(form, "decision", 32)
            if decision not in dict(_DECISIONS):
                raise IraError("invalid_input")
            await run_in_threadpool(
                _workflow(request).review,
                _id(run_id, 32),
                _id(finding_id),
                _str(form, "reviewer", 80),
                decision,
                _str(form, "note", 2000),
            )
            return _redirect(f"/workbench/runs/{run_id}", finding_id=finding_id)

    return await _action(request, execute)


@router.post("/workbench/runs/{run_id}/investigate")
async def investigate(request: Request, run_id: str) -> Response:
    async def execute() -> Response:
        async with _form(request):
            report = await _workflow(request).investigate(_id(run_id, 32))
            return _page(request, "investigate", run_id=run_id, report=report)

    return await _action(request, execute)


@router.get("/workbench/runs/{run_id}/export.csv")
def export_csv(request: Request, run_id: str) -> Response:
    try:
        return Response(
            _workflow(request).export_csv(_id(run_id, 32)),
            media_type="text/csv",
            headers={"Content-Disposition": f'attachment; filename="ira-{run_id}.csv"'},
        )
    except IraError as error:
        return _error(request, error)


@router.get("/workbench/runs/{run_id}/export.json")
def export_json(request: Request, run_id: str) -> Response:
    try:
        return JSONResponse(
            _workflow(request).get_run(_id(run_id, 32)),
            headers={"Content-Disposition": f'attachment; filename="ira-{run_id}.json"'},
        )
    except IraError as error:
        return _error(request, error)
