"""R2 identity, source-state and field-pairing contract regressions."""

import pytest

from ira_native.application.candidates import normalize
from ira_native.domain.candidate_matching import assess, generate_candidates
from ira_native.domain.values import ValueState, field_value


def record(requirement, **overrides):
    raw = {
        "equipment_class": "ONU",
        "configuration_variant": "GE型ONU",
        "scope": "SCOPE-A",
        "quantity": "4",
        "unit": "台",
        "count_basis": "按标段合计（per_contract_scope）",
        **({"quantity_role": "exact"} if requirement else {}),
        **overrides,
    }
    key = "candidate_id" if requirement else "item_id"
    return normalize(raw, requirement=requirement) | {
        key: "req" if requirement else raw.get("id", "decl")
    }


def test_placeholder_handling_depends_on_role_and_does_not_zero_quantities():
    assert field_value("manufacturer_model", "SYN-ONU-M1").normalized == "SYN-ONU-M1"
    assert field_value("manufacturer_model", " — ").raw == " — "
    assert field_value("manufacturer_model", "—").state == ValueState.UNKNOWN
    assert field_value("scope", "A—B").normalized == "A—B"
    assert field_value("scope", "—").state == ValueState.UNKNOWN
    assert field_value("site_group", "").state == ValueState.NOT_STATED
    assert field_value("quantity", None).normalized is None
    assert field_value("quantity", "—").normalized is None
    with pytest.raises(ValueError):
        field_value("site_group", None, semantic_state=ValueState.NOT_APPLICABLE)
    assert (
        record(True, site_group="不适用")["field_values"]["site_group"]["state"] == "not_applicable"
    )
    assert record(False, site_group="不适用")["field_values"]["site_group"]["state"] == "present"


@pytest.mark.parametrize(
    "case,klass,left_model,right_model",
    [
        ("T2A-001", "ONU", "—", "SYN-ONU-M1"),
        ("T2A-002", "OLT", "SYN-OLT-C7", "SYN-OLT-C7"),
    ],
)
def test_t2a001_002_recall_with_site_absence(case, klass, left_model, right_model):
    left = record(True, equipment_class=klass, manufacturer_model=left_model)
    right = record(False, equipment_class=klass, manufacturer_model=right_model, site_group="—")
    (edge,) = generate_candidates([left], [right])
    assert edge["match_label"] == "candidate_same_slot", case
    assert edge["dimensions"]["site_group"] == "unresolved"
    assert edge["binding_state"] == "unconfirmed"
    assert edge["system_confirmation"] is False


def test_t2a003_interface_config_pairing_ignores_old_declaration_property():
    left = record(
        True, property="interface_config", config="2×GPON+4×GE", count_basis="按设备（per_device）"
    )
    right = record(False, config="4×GE+2×GPON", count_basis="按设备（per_device）", site_group="S1")
    (edge,) = generate_candidates([left], [right])
    assert edge["field_pairing"]["field"] == "interface_config"
    assert edge["field_pairing"]["declaration"]["value"]["raw"] == "4×GE+2×GPON"
    assert "site_group" in edge["unresolved_dimensions"]
    assert "equipment_class" not in edge["recall_blockers"]


def test_t2a010_model_conflict_retained_only_as_candidate():
    left = record(True, manufacturer_model="SYN-ONU-M1")
    right = record(False, manufacturer_model="SYN-ONU-M1B")
    (edge,) = generate_candidates([left], [right])
    assert edge["dimensions"]["manufacturer_model"] == "conflict"
    assert edge["binding_eligibility"] == "not_eligible"
    assert "model_conflict" in edge["confirmation_basis"]


def test_t2a025_no_inference_of_coverage_between_named_scopes():
    left = record(True, scope="全线")
    right = record(False, scope="虚构标段一", site_group="虚构站点组甲")
    assert generate_candidates([left], [right]) == []
    assert "scope_relation_unproven" in assess(left, right)["recall_blockers"]


def test_one_to_many_and_target_port_count_difference_are_preserved():
    left = record(True, property="GE_port_count", configuration_variant="4×GE+2×POTS")
    a = record(False, id="a", configuration_variant="6×GE+2×POTS", config="6×GE+2×POTS")
    b = record(False, id="b", configuration_variant="3×GE+2×POTS", config="3×GE+2×POTS")
    edges = generate_candidates([left], [a, b])
    assert {edge["right_ref"] for edge in edges} == {"a", "b"}
    assert all(
        edge["topology"] == "one_to_many" and not edge["system_confirmation"] for edge in edges
    )
    assert edges[0]["field_pairing"]["declaration"]["value"]["normalized"] == "6"
    assert generate_candidates([left], [record(False, equipment_class="OLT")]) == []
    assert generate_candidates([left], [record(False, configuration_variant="4×GE+3×POTS")]) == []
    assert (
        generate_candidates(
            [left], [record(False, configuration_variant="different named identity")]
        )
        == []
    )


def test_interface_requirement_does_not_pair_with_device_quantity():
    (edge,) = generate_candidates([record(True, property="interface_config")], [record(False)])
    assert edge["field_pairing"] == {
        "field": "interface_config",
        "status": "not_observed",
        "declaration": None,
    }
    assert "comparison_field_not_observed" in edge["comparison_blockers"]


def test_technical_quantity_is_not_a_device_count():
    declaration = record(False, property="test_duration", quantity="2", unit="h")
    assert declaration["declaration_fields"]["device_quantity"] is None
    (edge,) = generate_candidates([record(True, property="test_duration", unit="h")], [declaration])
    assert edge["field_pairing"]["field"] == "technical_attribute.test_duration"
    assert edge["field_pairing"]["declaration"]["unit"] == "h"
    assert edge["field_pairing"]["declaration"]["value"]["normalized"] == "2"


@pytest.mark.parametrize("placeholder", ["—", "N/A", "未注明"])
def test_unattributed_placeholder_is_not_a_condition_or_strength(placeholder):
    req = record(True, condition=placeholder, strength=placeholder)
    assert req["condition"] is None
    assert req["strength"] == "unknown"
    assert req["field_values"]["condition"]["raw"] == placeholder
    assert req["field_values"]["condition"]["state"] == "unknown"


def test_unknown_model_and_unproven_site_coverage_are_distinct():
    req = record(True, manufacturer_model="NX-4800S", site_group="不限定", count_basis="per_site")
    decl = record(False, site_group="S1", count_basis="per_site")
    (edge,) = generate_candidates([req], [decl])
    assert edge["match_label"] == "ambiguous_same_slot"
    assert edge["comparison_blockers"] == ["model_unresolved"]
    assert edge["applicability_blockers"] == ["site_coverage_unproven"]
    decl["model"] = req["model"]
    (edge,) = generate_candidates([req], [decl])
    assert edge["match_label"] == "candidate_same_slot"
    assert edge["comparability"] == "blocked"
    assert edge["binding_state"] == "unconfirmed"


@pytest.mark.parametrize(
    ("attribute", "target"),
    [
        ("ge_ports", "GE"),
        ("fe_ports", "FE"),
        ("gpon_ports", "GPON"),
        ("POTS端口数量", "POTS"),
        ("ethernet_ports", "unknown"),
        ("port_count", "unknown"),
    ],
)
def test_port_field_requires_a_specific_interface(attribute, target):
    req = record(True, property=attribute)
    decl = record(False, config="1×GE+2×FE+3×GPON+4×POTS", count_basis=None)
    found = assess(req, decl)["field_pairing"]
    assert found["field"] == f"interface_port_count.{target}"
    assert found["status"] == ("not_observed" if target == "unknown" else "observed")
    assert decl["declaration_fields"]["device_quantity"]["count_basis"] == "unknown"
    assert decl["field_values"]["count_basis"]["state"] == "not_stated"


def test_explicit_technical_columns_coexist_with_device_quantity_and_config():
    decl = record(
        False,
        property="test_duration",
        config="4×GE",
        quantity="2",
        unit="台",
        technical_value="6",
        technical_unit="h",
    )
    typed = decl["declaration_fields"]
    assert typed["device_quantity"]["value"]["normalized"] == "2"
    assert typed["interface_port_counts"]["GE"]["value"]["normalized"] == "4"
    tech = typed["technical_attributes"]["test_duration"]
    assert tech["value"]["normalized"] == "6"
    assert (tech["source_field"], tech["unit_source_field"]) == (
        "technical_value",
        "technical_unit",
    )
