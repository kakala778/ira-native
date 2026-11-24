import json
from copy import deepcopy

import pytest
from bs4 import BeautifulSoup
from fastapi.testclient import TestClient
from typer.testing import CliRunner

from ira_native.application.service import EvidenceService
from ira_native.application.workflow import VerificationWorkflow
from ira_native.errors import IraError
from ira_native.interfaces.api import create_app
from ira_native.interfaces.cli import app
from ira_native.settings import Settings

LOCAL = "http://127.0.0.1"
RISK = "ANNOTATION_ASSOCIATION_UNVERIFIED"
HEADERS = (
    "equipment_class",
    "configuration_variant",
    "scope",
    "strength",
    "quantity",
    "quantity_role",
    "count_basis",
    "unit",
    "inclusion_status",
)
ROW = ("OLT", "V1", "SCOPE-A", "mandatory", "4", "exact", "per_site", "台", "in_scope")
CSV = b"equipment_class,configuration_variant,scope,quantity,count_basis,unit\nOLT,V1,SCOPE-A,3,per_site,\xe5\x8f\xb0\n"


def table(*, headers=HEADERS, values=ROW, header_tag="th", attr="", header=True):
    heading = "".join(f"<{header_tag}>{x}</{header_tag}>" for x in headers)
    row = "".join(f"<td{attr if i == 0 else ''}>{x}</td>" for i, x in enumerate(values))
    return "<table>" + (f"<tr>{heading}</tr>" if header else "") + f"<tr>{row}</tr></table>"


def registered(pair, tmp_path, blocks):
    pdf, parsed, payload = pair
    payload["pages"][0]["blocks"] = blocks
    parsed.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    settings = Settings(tmp_path / "p1.sqlite", (pdf.parent,), enable_uploads=True)
    with EvidenceService(settings) as service:
        workflow = VerificationWorkflow(service)
        run = service.ingest_pair(pdf, parsed).parse_run
        sheet = workflow.ingest_spreadsheet(CSV, "synthetic.csv", {})
        draft = workflow.prepare(run.parse_run_id, sheet["import_id"])
    return settings, run, draft


@pytest.mark.parametrize("kind", ["text", "paragraph_title", "doc_title", "ref_text"])
def test_bare_evidence_and_page_are_conservative_but_body_query_can_locate(pair, tmp_path, kind):
    _, _, payload = pair
    parent = payload["pages"][0]["blocks"][0]
    parent.update(
        type=kind,
        captions=[{"content": "caption-only marker"}],
        footnotes=[{"content": "footnote-only marker"}],
    )
    settings, run, _ = registered(pair, tmp_path, [parent])
    with TestClient(create_app(settings), base_url=LOCAL) as client:
        results = client.get(
            "/api/v1/search",
            params={
                "q": "OLT",
                "parse_run_id": run.parse_run_id,
            },
        ).json()["items"]
        identifier = results[0]["evidence_id"]
        for suffix in ("", "/page"):
            route = f"/api/v1/evidence/{identifier}{suffix}"
            bare = client.get(route).json()
            assert bare["evidence_id"] == identifier
            assert bare["precision"] == "page" and bare["pdf_bbox"] is None
            body = client.get(route, params={"q": "OLT"}).json()
            assert body["match_source"] == ["body"] and body["pdf_bbox"] is not None
            note = client.get(route, params={"q": "footnote-only"}).json()
            assert note["match_source"] == ["footnote"] and note["pdf_bbox"] is None
        assert client.get(f"/api/v1/evidence/{identifier}").json()["native_context"]["captions"]


def test_web_zero_and_cli_share_counts_and_reasons(pair, tmp_path, monkeypatch):
    cases = [
        (table(header_tag="td"), False, "TABLE_HEADER_NOT_EXPLICIT"),
        (table(attr=' rowspan="2"'), False, "TABLE_ROWSPAN_UNSUPPORTED"),
        (table(attr=' colspan="2"'), False, "TABLE_COLSPAN_UNSUPPORTED"),
        (table(header=False), True, "CONTINUATION_HEADER_UNCONFIRMED"),
        (table(headers=("synthetic-secret-heading",)), False, "TABLE_HEADER_MAPPING_FAILED"),
        (table(values=("OTHER", *ROW[1:])), False, "EQUIPMENT_CLASS_REJECTED"),
    ]
    settings, run, draft = registered(
        pair,
        tmp_path,
        [{"type": "table", "content": html, "continues_prev": prev} for html, prev, _ in cases],
    )
    monkeypatch.setenv("IRA_DB", str(settings.database))
    monkeypatch.setenv("IRA_ALLOWED_ROOTS", str(pair[0].parent))
    cli = CliRunner().invoke(app, ["verify", "diagnose", run.parse_run_id])
    assert cli.exit_code == 0, cli.output
    expected = json.loads(cli.stdout)
    assert expected["candidates_emitted"] == 0 and expected["records_rejected"] == 1
    with TestClient(create_app(settings), base_url=LOCAL) as client:
        response = client.get(f"/workbench/drafts/{draft['draft_id']}")
        view = BeautifulSoup(response.text, "html.parser")
        assert "未找到要求候选不等于原始文档不存在相关要求。" in response.text
        section = view.select_one("#candidate-diagnostics")
        for key in (
            "evidence_total",
            "tables_seen",
            "records_seen",
            "records_rejected",
            "candidates_emitted",
        ):
            assert section.select_one(f'[data-diagnostic="{key}"]').text.strip() == str(
                expected[key]
            )
        for _, _, reason in cases:
            assert section.select_one(f'[data-reason="{reason}"]').text.strip() == str(
                expected["reason_counts"][reason]
            )
        assert "synthetic-secret-heading" not in response.text
        assert "SCOPE-A" not in section.text


def test_annotation_contradiction_stays_a_risk_through_draft_verify_compare(pair, tmp_path):
    note = "仅适用于 SCOPE-B，且为参考。"
    caption = "共用题注（合成）"
    settings, run, draft = registered(
        pair,
        tmp_path,
        [
            {
                "type": "table",
                "content": table(),
                "captions": [{"content": caption}],
                "footnotes": [{"content": note}],
            },
            {"type": "table", "content": table(), "captions": [{"content": caption}]},
        ],
    )
    assert len(draft["requirements"]) == 2
    for req in draft["requirements"]:
        assert RISK in req["risk"]
        assert (req["scope"], req["strength"], req["quantity_role"]) == (
            "SCOPE-A",
            "mandatory",
            "exact",
        )
    with TestClient(create_app(settings), base_url=LOCAL) as client:
        response = client.get(f"/workbench/drafts/{draft['draft_id']}")
        assert response.status_code == 200
        view = BeautifulSoup(response.text, "html.parser")
        assert view.select_one("#candidate-diagnostics") is None
        details = view.select('[data-annotation-association="unverified"]')
        assert len(details) == 2 and all(RISK in item.text for item in details)
        assert note in response.text and caption in response.text and "物理第 1 页" in response.text
    first, second = draft["requirements"]
    with EvidenceService(settings) as service:
        workflow = VerificationWorkflow(service)
        diagnostic = workflow.diagnose_requirements(run.parse_run_id)
        assert diagnostic["reason_counts"][RISK] == 2
        confirmation = {
            "requirement_id": first["candidate_id"],
            "declaration_id": draft["declarations"][0]["item_id"],
            "fields_checked": True,
            "same_version": True,
        }
        result = workflow.verify(draft["draft_id"], [confirmation], "Synthetic reviewer")
        assert result["field_verification_state"] == "not_verified"
        assert result["applicability_snapshot"][0]["fields_checked"] is True
        assert all(f["disposition"] == "review_required" for f in result["findings"])
        actual = result["findings"][0]
        assert actual["preconditions"]["entity_binding"] == "unconfirmed"
        assert actual["evaluation"]["reason_code"] == RISK
        other = workflow.compare(
            first["evidence_id"], second["evidence_id"], fields_checked=True, same_version=True
        )
        assert other["preconditions"]["entity_binding"] == "unconfirmed"
        assert other["evaluation"]["finding_disposition"] == "review_required"
        assert other["evaluation"]["reason_code"] == RISK
        with pytest.raises(IraError, match="invalid_input"):
            workflow.verify(draft["draft_id"], [confirmation], " ")
        with pytest.raises(IraError, match="invalid_input"):
            workflow.verify(draft["draft_id"], [confirmation, confirmation], "Synthetic reviewer")
        with pytest.raises(IraError, match="invalid_input"):
            workflow.review(
                result["run_id"], actual["finding_id"], " ", "needs_more_evidence", "Synthetic note"
            )
        assert workflow.store.events("review", result["run_id"]) == []

        legacy = deepcopy(draft)
        legacy["draft_id"] = "f" * 64
        for req in legacy["requirements"]:
            req.pop("annotation_context")
            req["risk"].remove(RISK)
        workflow.store.put(
            "draft",
            legacy,
            draft_id=legacy["draft_id"],
            parse_run_id=run.parse_run_id,
            import_id=legacy["declaration_import_id"],
        )
        reopened = workflow.get_draft(legacy["draft_id"])
        assert all(RISK in req["risk"] for req in reopened["requirements"])
        assert (
            workflow.verify(legacy["draft_id"], [confirmation], "Synthetic reviewer")["findings"][
                0
            ]["evaluation"]["reason_code"]
            == RISK
        )
        assert workflow.store.get("draft", legacy["draft_id"]) == legacy
