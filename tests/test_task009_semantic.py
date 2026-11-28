"""Bounded local-host proposal replay with no model or trust promotion."""

import pytest
from test_task008_workflow import declaration, source

from ira_native.application.service import EvidenceService
from ira_native.application.workflow import VerificationWorkflow
from ira_native.domain.models import Document, DocumentRevision
from ira_native.errors import IraError
from ira_native.settings import Settings


@pytest.fixture
def context(tmp_path):
    with EvidenceService(Settings(tmp_path / "registry.sqlite", (tmp_path,))) as service:
        workflow = VerificationWorkflow(service)
        parsed, _, requirement = source(workflow, tmp_path, "Synthetic")
        sheet, _ = declaration(workflow, tmp_path, "Synthetic", [requirement | {"quantity": "1"}])
        draft = workflow.prepare_task(
            sources={parsed.parse_run_id: (1,)},
            declarations={sheet: (2,)},
        )
        binding = workflow.semantic_binding(draft["draft_id"])
        ref = next(iter(binding["refs"]))
        proposal = {
            "schema": "ira.semantic-proposal/v1",
            **{key: binding[key] for key in ("task_id", "input_hash", "ref_map_hash")},
            "facts": [
                {
                    "side": "requirement",
                    "attribute": "设备数量",
                    "state": "present",
                    "model": requirement["manufacturer_model"],
                    "scope": requirement["scope"],
                    "value": "2",
                    "unit": "台",
                    "op": ">=",
                    "refs": [ref],
                    "field_refs": {"value": [ref], "attribute": [ref]},
                }
            ],
            "unknowns": [],
        }
        yield workflow, draft, parsed, proposal


def submit(workflow, draft, proposal, **kwargs):
    return workflow.verify(
        draft["draft_id"],
        [],
        "Synthetic replay",
        semantic_proposal=proposal,
        semantic_producer="Synthetic adapter",
        **kwargs,
    )


def test_legal_proposal_runs_original_rules_without_formal_confirmation(context):
    workflow, draft, _parsed, proposal = context
    record = submit(workflow, draft, proposal)
    audit = record["host_compilation"]
    assert audit["status"] == "compiled"
    assert audit["producer"] == "Synthetic adapter"
    row = audit["compiled"][0]
    assert row["declaration_physical_row"] == 2
    assert row["declaration_import_id"] in draft["task_scope"]["declarations"]
    assert row["field_evidence"]["value"][0]["evidence_id"] in row["evidence_ids"]
    result = workflow.domain_result(record["run_id"])
    assert result.findings[0].conditional.status == "violated"
    assert result.findings[0].formal_disposition == "review_required"
    assert result.findings[0].human_confirmation.state == "not_performed"
    assert result.host_compilation == audit
    with pytest.raises(IraError, match="invalid_input"):
        submit(workflow, draft, proposal, use_human_reviews=True)


@pytest.mark.parametrize(
    ("change", "code"),
    [
        ("task", "permission_denied"),
        ("reference", "permission_denied"),
        ("actual_evidence_id", "permission_denied"),
        ("map", "verification_stale"),
        ("input", "verification_stale"),
        ("self_validated", "invalid_input"),
    ],
)
def test_binding_and_protocol_reject_before_comparison(context, change, code):
    workflow, draft, parsed, proposal = context
    if change == "task":
        proposal["task_id"] = "0" * 64
    elif change == "reference":
        proposal["facts"][0]["refs"] = [2]  # registered page two is outside this task
    elif change == "actual_evidence_id":
        actual = workflow.evidence.list_run_evidence(parsed.parse_run_id, pages=(2,))[0]
        proposal["facts"][0]["refs"] = [actual.evidence_id]
    elif change == "map":
        proposal["ref_map_hash"] = "0" * 64
    elif change == "input":
        proposal["input_hash"] = "0" * 64
    else:
        proposal["facts"][0]["validated"] = True
    with pytest.raises(IraError, match=code):
        submit(workflow, draft, proposal)
    assert not workflow.list_task_runs(draft["draft_id"])


def test_proposed_conflict_or_unproven_derivation_cannot_replace_original(context):
    workflow, draft, _parsed, proposal = context
    legal = proposal["facts"][0]
    conflict = legal | {"value": "999"}
    fabricated = legal | {"field_refs": {"trusted_basis": legal["refs"]}}
    record = submit(workflow, draft, proposal | {"facts": [legal, conflict, fabricated]})
    audit = record["host_compilation"]
    assert audit["status"] == "rejected"
    assert audit["conflicts"][0]["deterministic_candidate_ids"] == [
        draft["requirements"][0]["candidate_id"]
    ]
    assert audit["proposed_facts"][1]["value"] == "999"
    assert audit["rejected_candidates"][0]["reason"] == "unproven_field_or_pair"
    assert record["findings"][0]["requirement"]["numeric_value"] == "2"
    assert record["findings"][0]["conditional_calculation"]["status"] == "undetermined"


def test_replay_uses_current_bindings_and_old_result_becomes_stale(context):
    workflow, draft, parsed, proposal = context
    first = submit(workflow, draft, proposal)
    assert submit(workflow, draft, proposal)["run_id"] != first["run_id"]
    document = Document(document_id="synthetic-doc")
    workflow.evidence.register_revision(
        document,
        DocumentRevision(
            document_id=document.document_id,
            asset_id=parsed.source_asset_id,
            revision_id="new-registration",
            revision_label="R2",
        ),
    )
    result = workflow.domain_result(first["run_id"])
    assert result.status == "stale"
    assert result.findings[0].conditional is None
    assert result.host_compilation["status"] == "compiled"
    with pytest.raises(IraError, match="verification_stale"):
        submit(workflow, draft, proposal)


def test_live_evidence_ref_map_is_rechecked_on_read(context, monkeypatch):
    workflow, draft, _parsed, proposal = context
    record = submit(workflow, draft, proposal)
    original = workflow.evidence.list_run_evidence

    def changed(*args, **kwargs):
        return [
            item.model_copy(update={"raw_content": "reprojected synthetic evidence"})
            for item in original(*args, **kwargs)
        ]

    monkeypatch.setattr(workflow.evidence, "list_run_evidence", changed)
    result = workflow.domain_result(record["run_id"])
    assert result.status == "stale"
    assert result.findings[0].conditional is None
    with pytest.raises(IraError, match="verification_stale"):
        submit(workflow, draft, proposal)
