"""Run TASK-002A development measurements. Run as evaluation.run_task002a."""

import argparse
import hashlib
import json
import tempfile
from collections import Counter
from pathlib import Path

from ira_native.domain.rules import evaluate

from .task002a_inputs import UnsupportedInput, materialize
from .task002a_scoring import COMPARABLE_STAGES, POSITIVE, evaluate_case, observe


def read_jsonl(file: Path) -> list[dict]:
    return [json.loads(line) for line in file.read_text(encoding="utf-8-sig").splitlines() if line]


def indexed(rows: list[dict], key: str) -> dict:
    result = {row[key]: row for row in rows}
    if len(result) != len(rows):
        raise ValueError(f"Duplicate {key}")
    return result


def sum_counts(records, key) -> dict:
    result: Counter = Counter()
    for row in records:
        result.update(row[key])
    return dict(result)


def tally(counts: dict) -> dict:
    total = sum(counts.values())
    return {
        "numerator": counts.get("match", 0),
        "denominator": total,
        "supported_denominator": total
        - counts.get("unsupported", 0)
        - counts.get("not_implemented", 0),
        "status_counts": counts,
    }


def field_denominators(counts: dict) -> dict:
    full = sum(counts.values()) - counts.get("not_in_scope", 0)
    implemented = full - counts.get("not_implemented", 0)
    observed = implemented - counts.get("not_observed", 0) - counts.get("unsupported", 0)
    return {
        name: {"numerator": counts.get("match", 0), "denominator": denominator}
        for name, denominator in (
            ("full_contract", full),
            ("implemented", implemented),
            ("observed_and_implemented", observed),
        )
    }


def candidate_summary(cases: list[dict]) -> dict:
    counts = sum_counts(cases, "candidate_metrics")
    return {
        name: {
            "numerator": counts.get(f"{name}_recalled", 0),
            "denominator": counts.get(f"{name}_expected", 0),
            "correct_label": counts.get(f"{name}_correct_label", 0),
        }
        for name in ("clear", "ambiguous")
    } | {
        "correct_edges": counts.get("clear_recalled", 0) + counts.get("ambiguous_recalled", 0),
        "missing_edges": sum(row["candidate_pairs"]["missing"] for row in cases),
        "false_positive": sum(row["candidate_pairs"]["unexpected"] for row in cases),
        "label_errors": counts.get("label_errors", 0),
        "binding_confirmed": counts.get("binding_confirmed", 0),
        "unsupported_confirmations": counts.get("unsupported_confirmations", 0),
        "endpoint_not_observed": sum(
            row["missing_pair_reasons_overlapping"].get("endpoint_not_observed", 0) for row in cases
        ),
    }


def stage_summary(statuses: list[str]) -> dict:
    counts = Counter(statuses)
    return {
        "numerator": counts["match"],
        "denominator": len(statuses) - counts["not_comparable"],
        "not_comparable": counts["not_comparable"],
        "status_counts": dict(counts),
    }


def baseline_r2(legacy: dict, references: dict) -> dict:
    """Apply R2 denominators to the recorded zero-edge 413a051 execution."""
    cases = legacy["cases"]
    if {row["case_id"] for row in cases} != references.keys() or any(
        row["candidate_pairs"]["observed"] for row in cases
    ):
        raise ValueError("Expected a recorded zero-edge baseline with matching references")
    labels = Counter(
        edge["match_label"]
        for gold in references.values()
        for edge in gold["entity_resolution"]["candidates"]
    )
    statuses = []
    for row in cases:
        gold = references[row["case_id"]]
        stage = gold["workflow_expectation"]["expected_stage"]
        reqs = gold["expected_extraction"]["requirements"]
        missing = {item["ref"] for item in row["unmatched_reference_objects"]}
        all_reviewed = (
            bool(reqs)
            and not any(req["local_ref"] in missing for req in reqs)
            and row["workflow_gate"]["status"] == "match"
            and sum(row["workflow_outcomes"].values())
            == row["objects"]["matched"]
            + row["objects"]["unexpected"]
            - len(gold["expected_extraction"]["declarations"])
            and all(key in ("review_required", "blocked") for key in row["workflow_outcomes"])
        )
        statuses.append(
            "not_comparable"
            if stage not in COMPARABLE_STAGES
            else "match"
            if stage == "review_required" and all_reviewed
            else "mismatch"
        )
    return {
        "measurement_status": "development_regression_not_acceptance",
        "gold_review_state": "unreviewed",
        "source": "recorded 413a051 run; stage rescored using its zero edges and source-anchored objects",
        "candidates": {
            "clear": {"numerator": 0, "denominator": labels["candidate_same_slot"]},
            "ambiguous": {
                "numerator": 0,
                "denominator": sum(labels[x] for x in POSITIVE if x != "candidate_same_slot"),
            },
            "binding_confirmed": 0,
            "endpoint_not_observed": sum(
                r["missing_pair_reasons_overlapping"].get("endpoint_not_observed", 0) for r in cases
            ),
        },
        "field_denominators": field_denominators(legacy["fields"]["status_counts"]),
        "workflow": {
            "safe_review_state": legacy["workflow_gate"],
            "expected_stage_match": stage_summary(statuses),
        },
        "original_measurement": legacy,
    }


def logic_seeds(path: Path) -> dict:
    results = []
    for case in read_jsonl(path):
        actual = evaluate(case["requirement"], case["declaration"], case["preconditions"])
        expected = case["expected"]
        assert case["test_scope"] == "logic_only"
        results.append(
            {
                "case_id": case["case_id"],
                "match": all(actual[key] == value for key, value in expected.items()),
            }
        )
    return {
        "numerator": sum(record["match"] for record in results),
        "denominator": len(results),
        "cases": results,
    }


def check_layout(base, changed) -> dict:
    def raw(observation, side):
        return {a: item["raw"] for a, item in side(observation).items()}

    individual = {
        "requirement_raw_by_source_anchor": raw(base, lambda x: x.requirements)
        == raw(changed, lambda x: x.requirements),
        "declaration_raw_by_source_anchor": raw(base, lambda x: x.declarations)
        == raw(changed, lambda x: x.declarations),
        "candidate_pairs_by_source_anchor": base.pairs == changed.pairs,
    }
    return {
        "status": "partial" if all(individual.values()) else "mismatch",
        "checks": individual,
        "pair_counts": {"base": len(base.pairs), "derived": len(changed.pairs)},
        "unscored": [
            "numeric_pdf_column_positions",
            "conditional_evaluation",
            "gold_ambiguity_of_base_not_proven_by_invariance",
        ],
    }


def run_metamorphic(cases, derived, specs, folder):
    results = []
    for row in derived:
        definition = specs[row["metamorphic_id"]]
        if (
            definition["derived_case_id"] != row["case_id"]
            or definition["derived_from"] != row["derived_from"]
        ):
            raise ValueError("Derived case identity disagrees with metamorphic definition")
        result = {"meta_id": row["metamorphic_id"], "operation": definition["operation"]}
        try:
            inputs = materialize(row)
        except UnsupportedInput as error:
            result["pipeline"] = {
                "status": "unsupported",
                "stage": "source_projection",
                "reason": str(error),
            }
            changed = None
        else:
            changed, result["pipeline"] = observe(inputs, folder / f"derived-{len(results)}.sqlite")
        if changed:
            result["candidate_count"] = len(changed.requirements)
            result["workflow_outcomes"] = dict(
                Counter(f["disposition"] for f in changed.workflow["findings"])
            )
        if changed and definition["change_kind"] == "modify_layout_or_order":
            base, status = observe(
                materialize(cases[row["derived_from"]]),
                folder / f"layout-base-{len(results)}.sqlite",
            )
            result["metamorphic_checks"] = check_layout(base, changed) if base else status
        else:
            result["metamorphic_checks"] = {
                "status": "unsupported",
                "reason": "no_derived_layered_gold_or_layout_projection",
            }
        results.append(result)
    return results


def run(dataset: Path, derived_file: Path, legacy: Path) -> dict:
    cases_path = dataset / "cases.jsonl"
    gold_path = dataset / "gold.jsonl"
    meta_path = dataset / "metamorphic-cases.jsonl"
    cases = indexed(read_jsonl(cases_path), "case_id")
    gold = indexed(read_jsonl(gold_path), "case_id")
    meta = indexed(read_jsonl(meta_path), "meta_id")
    derived = read_jsonl(derived_file)
    if cases.keys() != gold.keys() or indexed(derived, "metamorphic_id").keys() != meta.keys():
        raise ValueError("Dataset inputs and references have different IDs")
    if any(row.get("data_kind") != "synthetic_public" for row in [*cases.values(), *derived]):
        raise ValueError("This harness accepts only synthetic public cases")
    with tempfile.TemporaryDirectory(prefix="ira-task002a-") as path:
        folder = Path(path)
        results = [
            evaluate_case(case, gold[case_id], folder / f"base-{index}.sqlite")
            for index, (case_id, case) in enumerate(cases.items())
        ]
        variants = run_metamorphic(cases, derived, meta, folder)
    stages = Counter(
        f"{row['pipeline'].get('stage', 'complete')}:{row['pipeline'].get('reason', 'complete')}"
        for row in results
    )
    field_details: dict[str, Counter] = {}
    for row in results:
        for key, counts in row["fields_by_name"].items():
            field_details.setdefault(key, Counter()).update(counts)
    return {
        "schema_version": "task002a-eval/2",
        "scope": "synthetic_development_regression_not_held_out",
        "measurement_status": "development_regression_not_acceptance",
        "gold_review_state": "unreviewed",
        "provenance_sha256": {
            name: hashlib.sha256(path.read_bytes()).hexdigest()
            for name, path in (
                ("cases", cases_path),
                ("gold", gold_path),
                ("metamorphic", meta_path),
                ("derived", derived_file),
                ("logic_seeds", legacy),
            )
        },
        "base_cases": {
            "total": len(results),
            "adapted": stages["complete:complete"],
            "pipeline_stages": dict(stages),
        },
        "logic_only_frozen_seeds": logic_seeds(legacy),
        "objects": sum_counts(results, "objects"),
        **{
            name: tally(sum_counts(results, name))
            for name in ("fields", "field_evidence", "unknowns", "ambiguity")
        },
        "candidate_pairs": sum_counts(results, "candidate_pairs"),
        "candidates": candidate_summary(results),
        "candidate_topology": sum_counts(results, "candidate_topology"),
        "field_denominators": field_denominators(sum_counts(results, "fields")),
        "missing_pair_reasons_overlapping": sum_counts(results, "missing_pair_reasons_overlapping"),
        "fields_by_name": {key: tally(dict(counts)) for key, counts in field_details.items()},
        "workflow_gate": tally(dict(Counter(row["workflow_gate"]["status"] for row in results))),
        "workflow": {
            "safe_review_state": tally(
                dict(Counter(row["workflow_gate"]["status"] for row in results))
            ),
            "expected_stage_match": stage_summary(
                [row["workflow_gate"]["expected_stage_match"] for row in results]
            ),
        },
        "workflow_self_report_inputs": sum(
            row["workflow_gate"]["claims_exercised"] for row in results
        ),
        "workflow_outcomes": sum_counts(results, "workflow_outcomes"),
        "conditional": tally(dict(Counter(row["conditional"]["status"] for row in results))),
        "conditional_unsupported_reasons": dict(
            Counter(
                row["conditional"]["reason"]
                for row in results
                if row["conditional"]["status"] == "unsupported"
            )
        ),
        "metamorphic": {
            "total": len(variants),
            "pipeline_status": dict(Counter(row["pipeline"]["status"] for row in variants)),
            "checks_status": dict(Counter(row["metamorphic_checks"]["status"] for row in variants)),
            "cases": variants,
        },
        "cases": results,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dataset", type=Path, required=True)
    parser.add_argument("--derived", type=Path)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--before", type=Path, help="Rescore the retained 413a051 execution")
    args = parser.parse_args()
    legacy = Path(__file__).resolve().parents[1] / "fixtures/synthetic/cases.jsonl"
    result = (
        baseline_r2(
            json.loads(args.before.read_text(encoding="utf-8")),
            indexed(read_jsonl(args.dataset / "gold.jsonl"), "case_id"),
        )
        if args.before
        else run(
            args.dataset, args.derived or args.dataset / "metamorphic-derived-cases.jsonl", legacy
        )
    )
    with args.out.open("x", encoding="utf-8") as output:
        json.dump(result, output, indent=2, ensure_ascii=False)
        output.write("\n")
    print(
        json.dumps(
            {
                k: v
                for k, v in result.items()
                if k
                not in (
                    "cases",
                    "metamorphic",
                    "logic_only_frozen_seeds",
                    "fields_by_name",
                    "original_measurement",
                )
            },
            indent=2,
            ensure_ascii=False,
        )
    )
    if result["workflow"]["safe_review_state"]["status_counts"].get("mismatch") or result[
        "candidates"
    ].get("unsupported_confirmations"):
        raise SystemExit("Workflow trust gate regression")


if __name__ == "__main__":
    main()
