import json

from ira_native.application.candidates import diagnose_requirements, extract_requirements
from ira_native.application.service import EvidenceService
from ira_native.settings import Settings


def test_structured_cells_preserve_exact_source_path(pair, tmp_path):
    pdf, parsed, payload = pair
    table = payload["pages"][0]["blocks"][1]
    pointer = "/pages/0/blocks/1"
    names = ["equipment_class", "configuration_variant", "scope"]
    table["structured_table"] = {
        "header_state": "explicit",
        "headers": names,
        "rows": [
            [
                {"text": value, "native_pointer": f"{pointer}/structured_table/rows/0/{index}"}
                for index, value in enumerate(["ONU", "model-variant", " Sector-A "])
            ]
        ],
    }
    parsed.write_text(json.dumps(payload), encoding="utf-8")
    with EvidenceService(Settings(tmp_path / "db.sqlite", (pdf.parent,))) as service:
        run = service.ingest_pair(pdf, parsed).parse_run
        block = next(x for x in service.list_run_evidence(run.parse_run_id) if x.kind == "table")
        (candidate,) = extract_requirements(block)
        scope = next(f for f in candidate["field_evidence"] if f["field_name"] == "scope")
        assert scope["raw_span"] == " Sector-A "
        assert scope["physical_page"] == 1
        assert scope["column_index"] == 3
        assert scope["native_pointer"] == f"{pointer}/structured_table/rows/0/2"
        assert scope["support_state"] == "candidate"


def test_recovery_projection_and_headerless_continuation_are_not_read_as_original(pair, tmp_path):
    pdf, parsed, payload = pair
    table = payload["pages"][0]["blocks"][1]
    table["content"] = "<table><tr><th>设备类别</th><th>配置变体</th><th>工程范围</th></tr></table>"
    extension = {
        "adapter_version": "exp03-table-adapter/1",
        "recoveries": [
            {
                "b0_native_pointer": "/pages/0/blocks/1",
                "selected_native_pointer": "pymupdf:page1:find_tables:t0",
                "header_is_original": False,
                "projection": {"source_content": "experimental_projection"},
            }
        ],
    }
    payload["extensions"]["exp03_table_recovery"] = extension
    with EvidenceService(Settings(tmp_path / "db.sqlite", (pdf.parent,))) as service:
        for projected, reason in (
            (True, "TABLE_EXPERIMENTAL_PROJECTION"),
            (False, "CONTINUATION_HEADER_UNCONFIRMED"),
        ):
            if not projected:
                extension["recoveries"][0]["projection"] = {}
            parsed.write_text(json.dumps(payload), encoding="utf-8")
            run = service.ingest_pair(pdf, parsed).parse_run
            blocks = service.list_run_evidence(run.parse_run_id)
            block = next(x for x in blocks if x.kind == "table")
            assert diagnose_requirements([block])["reason_counts"][reason] == 1
            assert "continuation_unverified" in block.risk
            assert block.native_context["table_recovery"]["selected_native_pointer"].startswith(
                "pymupdf:"
            )


def test_raw_headings_and_operator_cells_stay_separate(pair, tmp_path):
    pdf, parsed, payload = pair
    table = payload["pages"][0]["blocks"][1]
    table["content"] = (
        "<table><tr><th>设备类别</th><th>工程范围</th><th>属性</th><th>比较符</th>"
        "<th>数值</th><th>单位</th><th>要求等级</th></tr><tr><td>ONU</td>"
        "<td> 合成区域 </td><td>ge_ports</td><td>≥</td><td>2</td><td>个</td>"
        "<td>强制</td></tr></table>"
    )
    payload["pages"][0]["blocks"][0]["content"] = "每座泵站设置 OLT 1 台，按泵站合计，单位为台。"
    parsed.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    with EvidenceService(Settings(tmp_path / "db.sqlite", (pdf.parent,))) as service:
        run = service.ingest_pair(pdf, parsed).parse_run
        by_class = {
            req["equipment_class"]: req
            for block in service.list_run_evidence(run.parse_run_id)
            for req in extract_requirements(block)
        }
        table_req = by_class["ONU"]
        assert (table_req["expr"], table_req["unit"], table_req["strength"]) == (
            ">=2",
            "port",
            "mandatory",
        )
        assert table_req["field_derivations"]["expr"] == ["operator", "value"]
        by_field = {f["field_name"]: f for f in table_req["field_evidence"]}
        assert by_field["operator"]["raw_span"] == "≥"
        assert by_field["value"]["raw_heading"] == "数值"
        assert by_field["scope"]["raw_cell_text"] == " 合成区域 "
        assert by_class["OLT"]["raw"]["quantity"] == "1"
        assert by_class["OLT"]["strength"] == "unknown"
        assert by_class["OLT"]["expr"] is None
