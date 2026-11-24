"""Synthetic gold for the existing GPON candidate boundary and its diagnostics."""

import json

import pytest

from ira_native.application.candidates import diagnose_requirements, extract_requirements
from ira_native.domain.models import AccessPolicy, Evidence

CANONICAL = ("equipment_class", "configuration_variant", "scope")
VALUES = ("OLT", "SYNTHETIC-VARIANT", "SYNTHETIC-SCOPE")


def _evidence(content, *, number=1, kind="text", continuation=False, context=None):
    return Evidence(
        evidence_id=f"{number:064x}",
        parse_run_id="1" * 64,
        source_asset_id="2" * 64,
        artifact_asset_id="3" * 64,
        source_pointer=f"/pages/0/blocks/{number - 1}",
        page_index=0,
        physical_page=1,
        raw_content=content,
        kind=kind,
        continues_prev=continuation,
        native_context=context or {},
        precision="page",
        location_basis="physical_page",
        risk=(),
        access_policy=AccessPolicy(),
    )


def _table(headers=CANONICAL, *, header_tag="th", cells=VALUES, first_cell_attributes=""):
    heading = "".join(f"<{header_tag}>{header}</{header_tag}>" for header in headers)
    row = "".join(
        f"<td{first_cell_attributes if index == 0 else ''}>{value}</td>"
        for index, value in enumerate(cells)
    )
    return f"<table><tr>{heading}</tr><tr>{row}</tr></table>"


@pytest.mark.parametrize(
    ("kind", "raw"),
    [
        (
            "text",
            "equipment_class: OLT\nconfiguration_variant: SYNTHETIC-VARIANT\nscope: SYNTHETIC-SCOPE",
        ),
        ("table", _table()),
        ("table", _table(("设备类别", "配置变体", "工程范围"))),
    ],
)
def test_explicit_records_remain_candidates(kind, raw):
    evidence = _evidence(raw, kind=kind)
    (candidate,) = extract_requirements(evidence)
    assert (candidate["equipment_class"], candidate["config_variant"], candidate["scope"]) == VALUES
    report = diagnose_requirements(iter([evidence]))
    assert report["evidence_total"] == report["records_seen"] == report["candidates_emitted"] == 1
    assert report["records_rejected"] == 0
    assert report["evidence_index"][0]["records"][0]["outcome"] == "emitted"


@pytest.mark.parametrize(
    ("raw", "continuation", "counter", "reason"),
    [
        (_table(header_tag="td"), False, "header_mapping_failed", "TABLE_HEADER_NOT_EXPLICIT"),
        (
            _table(first_cell_attributes=' rowspan="2"'),
            False,
            "unsupported_table_count",
            "TABLE_ROWSPAN_UNSUPPORTED",
        ),
        (
            _table(first_cell_attributes=' colspan="2"'),
            False,
            "unsupported_table_count",
            "TABLE_COLSPAN_UNSUPPORTED",
        ),
        (_table() + _table(), False, "unsupported_table_count", "TABLE_MULTIPLE_HTML_TABLES"),
        (
            _table(VALUES, header_tag="td"),
            True,
            "header_mapping_failed",
            "CONTINUATION_HEADER_UNCONFIRMED",
        ),
        (
            _table(("unmapped category", "variant", "region")),
            False,
            "header_mapping_failed",
            "TABLE_HEADER_MAPPING_FAILED",
        ),
        ("| category | variant | scope |", False, "unsupported_table_count", "TABLE_NOT_HTML"),
    ],
)
def test_unsupported_table_shapes_report_why_candidate_rows_were_not_scanned(
    raw,
    continuation,
    counter,
    reason,
):
    item = _evidence(raw, kind="table", continuation=continuation)
    assert list(extract_requirements(item)) == []
    report = diagnose_requirements([item])
    assert report["tables_seen"] == report[counter] == 1
    assert report["records_seen"] == report["candidates_emitted"] == 0
    assert report["evidence_index"][0]["reason"] == reason


def test_category_and_required_fields_distinguish_rejection_and_review():
    invalid = _evidence(dict(zip(CANONICAL, ("SYNTHETIC-OTHER", *VALUES[1:]), strict=True)))
    no_class = _evidence({"scope": "SYNTHETIC-SCOPE"}, number=2)
    partial = _evidence({"equipment_class": "ONU"}, number=3)
    report = diagnose_requirements([invalid, no_class, partial])
    assert report["records_seen"] == 3
    assert report["records_rejected"] == 2
    assert report["equipment_class_rejected"] == 1
    assert report["required_fields_missing"] == 2
    assert report["candidates_emitted"] == 1
    assert list(extract_requirements(partial))[0]["scope"] is None
    assert [i["records"][0]["outcome"] for i in report["evidence_index"]] == [
        "rejected",
        "rejected",
        "emitted_review_required",
    ]


def test_unsupported_configuration_remains_uninterpreted_candidate():
    raw = dict(zip(CANONICAL, VALUES, strict=True))
    raw.update(property="interface_config", config="SYNTHETIC unsupported combination")
    item = _evidence(raw)
    (candidate,) = extract_requirements(item)
    assert candidate["configuration"] == {}
    assert candidate["expr"] == raw["config"]
    report = diagnose_requirements([item])
    assert report["expression_unsupported"] == 1
    assert report["records_rejected"] == 0
    assert report["candidates_emitted"] == 1
    assert report["evidence_index"][0]["records"][0]["outcome"] == "emitted_review_required"


def test_annotations_are_never_used_as_scope_authority_and_output_contains_no_raw_text():
    fake_scope = "SYNTHETIC-WRONG-ATTACHED-SCOPE"
    item = _evidence(
        "equipment_class: ONU\nconfiguration_variant: SYNTHETIC-VARIANT",
        context={
            "captions": [{"content": f"scope: {fake_scope}"}],
            "footnotes": [{"content": f"scope: {fake_scope}"}],
        },
    )
    (candidate,) = extract_requirements(item)
    assert candidate["scope"] is None
    assert "scope" not in {field["field_name"] for field in candidate["field_evidence"]}
    report = diagnose_requirements([item])
    assert report["evidence_index"][0]["warnings"] == ["ANNOTATION_ASSOCIATION_UNVERIFIED"]
    assert report["required_fields_missing"] == 1
    assert fake_scope not in json.dumps(report)
    assert "SYNTHETIC-VARIANT" not in json.dumps(report)


def test_stage_denominators_balance_even_with_zero_candidates():
    evidence = [
        _evidence("SYNTHETIC PARAGRAPH WITHOUT FIELDS"),
        _evidence(_table(cells=VALUES[:2]), number=2, kind="table"),
        _evidence(_table(headers=("a", "b", "c")), number=3, kind="table"),
        _evidence(_table() + _table(), number=4, kind="table"),
        _evidence("equipment_class: ONU\nequipment_class: OLT", number=5),
    ]
    report = diagnose_requirements(evidence)
    assert (
        report["evidence_total"] == sum(report["stage_counts"]["evidence_outcomes"].values()) == 5
    )
    assert report["tables_seen"] == sum(report["stage_counts"]["table_outcomes"].values()) == 3
    assert report["records_seen"] == sum(report["stage_counts"]["record_outcomes"].values()) == 2
    assert report["records_seen"] == report["records_rejected"] + report["candidates_emitted"]
    assert report["records_rejected"] == 2
    assert report["candidates_emitted"] == 0
    assert report["reason_counts"]["TABLE_ROW_WIDTH_MISMATCH"] == 1
    assert report["reason_counts"]["DUPLICATE_RECORD_FIELD"] == 1
    assert all(not list(extract_requirements(item)) for item in evidence)
