"""Typer entry point for local evidence workflows."""

import json
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Annotated

import typer
import uvicorn
from pydantic import BaseModel

from ira_native.application.service import EvidenceService
from ira_native.errors import IraError
from ira_native.settings import Settings

app = typer.Typer(
    no_args_is_help=True, pretty_exceptions_enable=False, help="Local source and evidence registry."
)
ingest = typer.Typer(no_args_is_help=True, help="Register local source files.")
evidence = typer.Typer(no_args_is_help=True, help="Read evidence and locate the original page.")
app.add_typer(ingest, name="ingest")
app.add_typer(evidence, name="evidence")


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


def main() -> None:
    app()
