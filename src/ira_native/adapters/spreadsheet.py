"""Mapped CSV/XLSX sources with original row and cell references."""

import csv
import io
import json
from contextlib import closing
from pathlib import Path
from xml.etree.ElementTree import ParseError
from zipfile import BadZipFile, ZipFile

from defusedxml.common import DefusedXmlException
from openpyxl import load_workbook
from openpyxl.utils.exceptions import InvalidFileException

from ira_native.errors import IraError

FIELDS = set(
    (
        "item_id equipment_class manufacturer_model configuration_variant scope site_group "
        "quantity unit count_basis config property expr condition source_revision"
    ).split()
)
REQUIRED = {"equipment_class", "configuration_variant", "scope"}


def _rows(data, kind, sheet, encoding):
    if kind == "csv":
        if sheet is not None or encoding not in ("utf-8", "utf-8-sig", "gb18030"):
            raise IraError("invalid_input")
        reader = csv.reader(io.StringIO(data.decode(encoding), newline=""), strict=True)
        while True:
            start = reader.line_num + 1
            row = next(reader, None)
            if row is None:
                return
            yield None, start, reader.line_num, row, {}, {}
    with ZipFile(io.BytesIO(data)) as archive:
        members = archive.infolist()
        if len(members) > 2048 or sum(m.file_size for m in members) > 64 * 1024**2:
            raise IraError("resource_limit")
        if any("vbaproject" in m.filename.casefold() for m in members):
            raise IraError("unsupported_format")
    book = load_workbook(io.BytesIO(data), read_only=True, data_only=False, keep_links=False)
    try:
        if (sheet is None and len(book.sheetnames) != 1) or (
            sheet is not None and sheet not in book.sheetnames
        ):
            raise IraError("invalid_input")
        page = book[sheet or book.sheetnames[0]]
        if page.max_column and page.max_column > 256:
            raise IraError("resource_limit")
        page.reset_dimensions()
        with closing(
            load_workbook(io.BytesIO(data), read_only=True, data_only=True, keep_links=False)
        ) as cached_book:
            cached_page = cached_book[page.title]
            cached_page.reset_dimensions()
            cached_rows = cached_page.iter_rows(values_only=True)
            for number, (row, cached) in enumerate(
                zip(page.iter_rows(), cached_rows, strict=True), 1
            ):
                flags = {i: c.data_type for i, c in enumerate(row) if c.data_type in ("f", "e")}
                availability = {
                    i: i < len(cached) and cached[i] is not None
                    for i, cell_type in flags.items()
                    if cell_type == "f"
                }
                values = [
                    c.value if c.value is None or isinstance(c.value, (str, int)) else str(c.value)
                    for c in row
                ]
                yield page.title, number, number, values, flags, availability
    finally:
        book.close()


def read_sheet(
    data: bytes,
    filename: str,
    mapping: dict[str, str],
    sheet: str | None,
    encoding: str,
    max_rows: int,
) -> tuple[str, list[dict]]:
    kind = Path(filename).suffix.lower().lstrip(".")
    if kind not in ("csv", "xlsx"):
        raise IraError("unsupported_format")
    iterator = _rows(data, kind, sheet, encoding)
    try:
        header = next(iterator, None)
        if header is None:
            raise IraError("parse_failed")
        headers = [str(v).strip() if v is not None else "" for v in header[3]]
        names = [h for h in headers if h]
        if header[4] or len(names) != len(set(names)) or len(headers) > 256:
            raise IraError("parse_failed")
        mapped = dict(mapping) if mapping else {h: h for h in names if h in FIELDS}
        if (
            not REQUIRED <= mapped.keys()
            or not mapped.keys() <= FIELDS
            or len(set(mapped.values())) != len(mapped)
            or any(name not in names for name in mapped.values())
        ):
            raise IraError("invalid_input")
        indices = {field: headers.index(name) for field, name in mapped.items()}
        result, seen = [], set()
        for sheet_name, row_id, end_row, row, flags, availability in iterator:
            if end_row > max_rows + 1 or len(row) > 256:
                raise IraError("resource_limit")
            raw = {key: row[i] if i < len(row) else None for key, i in indices.items()}
            empty = not any(v is not None and str(v).strip() for v in row)
            risk = ["empty_row"] if empty else []
            fields = [
                {
                    "field_name": field,
                    "column_index": i + 1,
                    "column": mapped[field],
                    "raw_span": raw[field],
                    "cell_type": flags.get(i),
                    "cached_value_available": availability.get(i),
                    "support_state": "insufficient"
                    if i in flags or raw[field] in (None, "")
                    else "candidate",
                }
                for field, i in indices.items()
            ]
            if "f" in flags.values():
                risk.append("formula_unresolved")
            if "e" in flags.values():
                risk.append("spreadsheet_cell_error")
            key = json.dumps({k: v for k, v in raw.items() if k != "item_id"}, sort_keys=True)
            if key in seen and not empty:
                risk.append("duplicate_row")
            seen.add(key)
            result.append(
                {
                    "row_id": row_id,
                    "row_end": end_row,
                    "sheet_or_section": sheet_name,
                    "raw": raw,
                    "original_cells": row,
                    "risk": risk,
                    "field_evidence": fields,
                }
            )
        return kind, result
    except (
        BadZipFile,
        OSError,
        InvalidFileException,
        ValueError,
        KeyError,
        ParseError,
        DefusedXmlException,
        csv.Error,
    ):
        raise IraError("parse_failed") from None
    finally:
        iterator.close()
