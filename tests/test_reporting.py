import json

from ira_native.application.reporting import summarize_run


def test_summary_projects_history_and_omits_sensitive_payloads():
    pdf = "a" * 64
    evidence = "b" * 64
    snapshot = "c" * 64
    original = {
        "run_id": "a" * 32,
        "draft_id": "d" * 64,
        "parse_run_id": "e" * 64,
        "status": "stale",
        "stale_reason": "verification_stale",
        "ruleset_version": "gp/1",
        "field_verification_state": "not_verified",
        "reviewer": "SYNTH_REVIEWER_ONLY",
        "findings": [
            {
                "finding_id": "f" * 64,
                "disposition": "no_issue",
                "formal_status": "undetermined",
                "historical_formal_status": "satisfied",
                "conditional_calculation": {"status": "satisfied", "reason_code": "MATCH"},
                "requirement": {
                    "source_asset_id": pdf,
                    "physical_page": 2,
                    "evidence_id": evidence,
                    "equipment_class": "OLT",
                    "raw": {"file": "SYNTH_PRIVATE_EXCERPT"},
                },
                "declaration": {
                    "source_asset_id": "1" * 64,
                    "row_id": 3,
                    "original_cells": ["SYNTH_PRIVATE_CELL"],
                },
                "evaluation": {"reason_code": "OK"},
                "verification_snapshot": {
                    "snapshot_id": snapshot,
                    "review_ids": ["example"],
                    "inputs": {"note": "SYNTH_NOTE_ONLY"},
                },
            }
        ],
        "agent_history": [
            {
                "task_id": "b" * 32,
                "task_state": "completed",
                "goal": "SYNTH_GOAL_ONLY",
                "evidence_ids": [evidence, "SYNTH_FOREIGN_VALUE"],
                "attempts": [{"query": "SYNTH_QUERY_ONLY"}],
            }
        ],
    }
    report = summarize_run(original)
    (item,) = report["findings"]
    assert item["current_disposition"] == "review_required"
    assert item["historical_disposition"] == "no_issue"
    assert item["source"]["physical_page"] == 2
    assert item["snapshot"] == {"id": snapshot, "review_count": 1}
    assert report["agent_tasks"][0]["evidence_ids"] == [evidence]
    assert "SYNTH_" not in json.dumps(report)
