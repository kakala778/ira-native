"""Source, evidence and locator contracts for TASK-001."""

from datetime import date
from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, JsonValue

BBox = tuple[float, float, float, float]


class Record(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class SourceKind(StrEnum):
    PDF = "pdf"
    PARSED_JSON = "parsed_json"

    @property
    def media_type(self) -> str:
        return "application/pdf" if self == SourceKind.PDF else "application/json"


class BindingState(StrEnum):
    DECLARED = "declared"
    MANIFEST_ATTESTED = "manifest_attested"
    SPOT_CHECKED = "spot_checked"
    MISMATCH = "mismatch"
    UNKNOWN = "unknown"


class SourceState(StrEnum):
    VERIFIED_FOR_CHECKED_FIELDS = "verified_for_checked_fields"
    UNVERIFIED = "unverified"
    MISMATCH = "mismatch"
    DENIED = "denied"


class AccessPolicy(Record):
    local_read: bool = True
    external_model: Literal[False] = False


class AssetView(Record):
    asset_id: str
    sha256: str
    media_type: str
    source_kind: SourceKind
    access_policy: AccessPolicy
    size_bytes: int
    page_count: int | None = None


class SourceAsset(AssetView):
    locator_internal: str = Field(exclude=True, repr=False)


class Document(Record):
    document_id: str
    logical_title: str | None = None


class DocumentRevision(Record):
    revision_id: str
    document_id: str
    asset_id: str
    revision_label: str | None = None
    effective_from: date | None = None
    scope: JsonValue = None
    relation_evidence: list[str] = Field(default_factory=list)


class AssetDetail(AssetView):
    revisions: list[DocumentRevision] = Field(default_factory=list)


class ParseRun(Record):
    parse_run_id: str
    source_asset_id: str
    artifact_asset_id: str
    adapter_version: str
    parser_version: str | None = None
    parser_config: JsonValue = None
    binding_state: BindingState
    binding_basis: tuple[str, ...]


class Evidence(Record):
    evidence_id: str
    parse_run_id: str
    source_asset_id: str
    artifact_asset_id: str
    source_pointer: str
    page_index: int
    physical_page: int
    raw_content: JsonValue
    kind: str
    native_bbox: JsonValue = None
    coord_system: str | None = None
    continues_prev: bool | None = None
    precision: Literal["page", "block"]
    pdf_bbox: BBox | None = None
    location_basis: Literal["physical_page", "unique_pdf_text_region"]
    risk: tuple[str, ...]
    source_state: SourceState = SourceState.UNVERIFIED
    extraction_state: Literal["candidate"] = "candidate"
    content_check: Literal["not_reviewed"] = "not_reviewed"
    access_policy: AccessPolicy


class IngestResult(Record):
    source: AssetView
    artifact: AssetView
    parse_run: ParseRun
    evidence_count: int


class SearchResult(Record):
    items: list[Evidence]
    backend: Literal["fts5", "like", "exact"]
    code: Literal["no_evidence_found"] | None = None


class PdfPageLocation(Record):
    asset_id: str
    page_index: int
    physical_page: int
    page_count: int
    page_box: BBox
    width: float
    height: float
    rotation: int
    precision: Literal["page", "block"] = "page"
    pdf_bbox: BBox | None = None
    evidence_id: str | None = None
    risk: tuple[str, ...] = ()
