"""Logic-only gold cases and branch regressions for deterministic GPON rules."""

import json
from pathlib import Path

import pytest

from ira_native.domain.rules import compare_requirements, evaluate

CASES = [
    json.loads(line)
    for line in (Path(__file__).parent / "fixtures/synthetic/cases.jsonl")
    .read_text(encoding="utf-8")
    .splitlines()
]
CHECKED = {
    "entity_binding": "confirmed",
    "source_assumption": "verified_for_checked_fields",
    "revision_relation": "same_version",
}
REQUIREMENT = {
    "equipment_class": "ONU",
    "scope": "A",
    "property": "ethernet_ports",
    "strength": "mandatory",
    "count_basis": "per_device",
    "unit": "port",
    "value_role": "requirement",
    "expr": ">=8",
}
DECLARATION = {**REQUIREMENT, "value_role": "declaration", "expr": "=9"}


@pytest.mark.parametrize("case", CASES, ids=[case["case_id"] for case in CASES])
def test_existing_logic_only_gold(case):
    assert case["test_scope"] == "logic_only"
    actual = evaluate(case["requirement"], case["declaration"], case["preconditions"])
    assert {key: actual[key] for key in case["expected"]} == case["expected"]


@pytest.mark.parametrize(
    ("left", "right", "relation", "strictness"),
    [
        (">=8", ">7", "equivalent", "same"),
        ("[2,5]", "[5,8]", "compatible", "unknown"),
        ("[2,5)", "[5,8]", "contradictory", "unknown"),
        ("[2,8]", "=4", "compatible", "right_stricter"),
        ("=4", "[2,8]", "compatible", "left_stricter"),
    ],
)
def test_constraint_relation_has_its_own_engine(left, right, relation, strictness):
    result = compare_requirements(
        {**REQUIREMENT, "expr": left}, {**REQUIREMENT, "expr": right}, CHECKED
    )
    assert result["constraint_relation"] == relation
    assert result["strictness"] == strictness
    assert result["requirement_evaluation"] is None


@pytest.mark.parametrize("expr", ["NaN", "=Infinity", "=1e999999", "=8.5", "=-8", ">=9"])
def test_bad_or_nonscalar_declarations_do_not_issue(expr):
    result = evaluate(REQUIREMENT, {**DECLARATION, "expr": expr}, CHECKED)
    assert result["finding_disposition"] == "review_required"
    assert result["requirement_evaluation"] == "undetermined"


def test_count_unit_and_reference_guard_prevent_false_issue():
    invalid_unit = evaluate(REQUIREMENT, {**DECLARATION, "unit": "台"}, CHECKED)
    assert invalid_unit["reason_code"] == "COUNT_UNIT_SEMANTICS_UNKNOWN"
    estimate = evaluate(
        {**REQUIREMENT, "quantity_role": "estimate"}, {**DECLARATION, "expr": "=3"}, CHECKED
    )
    assert estimate["finding_disposition"] != "issue"
    assert estimate["requirement_evaluation"] != "violated"


def test_configuration_multiset_keeps_port_types_independent():
    req = {
        **REQUIREMENT,
        "property": "interface_config",
        "unit": "configuration",
        "expr": "2 × GPON + 8 x FE ports",
    }
    right = {**req, "value_role": "declaration", "expr": "8xFE + 2xGPON"}
    assert evaluate(req, right, CHECKED)["requirement_evaluation"] == "satisfied"
    wrong = {**right, "expr": "10xFE"}
    assert evaluate(req, wrong, CHECKED)["requirement_evaluation"] == "violated"
    glyph = {**right, "expr": "8xFE + 2xGE(0)"}
    assert evaluate(req, glyph, CHECKED)["requirement_evaluation"] == "undetermined"


def test_guard_precedes_rules_even_with_known_difference():
    wrong = {**DECLARATION, "expr": "=2"}
    for facts, expected in [
        ({**CHECKED, "source_assumption": "mismatch"}, "blocked"),
        ({**CHECKED, "source_assumption": "candidate_only"}, "review_required"),
        ({**CHECKED, "revision_relation": "unknown"}, "review_required"),
        ({**CHECKED, "entity_binding": "unconfirmed"}, "review_required"),
    ]:
        assert evaluate(REQUIREMENT, wrong, facts)["finding_disposition"] == expected


def test_unknown_applicability_and_missing_conditions_refuse_calculation():
    for left, facts in [
        (REQUIREMENT, {**CHECKED, "applicability": "unknown"}),
        ({**REQUIREMENT, "condition": "indoor"}, CHECKED),
    ]:
        result = evaluate(left, DECLARATION, facts)
        assert result["requirement_evaluation"] == "undetermined"


def test_explicit_revision_requires_matching_checked_scope():
    req = {**REQUIREMENT, "source_revision": "A"}
    for facts in [
        {**CHECKED, "revision_relation": "explicit_supersedes"},
        {
            **CHECKED,
            "revision_relation": "explicit_supersedes",
            "amendment_scope_confirmed": True,
            "effective_requirement_revision": "B",
        },
    ]:
        assert evaluate(req, DECLARATION, facts)["reason_code"] == "REVISION_UNKNOWN"


def test_enum_requirement_checks_literal_membership_and_set_inclusion():
    req = {**REQUIREMENT, "property": "operating_mode", "expr": "{A,B}", "unit": "enum"}
    matching = {**req, "value_role": "declaration", "expr": "=B"}
    assert evaluate(req, matching, CHECKED)["requirement_evaluation"] == "satisfied"
    assert (
        evaluate(req, {**matching, "expr": "=b"}, CHECKED)["requirement_evaluation"] == "violated"
    )
    assert (
        evaluate(req, {**matching, "expr": "{A,B}"}, CHECKED)["requirement_evaluation"]
        == "undetermined"
    )
    stricter = {**req, "expr": "{B}"}
    result = compare_requirements(req, stricter, CHECKED)
    assert (result["constraint_relation"], result["strictness"]) == ("compatible", "right_stricter")


def test_incomplete_configuration_or_wrong_class_cannot_be_confirmed_by_flag_alone():
    req = {**REQUIREMENT, "config_variant": "FE-8"}
    assert evaluate(req, DECLARATION, CHECKED)["reason_code"] == "CONFIG_VARIANT_UNKNOWN"
    wrong_class = {**REQUIREMENT, "equipment_class": "router"}
    assert evaluate(wrong_class, None, CHECKED)["requirement_evaluation"] == "undetermined"


def test_fixture_precondition_does_not_certify_live_inputs():
    facts = {**CHECKED, "source_assumption": "fixture_field_supported"}
    assert evaluate(REQUIREMENT, DECLARATION, facts)["reason_code"] == "FIELD_SUPPORT_UNKNOWN"
