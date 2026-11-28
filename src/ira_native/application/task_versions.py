"""Resolve only explicit, task-scoped supersession reviewed by a local operator."""

import json
import re
from collections import defaultdict
from datetime import date

from ira_native.application.service import _identity
from ira_native.application.task_observation import _revision_conflicts, _revision_keys

_IDENTITY = (
    "equipment_class",
    "model",
    "config_variant",
    "scope",
    "site_group",
    "requirement_slot",
    "property",
    "part_number",
    "count_basis",
    "condition",
)
_CLAIM = re.compile(
    r"(?:修订\s*|revision\s+)?(?P<later>[\w.-]{1,48})\s*"
    r"(?:supersedes\s+|replaces\s+|替代|取代)\s*"
    r"(?:修订\s*|revision\s+)?(?P<earlier>[\w.-]{0,47}\w)[。.]?",
    re.I,
)


def replacement_claims(item) -> list[list[str]]:
    if item.kind != "text" or not isinstance(item.raw_content, str):
        return []
    matches = [
        _CLAIM.fullmatch(line.strip()) for line in item.raw_content.splitlines() if line.strip()
    ]
    return [[match["later"], match["earlier"]] for match in matches] if all(matches) else []


def complete_amendment_record(item, rows: list[dict]) -> bool:
    if (
        item.kind != "text"
        or not isinstance(item.raw_content, str)
        or item.annotations
        or len(rows) != 1
    ):
        return False
    row = rows[0]
    fields = {source["raw_heading"]: source["raw_span"] for source in row["field_evidence"]}
    fields.update({key: value for key, value in row["raw"].items() if value == ""})
    for line in item.raw_content.splitlines():
        if not line.strip():
            continue
        pair = re.fullmatch(r"([^:：]+)[:：](.*)", line)
        if pair is None or fields.get(pair[1].strip()) != pair[2].strip():
            return False
    return True


def _same_subject(earlier: dict, later: dict) -> bool:
    return all(
        earlier.get(key) for key in ("equipment_class", "model", "scope", "property")
    ) and all(earlier.get(key) == later.get(key) for key in _IDENTITY)


def resolve_versions(draft: dict, runs: dict, versions: dict, history: list[dict]) -> dict:
    requirements = draft["requirements"]
    scope = draft["task_scope"]
    evidence = draft["task_evidence"]
    evidence_by_id = {item["evidence_id"]: item for item in evidence}
    scoped_revisions = {
        req["candidate_id"]: [
            revision
            for revision in versions[req["parse_run_id"]]
            if req["source_revision"] and revision["revision_label"] == req["source_revision"]
        ]
        for req in requirements
    }
    as_of = date.fromisoformat(scope["as_of"]) if scope["as_of"] else None
    candidates = defaultdict(list)
    for item in requirements:
        for key in _revision_keys(item):
            candidates[key].append(item)
    conflicts, relations = {}, []
    reviews = {(row["left_ref"], row["right_ref"]): row for row in history}
    authority = defaultdict(set)
    for later in requirements:
        later_id = later["candidate_id"]
        overlapping = {
            other["candidate_id"]: other
            for key in _revision_keys(later)
            for other in candidates[key]
            if other["parse_run_id"] != later["parse_run_id"] and _revision_conflicts(later, other)
        }
        if overlapping:
            conflicts[later_id] = sorted(overlapping)
        for earlier_id, earlier in overlapping.items():
            prior, current = scoped_revisions[earlier_id], scoped_revisions[later_id]
            if len(prior) != 1 or len(current) != 1:
                continue
            old, new = prior[0], current[0]
            stated = new.get("scope")
            if (
                not isinstance(stated, dict)
                or stated.get("supersedes_revision_id") != old["revision_id"]
                or new["document_id"] != old["document_id"]
            ):
                continue
            proofs = sorted(
                identity
                for identity in set(new["relation_evidence"])
                if identity in evidence_by_id
                and (
                    evidence_by_id[identity]["parse_run_id"] == later["parse_run_id"]
                    or scope["roles"][evidence_by_id[identity]["parse_run_id"]] == "amendment"
                )
                and runs[evidence_by_id[identity]["parse_run_id"]].binding_state != "mismatch"
                and evidence_by_id[identity]["source_state"] not in ("denied", "mismatch")
                and [new["revision_label"], old["revision_label"]]
                in evidence_by_id[identity].get("replacement_claims", [])
            )
            blockers = []
            if not _same_subject(earlier, later):
                blockers.append("REVISION_SCOPE_UNPROVEN")
            if (
                not as_of
                or not new["effective_from"]
                or date.fromisoformat(new["effective_from"]) > as_of
                or old["effective_from"]
                and new["effective_from"] <= old["effective_from"]
            ):
                blockers.append("REVISION_EFFECTIVITY_UNPROVEN")
            if not proofs:
                blockers.append("REVISION_EVIDENCE_UNAVAILABLE")
            if any(
                runs[req["parse_run_id"]].binding_state == "mismatch" for req in (earlier, later)
            ):
                blockers.append("SOURCE_MISMATCH")
            snapshot_id = _identity(
                "revision_relation/1",
                json.dumps([scope, earlier_id, later_id, old, new, proofs], sort_keys=True),
            )
            review = reviews.get((later_id, earlier_id), {})
            current_review = (
                review.get("snapshot_id") == snapshot_id and review.get("evidence_id") in proofs
            )
            confirmed = not blockers and current_review and review.get("decision") == "confirmed"
            if confirmed:
                authority[later_id].add(earlier_id)
            relations.append(
                {
                    "later_requirement_id": later_id,
                    "earlier_requirement_id": earlier_id,
                    "evidence_ids": proofs,
                    "snapshot_id": snapshot_id,
                    "review_token": _identity(
                        "revision_form", snapshot_id, review.get("review_id", "")
                    ),
                    "blockers": blockers,
                    "state": "declared"
                    if blockers
                    else "confirmed"
                    if confirmed
                    else "unresolved"
                    if current_review
                    else "supported",
                    "review_id": review["review_id"] if current_review else None,
                }
            )
    superseded = {old for item in authority.values() for old in item}
    # Branching supersession never suppresses the older facts.
    forks = {old for old in superseded if sum(old in item for item in authority.values()) > 1}
    affected_forks = forks | {new for new, old in authority.items() if old & forks}
    superseded -= affected_forks

    def ancestors(identity):
        found = set()
        pending = list(authority[identity])
        while pending:
            previous = pending.pop()
            if previous not in found:
                found.add(previous)
                pending.extend(authority[previous])
        return found

    amendment_ids = {identity for identity, role in scope["roles"].items() if role == "amendment"}
    # A partly extracted notice may still change other requirements. An
    # entirely located row can be contained to its own stated subject even
    # before its precedence is confirmed.
    accounted_blocks = {
        relation_evidence
        for relation in relations
        for relation_evidence in relation["evidence_ids"]
    } | {
        item["evidence_id"]
        for item in requirements
        if evidence_by_id[item["evidence_id"]].get("complete_amendment_record")
        and all(item.get(key) for key in ("equipment_class", "model", "scope", "property"))
    }
    amendment_unknown = {
        item["parse_run_id"]
        for item in evidence
        if item["parse_run_id"] in amendment_ids and item["evidence_id"] not in accounted_blocks
    } | (amendment_ids - {item["parse_run_id"] for item in evidence})
    states = {}
    for item in requirements:
        identity = item["candidate_id"]
        prior = ancestors(identity)
        if identity in affected_forks or amendment_unknown:
            status = "unresolved"
        elif identity in superseded:
            status = "superseded"
        elif set(conflicts.get(identity, [])) - prior:
            status = "unresolved"
        elif prior:
            status = "effective_replacement"
        elif item["parse_run_id"] in amendment_ids:
            status = "unresolved"
        else:
            status = "no_competing_requirement"
        states[identity] = status
    return {"relations": relations, "conflicts": conflicts, "states": states}
