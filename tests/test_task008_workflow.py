"""Persistent multi-source acceptance over actual registered synthetic inputs."""

import csv
import json
from pathlib import Path

import pytest
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.cidfonts import UnicodeCIDFont
from reportlab.pdfgen.canvas import Canvas

from ira_native.application.service import EvidenceService
from ira_native.application.workflow import VerificationWorkflow
from ira_native.domain.models import Document, DocumentRevision
from ira_native.errors import IraError
from ira_native.settings import Settings


def source(
    workflow,
    path: Path,
    name,
    *,
    condition="",
    cls="OLT",
    scope="Test site",
    part="",
    model="SYN-8",
    variant="STD",
    slot="A1",
    count="per_site",
    revision="R1",
    quantity="2",
    replacement="",
):
    record = {
        "equipment_class": cls,
        "manufacturer_model": model,
        "configuration_variant": variant,
        "scope": scope,
        "site_group": "SYN-A",
        "count_basis": count,
        "requirement_slot": slot,
        "property": "device_quantity",
        "quantity_role": "minimum",
        "strength": "mandatory",
        "source_revision": revision,
        "condition": condition,
        "part_number": part,
        "quantity": quantity,
        "unit": "台",
    }
    pdf = path / (name + ".pdf")
    parsed = path / (name + ".json")
    pdfmetrics.registerFont(UnicodeCIDFont("STSong-Light"))
    canvas = Canvas(str(pdf), invariant=True)
    canvas.setFont("STSong-Light", 11)
    canvas.drawString(20, 800, name)
    text = "\n".join(f"{key}: {value}" for key, value in record.items())
    for line, value in enumerate(text.splitlines()):
        canvas.drawString(20, 770 - 16 * line, value)
    if replacement:
        canvas.drawString(20, 470, replacement)
    canvas.showPage()
    canvas.drawString(20, 770, "UNAUTHORIZED SECOND PAGE")
    canvas.showPage()
    canvas.save()
    parsed.write_text(
        json.dumps(
            {
                "pages": [
                    {
                        "page_idx": 0,
                        "blocks": [{"type": "text", "content": text}]
                        + ([{"type": "text", "content": replacement}] if replacement else []),
                    },
                    {
                        "page_idx": 1,
                        "blocks": [{"type": "text", "content": "UNAUTHORIZED SECOND PAGE"}],
                    },
                ],
                "is_full_document": True,
                "metadata": {"producer": {"version": "4.2.3"}},
                "extensions": {},
            }
        ),
        encoding="utf-8",
    )
    return workflow.evidence.ingest_pair(pdf, parsed).parse_run, pdf, record


def declaration(workflow, path, name, rows):
    dst = path / (name + ".csv")
    with dst.open("w", newline="", encoding="utf-8") as handle:
        out = csv.DictWriter(handle, fieldnames=list(rows[0]))
        out.writeheader()
        out.writerows(rows)
    return workflow.ingest_spreadsheet_path(dst, {})["import_id"], dst


@pytest.fixture
def registry(tmp_path):
    settings = Settings(tmp_path / "registry.sqlite", (tmp_path,))
    with EvidenceService(settings) as service:
        yield VerificationWorkflow(service), settings


def pair(edge):
    return {"requirement_id": edge["left_ref"], "declaration_id": edge["right_ref"]}


def approve(workflow, draft_id, edge):
    for category in ("fields", "source", "binding", "applicability"):
        context = workflow.human_review_context(draft_id, edge["left_ref"], edge["right_ref"])
        targets = list(context["fields"]) if category == "fields" else [category]
        workflow.record_human_review(
            draft_id,
            edge["left_ref"],
            edge["right_ref"],
            context["review_token"],
            category,
            targets,
            "confirmed",
            "Synthetic reviewer",
            "Checked original scope fixture.",
        )


def test_complementary_sources_and_sheets_reopen_and_keep_unrelated_changes_out(registry, tmp_path):
    workflow, settings = registry
    first, _, req = source(workflow, tmp_path, "first")
    second, _, other = source(
        workflow, tmp_path, "second", cls="ONU", model="SYN-9", slot="A2", scope="B site"
    )
    off, off_pdf, _ = source(workflow, tmp_path, "off", scope="Unrelated")
    imp1, _ = declaration(workflow, tmp_path, "first", [req | {"quantity": "3"}])
    imp2, sheet2 = declaration(workflow, tmp_path, "second", [other | {"quantity": "1"}])
    draft = workflow.prepare_task(
        sources={first.parse_run_id: (1,), second.parse_run_id: (1,)},
        declarations={imp1: (2,), imp2: (2,)},
        roles={second.parse_run_id: "supplement"},
        task_name="Synthetic two sources",
    )
    assert len(draft["candidate_edges"]) == 2
    assert {row["declaration_import_id"] for row in draft["declarations"]} == {imp1, imp2}
    choices = [pair(edge) for edge in draft["candidate_edges"]]
    provisional = workflow.verify(
        draft["draft_id"], choices, "Synthetic reviewer", conditional_preview=True
    )
    before = workflow.domain_result(provisional["run_id"])
    assert {f.conditional.status for f in before.findings} == {"satisfied", "violated"}
    assert {f.formal_disposition for f in before.findings} == {"review_required"}
    assert all(f.human_confirmation.state == "not_performed" for f in before.findings)
    assert len(before.evidence) == 2
    for edge in draft["candidate_edges"]:
        approve(workflow, draft["draft_id"], edge)
    formal = workflow.verify(
        draft["draft_id"], choices, "Synthetic reviewer", use_human_reviews=True
    )
    assert {f["disposition"] for f in formal["findings"]} == {"issue", "no_issue"}
    off_pdf.write_bytes(b"not a pdf")
    with EvidenceService(settings) as again:
        opened = VerificationWorkflow(again)
        result = opened.domain_result(formal["run_id"])
        assert all(f.human_confirmation.state == "confirmed" for f in result.findings)
        assert off.parse_run_id not in result.task_scope["sources"]
        assert opened.list_task_runs(draft["draft_id"])[0]["status"] == "recorded"
    with pytest.raises(IraError, match="permission_denied"):
        workflow.launch_investigation(formal["run_id"])
    document = Document(document_id="selected-doc")
    workflow.evidence.register_revision(
        document,
        DocumentRevision(
            document_id=document.document_id,
            revision_id="selected-revised",
            asset_id=second.source_asset_id,
            revision_label="R2",
        ),
    )
    stale = workflow.domain_result(formal["run_id"])
    assert stale.status == "stale"
    assert {f.historical_disposition for f in stale.findings} == {"issue", "no_issue"}
    with sheet2.open("a", encoding="utf-8") as handle:
        handle.write("\n")
    with pytest.raises(IraError, match="source_mismatch"):
        workflow.verify(draft["draft_id"], choices, "Synthetic reviewer")


def test_version_resolution_requires_effectivity_scope_and_separate_human_reviews(
    registry, tmp_path
):
    workflow, _ = registry
    old, _, _ = source(workflow, tmp_path, "base", revision="R1")
    later, _, req = source(
        workflow,
        tmp_path,
        "amendment",
        revision="R2",
        quantity="4",
        replacement="R2 supersedes R1",
    )
    proof = workflow.evidence.list_run_evidence(later.parse_run_id, pages=(1,))[1]
    doc = Document(document_id="synthetic-logical-doc")
    workflow.evidence.register_revision(
        doc,
        DocumentRevision(
            revision_id="old",
            document_id=doc.document_id,
            asset_id=old.source_asset_id,
            revision_label="R1",
            effective_from="2026-01-01",
        ),
    )
    workflow.evidence.register_revision(
        doc,
        DocumentRevision(
            revision_id="new",
            document_id=doc.document_id,
            asset_id=later.source_asset_id,
            revision_label="R2",
            effective_from="2026-06-01",
            scope={"supersedes_revision_id": "old"},
            relation_evidence=[proof.evidence_id],
        ),
    )
    imported, _ = declaration(workflow, tmp_path, "delivery", [req | {"quantity": "3"}])

    def prepare(day):
        return workflow.prepare_task(
            sources={old.parse_run_id: (1,), later.parse_run_id: (1,)},
            declarations={imported: (2,)},
            roles={later.parse_run_id: "amendment"},
            as_of=day,
        )

    future = prepare("2026-05-01")
    assert future["version_relations"][0]["state"] == "declared"
    assert "REVISION_EFFECTIVITY_UNPROVEN" in future["version_relations"][0]["blockers"]
    draft = prepare("2026-07-01")
    relation = draft["version_relations"][0]
    assert relation["state"] == "supported"
    edge = next(
        e for e in draft["candidate_edges"] if e["left_ref"] == relation["later_requirement_id"]
    )
    approve(workflow, draft["draft_id"], edge)
    choices = [pair(edge)]
    pending = workflow.verify(draft["draft_id"], choices, "Synthetic", use_human_reviews=True)
    assert all(f["disposition"] == "review_required" for f in pending["findings"])
    outside = workflow.evidence.list_run_evidence(later.parse_run_id, pages=(2,))[0]
    args = (draft["draft_id"], relation["later_requirement_id"], relation["earlier_requirement_id"])
    with pytest.raises(IraError, match="permission_denied"):
        workflow.record_version_relation(*args, outside.evidence_id, "Synthetic", "outside scope")
    workflow.record_version_relation(
        *args,
        proof.evidence_id,
        "Synthetic",
        "Verified scoped relation.",
        snapshot_id=relation["snapshot_id"],
        review_token=relation["review_token"],
    )
    with pytest.raises(IraError, match="verification_stale"):
        workflow.record_version_relation(
            *args,
            proof.evidence_id,
            "Synthetic",
            "Replayed old form.",
            review_token=relation["review_token"],
        )
    assert workflow.get_run(pending["run_id"])["status"] == "stale"
    approve(workflow, draft["draft_id"], edge)
    formal = workflow.verify(draft["draft_id"], choices, "Synthetic", use_human_reviews=True)
    assert {f["revision_state"]: f["disposition"] for f in formal["findings"]} == {
        "superseded": "review_required",
        "effective_replacement": "issue",
    }
    workflow.record_version_relation(
        *args, proof.evidence_id, "Synthetic", "New discrepancy.", decision="rejected"
    )
    stale = workflow.domain_result(formal["run_id"])
    assert stale.status == "stale"
    assert any(f.historical_disposition == "issue" for f in stale.findings)


def test_unresolved_conflict_and_declared_basis_mismatch_remain_blocked(registry, tmp_path):
    workflow, _ = registry
    first, _, req = source(workflow, tmp_path, "first")
    second, _, _ = source(workflow, tmp_path, "second", quantity="7")
    imp, _ = declaration(workflow, tmp_path, "delivery", [req | {"count_basis": "total"}])
    draft = workflow.prepare_task(
        sources={first.parse_run_id: (1,), second.parse_run_id: (1,)}, declarations={imp: (2,)}
    )
    assert len(draft["revision_conflicts"]) == 2
    context = workflow.human_review_context(
        draft["draft_id"], **{k: draft["candidate_edges"][0][k] for k in ("left_ref", "right_ref")}
    )
    assert {"REVISION_RELATION_UNRESOLVED", "COUNT_BASIS_NOT_COMPARABLE"} <= set(
        context["gates"]["blockers"]
    )


def test_scope_preflight_rejects_invalid_range_before_payload_and_separates_parts(
    registry, tmp_path, monkeypatch
):
    workflow, _ = registry
    main, _, req = source(workflow, tmp_path, "assembly")
    part, _, _ = source(workflow, tmp_path, "module", part="MODULE-A")
    imp, _ = declaration(workflow, tmp_path, "delivery", [req, req | {"quantity": "8"}])
    good = workflow.prepare_task(
        sources={main.parse_run_id: (1,), part.parse_run_id: (1,)}, declarations={imp: (2,)}
    )
    assert all(row["row_id"] == 2 for row in good["declarations"])
    assert len(good["machine_matches"]) == 1
    edge = next(e for e in good["candidate_edges"] if e["left_ref"] not in good["machine_matches"])
    assert "part_identity_unproven" in edge["comparison_blockers"]
    excluded = workflow.evidence.list_run_evidence(main.parse_run_id, pages=(2,))[0]
    monkeypatch.setattr(
        workflow.evidence, "get_evidence", lambda *_a, **_k: pytest.fail("payload read")
    )
    with pytest.raises(IraError, match="permission_denied"):
        workflow.task_evidence(good["draft_id"], excluded.evidence_id)
    monkeypatch.setattr(
        workflow.evidence, "list_run_evidence", lambda *_a, **_k: pytest.fail("payload read")
    )
    with pytest.raises(IraError, match="invalid_location"):
        workflow.prepare_task(sources={main.parse_run_id: (99,)}, declarations={imp: (2,)})
    with pytest.raises(IraError, match="permission_denied"):
        workflow.prepare_task(sources={main.parse_run_id: (1,)}, declarations={imp: (99,)})


def test_partly_extracted_notice_blocks_original_and_export_rechecks_payload(
    registry, tmp_path, monkeypatch
):
    workflow, _ = registry
    main, _, req = source(workflow, tmp_path, "assembly")
    notice, _, _ = source(
        workflow,
        tmp_path,
        "notice",
        scope="Separate site",
        model="SYN-9",
        slot="B1",
        replacement="Additional operating revisions apply to existing requirements.",
    )
    imp, _ = declaration(workflow, tmp_path, "delivery", [req | {"quantity": "3"}])
    draft = workflow.prepare_task(
        sources={main.parse_run_id: (1,), notice.parse_run_id: (1,)},
        declarations={imp: (2,)},
        roles={notice.parse_run_id: "amendment"},
    )
    edge = draft["candidate_edges"][0]
    approve(workflow, draft["draft_id"], edge)
    run = workflow.verify(
        draft["draft_id"],
        [pair(edge)],
        "Synthetic",
        conditional_preview=True,
        use_human_reviews=True,
    )
    assert all(item["disposition"] == "review_required" for item in run["findings"])
    assert any(
        item["conditional_calculation"]["reason_code"] == "REVISION_RELATION_UNRESOLVED"
        for item in run["findings"]
    )
    extra = workflow.evidence.list_run_evidence(main.parse_run_id, pages=(2,))[0]
    baseline = workflow.evidence.list_run_evidence
    monkeypatch.setattr(
        workflow.evidence,
        "list_run_evidence",
        lambda *args, **kwargs: [*baseline(*args, **kwargs), extra],
    )
    with pytest.raises(IraError, match="permission_denied"):
        workflow.domain_result(run["run_id"])
