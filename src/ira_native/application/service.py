"""One local evidence service for CLI, HTTP and later verification workflows."""

import hashlib
import logging
import sqlite3
from collections.abc import Callable
from pathlib import Path
from typing import Literal, Self

from ira_native import __version__
from ira_native.adapters import mineru, pdf
from ira_native.adapters.files import LocalFiles
from ira_native.adapters.repository import AssetInput, Backend, Registry
from ira_native.domain.models import (
    AccessPolicy,
    AssetDetail,
    AssetView,
    BindingState,
    Document,
    DocumentRevision,
    Evidence,
    IngestResult,
    ParseRun,
    PdfPageLocation,
    SearchResult,
    SourceAsset,
    SourceKind,
)
from ira_native.errors import IraError
from ira_native.settings import Settings

_LOGGER = logging.getLogger("ira_native.events")
_MANAGED = "registry"


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _identity(*parts: str) -> str:
    return _sha("\0".join(parts).encode("utf-8"))


def _require_pdf(asset: SourceAsset) -> None:
    if asset.source_kind != SourceKind.PDF:
        raise IraError("unsupported_format")


class EvidenceService:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.files = LocalFiles(settings.allowed_roots)
        self.repository = Registry(settings.database)

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *_exc) -> None:
        self.repository.close()

    def doctor(self) -> dict[str, str | int | bool]:
        return {
            "status": "ok",
            "version": __version__,
            "sqlite_version": sqlite3.sqlite_version,
            "fts5_trigram": self.repository.fts_available,
            "search_backend": self._search_backend("long", "literal"),
            "allowed_root_count": len(self.files.roots),
            "external_models": False,
            "http_ingest_enabled": self.settings.enable_uploads,
        }

    def byte_limit(self, kind: SourceKind) -> int:
        return (
            self.settings.max_pdf_bytes if kind == SourceKind.PDF else self.settings.max_json_bytes
        )

    def _prepare_source(self, data: bytes, kind: SourceKind, locator: str) -> AssetInput:
        if len(data) > self.byte_limit(kind):
            raise IraError("resource_limit")
        page_count = None
        if kind == SourceKind.PDF:
            with pdf.open_pdf(data, self.settings.max_pages) as document:
                page_count = len(document)
                for physical_page in range(1, page_count + 1):
                    with pdf.open_page(document, physical_page) as page:
                        pdf.page_info("", page, physical_page, page_count)
        else:
            self._parse(data)
        sha = _sha(data)
        asset = SourceAsset(
            asset_id=_identity("source_asset", kind.value, sha),
            sha256=sha,
            media_type=kind.media_type,
            source_kind=kind,
            access_policy=AccessPolicy(),
            size_bytes=len(data),
            page_count=page_count,
            locator_internal=locator,
        )
        cached = data if kind == SourceKind.PARSED_JSON or locator == _MANAGED else None
        return AssetInput(asset, cached)

    def _read_local(self, path: Path, kind: SourceKind) -> tuple[str, bytes]:
        resolved, data = self.files.read(path, self.byte_limit(kind))
        return str(resolved), data

    def ingest_source(self, path: Path, kind: SourceKind) -> SourceAsset:
        locator, data = self._read_local(path, kind)
        return self._register_source(self._prepare_source(data, kind, locator))

    def ingest_uploaded_source(self, data: bytes, kind: SourceKind) -> SourceAsset:
        if not self.settings.enable_uploads:
            raise IraError("permission_denied")
        return self._register_source(self._prepare_source(data, kind, _MANAGED))

    def _register_source(self, prepared: AssetInput) -> SourceAsset:
        self._authorize(prepared.asset)
        # Authorization of any existing record must precede deduplication into that record.
        try:
            self._get_asset(prepared.asset.asset_id)
        except IraError as error:
            if error.code != "source_missing":
                raise
        asset = self.repository.register_asset(prepared)
        self._authorize(asset)
        _LOGGER.info("event=source_registered")
        return asset

    def _parse(self, data: bytes) -> mineru.ParsedArtifact:
        return mineru.read_artifact(data, self.settings.max_pages, self.settings.max_blocks)

    def ingest_pair(
        self, pdf_path: Path, parsed_path: Path, *, declared: bool = True
    ) -> IngestResult:
        """Validate and commit the two assets, parse run and evidence in one transaction."""
        try:
            pdf_locator, pdf_data = self._read_local(pdf_path, SourceKind.PDF)
            json_locator, json_data = self._read_local(parsed_path, SourceKind.PARSED_JSON)
            source = self._prepare_source(pdf_data, SourceKind.PDF, pdf_locator)
            artifact = self._prepare_source(json_data, SourceKind.PARSED_JSON, json_locator)
            result = self._ingest_run(
                source.asset,
                artifact.asset,
                pdf_data,
                json_data,
                declared=declared,
                new_assets=(source, artifact),
            )
        except IraError as error:
            _LOGGER.warning("event=ingest_failed code=%s", error.code)
            raise
        _LOGGER.info("event=parse_run_registered")
        return result

    def ingest_uploaded_pair(self, pdf_data: bytes, json_data: bytes) -> IngestResult:
        """Register both uploaded byte assets and their declared parse run atomically."""
        if not self.settings.enable_uploads:
            raise IraError("permission_denied")
        source = self._prepare_source(pdf_data, SourceKind.PDF, _MANAGED)
        artifact = self._prepare_source(json_data, SourceKind.PARSED_JSON, _MANAGED)
        return self._ingest_run(
            source.asset,
            artifact.asset,
            pdf_data,
            json_data,
            declared=True,
            new_assets=(source, artifact),
        )

    def ingest_parse_run(
        self, source_asset_id: str, artifact_asset_id: str, *, declared: bool = True
    ) -> IngestResult:
        try:
            source = self._get_asset(source_asset_id)
            artifact = self._get_asset(artifact_asset_id)
            _require_pdf(source)
            if artifact.source_kind != SourceKind.PARSED_JSON:
                raise IraError("unsupported_format")
            result = self._ingest_run(
                source,
                artifact,
                self._read_asset(source),
                self._read_asset(artifact),
                declared=declared,
            )
        except IraError as error:
            _LOGGER.warning("event=ingest_failed code=%s", error.code)
            raise
        _LOGGER.info("event=parse_run_registered")
        return result

    def _ingest_run(
        self,
        source: SourceAsset,
        artifact: SourceAsset,
        pdf_data: bytes,
        json_data: bytes,
        *,
        declared: bool,
        new_assets: tuple[AssetInput, ...] = (),
    ) -> IngestResult:
        parsed = self._parse(json_data)
        claim = parsed.claimed_source_sha256
        if claim is not None and claim != source.sha256:
            raise IraError("source_mismatch")
        basis = ["user_declared_pair" if declared else "no_pair_attestation"]
        if claim is not None:
            basis.append("untrusted_artifact_sha256_claim_matches")
        run = ParseRun(
            parse_run_id=_identity(
                "parse_run", source.asset_id, artifact.asset_id, mineru.ADAPTER_VERSION
            ),
            source_asset_id=source.asset_id,
            artifact_asset_id=artifact.asset_id,
            adapter_version=mineru.ADAPTER_VERSION,
            parser_version=parsed.parser_version,
            parser_config=parsed.parser_config,
            binding_state=BindingState.DECLARED if declared else BindingState.UNKNOWN,
            binding_basis=tuple(basis),
        )
        records: list[tuple[Evidence, str]] = []
        with pdf.open_pdf(pdf_data, self.settings.max_pages) as document:
            indices = {index for index, _ in parsed.pages}
            if any(i >= len(document) for i in indices):
                raise IraError("invalid_location")
            if parsed.is_full_document and len(indices) != len(document):
                raise IraError("source_mismatch")
            for index, blocks in parsed.pages:
                with pdf.open_page(document, index + 1) as page:
                    for block in blocks:
                        bbox, reason = pdf.block_bbox(page, block)
                        risks = (*block.risk, "source_binding_unverified")
                        if reason:
                            risks = (*risks, reason)
                        records.append(
                            (
                                Evidence(
                                    evidence_id=_identity(
                                        "evidence",
                                        run.parse_run_id,
                                        block.pointer,
                                        run.adapter_version,
                                    ),
                                    parse_run_id=run.parse_run_id,
                                    source_asset_id=source.asset_id,
                                    artifact_asset_id=artifact.asset_id,
                                    source_pointer=block.pointer,
                                    page_index=index,
                                    physical_page=index + 1,
                                    raw_content=block.content,
                                    native_context=block.native_context,
                                    kind=block.kind,
                                    native_bbox=block.bbox,
                                    coord_system=block.coord_system,
                                    continues_prev=block.continues_prev,
                                    pdf_bbox=bbox,
                                    precision="block" if bbox else "page",
                                    location_basis="unique_pdf_text_region"
                                    if bbox
                                    else "physical_page",
                                    risk=risks,
                                    access_policy=source.access_policy,
                                ),
                                mineru.search_text(block),
                            )
                        )

        def verify_registered_assets(
            registered_pdf: SourceAsset, registered_json: SourceAsset
        ) -> None:
            self._authorize(registered_pdf)
            self._authorize(registered_json)
            # Identity deduplication keeps the first locator. Validate that it can still
            # supply the original bytes when the input was read from a different locator.
            if (
                registered_pdf.locator_internal != source.locator_internal
                and registered_pdf.locator_internal != _MANAGED
            ):
                self._read_asset(registered_pdf)

        run, count = self.repository.save_run(run, records, new_assets, verify_registered_assets)
        return IngestResult(
            source=AssetView(**source.model_dump()),
            artifact=AssetView(**artifact.model_dump()),
            parse_run=run,
            evidence_count=count,
        )

    def _authorize(self, asset: SourceAsset) -> None:
        if not asset.access_policy.local_read:
            raise IraError("permission_denied")
        if asset.locator_internal != _MANAGED:
            self.files.authorize(Path(asset.locator_internal))

    def _get_asset(self, asset_id: str) -> SourceAsset:
        asset = self.repository.get_asset(asset_id)
        self._authorize(asset)
        return asset

    def _read_asset(self, asset: SourceAsset) -> bytes:
        self._authorize(asset)
        limit = self.byte_limit(asset.source_kind)
        if asset.size_bytes > limit:
            raise IraError("resource_limit")
        if asset.locator_internal == _MANAGED or asset.source_kind == SourceKind.PARSED_JSON:
            data = self.repository.get_asset_data(asset.asset_id)
            if data is None:
                raise IraError("source_missing")
        else:
            _, data = self.files.read(Path(asset.locator_internal), limit)
        if len(data) > limit:
            raise IraError("resource_limit")
        if _sha(data) != asset.sha256:
            raise IraError("source_mismatch")
        return data

    def get_asset(self, asset_id: str) -> AssetDetail:
        asset = self._get_asset(asset_id)
        return AssetDetail(**asset.model_dump(), revisions=self.repository.list_revisions(asset_id))

    def register_revision(self, document: Document, revision: DocumentRevision) -> None:
        _require_pdf(self._get_asset(revision.asset_id))
        if document.document_id != revision.document_id or not all(
            value.strip() for value in (document.document_id, revision.revision_id)
        ):
            raise IraError("invalid_input")
        for evidence_id in revision.relation_evidence:
            self.get_evidence(evidence_id)
        self.repository.register_revision(document, revision)

    def get_run(self, parse_run_id: str) -> ParseRun:
        run = self.repository.get_run(parse_run_id)
        self._get_asset(run.source_asset_id)
        self._get_asset(run.artifact_asset_id)
        return run

    def revision_family(self, asset_id: str) -> list[DocumentRevision]:
        self._get_asset(asset_id)
        return self.repository.list_revision_family(asset_id)

    def check_parse_artifact(self, parse_run_id: str) -> None:
        run = self.get_run(parse_run_id)
        self._read_asset(self._get_asset(run.artifact_asset_id))

    def get_evidence(
        self,
        evidence_id: str,
        *,
        query: str | None = None,
        mode: Literal["literal", "exact"] = "literal",
        parse_run_id: str | None = None,
    ) -> Evidence:
        item = self.repository.get_evidence(evidence_id, parse_run_id=parse_run_id)
        self._get_asset(item.source_asset_id)
        self._get_asset(item.artifact_asset_id)
        if query is None:
            return (
                self._page_only(item, "annotation_context_unverified") if item.annotations else item
            )
        normalized = self._search_query(query, mode)
        hit = self._search_match(item, normalized, mode)
        if hit is None:
            raise IraError("no_evidence_found")
        return hit

    def _search_query(self, query: str, mode: Literal["literal", "exact"]) -> str:
        if (
            not query.strip()
            or "\0" in query
            or len(query) > self.settings.max_search_chars
            or mode not in ("literal", "exact")
        ):
            raise IraError("invalid_input")
        return mineru.normalize_search_text(query)

    @staticmethod
    def _search_match(
        item: Evidence, query: str, mode: Literal["literal", "exact"]
    ) -> Evidence | None:
        sources = tuple(
            dict.fromkeys(
                source
                for source, segment in mineru.search_segments(
                    item.kind, item.raw_content, item.native_context
                )
                if (segment == query if mode == "exact" else query in segment)
            )
        )
        if not sources:
            return None
        hit = item.model_copy(update={"match_source": sources})
        if "body" not in sources:
            return EvidenceService._page_only(hit, "annotation_only_match")
        if item.annotations and (
            item.location_basis != "unique_pdf_text_region" or item.pdf_bbox is None
        ):
            return EvidenceService._page_only(hit, "body_location_unverified")
        return hit

    @staticmethod
    def _page_only(item: Evidence, risk: str) -> Evidence:
        return item.model_copy(
            update={
                "precision": "page",
                "pdf_bbox": None,
                "native_bbox": None,
                "coord_system": None,
                "location_basis": "physical_page",
                "risk": (*item.risk, risk),
            }
        )

    def list_run_evidence(
        self, parse_run_id: str, *, pages: tuple[int, ...] | None = None
    ) -> list[Evidence]:
        run = self.get_run(parse_run_id)
        if pages is not None:
            total = self.get_asset(run.source_asset_id).page_count or 0
            if not pages or any(type(page) is not int or not 1 <= page <= total for page in pages):
                raise IraError("invalid_location")
        return self.repository.list_run_evidence(parse_run_id, pages=pages)

    def _search_backend(self, query: str, mode: Literal["literal", "exact"]) -> Backend:
        if mode == "exact":
            return "exact"
        if (
            self.repository.fts_available
            and self.settings.search_backend == "auto"
            and len(query) >= 3
        ):
            return "fts5"
        return "like"

    def _can_read(self, asset: SourceAsset) -> bool:
        try:
            self._authorize(asset)
        except IraError:
            return False
        return True

    def search_evidence(
        self,
        query: str,
        *,
        source_asset_id: str | None = None,
        parse_run_id: str | None = None,
        physical_page: int | None = None,
        kind: str | None = None,
        mode: Literal["literal", "exact"] = "literal",
        limit: int = 50,
        physical_pages: tuple[int, int] | None = None,
    ) -> SearchResult:
        if not 1 <= limit <= 200:
            raise IraError("invalid_input")
        normalized = self._search_query(query, mode)
        run = self.get_run(parse_run_id) if parse_run_id is not None else None
        if run is not None:
            if source_asset_id is None:
                source_asset_id = run.source_asset_id
        asset = self._get_asset(source_asset_id) if source_asset_id is not None else None
        if asset is not None:
            _require_pdf(asset)
        if physical_page is not None and (
            type(physical_page) is not int
            or physical_page < 1
            or (asset is not None and physical_page > asset.page_count)
        ):
            raise IraError("invalid_location")
        backend = self._search_backend(normalized, mode)
        allowed = (
            [run.source_asset_id, run.artifact_asset_id]
            if run
            else [
                source.asset_id
                for source in self.repository.list_assets()
                if self._can_read(source)
            ]
        )
        matches = self.repository.search(
            normalized,
            backend,
            allowed,
            source_asset_id=source_asset_id,
            parse_run_id=parse_run_id,
            physical_page=physical_page,
            kind=kind,
            limit=limit,
            project=lambda item: self._search_match(item, normalized, mode),
            physical_pages=physical_pages,
        )
        return SearchResult(
            items=matches, backend=backend, code=None if matches else "no_evidence_found"
        )

    def get_pdf_bytes(self, asset_id: str) -> bytes:
        asset = self._get_asset(asset_id)
        _require_pdf(asset)
        return self._read_asset(asset)

    def _with_pdf_page[T](
        self,
        asset_id: str,
        physical_page: int,
        callback: Callable[[pdf.pdfium.PdfPage, PdfPageLocation], T],
    ) -> T:
        data = self.get_pdf_bytes(asset_id)
        with pdf.open_pdf(data, self.settings.max_pages) as document:
            with pdf.open_page(document, physical_page) as page:
                location = pdf.page_info(asset_id, page, physical_page, len(document))
                return callback(page, location)

    def get_pdf_page(self, asset_id: str, physical_page: int) -> PdfPageLocation:
        return self._with_pdf_page(asset_id, physical_page, lambda _page, location: location)

    def get_pdf_page_image(self, asset_id: str, physical_page: int) -> bytes:
        return self._with_pdf_page(asset_id, physical_page, pdf.render_page)

    def get_evidence_page(
        self,
        evidence_id: str,
        *,
        query: str | None = None,
        mode: Literal["literal", "exact"] = "literal",
    ) -> PdfPageLocation:
        item = self.get_evidence(evidence_id, query=query, mode=mode)
        page = self.get_pdf_page(item.source_asset_id, item.physical_page)
        return page.model_copy(
            update={
                "evidence_id": item.evidence_id,
                "match_source": item.match_source,
                "annotation_fields": tuple(item.annotations),
                "precision": item.precision,
                "pdf_bbox": item.pdf_bbox,
                "risk": item.risk,
            }
        )
