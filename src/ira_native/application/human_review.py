"""Build immutable inputs and derive formal gates from scoped human records."""

from ira_native.domain.models import ANNOTATION_RISK

COMMON_FIELDS = {
    "equipment_class": "equipment_class",
    "model": "manufacturer_model",
    "config_variant": "configuration_variant",
    "scope": "scope",
    "site_group": "site_group",
    "requirement_slot": "requirement_slot",
    "condition": "condition",
    "source_revision": "source_revision",
}
OPTIONAL_ABSENCE = {"site_group", "requirement_slot", "condition", "model", "inclusion_status"}
GATE_NAMES = {"source", "binding", "applicability"}


def review_fields(requirement: dict, declaration: dict, pairing: dict) -> dict:
    fields = {}
    slot = pairing["declaration"] or {}
    for side, item in (("requirement", requirement), ("declaration", declaration)):
        available = item["field_evidence"]
        metadata = item["field_values"]
        same = {name: (item.get(name), [source]) for name, source in COMMON_FIELDS.items()}
        if requirement.get("part_number") or declaration.get("part_number"):
            same["part_number"] = (item.get("part_number"), ["part_number"])
        if side == "requirement":
            derivations = item.get("field_derivations", {})
            expression_fields = derivations.get("expr", ["expr"])
            specific = {
                "value": (item.get("numeric_value") or item.get("expr"), expression_fields),
                "operator": (item.get("operator"), expression_fields),
                "property": (item.get("property"), ["property", *expression_fields]),
                "unit": (item.get("unit"), ["unit", "config"]),
                "count_basis": (item.get("count_basis"), ["count_basis"]),
                "strength": (item.get("strength"), ["strength"]),
                "quantity_role": (
                    item.get("quantity_role"),
                    derivations.get("quantity_role", ["quantity_role"]),
                ),
                "inclusion_status": (item.get("inclusion_status"), ["inclusion_status"]),
            }
        else:
            value_field = slot.get("source_field")
            specific = {
                "value": (slot.get("value", {}).get("normalized"), [value_field]),
                "operator": (
                    "=" if value_field and pairing["field"] != "interface_config" else None,
                    [value_field],
                ),
                "property": (pairing["field"], ["property", value_field]),
                "unit": (slot.get("unit"), [slot.get("unit_source_field", "unit"), value_field]),
                "count_basis": (slot.get("count_basis"), ["count_basis", value_field]),
            }
        for name, (normalized, origins) in (same | specific).items():
            provenance = [field for field in available if field["field_name"] in origins]
            state = metadata.get(origins[0], {}).get("state") if origins else None
            absence = (
                (
                    name in OPTIONAL_ABSENCE
                    or name == "quantity_role"
                    and requirement["property"] != "device_quantity"
                )
                and normalized in (None, "", "unknown")
                and state in (None, "not_stated", "not_applicable", "unconstrained")
            )
            config_operator = name == "operator" and requirement["property"] == "interface_config"
            observed = (
                normalized not in (None, "", "unknown")
                and any(f["support_state"] != "insufficient" for f in provenance)
                and not any(f.get("cell_type") for f in provenance)
            )
            if name == "quantity_role" and len(origins) > 1:
                observed = observed and set(origins) == {
                    field["field_name"]
                    for field in provenance
                    if field["support_state"] != "insufficient"
                }
            fields[f"{side}.{name}"] = {
                "object_side": side,
                "object_id": item.get("candidate_id") or item["item_id"],
                "field_name": name,
                "raw": {field["field_name"]: field["raw_span"] for field in provenance},
                "normalized": normalized,
                "value_state": state,
                "provenance": provenance,
                "record_locator": {
                    key: item[key]
                    for key in (
                        "source_asset_id",
                        "evidence_id",
                        "physical_page",
                        "source_pointer",
                        "row_in_block",
                        "sheet_or_section",
                        "row_id",
                    )
                    if key in item
                },
                "reviewable": observed or absence or config_operator,
            }
    return fields


def active_records(history: list[dict], snapshot_id: str) -> dict:
    active = {}
    for entry in history:
        if entry["snapshot_id"] == snapshot_id:
            for target in entry["targets"]:
                active[target] = entry
    return active


def assess_trust(inputs: dict, history: list[dict], snapshot_id: str) -> dict:
    records = active_records(history, snapshot_id)
    edge, fields = inputs["edge"], inputs["fields"]

    def decision(target):
        return records.get(target, {}).get("decision")

    missing_fields = [
        target
        for target, field in fields.items()
        if not field["reviewable"] or decision(target) != "confirmed"
    ]
    not_applicable = decision("applicability") == "not_applicable"
    comparison_blockers = [
        reason.upper()
        for reason in edge["comparison_blockers"]
        if reason != "unit_requires_comparison"
        and not (not_applicable and reason == "condition_unproven")
    ]
    binding_conflicts = [
        reason.upper()
        for reason in edge["confirmation_basis"]
        if reason in ("model_conflict", "model_unresolved")
    ]
    blockers = []
    native = inputs["source"]
    if native["parse_run"]["binding_state"] == "mismatch" or native["evidence"]["source_state"] in (
        "mismatch",
        "denied",
    ):
        blockers.append("SOURCE_MISMATCH")
    if decision("source") != "confirmed":
        blockers.append("SOURCE_NOT_REVIEWED")
    if decision("binding") != "confirmed":
        blockers.append("ENTITY_BINDING_NOT_REVIEWED")
    if decision("applicability") not in ("confirmed", "not_applicable"):
        blockers.append("APPLICABILITY_NOT_REVIEWED")
    if missing_fields:
        blockers.append("FIELDS_NOT_VERIFIED")
    if not inputs["same_revision"]:
        blockers.append("REVISION_UNKNOWN")
    if revision_blocker := inputs.get("revision_blocker"):
        blockers.append(revision_blocker)
    if source_blocker := inputs.get("calculation_source_blocker"):
        blockers.append(source_blocker)
    if ANNOTATION_RISK in inputs["requirement_risk"]:
        blockers.append(ANNOTATION_RISK)
    if "continuation_unverified" in inputs["requirement_risk"]:
        blockers.append("CONTINUATION_UNVERIFIED")
    blockers += comparison_blockers + binding_conflicts
    blockers += [reason.upper() for reason in edge["applicability_blockers"]]
    excluded = inputs["inclusion_status"] in ("excluded", "reference_out_of_scope") or inputs.get(
        "explicit_non_applicability", False
    )
    left_condition = fields["requirement.condition"]["normalized"]
    right_condition = fields["declaration.condition"]["normalized"]
    if excluded and not not_applicable:
        blockers.append("APPLICABILITY_CONFLICT")
    if not_applicable and not (
        excluded or left_condition and right_condition and left_condition != right_condition
    ):
        blockers.append("NON_APPLICABILITY_NOT_SUPPORTED")
    return {
        "blockers": list(dict.fromkeys(blockers)),
        "unverified_fields": missing_fields,
        "field_state": "not_verified" if missing_fields else "verified",
        "source_review": decision("source"),
        "binding_review": decision("binding"),
        "applicability_review": decision("applicability"),
        "revision_relation": "explicit_supersedes"
        if inputs.get("effective_replacement")
        else "same_version"
        if inputs["same_revision"]
        else "unknown",
        "review_ids": sorted({record["review_id"] for record in records.values()}),
    }
