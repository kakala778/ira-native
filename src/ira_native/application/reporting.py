"""Explicitly limited report projection shared by HTML and JSON."""

import re
from collections import Counter
from collections.abc import Mapping


def _token(value):
    return (
        value if isinstance(value, str) and re.fullmatch(r"[A-Za-z0-9_./-]{1,96}", value) else None
    )


def _identity(value):
    return (
        value
        if isinstance(value, str) and re.fullmatch(r"[0-9a-f]{32}|[0-9a-f]{64}", value)
        else None
    )


def _positive(value):
    return value if type(value) is int and value > 0 else None


def _codes(values):
    return [code for value in values if (code := _token(value))]


def _finding(finding, stale):
    requirement, declaration = finding["requirement"], finding.get("declaration") or {}
    evaluation = finding["evaluation"]
    calculation = finding.get("conditional_calculation") or {}
    snapshot = finding.get("verification_snapshot") or {}
    gates = finding.get("trust_gates") or {}
    reference = _identity(requirement.get("evidence_id"))
    return {
        "finding_id": _identity(finding["finding_id"]),
        "equipment_class": requirement.get("equipment_class")
        if requirement.get("equipment_class") in ("OLT", "ONU")
        else None,
        "current_disposition": "review_required" if stale else _token(finding.get("disposition")),
        "historical_disposition": _token(finding.get("disposition")) if stale else None,
        "formal_status": "undetermined"
        if stale
        else _token(finding.get("formal_status")) or "undetermined",
        "historical_formal_status": _token(finding.get("historical_formal_status"))
        if stale
        else None,
        "conditional_status": None if stale else _token(calculation.get("status")),
        "conditional_reason": None if stale else _token(calculation.get("reason_code")),
        "evaluation_reason": _token(evaluation.get("reason_code")),
        "trust_blockers": _codes(gates.get("blockers", [])),
        "unverified_fields": _codes(gates.get("unverified_fields", [])),
        "review_state": _token(finding.get("review_state")),
        "source": {
            "evidence_id": reference,
            "physical_page": _positive(requirement.get("physical_page")),
            "pdf_asset_id": _identity(requirement.get("source_asset_id")),
            "declaration_asset_id": _identity(declaration.get("source_asset_id")),
            "declaration_row": _positive(declaration.get("row_id")),
            "declaration_columns": sorted(
                {
                    pos
                    for field in declaration.get("field_evidence", [])
                    if (pos := _positive(field.get("column_index"))) is not None
                }
            ),
        },
        "snapshot": {
            "id": _identity(snapshot.get("snapshot_id")),
            "review_count": len(snapshot.get("review_ids", [])),
        },
    }


def summarize_run(run: Mapping) -> dict:
    """Keep structured dispositions and references, omitting evidence bodies and free text."""
    stale = run["status"] == "stale"
    findings = [_finding(finding, stale) for finding in run["findings"]]
    tasks = [
        {
            "task_id": _identity(task.get("task_id")),
            "state": _token(task.get("task_state")),
            "stop_reason": _token(task.get("stop_reason")),
            "evidence_ids": [
                value for item in task.get("evidence_ids", []) if (value := _identity(item))
            ],
            "tool_calls": _positive(task.get("usage", {}).get("tool_calls")) or 0,
        }
        for task in run.get("agent_history", [])
    ]
    return {
        "schema": "ira.verification-summary.v1",
        "run_id": _identity(run["run_id"]),
        "draft_id": _identity(run["draft_id"]),
        "parse_run_id": _identity(run["parse_run_id"]),
        "status": _token(run["status"]),
        "stale_reason": _token(run.get("stale_reason")),
        "ruleset": _token(run.get("ruleset_version")),
        "field_verification_state": _token(run.get("field_verification_state")),
        "counts": dict(
            Counter(item["current_disposition"] for item in findings if item["current_disposition"])
        ),
        "findings": findings,
        "agent_tasks": tasks,
    }
