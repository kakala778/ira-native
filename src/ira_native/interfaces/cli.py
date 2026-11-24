"""Typer entry point for local evidence workflows."""

import asyncio
import json
from collections.abc import Iterator
from contextlib import contextmanager
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
        bool, typer.Option("--exact", help="Match the complete normalized block.")
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
def show(evidence_id: str) -> None:
    """Show the registered evidence and its native reference."""
    with _service() as service:
        _emit(service.get_evidence(evidence_id))


@evidence.command()
def page(evidence_id: str) -> None:
    """Check the original PDF and describe the physical page and trusted precision."""
    with _service() as service:
        _emit(service.get_evidence_page(evidence_id))


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
    draft_id: str, reviewer: Annotated[str, typer.Option()], confirmations: Path | None = None
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
        _emit(VerificationWorkflow(service).verify(draft_id, selected, reviewer))


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
def investigate_verification(run_id: str) -> None:
    """Investigate a deterministic evidence gap using the configured local model."""
    with _service() as service:
        _emit(asyncio.run(VerificationWorkflow(service).investigate(run_id)))


def main() -> None:
    app()
