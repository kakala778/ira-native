"""Measure the existing workflow against locator-keyed development Gold."""

from collections import Counter, defaultdict
from dataclasses import dataclass
from pathlib import Path

from ira_native.application.service import EvidenceService
from ira_native.application.workflow import VerificationWorkflow
from ira_native.domain.candidate_matching import assess
from ira_native.domain.rules import evaluate
from ira_native.errors import IraError
from ira_native.settings import Settings

from .task002a_inputs import Anchor, Inputs, UnsupportedInput, materialize

COMMON = {
    "equipment_class": "equipment_class",
    "manufacturer_model": "model",
    "configuration_variant": "config_variant",
    "scope": "scope",
    "site_group": "site_group",
    "count_basis": "count_basis",
    "condition": "condition",
    "source_revision": "source_revision",
}
FIELDS = {
    "requirements": COMMON
    | {
        "attribute": "property",
        "expression.normalized": "expr",
        "expression.unit": "unit",
        "strength": "strength",
        "quantity_role": None,
        "inclusion_status": None,
        "source_revision": None,
        "requirement_slot": "requirement_slot",
        "expression.operator": None,
        "strength_candidates": None,
    },
    "declarations": COMMON
    | {
        "quantity_raw": "raw.quantity",
        "unit": "unit",
        "interface_config.raw": "raw.config",
        "device_quantity": "declaration_fields.device_quantity",
        "interface_port_counts": "declaration_fields.interface_port_counts",
        "technical_attributes": "declaration_fields.technical_attributes",
    },
}
PROVENANCE = {
    "manufacturer_model": {"manufacturer_model"},
    "attribute": {"property"},
    "expression.raw": {"quantity", "expr", "config"},
    "expression.unit": {"unit"},
    "device_quantity": {"quantity"},
    "interface_config": {"config"},
} | {
    value: {value}
    for value in (
        "equipment_class",
        "configuration_variant",
        "scope",
        "site_group",
        "count_basis",
        "condition",
        "source_revision",
        "unit",
        "strength",
        "quantity_role",
        "inclusion_status",
    )
}
POSITIVE = {"candidate_same_slot", "ambiguous_same_slot", "ambiguous_one_to_many"}
COMPARABLE_STAGES = {
    "review_required",
    "candidate_supported_but_not_system_confirmed",
    "candidate_but_not_evaluable",
}
UNKNOWN = {"unknown_not_stated", "explicit_absence", "not_stated", "empty_cell", "not_constrained"}
MISSING = object()


@dataclass
class Observation:
    requirements: dict[Anchor, dict]
    declarations: dict[Anchor, dict]
    pairs: set[tuple[Anchor, Anchor]]
    edges: dict[tuple[Anchor, Anchor], dict]
    diagnostic: dict
    workflow: dict
    claims: int


def observe(inputs: Inputs, database: Path) -> tuple[Observation | None, dict]:
    """Run the existing services on materialized bytes before accessing any reference."""
    stage = "registry"
    try:
        with EvidenceService(Settings(database, enable_uploads=True)) as service:
            app = VerificationWorkflow(service)
            stage = "parse_ingest"
            run = app.ingest_pair_bytes(inputs.pdf, inputs.parsed).parse_run
            stage = "spreadsheet_ingest"
            imported = app.ingest_spreadsheet(
                inputs.spreadsheet, inputs.filename, inputs.mapping, sheet=inputs.sheet
            )
            stage = "candidate_extraction"
            draft = app.prepare(run.parse_run_id, imported["import_id"])
            diagnostic = app.diagnose_requirements(run.parse_run_id)
            stage = "evidence_association"
            reqs = {}
            for item in draft["requirements"]:
                locator = inputs.requirements.get((item["source_pointer"], item["row_in_block"]))
                if locator is None:
                    return None, {
                        "status": "error",
                        "stage": stage,
                        "reason": "requirement_locator_unmapped",
                    }
                evidence = service.get_evidence(item["evidence_id"])
                if (
                    evidence.parse_run_id != run.parse_run_id
                    or evidence.source_pointer != item["source_pointer"]
                    or evidence.physical_page != item["physical_page"]
                ):
                    return None, {
                        "status": "error",
                        "stage": stage,
                        "reason": "requirement_evidence_unresolved",
                    }
                reqs[locator] = item
            decls = {}
            for item in draft["declarations"]:
                locator = inputs.declarations.get(item["row_id"])
                if locator is None:
                    return None, {
                        "status": "error",
                        "stage": stage,
                        "reason": "declaration_locator_unmapped",
                    }
                decls[locator] = item

            stage = "candidate_pairing"
            req_ids = {item["candidate_id"]: a for a, item in reqs.items()}
            decl_ids = {item["item_id"]: a for a, item in decls.items()}
            edges = {
                (req_ids[e["left_ref"]], decl_ids[e["right_ref"]]): e
                for e in draft["candidate_edges"]
            }
            pairs, claims = set(edges), []
            for left, req in reqs.items():
                options = sorted(right for right in decls if (left, right) in pairs)
                if len(options) == 1:
                    # Exercise the strongest current self-report, separately from Gold.
                    claims.append(
                        {
                            "requirement_id": req["candidate_id"],
                            "declaration_id": decls[options[0]]["item_id"],
                            "fields_checked": True,
                            "same_version": True,
                        }
                    )
            stage = "workflow_gate"
            result = app.verify(draft["draft_id"], claims, "Synthetic evaluation")
    except IraError as error:
        return None, {"status": "error", "stage": stage, "reason": error.code}
    return Observation(reqs, decls, pairs, edges, diagnostic, result, len(claims)), {
        "status": "complete"
    }


def anchor(locator: dict) -> Anchor | None:
    if locator.get("table_id") and locator.get("row_id") is not None:
        return (
            "table",
            locator["source_id"],
            locator["physical_page"],
            locator["table_id"],
            str(locator["row_id"]),
        )
    if locator.get("sheet_or_section") and locator.get("row_id") is not None:
        return "sheet", locator["source_id"], locator["sheet_or_section"], str(locator["row_id"])
    if locator.get("section_heading"):
        return "section", locator["source_id"], locator["physical_page"], locator["section_heading"]
    return None


def reference_anchor(item: dict) -> Anchor | None:
    # Pick the reference object's identity from its authored class locator,
    # not from any extracted fields, emitted ordering, or detected values.
    anchors = {
        anchor(field["locator"])
        for field in item["field_evidence"]
        if field["field_name"] == "equipment_class"
    } - {None}
    return next(iter(anchors)) if len(anchors) == 1 else None


def value_at(item: dict, path: str):
    value = item
    for field in path.split("."):
        if not isinstance(value, dict) or field not in value:
            return MISSING
        value = value[field]
    return value


def score_field(expected, actual, path):
    if path is None:
        return "not_implemented"
    if actual is None:
        return "not_observed"
    emitted = value_at(actual, path)
    if emitted is MISSING or (emitted is None and expected is not None):
        return "not_observed"
    if path == "declaration_fields.device_quantity" and expected is not None:
        keys = ("normalized", "unit", "count_basis")
        emitted = emitted["value"] | emitted
        return (
            "match"
            if all(str(emitted.get(k)) == str(expected.get(k)) for k in keys)
            else "mismatch"
        )
    if path == "declaration_fields.interface_port_counts":

        def signature(item):
            return str(item["normalized"]), item["unit"], item["count_basis"]

        wanted = {item["interface_type"]: signature(item) for item in expected or []}
        counts = {kind: signature(item["value"] | item) for kind, item in emitted.items()}
        return (
            "match" if counts == wanted else "not_observed" if wanted and not counts else "mismatch"
        )
    if path == "declaration_fields.technical_attributes":
        wanted = {
            item["attribute"]: (str(item["normalized"]), item["unit"]) for item in expected or []
        }
        got = {key: (value["value"]["normalized"], value["unit"]) for key, value in emitted.items()}
        return "match" if got == wanted else "not_observed" if wanted and not got else "mismatch"
    return "match" if emitted == expected else "mismatch"


def score_evidence(field, actual, object_anchor, columns):
    accepted = PROVENANCE.get(field["field_name"])
    locator = field["locator"]
    if not accepted or "column" not in locator:
        return "unsupported"
    if actual is None:
        return "not_observed"
    if anchor(locator) != object_anchor:
        return "mismatch"
    for found in actual["field_evidence"]:
        if found["field_name"] not in accepted or found["raw_span"] != field["quote"]:
            continue
        if object_anchor[0] == "sheet":
            if (
                found["column"] == locator["column"]
                and found["row_id"] == actual["row_id"]
                and found["sheet"] == actual["sheet_or_section"]
                and found["asset_id"] == actual["source_asset_id"]
                and found["column_index"] == columns.get(locator["column"])
            ):
                return "match"
        elif (
            found["raw_heading"] == locator["column"]
            and found["row_in_block"] == actual["row_in_block"]
            and found["evidence_id"] == actual["evidence_id"]
            and actual["physical_page"] == locator["physical_page"]
        ):
            return "match"
    return "mismatch"


def score(observed: Observation | None, gold: dict, columns: dict | None = None) -> dict:
    fields, pointers, unknowns, objects = (Counter() for _ in range(4))
    field_details: dict[str, Counter[str]] = defaultdict(Counter)
    unmatched = []
    refs = {}
    field_errors = []
    for side, actual_rows in (
        ("requirements", observed.requirements if observed else {}),
        ("declarations", observed.declarations if observed else {}),
    ):
        expected_anchors = set()
        for item in gold["expected_extraction"][side]:
            a = reference_anchor(item)
            refs[item["local_ref"]] = a
            actual = actual_rows.get(a)
            expected_anchors.add(a)
            objects["expected"] += 1
            if actual is None:
                objects["missing"] += 1
                unmatched.append(
                    {
                        "ref": item["local_ref"],
                        "reason": "object_not_observed" if a else "reference_anchor_unmapped",
                    }
                )
            else:
                objects["matched"] += 1
            for name, path in FIELDS[side].items():
                expected = value_at(item, name)
                if expected is not MISSING:
                    technical_only = (
                        side == "declarations"
                        and bool(item.get("technical_attributes"))
                        and not item.get("device_quantity")
                        and name in {"quantity_raw", "unit", "device_quantity"}
                    )
                    result = (
                        "not_in_scope" if technical_only else score_field(expected, actual, path)
                    )
                    fields[result] += 1
                    field_details[f"{side}.{name}"][result] += 1
                    if result != "match":
                        field_errors.append(
                            {
                                "side": side,
                                "ref": item["local_ref"],
                                "field": name,
                                "status": result,
                            }
                        )
            for field in item["field_evidence"]:
                pointers[score_evidence(field, actual, a, columns or {})] += 1
            for item_state in item.get("field_support", []):
                if item_state["state"] in UNKNOWN:
                    path = FIELDS[side].get(item_state["field"])
                    if not path:
                        unknowns["not_implemented"] += 1
                    elif actual is None:
                        unknowns["not_observed"] += 1
                    else:
                        got = value_at(actual, path)
                        unknowns["match" if got in (None, "", "unknown") else "mismatch"] += 1
        objects["unexpected"] += len(set(actual_rows) - expected_anchors)

    relations = gold["entity_resolution"]["candidates"]
    edge_counts = Counter()
    candidate_errors = []
    wanted, unresolved = {}, 0
    unsupported_relationships = sum(
        r["match_label"] == "candidate_same_requirement_slot" for r in relations
    )
    for record in relations:
        if record["match_label"] not in POSITIVE:
            continue
        group = "clear" if record["match_label"] == "candidate_same_slot" else "ambiguous"
        edge_counts[f"{group}_expected"] += 1
        pair = refs.get(record.get("left_ref")), refs.get(record.get("right_ref"))
        if any(a is None for a in pair):
            unresolved += 1
            candidate_errors.append(
                {"status": "missing", "edge": pair, "reason": ["reference_anchor_unmapped"]}
            )
        else:
            if pair in wanted:
                raise ValueError("Duplicate reference candidate edge at the same source anchors")
            wanted[pair] = record["match_label"]
    actual = observed.pairs if observed else set()
    expected_pairs = set(wanted)
    kept = expected_pairs & actual
    for pair, label in wanted.items():
        group = "clear" if label == "candidate_same_slot" else "ambiguous"
        if pair in kept:
            edge_counts[f"{group}_recalled"] += 1
            observed_label = observed.edges[pair]["match_label"]
            if observed_label == label:
                edge_counts[f"{group}_correct_label"] += 1
            else:
                edge_counts["label_errors"] += 1
                candidate_errors.append(
                    {
                        "status": "label_error",
                        "edge": pair,
                        "expected": label,
                        "actual": observed_label,
                    }
                )
    for pair in actual - expected_pairs:
        candidate_errors.append(
            {"status": "unexpected", "edge": pair, "actual": observed.edges[pair]["match_label"]}
        )
    missing_pair_reasons: Counter[str] = Counter()
    for left, right in expected_pairs - actual:
        if not observed:
            missing_pair_reasons["pipeline_not_observed"] += 1
            candidate_errors.append(
                {"status": "missing", "edge": (left, right), "reason": ["pipeline_not_observed"]}
            )
            continue
        req, decl = observed.requirements.get(left), observed.declarations.get(right)
        if req is None or decl is None:
            missing_pair_reasons["endpoint_not_observed"] += 1
            candidate_errors.append(
                {"status": "missing", "edge": (left, right), "reason": ["endpoint_not_observed"]}
            )
            continue
        blockers = assess(req, decl)["recall_blockers"]
        missing_pair_reasons.update(blockers)
        candidate_errors.append({"status": "missing", "edge": (left, right), "reason": blockers})
    pair_score = {
        "expected": len(wanted) + unresolved,
        "observed": len(actual),
        "matched": len(kept),
        "missing": len(expected_pairs - actual) + unresolved,
        "unexpected": len(actual - expected_pairs),
        "unresolved_reference": unresolved,
        "unsupported_relationships": unsupported_relationships,
    }
    ambiguity = Counter()
    for a in {
        pair[0]
        for pair, label in wanted.items()
        if label == "ambiguous_one_to_many" and sum(other[0] == pair[0] for other in wanted) > 1
    }:
        if not observed or a not in observed.requirements:
            ambiguity["not_observed"] += 1
        else:
            group = {pair for pair in wanted if pair[0] == a}
            ambiguity["match" if group <= actual else "mismatch"] += 1

    findings = observed.workflow["findings"] if observed else []
    review_state = gold["workflow_expectation"]["expected_stage"]
    status = "not_observed"
    if observed:
        safe = observed.workflow["field_verification_state"] == "not_verified" and all(
            f["disposition"] in {"review_required", "blocked"}
            and f["preconditions"]["entity_binding"] != "confirmed"
            for f in findings
        )
        status = ("match" if safe else "mismatch") if findings else "empty"
    stage = "not_comparable"
    if review_state in COMPARABLE_STAGES:
        targets = {reference_anchor(req) for req in gold["expected_extraction"]["requirements"]}
        by_id = (
            {item["candidate_id"]: a for a, item in observed.requirements.items()}
            if observed
            else {}
        )
        reviewed = {
            by_id.get(f["requirement"]["candidate_id"])
            for f in findings
            if f["disposition"] in {"review_required", "blocked"}
        }
        stage_ok = (
            bool(targets) and None not in targets and targets <= reviewed and status == "match"
        )
        if review_state != "review_required":
            eligible_edges = {
                (left, right)
                for left, right in kept
                if observed.edges[left, right]["binding_state"] == "unconfirmed"
                and (
                    review_state != "candidate_but_not_evaluable"
                    or observed.edges[left, right]["comparability"] == "blocked"
                )
            }
            stage_ok = stage_ok and targets <= {left for left, _ in eligible_edges}
        stage = "match" if stage_ok else "mismatch"
    confirmed = sum(
        e["binding_state"] == "confirmed" or e["system_confirmation"]
        for e in (observed.edges.values() if observed else ())
    )
    return {
        "objects": dict(objects),
        "fields": dict(fields),
        "fields_by_name": {name: dict(counts) for name, counts in field_details.items()},
        "field_evidence": dict(pointers),
        "unknowns": dict(unknowns),
        "candidate_pairs": pair_score,
        "candidate_metrics": dict(edge_counts)
        | {
            "binding_confirmed": confirmed,
            "unsupported_confirmations": confirmed
            + sum(f["preconditions"]["entity_binding"] == "confirmed" for f in findings),
        },
        "candidate_errors": sorted(candidate_errors, key=lambda error: repr(error["edge"])),
        "candidate_topology": dict(Counter(e["topology"] for e in observed.edges.values()))
        if observed
        else {},
        "field_errors": field_errors,
        "missing_pair_reasons_overlapping": dict(missing_pair_reasons),
        "ambiguity": dict(ambiguity),
        "workflow_gate": {
            "status": status,
            "expected_stage_match": stage,
            "expected_stage": review_state,
            "claims_exercised": observed.claims if observed else 0,
        },
        "workflow_outcomes": dict(Counter(f["disposition"] for f in findings)),
        "unmatched_reference_objects": unmatched,
    }


def conditional(gold: dict) -> dict:
    """Separate, intentionally narrow logic-only probe with authored assumed inputs."""
    layer = gold.get("conditional_evaluation", {})
    preconditions = {p["id"]: p["status"] for p in layer.get("preconditions", [])}
    exact = (
        "P1_FIELD_SUPPORT",
        "P2_ENTITY_BINDING",
        "P3_APPLICABILITY",
        "P4_UNIT_AND_BASIS",
        "P6_REQUIREMENT_STRENGTH",
    )
    expected = layer.get("logic_result", {})
    if expected.get("kind") != "requirement_evaluation":
        return {"status": "unsupported", "reason": "unsupported_conditional_result_kind"}
    if any(preconditions.get(key) != "supported_by_source" for key in exact):
        return {"status": "unsupported", "reason": "conditional_prerequisites_not_explicit"}
    if layer.get("branches") or len(layer.get("per_requirement", [])) > 1:
        return {"status": "unsupported", "reason": "conditional_branches_or_multiple_results"}
    reqs = gold["expected_extraction"]["requirements"]
    decls = gold["expected_extraction"]["declarations"]
    pairs = [r for r in gold["entity_resolution"]["candidates"] if r["match_label"] in POSITIVE]
    if len(reqs) != 1 or len(decls) != 1 or len(pairs) != 1:
        return {"status": "unsupported", "reason": "conditional_pair_not_unique"}
    left, right = reqs[0], decls[0]
    qty = right.get("device_quantity")
    if left["attribute"] != "device_quantity" or not qty or not qty.get("normalized"):
        return {"status": "unsupported", "reason": "conditional_non_scalar_device_quantity"}
    if not left.get("source_revision") or left["source_revision"] != right.get("source_revision"):
        return {"status": "unsupported", "reason": "conditional_revision_not_explicit"}

    def term(obj, **data):
        return {runtime: obj.get(name) for name, runtime in COMMON.items()} | {
            "property": "device_quantity",
            **data,
        }

    requirement = term(
        left,
        value_role="requirement",
        expr=left["expression"]["normalized"],
        unit=left["expression"]["unit"],
        strength=left["strength"],
        quantity_role=left["quantity_role"],
        inclusion_status=left["inclusion_status"],
    )
    declaration = term(
        right, value_role="declaration", expr="=" + qty["normalized"], unit=qty["unit"]
    )
    got = evaluate(
        requirement,
        declaration,
        {
            "source_assumption": "fixture_field_supported",
            "document_scope": "synthetic_only",
            "entity_binding": "confirmed",
            "revision_relation": "same_version",
        },
    )
    keys = ("requirement_evaluation", "finding_disposition")
    wanted = {k: expected[k] for k in keys}
    actual = {k: got[k] for k in keys}
    return {
        "status": "match" if actual == wanted else "mismatch",
        "expected": wanted,
        "actual": actual,
        "actual_reason": got["reason_code"],
    }


def evaluate_case(case: dict, gold: dict, database: Path) -> dict:
    inputs = None
    try:
        inputs = materialize(case)
    except UnsupportedInput as error:
        observed = None
        status = {"status": "unsupported", "stage": "source_projection", "reason": str(error)}
    else:
        observed, status = observe(inputs, database)
    results = score(observed, gold, inputs.columns if inputs else None)
    return results | {
        "case_id": case["case_id"],
        "pipeline": status,
        "conditional": conditional(gold),
        "diagnostics": {k: v for k, v in observed.diagnostic.items() if k != "evidence_index"}
        if observed
        else None,
    }
