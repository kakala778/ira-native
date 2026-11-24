import asyncio
import io
import sqlite3

import pytest
from fixtures.synthetic.generate_gpon_workflow import generate
from openpyxl import Workbook
from pydantic_ai.messages import ModelResponse, ToolCallPart, ToolReturnPart
from pydantic_ai.models.function import FunctionModel

from ira_native.adapters.spreadsheet import read_sheet
from ira_native.application.service import EvidenceService
from ira_native.application.workflow import VerificationWorkflow
from ira_native.errors import IraError
from ira_native.settings import Settings


def test_csv_multiline_raw_mapping_and_duplicate_rows():
    data = '类别,变体,范围,量,计数\nONU,V1,"A\nB",3,per_site\nONU,V1,"A\nB",3,per_site\n'
    mapping = {
        "equipment_class": "类别",
        "configuration_variant": "变体",
        "scope": "范围",
        "quantity": "量",
        "count_basis": "计数",
    }
    _, rows = read_sheet(data.encode("gb18030"), "list.csv", mapping, None, "gb18030", 10)
    assert (rows[0]["row_id"], rows[0]["row_end"]) == (2, 3)
    assert rows[0]["raw"]["scope"] == "A\nB"
    assert rows[1]["risk"] == ["duplicate_row"]
    assert rows[0]["field_evidence"][3]["column_index"] == 4


def test_xlsx_formula_kept_and_not_executed():
    workbook = Workbook()
    sheet = workbook.active
    sheet.title = "声明"
    sheet.append(["equipment_class", "configuration_variant", "scope", "quantity"])
    sheet.append(["ONU", "FE-8", "A", "=1+2"])
    buf = io.BytesIO()
    workbook.save(buf)
    _, rows = read_sheet(buf.getvalue(), "list.xlsx", {}, "声明", "utf-8", 10)
    assert rows[0]["raw"]["quantity"] == "=1+2"
    assert rows[0]["risk"] == ["formula_unresolved"]
    assert rows[0]["sheet_or_section"] == "声明"
    assert rows[0]["field_evidence"][3]["support_state"] == "insufficient"
    assert rows[0]["field_evidence"][3]["cached_value_available"] is False


def test_synthetic_full_deterministic_flow(tmp_path):
    pdf, parsed, sheet = generate(tmp_path / "inputs")
    settings = Settings(tmp_path / "ira.sqlite", (pdf.parent,), enable_uploads=True)
    with EvidenceService(settings) as evidence:
        workflow = VerificationWorkflow(evidence)
        pair = workflow.ingest_pair_bytes(pdf.read_bytes(), parsed.read_bytes())
        declaration = workflow.ingest_spreadsheet(sheet.read_bytes(), sheet.name, {})
        draft = workflow.prepare(pair.parse_run.parse_run_id, declaration["import_id"])
        assert len(draft["requirements"]) == 4 and len(draft["declarations"]) == 3
        assert draft["requirements"][0]["raw"]["quantity"] == "4"
        assert draft["requirements"][0]["field_evidence"][0]["evidence_id"]
        initial = workflow.verify(draft["draft_id"], [], "示例审查")
        assert all(f["disposition"] != "issue" for f in initial["findings"])
        confirmed = [
            {
                "requirement_id": requirement["candidate_id"],
                "declaration_id": item["item_id"],
                "fields_checked": True,
                "same_version": True,
            }
            for requirement, item in zip(
                draft["requirements"][:3], draft["declarations"], strict=True
            )
        ]
        run = workflow.verify(draft["draft_id"], confirmed, "示例审查")
        assert [f["disposition"] for f in run["findings"]] == [
            "issue",
            "informational",
            "no_issue",
            "review_required",
        ]
        finding = run["findings"][0]
        assert evidence.get_evidence_page(finding["requirement"]["evidence_id"]).physical_page == 1
        workflow.review(
            run["run_id"],
            finding["finding_id"],
            "审查人甲",
            "needs_more_evidence",
            "确认原文后补充依据",
        )
        workflow.review(
            run["run_id"],
            finding["finding_id"],
            "审查人乙",
            "accepted_with_scope",
            "仅限当前合成范围",
        )
        assert "accepted_with_scope" in workflow.export_csv(run["run_id"])
    with EvidenceService(settings) as evidence:
        loaded = VerificationWorkflow(evidence).get_run(run["run_id"])
        assert len(loaded["findings"][0]["review_history"]) == 2
        assert loaded["findings"][0]["disposition"] == "issue"
    with sqlite3.connect(settings.database) as conn:
        assert conn.execute("PRAGMA foreign_key_check").fetchall() == []


def test_workflow_upload_requires_opt_in(tmp_path):
    with EvidenceService(Settings(tmp_path / "registry.sqlite")) as evidence:
        with pytest.raises(IraError, match="permission_denied"):
            VerificationWorkflow(evidence).ingest_spreadsheet(b"", "test.csv", {})


def test_spreadsheet_binding_does_not_change_raw_asset_identity(tmp_path):
    with EvidenceService(Settings(tmp_path / "ira.sqlite", enable_uploads=True)) as evidence:
        workflow = VerificationWorkflow(evidence)
        data = b"equipment_class,configuration_variant,scope,quantity,alternate\nONU,V1,A,3,4\n"
        first = workflow.ingest_spreadsheet(data, "test.csv", {})
        different = workflow.ingest_spreadsheet(
            data,
            "test.csv",
            {
                "equipment_class": "equipment_class",
                "configuration_variant": "configuration_variant",
                "scope": "scope",
                "quantity": "alternate",
            },
        )
        assert first["asset_id"] == different["asset_id"]
        assert first["import_id"] != different["import_id"]


def test_local_source_revision_invalidates_reviewable_run(tmp_path):
    pdf, parsed, sheet = generate(tmp_path / "inputs")
    with EvidenceService(Settings(tmp_path / "ira.sqlite", (pdf.parent,))) as evidence:
        workflow = VerificationWorkflow(evidence)
        pair = evidence.ingest_pair(pdf, parsed)
        declaration = workflow.ingest_spreadsheet_path(sheet, {})
        draft = workflow.prepare(pair.parse_run.parse_run_id, declaration["import_id"])
        run = workflow.verify(draft["draft_id"], [], "示例")
        sheet.write_bytes(sheet.read_bytes() + b"\n")
        historical = workflow.get_run(run["run_id"])
        assert historical["status"] == "stale"
        assert historical["stale_reason"] == "source_mismatch"
        assert historical["result_ids"] == run["result_ids"]
        with pytest.raises(IraError, match="source_mismatch"):
            workflow.review(
                run["run_id"], run["result_ids"][0], "示例", "accepted_with_scope", "提交旧版本结果"
            )


@pytest.mark.parametrize("agent_requests_recheck", [True, False])
def test_agent_recomputation_preserves_the_run_and_appends_audit(tmp_path, agent_requests_recheck):
    pdf, parsed, sheet = generate(tmp_path / "inputs")
    with EvidenceService(Settings(tmp_path / "ira.sqlite", enable_uploads=True)) as evidence:
        workflow = VerificationWorkflow(evidence)
        pair = workflow.ingest_pair_bytes(pdf.read_bytes(), parsed.read_bytes())
        rows = workflow.ingest_spreadsheet(sheet.read_bytes(), sheet.name, {})
        draft = workflow.prepare(pair.parse_run.parse_run_id, rows["import_id"])
        run = workflow.verify(draft["draft_id"], [], "审查测试")

        def model(messages, info):
            returned = [
                part
                for message in messages
                for part in message.parts
                if isinstance(part, ToolReturnPart)
            ]
            if not returned:
                return ModelResponse(parts=[ToolCallPart("search_evidence", {"query": "OLT"})])
            if len(returned) == 1 and agent_requests_recheck:
                return ModelResponse(parts=[ToolCallPart("request_reverification", {})])
            return ModelResponse(
                parts=[
                    ToolCallPart(
                        info.output_tools[0].name,
                        {"evidence_ids": [returned[0].content[0]["evidence_id"]]},
                    )
                ]
            )

        agent = asyncio.run(workflow.investigate(run["run_id"], model=FunctionModel(model)))
        assert agent["status"] == "proposals_available"
        assert agent["reverification_requested"] is agent_requests_recheck
        assert agent["reverification_performed"]
        assert agent["reverification"]["trigger"] == (
            "agent_tool" if agent_requests_recheck else "workflow"
        )
        current = workflow.get_run(run["run_id"])
        assert current["findings"] == run["findings"]
        assert current["agent_history"][0]["evidence_ids"] == agent["evidence_ids"]
