"""Scoped industrial observations and conditional rule decisions."""

import json

import pytest
from fixtures.synthetic.mvp_workflow import generate

from ira_native.application.candidates import normalize
from ira_native.application.results import fact_for, paired_declaration
from ira_native.application.service import EvidenceService
from ira_native.application.workflow import VerificationWorkflow
from ira_native.domain.candidate_matching import pair_field
from ira_native.domain.models import BindingState
from ira_native.errors import IraError
from ira_native.settings import Settings


def test_task_observation_reads_only_allowed_pages_rows_and_documents(tmp_path):
    pdf, parsed, csv, _ = generate(tmp_path / "inputs")
    with EvidenceService(Settings(tmp_path / "task.sqlite", (tmp_path,))) as service:
        workflow = VerificationWorkflow(service)
        run = service.ingest_pair(pdf, parsed).parse_run
        sheet = workflow.ingest_spreadsheet_path(csv, {})
        raw = json.loads(parsed.read_text(encoding="utf-8"))
        raw["pages"][1]["blocks"].append(
            {
                "type": "text",
                "content": "第6.1款 SYN-ONU-2 的 A3 光纤接口不适用；本项目不设置光纤接口。",
            }
        )
        alternate = parsed.with_name("supplement.json")
        alternate.write_text(json.dumps(raw), encoding="utf-8")
        supplement = service.ingest_pair(pdf, alternate).parse_run

        observed = workflow.observe_task(
            sources={run.parse_run_id: (1,), supplement.parse_run_id: (2,)},
            declarations={sheet["import_id"]: (2, 3)},
        )
        assert {(x.parse_run_id, x.physical_page) for x in observed.evidence} == {
            (run.parse_run_id, 1),
            (supplement.parse_run_id, 2),
        }
        assert {row["row_id"] for row in observed.declarations} == {2, 3}
        assert all(f.human_confirmation.state == "not_performed" for f in observed.findings)
        assert any(f.conditional.status == "not_applicable" for f in observed.findings)
        assert any(f.conditional.status == "satisfied" for f in observed.findings)
        assert len({fact.asset_id for fact in observed.facts if fact.side == "declaration"}) == 1
        assert all(f.formal_disposition == "review_required" for f in observed.findings)

        with pytest.raises(IraError, match="invalid_location"):
            workflow.observe_task(sources={run.parse_run_id: (4,)}, declarations={})
        with pytest.raises(IraError, match="permission_denied"):
            workflow.observe_task(
                sources={run.parse_run_id: (1,)}, declarations={sheet["import_id"]: (99,)}
            )


def test_condition_and_subject_cannot_be_combined(tmp_path):
    pdf, parsed, sheet, _ = generate(tmp_path / "inputs")
    with EvidenceService(Settings(tmp_path / "task.sqlite", (tmp_path,))) as service:
        workflow = VerificationWorkflow(service)
        run = service.ingest_pair(pdf, parsed).parse_run
        imported = workflow.ingest_spreadsheet_path(sheet, {})
        by_condition = workflow.observe_task(
            sources={run.parse_run_id: (3,)}, declarations={imported["import_id"]: (4,)}
        )
        assert by_condition.findings[0].conditional.reason_code == "CONDITION_UNPROVEN"
        assert by_condition.findings[0].conditional.comparison_state == "not_comparable"
        different_subject = workflow.observe_task(
            sources={run.parse_run_id: (1,)}, declarations={imported["import_id"]: (3,)}
        )
        assert not different_subject.candidate_edges
        assert different_subject.findings[0].conditional.status == "undetermined"


def test_competing_scoped_revisions_require_resolution(tmp_path):
    pdf, parsed, sheet, _ = generate(tmp_path / "inputs")
    data = json.loads(parsed.read_text(encoding="utf-8"))
    block = data["pages"][0]["blocks"][1]
    block["content"] = block["content"].replace(
        "source_revision: REV-S1", "source_revision: REV-S2"
    )
    changed = parsed.with_name("second-parsed.json")
    changed.write_text(json.dumps(data), encoding="utf-8")
    with EvidenceService(Settings(tmp_path / "task.sqlite", (tmp_path,))) as service:
        workflow = VerificationWorkflow(service)
        base = service.ingest_pair(pdf, parsed).parse_run
        supplement = service.ingest_pair(pdf, changed).parse_run
        imported = workflow.ingest_spreadsheet_path(sheet, {})
        observed = workflow.observe_task(
            sources={base.parse_run_id: (1,), supplement.parse_run_id: (1,)},
            declarations={imported["import_id"]: (2,)},
        )
        assert len(observed.findings) == 2
        assert all(
            item.conditional.reason_code == "REVISION_RELATION_UNRESOLVED"
            and item.formal_disposition == "review_required"
            for item in observed.findings
        )


def test_typed_facts_keep_quantity_and_power_in_separate_facts():
    row = normalize(
        {
            "property": "maximum_power",
            "quantity": "2",
            "unit": "台",
            "technical_value": "100",
            "technical_unit": "W",
        },
        requirement=False,
    ) | {"item_id": "original-row", "row_id": 2}
    raw = fact_for(row)
    assert (raw.attribute, raw.measurement.value, raw.measurement.unit) == (
        "maximum_power",
        "100",
        "W",
    )
    result = []
    for attribute in ("device_quantity", "maximum_power"):
        requirement = {"property": attribute}
        edge = {"field_pairing": pair_field(requirement, row)}
        result.append(fact_for(paired_declaration(requirement, row, edge), paired=True))
    assert len({item.identity for item in result}) == 2
    assert {item.record_id for item in result} == {"original-row"}
    assert {(x.measurement.value, x.measurement.unit) for x in result} == {
        ("2", "台"),
        ("100", "W"),
    }


def test_mismatched_parse_pair_blocks_machine_result(tmp_path, monkeypatch):
    pdf, parsed, sheet, _ = generate(tmp_path / "inputs")
    with EvidenceService(Settings(tmp_path / "task.sqlite", (tmp_path,))) as service:
        workflow = VerificationWorkflow(service)
        run = service.ingest_pair(pdf, parsed).parse_run
        imported = workflow.ingest_spreadsheet_path(sheet, {})
        original = service.get_run
        monkeypatch.setattr(
            service,
            "get_run",
            lambda identity: original(identity).model_copy(
                update={"binding_state": BindingState.MISMATCH}
            ),
        )
        observed = workflow.observe_task(
            sources={run.parse_run_id: (1,)}, declarations={imported["import_id"]: (2,)}
        )
        assert observed.findings[0].conditional.reason_code == "SOURCE_MISMATCH"
        assert observed.findings[0].conditional.status == "undetermined"
