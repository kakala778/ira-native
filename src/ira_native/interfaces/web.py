"""Server-rendered review flow for the local IRA workspace."""

from collections import defaultdict
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from hmac import digest
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

from ira_native import __version__
from ira_native.application.reporting import summarize_run
from ira_native.application.results import persisted_finding
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
    ("technical_value", "技术属性值"),
    ("technical_unit", "技术属性单位"),
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
_OUTCOMES = {
    "issue": "待核实问题",
    "no_issue": "规则满足",
    "informational": "信息性差异",
    "review_required": "待复核",
    "blocked": "无法核验",
}
_SOURCE_ROLES = {
    "specification": "技术规格",
    "supplement": "补遗",
    "amendment": "变更",
}


def _page(request: Request, name: str, *, status: int = 200, **context) -> Response:
    token = request.cookies.get(_COOKIE)
    issue_token = token is None or len(token) != 43 or not token.isascii()
    if issue_token:
        token = token_urlsafe(32)
    response = _templates.TemplateResponse(
        request,
        f"{name}.html",
        {"csrf": token, "app_version": __version__, **context},
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


def _interval(value: str) -> tuple[int, int]:
    parts = value.split("-")
    if len(parts) not in (1, 2) or not all(p.isascii() and p.isdecimal() for p in parts):
        raise IraError("invalid_location")
    return int(parts[0]), int(parts[-1])


def _selection(value: str) -> tuple[int, ...]:
    start, end = _interval(value)
    if not 1 <= start <= end or end - start >= 5000:
        raise IraError("invalid_location")
    return tuple(range(start, end + 1))


def _range_label(selection: list[int]) -> str:
    intervals: list[list[int]] = []
    for value in sorted(selection):
        if not intervals or value != intervals[-1][-1] + 1:
            intervals.append([value, value])
        else:
            intervals[-1][-1] = value
    return ", ".join(str(start) if start == end else f"{start}–{end}" for start, end in intervals)


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
@router.get("/workbench/import")
def home(request: Request, parse_run_id: str = "", import_id: str = "", step: int = 0) -> Response:
    try:
        workflow = _workflow(request)
        summary = workflow.input_summary(
            _id(parse_run_id) if parse_run_id else "",
            _id(import_id) if import_id else "",
        )
        if request.url.path == "/" and not (parse_run_id or import_id):
            return _page(request, "home", recent=workflow.recent_work())
        return _page(
            request,
            "import",
            uploads=_evidence(request).settings.enable_uploads,
            columns=_COLUMNS,
            parse_run_id=parse_run_id,
            import_id=import_id,
            summary=summary,
            choices=workflow.input_choices(),
            step=3
            if parse_run_id and import_id and step != 1 and step != 2
            else 2
            if parse_run_id and step != 1
            else 1,
        )
    except IraError as error:
        return _error(request, error)


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


@router.get("/workbench/tasks/new")
def new_task(request: Request, added: str = "") -> Response:
    try:
        return _page(
            request,
            "task_new",
            choices=_workflow(request).input_choices(),
            roles=_SOURCE_ROLES,
            columns=_COLUMNS,
            uploads=_evidence(request).settings.enable_uploads,
            added=_id(added) if added else "",
        )
    except IraError as error:
        return _error(request, error)


@router.post("/workbench/tasks/new")
async def create_task(request: Request) -> Response:
    async def execute() -> Response:
        async with _form(request) as form:
            intervals = {}
            for key in ("source", "declaration"):
                selections = form.getlist(key)
                if not 1 <= len(selections) <= 32 or len(set(selections)) != len(selections):
                    raise IraError("invalid_input")
                intervals[key] = {
                    _id(value): _selection(_str(form, f"{key}_range_{value}", 20))
                    for value in selections
                }
            roles = {
                source: _str(form, f"source_role_{source}", 24) for source in intervals["source"]
            }
            if not set(roles.values()) <= _SOURCE_ROLES.keys():
                raise IraError("invalid_input")
            result = await run_in_threadpool(
                _workflow(request).prepare_task,
                sources=intervals["source"],
                declarations=intervals["declaration"],
                roles=roles,
                task_name=_str(form, "task_name", 100, required=False),
                as_of=_str(form, "as_of", 10, required=False) or None,
            )
            return _redirect(f"/workbench/drafts/{result['draft_id']}")

    return await _action(request, execute)


@router.post("/workbench/inputs/pdf")
async def upload_pdf(request: Request) -> Response:
    async def execute() -> Response:
        settings = _evidence(request).settings
        if not settings.enable_uploads:
            raise IraError("permission_denied")
        limit = settings.max_pdf_bytes + settings.max_json_bytes + 16 * 1024
        async with _form(request, limit, files=2) as form:
            filename, pdf = await _upload(form, "pdf", settings.max_pdf_bytes)
            _, parsed = await _upload(form, "parsed", settings.max_json_bytes)
            import_id = _str(form, "import_id", 64, required=False)
            result = await run_in_threadpool(
                _workflow(request).ingest_pair_bytes, pdf, parsed, filename=filename[:255]
            )
            if form.get("task_mode") == "multi":
                return _redirect("/workbench/tasks/new", added=result.parse_run.parse_run_id)
            return _redirect(
                "/workbench/import", parse_run_id=result.parse_run.parse_run_id, import_id=import_id
            )

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
            if form.get("task_mode") == "multi":
                return _redirect("/workbench/tasks/new", added=result["import_id"])
            return _redirect(
                "/workbench/import", parse_run_id=parse_run_id, import_id=result["import_id"]
            )

    return await _action(request, execute)


@router.post("/workbench/drafts")
async def prepare(request: Request) -> Response:
    async def execute() -> Response:
        async with _form(request) as form:
            pages = _str(form, "pages", 20, required=False)
            result = await run_in_threadpool(
                _workflow(request).prepare,
                _id(_str(form, "parse_run_id", 64)),
                _id(_str(form, "import_id", 64)),
                pages=_interval(pages) if pages else None,
                task_name=_str(form, "task_name", 100, required=False),
            )
            return _redirect(f"/workbench/drafts/{result['draft_id']}")

    return await _action(request, execute)


@router.get("/workbench/drafts/{draft_id}", name="web_draft")
def draft(request: Request, draft_id: str, version_saved: bool = False) -> Response:
    try:
        workflow = _workflow(request)
        item = workflow.get_draft(_id(draft_id))
        scope = item.get("task_scope")
        diagnostics = (
            None
            if item["requirements"] or scope
            else workflow.diagnose_requirements(
                item["parse_run_id"],
                pages=tuple(item["physical_pages"]) if item.get("physical_pages") else None,
            )
        )
        declarations = {row["item_id"]: row for row in item["declarations"]}
        matches = defaultdict(list)
        for edge in item["candidate_edges"]:
            matches[edge["left_ref"]].append(
                declarations[edge["right_ref"]] | {"candidate_edge": edge}
            )
        latest = workflow.store.latest_selection(draft_id)
        selected = {
            entry["requirement_id"]: entry
            for entry in (latest["applicability_snapshot"] if latest else [])
            if any(
                row["item_id"] == entry["declaration_id"]
                for row in matches.get(entry["requirement_id"], [])
            )
        }
        unresolved = {
            key for key, state in item.get("revision_states", {}).items() if state == "unresolved"
        }
        conflicts = [
            (left, right)
            for left, others in item.get("revision_conflicts", {}).items()
            for right in others
            if left < right and (left in unresolved or right in unresolved)
        ]
        return _page(
            request,
            "draft",
            draft=item,
            matches=matches,
            diagnostics=diagnostics,
            task_label=workflow.draft_label(item),
            scope=scope,
            source_labels=item.get("task_source_labels", {}),
            sheet_labels=item.get("task_sheet_labels", {}),
            source_ranges={key: _range_label(value) for key, value in scope["sources"].items()}
            if scope
            else {},
            sheet_ranges={key: _range_label(value) for key, value in scope["declarations"].items()}
            if scope
            else {},
            role_labels=_SOURCE_ROLES,
            requirements={row["candidate_id"]: row for row in item["requirements"]},
            task_runs=workflow.list_task_runs(draft_id) if scope else [],
            version_saved=version_saved,
            unresolved=unresolved,
            conflicts=conflicts,
            selection_run=latest["run_id"] if latest else "",
            selected_pairs=selected,
        )
    except IraError as error:
        return _error(request, error)


@router.post("/workbench/drafts/{draft_id}/version-reviews")
async def record_version_review(request: Request, draft_id: str) -> Response:
    async def execute() -> Response:
        _require_human_origin(request)
        identity = _id(draft_id)
        async with _form(request) as form:
            expected = {
                "csrf",
                "later",
                "earlier",
                "evidence",
                "reviewer",
                "note",
                "attest",
                "snapshot",
                "review_token",
                "decision",
            }
            if (
                set(form) != expected
                or any(len(form.getlist(key)) != 1 for key in form)
                or form.get("attest") != "on"
            ):
                raise IraError("invalid_input")
            decision = _str(form, "decision", 24)
            if decision not in ("confirmed", "rejected"):
                raise IraError("invalid_input")
            await run_in_threadpool(
                _workflow(request).record_version_relation,
                identity,
                _id(_str(form, "later", 64)),
                _id(_str(form, "earlier", 64)),
                _id(_str(form, "evidence", 64)),
                _str(form, "reviewer", 120),
                _str(form, "note", 2000),
                snapshot_id=_id(_str(form, "snapshot", 64)),
                review_token=_id(_str(form, "review_token", 64)),
                decision=decision,
            )
            return _redirect(f"/workbench/drafts/{identity}", version_saved="true")

    return await _action(request, execute)


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
                _workflow(request).verify,
                _id(draft_id),
                confirmations,
                _str(form, "reviewer", 80),
                mapping_reviewed=form.get("mapping_reviewed") == "on",
                conditional_preview=form.get("conditional_preview") == "on",
                use_human_reviews=form.get("use_human_reviews") == "on",
                expected_run=_selection_run(form),
            )
            return _redirect(f"/workbench/runs/{run['run_id']}")

    return await _action(request, execute)


def _selection_run(form: FormData) -> str:
    if len(form.getlist("selection_run")) > 1:
        raise IraError("invalid_input")
    value = _str(form, "selection_run", 32, required=False)
    return _id(value, 32) if value else ""


def _human_form_signature(request: Request, token: str) -> str:
    return digest(request.app.state.human_form_key, token.encode("ascii"), "sha256").hex()


def _human_pair_path(draft_id: str, left_ref: str, right_ref: str) -> str:
    return f"/workbench/drafts/{_id(draft_id)}/pairs/{_id(left_ref)}/{_id(right_ref)}"


def _require_human_origin(request: Request) -> None:
    if request.headers.get("origin") != f"http://{request.headers.get('host')}":
        raise IraError("permission_denied")


def _pair_return(workflow, draft_id, left_ref, right_ref, run_id, finding_id) -> dict:
    if not run_id and not finding_id:
        return {}
    run = workflow.get_run(_id(run_id, 32))
    if run["draft_id"] != draft_id or not any(
        finding["finding_id"] == finding_id
        and (finding.get("candidate_edge") or {}).get("left_ref") == left_ref
        and (finding.get("candidate_edge") or {}).get("right_ref") == right_ref
        for finding in run["findings"]
    ):
        raise IraError("not_found")
    return {"run_id": run_id, "finding_id": finding_id}


@router.get("/workbench/drafts/{draft_id}/pairs/{left_ref}/{right_ref}")
def pair_review(
    request: Request,
    draft_id: str,
    left_ref: str,
    right_ref: str,
    run_id: str = "",
    finding_id: str = "",
    saved: str = "",
) -> Response:
    try:
        route = _human_pair_path(draft_id, left_ref, right_ref)
        workflow = _workflow(request)
        return_query = _pair_return(workflow, draft_id, left_ref, right_ref, run_id, finding_id)
        context = workflow.human_review_context(draft_id, left_ref, right_ref)
        latest = workflow.store.latest_selection(draft_id)
        evidence = context["inputs"]["source"]["evidence"]
        scope_draft = draft_id if context["inputs"].get("task_scope") else ""
        item, location, problem = _original(request, evidence["evidence_id"], draft_id=scope_draft)
        return _page(
            request,
            "pair_review",
            context=context,
            route=route,
            draft_id=draft_id,
            item=item,
            location=location,
            problem=problem,
            scope_draft=scope_draft,
            signature=_human_form_signature(request, context["review_token"]),
            task_label=workflow.get_draft_label(draft_id),
            source_label=workflow.specification_label(item.parse_run_id) if item else "",
            return_run_id=run_id,
            return_finding_id=finding_id,
            return_query=urlencode(return_query),
            selection_run=latest["run_id"] if latest else "",
            can_recalculate=not latest
            or any(
                (entry["requirement_id"], entry["declaration_id"]) == (left_ref, right_ref)
                for entry in latest["applicability_snapshot"]
            ),
            saved=saved in ("fields", "source", "binding", "applicability"),
        )
    except IraError as error:
        return _error(request, error)


@router.post("/workbench/drafts/{draft_id}/pairs/{left_ref}/{right_ref}/record")
async def record_pair_review(
    request: Request,
    draft_id: str,
    left_ref: str,
    right_ref: str,
    run_id: str = "",
    finding_id: str = "",
) -> Response:
    async def execute() -> Response:
        _require_human_origin(request)
        route = _human_pair_path(draft_id, left_ref, right_ref)
        async with _form(request) as form:
            allowed = {
                "csrf",
                "token",
                "signature",
                "category",
                "target",
                "decision",
                "reviewer",
                "note",
                "attest",
            }
            if (
                not set(form) <= allowed
                or form.get("attest") != "on"
                or any(len(form.getlist(key)) != 1 for key in form if key != "target")
            ):
                raise IraError("invalid_input")
            token, signature = _id(_str(form, "token", 64)), _id(_str(form, "signature", 64))
            if not compare_digest(signature, _human_form_signature(request, token)):
                raise IraError("permission_denied")
            category = _str(form, "category", 24)
            workflow = _workflow(request)
            return_query = await run_in_threadpool(
                _pair_return, workflow, draft_id, left_ref, right_ref, run_id, finding_id
            )
            targets = form.getlist("target")
            if len(targets) > 32 or not all(
                isinstance(target, str) and len(target) <= 64 for target in targets
            ):
                raise IraError("invalid_input")
            await run_in_threadpool(
                workflow.record_human_review,
                draft_id,
                left_ref,
                right_ref,
                token,
                category,
                targets if category == "fields" else [category],
                _str(form, "decision", 24),
                _str(form, "reviewer", 120),
                _str(form, "note", 2000),
            )
            return _redirect(route, **return_query, saved=category)

    return await _action(request, execute)


@router.post("/workbench/drafts/{draft_id}/pairs/{left_ref}/{right_ref}/recalculate")
async def recalculate_pair(
    request: Request, draft_id: str, left_ref: str, right_ref: str
) -> Response:
    async def execute() -> Response:
        _require_human_origin(request)
        _human_pair_path(draft_id, left_ref, right_ref)
        async with _form(request) as form:
            if not {"csrf", "reviewer"} <= set(form) <= {
                "csrf",
                "reviewer",
                "selection_run",
            } or any(len(form.getlist(key)) != 1 for key in form):
                raise IraError("invalid_input")
            run = await run_in_threadpool(
                _workflow(request).recalculate_pair,
                draft_id,
                left_ref,
                right_ref,
                _str(form, "reviewer", 120),
                _selection_run(form),
            )
            finding = next(
                f for f in run["findings"] if f["requirement"]["candidate_id"] == left_ref
            )
            return _redirect(f"/workbench/runs/{run['run_id']}", finding_id=finding["finding_id"])

    return await _action(request, execute)


@router.get("/workbench/review")
def open_run(request: Request, run_id: str) -> Response:
    try:
        return _redirect(f"/workbench/runs/{_id(run_id, 32)}")
    except IraError as error:
        return _error(request, error)


@router.get("/workbench/search", name="web_search")
def search(request: Request, q: str = "", asset_id: str = "", physical_page: str = "") -> Response:
    try:
        if physical_page and not (
            len(physical_page) <= 9 and physical_page.isascii() and physical_page.isdecimal()
        ):
            raise IraError("invalid_location")
        result = None
        if q:
            result = _evidence(request).search_evidence(
                q,
                source_asset_id=_id(asset_id) if asset_id else None,
                physical_page=int(physical_page) if physical_page else None,
            )
        return _page(
            request,
            "search",
            q=q,
            asset_id=asset_id,
            physical_page=physical_page,
            result=result,
            specifications=_workflow(request).specification_choices(by_asset=True),
        )
    except IraError as error:
        return _error(request, error)


def _original(
    request: Request, evidence_id: str, query: str | None = None, *, draft_id: str = ""
) -> tuple[object | None, object | None, dict | None]:
    if not evidence_id:
        return None, None, None
    if draft_id:
        return _workflow(request).task_evidence(draft_id, _id(evidence_id)), None, None
    try:
        item = _evidence(request).get_evidence(_id(evidence_id), query=query)
    except IraError as error:
        return None, None, error.payload()
    try:
        return item, _evidence(request).get_evidence_page(evidence_id, query=query), None
    except IraError as error:
        return item, None, error.payload()


@router.get("/workbench/drafts/{draft_id}/evidence/{evidence_id}")
def scoped_evidence(request: Request, draft_id: str, evidence_id: str) -> Response:
    try:
        workflow = _workflow(request)
        item = workflow.task_evidence(_id(draft_id), _id(evidence_id))
        return _page(
            request,
            "evidence",
            item=item,
            location=None,
            problem=None,
            scope_draft=draft_id,
            task_label=workflow.get_draft_label(draft_id),
            source_label=workflow.specification_label(item.parse_run_id),
        )
    except IraError as error:
        return _error(request, error)


@router.get("/workbench/drafts/{draft_id}/evidence/{evidence_id}/pages/{physical_page}/image")
def scoped_evidence_image(
    request: Request, draft_id: str, evidence_id: str, physical_page: int
) -> Response:
    try:
        item = _workflow(request).task_evidence(_id(draft_id), _id(evidence_id))
        if physical_page != item.physical_page:
            raise IraError("permission_denied")
        return Response(
            _evidence(request).get_pdf_page_image(item.source_asset_id, physical_page),
            media_type="image/png",
        )
    except IraError as error:
        return _error(request, error)


@router.get("/workbench/evidence/{evidence_id}", name="web_evidence")
def evidence(
    request: Request,
    evidence_id: str,
    q: str | None = None,
    run_id: str = "",
    task_id: str = "",
) -> Response:
    try:
        agent_return = None
        if run_id and task_id:
            task = _workflow(request).get_agent_task(_id(run_id, 32), _id(task_id, 32))
            if evidence_id not in task.get("evidence_ids", []):
                raise IraError("not_found")
            agent_return = f"/workbench/runs/{run_id}/agent-tasks/{task_id}"
        item, location, problem = _original(request, evidence_id, query=q)
        return _page(
            request,
            "evidence",
            item=item,
            location=location,
            problem=problem,
            agent_return=agent_return,
            source_label=_workflow(request).specification_label(item.parse_run_id) if item else "",
        )
    except IraError as error:
        return _error(request, error)


@router.get("/workbench/runs/{run_id}", name="web_run")
def inspect_run(
    request: Request,
    run_id: str,
    finding_id: str = "",
    status: str = "",
    page: int | None = None,
    view: str = "",
    evidence: bool = False,
    panel: str = "",
) -> Response:
    try:
        workflow = _workflow(request)
        run = workflow.get_run(_id(run_id, 32))
        multi_source = bool(run.get("task_scope"))
        findings = run["findings"]
        if status and status not in _OUTCOMES:
            raise IraError("invalid_input")
        selected = next(
            (finding for finding in findings if finding["finding_id"] == finding_id), None
        )
        if finding_id and selected is None:
            raise IraError("no_evidence_found")
        if multi_source and panel == "agent":
            raise IraError("permission_denied")
        show_agent = panel == "agent"
        if selected is None and findings and view != "list" and not status and not show_agent:
            selected = findings[0]
        filtered = [row for row in findings if not status or row["disposition"] == status]
        page_count = max(1, (len(filtered) + 9) // 10)
        if page is None:
            page = filtered.index(selected) // 10 + 1 if selected in filtered else 1
        page = max(1, min(page, page_count))
        requirement = selected.get("requirement") if selected and not show_agent else None
        scope_draft = run["draft_id"] if multi_source else ""
        item, location, problem = _original(
            request, (requirement or {}).get("evidence_id", ""), draft_id=scope_draft
        )
        source_binding = run["binding_state"]
        if multi_source and requirement:
            source_binding = run["task_assets"]["sources"][requirement["parse_run_id"]][
                "parse_run"
            ]["binding_state"]
        return _page(
            request,
            "review",
            run=run,
            selected=selected,
            domain_finding=persisted_finding(selected, stale=run["status"] == "stale")
            if selected
            else None,
            item=item,
            location=location,
            problem=problem,
            scope_draft=scope_draft,
            decisions=_DECISIONS,
            outcomes=_OUTCOMES,
            listed=filtered[(page - 1) * 10 : page * 10],
            filtered_count=len(filtered),
            filter_status=status,
            page_number=page,
            page_count=page_count,
            show_evidence=evidence,
            show_agent=show_agent,
            multi_source=multi_source,
            source_binding=source_binding,
            task_label=workflow.get_draft_label(run["draft_id"]),
            source_label=workflow.specification_label(item.parse_run_id) if item else "",
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
        async with _form(request) as form:
            finding_id = _str(form, "finding_id", 64, required=False)
            task_id = _workflow(request).launch_investigation(
                _id(run_id, 32),
                goal=_str(form, "goal", 256, required=False) or None,
                finding_id=_id(finding_id) if finding_id else None,
                allow_reverification=form.get("allow_reverification") == "on",
            )
            return _redirect(f"/workbench/runs/{run_id}/agent-tasks/{task_id}")

    return await _action(request, execute)


@router.get("/workbench/runs/{run_id}/agent-tasks/{task_id}")
def inspect_agent_task(
    request: Request, run_id: str, task_id: str, export: bool = False
) -> Response:
    try:
        workflow = _workflow(request)
        report = workflow.get_agent_task(_id(run_id, 32), _id(task_id, 32))
        if export:
            return JSONResponse(report)
        run = workflow.get_run(run_id)
        candidates = (report.get("scope") or {}).get("candidate_ids", [])
        target = next(
            (row for row in run["findings"] if row["requirement"]["candidate_id"] in candidates),
            None,
        )
        return _page(
            request,
            "investigate",
            run_id=run_id,
            report=report,
            run=run,
            task_label=workflow.get_draft_label(run["draft_id"]),
            target=target,
        )
    except IraError as error:
        return _error(request, error)


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


@router.get("/workbench/runs/{run_id}/verification.json")
def verification_result(request: Request, run_id: str) -> Response:
    try:
        identity = _id(run_id, 32)
        result = _workflow(request).domain_result(identity)
        return JSONResponse(
            result.model_dump(mode="json"),
            headers={
                "Content-Disposition": f'attachment; filename="ira-verification-{identity}.json"'
            },
        )
    except IraError as error:
        return _error(request, error)


@router.get("/workbench/runs/{run_id}/report")
@router.get("/workbench/runs/{run_id}/report.json")
def run_report(request: Request, run_id: str) -> Response:
    try:
        workflow = _workflow(request)
        run = workflow.get_run(_id(run_id, 32))
        report = summarize_run(run)
        if request.url.path.endswith(".json"):
            return JSONResponse(
                report,
                headers={
                    "Content-Disposition": f'attachment; filename="ira-summary-{run_id}.json"'
                },
            )
        return _page(
            request,
            "report",
            report=report,
            run=run,
            outcomes=_OUTCOMES,
            task_label=workflow.get_draft_label(run["draft_id"]),
        )
    except IraError as error:
        return _error(request, error)
