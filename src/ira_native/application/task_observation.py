"""Read only the registered evidence and declaration rows in an explicit task view."""

from collections import defaultdict
from pathlib import Path
from typing import TYPE_CHECKING
from uuid import uuid4

from ira_native.application.candidates import extract_requirements_from_evidence
from ira_native.application.results import (
    declaration_fact_id,
    fact_for,
    machine_binding,
    machine_calculation,
    paired_declaration,
    source_association,
)
from ira_native.application.service import _identity
from ira_native.domain.candidate_matching import generate_candidates
from ira_native.domain.rules import PART_RULESET_VERSION, RULESET_VERSION
from ira_native.domain.verification import FindingResult, HumanConfirmation, TaskObservation
from ira_native.errors import IraError

if TYPE_CHECKING:
    from ira_native.application.workflow import VerificationWorkflow


def _validate_selection(selection: tuple[int, ...]) -> None:
    if (
        not selection
        or len(selection) > 5000
        or any(type(item) is not int or item < 1 for item in selection)
        or len(set(selection)) != len(selection)
    ):
        raise IraError("invalid_input")


def _revision_keys(item: dict):
    if item.get("requirement_slot"):
        yield item["property"], "slot", item["requirement_slot"]
    if item.get("model") and item.get("scope"):
        yield item["property"], "subject", item["model"], item["scope"]


def _revision_conflicts(left: dict, right: dict) -> bool:
    # Separate explicit task locations. Missing qualifiers in a notice cannot
    # establish that it applies or that the original remains effective.
    dimensions = ("scope", "site_group", "equipment_class", "requirement_slot")
    if any(left.get(key) and right.get(key) and left[key] != right[key] for key in dimensions):
        return False
    same_slot = left.get("requirement_slot") and left["requirement_slot"] == right.get(
        "requirement_slot"
    )
    if not same_slot and (
        left.get("part_number")
        and right.get("part_number")
        and left["part_number"] != right["part_number"]
    ):
        return False
    semantics = (
        "model",
        "config_variant",
        "part_number",
        "source_revision",
        "expr",
        "unit",
        "scope",
        "site_group",
        "condition",
        "count_basis",
        "strength",
        "quantity_role",
        "inclusion_status",
        "applicability",
        "presence",
    )
    return any(left.get(key) != right.get(key) for key in semantics)


def authorize_task_inputs(
    workflow: "VerificationWorkflow",
    *,
    sources: dict[str, tuple[int, ...]],
    declarations: dict[str, tuple[int, ...]],
) -> tuple[dict, dict]:
    if not sources or len(sources) > 32 or len(declarations) > 32:
        raise IraError("invalid_input")
    service = workflow.evidence
    # Verify every input authority before loading any scoped payload.
    runs = {}
    for parse_id, pages in sources.items():
        _validate_selection(pages)
        run = service.get_run(parse_id)
        total = service.get_asset(run.source_asset_id).page_count or 0
        if max(pages) > total:
            raise IraError("invalid_location")
        runs[parse_id] = run
    sheets = {}
    for import_id, rows in declarations.items():
        _validate_selection(rows)
        sheet = workflow.store.sheet_scope_metadata(import_id)
        if not sheet["access_policy"]["local_read"]:
            raise IraError("permission_denied")
        if sheet["locator_internal"]:
            service.files.authorize(Path(sheet["locator_internal"]))
        if not workflow.store.sheet_has_rows(import_id, rows):
            raise IraError("permission_denied")
        sheets[import_id] = sheet
    return runs, sheets


def read_scoped_evidence(service, sources, runs):
    evidence = []
    for parse_id, pages in sources.items():
        run = runs[parse_id]
        for item in service.list_run_evidence(parse_id, pages=pages):
            if (
                item.parse_run_id != parse_id
                or item.source_asset_id != run.source_asset_id
                or item.artifact_asset_id != run.artifact_asset_id
                or item.physical_page not in pages
            ):
                raise IraError("permission_denied")
            evidence.append(item)
    return evidence


def read_task_inputs(workflow, sources, declarations):
    runs, sheets = authorize_task_inputs(workflow, sources=sources, declarations=declarations)
    evidence = read_scoped_evidence(workflow.evidence, sources, runs)
    selected_rows = []
    for import_id, rows in declarations.items():
        selected = workflow.store.scoped_sheet_rows(import_id, rows)
        if {item["row_id"] for item in selected} != set(rows) or any(
            item["source_asset_id"] != sheets[import_id]["asset_id"] for item in selected
        ):
            raise IraError("permission_denied")
        selected_rows.extend(item | {"declaration_import_id": import_id} for item in selected)

    index = {item.evidence_id: item for item in evidence}
    requirements = [
        item
        | {
            "candidate_id": _identity(item["evidence_id"], str(item["row_in_block"])),
            "parse_run_id": index[item["evidence_id"]].parse_run_id,
            "artifact_asset_id": index[item["evidence_id"]].artifact_asset_id,
        }
        for item in extract_requirements_from_evidence(evidence, allow_explicit_classes=True)
    ]
    return runs, sheets, evidence, requirements, selected_rows


def observe_task(
    workflow: "VerificationWorkflow",
    *,
    sources: dict[str, tuple[int, ...]],
    declarations: dict[str, tuple[int, ...]],
) -> TaskObservation:
    runs, _, evidence, requirements, selected_rows = read_task_inputs(
        workflow, sources, declarations
    )
    service = workflow.evidence
    index = {item.evidence_id: item for item in evidence}
    edges = generate_candidates(requirements, selected_rows)
    statements = defaultdict(list)
    for item in requirements:
        for key in _revision_keys(item):
            statements[key].append(item)
    source_versions = {
        run.parse_run_id: [
            revision.model_dump(mode="json")
            | {"relation_evidence": [ref for ref in revision.relation_evidence if ref in index]}
            for revision in service.get_asset(run.source_asset_id).revisions
        ]
        for run in runs.values()
    }

    candidates = defaultdict(list)
    for edge in edges:
        candidates[edge["left_ref"]].append(edge)
    rows_by_id = {row["item_id"]: row for row in selected_rows}
    run_id = uuid4().hex
    findings = []
    paired_facts = {}
    for item in requirements:
        candidate_id = item["candidate_id"]
        matching = [
            edge
            for edge in candidates[candidate_id]
            if machine_binding(edge, item, rows_by_id[edge["right_ref"]])
        ]
        edge = matching[0] if len(matching) == 1 else None
        if item.get("applicability") == "not_applicable":
            edge = None
        decl = paired_declaration(item, rows_by_id[edge["right_ref"]], edge) if edge else None
        if decl is not None:
            fact = fact_for(decl, paired=True)
            paired_facts[fact.identity] = fact
        located = index[item["evidence_id"]]
        association = source_association(
            item,
            decl,
            edge,
            located,
            binding_mismatch=runs[located.parse_run_id].binding_state == "mismatch",
        )
        competing_sources = any(
            index[other["evidence_id"]].parse_run_id != located.parse_run_id
            and _revision_conflicts(item, other)
            for key in _revision_keys(item)
            for other in statements[key]
        )
        # Registered version metadata is shown. Ordering or labels alone do not
        # prove an effective version for the comparison.
        version_count = len(
            {revision["revision_id"] for revision in source_versions[located.parse_run_id]}
        )
        calculated = machine_calculation(
            item, decl, edge, association, competing_sources or version_count > 1
        )
        blockers = []
        if calculated.status == "undetermined":
            blockers.append(calculated.reason_code)
        if len(matching) > 1:
            blockers.append("MULTIPLE_COMPATIBLE_DECLARATIONS")
        blockers.append("HUMAN_CONFIRMATION_REQUIRED")
        findings.append(
            FindingResult(
                finding_id=_identity(run_id, candidate_id),
                requirement_id=candidate_id,
                declaration_id=decl["item_id"] if decl else None,
                declaration_fact_id=declaration_fact_id(decl) if decl else None,
                binding_state="machine_candidate" if edge else "unconfirmed",
                association=association,
                conditional=calculated,
                formal_status="undetermined",
                formal_disposition="review_required",
                applicability="not_applicable"
                if calculated.status == "not_applicable"
                else "unknown",
                human_confirmation=HumanConfirmation(state="not_performed"),
                blockers=blockers,
            )
        )
    return TaskObservation(
        run_id=run_id,
        status="observed" if requirements else "no_evidence_found",
        ruleset_version=PART_RULESET_VERSION
        if any(item.get("part_number") for item in requirements + selected_rows)
        else RULESET_VERSION,
        facts=[fact_for(item, index[item["evidence_id"]]) for item in requirements]
        + [fact_for(row) for row in selected_rows]
        + list(paired_facts.values()),
        findings=findings,
        requirements=requirements,
        declarations=selected_rows,
        candidate_edges=edges,
        evidence=evidence,
        source_versions=source_versions,
        blockers=[] if requirements else ["NO_REQUIREMENT_OBSERVED"],
    )
