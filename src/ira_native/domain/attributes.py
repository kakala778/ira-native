"""Exact, auditable attribute identities shared by document and sheet normalization."""

import re
import unicodedata
from typing import TypedDict

from ira_native.domain.candidate_matching import port_type
from ira_native.domain.rules import COUNTS, UNITS


class AttributeNormalization(TypedDict):
    raw: str
    identity: str
    state: str
    rule: str
    blocker: str | None


_EXACT_LABELS = {
    "设备数量": "device_quantity",
    "最大功耗": "maximum_power",
    "功耗": "power_consumption",
    "额定功率": "rated_power",
    "电压": "voltage",
    "传输距离": "transmission_distance",
    "距离": "distance",
    "测试时长": "test_duration",
    "接口配置": "interface_config",
    "光纤接口": "fiber_interface",
    "电源模块": "power_module",
}
_UPPER = {"光口数量", "光口数", "数量", "ethernet", "以太网端口数", "以太网端口数量"}
_AMBIGUOUS = {"最大功率", "输入功率"}
_QUALIFIERS = ("实测", "申报", "要求", "额定", "标称", "设计", "输入", "输出", "工作")
_CANONICAL = (
    set(UNITS)
    | COUNTS
    | {
        "interface_config",
        "required_inventory_entry",
        "fiber_interface",
        "power_module",
    }
)
_CONTEXT_ONLY = {"fiber_interface", "power_module", "ethernet_ports", "port_count"}


def normalize_attribute(raw: str) -> AttributeNormalization:
    """Normalize only explicit identities and exact labels, preserving raw text."""
    key = " ".join(unicodedata.normalize("NFKC", raw).split()).casefold()
    # Ignore a separator only in an explicitly typed interface label.
    port = port_type(re.sub(r" (?=端口)", "", key))
    if port:
        exact = f"{port.lower()}_ports"
    else:
        exact = _EXACT_LABELS.get(key) or (key if key in _CANONICAL else None)
    if exact:
        state = "non_comparable" if exact in _CONTEXT_ONLY else "exact"
        rule = "canonical" if raw == exact else "exact_label"
    elif key in _UPPER:
        state, rule = "broader", "no_narrower_identity"
    elif key in _AMBIGUOUS:
        state, rule = "ambiguous", "ambiguous_label"
    elif key.startswith(_QUALIFIERS) or key.endswith(_QUALIFIERS):
        state, rule = "qualified", "qualifier_retained"
    else:
        state, rule = "unknown", "unrecognized_label"
    return {
        "raw": raw,
        "identity": exact or raw,
        "state": state,
        "rule": rule,
        "blocker": None if state == "exact" else f"ATTRIBUTE_{state.upper()}",
    }
