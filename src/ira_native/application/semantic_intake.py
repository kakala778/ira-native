"""Corroborate the frozen semantic proposal protocol inside the local host."""

import json
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from ira_native.application.service import _identity
from ira_native.application.task_observation import read_scoped_evidence
from ira_native.domain.attributes import normalize_attribute
from ira_native.errors import IraError


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class _Fact(_StrictModel):
    attribute: str = Field(min_length=1, max_length=128)
    state: Literal[
        "present",
        "not_stated",
        "explicit_absence",
        "not_applicable",
        "unconstrained",
        "unknown",
        "parse_failed",
    ]
    refs: list[int | str] = Field(min_length=1, max_length=16)
    side: Literal["requirement", "declaration", "observed", "unknown"] = "unknown"
    model: str | None = None
    component: str | None = None
    level: Literal["model", "part", "whole", "series", "unknown"] = "unknown"
    equipment_class: str | None = None
    configuration_variant: str | None = None
    value: str | None = None
    unit: str | None = None
    op: Literal[">=", "<=", ">", "<", "=", "range", "unknown"] | None = None
    lower: str | None = None
    upper: str | None = None
    strength: Literal["mandatory", "recommended", "informative", "unknown"] = "unknown"
    count_basis: str | None = None
    conditions: list[str] = Field(default_factory=list, max_length=8)
    scope: str | None = None
    field_refs: dict[str, list[int | str]] = Field(default_factory=dict, max_length=20)


class _Unknown(_StrictModel):
    reason: Literal[
        "not_stated",
        "ambiguous",
        "conflict",
        "insufficient_evidence",
        "condition_unresolved",
        "revision_unresolved",
        "unsupported",
    ]
    model: str | None = None
    attribute: str | None = None
    refs: list[int | str] = Field(min_length=1, max_length=16)


class _Proposal(_StrictModel):
    schema_id: Literal["ira.semantic-proposal/v1"] = Field(alias="schema")
    task_id: str
    input_hash: str
    ref_map_hash: str
    facts: list[_Fact] = Field(max_length=32)
    unknowns: list[_Unknown] = Field(max_length=32)


def task_binding(workflow, draft):
    if not (scope := draft.get("task_scope")):
        raise IraError("permission_denied")
    runs, _ = workflow._task_authority(scope)
    refs = {
        str(number): {
            "evidence_id": item.evidence_id,
            "source_asset_id": item.source_asset_id,
            "artifact_asset_id": item.artifact_asset_id,
            "parse_run_id": item.parse_run_id,
            "physical_page": item.physical_page,
            "source_pointer": item.source_pointer,
            "source_state": item.source_state.value,
            "binding_state": runs[item.parse_run_id].binding_state.value,
            "kind": item.kind,
            "quote": item.raw_content,
        }
        for number, item in enumerate(
            read_scoped_evidence(workflow.evidence, scope["sources"], runs), 1
        )
    }
    ref_hash = _identity(json.dumps(refs, ensure_ascii=False, sort_keys=True))
    input_hash = _identity(
        "semantic_binding/1",
        draft["draft_id"],
        ref_hash,
        draft["task_revision_signature"],
        json.dumps(draft["task_assets"], sort_keys=True),
    )
    return {
        "schema": "ira.semantic-proposal-refmap/v1",
        "task_id": draft["draft_id"],
        "input_hash": input_hash,
        "ref_map_hash": ref_hash,
        "refs": refs,
    }


def _proof_fields(fact, row, ref):
    # Re-derive from host-owned fields. A model reference is never field support.
    origins = row["field_derivations"].get("expr", [])
    keys = {
        "attribute": ["property"],
        "model": ["manufacturer_model"],
        "value": origins,
        "op": origins,
        "unit": ["unit"],
        "conditions": ["condition"],
        "scope": ["scope"],
        "component": ["part_number"],
        "equipment_class": ["equipment_class"],
        "configuration_variant": ["configuration_variant"],
        "strength": ["strength"],
        "count_basis": ["count_basis"],
    }
    required = {"attribute", "model", "value", "op", "unit", "scope"}
    required.update(name for name in keys if getattr(fact, name, None) not in (None, [], "unknown"))
    if set(fact.field_refs) - keys.keys() or any(
        not refs or any(str(value) != ref for value in refs) for refs in fact.field_refs.values()
    ):
        return None
    positions = {}
    for name in sorted(required | fact.field_refs.keys()):
        matches = [
            field
            for field in row["field_evidence"]
            if field["field_name"] in keys[name]
            and field["support_state"] != "insufficient"
            and field["evidence_id"] == row["evidence_id"]
            and field.get("raw_span") not in (None, "")
        ]
        if not keys[name] or {field["field_name"] for field in matches} != set(keys[name]):
            return None
        positions[name] = matches
    return positions


def _matches(fact, row):
    attribute = normalize_attribute(fact.attribute)
    if (
        attribute["blocker"]
        or attribute["identity"] != row["property"]
        or fact.model is None
        or fact.model != row["model"]
        or fact.scope is None
        or fact.scope != row["scope"]
        or fact.value is None
        or fact.value != row["numeric_value"]
        or fact.op is None
        or fact.op != row["operator"]
        or fact.unit is None
        or fact.unit != row["unit"]
        or fact.component != row.get("part_number")
        or fact.conditions != ([row["condition"]] if row["condition"] else [])
        or fact.lower is not None
        or fact.upper is not None
        or fact.level == "series"
        or (fact.level == "part" and not fact.component)
        or (fact.level == "whole" and fact.component)
        or any(
            supplied not in (None, "unknown", observed)
            for supplied, observed in (
                (fact.equipment_class, row["equipment_class"]),
                (fact.configuration_variant, row["config_variant"]),
                (fact.strength, row["strength"]),
                (fact.count_basis, row["count_basis"]),
            )
        )
    ):
        return False
    return True


def compile_proposal(workflow, draft, payload, producer):
    if not isinstance(producer, str) or not producer.strip() or len(producer) > 120:
        raise IraError("invalid_input")
    try:
        if len(json.dumps(payload, ensure_ascii=False)) > 65536:
            raise IraError("resource_limit")
        proposal = _Proposal.model_validate(payload)
    except (ValidationError, TypeError, RecursionError):
        raise IraError("invalid_input") from None
    if proposal.task_id != draft["draft_id"]:
        raise IraError("permission_denied")
    binding = task_binding(workflow, draft)
    if (proposal.input_hash, proposal.ref_map_hash) != (
        binding["input_hash"],
        binding["ref_map_hash"],
    ):
        raise IraError("verification_stale")
    refs = binding["refs"]
    all_refs = {str(ref) for item in [*proposal.facts, *proposal.unknowns] for ref in item.refs} | {
        str(ref) for fact in proposal.facts for group in fact.field_refs.values() for ref in group
    }
    if any(
        ref not in refs
        or refs[ref]["source_state"] in ("denied", "mismatch")
        or refs[ref]["binding_state"] == "mismatch"
        for ref in all_refs
    ):
        raise IraError("permission_denied")

    rows_by_id = {row["item_id"]: row for row in draft["declarations"]}
    accepted, rejected, conflicts = {}, [], []
    conflicting_ids = set()
    for index, fact in enumerate(proposal.facts):
        if fact.side != "requirement" or fact.state != "present" or len(fact.refs) != 1:
            rejected.append({"source_fact_index": index, "reason": "unsupported_fact"})
            continue
        ref = str(fact.refs[0])
        identity = refs[ref]["evidence_id"]
        source_rows = [row for row in draft["requirements"] if row["evidence_id"] == identity]
        supported = [row for row in source_rows if _matches(fact, row)]
        if len(supported) != 1:
            conflicts.append(
                {
                    "source_fact_index": index,
                    "deterministic_candidate_ids": [row["candidate_id"] for row in source_rows],
                    "reason": "conflicting_or_ambiguous_source_fact",
                }
            )
            conflicting_ids.update(row["candidate_id"] for row in source_rows)
            continue
        row = supported[0]
        positions = _proof_fields(fact, row, ref)
        left = row["candidate_id"]
        if not positions or left not in draft["machine_matches"]:
            rejected.append({"source_fact_index": index, "reason": "unproven_field_or_pair"})
            continue
        right = draft["machine_matches"][left]
        sheet_row = rows_by_id[right]
        accepted[left] = {
            "source_fact_index": index,
            "requirement_id": left,
            "declaration_id": right,
            "declaration_import_id": sheet_row["declaration_import_id"],
            "declaration_physical_row": sheet_row["row_id"],
            "evidence_ids": [identity],
            "field_evidence": positions,
            "normalized": {
                "attribute": row["property"],
                "expression": row["expr"],
                "unit": row["unit"],
            },
            "locator_verified": True,
        }
    results = [entry for left, entry in accepted.items() if left not in conflicting_ids]
    needs_review = bool(rejected or conflicts or proposal.unknowns)
    audit = {
        "schema": "ira.host-compilation/v1",
        "status": "compiled_with_gaps"
        if results and needs_review
        else "compiled"
        if results
        else "rejected",
        "task_id": draft["draft_id"],
        "input_hash": binding["input_hash"],
        "ref_map_hash": binding["ref_map_hash"],
        "method": "deterministic_source_corroboration",
        "producer": producer.strip(),
        "proposed_facts": [fact.model_dump(mode="json") for fact in proposal.facts],
        "unknowns": [item.model_dump(mode="json") for item in proposal.unknowns],
        "compiled": results,
        "rejected_candidates": rejected,
        "conflicts": conflicts,
        "semantic_review_required": needs_review,
    }
    return [
        {"requirement_id": item["requirement_id"], "declaration_id": item["declaration_id"]}
        for item in results
    ], audit
