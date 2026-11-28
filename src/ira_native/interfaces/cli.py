"""Typer entry point for local evidence workflows."""

import asyncio
import json
from collections import Counter
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import replace
from pathlib import Path
from typing import Annotated

import typer
import uvicorn
from pydantic import BaseModel

from ira_native.application.service import EvidenceService
from ira_native.application.workflow import VerificationWorkflow
from ira_native.errors import IraError
from ira_native.settings import Settings

app = typer.Typer(
    no_args_is_help=True, pretty_exceptions_enable=False, help="Local source and evidence registry."
)
ingest = typer.Typer(no_args_is_help=True, help="Register local source files.")
evidence = typer.Typer(no_args_is_help=True, help="Read evidence and locate the original page.")
app.add_typer(ingest, name="ingest")
app.add_typer(evidence, name="evidence")
verification = typer.Typer(no_args_is_help=True, help="Prepare and verify GPON declarations.")
app.add_typer(verification, name="verify")


@contextmanager
def _service() -> Iterator[EvidenceService]:
    try:
        with EvidenceService(Settings.from_env()) as service:
            yield service
    except IraError as error:
        typer.echo(json.dumps(error.payload(), ensure_ascii=False), err=True)
        raise typer.Exit(code=1) from None


def _emit(value: BaseModel | dict) -> None:
    data = value.model_dump(mode="json") if isinstance(value, BaseModel) else value
    typer.echo(json.dumps(data, ensure_ascii=False))


@app.command()
def doctor() -> None:
    """Report the active SQLite search backend and local policy."""
    with _service() as service:
        _emit(service.doctor())


@ingest.command("pdf")
def ingest_pdf(
    pdf: Annotated[Path, typer.Option(help="PDF inside an allowed local root.")],
    parsed: Annotated[Path, typer.Option(help="MinerU 4.2-shaped JSON inside an allowed root.")],
    declared: Annotated[bool, typer.Option(help="Record the user's pairing declaration.")] = True,
) -> None:
    with _service() as service:
        _emit(service.ingest_pair(pdf, parsed, declared=declared))


@app.command()
def search(
    query: str,
    asset_id: Annotated[str | None, typer.Option("--asset-id")] = None,
    parse_run_id: Annotated[str | None, typer.Option("--parse-run-id")] = None,
    page: Annotated[int | None, typer.Option("--page", help="1-based physical PDF page.")] = None,
    kind: Annotated[str | None, typer.Option("--kind")] = None,
    exact: Annotated[
        bool, typer.Option("--exact", help="Match a complete normalized body or annotation.")
    ] = False,
    limit: Annotated[int, typer.Option("--limit")] = 50,
) -> None:
    """Search registered evidence in the active allowlist."""
    with _service() as service:
        _emit(
            service.search_evidence(
                query,
                source_asset_id=asset_id,
                parse_run_id=parse_run_id,
                physical_page=page,
                kind=kind,
                mode="exact" if exact else "literal",
                limit=limit,
            )
        )


@evidence.command()
def show(evidence_id: str, query: str | None = None, exact: bool = False) -> None:
    """Show the registered evidence and its native reference."""
    with _service() as service:
        _emit(service.get_evidence(evidence_id, query=query, mode="exact" if exact else "literal"))


@evidence.command()
def page(evidence_id: str, query: str | None = None, exact: bool = False) -> None:
    """Check the original PDF and describe the physical page and trusted precision."""
    with _service() as service:
        _emit(
            service.get_evidence_page(
                evidence_id, query=query, mode="exact" if exact else "literal"
            )
        )


@app.command()
def serve(port: Annotated[int, typer.Option(min=1, max=65535)] = 8000) -> None:
    """Start the HTTP API on IPv4 loopback."""
    uvicorn.run(
        "ira_native.interfaces.api:create_app",
        factory=True,
        host="127.0.0.1",
        port=port,
        access_log=False,
        proxy_headers=False,
    )


@verification.command("prepare")
def prepare_verification(
    parse_run_id: str,
    spreadsheet: Path,
    sheet: str | None = None,
    encoding: str = "utf-8-sig",
    mapping_json: str = "{}",
) -> None:
    """Import a mapped local declaration file and show reviewable evidence candidates."""
    with _service() as service:
        try:
            mapping = json.loads(mapping_json)
        except ValueError:
            raise IraError("invalid_input") from None
        if not isinstance(mapping, dict) or any(not isinstance(v, str) for v in mapping.values()):
            raise IraError("invalid_input")
        workflow = VerificationWorkflow(service)
        declaration = workflow.ingest_spreadsheet_path(spreadsheet, mapping, sheet, encoding)
        _emit(workflow.prepare(parse_run_id, declaration["import_id"]))


@verification.command("run")
def run_verification(
    draft_id: str,
    reviewer: Annotated[str, typer.Option()],
    confirmations: Path | None = None,
    conditional_preview: bool = False,
    mapping_reviewed: bool = False,
    use_human_reviews: bool = False,
) -> None:
    """Run against explicit checked pairing records, or keep the inputs as candidates."""
    with _service() as service:
        selected = []
        if confirmations is not None:
            _, data = service.files.read(confirmations, service.settings.max_json_bytes)
            try:
                selected = json.loads(data)
            except ValueError:
                raise IraError("invalid_input") from None
            if not isinstance(selected, list):
                raise IraError("invalid_input")
        _emit(
            VerificationWorkflow(service).verify(
                draft_id,
                selected,
                reviewer,
                conditional_preview=conditional_preview,
                mapping_reviewed=mapping_reviewed,
                use_human_reviews=use_human_reviews,
            )
        )


@verification.command("offline")
def offline_verification(
    pdf: Annotated[Path, typer.Option()],
    parsed: Annotated[Path, typer.Option()],
    spreadsheet: Annotated[Path, typer.Option()],
    database: Annotated[Path, typer.Option()],
    pages: Annotated[
        str | None, typer.Option(help="Inclusive 1-based physical page range, e.g. 2-4.")
    ] = None,
    mapping_json: str = "{}",
    sheet: str | None = None,
    encoding: str = "utf-8-sig",
    summary: Path | None = None,
    local_details: Path | None = None,
    confirmations: Path | None = None,
    reviewer: str = "Offline review",
    use_human_reviews: bool = False,
) -> None:
    """Run locally. Standard output and --summary contain counts and reason codes only."""
    try:
        interval = None
        if pages is not None:
            first, _, last = pages.partition("-")
            interval = (int(first), int(last or first))
            if not 1 <= interval[0] <= interval[1]:
                raise ValueError
        mapping = json.loads(mapping_json)
        if not isinstance(mapping, dict) or any(not isinstance(v, str) for v in mapping.values()):
            raise ValueError
        paths = [pdf, parsed, spreadsheet]
        if confirmations is not None:
            paths.append(confirmations)
        protected = {path.resolve() for path in paths}
        outputs = [
            path.resolve() for path in (database, summary, local_details) if path is not None
        ]
        if len(outputs) != len(set(outputs)) or protected.intersection(outputs):
            raise ValueError
        if any(path.exists() for path in outputs if path != database.resolve()):
            raise ValueError
        settings = replace(
            Settings.from_env(),
            database=database,
            allowed_roots=tuple(dict.fromkeys(path.resolve().parent for path in paths)),
        )
        with EvidenceService(settings) as service:
            workflow = VerificationWorkflow(service)
            source = service.ingest_pair(pdf, parsed, declared=True).parse_run
            imported = workflow.ingest_spreadsheet_path(spreadsheet, mapping, sheet, encoding)
            draft = workflow.prepare(source.parse_run_id, imported["import_id"], pages=interval)
            selected = []
            if confirmations is not None:
                _, data = service.files.read(confirmations, settings.max_json_bytes)
                selected = json.loads(data)
                if not isinstance(selected, list):
                    raise ValueError
            run = workflow.verify(
                draft["draft_id"],
                selected,
                reviewer,
                conditional_preview=True,
                use_human_reviews=use_human_reviews,
            )
            diagnostic = workflow.diagnose_requirements(source.parse_run_id, pages=interval)
            counts = {key: value for key, value in diagnostic.items() if key != "evidence_index"}
            counts.update(
                declaration_rows=len(draft["declarations"]),
                candidate_edges=len(draft["candidate_edges"]),
                system_confirmed=sum(e["system_confirmation"] for e in draft["candidate_edges"]),
                field_verification_state=run["field_verification_state"],
                formal_statuses=dict(Counter(f["formal_status"] for f in run["findings"])),
                workflow_dispositions=dict(Counter(f["disposition"] for f in run["findings"])),
                conditional_statuses=dict(
                    Counter(f["conditional_calculation"]["status"] for f in run["findings"])
                ),
                conditional_reasons=dict(
                    Counter(f["conditional_calculation"]["reason_code"] for f in run["findings"])
                ),
            )
            for path, content in ((summary, counts), (local_details, {"draft": draft, "run": run})):
                if path is not None:
                    with path.open("x", encoding="utf-8") as output:
                        json.dump(content, output, ensure_ascii=False, indent=2)
            _emit(counts)
    except (IraError, OSError, ValueError) as error:
        problem = error if isinstance(error, IraError) else IraError("invalid_input")
        typer.echo(json.dumps(problem.payload(), ensure_ascii=False), err=True)
        raise typer.Exit(code=1) from None


@verification.command("diagnose")
def diagnose_requirements(parse_run_id: str) -> None:
    """Print extraction stage counts and reasons without document text."""
    with _service() as service:
        _emit(VerificationWorkflow(service).diagnose_requirements(parse_run_id))


@verification.command("show")
def show_verification(run_id: str, csv: bool = False) -> None:
    """Read JSON results or export formula-safe CSV."""
    with _service() as service:
        workflow = VerificationWorkflow(service)
        if csv:
            typer.echo(workflow.export_csv(run_id), nl=False)
        else:
            _emit(workflow.get_run(run_id))


@verification.command("compare")
def compare_requirements(
    left_evidence_id: str,
    right_evidence_id: str,
    fields_checked: bool = False,
    same_version: bool = False,
    left_row: int | None = None,
    right_row: int | None = None,
) -> None:
    """Compare two independently located requirements."""
    with _service() as service:
        _emit(
            VerificationWorkflow(service).compare(
                left_evidence_id,
                right_evidence_id,
                fields_checked=fields_checked,
                same_version=same_version,
                left_row=left_row,
                right_row=right_row,
            )
        )


@verification.command("investigate")
def investigate_verification(
    run_id: str,
    goal: str | None = None,
    finding_id: str | None = None,
    allow_reverification: bool = False,
) -> None:
    """Investigate a deterministic evidence gap using the configured local model."""
    with _service() as service:
        _emit(
            asyncio.run(
                VerificationWorkflow(service).investigate(
                    run_id,
                    goal=goal,
                    finding_id=finding_id,
                    allow_reverification=allow_reverification,
                )
            )
        )


@verification.command("agent-task")
def show_agent_task(run_id: str, task_id: str) -> None:
    """Read the persisted structured Agent trace."""
    with _service() as service:
        _emit(VerificationWorkflow(service).get_agent_task(run_id, task_id))


def main() -> None:
    app()
