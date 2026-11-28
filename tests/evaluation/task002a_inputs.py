"""Source-only projection for the development corpus. No Gold inputs here."""

import csv
import io
import json
import textwrap
from contextlib import closing
from dataclasses import dataclass
from html import escape
from pathlib import Path

from openpyxl import Workbook
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.cidfonts import UnicodeCIDFont
from reportlab.pdfgen.canvas import Canvas

Anchor = tuple[str | int, ...]

# Explicit tester-supplied column mapping, using input headers only.
SHEET_MAP = {
    "设备类别": "equipment_class",
    "厂家型号": "manufacturer_model",
    "配置变体": "configuration_variant",
    "工程范围": "scope",
    "站点组": "site_group",
    "接口配置": "config",
    "数量": "quantity",
    "单位": "unit",
    "计数口径": "count_basis",
    "来源版本": "source_revision",
    "适用条件": "condition",
    "属性": "property",
    "约束表达式": "expr",
}


class UnsupportedInput(ValueError):
    """A declared input cannot be faithfully projected into the current adapter."""


@dataclass(frozen=True)
class Inputs:
    pdf: bytes
    parsed: bytes
    spreadsheet: bytes
    filename: str
    mapping: dict[str, str]
    sheet: str | None
    requirements: dict[tuple[str, int], Anchor]
    declarations: dict[int, Anchor]
    columns: dict[str, int]


def _string(value):
    return "" if value is None else str(value)


def _page(item):
    index = item["page_index"]
    if type(index) is not int or not 0 <= index < 200 or item["physical_page"] != index + 1:
        raise UnsupportedInput("invalid_page_metadata")
    return index


def _documents(source):
    items = [(_page(item), "section", item) for item in source["sections"]]
    items.extend((_page(item), "table", item) for item in source["tables"])
    if not items:
        raise UnsupportedInput("no_document_blocks")
    pages = [{"page_idx": i, "blocks": []} for i in range(max(x[0] for x in items) + 1)]
    lines = [[] for _ in pages]
    anchors = {}
    for page, kind, entry in items:
        block_list = pages[page]["blocks"]
        pointer = f"/pages/{page}/blocks/{len(block_list)}"
        prefix = (kind, source["source_id"], page + 1)
        if kind == "section":
            anchors[pointer, 0] = (*prefix, entry["heading"])
            block = {"type": "text", "content": entry["text"]}
            lines[page].extend([entry["heading"], entry["text"]])
        else:
            columns = entry["columns"]
            markup = ["<table>"]
            if columns:
                markup.append("<tr>" + "".join(f"<th>{escape(c)}</th>" for c in columns) + "</tr>")
                lines[page].append(" | ".join(columns))
            for ordinal, row in enumerate(entry["rows"], 1):
                cells = row["cells"]
                if isinstance(cells, dict) and columns:
                    values = [_string(cells.get(k)) for k in columns]
                elif isinstance(cells, list) and not columns:
                    values = list(map(_string, cells))
                else:
                    raise UnsupportedInput("unsupported_table_row_shape")
                markup.append("<tr>" + "".join(f"<td>{escape(v)}</td>" for v in values) + "</tr>")
                lines[page].append(" | ".join(values))
                anchors[pointer, ordinal] = (*prefix, entry["table_id"], str(row["row_id"]))
            block = {"type": "table", "content": "".join(markup) + "</table>"}
            if "continues_prev" in entry:
                block["continues_prev"] = entry["continues_prev"]
            if entry.get("caption"):
                block["captions"] = [{"content": entry["caption"]}]
                lines[page].append(entry["caption"])
            if entry.get("footnotes"):
                block["footnotes"] = [{"content": note["text"]} for note in entry["footnotes"]]
                lines[page].extend(note["text"] for note in entry["footnotes"])
        block_list.append(block)
    if len(set(anchors.values())) != len(anchors):
        raise UnsupportedInput("nonunique_requirement_locators")

    result = io.BytesIO()
    pdfmetrics.registerFont(UnicodeCIDFont("STSong-Light"))
    canvas = Canvas(result, pagesize=(650, 900), invariant=True)
    for page_lines in lines:
        wrapped = [piece for line in page_lines for piece in textwrap.wrap(line, 65)]
        height = max(900, 60 + 15 * len(wrapped))
        canvas.setPageSize((650, height))
        canvas.setFont("STSong-Light", 10)
        for ordinal, line in enumerate(wrapped):
            canvas.drawString(24, height - 30 - 15 * ordinal, line)
        canvas.showPage()
    canvas.save()
    parsed = json.dumps(
        {
            "metadata": {"producer": {"name": "MinerU", "version": "4.2.0"}},
            "extensions": {"synthetic_pair": True, "fixture_origin": "DATA-R1 source projection"},
            "is_full_document": True,
            "pages": pages,
        },
        ensure_ascii=False,
    ).encode()
    return result.getvalue(), parsed, anchors


def _listing(source):
    kind = Path(source["file_name"]).suffix.casefold()
    if kind not in (".csv", ".xlsx"):
        raise UnsupportedInput("unsupported_spreadsheet_format")
    if len(source["sheets"]) != 1:
        raise UnsupportedInput("requires_explicit_sheet_selection")
    (sheet,) = source["sheets"]
    label = sheet["sheet_or_section"]
    headers = sheet["header"]
    mapping = {SHEET_MAP[h]: h for h in headers if h in SHEET_MAP}
    positions = {h: n for n, h in enumerate(headers, 1)}
    anchors = {}
    prefix = ("sheet", source["source_id"], label)
    if kind == ".csv":
        result = io.StringIO(newline="")
        writer = csv.writer(result)
        writer.writerow(headers)
        for row in sheet["rows"]:
            # Physical file position differs from authored row ID after a permutation.
            physical = result.getvalue().count("\n") + 1
            anchors[physical] = (*prefix, str(row["row_id"]))
            writer.writerow([row["cells"].get(h) for h in headers])
        data = result.getvalue().encode()
    else:
        result = io.BytesIO()
        with closing(Workbook()) as workbook:
            page = workbook.active
            page.title = label
            page.append(headers)
            for physical, row in enumerate(sheet["rows"], 2):
                if any(row.get("cached_value_available", {}).values()):
                    raise UnsupportedInput("xlsx_formula_cache_projection_unsupported")
                anchors[physical] = (*prefix, str(row["row_id"]))
                for column, h in enumerate(headers, 1):
                    value = row["cells"].get(h)
                    cell = page.cell(physical, column, value)
                    if isinstance(value, str) and row.get("cell_types", {}).get(h) != "formula":
                        cell.data_type = "s"
            workbook.save(result)
        data = result.getvalue()
    if len(set(anchors.values())) != len(anchors):
        raise UnsupportedInput("nonunique_declaration_locators")
    return (
        data,
        "declarations" + kind,
        mapping,
        label if kind == ".xlsx" else None,
        anchors,
        positions,
    )


def materialize(case: dict) -> Inputs:
    if case.get("data_kind") != "synthetic_public":
        raise UnsupportedInput("requires_synthetic_public_input")
    source, listing = case["requirement_source"], case["declaration_source"]
    if not source.get("source_id") or not listing.get("source_id"):
        raise UnsupportedInput("source_registration_missing")
    if source.get("revision_relations"):
        raise UnsupportedInput("explicit_revision_relation_projection_unsupported")
    pdf, parsed, requirements = _documents(source)
    sheet, filename, mapping, label, declarations, columns = _listing(listing)
    return Inputs(pdf, parsed, sheet, filename, mapping, label, requirements, declarations, columns)
