"""Candidate retrieval and field selection, with no trust promotion."""

import re
from collections import Counter, defaultdict
from dataclasses import asdict
from typing import Literal

from .values import field_value, parse_configuration

_DEVICE_UNITS = {"台", "端", "套", "块", "个", "device", "devices"}
_UNSIGNED_INTEGER = re.compile(r"[0-9]+")
_PORT_ATTRIBUTE = re.compile(
    r"(?:(GE|FE|POTS|GPON)(?:_port_count|_ports|端口(?:数|数量))|interface_port_count\.(GE|FE|POTS|GPON))",
    re.I,
)
_UNTYPED_PORTS = {"port_count", "ethernet_ports"}
_NUMBER = re.compile(r"[+-]?(?:[0-9]+(?:\.[0-9]+)?|\.[0-9]+)")


def port_type(property_name: str) -> str | None:
    match = _PORT_ATTRIBUTE.fullmatch(property_name)
    return (match[1] or match[2]).upper() if match else None


def _variant_difference(
    left: str, right: str, prop: str
) -> Literal["same", "value_to_check", "conflict"]:
    left_ports, right_ports = parse_configuration(left), parse_configuration(right)
    if not left_ports or not right_ports or left_ports.keys() != right_ports.keys():
        return "conflict"
    changed = {name for name in left_ports if left_ports[name] != right_ports[name]}
    if not changed:
        return "same"
    if prop == "interface_config" or changed == {port_type(prop)}:
        return "value_to_check"
    return "conflict"


def _slot_field(value: dict, unit: str | None, count_basis: str | None, source_field: str) -> dict:
    # source_field points back to the row's existing source evidence; derived
    # port quantities share the config cell, never a fabricated new locator.
    return {"value": value, "unit": unit, "count_basis": count_basis, "source_field": source_field}


def declaration_fields(raw: dict, parsed_config: dict[str, int], basis: str | None) -> dict:
    basis = basis or "unknown"
    quantity = field_value("quantity", raw.get("quantity"))
    config = field_value("config", raw.get("config"))
    unit = field_value("unit", raw.get("unit")).normalized
    prop = field_value("property", raw.get("property")).normalized
    dedicated_technical = "technical_value" in raw or "technical_unit" in raw
    device_quantity = None
    if (
        quantity.normalized is not None
        and _UNSIGNED_INTEGER.fullmatch(quantity.normalized)
        and unit in _DEVICE_UNITS
        and (dedicated_technical or prop in (None, "device_quantity", "interface_config"))
    ):
        device_quantity = _slot_field(asdict(quantity), unit, basis, "quantity")
    interface_config = None
    if parsed_config:
        interface_config = _slot_field(asdict(config), "configuration", "per_device", "config")
    ports = {
        name: _slot_field(
            asdict(field_value("port_count", count)) | {"raw": config.raw},
            "port",
            "per_device",
            "config",
        )
        for name, count in parsed_config.items()
    }
    technical = {}
    # No inference from a number or prose label. An explicit technical property
    # plus a numeric value and unit is the current supported declaration format.
    name = prop.removeprefix("technical_attribute.") if prop else None
    source = "technical_value" if dedicated_technical else "expr" if "expr" in raw else "quantity"
    unit_field = "technical_unit" if dedicated_technical else "unit"
    technical_unit = field_value(unit_field, raw.get(unit_field)).normalized
    technical_value = field_value(source, raw.get(source)).normalized
    value = (
        technical_value.removeprefix("=")
        if technical_value and source == "expr"
        else technical_value
    )
    if (
        name
        and name not in {"device_quantity", "interface_config"}
        and not port_type(name)
        and name not in _UNTYPED_PORTS
        and value
        and _NUMBER.fullmatch(value)
        and technical_unit
        and technical_unit not in _DEVICE_UNITS
    ):
        technical[name] = _slot_field(
            asdict(field_value(name, value)) | {"raw": raw[source]},
            technical_unit,
            basis,
            source,
        ) | {"unit_source_field": unit_field}
    return {
        "device_quantity": device_quantity,
        "interface_config": interface_config,
        "interface_port_counts": ports,
        "technical_attributes": technical,
    }


def pair_field(requirement: dict, declaration: dict) -> dict:
    prop = requirement["property"]
    fields = declaration["declaration_fields"]
    port = port_type(prop)
    if prop in ("device_quantity", "interface_config"):
        field, value = prop, fields[prop]
    elif port:
        field = f"interface_port_count.{port}"
        value = fields["interface_port_counts"].get(port)
    elif prop in _UNTYPED_PORTS:
        field, value = "interface_port_count.unknown", None
    else:
        name = prop.removeprefix("technical_attribute.")
        field = f"technical_attribute.{name}"
        value = fields["technical_attributes"].get(name)
    return {"field": field, "status": "observed" if value else "not_observed", "declaration": value}


def assess(requirement: dict, declaration: dict) -> dict:
    """Evaluate identity dimensions without using declaration.property as a key."""
    dimensions: dict[str, Literal["same", "conflict", "unresolved", "value_to_check"]] = {}
    recall, unresolved, binding, comparison = [], [], [], []
    prop = requirement["property"]
    applicability = []
    attribute = requirement.get("attribute_normalization") or {}
    if attribute.get("blocker"):
        comparison.append(attribute["blocker"].lower())

    identity_fields = (
        ("equipment_class", "equipment_class"),
        ("manufacturer_model", "model"),
        ("configuration_variant", "config_variant"),
        ("scope", "scope"),
        ("site_group", "site_group"),
        ("requirement_slot", "requirement_slot"),
    )
    if requirement.get("part_number") or declaration.get("part_number"):
        identity_fields += (("part_number", "part_number"),)
    for dimension, key in identity_fields:
        left, right = requirement.get(key), declaration.get(key)
        if left is None or right is None:
            dimensions[dimension] = "unresolved"
            unresolved.append(dimension)
            if dimension == "equipment_class":
                recall.append("equipment_class_unobserved")
            if dimension == "manufacturer_model" and left is not None:
                binding.append("model_unresolved")
                comparison.append("model_unresolved")
            if dimension == "part_number" and (left is not None or right is not None):
                comparison.append("part_identity_unproven")
            if dimension in ("scope", "configuration_variant"):
                comparison.append(f"{dimension}_unproven")
        elif left == right:
            dimensions[dimension] = "same"
        elif dimension == "configuration_variant":
            state = _variant_difference(left, right, prop)
            dimensions[dimension] = state
            if state == "conflict":
                recall.append("configuration_variant_conflict")
            elif state == "value_to_check":
                unresolved.append(dimension)
        else:
            dimensions[dimension] = "conflict"
            if dimension == "manufacturer_model":
                binding.append("model_conflict")
            elif dimension == "scope":
                recall.append("scope_relation_unproven")
            else:
                recall.append(f"{dimension}_conflict")

    pairing = pair_field(requirement, declaration)
    if not requirement.get("expr"):
        comparison.append("requirement_field_not_observed")
    if pairing["status"] == "not_observed":
        comparison.append("comparison_field_not_observed")
    else:
        paired = pairing["declaration"]
        if requirement.get("count_basis") != paired["count_basis"] or requirement.get(
            "count_basis"
        ) in (None, "unknown"):
            comparison.append("count_basis_not_comparable")
        if not requirement.get("unit") or paired["unit"] is None:
            comparison.append("unit_unobserved")
        elif requirement["unit"] != paired["unit"]:
            # No conversions here; the rule layer can later assess appropriate units.
            comparison.append("unit_requires_comparison")
    if (
        requirement.get("count_basis") in ("per_site", "per_site_group")
        and dimensions["site_group"] != "same"
    ):
        applicability.append("site_coverage_unproven")
        unresolved.append("site_coverage")
        binding.append("site_coverage_unproven")
        site_state = requirement.get("field_values", {}).get("site_group", {}).get("state")
        if site_state not in ("unconstrained", "not_applicable") or not declaration.get(
            "site_group"
        ):
            applicability.append("site_group_unresolved")
    if requirement.get("condition") != declaration.get("condition"):
        comparison.append("condition_unproven")

    return {
        "dimensions": dimensions,
        "unresolved_dimensions": [*unresolved, "source_revision"],
        "recall_blockers": recall,
        "comparison_blockers": comparison,
        "applicability_blockers": applicability,
        "comparability": "blocked"
        if recall or comparison or binding or applicability
        else "pending_verification",
        "binding_state": "unconfirmed",
        "binding_eligibility": "not_eligible",
        "confirmation_basis": ["no_auto_confirmation", "source_revision_unimplemented", *binding],
        "system_confirmation": False,
        "field_pairing": pairing,
    }


def generate_candidates(requirements: list[dict], declarations: list[dict]) -> list[dict]:
    by_class = defaultdict(list)
    for item in declarations:
        by_class[item.get("equipment_class")].append(item)
    edges = []
    for req in requirements:
        for decl in by_class[req.get("equipment_class")]:
            result = assess(req, decl)
            if not result["recall_blockers"]:
                edges.append(
                    {"left_ref": req["candidate_id"], "right_ref": decl["item_id"], **result}
                )
    left_counts = Counter(edge["left_ref"] for edge in edges)
    right_counts = Counter(edge["right_ref"] for edge in edges)
    for edge in edges:
        multiple_left = left_counts[edge["left_ref"]] > 1
        multiple_right = right_counts[edge["right_ref"]] > 1
        if multiple_left and multiple_right:
            edge["topology"] = "many_to_many"
        elif multiple_left:
            edge["topology"] = "one_to_many"
        elif multiple_right:
            edge["topology"] = "many_to_one"
        else:
            edge["topology"] = "one_to_one"
        edge["match_label"] = (
            "ambiguous_one_to_many"
            if multiple_left or multiple_right
            else "ambiguous_same_slot"
            if edge["comparison_blockers"]
            or "site_group_unresolved" in edge["applicability_blockers"]
            else "candidate_same_slot"
        )
    return edges
