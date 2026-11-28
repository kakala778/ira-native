import pytest
from fixtures.synthetic.mvp_workflow import generate
from test_task008_workflow import approve, pair, source

from ira_native.application.service import EvidenceService
from ira_native.application.workflow import VerificationWorkflow
from ira_native.domain.models import Document, DocumentRevision
from ira_native.errors import IraError
from ira_native.settings import Settings


@pytest.mark.parametrize("multi", [False, True])
def test_continuous_recalculation_roundtrip(tmp_path, multi):
    pdf, artifact, sheet, _ = generate(tmp_path / "inputs")
    settings = Settings(tmp_path / "tasks.sqlite", (tmp_path,))
    with EvidenceService(settings) as service:
        workflow = VerificationWorkflow(service)
        main = service.ingest_pair(pdf, artifact).parse_run
        sheet_id = workflow.ingest_spreadsheet_path(sheet, {})["import_id"]
        if multi:
            other, _, _ = source(workflow, tmp_path, "Supplement")
            draft = workflow.prepare_task(
                sources={main.parse_run_id: (1, 2, 3), other.parse_run_id: (1,)},
                declarations={sheet_id: (2, 3, 4)},
                roles={other.parse_run_id: "supplement"},
            )
        else:
            draft = workflow.prepare(main.parse_run_id, sheet_id)
        task = draft["draft_id"]
        edges = draft["candidate_edges"]
        by_variant = {
            requirement["config_variant"]: next(
                edge for edge in edges if edge["left_ref"] == requirement["candidate_id"]
            )
            for requirement in draft["requirements"]
            if requirement["parse_run_id"] == main.parse_run_id
        }
        first, second, third = (by_variant[f"Variant-{n}"] for n in (1, 2, 3))
        before = workflow.verify(task, [pair(edge) for edge in (first, second, third)], "synthetic")
        approve(workflow, task, first)
        after_first = workflow.recalculate_pair(
            task, first["left_ref"], first["right_ref"], "synthetic", before["run_id"]
        )
        assert any(f["formal_status"] == "satisfied" for f in after_first["findings"])
        assert sum(f["formal_status"] != "undetermined" for f in after_first["findings"]) == 1
        approve(workflow, task, second)
        after_second = workflow.recalculate_pair(
            task, second["left_ref"], second["right_ref"], "synthetic", after_first["run_id"]
        )
        repeated = workflow.recalculate_pair(
            task, second["left_ref"], second["right_ref"], "synthetic", after_second["run_id"]
        )
        assert repeated["applicability_snapshot"] == after_second["applicability_snapshot"]
    with EvidenceService(settings) as service:
        workflow = VerificationWorkflow(service)
        latest = workflow.get_run(repeated["run_id"])
        result = {f["requirement"]["config_variant"]: f for f in latest["findings"]}
        assert result["Variant-1"]["formal_status"] == "satisfied"
        assert result["Variant-2"]["formal_status"] == "violated"
        pending = result["Variant-3"]
        assert pending["candidate_edge"] and pending["formal_status"] == "undetermined"
        assert "CONDITION_UNPROVEN" in pending["trust_gates"]["blockers"]
        assert pending["conditional_calculation"]["status"] == "undetermined"
        cancelled = workflow.verify(
            task, [pair(second)], "synthetic", expected_run=repeated["run_id"]
        )
        with pytest.raises(IraError, match="selection_changed"):
            workflow.recalculate_pair(
                task, first["left_ref"], first["right_ref"], "synthetic", repeated["run_id"]
            )
        with pytest.raises(IraError, match="pair_not_selected"):
            workflow.recalculate_pair(
                task, first["left_ref"], first["right_ref"], "synthetic", cancelled["run_id"]
            )
        with pytest.raises(IraError, match="selection_changed"):
            workflow.verify(task, [pair(first)], "synthetic", expected_run=repeated["run_id"])
        after_cancel = workflow.recalculate_pair(
            task, second["left_ref"], second["right_ref"], "synthetic", cancelled["run_id"]
        )
        assert [f["formal_status"] for f in after_cancel["findings"]].count("violated") == 1
        assert len(after_cancel["applicability_snapshot"]) == 1

        doc = Document(document_id="SYN-FIX")
        service.register_revision(
            doc,
            DocumentRevision(
                document_id=doc.document_id,
                revision_id="SYN-R2",
                revision_label="REV-S2",
                asset_id=main.source_asset_id,
            ),
        )
        assert workflow.get_run(after_cancel["run_id"])["status"] == "stale"
        refreshed = workflow.recalculate_pair(
            task, second["left_ref"], second["right_ref"], "synthetic", after_cancel["run_id"]
        )
        assert all(f["formal_status"] == "undetermined" for f in refreshed["findings"])
        pdf.write_bytes(b"changed")
        with pytest.raises(IraError, match="source_mismatch"):
            workflow.recalculate_pair(
                task, second["left_ref"], second["right_ref"], "synthetic", refreshed["run_id"]
            )
