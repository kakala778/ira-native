import hashlib
import json
import sqlite3
from io import BytesIO

import pytest
from PIL import Image
from reportlab.pdfgen.canvas import Canvas

from ira_native.application.service import EvidenceService
from ira_native.errors import IraError
from ira_native.settings import Settings


def write_json(path, payload):
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")


@pytest.mark.parametrize(
    ("variant", "risk"),
    [
        ("unknown_protocol", "unsupported_coord_system"),
        ("shifted", "block_location_unverified"),
        ("duplicate", "block_location_unverified"),
        ("out_of_range", "invalid_bbox"),
        ("rotated", "page_geometry_unverified"),
        ("cropped", "page_geometry_unverified"),
        ("invisible", "block_location_unverified"),
        ("transparent", "block_location_unverified"),
    ],
)
def test_unsafe_bbox_is_downgraded(pair, tmp_path, variant, risk):
    pdf, parsed, payload = pair
    block = payload["pages"][0]["blocks"][0]
    if variant == "unknown_protocol":
        block["coord_system"] = "untrusted_points"
    elif variant == "shifted":
        block["bbox"] = [0.08, 0.67, 0.65, 0.79]
    elif variant == "out_of_range":
        block["bbox"] = [-0.5, 0.2, 0.65, 0.79]
    else:
        canvas = Canvas(str(pdf), pagesize=(200, 200), invariant=True)
        if variant == "rotated":
            canvas.setPageRotation(90)
        if variant == "cropped":
            canvas.setCropBox((20, 20, 190, 190))
        if variant == "invisible":
            text = canvas.beginText(20, 150)
            text.setTextRenderMode(3)
            text.textOut("OLT GPON GE(0)")
            canvas.drawText(text)
        else:
            if variant == "transparent":
                canvas.setFillAlpha(0)
            canvas.drawString(20, 150, "OLT GPON GE(0)")
        if variant == "duplicate":
            canvas.drawString(20, 100, "OLT GPON GE(0)")
        canvas.showPage()
        if variant == "rotated":
            canvas.setPageRotation(0)
        canvas.showPage()
        canvas.drawString(20, 150, "ONU LAST")
        canvas.showPage()
        canvas.save()
    write_json(parsed, payload)
    with EvidenceService(Settings(tmp_path / "ira.sqlite", (pdf.parent,))) as service:
        service.ingest_pair(pdf, parsed)
        hit = service.search_evidence("OLT").items[0]
        page = service.get_evidence_page(hit.evidence_id)
        assert hit.precision == page.precision == "page"
        assert hit.pdf_bbox is page.pdf_bbox is None
        assert hit.native_bbox == block["bbox"]
        assert risk in hit.risk
        assert page.physical_page == 1
        if variant == "rotated":
            assert page.rotation == 90
        if variant == "cropped":
            assert page.page_box == (20.0, 20.0, 190.0, 190.0)


def test_rendered_first_page_places_verified_text_within_bbox(pair, tmp_path):
    pdf = pair[0]
    with EvidenceService(Settings(tmp_path / "ira.sqlite", (pdf.parent,))) as service:
        service.ingest_pair(*pair[:2])
        hit = service.search_evidence("OLT").items[0]
        with Image.open(BytesIO(service.get_pdf_page_image(hit.source_asset_id, 1))) as image:
            # Independent expected pixel rectangle, including the baseline from the synthetic PDF.
            patch = image.convert("L").crop((24, 51, 195, 87))
            assert patch.getextrema()[0] < 50
            assert image.convert("L").crop((24, 160, 195, 190)).getextrema() == (255, 255)


def test_like_fallback_exact_literal_matching_and_empty_artifact(pair, tmp_path):
    pdf, parsed, payload = pair
    settings = Settings(tmp_path / "ira.sqlite", (pdf.parent,), search_backend="like")
    with EvidenceService(settings) as service:
        service.ingest_pair(pdf, parsed)
        for query, count in [
            ("设备", 2),
            ("GPON", 1),
            ("GE(0)", 1),
            ("GE(O)", 1),
            ("%", 0),
            ("_GPON", 0),
            ('" OR GPON', 0),
        ]:
            result = service.search_evidence(query)
            assert result.backend == "like" and len(result.items) == count
        for page in payload["pages"]:
            page["blocks"] = []
        write_json(parsed, payload)
        result = service.ingest_pair(pdf, parsed)
        assert result.evidence_count == 0
        assert service.search_evidence("OLT", parse_run_id=result.parse_run.parse_run_id).code == (
            "no_evidence_found"
        )


def test_claim_and_match_do_not_attest_provenance(pair, tmp_path):
    pdf, parsed, payload = pair
    payload["metadata"]["source_sha256"] = hashlib.sha256(pdf.read_bytes()).hexdigest()
    write_json(parsed, payload)
    with EvidenceService(Settings(tmp_path / "ira.sqlite", (pdf.parent,))) as service:
        result = service.ingest_pair(pdf, parsed, declared=False)
        assert result.parse_run.binding_state == "unknown"
        assert "untrusted_artifact_sha256_claim_matches" in result.parse_run.binding_basis
        assert service.search_evidence("OLT").items[0].source_state == "unverified"


def test_reopening_without_allowlist_revokes_access_to_both_records_and_original(pair, tmp_path):
    pdf = pair[0]
    database = tmp_path / "ira.sqlite"
    with EvidenceService(Settings(database, (pdf.parent,))) as service:
        result = service.ingest_pair(*pair[:2])
        hit = service.search_evidence("OLT").items[0]
    with EvidenceService(Settings(database)) as service:
        assert not service.search_evidence("OLT").items
        with pytest.raises(IraError, match="permission_denied"):
            service.get_pdf_bytes(result.source.asset_id)
        with pytest.raises(IraError, match="permission_denied"):
            service.get_evidence(hit.evidence_id)
        with pytest.raises(IraError, match="permission_denied"):
            service.get_run(result.parse_run.parse_run_id)


def test_failed_write_rolls_back_sources_and_run(pair, tmp_path):
    pdf = pair[0]
    database = tmp_path / "ira.sqlite"
    with EvidenceService(Settings(database, (pdf.parent,))) as service:
        with sqlite3.connect(database) as connection:
            connection.execute("""
                CREATE TRIGGER fail_evidence BEFORE INSERT ON evidence
                BEGIN SELECT RAISE(ABORT, 'synthetic write failure'); END
            """)
        with pytest.raises(IraError, match="storage_error"):
            service.ingest_pair(*pair[:2])
        with sqlite3.connect(database) as connection:
            for table in ("source_assets", "parse_runs", "evidence"):
                assert connection.execute(f"SELECT count(*) FROM {table}").fetchone() == (0,)
            connection.execute("DROP TRIGGER fail_evidence")
        assert service.ingest_pair(*pair[:2]).evidence_count == 4


def test_source_in_two_allowed_roots_does_not_rebind_silently(pair, tmp_path):
    pdf, parsed, _ = pair
    database = tmp_path / "ira.sqlite"
    with EvidenceService(Settings(database, (pdf.parent,))) as service:
        original = service.ingest_pair(pdf, parsed)
    other = tmp_path / "other"
    other.mkdir()
    new_pdf, new_json = other / pdf.name, other / parsed.name
    new_pdf.write_bytes(pdf.read_bytes())
    new_json.write_bytes(parsed.read_bytes())
    with EvidenceService(Settings(database, (other,))) as service:
        with pytest.raises(IraError, match="permission_denied"):
            service.ingest_pair(new_pdf, new_json)
        with pytest.raises(IraError, match="permission_denied"):
            service.get_pdf_bytes(original.source.asset_id)


def test_a_duplicate_copy_cannot_publish_a_run_with_a_stale_original(pair, tmp_path):
    pdf, parsed, payload = pair
    duplicate = pdf.with_name("copy.pdf")
    duplicate.write_bytes(pdf.read_bytes())
    with EvidenceService(Settings(tmp_path / "ira.sqlite", (pdf.parent,))) as service:
        original = service.ingest_pair(pdf, parsed)
        pdf.write_bytes(pdf.read_bytes() + b"\n% replaced")
        second = parsed.with_name("second.json")
        payload["extensions"]["other_parse"] = True
        write_json(second, payload)
        with pytest.raises(IraError, match="source_mismatch"):
            service.ingest_pair(duplicate, second)
        with sqlite3.connect(service.settings.database) as connection:
            assert connection.execute("SELECT count(*) FROM parse_runs").fetchone() == (1,)
            assert connection.execute("SELECT count(*) FROM source_assets").fetchone() == (2,)
        assert service.get_run(original.parse_run.parse_run_id) == original.parse_run
