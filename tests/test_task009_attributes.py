import pytest

from ira_native.application.candidates import normalize
from ira_native.domain.attributes import normalize_attribute
from ira_native.domain.candidate_matching import assess, pair_field


@pytest.mark.parametrize(
    ("raw", "identity"),
    [
        ("最大功耗", "maximum_power"),
        ("设备数量", "device_quantity"),
        ("GPON 端口数量", "gpon_ports"),
        ("GE端口数", "ge_ports"),
        ("额定功率", "rated_power"),
        ("功耗", "power_consumption"),
        ("距离", "distance"),
        ("传输距离", "transmission_distance"),
    ],
)
def test_exact_labels(raw, identity):
    assert normalize_attribute(raw) == {
        "raw": raw,
        "identity": identity,
        "state": "exact",
        "rule": "exact_label",
        "blocker": None,
    }


@pytest.mark.parametrize(
    "label",
    [
        "光口数量",
        "Ethernet",
        "最大功率",
        "输入功率",
        "额定电压",
        "数量",
        "实测最大功耗",
        "申报最大功耗",
        "要求最大功耗",
        "设计最大功耗",
        "额定电流",
    ],
)
def test_qualified_broader_and_unknown_names_are_never_inferred(label):
    attribute = normalize_attribute(label)
    assert attribute["identity"] == label
    assert attribute["blocker"]


def test_count_role_uses_normalized_explicit_subject_and_keeps_count_basis():
    raw = {
        "property": "设备数量",
        "value": "4",
        "operator": ">=",
        "unit": "台",
        "condition": "室外",
        "count_basis": "per_site",
    }
    requirement = normalize(raw, requirement=True)
    assert requirement["quantity_role"] == "minimum"
    assert requirement["count_basis"] == "per_site"
    assert requirement["condition"] == "室外"
    assert requirement["equipment_class"] is None
    assert requirement["field_derivations"]["quantity_role"] == ["property", "operator", "value"]
    assert normalize(raw | {"property": "数量"}, requirement=True)["quantity_role"] == "unknown"
    row = normalize(raw | {"quantity": "6", "count_basis": "total"}, requirement=False)
    assert pair_field(requirement, row)["status"] == "observed"
    assert "count_basis_not_comparable" in assess(requirement, row)["comparison_blockers"]


def test_exact_technical_identity_shared_by_both_sides():
    left = normalize(
        {"property": "最大功耗", "value": "75", "operator": "<=", "unit": "W"}, requirement=True
    )
    right = normalize(
        {"property": "最大功耗", "technical_value": "74", "technical_unit": "W"}, requirement=False
    )
    assert pair_field(left, right)["declaration"]["source_field"] == "technical_value"
    wrong = normalize(
        {"property": "输入功率", "technical_value": "74", "technical_unit": "W"}, requirement=False
    )
    assert pair_field(left, wrong)["status"] == "not_observed"
