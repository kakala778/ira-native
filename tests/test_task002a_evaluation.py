"""Contract tests for the evaluation harness, using independently specified answers."""

import pytest
from evaluation.task002a_inputs import UnsupportedInput, materialize
from evaluation.task002a_scoring import evaluate_case


def source_case():
    return {
        "case_id": "fixture",
        "data_kind": "synthetic_public",
        "requirement_source": {
            "source_id": "spec",
            "sections": [],
            "tables": [
                {
                    "table_id": "t",
                    "page_index": 0,
                    "physical_page": 1,
                    "columns": ["设备类别", "配置变体", "工程范围", "数量", "约束表达式"],
                    "rows": [
                        {
                            "row_id": "a",
                            "cells": {
                                "设备类别": "OLT",
                                "配置变体": "v1",
                                "工程范围": "demo",
                                "数量": "4",
                                "约束表达式": "=4",
                            },
                        }
                    ],
                }
            ],
        },
        "declaration_source": {
            "source_id": "list",
            "file_name": "test.csv",
            "sheets": [
                {
                    "sheet_or_section": "list",
                    "header": [
                        "设备类别",
                        "配置变体",
                        "工程范围",
                        "数量",
                    ],
                    "rows": [
                        {
                            "row_id": 2,
                            "cells": {
                                "设备类别": "OLT",
                                "配置变体": "v1",
                                "工程范围": "demo",
                                "数量": "3",
                            },
                        }
                    ],
                }
            ],
        },
    }


def gold_case():
    return {
        "expected_extraction": {
            "requirements": [
                {
                    "local_ref": "r",
                    "equipment_class": "OLT",
                    "scope": "demo",
                    "field_evidence": [
                        {
                            "field_name": "equipment_class",
                            "quote": "OLT",
                            "locator": {
                                "source_id": "spec",
                                "physical_page": 1,
                                "table_id": "t",
                                "row_id": "a",
                                "column": "设备类别",
                            },
                        }
                    ],
                }
            ],
            "declarations": [
                {
                    "local_ref": "d",
                    "equipment_class": "OLT",
                    "quantity_raw": "3",
                    "field_evidence": [
                        {
                            "field_name": "equipment_class",
                            "quote": "OLT",
                            "locator": {
                                "source_id": "list",
                                "sheet_or_section": "list",
                                "row_id": 2,
                                "column": "设备类别",
                            },
                        }
                    ],
                }
            ],
        },
        "entity_resolution": {
            "candidates": [
                {"left_ref": "r", "right_ref": "d", "match_label": "candidate_same_slot"},
            ]
        },
        "workflow_expectation": {"expected_stage": "review_required"},
    }


def test_real_workflow_scored_without_using_answers_to_materialize_inputs(tmp_path):
    case, expected = source_case(), gold_case()
    expected["expected_extraction"]["requirements"][0]["scope"] = "independent negative answer"
    report = evaluate_case(case, expected, tmp_path / "test.sqlite")
    assert report["pipeline"]["status"] == "complete"
    assert report["fields"]["match"] == 3
    assert report["fields"]["mismatch"] == 1
    assert report["field_evidence"]["match"] == 2
    assert report["candidate_pairs"]["matched"] == 1
    assert report["workflow_gate"]["status"] == "match"
    assert report["workflow_gate"]["claims_exercised"] == 1
    assert report["workflow_outcomes"] == {"review_required": 1}


def test_unrelated_review_finding_does_not_match_expected_stage(tmp_path):
    case, expected = source_case(), gold_case()
    expected["expected_extraction"]["requirements"][0]["field_evidence"][0]["locator"]["row_id"] = (
        "different-source-row"
    )
    report = evaluate_case(case, expected, tmp_path / "test.sqlite")
    assert report["workflow_gate"]["status"] == "match"
    assert report["workflow_gate"]["expected_stage_match"] == "mismatch"


def test_missing_declared_source_is_not_silently_recreated():
    case = source_case()
    del case["requirement_source"]["source_id"]
    with pytest.raises(UnsupportedInput, match="source_registration_missing"):
        materialize(case)


def test_condition_answers_do_not_override_workflow_expectation(tmp_path):
    case, expected = source_case(), gold_case()
    expected["conditional_evaluation"] = {"logic_result": {"finding_disposition": "issue"}}
    report = evaluate_case(case, expected, tmp_path / "test.sqlite")
    assert report["workflow_gate"]["status"] == "match"
    assert report["workflow_outcomes"] == {"review_required": 1}
    assert report["conditional"]["status"] == "unsupported"


def test_authored_conditional_scalar_runs_separately_from_workflow(tmp_path):
    case, expected = source_case(), gold_case()
    left = expected["expected_extraction"]["requirements"][0]
    right = expected["expected_extraction"]["declarations"][0]
    for item in (left, right):
        item.update(
            configuration_variant="v1", scope="demo", count_basis="per_site", source_revision="R1"
        )
    left.update(
        attribute="device_quantity",
        expression={"normalized": "=4", "unit": "台"},
        strength="mandatory",
        quantity_role="exact",
        inclusion_status="in_scope",
    )
    right["device_quantity"] = {"normalized": "3", "unit": "台"}
    expected["conditional_evaluation"] = {
        "preconditions": [
            {"id": key, "status": "supported_by_source"}
            for key in (
                "P1_FIELD_SUPPORT",
                "P2_ENTITY_BINDING",
                "P3_APPLICABILITY",
                "P4_UNIT_AND_BASIS",
                "P6_REQUIREMENT_STRENGTH",
            )
        ],
        "logic_result": {
            "kind": "requirement_evaluation",
            "finding_disposition": "issue",
            "requirement_evaluation": "violated",
        },
        "per_requirement": [{"requirement_ref": "r"}],
    }
    report = evaluate_case(case, expected, tmp_path / "test.sqlite")
    assert report["conditional"]["status"] == "match"
    assert report["conditional"]["actual"]["finding_disposition"] == "issue"
    assert report["workflow_outcomes"] == {"review_required": 1}
