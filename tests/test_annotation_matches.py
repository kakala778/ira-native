"""Synthetically reproduce annotation hits inheriting a parent text box."""

import json
from html import unescape
from re import search

import pytest
from fastapi.testclient import TestClient

from ira_native.application.service import EvidenceService
from ira_native.interfaces.api import create_app
from ira_native.settings import Settings

_LOCAL = "http://127.0.0.1"


@pytest.fixture
def annotated_pair(pair, tmp_path):
    pdf, parsed, artifact = pair
    parent = artifact["pages"][0]["blocks"][0]
    parent.update(
        captions=[{"content": "Caption alpha OLT", "bbox": [0.01, 0.5, 0.08, 0.8]}],
        footnotes=[{"content": "Footnote beta OLT", "bbox": [0.1, 0.8, 0.2, 0.9]}],
    )
    return pdf, parsed, artifact, parent, Settings(tmp_path / "annotations.sqlite", (pdf.parent,))


@pytest.mark.parametrize("kind", ["text", "paragraph_title", "doc_title"])
@pytest.mark.parametrize(("query", "source"), [("alpha", "caption"), ("beta", "footnote")])
def test_annotation_only_never_uses_text_parent_box(annotated_pair, kind, query, source):
    pdf, parsed, artifact, parent, settings = annotated_pair
    parent["type"] = kind
    parsed.write_text(json.dumps(artifact), encoding="utf-8")
    with EvidenceService(settings) as app:
        imported = app.ingest_pair(pdf, parsed)
        (hit,) = app.search_evidence(query, parse_run_id=imported.parse_run.parse_run_id).items
        assert hit.precision == "page", "Annotation-only hit used the text parent's block box"
        assert hit.pdf_bbox is None
        assert hit.match_source == (source,)
        assert "annotation_only_match" in hit.risk
        assert app.get_evidence(hit.evidence_id).precision == "page"
        assert app.get_evidence_page(hit.evidence_id, query=query).precision == "page"


def test_body_and_annotation_match_retains_explicit_body_location(annotated_pair):
    pdf, parsed, artifact, _, settings = annotated_pair
    parsed.write_text(json.dumps(artifact), encoding="utf-8")
    with EvidenceService(settings) as app:
        app.ingest_pair(pdf, parsed)
        (hit,) = app.search_evidence("OLT").items
        assert hit.precision == "block"
        assert hit.pdf_bbox is not None
        assert hit.match_source == ("body", "caption", "footnote")
        assert "annotation_only_match" not in hit.risk


def test_query_cannot_join_unrelated_native_fields(annotated_pair):
    pdf, parsed, artifact, _, settings = annotated_pair
    parsed.write_text(json.dumps(artifact), encoding="utf-8")
    with EvidenceService(settings) as app:
        app.ingest_pair(pdf, parsed)
        assert not app.search_evidence("GE(0) Caption").items
        assert not app.search_evidence("OLT Footnote").items
        (hit,) = app.search_evidence("Footnote beta OLT", mode="exact").items
        assert hit.match_source == ("footnote",)
        assert hit.precision == "page"


def test_candidate_filter_applies_before_search_limit(annotated_pair):
    pdf, parsed, artifact, _, settings = annotated_pair
    artifact["pages"][2]["blocks"][0]["footnotes"] = [{"content": "GE(0) Caption"}]
    parsed.write_text(json.dumps(artifact), encoding="utf-8")
    with EvidenceService(settings) as app:
        app.ingest_pair(pdf, parsed)
        (hit,) = app.search_evidence("GE(0) Caption", limit=1).items
        assert hit.physical_page == 3
        assert hit.match_source == ("footnote",)
        assert hit.precision == "page"


def test_web_search_link_and_api_keep_annotation_location(annotated_pair):
    pdf, parsed, artifact, _, settings = annotated_pair
    parsed.write_text(json.dumps(artifact), encoding="utf-8")
    with EvidenceService(settings) as app:
        app.ingest_pair(pdf, parsed)
        (hit,) = app.search_evidence("beta OLT").items
    with TestClient(create_app(settings), base_url=_LOCAL) as client:
        response = client.get("/workbench/search", params={"q": "beta OLT"})
        assert "脚注" in response.text
        link = search(r'href="(/workbench/evidence/[^"]+)"', response.text)
        assert link is not None
        opened = client.get(unescape(link.group(1)))
        assert opened.status_code == 200
        assert 'data-evidence-precision="page"' in opened.text
        assert 'data-bbox="null"' in opened.text
        assert "脚注" in opened.text
        path = f"/api/v1/evidence/{hit.evidence_id}"
        assert client.get(path).json()["precision"] == "page"
        contextual = client.get(f"{path}/page", params={"q": "beta OLT"})
        assert contextual.status_code == 200
        assert contextual.json()["match_source"] == ["footnote"]
        assert contextual.json()["pdf_bbox"] is None
        other = client.get(f"{path}/page", params={"q": "nonmatching"})
        assert other.status_code == 404
