from urllib.parse import parse_qs, urlsplit

from bs4 import BeautifulSoup
from fastapi.testclient import TestClient
from fixtures.synthetic.generate_gpon_workflow import generate

from ira_native.interfaces.api import create_app
from ira_native.settings import Settings


def _csrf(response):
    document = BeautifulSoup(response.text, "html.parser")
    return document.select_one("input[name=csrf]")["value"]


def _ids(response):
    return {
        key: values[0]
        for key, values in parse_qs(urlsplit(response.headers["location"]).query).items()
    }


def test_workbench_runs_confirmation_and_review_against_registered_sources(tmp_path):
    pdf, parsed, sheet = generate(tmp_path / "inputs")
    settings = Settings(tmp_path / "registry.sqlite", enable_uploads=True)
    with TestClient(create_app(settings), base_url="http://127.0.0.1") as client:
        home = client.get("/")
        token = _csrf(home)
        assert "script-src 'self'" in home.headers["content-security-policy"]
        assert "SameSite=strict" in home.headers["set-cookie"]
        assert "HttpOnly" in home.headers["set-cookie"]
        pdf_upload = client.post(
            "/workbench/inputs/pdf",
            data={"csrf": token},
            files={
                "pdf": (pdf.name, pdf.read_bytes(), "application/pdf"),
                "parsed": (parsed.name, parsed.read_bytes(), "application/json"),
            },
            follow_redirects=False,
        )
        assert pdf_upload.status_code == 303, pdf_upload.text
        pair = _ids(pdf_upload)
        list_upload = client.post(
            "/workbench/inputs/spreadsheet",
            data={"csrf": token, "encoding": "utf-8-sig", **pair},
            files={"spreadsheet": (sheet.name, sheet.read_bytes(), "text/csv")},
            follow_redirects=False,
        )
        assert list_upload.status_code == 303, list_upload.text
        draft = client.post("/workbench/drafts", data={"csrf": token, **_ids(list_upload)})
        assert draft.status_code == 200
        assert "4 个要求候选" in draft.text
        form = BeautifulSoup(draft.text, "html.parser")
        candidate = form.select_one("input[name=confirmed_id]")["value"]
        row_select = f"declaration_{candidate}"
        declaration = form.select(f"select[name={row_select}] option")[1]["value"]
        verify = str(draft.url) + "/verify"
        payload = {
            "csrf": token,
            "reviewer": "合成审查员",
            "confirmed_id": candidate,
            row_select: declaration,
            f"fields_checked_{candidate}": "on",
        }
        partial = client.post(verify, data=payload)
        assert partial.status_code == 200, partial.text
        partial_record = client.get(str(partial.url) + "/export.json").json()
        assert partial_record["findings"][0]["disposition"] != "issue"
        payload[f"same_version_{candidate}"] = "on"
        run = client.post(verify, data=payload)
        assert run.status_code == 200, run.text
        assert "待核实问题" in run.text and "清单与核验" in run.text
        assert "页级定位" in run.text
        assert 'data-bbox="null"' in run.text
        export = client.get(str(run.url) + "/export.json").json()
        first = export["findings"][0]
        assert first["disposition"] == "issue"
        finding_url = str(run.url) + f"/findings/{first['finding_id']}/review"
        review = client.post(
            finding_url,
            data={
                "csrf": token,
                "reviewer": "合成审查员",
                "decision": "needs_more_evidence",
                "note": "<script>not an executable review note</script>",
            },
        )
        assert review.status_code == 200, review.text
        assert "&lt;script&gt;not an executable review note&lt;/script&gt;" in review.text
        csv_export = client.get(str(run.url) + "/export.csv")
        assert csv_export.status_code == 200 and "needs_more_evidence" in csv_export.text
        assert client.get("/workbench/search", params={"q": "equipment_class"}).status_code == 200


def test_workbench_rejects_unsafe_requests_and_serves_pinned_resources(tmp_path):
    settings = Settings(tmp_path / "registry.sqlite", max_pdf_bytes=8, enable_uploads=True)
    with TestClient(create_app(settings), base_url="http://127.0.0.1") as client:
        home = client.get("/")
        token = _csrf(home)
        assert client.post("/workbench/drafts", data={"csrf": "wrong"}).status_code == 403
        assert (
            client.post(
                "/workbench/drafts", data={"csrf": token}, headers={"Sec-Fetch-Site": "cross-site"}
            ).status_code
            == 403
        )
        large = client.post(
            "/workbench/inputs/pdf",
            data={"csrf": token},
            files={
                "pdf": ("example.pdf", b"123456789", "application/pdf"),
                "parsed": ("example.json", b"{}", "application/json"),
            },
        )
        assert large.status_code == 413 and "resource_limit" in large.text
        main = client.get("/workbench/static/pdfjs/pdf.min.mjs")
        assert main.status_code == 200
        assert "6.5.438" in main.text
        assert main.headers["content-type"].startswith("text/javascript")
        worker = client.get("/workbench/static/pdfjs/pdf.worker.min.mjs")
        assert worker.status_code == 200
        assert client.get("/workbench/static/%2e%2e%2fweb.py").status_code in (403, 404)
