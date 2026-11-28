import csv
import json
import os
import time

import pytest
from bs4 import BeautifulSoup
from fastapi.testclient import TestClient
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.cidfonts import UnicodeCIDFont
from reportlab.pdfgen.canvas import Canvas
from typer.testing import CliRunner

from ira_native.application.service import EvidenceService
from ira_native.application.workflow import VerificationWorkflow
from ira_native.domain.models import Document, DocumentRevision
from ira_native.errors import IraError
from ira_native.interfaces.api import create_app
from ira_native.interfaces.cli import app as cli
from ira_native.settings import Settings


@pytest.fixture
def build(tmp_path):
    def create(
        *,
        quantity=2,
        condition="",
        declaration_condition="",
        recovered=False,
        strength="mandatory",
        scope="SYN-SCOPE",
        continuation=False,
    ):
        folder = tmp_path / f"inputs-{len(list(tmp_path.glob('inputs-*')))}"
        folder.mkdir()
        record = dict(
            equipment_class="ONU",
            manufacturer_model="SYN-ONU-X",
            configuration_variant="V1",
            property="device_quantity",
            scope=scope,
            count_basis="per_device",
            unit="台",
            quantity_role="minimum",
            strength=strength,
            quantity="2",
            condition=condition,
            source_revision="R1",
        )
        pdf = folder / "spec.pdf"
        pdfmetrics.registerFont(UnicodeCIDFont("STSong-Light"))
        canvas = Canvas(str(pdf), invariant=True)
        canvas.setFont("STSong-Light", 11)
        text = "\n".join(f"{key}: {value}" for key, value in record.items())
        for line, value in enumerate(text.splitlines()):
            canvas.drawString(30, 800 - 20 * line, value)
        canvas.save()
        block = {"type": "text", "content": text}
        extensions = {}
        if recovered:
            heads = "".join(f"<th>{key}</th>" for key in record)
            values = "".join(f"<td>{value}</td>" for value in record.values())
            block = {
                "type": "table",
                "continues_prev": continuation,
                "content": f"<table><tr>{heads}</tr><tr>{values}</tr></table>",
            }
            extensions = {
                "exp03_table_recovery": {
                    "adapter_version": "exp03-table-adapter/1",
                    "recoveries": [
                        {
                            "b0_native_pointer": "/pages/0/blocks/0",
                            "header_is_original": True,
                            "selected_parser": "synthetic-recovery",
                            "selected_native_pointer": "synthetic/0",
                        }
                    ],
                }
            }
        artifact = folder / "parse.json"
        artifact.write_text(
            json.dumps(
                {
                    "pages": [{"page_idx": 0, "blocks": [block]}],
                    "is_full_document": True,
                    "metadata": {"producer": {"version": "4.2.3"}},
                    "extensions": extensions,
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        sheet = folder / "sheet.csv"
        row = {
            key: value for key, value in record.items() if key not in ("quantity_role", "strength")
        }
        row.update(quantity=str(quantity), condition=declaration_condition, scope="SYN-SCOPE")
        with sheet.open("w", newline="", encoding="utf-8") as stream:
            writer = csv.DictWriter(stream, fieldnames=row)
            writer.writeheader()
            writer.writerow(row)
        return pdf, artifact, sheet

    return create


def draft_for(workflow, paths):
    pdf, artifact, sheet = paths
    source = workflow.evidence.ingest_pair(pdf, artifact)
    imported = workflow.ingest_spreadsheet_path(sheet, {})
    draft = workflow.prepare(source.parse_run.parse_run_id, imported["import_id"])
    return draft, draft["candidate_edges"][0]


def choice(edge):
    return [
        {
            "requirement_id": edge["left_ref"],
            "declaration_id": edge["right_ref"],
            "same_version": True,
            "fields_checked": True,
        }
    ]


def record_reviews(workflow, draft, edge, *, omit=None, applicability="confirmed"):
    def view():
        return workflow.human_review_context(draft["draft_id"], edge["left_ref"], edge["right_ref"])

    for category in ("fields", "source", "binding", "applicability"):
        if category != omit:
            context = view()
            targets = list(context["fields"]) if category == "fields" else [category]
            workflow.record_human_review(
                draft["draft_id"],
                edge["left_ref"],
                edge["right_ref"],
                context["review_token"],
                category,
                targets,
                applicability if category == "applicability" else "confirmed",
                "Synthetic operator",
                "Compared with the original PDF and declaration row.",
            )
    return view()


def test_versioned_projection_separates_calculation_confirmation_and_staleness(build, tmp_path):
    pdf, parsed, sheet = build()
    with EvidenceService(Settings(tmp_path / "versioned.sqlite", (tmp_path,))) as service:
        workflow = VerificationWorkflow(service)
        draft, edge = draft_for(workflow, (pdf, parsed, sheet))
        first = workflow.verify(
            draft["draft_id"], choice(edge), "synthetic", conditional_preview=True
        )
        before = workflow.domain_result(first["run_id"])
        observed = before.findings[0]
        assert before.schema_version == "ira.industrial-verification/v1"
        assert observed.association.status == "machine_linked"
        assert observed.conditional.status == "satisfied"
        assert observed.formal_disposition == "review_required"
        assert observed.human_confirmation.state == "not_performed"
        assert before.facts[0].measurement.lower == "2"

        record_reviews(workflow, draft, edge)
        current = workflow.verify(
            draft["draft_id"], choice(edge), "synthetic", use_human_reviews=True
        )
        confirmed = workflow.domain_result(current["run_id"]).findings[0]
        assert confirmed.formal_disposition == "no_issue"
        assert confirmed.human_confirmation.state == "confirmed"
        service.register_revision(
            Document(document_id="synthetic-current"),
            DocumentRevision(
                revision_id="synthetic-r2",
                document_id="synthetic-current",
                asset_id=draft["source_asset_id"],
                revision_label="R2",
            ),
        )
        stale = workflow.domain_result(current["run_id"]).findings[0]
        assert stale.human_confirmation.state == "stale"
        assert stale.formal_disposition == "review_required"
        assert stale.historical_disposition == "no_issue"


@pytest.mark.parametrize(
    ("quantity", "wanted", "disposition"), [(2, "satisfied", "no_issue"), (1, "violated", "issue")]
)
def test_verified_mathematics_only_upgrades_with_independent_records(
    build, tmp_path, quantity, wanted, disposition
):
    settings = Settings(tmp_path / "review.sqlite", (tmp_path,))
    with EvidenceService(settings) as app:
        workflow = VerificationWorkflow(app)
        draft, edge = draft_for(workflow, build(quantity=quantity))
        unchecked = workflow.verify(
            draft["draft_id"], choice(edge), "Synthetic", conditional_preview=True
        )
        assert unchecked["findings"][0]["disposition"] == "review_required"
        assert unchecked["findings"][0]["conditional_calculation"]["status"] == wanted
        reviewed = record_reviews(workflow, draft, edge)
        assert all(event["actor_kind"] == "local_human" for event in reviewed["history"])
        by_field = [item for event in reviewed["history"] for item in event.get("fields", [])]
        assert any(item["object_side"] == "requirement" and item["provenance"] for item in by_field)
        assert any(item["object_side"] == "declaration" and item["provenance"] for item in by_field)
        run = workflow.verify(draft["draft_id"], choice(edge), "Synthetic", use_human_reviews=True)
        assert run["field_verification_state"] == "verified"
        assert run["findings"][0]["evaluation"]["requirement_evaluation"] == wanted
        assert run["findings"][0]["disposition"] == disposition
        assert run["findings"][0]["verification_snapshot"]["review_ids"]
    with EvidenceService(settings) as app:
        assert (
            VerificationWorkflow(app).get_run(run["run_id"])["field_verification_state"]
            == "verified"
        )


def test_missing_gate_and_recovered_source(build, tmp_path):
    with EvidenceService(Settings(tmp_path / "review.sqlite", (tmp_path,))) as app:
        workflow = VerificationWorkflow(app)
        draft, edge = draft_for(workflow, build(recovered=True))
        record_reviews(workflow, draft, edge, omit="source")
        finding = workflow.verify(
            draft["draft_id"], choice(edge), "Synthetic", use_human_reviews=True
        )["findings"][0]
        assert finding["disposition"] == "review_required"
        assert "SOURCE_NOT_REVIEWED" in finding["trust_gates"]["blockers"]
        record_reviews(workflow, draft, edge)
        finding = workflow.verify(
            draft["draft_id"], choice(edge), "Synthetic", use_human_reviews=True
        )["findings"][0]
        assert finding["disposition"] == "no_issue"
        assert finding["verification_snapshot"]["inputs"]["source"]["evidence"]["native_context"][
            "table_recovery"
        ]


def test_forgery_unknown_condition_and_new_revision(build, tmp_path):
    with EvidenceService(Settings(tmp_path / "review.sqlite", (tmp_path,))) as app:
        workflow = VerificationWorkflow(app)
        draft, edge = draft_for(workflow, build())
        recorded = record_reviews(workflow, draft, edge)
        run = workflow.verify(draft["draft_id"], choice(edge), "Synthetic", use_human_reviews=True)
        with pytest.raises(IraError, match="invalid_input"):
            workflow.verify(draft["draft_id"], [choice(edge)[0] | {"verified": True}], "Synthetic")
        with pytest.raises(IraError):
            workflow.record_human_review(
                draft["draft_id"],
                edge["left_ref"],
                edge["right_ref"],
                "invalid",
                "binding",
                ["binding"],
                "confirmed",
                "Synthetic",
                "Bad token",
            )
        app.register_revision(
            Document(document_id="spec"),
            DocumentRevision(
                revision_id="new-revision",
                document_id="spec",
                asset_id=draft["source_asset_id"],
                revision_label="R2",
            ),
        )
        stale = workflow.get_run(run["run_id"])
        assert stale["status"] == "stale"
        assert stale["findings"][0]["disposition"] == "no_issue"
        assert stale["findings"][0]["historical_formal_status"] == "satisfied"
        assert stale["findings"][0]["formal_status"] == "undetermined"
        assert not workflow.human_review_context(
            draft["draft_id"], edge["left_ref"], edge["right_ref"]
        )["active_review_ids"]
        assert recorded["history"]
        next_draft, next_edge = draft_for(workflow, build(condition="indoor only"))
        record_reviews(workflow, next_draft, next_edge, omit="applicability")
        finding = workflow.verify(
            next_draft["draft_id"], choice(next_edge), "Synthetic", use_human_reviews=True
        )["findings"][0]
        assert finding["disposition"] == "review_required"
        assert "APPLICABILITY_NOT_REVIEWED" in finding["trust_gates"]["blockers"]


def test_rejected_field_and_changed_input_leave_audit_history(build, tmp_path):
    with EvidenceService(Settings(tmp_path / "registry.sqlite", (tmp_path,))) as app:
        workflow = VerificationWorkflow(app)
        paths = build()
        draft, edge = draft_for(workflow, paths)
        state = record_reviews(workflow, draft, edge)
        run = workflow.verify(draft["draft_id"], choice(edge), "Synthetic", use_human_reviews=True)
        args = (draft["draft_id"], edge["left_ref"], edge["right_ref"])
        workflow.record_human_review(
            *args,
            state["review_token"],
            "fields",
            ["declaration.value"],
            "rejected",
            "Second operator",
            "This value needs another source check.",
        )
        assert workflow.get_run(run["run_id"])["status"] == "stale"
        with pytest.raises(IraError, match="verification_stale"):
            workflow.record_human_review(
                *args,
                state["review_token"],
                "fields",
                ["declaration.value"],
                "confirmed",
                "Synthetic",
                "Old form",
            )
        again = workflow.verify(
            draft["draft_id"], choice(edge), "Synthetic", use_human_reviews=True
        )
        assert again["findings"][0]["disposition"] == "review_required"
        path = paths[2]
        path.write_text(path.read_text(encoding="utf-8").replace(",2,", ",1,"), encoding="utf-8")
        with pytest.raises(IraError, match="source_mismatch"):
            workflow.verify(draft["draft_id"], choice(edge), "Synthetic", use_human_reviews=True)
        changed, changed_edge = draft_for(workflow, paths)
        assert changed["draft_id"] != draft["draft_id"]
        assert changed_edge["field_pairing"]["declaration"]["value"]["normalized"] == "1"
        result = workflow.verify(
            changed["draft_id"], choice(changed_edge), "Synthetic", use_human_reviews=True
        )
        assert result["findings"][0]["disposition"] == "review_required"
        assert workflow.store.get("run", run["run_id"])["findings"][0]["disposition"] == "no_issue"
        assert len(workflow.store.human_history(*args)) == 5


@pytest.mark.parametrize(
    ("inputs", "applicability", "status", "disposition", "blocking_reason"),
    [
        (
            {"condition": "indoors", "declaration_condition": "outdoors"},
            "not_applicable",
            "not_applicable",
            "informational",
            None,
        ),
        (
            {},
            "not_applicable",
            "undetermined",
            "review_required",
            "NON_APPLICABILITY_NOT_SUPPORTED",
        ),
        (
            {"strength": "reference", "quantity": 1},
            "confirmed",
            "not_applicable",
            "informational",
            None,
        ),
        ({"scope": ""}, "confirmed", "undetermined", "review_required", "SCOPE_UNPROVEN"),
        (
            {"recovered": True, "continuation": True},
            "confirmed",
            "undetermined",
            "review_required",
            "CONTINUATION_UNVERIFIED",
        ),
    ],
)
def test_applicability_and_source_rules_remain_independent(
    build,
    tmp_path,
    inputs,
    applicability,
    status,
    disposition,
    blocking_reason,
):
    with EvidenceService(Settings(tmp_path / "registry.sqlite", (tmp_path,))) as app:
        workflow = VerificationWorkflow(app)
        draft, edge = draft_for(workflow, build(**inputs))
        record_reviews(workflow, draft, edge, applicability=applicability)
        result = workflow.verify(
            draft["draft_id"], choice(edge), "Synthetic", use_human_reviews=True
        )
        finding = result["findings"][0]
        assert (finding["formal_status"], finding["disposition"]) == (status, disposition)
        if blocking_reason:
            assert blocking_reason in finding["trust_gates"]["blockers"]


def test_workbench_writes_records_and_cli_only_reads_them(build, tmp_path, monkeypatch):
    settings = Settings(tmp_path / "registry.sqlite", (tmp_path,))
    with EvidenceService(settings) as app:
        draft, edge = draft_for(VerificationWorkflow(app), build())
    route = f"/workbench/drafts/{draft['draft_id']}/pairs/{edge['left_ref']}/{edge['right_ref']}"
    origin = {"Origin": "http://127.0.0.1"}
    with TestClient(create_app(settings), base_url="http://127.0.0.1") as client:

        def form_for(category):
            response = client.get(route)
            assert response.status_code == 200
            page = BeautifulSoup(response.text, "html.parser")
            form = page.select_one(f'form:has(input[name="category"][value="{category}"])')
            return {item["name"]: item["value"] for item in form.select('input[type="hidden"]')} | {
                "target": [item["value"] for item in form.select('input[name="target"]')]
            }

        submitted = form_for("fields") | {
            "reviewer": "Local reviewer",
            "note": "Checked original fields.",
            "decision": "confirmed",
            "attest": "on",
        }
        assert client.post(route + "/record", data=submitted).status_code == 403
        forged = submitted | {"verified": "true"}
        assert client.post(route + "/record", data=forged, headers=origin).status_code == 422
        forged = submitted | {"signature": "0" * 64}
        assert client.post(route + "/record", data=forged, headers=origin).status_code == 403
        assert client.post(route + "/record", data=submitted, headers=origin).status_code == 200
        assert client.post(route + "/record", data=submitted, headers=origin).status_code == 409
        for category in ("source", "binding", "applicability"):
            payload = form_for(category) | {
                "reviewer": "Local reviewer",
                "note": "Checked original sources.",
                "decision": "confirmed",
                "attest": "on",
            }
            assert client.post(route + "/record", data=payload, headers=origin).status_code == 200
        result = client.post(
            route + "/recalculate",
            data={"csrf": submitted["csrf"], "reviewer": "Local reviewer"},
            headers=origin,
        )
        assert result.status_code == 200
        assert "核验输入快照" in result.text
        exported = client.get(str(result.url).partition("?")[0] + "/export.json").json()
        assert exported["findings"][0]["formal_status"] == "satisfied"

    confirmations = tmp_path / "selections.json"
    confirmations.write_text(json.dumps(choice(edge)), encoding="utf-8")
    monkeypatch.setenv("IRA_DB", str(settings.database))
    monkeypatch.setenv("IRA_ALLOWED_ROOTS", os.pathsep.join(map(str, settings.allowed_roots)))
    args = [
        "verify",
        "run",
        draft["draft_id"],
        "--reviewer",
        "Synthetic",
        "--confirmations",
        str(confirmations),
    ]
    runner = CliRunner()
    for suffix, expected in (([], "review_required"), (["--use-human-reviews"], "no_issue")):
        cli_result = runner.invoke(cli, args + suffix)
        assert cli_result.exit_code == 0, cli_result.output
        assert json.loads(cli_result.output)["findings"][0]["disposition"] == expected


def test_registered_sibling_revision_invalidates_previous_snapshot(build, tmp_path):
    with EvidenceService(Settings(tmp_path / "registry.sqlite", (tmp_path,))) as app:
        workflow = VerificationWorkflow(app)
        draft, edge = draft_for(workflow, build())
        doc = Document(document_id="synthetic-spec")

        def revision(target, rev):
            app.register_revision(
                doc,
                DocumentRevision(
                    revision_id=rev,
                    document_id=doc.document_id,
                    asset_id=target["source_asset_id"],
                    revision_label=rev,
                ),
            )

        revision(draft, "R1")
        record_reviews(workflow, draft, edge)
        run = workflow.verify(draft["draft_id"], choice(edge), "Synthetic", use_human_reviews=True)
        assert run["findings"][0]["disposition"] == "no_issue"
        other, _ = draft_for(workflow, build(condition="new condition"))
        revision(other, "R2")
        assert workflow.get_run(run["run_id"])["status"] == "stale"
        result = workflow.verify(
            draft["draft_id"], choice(edge), "Synthetic", use_human_reviews=True
        )
        assert result["findings"][0]["disposition"] == "review_required"


def test_alembic_0002_database_upgrades_without_mutating_legacy_data(tmp_path):
    import sqlite3
    from pathlib import Path

    from alembic import command
    from alembic.config import Config
    from sqlalchemy import create_engine

    import ira_native.adapters

    db = tmp_path / "old.sqlite"
    revisions = Path(ira_native.adapters.__path__[0]) / "migrations"
    old = Config()
    old.set_main_option("script_location", str(revisions).replace("%", "%%"))
    engine = create_engine("sqlite:///" + db.as_posix())
    with engine.begin() as conn:
        old.attributes["connection"] = conn
        command.upgrade(old, "0002_workflow")
        conn.exec_driver_sql(
            "INSERT INTO documents (document_id, logical_title) VALUES ('old', 'Synthetic old doc')"
        )
    engine.dispose()
    with EvidenceService(Settings(db, (tmp_path,))), sqlite3.connect(db) as conn:
        assert conn.execute("SELECT version_num FROM alembic_version").fetchone() == (
            "0004_workbench_names",
        )
        assert conn.execute(
            "SELECT logical_title FROM documents WHERE document_id='old'"
        ).fetchone() == ("Synthetic old doc",)
        assert conn.execute("SELECT COUNT(*) FROM human_verification_records").fetchone() == (0,)
        assert conn.execute("SELECT COUNT(*) FROM workbench_specification_names").fetchone() == (0,)
        assert conn.execute("PRAGMA foreign_key_check").fetchall() == []


def test_agent_recompute_uses_only_existing_trusted_snapshot(build, tmp_path):
    import asyncio

    from pydantic_ai.messages import ModelResponse, ToolCallPart, ToolReturnPart
    from pydantic_ai.models.function import FunctionModel

    with EvidenceService(Settings(tmp_path / "registry.sqlite", (tmp_path,))) as app:
        workflow = VerificationWorkflow(app)
        draft, edge = draft_for(workflow, build(quantity=1))
        record_reviews(workflow, draft, edge)
        checked = workflow.verify(
            draft["draft_id"], choice(edge), "Synthetic", use_human_reviews=True
        )
        finding = checked["findings"][0]
        assert finding["disposition"] == "issue"

        def model(messages, info):
            returned = [
                p.content
                for message in messages
                for p in message.parts
                if isinstance(p, ToolReturnPart)
            ]
            if not returned:
                return ModelResponse(
                    parts=[ToolCallPart("search_evidence", {"query": "SYN-ONU-X"})]
                )
            if len(returned) == 1:
                return ModelResponse(parts=[ToolCallPart("request_reverification", {})])
            return ModelResponse(
                parts=[
                    ToolCallPart(
                        info.output_tools[0].name,
                        {
                            "evidence_ids": [returned[0][0]["evidence_id"]],
                        },
                    )
                ]
            )

        task = asyncio.run(
            workflow.investigate(
                checked["run_id"],
                model=FunctionModel(model),
                finding_id=finding["finding_id"],
                allow_reverification=True,
            )
        )
        assert task["reverification"]["disposition"] == "issue"
        assert workflow.get_run(checked["run_id"])["findings"][0] == finding
        assert (
            len(
                workflow.store.human_history(draft["draft_id"], edge["left_ref"], edge["right_ref"])
            )
            == 4
        )
        assert (
            workflow.get_agent_task(checked["run_id"], task["task_id"])["stop_reason"]
            == "evidence_proposed"
        )


def test_agent_trace_view_and_export_after_local_task_start(build, tmp_path, monkeypatch):
    from functools import partial

    from pydantic_ai.models.test import TestModel

    settings = Settings(tmp_path / "registry.sqlite", (tmp_path,))
    with EvidenceService(settings) as app:
        workflow = VerificationWorkflow(app)
        draft, edge = draft_for(workflow, build())
        initial = workflow.verify(draft["draft_id"], choice(edge), "Synthetic")
    route = f"/workbench/runs/{initial['run_id']}"
    web = create_app(settings)
    with TestClient(web, base_url="http://127.0.0.1") as client:
        launch = web.state.workflow.investigate
        monkeypatch.setattr(
            web.state.workflow,
            "investigate",
            partial(
                launch,
                model=TestModel(call_tools=[], custom_output_args={"decision": "human_review"}),
            ),
        )
        csrf = BeautifulSoup(client.get(route).text, "html.parser").select_one("input[name=csrf]")[
            "value"
        ]
        status = client.post(
            route + "/investigate", data={"csrf": csrf, "goal": "Review current case."}
        )
        assert status.status_code == 200
        for _ in range(50):
            if "waiting_human" in status.text:
                break
            time.sleep(0.02)
            status = client.get(status.url)
        assert "waiting_human" in status.text
        href = BeautifulSoup(status.text, "html.parser").find("a", string="导出 JSON 轨迹")["href"]
        trace = client.get(href).json()
        assert trace["model_mode"] == "test"
        assert trace["scope"]["workflow_run_id"] == initial["run_id"]
        assert trace["scope"]["control_tools"] == []
    monkeypatch.setenv("IRA_DB", str(settings.database))
    monkeypatch.setenv("IRA_ALLOWED_ROOTS", str(tmp_path))
    cli_result = CliRunner().invoke(
        cli, ["verify", "agent-task", initial["run_id"], trace["task_id"]]
    )
    assert cli_result.exit_code == 0, cli_result.output
    assert json.loads(cli_result.output) == trace
