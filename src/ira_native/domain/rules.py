"""Deterministic GPON requirement evaluation and constraint comparison."""

import re
from collections import Counter
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from decimal import Decimal, localcontext
from functools import lru_cache
from typing import Literal, TypedDict

from pint import UnitRegistry
from pydantic import BaseModel, ValidationError

RULESET_VERSION = "gpon/1"
Evaluation = Literal["satisfied", "violated", "not_applicable", "undetermined"]
Disposition = Literal["no_issue", "issue", "informational", "review_required", "blocked"]
Relation = Literal["equivalent", "compatible", "contradictory", "not_comparable", "unknown"]
Strictness = Literal["left_stricter", "right_stricter", "same", "unknown"]
Facts = Mapping[str, object]


class RuleResult(TypedDict):
    requirement_evaluation: Evaluation | None
    finding_disposition: Disposition
    reason_code: str
    applicability: Literal["applicable", "not_applicable", "unknown"]
    value_role: str | None
    constraint_relation: Relation | None
    strictness: Strictness | None


class Term(BaseModel):
    equipment_class: str
    value_role: str
    property: str
    model: str | None = None
    config_variant: str | None = None
    requirement_slot: str | None = None
    site_group: str | None = None
    scope: str | None = None
    condition: str | None = None
    count_basis: str | None = None
    strength: str = "unknown"
    quantity_role: str | None = None
    inclusion_status: str | None = None
    source_revision: str | None = None
    expr: str | None = None
    unit: str | None = None


class RuleInputError(ValueError):
    pass


COUNTS = {
    "device_quantity",
    "ethernet_ports",
    "gpon_ports",
    "pots_ports",
    "fe_ports",
    "ge_ports",
    "port_count",
}
COUNT_BASES = {
    "per_device",
    "per_site",
    "per_site_group",
    "per_contract_scope",
    "total",
    "per_scope",
}  # Historical logic seeds use per_scope.
COUNT_UNITS = {"台", "端", "块", "套", "个", "port", "ports"}
POWER = {"W": "watt", "kW": "kilowatt", "mW": "milliwatt"}
DISTANCE = {"m": "meter", "km": "kilometer"}
UNITS = {
    "test_duration": {"s": "second", "min": "minute", "h": "hour"},
    "maximum_power": POWER,
    "rated_power": POWER,
    "power_consumption": POWER,
    "distance": DISTANCE,
    "transmission_distance": DISTANCE,
    "voltage": {"V": "volt", "mV": "millivolt"},
    "temperature": {"°C": "degC", "K": "kelvin"},
    "optical_power": {"dBm": "dBm"},
    "receiver_sensitivity": {"dBm": "dBm"},
    "optical_budget": {"dB": "dB"},
}
NUMBER = r"[+-]?(?:[0-9]{1,24}(?:\.[0-9]{1,24})?|\.[0-9]{1,24})"
POINT = re.compile(rf"(>=|<=|=|>|<|≥|≤)?\s*({NUMBER})")
RANGE = re.compile(rf"(\[|\()\s*({NUMBER})\s*,\s*({NUMBER})\s*(\]|\))")
PORT = re.compile(
    r"([1-9][0-9]{0,5})\s*[xX×]\s*(GPON|POTS|FE|GE|10GE)(?:\s+ports?)?", re.IGNORECASE
)
BLOCKED = {"SOURCE_MISMATCH", "PERMISSION_DENIED"}
OUTSIDE = {"SCOPE_MISMATCH", "CONDITION_MISMATCH"}


def _result(
    state: Evaluation | None,
    disposition: Disposition,
    reason: str,
    right: Term | None = None,
    *,
    relation: Relation | None = None,
    strictness: Strictness | None = None,
) -> RuleResult:
    return {
        "requirement_evaluation": state,
        "finding_disposition": disposition,
        "reason_code": reason,
        "applicability": (
            "not_applicable"
            if state == "not_applicable"
            else "applicable"
            if state in ("satisfied", "violated")
            or relation in ("equivalent", "compatible", "contradictory")
            else "unknown"
        ),
        "value_role": right.value_role if right is not None else None,
        "constraint_relation": relation,
        "strictness": strictness,
    }


def _source_gap(facts: Facts) -> str | None:
    source = facts.get("source_assumption")
    if source in ("mismatch", "source_mismatch"):
        return "SOURCE_MISMATCH"
    if source in ("denied", "permission_denied"):
        return "PERMISSION_DENIED"
    if source == "parse_suspicious":
        return "EXTRACTION_RISK"
    if source == "visual_ambiguous":
        return "OCR_CHARACTER_AMBIGUITY"
    if source == "verified_for_checked_fields" or (
        source == "fixture_field_supported" and facts.get("document_scope") == "synthetic_only"
    ):
        return None
    return "FIELD_SUPPORT_UNKNOWN"


def _revision_gap(left: Term, facts: Facts) -> str | None:
    if facts.get("revision_relation") == "same_version":
        return None
    if (
        facts.get("revision_relation") == "explicit_supersedes"
        and facts.get("amendment_scope_confirmed") is True
        and left.source_revision
        and left.source_revision == facts.get("effective_requirement_revision")
    ):
        return None
    return "REVISION_UNKNOWN"


def _applicability(term: Term, facts: Facts) -> str | None:
    if term.inclusion_status == "reference_out_of_scope" or term.quantity_role == "quotation_only":
        return "OUT_OF_SCOPE_REFERENCE"
    if term.inclusion_status == "excluded" or facts.get("applicability") == "not_applicable":
        return "OUT_OF_SCOPE"
    if term.inclusion_status not in (None, "in_scope") or facts.get("applicability") not in (
        None,
        "applicable",
    ):
        return "APPLICABILITY_UNKNOWN"
    return None


def _comparison_gap(
    left: Term, right: Term, facts: Facts, *, comparing: bool = False
) -> str | None:
    if left.equipment_class not in ("OLT", "ONU") or left.equipment_class != right.equipment_class:
        return "EQUIPMENT_CLASS_MISMATCH"
    binding = facts.get("entity_binding")
    if (
        binding == "same_class_distinct_configuration"
        or left.config_variant
        and right.config_variant
        and left.config_variant != right.config_variant
    ):
        return "CONFIG_VARIANT_MISMATCH"
    if bool(left.config_variant) != bool(right.config_variant):
        return "CONFIG_VARIANT_UNKNOWN"
    if (
        left.requirement_slot
        and right.requirement_slot
        and left.requirement_slot != right.requirement_slot
    ):
        return "MODEL_BINDING_UNCONFIRMED"
    # Defer an exact-model issue until the other comparison gates pass.
    if binding != "confirmed" and not _exact_model_difference(left, right, facts):
        return (
            "MODEL_IDENTITY_UNKNOWN"
            if left.model and not right.model
            else "MODEL_BINDING_UNCONFIRMED"
        )
    if left.property != right.property:
        return "PROPERTY_DIFFERENCE"
    if not left.scope or not right.scope:
        return "SCOPE_UNKNOWN"
    if left.scope != right.scope:
        return "SCOPE_MISMATCH"
    if left.site_group != right.site_group:
        return "SCOPE_MISMATCH" if left.site_group and right.site_group else "SCOPE_UNKNOWN"
    if (left.condition or comparing) and left.condition != right.condition:
        return "CONDITION_MISMATCH" if left.condition and right.condition else "CONDITION_UNKNOWN"
    if (
        left.property in COUNTS
        or left.property == "interface_config"
        or left.count_basis is not None
        or right.count_basis is not None
    ):
        if left.count_basis not in COUNT_BASES or left.count_basis != right.count_basis:
            return "COUNT_BASIS_UNKNOWN"
    return None


def _exact_model_difference(left: Term, right: Term, facts: Facts) -> bool:
    return bool(
        left.model
        and right.model
        and left.model != right.model
        and facts.get("entity_binding") in ("confirmed", "confirmed_distinct")
        and facts.get("same_requirement_slot") is True
        and facts.get("exact_model_mandatory") is True
    )


@lru_cache(maxsize=1)
def _registry() -> UnitRegistry:
    return UnitRegistry(non_int_type=Decimal)


def _converter(left: Term, right: Term) -> Callable[[Decimal], Decimal]:
    if left.property in COUNTS:
        if left.unit not in COUNT_UNITS or left.unit != right.unit:
            raise RuleInputError("COUNT_UNIT_SEMANTICS_UNKNOWN")
        return lambda value: value
    units = UNITS.get(left.property)
    if units is None:
        raise RuleInputError("UNSUPPORTED_EXPRESSION")
    if left.unit not in units or right.unit not in units:
        raise RuleInputError("UNIT_KIND_MISMATCH")
    if left.unit == right.unit:
        return lambda value: value
    registry = _registry()
    return lambda value: registry.Quantity(value, units[right.unit]).to(units[left.unit]).magnitude


@dataclass(frozen=True)
class Interval:
    lower: Decimal | None
    upper: Decimal | None
    include_lower: bool = True
    include_upper: bool = True

    def contains(self, value: Decimal) -> bool:
        return self.includes(Interval(value, value))

    def includes(self, other: "Interval") -> bool:
        lower_ok = self.lower is None or (
            other.lower is not None
            and (
                other.lower > self.lower
                or other.lower == self.lower
                and (self.include_lower or not other.include_lower)
            )
        )
        upper_ok = self.upper is None or (
            other.upper is not None
            and (
                other.upper < self.upper
                or other.upper == self.upper
                and (self.include_upper or not other.include_upper)
            )
        )
        return lower_ok and upper_ok

    def before(self, other: "Interval") -> bool:
        return (
            self.upper is not None
            and other.lower is not None
            and (
                self.upper < other.lower
                or (self.upper == other.lower and not (self.include_upper and other.include_lower))
            )
        )

    def convert(self, convert: Callable[[Decimal], Decimal]) -> "Interval":
        return Interval(
            convert(self.lower) if self.lower is not None else None,
            convert(self.upper) if self.upper is not None else None,
            self.include_lower,
            self.include_upper,
        )


def _interval(term: Term, *, scalar: bool = False) -> tuple[Interval, str]:
    expr = (term.expr or "").strip()
    if point := POINT.fullmatch(expr):
        operator, number = point.groups()
        operator = {"≥": ">=", "≤": "<="}.get(operator, operator or "=")
        value = Decimal(number)
        interval = {
            "=": Interval(value, value),
            ">=": Interval(value, None),
            ">": Interval(value, None, include_lower=False),
            "<=": Interval(None, value),
            "<": Interval(None, value, include_upper=False),
        }[operator]
    elif span := RANGE.fullmatch(expr):
        start, lower, upper, end = span.groups()
        operator = "range"
        interval = Interval(Decimal(lower), Decimal(upper), start == "[", end == "]")
    else:
        raise RuleInputError("UNSUPPORTED_EXPRESSION")
    if scalar and operator != "=":
        raise RuleInputError("UNSUPPORTED_EXPRESSION")
    lower, upper = interval.lower, interval.upper
    if term.property in COUNTS:
        if any(v is not None and (v < 0 or v != v.to_integral_value()) for v in (lower, upper)):
            raise RuleInputError("UNSUPPORTED_EXPRESSION")
        interval = Interval(
            Decimal(0) if lower is None else lower + int(not interval.include_lower),
            None if upper is None else upper - int(not interval.include_upper),
        )
    if interval.before(interval):
        raise RuleInputError("UNSUPPORTED_EXPRESSION")
    return interval, operator


def _ports(term: Term) -> Counter[str]:
    if term.unit != "configuration" or not term.expr or len(term.expr) > 512:
        raise RuleInputError("CONFIGURATION_NOT_EQUIVALENT_PROVEN")
    result: Counter[str] = Counter()
    for component in term.expr.split("+"):
        if not (match := PORT.fullmatch(component.strip())):
            raise RuleInputError("CONFIGURATION_NOT_EQUIVALENT_PROVEN")
        count, kind = match.groups()
        result[kind.upper()] += int(count)
    return result


def _enum_values(term: Term, *, scalar: bool = False) -> frozenset[str]:
    expr = (term.expr or "").strip()
    if term.unit != "enum" or len(expr) > 512:
        raise RuleInputError("UNSUPPORTED_EXPRESSION")
    if not scalar and expr.startswith("{") and expr.endswith("}"):
        values = [item.strip() for item in expr[1:-1].split(",")]
    else:
        values = [expr.removeprefix("=").strip()]
    if not all(
        value and value.isprintable() and not any(c in value for c in "{},=><") for value in values
    ):
        raise RuleInputError("UNSUPPORTED_EXPRESSION")
    return frozenset(values)


def _resolved(satisfied: bool, reason: str, right: Term | None) -> RuleResult:
    return _result(
        "satisfied" if satisfied else "violated",
        "no_issue" if satisfied else "issue",
        reason,
        right,
    )


def _advisory(term: Term) -> bool:
    return term.strength in ("reference", "informative") or term.quantity_role == "estimate"


def _value_test(left: Term, right: Term) -> tuple[bool, str]:
    if left.property == "interface_config":
        return _ports(left) == _ports(right), "configuration"
    if left.property == "required_inventory_entry":
        if (
            left.unit == right.unit == "boolean"
            and left.expr == "present"
            and right.expr in ("present", "absent")
        ):
            return right.expr == "present", "presence"
        raise RuleInputError("UNSUPPORTED_EXPRESSION")
    if left.unit == "enum":
        return _enum_values(right, scalar=True) <= _enum_values(left), "enum"
    with localcontext() as context:
        context.prec = 80
        convert = _converter(left, right)
        interval, operator = _interval(left)
        scalar, _ = _interval(right, scalar=True)
        assert scalar.lower is not None
        return interval.contains(convert(scalar.lower)), operator


def _rule_reason(left: Term, right: Term, matches: bool, operator: str, facts: Facts) -> str:
    if matches and facts.get("revision_relation") == "explicit_supersedes":
        return "EXPLICIT_REVISION_APPLIED"
    if operator in (">", ">="):
        return "LOWER_BOUND_SATISFIED" if matches else "LOWER_BOUND_VIOLATED"
    if operator in ("<", "<="):
        return "UPPER_BOUND_SATISFIED" if matches else "UPPER_BOUND_VIOLATED"
    if operator == "range":
        return "RANGE_SATISFIED" if matches else "OUTSIDE_RANGE"
    if operator == "configuration":
        return "CONFIGURATION_MATCH" if matches else "CONFIGURATION_DIFFERENCE"
    if operator == "enum":
        return "ENUM_MATCH" if matches else "ENUM_DIFFERENCE"
    if matches:
        return "UNIT_CONVERSION_MATCH" if left.unit != right.unit else "RULE_MATCH"
    return (
        "MANDATORY_QUANTITY_DIFFERENCE"
        if left.property == "device_quantity"
        else "VALUE_DIFFERENCE"
    )


def _review(reason: str, right: Term | None = None) -> RuleResult:
    return _result(
        "not_applicable" if reason in OUTSIDE else "undetermined",
        "blocked" if reason in BLOCKED else "review_required",
        reason,
        right,
    )


def evaluate(
    requirement: Mapping[str, object],
    declaration: Mapping[str, object] | None,
    preconditions: Mapping[str, object],
) -> RuleResult:
    """Evaluate an asserted value, or route a second requirement to the separate comparator."""
    if declaration is not None and declaration.get("value_role") == "another_requirement":
        return compare_requirements(requirement, declaration, preconditions)
    facts = preconditions
    if gap := _source_gap(facts):
        return _review(gap)
    try:
        left = Term.model_validate(requirement)
        right = Term.model_validate(declaration) if declaration is not None else None
    except ValidationError:
        return _review("INVALID_RULE_INPUT")
    if gap := _revision_gap(left, facts):
        return _review(gap, right)
    if left.equipment_class not in ("OLT", "ONU"):
        return _review("EQUIPMENT_CLASS_MISMATCH", right)
    if left.value_role != "requirement" or (
        right and right.value_role not in ("declaration", "observed")
    ):
        return _review("VALUE_ROLE_UNKNOWN", right)
    if gap := _applicability(left, facts):
        if gap == "APPLICABILITY_UNKNOWN":
            return _review(gap, right)
        return _result("not_applicable", "informational", gap, right)
    if right is None:
        if (
            left.strength == "mandatory"
            and not _advisory(left)
            and left.property == "required_inventory_entry"
            and left.expr == "present"
            and left.unit == "boolean"
            and left.scope
            and left.count_basis in COUNT_BASES
            and facts.get("same_requirement_slot") is True
            and facts.get("entity_binding") == "confirmed"
            and facts.get("record_completeness") == "explicitly_confirmed_complete"
            and (not left.condition or facts.get("applicability") == "applicable")
        ):
            return _resolved(False, "REQUIRED_RECORD_ABSENT_CONFIRMED", None)
        return _review("NO_EVIDENCE_FOUND")
    if gap := _comparison_gap(left, right, facts):
        return _review(gap, right)
    if left.strength not in ("mandatory", "reference", "informative"):
        return _review("REQUIREMENT_STRENGTH_UNKNOWN", right)
    if left.quantity_role == "unknown":
        return _review("QUANTITY_ROLE_UNKNOWN", right)
    if _exact_model_difference(left, right, facts):
        if _advisory(left):
            return _result("not_applicable", "informational", "REFERENCE_MODEL_DIFFERENCE", right)
        return _resolved(False, "EXACT_MODEL_REQUIRED_DIFFERENCE", right)
    try:
        matches, operator = _value_test(left, right)
    except RuleInputError as error:
        return _review(str(error), right)
    if _advisory(left):
        reason = "REFERENCE_QUANTITY_MATCH" if matches else "REFERENCE_QUANTITY_DEVIATION"
        return _result("not_applicable", "informational", reason, right)
    return _resolved(matches, _rule_reason(left, right, matches, operator, facts), right)


def _relation(
    relation: Relation,
    strictness: Strictness = "unknown",
    reason: str = "REQUIREMENT_RELATION_ONLY",
    right: Term | None = None,
) -> RuleResult:
    disposition = (
        "blocked"
        if reason in BLOCKED
        else "review_required"
        if relation in ("unknown", "not_comparable")
        else "informational"
    )
    return _result(None, disposition, reason, right, relation=relation, strictness=strictness)


def compare_requirements(
    left: Mapping[str, object],
    right: Mapping[str, object],
    preconditions: Mapping[str, object],
) -> RuleResult:
    """Compare permitted value sets without producing a satisfaction state."""
    facts = preconditions
    if gap := _source_gap(facts):
        return _relation("unknown", reason=gap)
    try:
        first, second = Term.model_validate(left), Term.model_validate(right)
    except ValidationError:
        return _relation("unknown", reason="INVALID_RULE_INPUT")
    if first.value_role != "requirement" or second.value_role not in (
        "requirement",
        "another_requirement",
    ):
        return _relation("not_comparable", reason="VALUE_ROLE_UNKNOWN", right=second)
    if gap := _revision_gap(first, facts):
        return _relation("unknown", reason=gap, right=second)
    for term in (first, second):
        if gap := _applicability(term, facts):
            return _relation(
                "unknown" if gap == "APPLICABILITY_UNKNOWN" else "not_comparable",
                reason=gap,
                right=second,
            )
    if gap := _comparison_gap(first, second, facts, comparing=True):
        state = (
            "not_comparable"
            if gap in OUTSIDE
            or gap in ("PROPERTY_DIFFERENCE", "EQUIPMENT_CLASS_MISMATCH", "CONFIG_VARIANT_MISMATCH")
            else "unknown"
        )
        return _relation(state, reason=gap, right=second)
    if _exact_model_difference(first, second, facts):
        return _relation("not_comparable", reason="MODEL_BINDING_UNCONFIRMED", right=second)
    try:
        if first.property == "interface_config":
            matches = _ports(first) == _ports(second)
            return _relation(
                "equivalent" if matches else "contradictory",
                "same" if matches else "unknown",
                right=second,
            )
        if first.unit == "enum":
            first_values, second_values = _enum_values(first), _enum_values(second)
            if first_values == second_values:
                return _relation("equivalent", "same", right=second)
            if not first_values & second_values:
                return _relation("contradictory", right=second)
            if first_values < second_values:
                return _relation("compatible", "left_stricter", right=second)
            if second_values < first_values:
                return _relation("compatible", "right_stricter", right=second)
            return _relation("compatible", right=second)
        with localcontext() as context:
            context.prec = 80
            convert = _converter(first, second)
            left_interval, _ = _interval(first)
            right_interval, _ = _interval(second)
            right_interval = right_interval.convert(convert)
    except RuleInputError as error:
        reason = str(error)
        return _relation(
            "not_comparable" if reason == "UNIT_KIND_MISMATCH" else "unknown",
            reason=reason,
            right=second,
        )
    if left_interval == right_interval:
        return _relation("equivalent", "same", right=second)
    if left_interval.before(right_interval) or right_interval.before(left_interval):
        return _relation("contradictory", right=second)
    if right_interval.includes(left_interval):
        return _relation("compatible", "left_stricter", right=second)
    if left_interval.includes(right_interval):
        return _relation("compatible", "right_stricter", right=second)
    return _relation("compatible", right=second)
