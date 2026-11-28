import json

from ira_native.application.service import EvidenceService
from ira_native.settings import Settings


def test_mineru_native_titles_and_annotations_are_retrievable(pair, tmp_path):
    pdf, parsed, payload = pair
    page = payload["pages"][0]
    page["blocks"][0].update(type="paragraph_title", level=2)
    table = page["blocks"][1]
    table.update(
        captions=[{"bbox": [0.1, 0.3, 0.6, 0.32], "content": "合成题注"}],
        footnotes=[{"bbox": [0.1, 0.6, 0.6, 0.7], "content": "示例仅用于测试"}],
        image_source="images/generated.jpg",
    )
    parsed.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    with EvidenceService(Settings(tmp_path / "test.sqlite", (pdf.parent,))) as app:
        result = app.ingest_pair(pdf, parsed)
        assert result.parse_run.adapter_version == "mineru-structured-content/3"
        (title,) = app.search_evidence("OLT", kind="paragraph_title").items
        assert title.precision == "block"
        assert title.native_context == {"level": 2}
        assert "unknown_block_kind" not in title.risk
        (footnote,) = app.search_evidence("仅用于测试").items
        (caption,) = app.search_evidence("合成题注").items
        assert footnote.evidence_id == caption.evidence_id
        assert footnote.raw_content == table["content"]
        assert footnote.source_pointer == "/pages/0/blocks/1"
        assert footnote.raw_content.find("示例仅用于测试") == -1
        assert footnote.native_context == {
            key: table[key] for key in ("captions", "footnotes", "image_source")
        }
        assert footnote.precision == "page" and footnote.pdf_bbox is None
        assert not app.search_evidence("generated.jpg").items
