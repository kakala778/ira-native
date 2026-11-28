"""Build auditable observations from the existing candidates and rule engine."""

import re
from decimal import Decimal

from ira_native.application.service import _identity
from ira_native.domain.candidate_matching import pair_field
from ira_native.domain.models import Evidence
from ira_native.domain.rules import POINT, RANGE, calculate_conditional
from ira_native.domain.values import ValueState
from ira_native.domain.verification import (
    ConditionalResult,
    Fact,
    FieldSource,
    FindingResult,
    HumanConfirmation,
    Measurement,
    ObservedField,
    SourceAssociation,
)

_TITLE = re.compile(
    r"^(?:表\s*[0-9]+(?:-[0-9]+)?|表\s+[A-Z][A-Z0-9-]*[0-9])\s+[^\n。；：:=<>≤≥]{1,100}$"
)
_CONDITION_WORDS = ("除外", "仅", "条件", "不适用", "不含", "修订", "替代", "有效", "以内", "以外")
_SOURCE_RISKS = {"continuation_unverified", "table_recovery_unverified", "source_mismatch"}


def _measurement(item: dict) -> Measurement:
    expr, unit = item.get("expr"), item.get("unit")
    if item.get("applicability") == "not_applicable":
        return Measurement(
            state="unknown" if expr else "not_applicable", expression=expr, unit=unit
        )
    if item.get("presence") == "explicit_absence":
        return Measurement(
            state="unknown" if expr else "explicit_absence", expression=expr, unit=unit
        )
    if point := POINT.fullmatch(expr or ""):
        op, value = point.groups()
        op = {None: "=", "≤": "<=", "≥": ">="}.get(op, op)
        return Measurement(
            state="present",
            expression=expr,
            unit=unit,
            operator=op,
            value=str(Decimal(value)),
            lower=value if op in ("=", ">", ">=") else None,
            upper=value if op in ("=", "<", "<=") else None,
            lower_inclusive=op != ">" if op in ("=", ">", ">=") else None,
            upper_inclusive=op != "<" if op in ("=", "<", "<=") else None,
        )
    if span := RANGE.fullmatch(expr or ""):
        start, lower, upper, end = span.groups()
        return Measurement(
            state="present",
            expression=expr,
            unit=unit,
            operator="range",
            lower=lower,
            upper=upper,
            lower_inclusive=start == "[",
            upper_inclusive=end == "]",
        )
    if expr and (item.get("configuration") or expr.startswith("enum:")):
        return Measurement(state="present", expression=expr, unit=unit)
    fields = item.get("field_values", {})
    state = (
        "parse_failed"
        if expr
        else "unknown"
        if any(
            fields.get(name, {}).get("state") == "unknown" for name in ("value", "quantity", "expr")
        )
        else "not_stated"
    )
    return Measurement(state=state, expression=expr, unit=unit)


def declaration_fact_id(row: dict) -> str:
    return _identity("paired_declaration_fact", row["item_id"], row["property"])


def fact_for(item: dict, evidence: Evidence | None = None, *, paired: bool = False) -> Fact:
    requirement = "candidate_id" in item
    if not requirement and not paired:
        item = paired_declaration(item, item, {"field_pairing": pair_field(item, item)})
    fields = {}
    provenance = item.get("field_evidence", [])
    for name, value in item.get("field_values", {}).items():
        derived = item.get("field_derivations", {}).get(name, [])
        if name == "quantity_role" and derived and value["state"] == ValueState.NOT_STATED:
            value = value | {
                "normalized": item["quantity_role"],
                "state": "present",
                "semantic_basis": "explicit_quantity_constraint",
            }
        fields[name] = ObservedField(
            **value,
            sources=[
                FieldSource.model_validate(pos)
                for pos in provenance
                if pos.get("field_name") == name or pos.get("field_name") in derived
            ],
        )
    for name, value in (
        ("part_number", item.get("part_number")),
        ("applicability", item.get("applicability")),
        ("presence", item.get("presence")),
    ):
        if (
            value
            and value != "unknown"
            and (name not in fields or fields[name].state == ValueState.NOT_STATED)
        ):
            fields[name] = ObservedField(
                state=(
                    "not_applicable"
                    if name == "applicability"
                    else "explicit_absence"
                    if name == "presence"
                    else "present"
                ),
                normalized=value,
                semantic_basis="explicit_source_observation",
                sources=[
                    FieldSource.model_validate(pos)
                    for pos in provenance
                    if pos.get("field_name") == name
                ],
            )
    record_id = item["candidate_id"] if requirement else item["item_id"]
    return Fact(
        identity=declaration_fact_id(item) if paired else record_id,
        record_id=record_id,
        side="requirement" if requirement else "declaration",
        asset_id=item.get("source_asset_id"),
        parse_run_id=evidence.parse_run_id if evidence else item.get("parse_run_id"),
        artifact_asset_id=evidence.artifact_asset_id if evidence else item.get("artifact_asset_id"),
        evidence_id=item.get("evidence_id") if requirement else record_id,
        physical_page=item.get("physical_page"),
        source_pointer=item.get("source_pointer"),
        row=item.get("row_in_block") if requirement else item.get("row_id"),
        sheet=item.get("sheet_or_section"),
        equipment_class=item.get("equipment_class"),
        model=item.get("model"),
        component=item.get("part_number"),
        attribute=item["property"],
        attribute_normalization=item.get("attribute_normalization"),
        measurement=_measurement(item),
        fields=fields,
    )


def paired_declaration(requirement: dict, declaration: dict, edge: dict) -> dict:
    pairing = edge["field_pairing"]
    value = pairing["declaration"]
    expression = value["value"]["normalized"] if value else None
    if expression is not None and pairing["field"] != "interface_config":
        expression = "=" + expression
    return declaration | {
        "property": requirement["property"],
        "expr": expression,
        "unit": value["unit"] if value else None,
        "count_basis": (value["count_basis"] if value else None) or "unknown",
    }


def _annotation_state(evidence: Evidence) -> str:
    if not evidence.annotations:
        return "not_present"
    captions = evidence.annotations.get("captions")
    if (
        evidence.kind == "table"
        and not evidence.annotations.get("footnotes")
        and isinstance(captions, list)
        and all(
            isinstance(item, dict)
            and isinstance(title := item.get("content"), str)
            and _TITLE.fullmatch(title)
            and not any(word in title for word in _CONDITION_WORDS)
            for item in captions
        )
    ):
        # This classifies the visible label only; it makes no assertion about ownership.
        return "descriptive_only"
    return "unverified"


def source_association(
    requirement: dict,
    declaration: dict | None,
    edge: dict | None,
    evidence: Evidence,
    *,
    binding_mismatch: bool = False,
) -> SourceAssociation:
    annotation = _annotation_state(evidence)
    if binding_mismatch or evidence.source_state in ("mismatch", "denied"):
        return SourceAssociation(status="conflict", basis=["SOURCE_MISMATCH"])
    if requirement.get("expr") and (
        requirement.get("applicability") == "not_applicable"
        or requirement.get("presence") == "explicit_absence"
    ):
        return SourceAssociation(status="conflict", basis=["CONFLICTING_SOURCE_STATEMENTS"])
    origins = requirement.get("field_derivations", {}).get("expr", [])
    if requirement.get("applicability") == "not_applicable":
        origins = requirement.get("field_derivations", {}).get("applicability", ["applicability"])
    explicit_absence = requirement.get("presence") == "explicit_absence"
    if explicit_absence:
        origins = requirement.get("field_derivations", {}).get("presence", ["presence"])
    req_fields = [
        FieldSource.model_validate(item)
        for item in requirement["field_evidence"]
        if item.get("field_name") in origins
        and item["evidence_id"] == evidence.evidence_id
        and item.get("source_pointer") == evidence.source_pointer
        and item.get("physical_page") == evidence.physical_page
        and item.get("support_state") != "insufficient"
    ]
    decl_fields = []
    declaration_value_supported = False
    if declaration and edge and (paired := edge["field_pairing"]["declaration"]):
        decl_fields = [
            FieldSource.model_validate(item)
            for item in declaration["field_evidence"]
            if item.get("field_name")
            in (paired["source_field"], paired.get("unit_source_field", "unit"))
            and item.get("row_id") == declaration["row_id"]
            and item.get("asset_id") == declaration["source_asset_id"]
            and not item.get("cell_type")
            and item.get("support_state") != "insufficient"
        ]
        declaration_value_supported = any(
            item.field_name == paired["source_field"] for item in decl_fields
        )
    blockers = []
    if requirement["source_asset_id"] != evidence.source_asset_id:
        blockers.append("SOURCE_MISMATCH")
    risks = _SOURCE_RISKS.intersection(requirement["risk"])
    inherited = bool(req_fields) and all(field.header_evidence for field in req_fields)
    if inherited:
        risks.discard("continuation_unverified")
    if risks or annotation == "unverified":
        blockers.append("CONDITIONAL_SOURCE_RELATION_UNRESOLVED")
    if (
        not origins
        or len(req_fields) != len(set(origins))
        or (declaration and not declaration_value_supported)
    ):
        blockers.append("FIELD_SUPPORT_UNKNOWN")
    if (
        declaration is None
        and requirement.get("applicability") != "not_applicable"
        and not explicit_absence
    ):
        blockers.append("DECLARATION_NOT_LINKED")
    return SourceAssociation(
        status="unresolved" if blockers else "machine_linked",
        basis=blockers
        or ["registered_evidence_identity", "traceable_body_fields"]
        + (["explicit_in_scope_continuation_header"] if inherited else [])
        + (
            ["mapped_declaration_row"]
            if declaration
            else ["explicit_source_absence"]
            if explicit_absence
            else ["explicit_source_applicability"]
        ),
        requirement_fields=req_fields,
        declaration_fields=decl_fields,
        evidence_ids=list(
            dict.fromkeys(
                [
                    evidence.evidence_id,
                    *(
                        field.header_evidence.evidence_id
                        for field in req_fields
                        if field.header_evidence and field.header_evidence.evidence_id
                    ),
                    *([declaration["item_id"]] if declaration else []),
                ]
            )
        ),
        annotation_state=annotation,
    )


def machine_binding(edge: dict, requirement: dict, declaration: dict) -> bool:
    dims = edge["dimensions"]
    return (
        all(dims[key] == "same" for key in ("equipment_class", "scope", "configuration_variant"))
        and (dims["manufacturer_model"] == "same" or dims["requirement_slot"] == "same")
        and "conflict" not in dims.values()
        and "model_conflict" not in edge["confirmation_basis"]
        and requirement.get("part_number") == declaration.get("part_number")
    )


def machine_calculation(
    requirement: dict,
    declaration: dict | None,
    edge: dict | None,
    association: SourceAssociation,
    revision_conflict: bool,
) -> ConditionalResult:
    reason = None
    if association.status == "conflict":
        reason = association.basis[0]
    elif revision_conflict:
        reason = "REVISION_RELATION_UNRESOLVED"
    elif (
        requirement.get("applicability") == "not_applicable"
        and association.status == "machine_linked"
    ):
        return ConditionalResult(
            status="not_applicable", reason_code="EXPLICIT_SOURCE_NON_APPLICABILITY"
        )
    elif edge is None or declaration is None:
        reason = "CONDITIONAL_PAIR_NOT_SELECTED"
    elif association.status != "machine_linked":
        reason = association.basis[0]
    elif edge["applicability_blockers"] or (
        blockers := [x for x in edge["comparison_blockers"] if x != "unit_requires_comparison"]
    ):
        reason = (edge["applicability_blockers"] or blockers)[0].upper()
    elif (
        not requirement["source_revision"]
        or requirement["source_revision"] != declaration["source_revision"]
    ):
        reason = "REVISION_UNKNOWN"
    elif any(
        item["field_values"]["condition"]["state"] == ValueState.UNKNOWN
        for item in (requirement, declaration)
    ):
        reason = "CONDITION_UNKNOWN"
    elif requirement["strength"] != "mandatory":
        reason = "REQUIREMENT_FORCE_UNRESOLVED_OR_ADVISORY"
    elif requirement["property"] == "device_quantity" and requirement["quantity_role"] not in (
        "exact",
        "minimum",
    ):
        reason = "QUANTITY_ROLE_UNKNOWN"
    elif requirement["inclusion_status"] in ("excluded", "reference_out_of_scope"):
        reason = "REQUIREMENT_OUT_OF_SCOPE"
    if reason:
        return ConditionalResult(status="undetermined", reason_code=reason)
    return ConditionalResult(
        **calculate_conditional(requirement, declaration),
        assumptions=[
            "explicit_field_identity",
            "matching_stated_revision",
            "applicable_requirement",
            "extracted_values_as_stated",
        ],
    )


def persisted_finding(finding: dict, *, stale: bool) -> FindingResult:
    req, decl = finding["requirement"], finding.get("declaration") or {}
    snapshot = finding.get("verification_snapshot") or {}
    gates = finding.get("trust_gates") or {}
    review_ids = snapshot.get("review_ids", [])
    formal = finding.get("formal_status", "undetermined")
    trusted = bool(snapshot and not gates.get("blockers") and formal != "undetermined")
    association = finding.get("source_association") or {"status": "not_assessed"}
    human = HumanConfirmation(
        state="stale"
        if stale
        else "confirmed"
        if trusted
        else "incomplete"
        if review_ids
        else "not_performed",
        snapshot_id=snapshot.get("snapshot_id"),
        review_ids=review_ids,
    )
    return FindingResult(
        finding_id=finding["finding_id"],
        requirement_id=req["candidate_id"],
        declaration_id=decl.get("item_id"),
        declaration_fact_id=declaration_fact_id(decl) if decl else None,
        binding_state="confirmed" if trusted and not stale else "unconfirmed",
        association=SourceAssociation.model_validate(association),
        conditional=finding.get("conditional_calculation") if not stale else None,
        formal_status="undetermined" if stale else formal,
        formal_disposition="review_required" if stale else finding["disposition"],
        historical_formal_status=finding.get("historical_formal_status") if stale else None,
        historical_disposition=finding["disposition"] if stale else None,
        revision_state=finding.get("revision_state"),
        applicability="unknown" if stale else finding["evaluation"]["applicability"],
        human_confirmation=human,
        blockers=gates.get("blockers")
        or ([] if trusted else [finding["evaluation"]["reason_code"]]),
    )
