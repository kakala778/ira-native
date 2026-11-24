"""Recognize explicit GPON fields without inferring headers or force."""

import re
from collections import Counter
from collections.abc import Iterable, Iterator
from dataclasses import dataclass, field

from bs4 import BeautifulSoup

from ira_native.domain.models import ANNOTATION_RISK, Evidence

_FIELDS = set(
    (
        "req_id equipment_class manufacturer_model configuration_variant scope site_group "
        "quantity unit count_basis strength quantity_role config property expr condition "
        "source_revision inclusion_status"
    ).split()
)
_PORT = re.compile(r"\s*(\d{1,6})\s*[x×]\s*(GPON|POTS|FE|GE)(?:\s+ports?)?\s*", re.I)
_HEADINGS = {
    "设备类别": "equipment_class",
    "厂家型号": "manufacturer_model",
    "配置变体": "configuration_variant",
    "工程范围": "scope",
    "站点组": "site_group",
    "数量": "quantity",
    "单位": "unit",
    "计数口径": "count_basis",
    "要求效力": "strength",
    "数量角色": "quantity_role",
    "接口配置": "config",
    "属性": "property",
    "约束表达式": "expr",
    "适用条件": "condition",
    "来源版本": "source_revision",
    "范围状态": "inclusion_status",
}
_STRENGTH = {"强制": "mandatory", "参考": "reference", "信息性": "informative"}
_REQUIRED_HEADERS = {"equipment_class", "configuration_variant", "scope"}
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
    "TABLE_ROWSPAN_UNSUPPORTED",
    "TABLE_COLSPAN_UNSUPPORTED",
}


@dataclass(frozen=True)
class _ScannedRecord:
    ordinal: int
    raw: dict = field(default_factory=dict)
    headings: dict[str, str] = field(default_factory=dict)
    problem: str | None = None


def _scan_table(content: str, *, continuation: bool) -> tuple[str, list[_ScannedRecord]]:
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
    headers = [c.get_text(" ", strip=True) for c in rows[0].find_all("th", recursive=False)]
    if not headers:
        return (
            "CONTINUATION_HEADER_UNCONFIRMED" if continuation else "TABLE_HEADER_NOT_EXPLICIT"
        ), []
    names = [_HEADINGS.get(value, value) for value in headers]
    if not _REQUIRED_HEADERS <= set(names):
        return "TABLE_HEADER_MAPPING_FAILED", []
    if len(names) != len(set(names)):
        return "TABLE_HEADER_DUPLICATE", []
    headings = dict(zip(names, headers, strict=True))
    records = []
    for ordinal, row in enumerate(rows[1:], 1):
        values = [cell.get_text(" ", strip=True) for cell in row.find_all("td", recursive=False)]
        records.append(
            _ScannedRecord(ordinal, dict(zip(names, values, strict=True)), headings)
            if len(values) == len(names)
            else _ScannedRecord(ordinal, problem="TABLE_ROW_WIDTH_MISMATCH")
        )
    return ("RECORDS_SCANNED" if records else "TABLE_EMPTY_BODY"), records


def _scan(item: Evidence) -> tuple[str, list[_ScannedRecord]]:
    content = item.raw_content
    if isinstance(content, dict) and _FIELDS.intersection(content):
        return "RECORDS_SCANNED", [_ScannedRecord(0, content, {key: key for key in content})]
    if item.kind == "table":
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
        return "NO_RECOGNIZED_FIELDS", []
    return "RECORDS_SCANNED", [_ScannedRecord(0, fields, headings)]


def _parse_configuration(expression: str) -> dict[str, int]:
    configuration: dict[str, int] = {}
    for segment in expression.split("+"):
        match = _PORT.fullmatch(segment)
        if match is None:
            return {}
        count, interface = match.groups()
        name = interface.upper()
        configuration[name] = configuration.get(name, 0) + int(count)
    return configuration


def normalize(raw: dict, *, requirement: bool) -> dict:
    clean = {
        k: str(v).strip() or None
        for k, v in raw.items()
        if isinstance(v, (str, int, float)) and type(v) is not bool
    }
    role = clean.get("quantity_role") or "unknown"
    property_name = clean.get("property") or "device_quantity"
    expr = clean.get("expr")
    if not expr and property_name == "interface_config":
        expr = clean.get("config")
    if not expr and clean.get("quantity") is not None:
        if not requirement or role in ("minimum", "exact", "estimate", "quotation_only"):
            expr = (">=" if requirement and role == "minimum" else "=") + clean["quantity"]
    configuration = _parse_configuration(clean["config"]) if clean.get("config") else {}
    basis = clean.get("count_basis") or "unknown"
    return {
        "equipment_class": clean.get("equipment_class"),
        "model": clean.get("manufacturer_model"),
        "config_variant": clean.get("configuration_variant"),
        "scope": clean.get("scope"),
        "site_group": clean.get("site_group"),
        "property": property_name,
        "expr": expr,
        "unit": clean.get("unit")
        or ("configuration" if property_name == "interface_config" else None),
        "count_basis": "per_contract_scope" if basis == "per_scope" else basis,
        "strength": _STRENGTH.get(clean.get("strength"), clean.get("strength") or "unknown"),
        "quantity_role": role,
        "inclusion_status": clean.get("inclusion_status") or "unknown",
        "configuration": configuration,
        "condition": clean.get("condition"),
        "source_revision": clean.get("source_revision"),
        "value_role": "requirement" if requirement else "declaration",
    }


def _prepare_record(record: _ScannedRecord) -> tuple[dict | None, list[str]]:
    if record.problem is not None:
        return None, [record.problem]
    data = normalize(record.raw, requirement=True)
    reasons = []
    if any(data[field] is None for field in _REQUIRED_FIELDS):
        reasons.append("REQUIRED_FIELDS_MISSING")
    if data["equipment_class"] is None:
        return None, reasons
    if data["equipment_class"] not in ("OLT", "ONU"):
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


def extract_requirements(item: Evidence) -> Iterator[dict]:
    _, records = _scan(item)
    for record in records:
        data, _ = _prepare_record(record)
        if data is None:
            continue
        ordinal, raw, headings = record.ordinal, record.raw, record.headings
        yield attach_annotation_context(
            data
            | {
                "raw": raw,
                "evidence_id": item.evidence_id,
                "source": item.evidence_id,
                "source_pointer": item.source_pointer,
                "physical_page": item.physical_page,
                "source_asset_id": item.source_asset_id,
                "precision": item.precision,
                "risk": list(item.risk),
                "row_in_block": ordinal,
                "field_evidence": [
                    {
                        "field_name": key,
                        "evidence_id": item.evidence_id,
                        "raw_span": value,
                        "raw_heading": headings[key],
                        "support_state": "candidate",
                        "row_in_block": ordinal,
                    }
                    for key, value in raw.items()
                    if key in _FIELDS and value not in (None, "")
                ],
            },
            item,
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


def diagnose_requirements(evidence: Iterable[Evidence]) -> dict:
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
    for item in evidence:
        block_reason, attempts = _scan(item)
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
            data, details = _prepare_record(record)
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
