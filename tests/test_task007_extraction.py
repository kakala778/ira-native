import json

import pytest
from fixtures.synthetic.mvp_workflow import generate

from ira_native.application.candidates import (
    diagnose_requirements,
    extract_requirements,
    extract_requirements_from_evidence,
    normalize,
)
from ira_native.application.human_review import review_fields
from ira_native.application.service import EvidenceService
from ira_native.application.workflow import VerificationWorkflow
from ira_native.domain.candidate_matching import pair_field
from ira_native.domain.models import AccessPolicy, Evidence
from ira_native.settings import Settings


def _evidence(raw, *, kind="text", page=1, block=0, structured=None, continuation=False):
    return Evidence(
        evidence_id=f"E{page}-{block}",
        parse_run_id="R",
        source_asset_id="S",
        artifact_asset_id="M",
        kind=kind,
        raw_content=raw,
        source_pointer=f"/pages/{page - 1}/blocks/{block}",
        page_index=page - 1,
        physical_page=page,
        native_context={"structured_table": structured} if structured is not None else {},
        continues_prev=continuation,
        precision="page",
        location_basis="physical_page",
        risk=(),
        access_policy=AccessPolicy(),
    )


def test_prose_numeric_fields_have_verbatim_source_spans_but_no_guessed_force():
    text = "第4.1款 OLT DEMO-7 的 A1 每台 GE 端口数应不少于 8 个。"
    (candidate,) = extract_requirements(_evidence(text))
    assert candidate["equipment_class"] == "OLT"
    assert candidate["model"] == "DEMO-7"
    assert candidate["requirement_slot"] == "A1"
    assert candidate["property"] == "ge_ports"
    assert candidate["attribute_normalization"]["raw"] == "GE_port_count"
    assert candidate["operator"] == ">="
    assert candidate["numeric_value"] == "8"
    assert candidate["count_basis"] == "per_device"
    assert candidate["unit"] == "port"
    assert candidate["strength"] == "unknown"
    assert candidate["field_values"]["expr"]["normalized"] is None
    for field in candidate["field_evidence"]:
        start, end = field["raw_start"], field["raw_end"]
        assert text[start:end] == field["raw_span"]
        assert field["support_state"] == "candidate"


@pytest.mark.parametrize(
    "text",
    [
        "第4.1款 OLT DEMO-7 的 A1 每台 GE 端口数应不少于 8 W。",
        "OLT 的 GE 端口数不少于 8 个（仅供示例）。",
        "B2 光纤接口要求：不适用；其他项目不设置光纤接口。",
        "B2 光纤接口要求：不适用；本项目不设置电源模块。",
    ],
)
def test_mixed_or_unsupported_prose_is_not_interpreted(text):
    assert list(extract_requirements(_evidence(text), allow_explicit_classes=True)) == []


def test_source_only_applicability_and_negation_are_distinct_machine_facts():
    raw = "B2 光纤接口要求：不适用；本项目不设置任何光纤接口要求。"
    item = _evidence(raw)
    assert list(extract_requirements(item)) == []
    (candidate,) = extract_requirements(item, allow_explicit_classes=True)
    assert candidate["equipment_class"] is None
    assert candidate["requirement_slot"] == "B2"
    assert candidate["property"] == "fiber_interface"
    assert candidate["applicability"] == "not_applicable"
    assert candidate["presence"] == "explicit_absence"
    assert candidate["inclusion_status"] == "unknown"
    assert {f["raw_span"] for f in candidate["field_evidence"]} >= {"不适用", "不设置"}
    (negated,) = extract_requirements(_evidence("ONU 的 C1 不配置电源模块。"))
    assert negated["presence"] == "explicit_absence"
    assert negated["applicability"] == "unknown"
    assert negated["numeric_value"] is None


def test_model_only_applicability_and_note_only_unknown_preserve_their_scope():
    (candidate,) = extract_requirements(
        _evidence("第6.1款 DEMO-7 的 A3 光纤接口不适用；本项目不设置光纤接口。"),
        allow_explicit_classes=True,
    )
    assert candidate["equipment_class"] is None
    assert candidate["model"] == "DEMO-7"
    assert candidate["requirement_slot"] == "A3"
    assert candidate["applicability"] == "not_applicable"
    assert candidate["field_derivations"]["applicability"] == ["applicability"]
    (note,) = extract_requirements(
        _evidence("表注：D7的数值、单位和计数口径原文以破折号表示，未给出确定值。"),
        allow_explicit_classes=True,
    )
    assert note["requirement_slot"] == "D7"
    assert note["property"] == "unknown"
    assert note["presence"] == note["applicability"] == "unknown"
    for key in ("value", "unit", "count_basis"):
        value = note["field_values"][key]
        assert value["state"] == "unknown"
        assert value["normalized"] is None
        assert value["raw"].endswith("未给出确定值")


def test_explicit_part_does_not_fill_model_and_unknown_placeholders_do_not_fill_value():
    (part,) = extract_requirements(_evidence("OLT 的 A2 料号为 PWR-09。"))
    assert part["part_number"] == "PWR-09"
    assert part["model"] is None
    item = _evidence(
        {
            "equipment_class": "EDGE",
            "scope": "示例站",
            "configuration_variant": "STD",
            "property": "fiber_interface",
            "operator": "=",
            "value": "—",
            "unit": "—",
        }
    )
    assert list(extract_requirements(item)) == []
    (candidate,) = extract_requirements(item, allow_explicit_classes=True)
    assert candidate["equipment_class"] == "EDGE"
    assert candidate["unit"] is None
    assert candidate["numeric_value"] is None
    assert candidate["applicability"] == "unknown"
    assert candidate["presence"] == "unknown"
    assert candidate["field_values"]["value"]["state"] == "unknown"
    assert candidate["field_values"]["value"]["raw"] == "—"


@pytest.mark.parametrize(
    ("constraint", "expected_role"),
    [({"operator": "≥", "value": "3"}, "minimum"), ({"expr": "=3"}, "exact")],
)
def test_explicit_device_quantity_constraint_preserves_quantity_role_provenance(
    constraint, expected_role
):
    raw = {
        "equipment_class": "OLT",
        "property": "device_quantity",
        "unit": "台",
        "count_basis": "per_site",
    } | constraint
    (candidate,) = extract_requirements(_evidence(raw))
    assert candidate["quantity_role"] == expected_role
    assert candidate["field_derivations"]["quantity_role"] == ["property", *constraint]
    assert candidate["field_values"]["quantity_role"]["raw"] is None
    assert candidate["strength"] == "unknown"
    assert {
        field["field_name"]: field["raw_span"]
        for field in candidate["field_evidence"]
        if field["field_name"] in candidate["field_derivations"]["quantity_role"]
    } == {"property": "device_quantity", **constraint}

    declaration = normalize({}, requirement=False) | {"field_evidence": [], "item_id": "row"}
    reviewed = review_fields(
        candidate | {"candidate_id": "req"}, declaration, pair_field(candidate, declaration)
    )["requirement.quantity_role"]
    assert reviewed["reviewable"] is True
    assert reviewed["normalized"] == expected_role
    assert reviewed["raw"] == {"property": "device_quantity", **constraint}

    incomplete = candidate | {
        "candidate_id": "req",
        "field_evidence": [
            field for field in candidate["field_evidence"] if field["field_name"] != "property"
        ],
    }
    reviewed = review_fields(incomplete, declaration, pair_field(incomplete, declaration))[
        "requirement.quantity_role"
    ]
    assert reviewed["reviewable"] is False


@pytest.mark.parametrize(
    "override",
    [
        {"property": None},
        {"property": "GE_port_count"},
        {"operator": "<="},
        {"expr": "=3", "operator": ">="},
        {"expr": ">=4", "value": "3"},
        {"value": "—"},
        {"value": "3.5"},
        {"unit": "W"},
        {"quantity_role": "unknown"},
        {"quantity_role": "—"},
        {"quantity_role": "estimate"},
    ],
)
def test_device_quantity_role_requires_explicit_compatible_source_fields(override):
    (candidate,) = extract_requirements(
        _evidence(
            {
                "equipment_class": "OLT",
                "property": "device_quantity",
                "operator": ">=",
                "value": "3",
                "unit": "台",
            }
            | override
        ),
        allow_explicit_classes=True,
    )
    assert "quantity_role" not in candidate["field_derivations"]
    assert candidate["quantity_role"] == override.get("quantity_role", "unknown")


def test_explicit_quantity_bound_can_be_calculated_without_formal_approval(tmp_path):
    pdf, parsed, csv, _ = generate(tmp_path / "inputs")
    payload = json.loads(parsed.read_text(encoding="utf-8"))
    row = payload["pages"][1]["blocks"][1]
    row["content"] = (
        row["content"]
        .replace("quantity_role: minimum\n", "")
        .replace("quantity: 2", "operator: >=\nvalue: 2")
    )
    parsed.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    with EvidenceService(Settings(tmp_path / "task.sqlite", (tmp_path,))) as service:
        workflow = VerificationWorkflow(service)
        run = service.ingest_pair(pdf, parsed).parse_run
        sheet = workflow.ingest_spreadsheet_path(csv, {})
        observation = workflow.observe_task(
            sources={run.parse_run_id: (2,)}, declarations={sheet["import_id"]: (3,)}
        )
    (result,) = observation.findings
    assert result.conditional.status == "violated"
    assert result.conditional.reason_code == "CONDITIONAL_VALUE_DEVIATION"
    assert result.human_confirmation.state == "not_performed"
    assert result.formal_disposition == "review_required"


def _continuation():
    parent = _evidence(
        "<table><tr><th>设备类别</th><th>工程范围</th><th>配置变体</th>"
        "<th>需求槽位</th></tr><tr><td>OLT</td><td>示例站</td><td>STD</td><td>A1</td>"
        "</tr></table>",
        kind="table",
    )
    row = [
        {"text": text, "native_pointer": f"/pages/1/blocks/0/structured_table/rows/0/{i}"}
        for i, text in enumerate(("ONU", "示例站", "STD", "A2"))
    ]
    return parent, _evidence(
        "<table><tr><td>ONU</td><td>示例站</td><td>STD</td><td>A2</td></tr></table>",
        kind="table",
        page=2,
        structured={
            "header_state": "inherited",
            "continuation_header_pointer": parent.source_pointer,
            "rows": [row],
        },
        continuation=True,
    )


def test_explicit_continuation_needs_authorized_same_run_parent_and_keeps_both_sources():
    parent, child = _continuation()
    assert list(extract_requirements(child)) == []
    assert list(extract_requirements_from_evidence([child])) == []
    assert len(list(extract_requirements_from_evidence([parent, child]))) == 2
    _, candidate = extract_requirements_from_evidence([parent, child])
    assert candidate["requirement_slot"] == "A2"
    assert candidate["equipment_class"] == "ONU"
    assert "continuation_unverified" in candidate["risk"]
    assert diagnose_requirements([parent, child])["candidates_emitted"] == 2
    for source in candidate["field_evidence"]:
        assert source["source_pointer"] == child.source_pointer
        header = source["header_evidence"]
        assert header["source_pointer"] == parent.source_pointer
        assert header["evidence_id"] == parent.evidence_id
        assert header["physical_page"] == 1
    wrong_parent = parent.model_copy(update={"artifact_asset_id": "different"})
    assert [
        c["evidence_id"] for c in extract_requirements_from_evidence([wrong_parent, child])
    ] == [parent.evidence_id]
    duplicate = parent.model_copy(update={"evidence_id": "duplicate"})
    assert [
        c["evidence_id"] for c in extract_requirements_from_evidence([parent, duplicate, child])
    ] == [parent.evidence_id, duplicate.evidence_id]


def test_ingested_native_continuation_is_visible_only_with_its_header_page(pair, tmp_path):
    pdf, parsed, payload = pair
    for page, item in enumerate(_continuation()):
        payload["pages"][page]["blocks"] = [
            {"type": item.kind, "content": item.raw_content, **item.native_context}
            | ({"continues_prev": True} if item.continues_prev else {})
        ]
    parsed.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    with EvidenceService(Settings(tmp_path / "db.sqlite", (pdf.parent,))) as service:
        run = service.ingest_pair(pdf, parsed).parse_run
        both_pages = service.list_run_evidence(run.parse_run_id, pages=(1, 2))
        child_page = service.list_run_evidence(run.parse_run_id, pages=(2,))
        assert {c["requirement_slot"] for c in extract_requirements_from_evidence(both_pages)} == {
            "A1",
            "A2",
        }
        assert list(extract_requirements_from_evidence(child_page)) == []
