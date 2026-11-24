import hashlib
import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor

import pytest

from ira_native.application.service import EvidenceService
from ira_native.domain.models import Document, DocumentRevision, SourceKind
from ira_native.errors import IraError
from ira_native.settings import Settings


@pytest.fixture
def service(pair, tmp_path):
    with EvidenceService(Settings(tmp_path / "ira.sqlite", (pair[0].parent,))) as app:
        yield app


def test_independent_registry_and_native_provenance(service, pair):
    pdf, parsed, _ = pair
    imported = service.ingest_pair(pdf, parsed)
    assert imported.source.sha256 == hashlib.sha256(pdf.read_bytes()).hexdigest()
    assert imported.artifact.sha256 == hashlib.sha256(parsed.read_bytes()).hexdigest()
    assert imported.source.asset_id != imported.artifact.asset_id
    assert service.repository.get_asset(imported.source.asset_id).locator_internal == str(pdf)
    assert not imported.source.access_policy.external_model
    assert service.get_asset(imported.source.asset_id).revisions == []
    run = imported.parse_run
    assert run.binding_state == "declared"
    assert run.source_asset_id == imported.source.asset_id
    assert run.artifact_asset_id == imported.artifact.asset_id
    assert run.parser_version == "4.2.0"
    assert imported.evidence_count == 4
    hit = service.search_evidence("OLT").items[0]
    assert hit.parse_run_id == run.parse_run_id
    assert hit.source_pointer == "/pages/0/blocks/0"
    assert hit.source_state == "unverified"
    assert hit.extraction_state == "candidate"
    assert hit.page_index == 0
    assert hit.physical_page == 1
    assert hit.match_source == ("body",)
    assert service.get_evidence(hit.evidence_id, query="OLT") == hit
    assert service.get_evidence(hit.evidence_id) == hit.model_copy(update={"match_source": ()})
    assert service.repository.get_asset_data(imported.artifact.asset_id) == parsed.read_bytes()


def test_repeated_and_changed_artifacts_keep_old_evidence(service, pair):
    pdf, parsed, payload = pair
    first = service.ingest_pair(pdf, parsed)
    assert first == service.ingest_pair(pdf, parsed)
    old = service.search_evidence("ONU").items[0]
    parsed = parsed.with_name("second.json")
    payload["pages"][2]["blocks"][0]["content"] = "ONU alternative"
    parsed.write_text(json.dumps(payload), encoding="utf-8")
    second = service.ingest_pair(pdf, parsed)
    assert first.source.asset_id == second.source.asset_id
    assert first.parse_run.parse_run_id != second.parse_run.parse_run_id
    assert service.get_evidence(old.evidence_id, query="ONU") == old
    assert service.get_evidence(old.evidence_id) == old.model_copy(update={"match_source": ()})
    result = service.search_evidence("alternative", parse_run_id=second.parse_run.parse_run_id)
    assert len(result.items) == 1
    assert result.items[0].evidence_id != old.evidence_id
    assert service.get_asset(first.source.asset_id).revisions == []


def test_document_revision_is_explicit(service, pair):
    pdf = service.ingest_source(pair[0], SourceKind.PDF)
    doc = Document(document_id="test-doc", logical_title="Synthetic example")
    revision = DocumentRevision(
        revision_id="test-rev",
        document_id=doc.document_id,
        asset_id=pdf.asset_id,
        revision_label="A",
    )
    service.register_revision(doc, revision)
    service.register_revision(doc, revision)
    service.ingest_pair(*pair[:2])
    assert service.get_asset(pdf.asset_id).revisions == [revision]


def test_physical_page_and_block_precision(service, pair):
    imported = service.ingest_pair(*pair[:2])
    asset = imported.source.asset_id
    first = service.get_pdf_page(asset, 1)
    empty = service.get_pdf_page(asset, 2)
    last = service.get_pdf_page(asset, 3)
    assert [p.physical_page for p in (first, empty, last)] == [1, 2, 3]
    assert all(p.page_count == 3 for p in (first, empty, last))
    assert first.page_box == (0.0, 0.0, 200.0, 200.0)
    assert service.get_pdf_bytes(asset) == pair[0].read_bytes()
    for page in (0, 4):
        with pytest.raises(IraError, match="invalid_location"):
            service.get_pdf_page(asset, page)
    hit = service.search_evidence("OLT").items[0]
    assert hit.precision == "block"
    assert hit.pdf_bbox == pytest.approx((16, 142, 130, 166))
    assert hit.content_check == "not_reviewed"
    assert hit.location_basis == "unique_pdf_text_region"


@pytest.mark.parametrize("query", ["设备清单", "设备", "GPON", "olt", "ONU", "GE(0)", "GE(O)"])
def test_literal_search_real_results(service, pair, query):
    imported = service.ingest_pair(*pair[:2])
    result = service.search_evidence(query, source_asset_id=imported.source.asset_id)
    assert result.items
    assert result.backend == ("like" if len(query) < 3 else "fts5")
    assert result.code is None


def test_search_filters_and_no_hit(service, pair):
    imported = service.ingest_pair(*pair[:2])
    assert service.search_evidence("ONU", physical_page=1).code == "no_evidence_found"
    result = service.search_evidence("ONU", physical_page=3, kind="table")
    assert len(result.items) == 1
    assert result.items[0].physical_page == 3
    assert result.items[0].source_asset_id == imported.source.asset_id
    assert service.search_evidence("GE(0)", mode="exact").code == "no_evidence_found"
    assert len(service.search_evidence("OLT GPON GE(0)", mode="exact").items) == 1
    assert not service.search_evidence("%_").items
    with pytest.raises(IraError, match="invalid_input"):
        service.search_evidence("   ")


def test_table_continuation_preserves_both_raw_pointers(service, pair):
    service.ingest_pair(*pair[:2])
    first, continuation = service.search_evidence("设备", kind="table").items
    assert first.raw_content == pair[2]["pages"][0]["blocks"][1]["content"]
    assert continuation.raw_content == pair[2]["pages"][2]["blocks"][1]["content"]
    assert (first.source_pointer, continuation.source_pointer) == (
        "/pages/0/blocks/1",
        "/pages/2/blocks/1",
    )
    assert (first.physical_page, continuation.physical_page) == (1, 3)
    assert not first.continues_prev and continuation.continues_prev
    assert "continuation_unverified" in continuation.risk
    assert "cell_provenance_unavailable" in continuation.risk
    assert continuation.precision == "page" and continuation.pdf_bbox is None
    assert "cell_bbox" not in continuation.model_dump()


@pytest.mark.parametrize(
    ("kind", "code"),
    [
        ("broken_json", "parse_failed"),
        ("unknown_shape", "unsupported_format"),
        ("unknown_version", "unsupported_format"),
        ("claimed_mismatch", "source_mismatch"),
        ("out_of_bounds", "invalid_location"),
        ("broken_pdf", "invalid_pdf"),
        ("missing", "source_missing"),
        ("traversal", "permission_denied"),
        ("outside", "permission_denied"),
        ("duplicate_key", "parse_failed"),
        ("unpaired_surrogate", "parse_failed"),
        ("deep_json", "resource_limit"),
    ],
)
def test_failed_imports_leave_no_rows_or_sensitive_logs(
    service, pair, tmp_path, caplog, kind, code
):
    pdf, parsed, payload = pair
    if kind == "broken_json":
        parsed.write_bytes(b'{"SECRET-SYNTHETIC":')
    elif kind == "unknown_shape":
        parsed.write_text('{"SECRET-SYNTHETIC":true}')
    elif kind == "unknown_version":
        payload["metadata"]["producer"]["version"] = "5.0.0"
        parsed.write_text(json.dumps(payload))
    elif kind == "claimed_mismatch":
        payload["metadata"]["source_sha256"] = "0" * 64
        parsed.write_text(json.dumps(payload))
    elif kind == "out_of_bounds":
        payload["pages"][2]["page_idx"] = 3
        parsed.write_text(json.dumps(payload))
    elif kind == "broken_pdf":
        pdf.write_bytes(b"%PDF-1.7\nSECRET-SYNTHETIC")
    elif kind == "missing":
        pdf = pdf.with_name("missing.pdf")
    elif kind == "traversal":
        pdf = pdf.parent / ".." / pdf.parent.name / pdf.name
    elif kind == "duplicate_key":
        parsed.write_bytes(b'{"pages": [], "pages": []}')
    elif kind == "unpaired_surrogate":
        payload["pages"][0]["blocks"][0]["content"] = "\ud800"
        parsed.write_text(json.dumps(payload))
    elif kind == "deep_json":
        value = "SECRET-SYNTHETIC"
        for _ in range(200):
            value = [value]
        payload["pages"][0]["blocks"][0]["content"] = value
        parsed.write_text(json.dumps(payload))
    else:
        outside = tmp_path / "SECRET-SYNTHETIC.pdf"
        outside.write_bytes(pdf.read_bytes())
        pdf = outside
    with pytest.raises(IraError, match=code):
        service.ingest_pair(pdf, parsed)
    with sqlite3.connect(service.settings.database) as db:
        for table in ("source_assets", "parse_runs", "evidence"):
            assert db.execute(f"SELECT count(*) FROM {table}").fetchone() == (0,)
    assert "SECRET-SYNTHETIC" not in caplog.text
    assert str(tmp_path) not in caplog.text


def test_source_replacement_detected_on_page_read(service, pair):
    imported = service.ingest_pair(*pair[:2])
    pdf = pair[0]
    before = pdf.read_bytes()
    assert before == service.get_pdf_bytes(imported.source.asset_id)
    assert before == pdf.read_bytes()
    pdf.write_bytes(before + b"\n% change")
    with pytest.raises(IraError, match="source_mismatch"):
        service.get_pdf_page(imported.source.asset_id, 1)


def test_reimport_is_atomic_under_concurrency(service, pair):
    with ThreadPoolExecutor(3) as executor:
        results = list(executor.map(lambda _: service.ingest_pair(*pair[:2]), range(6)))
    assert all(result == results[0] for result in results)
    with sqlite3.connect(service.settings.database) as db:
        assert db.execute("SELECT count(*) FROM source_assets").fetchone() == (2,)
        assert db.execute("SELECT count(*) FROM parse_runs").fetchone() == (1,)
        assert db.execute("SELECT count(*) FROM evidence").fetchone() == (4,)
        assert db.execute("PRAGMA foreign_key_check").fetchall() == []
