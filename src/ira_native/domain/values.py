"""Conservative scalar field semantics, independent of provenance and support."""

import re
from dataclasses import dataclass
from enum import StrEnum

from pydantic import JsonValue


class ValueState(StrEnum):
    PRESENT = "present"
    NOT_STATED = "not_stated"
    EXPLICIT_ABSENCE = "explicit_absence"
    NOT_APPLICABLE = "not_applicable"
    UNCONSTRAINED = "unconstrained"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class FieldValue:
    raw: JsonValue
    normalized: str | None
    state: ValueState
    semantic_basis: str | None = None


_PLACEHOLDER_FIELDS = {
    "manufacturer_model",
    "site_group",
    "quantity",
    "scope",
    "configuration_variant",
    "requirement_slot",
    "condition",
    "strength",
}
_PLACEHOLDERS = {"—", "–", "-", "n/a"}
_BASIS = {"per_device", "per_site", "per_site_group", "per_contract_scope", "total"}
_CODE = re.compile(r"[^()（）]+(?:\(([a-z_]+)\)|（([a-z_]+)）)")
_PORT = re.compile(r"\s*(\d{1,6})\s*[x×]\s*(GPON|POTS|FE|GE)(?:\s+ports?)?\s*", re.I)


def field_value(
    field: str,
    raw: JsonValue,
    *,
    semantic_state: ValueState | None = None,
    semantic_basis: str | None = None,
) -> FieldValue:
    if semantic_state is not None:
        if semantic_state not in (
            ValueState.NOT_APPLICABLE,
            ValueState.UNCONSTRAINED,
            ValueState.EXPLICIT_ABSENCE,
        ):
            raise ValueError("Unsupported explicit semantic state")
        if not semantic_basis or not semantic_basis.strip():
            raise ValueError("Explicit semantic state requires a source basis")
        return FieldValue(raw, None, semantic_state, semantic_basis)
    if raw is None or (isinstance(raw, str) and not raw.strip()):
        return FieldValue(raw, None, ValueState.NOT_STATED)
    if not isinstance(raw, (str, int, float)) or isinstance(raw, bool):
        return FieldValue(raw, None, ValueState.UNKNOWN)
    text = str(raw).strip()
    if field in _PLACEHOLDER_FIELDS and text.casefold() in _PLACEHOLDERS | {
        "未知",
        "unknown",
        "未注明",
    }:
        return FieldValue(raw, None, ValueState.UNKNOWN)
    if field == "count_basis":
        if text == "per_scope":
            text = "per_contract_scope"
        if match := _CODE.fullmatch(text):
            text = match[1] or match[2]
        if text not in _BASIS:
            return FieldValue(raw, None, ValueState.UNKNOWN)
    return FieldValue(raw, text, ValueState.PRESENT)


def parse_configuration(expression: str) -> dict[str, int]:
    configuration: dict[str, int] = {}
    for segment in expression.split("+"):
        match = _PORT.fullmatch(segment)
        if match is None:
            return {}
        count, interface = match.groups()
        name = interface.upper()
        configuration[name] = configuration.get(name, 0) + int(count)
    return configuration
