"""Recognize explicit GPON fields without inferring headers or force."""

import re

from bs4 import BeautifulSoup

from ira_native.domain.models import Evidence

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


def _records(item: Evidence):
    content = item.raw_content
    if isinstance(content, dict) and _FIELDS.intersection(content):
        yield 0, content, {key: key for key in content}
    elif item.kind == "table" and isinstance(content, str):
        tables = BeautifulSoup(content, "html.parser").find_all("table")
        if len(tables) != 1:
            return
        table = tables[0]
        rows = table.find_all("tr")
        if not rows or any(
            c.get("rowspan", "1") != "1" or c.get("colspan", "1") != "1"
            for c in table.find_all(["td", "th"])
        ):
            return
        headers = [c.get_text(" ", strip=True) for c in rows[0].find_all("th", recursive=False)]
        names = [_HEADINGS.get(value, value) for value in headers]
        if not {"equipment_class", "configuration_variant", "scope"} <= set(names) or len(
            names
        ) != len(set(names)):
            return
        for i, row in enumerate(rows[1:], 1):
            values = [c.get_text(" ", strip=True) for c in row.find_all("td", recursive=False)]
            if len(values) == len(names):
                yield (
                    i,
                    dict(zip(names, values, strict=True)),
                    dict(zip(names, headers, strict=True)),
                )
    elif isinstance(content, str):
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
                    return
                fields[key] = value.strip()
                headings[key] = heading
        if fields:
            yield 0, fields, headings


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
    configuration = {}
    if config := clean.get("config"):
        for segment in config.split("+"):
            match = _PORT.fullmatch(segment)
            if match is None:
                configuration = {}
                break
            count, interface = match.groups()
            name = interface.upper()
            configuration[name] = configuration.get(name, 0) + int(count)
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


def extract_requirements(item: Evidence):
    for ordinal, raw, headings in _records(item):
        data = normalize(raw, requirement=True)
        if data["equipment_class"] not in ("OLT", "ONU"):
            continue
        yield data | {
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
        }
