"""Recognize explicit GPON fields without inferring headers or force."""

import re
from collections import Counter
from collections.abc import Iterable, Iterator
from dataclasses import asdict, dataclass, field

from bs4 import BeautifulSoup

from ira_native.domain.attributes import normalize_attribute
from ira_native.domain.candidate_matching import declaration_fields, port_type
from ira_native.domain.models import ANNOTATION_RISK, Evidence
from ira_native.domain.values import FieldValue, ValueState, field_value
from ira_native.domain.values import parse_configuration as _parse_configuration

_FIELDS = set(
    (
        "req_id equipment_class manufacturer_model configuration_variant scope site_group "
        "quantity unit count_basis strength quantity_role config property expr operator value condition "
        "source_revision inclusion_status requirement_slot technical_value technical_unit "
        "applicability presence part_number"
    ).split()
)
_HEADINGS = {
    "设备类别": "equipment_class",
    "厂家型号": "manufacturer_model",
    "配置变体": "configuration_variant",
    "配置": "configuration_variant",
    "工程范围": "scope",
    "区域": "scope",
    "站点组": "site_group",
    "数量": "quantity",
    "单位": "unit",
    "计数口径": "count_basis",
    "要求效力": "strength",
    "要求等级": "strength",
    "数量角色": "quantity_role",
    "接口配置": "config",
    "属性": "property",
    "约束表达式": "expr",
    "比较符": "operator",
    "数值": "value",
    "适用条件": "condition",
    "来源版本": "source_revision",
    "范围状态": "inclusion_status",
    "需求槽位": "requirement_slot",
    "适用性": "applicability",
    "配置状态": "presence",
    "料号": "part_number",
    "部件号": "part_number",
}
_STRENGTH = {"强制": "mandatory", "参考": "reference", "信息性": "informative"}
_REQUIRED_HEADERS = {"equipment_class", "scope"}
_OPERATOR = {"=": "=", ">=": ">=", "<=": "<=", "≥": ">=", "≤": "<=", ">": ">", "<": "<"}
_QUANTITY_ROLES_BY_OPERATOR = {"=": "exact", ">=": "minimum"}
_PROSE_OPERATORS = _OPERATOR | {
    "不少于": ">=",
    "不小于": ">=",
    "不低于": ">=",
    "至少": ">=",
    "不多于": "<=",
    "不大于": "<=",
    "不高于": "<=",
    "至多": "<=",
    "大于": ">",
    "小于": "<",
    "等于": "=",
    "为": "=",
}
_DECIMAL = re.compile(r"[+-]?(?:[0-9]{1,24}(?:\.[0-9]{1,24})?|\.[0-9]{1,24})")
_SCALAR_EXPR = re.compile(rf"(>=|<=|=|>|<|≥|≤)\s*({_DECIMAL.pattern})")
_SITE_QUANTITY = re.compile(
    r"(?P<count_basis>每座(?P<site>[^，。；]{1,24}?))设置\s*(?P<equipment_class>OLT|ONU)\s*(?P<quantity>[0-9]{1,12})\s*(?P<unit>台)(?:，按(?P=site)合计，单位为台)?。?"
)
_CLASS = re.compile(r"[A-Z][A-Z0-9_]{1,23}")
_MODEL = r"[A-Z][A-Z0-9]*(?:[-_][A-Z0-9]+)+"
_SUBJECT = (
    r"(?:第\s*[0-9]+(?:\.[0-9]+)*\s*[条款]\s*)?"
    rf"(?:(?:(?P<equipment_class>OLT|ONU|[A-Z][A-Z0-9_]{{1,23}}(?=\s+{_MODEL}))\s+)?"
    rf"(?P<manufacturer_model>{_MODEL})\s*(?:的\s*)?|(?P<category>OLT|ONU)\s*(?:的\s*)?)?"
    r"(?:(?P<requirement_slot>[A-Z][0-9]{1,6})(?![A-Z0-9_-])\s*)?"
)
_PROSE_PROPERTIES = {
    "设备数量": "device_quantity",
    "最大功耗": "maximum_power",
    "光纤接口": "fiber_interface",
    "电源模块": "power_module",
}
_PROSE_UNITS = {
    "device_quantity": {"台", "端", "块", "套", "个"},
    "maximum_power": {"W", "kW", "mW"},
}
_PORT_PROPERTY = r"(?:GE|FE|GPON|POTS)\s*端口(?:数量|数)"
_PROSE_NUMERIC = re.compile(
    _SUBJECT
    + r"(?:(?P<count_basis>每台|每站|按站|按合同范围|合计)\s*)?"
    + rf"(?P<property>{_PORT_PROPERTY}|设备数量|最大功耗)\s*(?:应\s*)?"
    + rf"(?P<operator>{'|'.join(map(re.escape, _PROSE_OPERATORS))})\s*"
    + rf"(?P<value>{_DECIMAL.pattern})\s*(?P<unit>台|端口|端|块|套|个|kW|mW|W)[。.]?"
)
_PROSE_NOT_APPLICABLE = re.compile(
    _SUBJECT
    + r"(?P<property>光纤接口|电源模块)(?:要求)?\s*[:：]?\s*(?P<applicability>不适用)"
    + r"(?:[；;]\s*本项目(?P<presence>不设置|不配置|不包含)(?:任何)?(?P=property)(?:要求)?)?[。.]?"
)
_PROSE_ABSENCE = re.compile(
    _SUBJECT + r"(?P<presence>不设置|不配置|不包含)(?:任何)?(?P<property>光纤接口|电源模块)[。.]?"
)
_PROSE_PART = re.compile(
    _SUBJECT + r"(?P<property>料号|部件号)\s*(?:为|[:：])\s*"
    r"(?P<part_number>[A-Z0-9]+(?:[-_./][A-Z0-9]+)*)[。.]?"
)
_UNSTATED_FIELDS = {
    "数值": "value",
    "单位": "unit",
    "计数口径": "count_basis",
    "比较符": "operator",
}
_UNSTATED_LABEL = rf"(?:{'|'.join(_UNSTATED_FIELDS)})"
_PROSE_UNSTATED = re.compile(
    r"(?:表注\s*[:：]\s*)?"
    + _SUBJECT
    + r"(?:的\s*)?"
    + rf"(?P<unknown_basis>(?P<labels>{_UNSTATED_LABEL}(?:(?:、|和|及)\s*{_UNSTATED_LABEL})*)"
    + r"(?:原文)?(?:以破折号表示[，,]\s*)?(?:未给出确定值|未注明|未明确))[。.]?"
)
_COUNT_BASIS = {
    "每台": "per_device",
    "每站": "per_site",
    "按站": "per_site",
    "按合同范围": "per_contract_scope",
    "合计": "total",
}
_EXPLICIT_ABSENCE = {"explicit_absence", "不设置", "不配置", "不包含", "明确不提供"}
_EXPLICIT_NOT_APPLICABLE = {"not_applicable", "不适用"}
_NUMERIC_PLACEHOLDERS = {"—", "–", "-", "n/a", "未知", "unknown", "未注明"}
_REQUIRED_FIELDS = ("equipment_class", "config_variant", "scope")
_HEADER_REASONS = {
    "TABLE_HEADER_NOT_EXPLICIT",
    "CONTINUATION_HEADER_UNCONFIRMED",
    "TABLE_HEADER_MAPPING_FAILED",
    "TABLE_HEADER_DUPLICATE",
}
_UNSUPPORTED_TABLE_REASONS = {
    "TABLE_NOT_HTML",
    "TABLE_MULTIPLE_HTML_TABLES",
    "TABLE_NO_ROWS",
    "TABLE_INVALID_STRUCTURED_CELLS",
    "TABLE_EXPERIMENTAL_PROJECTION",
    "TABLE_ROWSPAN_UNSUPPORTED",
    "TABLE_COLSPAN_UNSUPPORTED",
}


@dataclass(frozen=True)
class _ScannedRecord:
    ordinal: int
    raw: dict = field(default_factory=dict)
    headings: dict[str, str] = field(default_factory=dict)
    problem: str | None = None
    cells: dict = field(default_factory=dict)
    spans: dict[str, tuple[int, int]] = field(default_factory=dict)
    header_evidence: Evidence | None = None
    interpreted: dict = field(default_factory=dict)
    unknown_fields: frozenset[str] = frozenset()


def _html_table_rows(content: str):
    tables = BeautifulSoup(content, "html.parser").find_all("table")
    if not tables:
        return "TABLE_NOT_HTML", []
    if len(tables) != 1:
        return "TABLE_MULTIPLE_HTML_TABLES", []
    table = tables[0]
    rows = table.find_all("tr")
    if not rows:
        return "TABLE_NO_ROWS", []
    cells = table.find_all(["td", "th"])
    for attribute, reason in (
        ("rowspan", "TABLE_ROWSPAN_UNSUPPORTED"),
        ("colspan", "TABLE_COLSPAN_UNSUPPORTED"),
    ):
        if any(cell.get(attribute, "1") != "1" for cell in cells):
            return reason, []
    return None, rows


def _scan_table(content: str, *, continuation: bool) -> tuple[str, list[_ScannedRecord]]:
    reason, rows = _html_table_rows(content)
    if reason:
        return reason, []
    header_cells = rows[0].find_all("th", recursive=False)
    if any(cell.has_attr("data-original-heading") for cell in header_cells):
        return "TABLE_EXPERIMENTAL_PROJECTION", []
    headers = [c.get_text(" ", strip=True) for c in header_cells]
    if not headers:
        return (
            "CONTINUATION_HEADER_UNCONFIRMED" if continuation else "TABLE_HEADER_NOT_EXPLICIT"
        ), []
    body = [
        [
            {"text": c.get_text(" ", strip=True), "raw_text": c.get_text()}
            for c in row.find_all("td", recursive=False)
        ]
        for row in rows[1:]
    ]
    return _scan_cells(headers, body)


def _scan_cells(
    headers: list[str], body: list[list[dict]], *, header_evidence: Evidence | None = None
) -> tuple[str, list[_ScannedRecord]]:
    names = [_HEADINGS.get(value, value) for value in headers]
    if not _REQUIRED_HEADERS <= set(names):
        return "TABLE_HEADER_MAPPING_FAILED", []
    if len(names) != len(set(names)):
        return "TABLE_HEADER_DUPLICATE", []
    headings = dict(zip(names, headers, strict=True))
    records = []
    for ordinal, row in enumerate(body, 1):
        if len(row) != len(names):
            records.append(_ScannedRecord(ordinal, problem="TABLE_ROW_WIDTH_MISMATCH"))
            continue
        cells = dict(zip(names, row, strict=True))
        records.append(
            _ScannedRecord(
                ordinal,
                {key: cell["text"] for key, cell in cells.items()},
                headings,
                cells=cells,
                header_evidence=header_evidence,
            )
        )
    return ("RECORDS_SCANNED" if records else "TABLE_EMPTY_BODY"), records


def _structured_cells(
    table: object, item: Evidence, continuation_header: Evidence | None
) -> tuple[str, list[_ScannedRecord]]:
    invalid = ("TABLE_INVALID_STRUCTURED_CELLS", [])
    if not isinstance(table, dict):
        return invalid
    header_evidence = None
    headers = table.get("headers")
    if table.get("header_state") == "inherited":
        header_evidence = continuation_header
        if headers not in (None, []):
            return "CONTINUATION_HEADER_UNCONFIRMED", []
        headers = _linked_headers(item, table, header_evidence)
    if headers is None and table.get("header_state") == "inherited":
        return "CONTINUATION_HEADER_UNCONFIRMED", []
    if table.get("header_state") not in ("explicit", "inherited"):
        return "CONTINUATION_HEADER_UNCONFIRMED", []
    rows = table.get("rows")
    if (
        not isinstance(headers, list)
        or not all(isinstance(h, str) for h in headers)
        or not isinstance(rows, list)
    ):
        return invalid
    for row_index, row in enumerate(rows):
        if not isinstance(row, list):
            return invalid
        for col_index, cell in enumerate(row):
            if not isinstance(cell, dict) or not isinstance(cell.get("text"), str):
                return invalid
            native = cell.get("native_pointer")
            if native != f"{item.source_pointer}/structured_table/rows/{row_index}/{col_index}":
                return invalid
    return _scan_cells(headers, rows, header_evidence=header_evidence)


def _linked_headers(item: Evidence, table: dict, parent: Evidence | None) -> list[str] | None:
    if (
        parent is None
        or parent.source_state in ("mismatch", "denied")
        or item.continues_prev is not True
        or parent.kind != "table"
        or table.get("continuation_header_pointer") != parent.source_pointer
        or parent.parse_run_id != item.parse_run_id
        or parent.source_asset_id != item.source_asset_id
        or parent.artifact_asset_id != item.artifact_asset_id
        or parent.physical_page + 1 != item.physical_page
    ):
        return None
    recovery = parent.native_context.get("table_recovery")
    if isinstance(recovery, dict) and (
        recovery.get("projection_input") is True or recovery.get("header_is_original") is not True
    ):
        return None
    reason, _ = _scan(parent)
    if reason not in ("RECORDS_SCANNED", "TABLE_EMPTY_BODY"):
        return None
    structured = parent.native_context.get("structured_table")
    if isinstance(structured, dict):
        reason, _ = _structured_cells(structured, parent, None)
        return structured["headers"] if reason in ("RECORDS_SCANNED", "TABLE_EMPTY_BODY") else None
    if not isinstance(parent.raw_content, str):
        return None
    _, rows = _html_table_rows(parent.raw_content)
    if not rows or rows[0].find("td", recursive=False):
        return None
    return [cell.get_text(" ", strip=True) for cell in rows[0].find_all("th", recursive=False)]


def _prose_record(content: str) -> _ScannedRecord | None:
    text = content.strip()
    offset = len(content) - len(content.lstrip())
    for pattern in (
        _SITE_QUANTITY,
        _PROSE_NUMERIC,
        _PROSE_NOT_APPLICABLE,
        _PROSE_ABSENCE,
        _PROSE_PART,
        _PROSE_UNSTATED,
    ):
        match = pattern.fullmatch(text)
        if not match:
            continue
        fields = {
            key: value for key, value in match.groupdict().items() if key in _FIELDS and value
        }
        spans = {key: (offset + match.start(key), offset + match.end(key)) for key in fields}
        if pattern is _SITE_QUANTITY:
            fields["count_basis"] = "per_site"
        if "category" in match.groupdict() and match["category"]:
            fields["equipment_class"] = match["category"]
            spans["equipment_class"] = (
                offset + match.start("category"),
                offset + match.end("category"),
            )
        if not fields.keys() & {"equipment_class", "manufacturer_model", "requirement_slot"}:
            return None
        unknown_fields = frozenset()
        if pattern is _PROSE_UNSTATED:
            unknown_fields = frozenset(
                _UNSTATED_FIELDS[label] for label in re.findall(_UNSTATED_LABEL, match["labels"])
            )
            basis = (offset + match.start("unknown_basis"), offset + match.end("unknown_basis"))
            spans.update(dict.fromkeys(sorted(unknown_fields), basis))
        elif pattern is _PROSE_NUMERIC:
            prop = fields["property"]
            if re.fullmatch(_PORT_PROPERTY, prop):
                prop = f"{prop.split('端口', 1)[0].strip()}_port_count"
                units = {"个", "端口"}
            else:
                prop = _PROSE_PROPERTIES[prop]
                units = _PROSE_UNITS[prop]
            if fields["unit"] not in units:
                return None
            fields["property"] = prop
            fields["operator"] = _PROSE_OPERATORS[fields["operator"]]
            if "count_basis" in fields:
                fields["count_basis"] = _COUNT_BASIS[fields["count_basis"]]
        elif pattern is _PROSE_PART:
            fields["property"] = "part_number"
        elif "property" in fields:
            fields["property"] = _PROSE_PROPERTIES[fields["property"]]
        raw = {key: content[start:end] for key, (start, end) in spans.items()}
        return _ScannedRecord(
            0,
            raw,
            dict.fromkeys(raw, "正文"),
            spans=spans,
            interpreted=fields,
            unknown_fields=unknown_fields,
        )
    return None


def _scan(
    item: Evidence, *, continuation_header: Evidence | None = None
) -> tuple[str, list[_ScannedRecord]]:
    content = item.raw_content
    if isinstance(content, dict) and _FIELDS.intersection(content):
        return "RECORDS_SCANNED", [_ScannedRecord(0, content, {key: key for key in content})]
    if item.kind == "table":
        recovery = item.native_context.get("table_recovery")
        if isinstance(recovery, dict):
            if recovery.get("projection_input") is True:
                return "TABLE_EXPERIMENTAL_PROJECTION", []
            if recovery.get("header_is_original") is not True:
                return "CONTINUATION_HEADER_UNCONFIRMED", []
        if "structured_table" in item.native_context:
            return _structured_cells(
                item.native_context["structured_table"], item, continuation_header
            )
        if not isinstance(content, str):
            return "TABLE_NOT_HTML", []
        return _scan_table(content, continuation=item.continues_prev is True)
    if not isinstance(content, str):
        return "UNSUPPORTED_CONTENT_KIND", []
    fields, headings = {}, {}
    for line in content.splitlines():
        match = re.fullmatch(r"([^:：]+)[:：](.*)", line)
        if match is None:
            continue
        heading, value = match.groups()
        heading = heading.strip()
        key = _HEADINGS.get(heading, heading)
        if key in _FIELDS:
            if key in fields:
                return "RECORDS_SCANNED", [_ScannedRecord(0, problem="DUPLICATE_RECORD_FIELD")]
            fields[key] = value.strip()
            headings[key] = heading
    if not fields:
        if item.kind == "text" and (record := _prose_record(content)):
            return "RECORDS_SCANNED", [record]
        return "NO_RECOGNIZED_FIELDS", []
    return "RECORDS_SCANNED", [_ScannedRecord(0, fields, headings)]


def normalize(
    raw: dict,
    *,
    requirement: bool,
    unresolved_cells: frozenset[str] = frozenset(),
    normalize_properties: bool = True,
) -> dict:
    values = {
        key: FieldValue(raw.get(key), None, ValueState.UNKNOWN)
        if key in unresolved_cells
        else field_value(key, raw.get(key))
        for key in sorted(_FIELDS)
    }
    if requirement:
        for key in ("operator", "value", "unit", "expr", "part_number"):
            normalized = values[key].normalized
            if normalized is not None and normalized.casefold() in _NUMERIC_PLACEHOLDERS:
                values[key] = FieldValue(raw[key], None, ValueState.UNKNOWN)
        for key, marker, state in (
            ("site_group", "不适用", ValueState.NOT_APPLICABLE),
            ("site_group", "不限定", ValueState.UNCONSTRAINED),
            ("manufacturer_model", "不限定", ValueState.UNCONSTRAINED),
        ):
            if values[key].normalized == marker:
                values[key] = field_value(
                    key,
                    raw[key],
                    semantic_state=state,
                    semantic_basis=f"explicit_requirement_field:{key}",
                )
    applicability = "unknown"
    presence = "unknown"
    derivations = {}
    if requirement:
        for key, markers, state in (
            ("applicability", _EXPLICIT_NOT_APPLICABLE, ValueState.NOT_APPLICABLE),
            ("presence", _EXPLICIT_ABSENCE, ValueState.EXPLICIT_ABSENCE),
        ):
            source = key if values[key].normalized is not None else "value"
            if values[source].normalized in markers and (
                source == key or values["property"].normalized
            ):
                values[source] = field_value(
                    source,
                    raw[source],
                    semantic_state=state,
                    semantic_basis=f"explicit_requirement_field:{source}",
                )
                if key == "applicability":
                    applicability = state.value
                else:
                    presence = state.value
                derivations[key] = [source]
    clean = {key: value.normalized for key, value in values.items()}
    role = clean.get("quantity_role") or "unknown"
    attribute = (
        normalize_attribute(clean["property"])
        if normalize_properties and clean.get("property")
        else None
    )
    if attribute and isinstance(raw.get("property"), str):
        attribute["raw"] = raw["property"]
    property_name = (
        attribute["identity"] if attribute else clean.get("property") or "device_quantity"
    )
    expr = clean.get("expr")
    if unresolved_cells and requirement and clean.get("property") is None:
        property_name = "unknown"
    if (
        not expr
        and _OPERATOR.get(clean.get("operator"))
        and clean.get("value")
        and _DECIMAL.fullmatch(clean["value"])
    ):
        expr = _OPERATOR[clean["operator"]] + clean["value"]
        derivations["expr"] = ["operator", "value"]
    if expr and not derivations and clean.get("expr"):
        derivations["expr"] = ["expr"]
    if not expr and property_name == "interface_config":
        expr = clean.get("config")
        if expr:
            derivations["expr"] = ["config"]
    if not expr and clean.get("quantity") is not None:
        if not requirement or role in ("minimum", "exact", "estimate", "quotation_only"):
            expr = (">=" if requirement and role == "minimum" else "=") + clean["quantity"]
            derivations["expr"] = ["quantity", "quantity_role"] if requirement else ["quantity"]
    scalar = _SCALAR_EXPR.fullmatch(expr) if expr else None
    operator = _OPERATOR[scalar[1]] if scalar else None
    if (
        requirement
        and (attribute["identity"] if attribute else clean.get("property")) == "device_quantity"
        and values["quantity_role"].state == ValueState.NOT_STATED
        and scalar is not None
        and operator in _QUANTITY_ROLES_BY_OPERATOR
        and scalar[2].isdigit()
        and (clean.get("operator") is None or _OPERATOR.get(clean["operator"]) == operator)
        and clean.get("value") in (None, scalar[2])
        and clean.get("unit") in _PROSE_UNITS["device_quantity"]
        and applicability == presence == "unknown"
    ):
        role = _QUANTITY_ROLES_BY_OPERATOR[operator]
        derivations["quantity_role"] = ["property", *derivations["expr"]]
    configuration = _parse_configuration(clean["config"]) if clean.get("config") else {}
    basis = clean.get("count_basis") or "unknown"
    unit = clean.get("unit")
    if port_type(property_name) and unit in ("个", "端口", "ports", "port"):
        unit = "port"
    return {
        "equipment_class": clean.get("equipment_class"),
        "model": clean.get("manufacturer_model"),
        "config_variant": clean.get("configuration_variant"),
        "scope": clean.get("scope"),
        "site_group": clean.get("site_group"),
        "requirement_slot": clean["requirement_slot"],
        "property": property_name,
        **(
            {"attribute_normalization": attribute}
            if attribute and attribute["rule"] != "canonical"
            else {}
        ),
        "expr": expr,
        "operator": operator,
        "numeric_value": scalar[2] if scalar else None,
        "unit": unit or ("configuration" if property_name == "interface_config" else None),
        "field_derivations": derivations,
        "count_basis": basis,
        "strength": _STRENGTH.get(clean.get("strength"), clean.get("strength") or "unknown"),
        "quantity_role": role,
        "inclusion_status": clean.get("inclusion_status") or "unknown",
        "configuration": configuration,
        "condition": clean.get("condition"),
        "source_revision": clean.get("source_revision"),
        "part_number": clean.get("part_number"),
        "applicability": applicability,
        "presence": presence,
        "value_role": "requirement" if requirement else "declaration",
        "field_values": {key: asdict(value) for key, value in values.items()},
        "field_implementation": {
            key: "not_implemented"
            for key in (
                ("source_revision", "quantity_role", "inclusion_status") if requirement else ()
            )
        },
        "declaration_fields": {}
        if requirement
        else declaration_fields(
            {
                key: None
                if key in unresolved_cells
                else property_name
                if key == "property"
                else value
                for key, value in raw.items()
            },
            configuration,
            values["count_basis"].normalized,
        ),
    }


def _prepare_record(
    record: _ScannedRecord, *, allow_explicit_classes: bool = False
) -> tuple[dict | None, list[str]]:
    if record.problem is not None:
        return None, [record.problem]
    if record.unknown_fields and not allow_explicit_classes:
        return None, ["VALUE_EXPLICITLY_UNKNOWN"]
    data = normalize(
        record.raw | record.interpreted,
        requirement=True,
        unresolved_cells=record.unknown_fields,
    )
    for key in record.interpreted:
        data["field_values"][key]["raw"] = record.raw[key]
    reasons = []
    if any(data[field] is None for field in _REQUIRED_FIELDS):
        reasons.append("REQUIRED_FIELDS_MISSING")
    equipment = data["equipment_class"]
    identified = data["model"] or data["requirement_slot"]
    machine_fact = (
        allow_explicit_classes
        and identified
        and (
            data["applicability"] != "unknown"
            or data["presence"] != "unknown"
            or data["part_number"] is not None
            or (record.spans and data["numeric_value"] is not None)
            or record.unknown_fields
        )
    )
    if equipment is None and not machine_fact:
        return None, reasons
    if (
        equipment is not None
        and equipment not in ("OLT", "ONU")
        and not (allow_explicit_classes and _CLASS.fullmatch(equipment))
    ):
        return None, [*reasons, "EQUIPMENT_CLASS_REJECTED"]
    config = record.raw.get("config")
    explicit_configuration = (
        isinstance(config, (int, float, str)) and type(config) is not bool and str(config).strip()
    )
    if (explicit_configuration and not data["configuration"]) or (
        data["property"] == "interface_config"
        and data["expr"] is not None
        and not _parse_configuration(data["expr"])
    ):
        reasons.append("CONFIGURATION_EXPRESSION_UNSUPPORTED")
    return data, reasons


def _field_provenance(item: Evidence, record: _ScannedRecord) -> list[dict]:
    provenance = []
    for column, (key, value) in enumerate(record.raw.items(), 1):
        if key not in _FIELDS or value in (None, ""):
            continue
        cell = record.cells.get(key, {})
        entry = {
            "field_name": key,
            "evidence_id": item.evidence_id,
            "raw_span": value,
            "raw_cell_text": cell.get("raw_text", value),
            "raw_heading": record.headings[key],
            "support_state": "candidate",
            "row_in_block": record.ordinal,
            "physical_page": item.physical_page,
            "source_pointer": item.source_pointer,
            "column_index": column if record.cells else None,
            "native_pointer": cell.get("native_pointer"),
            "native_bbox": cell.get("bbox"),
            "native_coordinate_system": cell.get("coord_system"),
            "table_recovery": item.native_context.get("table_recovery"),
        }
        if key in record.spans:
            entry["raw_start"], entry["raw_end"] = record.spans[key]
        if (header := record.header_evidence) is not None:
            entry["header_evidence"] = {
                "evidence_id": header.evidence_id,
                "source_pointer": header.source_pointer,
                "physical_page": header.physical_page,
                "column_index": column,
                "raw_heading": record.headings[key],
                "support_state": "candidate",
            }
        provenance.append(entry)
    return provenance


def extract_requirements(
    item: Evidence,
    *,
    continuation_header: Evidence | None = None,
    allow_explicit_classes: bool = False,
) -> Iterator[dict]:
    _, records = _scan(item, continuation_header=continuation_header)
    for record in records:
        data, _ = _prepare_record(record, allow_explicit_classes=allow_explicit_classes)
        if data is None:
            continue
        yield attach_annotation_context(
            data
            | {
                "raw": record.raw,
                "evidence_id": item.evidence_id,
                "source": item.evidence_id,
                "source_pointer": item.source_pointer,
                "physical_page": item.physical_page,
                "source_asset_id": item.source_asset_id,
                "precision": item.precision,
                "risk": [
                    *item.risk,
                    *(["continuation_unverified"] if record.header_evidence else []),
                ],
                "row_in_block": record.ordinal,
                "field_evidence": _field_provenance(item, record),
            },
            item,
        )


def _scoped_header_sources(
    evidence: Iterable[Evidence],
) -> Iterator[tuple[Evidence, Evidence | None]]:
    items = tuple(evidence)
    by_pointer: dict[tuple[str, str], Evidence | None] = {}
    for item in items:
        key = (item.parse_run_id, item.source_pointer)
        by_pointer[key] = None if key in by_pointer else item
    for item in items:
        parent = None
        table = item.native_context.get("structured_table")
        if (
            item.continues_prev is True
            and isinstance(table, dict)
            and table.get("header_state") == "inherited"
        ):
            pointer = table.get("continuation_header_pointer")
            if isinstance(pointer, str):
                parent = by_pointer.get((item.parse_run_id, pointer))
        yield item, parent


def extract_requirements_from_evidence(
    evidence: Iterable[Evidence], *, allow_explicit_classes: bool = False
) -> Iterator[dict]:
    """Extract candidates using only explicit continuation links within this evidence scope."""
    for item, parent in _scoped_header_sources(evidence):
        yield from extract_requirements(
            item, continuation_header=parent, allow_explicit_classes=allow_explicit_classes
        )


def attach_annotation_context(candidate: dict, item: Evidence) -> dict:
    annotations = item.annotations
    return candidate | {
        "annotation_context": {
            "association_state": "unverified",
            "physical_page": item.physical_page,
            "fields": annotations,
        }
        if annotations
        else None,
        "risk": list(
            dict.fromkeys([*candidate["risk"], *([ANNOTATION_RISK] if annotations else [])])
        ),
    }


def diagnose_requirements(
    evidence: Iterable[Evidence], *, allow_explicit_classes: bool = False
) -> dict:
    """Account for candidate attempts without serializing any native field values.

    Evidence and table outcomes count blocks, including entire tables rejected before
    row scanning. Record outcomes count only explicit records and attempted body rows.
    Missing-field and unsupported-expression counts are overlapping review indicators.
    """
    blocks: Counter[str] = Counter()
    tables: Counter[str] = Counter()
    records: Counter[str] = Counter()
    reasons: Counter[str] = Counter()
    index = []
    for item, parent in _scoped_header_sources(evidence):
        block_reason, attempts = _scan(item, continuation_header=parent)
        blocks[block_reason] += 1
        if item.kind == "table":
            tables[block_reason] += 1
        if block_reason != "RECORDS_SCANNED":
            reasons[block_reason] += 1
        warnings = []
        if item.annotations:
            warnings.append(ANNOTATION_RISK)
            reasons.update(warnings)
        classified = []
        for record in attempts:
            data, details = _prepare_record(record, allow_explicit_classes=allow_explicit_classes)
            outcome = (
                "rejected" if data is None else "emitted_review_required" if details else "emitted"
            )
            records[outcome] += 1
            reasons.update(details)
            classified.append(
                {"row_in_block": record.ordinal, "outcome": outcome, "reasons": details}
            )
        index.append(
            {
                "evidence_id": item.evidence_id,
                "reason": block_reason,
                "warnings": warnings,
                "records": classified,
            }
        )
    return {
        "diagnostic_version": 1,
        "evidence_total": blocks.total(),
        "records_seen": records.total(),
        "records_rejected": records["rejected"],
        "tables_seen": tables.total(),
        "unsupported_table_count": sum(tables[reason] for reason in _UNSUPPORTED_TABLE_REASONS),
        "header_mapping_failed": sum(tables[reason] for reason in _HEADER_REASONS),
        "required_fields_missing": reasons["REQUIRED_FIELDS_MISSING"],
        "equipment_class_rejected": reasons["EQUIPMENT_CLASS_REJECTED"],
        "expression_unsupported": reasons["CONFIGURATION_EXPRESSION_UNSUPPORTED"],
        "candidates_emitted": records["emitted"] + records["emitted_review_required"],
        "stage_counts": {
            "evidence_outcomes": dict(blocks),
            "table_outcomes": dict(tables),
            "record_outcomes": dict(records),
        },
        "reason_counts": dict(reasons),
        "evidence_index": index,
    }
