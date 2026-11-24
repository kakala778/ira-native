import hashlib
import json
from io import BytesIO

from fastapi.testclient import TestClient
from PIL import Image
from typer.testing import CliRunner

from ira_native.interfaces.api import create_app
from ira_native.interfaces.cli import app
from ira_native.settings import Settings

BASE = "/api/v1"
LOCAL = "http://127.0.0.1"


def test_cli_and_http_share_evidence_and_physical_pages(pair, tmp_path, monkeypatch):
    pdf, parsed, _ = pair
    database = tmp_path / "ira.sqlite"
    monkeypatch.setenv("IRA_DB", str(database))
    monkeypatch.setenv("IRA_ALLOWED_ROOTS", str(pdf.parent))
    runner = CliRunner()
    doctor = runner.invoke(app, ["doctor"])
    assert doctor.exit_code == 0, doctor.output
    assert json.loads(doctor.stdout)["external_models"] is False
    imported = runner.invoke(app, ["ingest", "pdf", "--pdf", str(pdf), "--parsed", str(parsed)])
    assert imported.exit_code == 0, imported.output
    result = json.loads(imported.stdout)
    asset_id = result["source"]["asset_id"]
    assert "locator_internal" not in imported.stdout
    assert str(tmp_path) not in imported.stdout
    found = runner.invoke(app, ["search", "ONU", "--page", "3"])
    assert found.exit_code == 0, found.output
    item = json.loads(found.stdout)["items"][0]
    evidence_id = item["evidence_id"]
    shown = runner.invoke(app, ["evidence", "show", evidence_id])
    location = runner.invoke(app, ["evidence", "page", evidence_id])
    assert shown.exit_code == location.exit_code == 0
    assert json.loads(location.stdout)["physical_page"] == 3
    assert json.loads(location.stdout)["precision"] == "block"
    with TestClient(create_app(Settings(database, (pdf.parent,))), base_url=LOCAL) as client:
        response = client.get(f"{BASE}/evidence/{evidence_id}")
        assert response.status_code == 200
        assert response.json() == item == json.loads(shown.stdout)
        assert client.get(f"{BASE}/evidence/{evidence_id}/page").json() == json.loads(
            location.stdout
        )
        assert client.get(f"{BASE}/search", params={"q": "设备清单"}).json()["items"]
        assert (
            client.get(f"{BASE}/parse-runs/{result['parse_run']['parse_run_id']}").json()
            == (result["parse_run"])
        )
        assert (
            client.get(f"{BASE}/assets/{asset_id}").json()["sha256"]
            == hashlib.sha256(pdf.read_bytes()).hexdigest()
        )
        download = client.get(f"{BASE}/assets/{asset_id}/pdf")
        assert download.content == pdf.read_bytes()
        assert download.headers["Content-Disposition"].startswith("attachment;")
        assert download.headers["Cache-Control"] == "no-store"
        assert download.headers["X-Content-Type-Options"] == "nosniff"
        assert client.get(f"{BASE}/assets/{asset_id}/pages/2").json()["physical_page"] == 2
        assert client.get(f"{BASE}/assets/{asset_id}/pages/0").json()["code"] == "invalid_location"
        assert client.get(f"{BASE}/assets/{asset_id}/pages/wrong").json()["code"] == (
            "invalid_location"
        )
        for page in [1, 2, 3]:
            image = client.get(f"{BASE}/assets/{asset_id}/pages/{page}/image")
            assert image.headers["Content-Type"] == "image/png"
            with Image.open(BytesIO(image.content)) as rendered:
                assert rendered.size == (300, 300)
                assert (rendered.convert("L").getextrema() == (255, 255)) == (page == 2)


def test_http_cannot_read_paths_or_bypass_allowlist(pair, tmp_path):
    pdf, _, _ = pair
    database = tmp_path / "ira.sqlite"
    configured = Settings(database, (pdf.parent,))
    with TestClient(create_app(configured), base_url=LOCAL) as client:
        forbidden = client.get(f"{BASE}/assets/%2e%2e%2fprivate%2fread.pdf/pdf")
        assert forbidden.status_code in (404, 422)
        assert "private" not in forbidden.text
        assert client.get(f"{BASE}/assets/{'f' * 64}/pdf").status_code == 404
        assert client.get(f"{BASE}/assets/pdf", params={"path": str(pdf)}).status_code == 422
        assert (
            client.get(f"{BASE}/doctor", headers={"host": "untrusted.example"}).status_code == 403
        )
        assert (
            client.post(
                f"{BASE}/assets/upload/pdf",
                content=pdf.read_bytes(),
                headers={"Content-Type": "application/pdf"},
            ).status_code
            == 403
        )
        bad_query = client.get(f"{BASE}/search", params={"q": "x", "limit": "SECRET-SYNTHETIC"})
        assert bad_query.status_code == 422
        assert bad_query.json()["code"] == "invalid_input"
        assert "SECRET-SYNTHETIC" not in bad_query.text


def test_opt_in_http_upload_has_per_resource_byte_limits(pair, tmp_path):
    pdf, parsed, _ = pair
    data = pdf.read_bytes()
    artifact_data = parsed.read_bytes()
    settings = Settings(
        tmp_path / "ira.sqlite",
        max_pdf_bytes=len(data),
        max_json_bytes=len(artifact_data),
        enable_uploads=True,
    )
    with TestClient(create_app(settings), base_url=LOCAL) as client:
        pdf_route = f"{BASE}/assets/upload/pdf"
        json_route = f"{BASE}/assets/upload/parsed_json"
        rejected = client.post(
            pdf_route,
            content=iter([data, b"overflow"]),
            headers={"Content-Type": "application/pdf"},
        )
        assert rejected.status_code == 413
        assert rejected.json()["code"] == "resource_limit"
        rejected = client.post(
            json_route, content=artifact_data + b" ", headers={"Content-Type": "application/json"}
        )
        assert rejected.status_code == 413
        assert client.post(pdf_route, content=data).status_code == 415
        uploaded = client.post(pdf_route, content=data, headers={"Content-Type": "application/pdf"})
        artifact = client.post(
            json_route, content=artifact_data, headers={"Content-Type": "application/json"}
        )
        assert uploaded.status_code == artifact.status_code == 201
        assert "locator_internal" not in uploaded.json()
        source_id = uploaded.json()["asset_id"]
        payload = {
            "source_asset_id": source_id,
            "artifact_asset_id": artifact.json()["asset_id"],
            "declared": False,
        }
        run = client.post(f"{BASE}/parse-runs", json=payload)
        assert run.status_code == 201, run.text
        assert run.json()["parse_run"]["binding_state"] == "unknown"
        assert run.json()["evidence_count"] == 4
        assert client.post(f"{BASE}/parse-runs", json=payload).json() == run.json()
        assert client.get(f"{BASE}/assets/{source_id}/pdf").content == data
