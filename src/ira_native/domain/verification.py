"""Versioned local result contract for the existing verification workflow."""

from typing import Literal

from pydantic import BaseModel, JsonValue, model_validator

from ira_native.domain.models import DocumentRevision, Evidence
from ira_native.domain.values import ValueState


class FieldSource(BaseModel):
    field_name: str | None = None
    evidence_id: str | None = None
    asset_id: str | None = None
    physical_page: int | None = None
    source_pointer: str | None = None
    native_pointer: str | None = None
    row_in_block: int | None = None
    row_id: int | str | None = None
    sheet: str | None = None
    column: str | None = None
    column_index: int | None = None
    native_bbox: JsonValue = None
    native_coordinate_system: str | None = None
    raw_heading: str | None = None
    raw_span: JsonValue = None
    raw_start: int | None = None
    raw_end: int | None = None
    support_state: str | None = None
    header_evidence: "FieldSource | None" = None


class ObservedField(BaseModel):
    state: ValueState | Literal["parse_failed"]
    raw: JsonValue = None
    normalized: str | None = None
    semantic_basis: str | None = None
    sources: list[FieldSource]


class Measurement(BaseModel):
    state: ValueState | Literal["parse_failed"]
    expression: str | None
    operator: str | None = None
    value: str | None = None
    lower: str | None = None
    upper: str | None = None
    lower_inclusive: bool | None = None
    upper_inclusive: bool | None = None
    unit: str | None = None


class Fact(BaseModel):
    identity: str
    record_id: str
    side: Literal["requirement", "declaration"]
    asset_id: str | None = None
    parse_run_id: str | None = None
    artifact_asset_id: str | None = None
    evidence_id: str | None = None
    physical_page: int | None = None
    source_pointer: str | None = None
    row: int | str | None = None
    sheet: str | None = None
    attribute: str
    attribute_normalization: dict | None = None
    equipment_class: str | None = None
    model: str | None = None
    component: str | None = None
    measurement: Measurement
    fields: dict[str, ObservedField]


class SourceAssociation(BaseModel):
    status: Literal["machine_linked", "unresolved", "conflict", "not_assessed"]
    basis: list[str] = []
    requirement_fields: list[FieldSource] = []
    declaration_fields: list[FieldSource] = []
    evidence_ids: list[str] = []
    annotation_state: Literal["not_present", "unverified", "descriptive_only"] = "not_present"


class ConditionalResult(BaseModel):
    status: Literal["satisfied", "violated", "not_applicable", "undetermined"]
    reason_code: str
    assumptions: list[str] = []
    comparison_state: Literal["compared", "not_applicable", "not_comparable", "unresolved"] = (
        "unresolved"
    )

    @model_validator(mode="after")
    def derive_comparison_state(self):
        if self.status in ("satisfied", "violated"):
            self.comparison_state = "compared"
        elif self.status == "not_applicable":
            self.comparison_state = "not_applicable"
        elif self.reason_code in (
            "COUNT_BASIS_NOT_COMPARABLE",
            "COUNT_UNIT_SEMANTICS_UNKNOWN",
            "UNIT_KIND_MISMATCH",
            "CONDITION_UNPROVEN",
            "MODEL_BINDING_CONFLICT",
        ):
            self.comparison_state = "not_comparable"
        return self


class HumanConfirmation(BaseModel):
    required: bool = True
    state: Literal["not_performed", "incomplete", "confirmed", "stale"]
    snapshot_id: str | None = None
    review_ids: list[str] = []


class FindingResult(BaseModel):
    finding_id: str
    requirement_id: str
    declaration_id: str | None = None
    declaration_fact_id: str | None = None
    binding_state: Literal["unconfirmed", "machine_candidate", "confirmed", "conflict"]
    association: SourceAssociation
    conditional: ConditionalResult | None = None
    formal_status: str
    formal_disposition: str
    historical_formal_status: str | None = None
    historical_disposition: str | None = None
    revision_state: (
        Literal["unresolved", "superseded", "effective_replacement", "no_competing_requirement"]
        | None
    ) = None
    applicability: Literal["applicable", "not_applicable", "unknown"]
    human_confirmation: HumanConfirmation
    blockers: list[str] = []


class TaskObservation(BaseModel):
    schema_version: Literal["ira.industrial-verification/v1"] = "ira.industrial-verification/v1"
    run_id: str
    status: Literal["observed", "recorded", "no_evidence_found", "stale"]
    ruleset_version: str
    facts: list[Fact]
    findings: list[FindingResult]
    # Exact, scoped working inputs for local consumers and evidence-locator adapters.
    requirements: list[dict] = []
    declarations: list[dict] = []
    candidate_edges: list[dict] = []
    evidence: list[Evidence] = []
    source_versions: dict[str, list[DocumentRevision]] = {}
    blockers: list[str] = []
    task_scope: dict | None = None
    version_relations: list[dict] = []
    host_compilation: dict | None = None
