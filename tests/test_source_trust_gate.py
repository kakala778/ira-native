import asyncio
from copy import deepcopy
from uuid import uuid4

import pytest
from fixtures.synthetic.generate_gpon_workflow import generate

from ira_native.application.service import EvidenceService
from ira_native.application.workflow import VerificationWorkflow
from ira_native.errors import IraError
from ira_native.settings import Settings


@pytest.fixture
def registered(tmp_path):
    original, parsed, declaration = generate(tmp_path / "inputs")
    with EvidenceService(Settings(tmp_path / "registry.sqlite", (original.parent,))) as service:
        workflow = VerificationWorkflow(service)
        parse_run = service.ingest_pair(original, parsed).parse_run
        sheet = workflow.ingest_spreadsheet_path(declaration, {})
        draft = workflow.prepare(parse_run.parse_run_id, sheet["import_id"])
        confirmation = {
            "requirement_id": draft["requirements"][0]["candidate_id"],
            "declaration_id": draft["declarations"][0]["item_id"],
            "fields_checked": True,
            "same_version": True,
        }
        yield original, service, workflow, parse_run, draft, confirmation


def test_declared_unverified_checkbox_cannot_produce_issue(registered):
    _, service, workflow, parse_run, draft, confirmation = registered
    assert parse_run.binding_state == "declared"
    item = service.get_evidence(draft["requirements"][0]["evidence_id"])
    assert item.source_state == "unverified"
    run = workflow.verify(draft["draft_id"], [confirmation], "Self-reported synthetic reviewer")
    finding = run["findings"][0]
    assert finding["disposition"] == "review_required", finding["preconditions"]
    assert finding["preconditions"]["source_assumption"] == "candidate_only"
    assert finding["evaluation"]["reason_code"] == "FIELD_SUPPORT_UNKNOWN"
    assert run["binding_state"] == "declared"
    assert run["field_verification_state"] == "not_verified"
    assert run["applicability_snapshot"][0]["fields_checked"] is True


def test_compare_checkbox_cannot_certify_field_support(registered):
    _, _, workflow, _, draft, _ = registered
    left = draft["requirements"][0]["evidence_id"]
    actual = workflow.compare(left, left, fields_checked=True, same_version=True)
    assert actual["preconditions"]["source_assumption"] == "candidate_only"
    assert actual["evaluation"]["constraint_relation"] == "unknown"


def test_untrusted_proof_cannot_be_supplied_as_a_confirmation(registered):
    _, _, workflow, _, draft, confirmation = registered
    with pytest.raises(IraError, match="invalid_input"):
        workflow.verify(
            draft["draft_id"],
            [confirmation | {"source_assumption": "verified_for_checked_fields"}],
            "Synthetic user",
        )


def test_pdf_hash_mismatch_blocks_checkbox_and_comparison(registered):
    original, _, workflow, _, draft, confirmation = registered
    original.write_bytes(original.read_bytes() + b"\n% synthetic byte change\n")
    with pytest.raises(IraError, match="source_mismatch"):
        workflow.verify(draft["draft_id"], [confirmation], "Synthetic user")
    left = draft["requirements"][0]["evidence_id"]
    with pytest.raises(IraError, match="source_mismatch"):
        workflow.compare(left, left, fields_checked=True, same_version=True)


def test_legacy_checkbox_upgrade_is_stale_and_cannot_reenter_agent(registered):
    _, _, workflow, _, draft, confirmation = registered
    legacy = deepcopy(workflow.verify(draft["draft_id"], [confirmation], "Synthetic user"))
    legacy["run_id"] = uuid4().hex
    legacy.pop("field_verification_state", None)
    legacy.pop("binding_state", None)
    legacy["findings"][0]["preconditions"]["source_assumption"] = "verified_for_checked_fields"
    legacy["findings"][0]["disposition"] = "issue"
    legacy["findings"][0]["preconditions"]["entity_binding"] = "confirmed"
    workflow.store.put("run", legacy, run_id=legacy["run_id"], draft_id=draft["draft_id"])
    saved = workflow.get_run(legacy["run_id"])
    assert saved["status"] == "stale"
    assert saved["stale_reason"] == "field_verification_missing"
    assert saved["field_verification_state"] == "not_verified"
    assert saved["binding_state"] == "unknown"
    assert saved["findings"][0]["preconditions"]["entity_binding"] == "unconfirmed"
    assert workflow.store.get("run", legacy["run_id"]) == legacy
    with pytest.raises(IraError, match="field_verification_missing"):
        workflow.review(
            legacy["run_id"],
            legacy["findings"][0]["finding_id"],
            "Synthetic user",
            "needs_more_evidence",
            "Synthetic note",
        )
    assert not workflow.store.events("review", legacy["run_id"])
    with pytest.raises(IraError, match="field_verification_missing"):
        asyncio.run(workflow.investigate(legacy["run_id"]))
