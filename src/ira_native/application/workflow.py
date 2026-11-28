"""Compose persisted verification inputs, deterministic decisions and human reviews."""

import asyncio
import csv
import io
import json
from datetime import UTC, date, datetime
from pathlib import Path
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, ValidationError

from ira_native.adapters.spreadsheet import read_sheet
from ira_native.adapters.workflow_store import WorkflowStore
from ira_native.application.candidates import (
    attach_annotation_context,
    extract_requirements,
    extract_requirements_from_evidence,
    normalize,
)
from ira_native.application.human_review import GATE_NAMES, assess_trust, review_fields
from ira_native.application.results import (
    fact_for,
    machine_binding,
    paired_declaration,
    persisted_finding,
    source_association,
)
from ira_native.application.service import EvidenceService, _identity, _sha
from ira_native.domain.candidate_matching import generate_candidates
from ira_native.domain.models import ANNOTATION_RISK, DocumentRevision
from ira_native.domain.rules import (
    PART_RULESET_VERSION,
    RULESET_VERSION,
    calculate_conditional,
    compare_requirements,
    evaluate,
)
from ira_native.domain.verification import TaskObservation
from ira_native.errors import IraError

_REVIEW_DECISIONS = {"accepted_with_scope", "rejected_with_scope", "needs_more_evidence"}


class Confirmation(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    requirement_id: str
    declaration_id: str
    fields_checked: bool = False
    same_version: bool = False


def _time():
    return datetime.now(UTC).isoformat()


def _csv_value(value):
    if value is None:
        return ""
    text = str(value)
    if text and (ord(text[0]) < 32 or text.lstrip().startswith(("=", "+", "-", "@"))):
        return "'" + text
    return text


def _requirement_signature(requirements: list[dict]) -> str:
    fields = (
        "candidate_id",
        "raw",
        "expr",
        "unit",
        "quantity_role",
        "part_number",
        "applicability",
        "presence",
    )
    if any(item.get("attribute_normalization") for item in requirements):
        fields += ("property", "attribute_normalization")
    return _identity(
        json.dumps(
            [
                [
                    item.get(field, "unknown" if field in ("applicability", "presence") else None)
                    for field in fields
                ]
                for item in requirements
            ],
            sort_keys=True,
        )
    )


def _revision_blocker(state):
    return {
        "superseded": "SUPERSEDED_REQUIREMENT",
        "unresolved": "REVISION_RELATION_UNRESOLVED",
    }.get(state)


def _conditional_preview(requirement, declaration, edge, revision_relation, association):
    reason = None
    if association.status == "conflict":
        return {"status": "undetermined", "reason_code": association.basis[0]}
    if (
        requirement.get("applicability") == "not_applicable"
        and association.status == "machine_linked"
    ):
        return {"status": "not_applicable", "reason_code": "EXPLICIT_SOURCE_NON_APPLICABILITY"}
    if (
        any(
            flag in requirement["risk"]
            for flag in (ANNOTATION_RISK, "continuation_unverified", "table_recovery_unverified")
        )
        and association.status != "machine_linked"
    ):
        reason = "CONDITIONAL_SOURCE_RELATION_UNRESOLVED"
    elif edge is None:
        reason = "CONDITIONAL_PAIR_NOT_SELECTED"
    elif association.status != "machine_linked":
        reason = association.basis[0]
    elif edge["applicability_blockers"]:
        reason = edge["applicability_blockers"][0].upper()
    elif blockers := [
        blocker for blocker in edge["comparison_blockers"] if blocker != "unit_requires_comparison"
    ]:
        reason = blockers[0].upper()
    elif "model_conflict" in edge["confirmation_basis"]:
        reason = "MODEL_BINDING_CONFLICT"
    elif revision_relation not in ("same_version", "explicit_supersedes"):
        reason = "REVISION_UNKNOWN"
    elif any(
        item["field_values"]["condition"]["state"] == "unknown"
        for item in (requirement, declaration)
    ):
        reason = "CONDITION_UNKNOWN"
    elif requirement["strength"] != "mandatory":
        reason = "REQUIREMENT_FORCE_UNRESOLVED_OR_ADVISORY"
    elif requirement["property"] == "device_quantity" and requirement["quantity_role"] not in (
        "exact",
        "minimum",
    ):
        reason = "QUANTITY_ROLE_UNKNOWN"
    elif requirement["inclusion_status"] in ("excluded", "reference_out_of_scope"):
        reason = "REQUIREMENT_OUT_OF_SCOPE"
    if reason:
        return {"status": "undetermined", "reason_code": reason}
    return calculate_conditional(requirement, declaration) | {
        "assumptions": [
            "selected_pair",
            "asserted_same_version",
            "applicable_requirement",
            "extracted_values_as_stated",
        ],
    }


class VerificationWorkflow:
    def __init__(self, evidence: EvidenceService):
        self.evidence = evidence
        self.store = WorkflowStore(evidence.repository)
        self._active_agent_tasks: set[str] = set()
        self._background_agents: set[asyncio.Task] = set()

    def ingest_pair_bytes(self, pdf: bytes, parsed: bytes, *, filename: str = ""):
        result = self.evidence.ingest_uploaded_pair(pdf, parsed)
        if filename:
            self.store.register_specification_name(result.parse_run.parse_run_id, filename)
        return result

    def _specification_name(self, run):
        source = self.evidence._get_asset(run.source_asset_id)
        return self.store.specification_name(run.parse_run_id) or (
            Path(source.locator_internal).name
            if source.locator_internal != "registry"
            else "已登记规格"
        )

    @staticmethod
    def _sheet_name(sheet):
        name = sheet.get("filename") or (
            Path(sheet["locator_internal"]).name if sheet["locator_internal"] else "已登记清单"
        )
        return f"{name} · {sheet['sheet']}" if sheet.get("sheet") else name

    def specification_choices(self, *, by_asset: bool = False) -> list[tuple[str, str]]:
        specifications = {}
        for identity in self.store.specification_ids():
            try:
                run = self.evidence.get_run(identity)
                key = run.source_asset_id if by_asset else identity
                if key not in specifications:
                    specifications[key] = self._specification_name(run)
            except IraError as error:
                if error.code not in ("permission_denied", "source_missing"):
                    raise
        return list(specifications.items())

    def specification_label(self, parse_run_id: str) -> str:
        return self._specification_name(self.evidence.get_run(parse_run_id))

    def input_choices(self) -> dict:
        spreadsheets = []
        for row in self.store.recent_references("sheet", limit=-1):
            try:
                sheet = self._sheet(row["import_id"])
                spreadsheets.append((row["import_id"], self._sheet_name(sheet)))
            except IraError as error:
                if error.code not in ("permission_denied", "source_missing"):
                    raise
        return {"specifications": self.specification_choices(), "spreadsheets": spreadsheets}

    def draft_label(self, draft: dict) -> str:
        if draft.get("task_name"):
            return draft["task_name"]
        run = self.evidence.get_run(draft["parse_run_id"])
        sheet = self._sheet(draft.get("declaration_import_id") or draft["import_id"])
        return f"{self._specification_name(run)} · {self._sheet_name(sheet)}"

    def get_draft_label(self, draft_id: str) -> str:
        draft = self.store.get("draft", draft_id)
        if draft.get("task_scope"):
            self._task_authority(draft["task_scope"])
            return self.draft_label(draft)
        self.evidence.get_run(draft["parse_run_id"])
        self._sheet(draft.get("declaration_import_id") or draft["import_id"])
        return self.draft_label(draft)

    def ingest_spreadsheet(
        self,
        data: bytes,
        filename: str,
        mapping: dict[str, str],
        sheet: str | None = None,
        encoding: str = "utf-8-sig",
    ) -> dict:
        if not self.evidence.settings.enable_uploads:
            raise IraError("permission_denied")
        return self._register_sheet(data, filename, mapping, sheet, encoding)

    def ingest_spreadsheet_path(self, path, mapping, sheet=None, encoding="utf-8-sig"):
        resolved, data = self.evidence.files.read(path, self.evidence.settings.max_json_bytes)
        return self._register_sheet(data, resolved.name, mapping, sheet, encoding, str(resolved))

    def _register_sheet(self, data, name, mapping, sheet, encoding, locator=None):
        if len(data) > self.evidence.settings.max_json_bytes:
            raise IraError("resource_limit")
        kind, rows = read_sheet(data, name, mapping, sheet, encoding, 5000)
        sha = _sha(data)
        asset_id = _identity("source_asset", kind, sha)
        import_id = _identity(
            "spreadsheet_import",
            asset_id,
            json.dumps([mapping, sheet, encoding, locator], sort_keys=True),
        )
        normalized = []
        for row in rows:
            unresolved = frozenset(
                field["field_name"] for field in row["field_evidence"] if field["cell_type"]
            )
            normalized.append(normalize(row["raw"], requirement=False, unresolved_cells=unresolved))
        if any(row.get("attribute_normalization") for row in normalized):
            import_id = _identity(
                "spreadsheet_attribute_projection/1",
                import_id,
                json.dumps(
                    [row.get("attribute_normalization") for row in normalized], sort_keys=True
                ),
            )
        declarations = []
        for row, item in zip(rows, normalized, strict=True):
            item_id = _identity(import_id, str(row["row_id"]))
            declarations.append(
                item
                | row
                | {
                    "item_id": item_id,
                    "source_asset_id": asset_id,
                    "source": item_id,
                    "field_evidence": [
                        field
                        | {
                            "evidence_id": item_id,
                            "asset_id": asset_id,
                            "row_id": row["row_id"],
                            "sheet": row["sheet_or_section"],
                        }
                        for field in row["field_evidence"]
                    ],
                }
            )
        metadata = {
            "asset_id": asset_id,
            "sha256": sha,
            "source_kind": kind,
            "media_type": (
                "text/csv"
                if kind == "csv"
                else "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
            ),
            "access_policy": {"local_read": True, "external_model": False},
        }
        record = metadata | {
            "import_id": import_id,
            "filename": name,
            "locator_internal": locator,
            "declarations": declarations,
            "column_mapping": (
                {f["field_name"]: f["column"] for f in rows[0]["field_evidence"]}
                if rows
                else mapping
            ),
            "encoding": encoding,
            "sheet": sheet,
        }
        self.store.register_sheet(metadata, record, data)
        return {
            k: v
            for k, v in record.items()
            if k not in ("locator_internal", "declarations", "filename")
        } | {"row_count": len(declarations)}

    def _sheet(self, import_id):
        record = self.store.get("sheet", import_id)
        if record["locator_internal"]:
            self.evidence.files.authorize(Path(record["locator_internal"]))
        return record

    def _task_authority(self, scope: dict):
        from ira_native.application.task_observation import authorize_task_inputs

        return authorize_task_inputs(
            self, sources=scope["sources"], declarations=scope["declarations"]
        )

    def prepare_task(
        self,
        *,
        sources: dict[str, tuple[int, ...]],
        declarations: dict[str, tuple[int, ...]],
        roles: dict[str, str] | None = None,
        task_name: str = "",
        as_of: str | None = None,
    ) -> dict:
        from ira_native.application.task_observation import read_task_inputs
        from ira_native.application.task_versions import (
            complete_amendment_record,
            replacement_claims,
        )

        accepted_roles = {"specification", "supplement", "amendment"}
        roles = {} if roles is None else roles
        if (
            not declarations
            or len(task_name) > 100
            or not roles.keys() <= sources.keys()
            or not set(roles.values()) <= accepted_roles
        ):
            raise IraError("invalid_input")
        scope_roles = {key: roles.get(key, "specification") for key in sorted(sources)}
        if "specification" not in scope_roles.values():
            raise IraError("invalid_input")
        if as_of:
            try:
                as_of = date.fromisoformat(as_of).isoformat()
            except (TypeError, ValueError):
                raise IraError("invalid_input") from None
        else:
            as_of = None
        runs, sheets, evidence, requirements, rows = read_task_inputs(
            self, dict(sorted(sources.items())), dict(sorted(declarations.items()))
        )
        scope = {
            "schema": "ira.verification-task-scope/v1",
            "sources": {key: sorted(pages) for key, pages in sorted(sources.items())},
            "declarations": {key: sorted(rows) for key, rows in sorted(declarations.items())},
            "roles": scope_roles,
            "as_of": as_of,
        }
        parse_run_id = next(key for key, role in scope_roles.items() if role == "specification")
        import_id = min(sheets)
        by_evidence = {}
        for item in requirements:
            by_evidence.setdefault(item["evidence_id"], []).append(item)
        draft_id = _identity(
            "multi_draft/1",
            json.dumps(scope, sort_keys=True),
            task_name.strip(),
            _requirement_signature(requirements),
        )
        record = {
            "draft_id": draft_id,
            "task_name": task_name.strip() or "多资料核验",
            "task_scope": scope,
            "task_evidence": [
                {
                    "evidence_id": item.evidence_id,
                    "parse_run_id": item.parse_run_id,
                    "physical_page": item.physical_page,
                    "source_state": item.source_state.value,
                    "replacement_claims": replacement_claims(item),
                    "complete_amendment_record": complete_amendment_record(
                        item, by_evidence.get(item.evidence_id, [])
                    ),
                }
                for item in evidence
            ],
            "parse_run_id": parse_run_id,
            "source_asset_id": runs[parse_run_id].source_asset_id,
            "declaration_import_id": import_id,
            "declaration_asset_id": sheets[import_id]["asset_id"],
            "requirements": requirements,
            "declarations": rows,
            "physical_pages": None,
        }
        self.store.put(
            "draft", record, draft_id=draft_id, parse_run_id=parse_run_id, import_id=import_id
        )
        return self._draft(draft_id)

    def _scoped_draft(self, draft: dict) -> dict:
        from ira_native.application.task_versions import resolve_versions

        scope = draft["task_scope"]
        runs, sheets = self._task_authority(scope)
        evidence_ids = {item["evidence_id"] for item in draft["task_evidence"]}
        versions = {
            identity: [
                row.model_dump(mode="json")
                | {
                    "relation_evidence": [
                        ref for ref in row.relation_evidence if ref in evidence_ids
                    ]
                }
                for row in self.evidence.get_asset(run.source_asset_id).revisions
            ]
            for identity, run in runs.items()
        }
        history = self.store.version_history(draft["draft_id"])
        version = resolve_versions(draft, runs, versions, history)
        edges = generate_candidates(draft["requirements"], draft["declarations"])
        requirements = {row["candidate_id"]: row for row in draft["requirements"]}
        declarations = {row["item_id"]: row for row in draft["declarations"]}
        matches = {}
        for edge in edges:
            left, right = edge["left_ref"], edge["right_ref"]
            if machine_binding(edge, requirements[left], declarations[right]):
                matches.setdefault(left, []).append(right)
        draft.update(
            candidate_edges=edges,
            column_mapping=sheets[draft["declaration_import_id"]]["column_mapping"],
            task_source_labels={key: self._specification_name(run) for key, run in runs.items()},
            task_sheet_labels={key: self._sheet_name(sheet) for key, sheet in sheets.items()},
            version_relations=version["relations"],
            version_review_history=history,
            revision_conflicts=version["conflicts"],
            revision_states=version["states"],
            machine_matches={
                left: choices[0] for left, choices in matches.items() if len(choices) == 1
            },
            task_version_evidence=versions,
            task_revision_signature=_identity(
                "task_revision_state/1", json.dumps([versions, version], sort_keys=True)
            ),
            task_assets={
                "sources": {
                    key: {
                        "parse_run": run.model_dump(mode="json"),
                        "pdf_sha256": self.evidence.get_asset(run.source_asset_id).sha256,
                        "artifact_sha256": self.evidence.get_asset(run.artifact_asset_id).sha256,
                    }
                    for key, run in runs.items()
                },
                "declarations": {
                    key: {
                        name: sheet[name]
                        for name in ("asset_id", "sha256", "column_mapping", "sheet")
                    }
                    for key, sheet in sheets.items()
                },
            },
        )
        return draft

    def prepare(
        self,
        parse_run_id: str,
        declaration_import_id: str,
        *,
        pages: tuple[int, int] | None = None,
        task_name: str = "",
    ) -> dict:
        source = self.evidence.get_run(parse_run_id)
        if pages is not None and not (
            1
            <= pages[0]
            <= pages[1]
            <= (self.evidence.get_asset(source.source_asset_id).page_count or 0)
        ):
            raise IraError("invalid_location")
        sheet = self._sheet(declaration_import_id)
        items = self.evidence.list_run_evidence(
            parse_run_id, pages=tuple(range(pages[0], pages[1] + 1)) if pages else None
        )
        requirements = [
            candidate
            | {
                "candidate_id": _identity(candidate["evidence_id"], str(candidate["row_in_block"])),
                "parse_run_id": parse_run_id,
                "artifact_asset_id": source.artifact_asset_id,
            }
            for candidate in extract_requirements_from_evidence(items)
        ]
        draft_id = (
            _identity("draft/1", parse_run_id, declaration_import_id)
            if pages is None
            else _identity("draft/2", parse_run_id, declaration_import_id, *map(str, pages))
        )
        if task_name:
            draft_id = _identity("named_draft/1", draft_id, task_name)
        try:
            previous = self.store.get("draft", draft_id)
        except IraError as error:
            if error.code != "not_found":
                raise
        else:
            signature = _requirement_signature(requirements)
            if signature != _requirement_signature(previous["requirements"]):
                # Re-extraction can create a new draft without replacing a review target.
                draft_id = _identity("draft_extraction/1", draft_id, signature)
        record = {
            "draft_id": draft_id,
            "parse_run_id": parse_run_id,
            "declaration_asset_id": sheet["asset_id"],
            "declaration_import_id": declaration_import_id,
            "source_asset_id": source.source_asset_id,
            "requirements": requirements,
            "physical_pages": list(pages) if pages else None,
            "declarations": sheet["declarations"],
        }
        if task_name:
            record["task_name"] = task_name
        self.store.put(
            "draft",
            record,
            draft_id=draft_id,
            parse_run_id=parse_run_id,
            import_id=declaration_import_id,
        )
        return self._draft(draft_id)

    def recent_work(self) -> dict:
        drafts = []
        runs = []
        for kind, destination in (("draft", drafts), ("run", runs)):
            for row in self.store.recent_references(kind):
                try:
                    draft = self.store.get("draft", row["draft_id"])
                    if draft.get("task_scope"):
                        self._task_authority(draft["task_scope"])
                    else:
                        self.evidence.get_run(draft["parse_run_id"])
                        self._sheet(draft.get("declaration_import_id") or draft["import_id"])
                except IraError as error:
                    if error.code not in ("permission_denied", "source_missing", "not_found"):
                        raise
                    continue
                destination.append(row | {"label": self.draft_label(draft)})
        return {"drafts": drafts, "runs": runs}

    def input_summary(self, parse_run_id: str, import_id: str) -> dict:
        result = {}
        if parse_run_id:
            run = self.evidence.get_run(parse_run_id)
            result["specification"] = {
                "name": self._specification_name(run),
                "binding_state": run.binding_state,
                "page_count": self.evidence.get_asset(run.source_asset_id).page_count,
                "parser_version": run.parser_version,
            }
        if import_id:
            sheet = self._sheet(import_id)
            result["spreadsheet"] = {
                "name": self._sheet_name(sheet),
                "sheet": sheet.get("sheet"),
                "column_mapping": sheet["column_mapping"],
                "row_count": len(sheet["declarations"]),
                "risk_count": sum(bool(row["risk"]) for row in sheet["declarations"]),
            }
        return result

    def _draft(self, draft_id: str) -> dict:
        draft = self.store.get("draft", draft_id)
        if draft.get("task_scope"):
            return self._scoped_draft(draft)
        self.evidence.get_run(draft["parse_run_id"])
        sheet = self._sheet(draft["declaration_import_id"])
        draft["column_mapping"] = sheet["column_mapping"]
        # R1 drafts predate the annotation context. Reattach it on read only.
        if any("annotation_context" not in item for item in draft["requirements"]):
            evidence = {
                item.evidence_id: item
                for item in self.evidence.list_run_evidence(
                    draft["parse_run_id"],
                    pages=tuple(sorted({r["physical_page"] for r in draft["requirements"]})),
                )
            }
            draft["requirements"] = [
                attach_annotation_context(item, evidence[item["evidence_id"]])
                for item in draft["requirements"]
            ]
        # Keep old draft IDs and bytes. Recreate only the read projection when
        # the stored draft predates structured field values.
        for key in ("requirements", "declarations"):
            draft[key] = [
                item
                if "field_values" in item
                else item
                | normalize(
                    item["raw"], requirement=key == "requirements", normalize_properties=False
                )
                for item in draft[key]
            ]
        draft["candidate_edges"] = generate_candidates(draft["requirements"], draft["declarations"])
        return draft

    def get_draft(self, draft_id: str) -> dict:
        return self._draft(draft_id)

    def task_evidence(self, draft_id: str, evidence_id: str):
        """Resolve one task-selected evidence block before any payload read."""
        draft = self.store.get("draft", draft_id)
        if not (scope := draft.get("task_scope")):
            raise IraError("permission_denied")
        runs, _ = self._task_authority(scope)
        selected = next(
            (item for item in draft["task_evidence"] if item["evidence_id"] == evidence_id),
            None,
        )
        if selected is None or selected["physical_page"] not in scope["sources"].get(
            selected["parse_run_id"], []
        ):
            raise IraError("permission_denied")
        run = runs[selected["parse_run_id"]]
        item = self.evidence.get_evidence(evidence_id, parse_run_id=run.parse_run_id)
        if (
            item.parse_run_id != run.parse_run_id
            or item.source_asset_id != run.source_asset_id
            or item.artifact_asset_id != run.artifact_asset_id
            or item.physical_page != selected["physical_page"]
        ):
            raise IraError("permission_denied")
        return item

    def list_task_runs(self, draft_id: str) -> list[dict]:
        self.get_draft_label(draft_id)
        return [
            {key: run.get(key) for key in ("run_id", "created_at", "status", "stale_reason")}
            for identity in self.store.task_run_ids(draft_id)
            if (run := self.get_run(identity))
        ]

    def record_version_relation(
        self,
        draft_id: str,
        later_requirement_id: str,
        earlier_requirement_id: str,
        evidence_id: str,
        reviewer: str,
        note: str,
        *,
        decision: str = "confirmed",
        snapshot_id: str | None = None,
        review_token: str | None = None,
    ) -> dict:
        if (
            not reviewer.strip()
            or len(reviewer) > 120
            or not note.strip()
            or len(note) > 2000
            or decision not in ("confirmed", "rejected")
        ):
            raise IraError("invalid_input")
        draft = self._draft(draft_id)
        relation = next(
            (
                item
                for item in draft.get("version_relations", [])
                if item["later_requirement_id"] == later_requirement_id
                and item["earlier_requirement_id"] == earlier_requirement_id
            ),
            None,
        )
        if relation is None or evidence_id not in relation["evidence_ids"]:
            raise IraError("permission_denied")
        if snapshot_id is not None and snapshot_id != relation["snapshot_id"]:
            raise IraError("verification_stale")
        if relation["blockers"]:
            raise IraError("invalid_input")
        self._check_originals(draft)
        history = self.store.human_history(draft_id, later_requirement_id, earlier_requirement_id)
        previous = history[-1]["review_id"] if history else ""
        if review_token is not None and review_token != _identity(
            "revision_form", relation["snapshot_id"], previous
        ):
            raise IraError("verification_stale")
        event = {
            "review_id": uuid4().hex,
            "snapshot_id": relation["snapshot_id"],
            "draft_id": draft_id,
            "left_ref": later_requirement_id,
            "right_ref": earlier_requirement_id,
            "actor_kind": "local_human",
            "reviewer": reviewer.strip(),
            "time": _time(),
            "method": "local_version_relation_review",
            "note": note.strip(),
            "category": "version",
            "decision": decision,
            "evidence_id": evidence_id,
        }
        self.store.append_human_review(event, previous)
        return event

    def diagnose_requirements(
        self, parse_run_id: str, *, pages: tuple[int, int] | None = None
    ) -> dict:
        """Inspect the rule-based candidate funnel for a registered parse run."""
        from ira_native.application.candidates import diagnose_requirements

        return diagnose_requirements(
            self.evidence.list_run_evidence(
                parse_run_id, pages=tuple(range(pages[0], pages[1] + 1)) if pages else None
            )
        )

    def compare(
        self,
        left_id: str,
        right_id: str,
        *,
        fields_checked: bool,
        same_version: bool,
        left_row: int | None = None,
        right_row: int | None = None,
    ) -> dict:
        def from_evidence(evidence_id: str, row: int | None) -> dict:
            evidence = self.evidence.get_evidence(evidence_id)
            candidates = [
                item
                for item in extract_requirements(evidence)
                if row is None or item["row_in_block"] == row
            ]
            if len(candidates) != 1:
                raise IraError("invalid_input")
            self.evidence.get_pdf_page(evidence.source_asset_id, evidence.physical_page)
            return candidates[0]

        left, right = from_evidence(left_id, left_row), from_evidence(right_id, right_row)
        version_agrees = not (
            left["source_revision"]
            and right["source_revision"]
            and left["source_revision"] != right["source_revision"]
        )
        facts = {
            "entity_binding": "unconfirmed",
            "fields_checked_claimed": fields_checked,
            "annotation_association_unverified": any(
                ANNOTATION_RISK in item["risk"] for item in (left, right)
            ),
            "source_assumption": "candidate_only",
            "field_verification_state": "not_verified",
            "revision_relation": "same_version" if same_version and version_agrees else "unknown",
        }
        return {
            "left": left,
            "right": right,
            "preconditions": facts,
            "evaluation": compare_requirements(left, right, facts),
        }

    def _human_context(self, draft, requirement, declaration, edge) -> dict:
        scoped = draft.get("task_scope")
        parse_id = requirement["parse_run_id"] if scoped else draft["parse_run_id"]
        sheet_id = (
            declaration["declaration_import_id"] if scoped else draft["declaration_import_id"]
        )
        parse_run = self.evidence.get_run(parse_id)
        artifact = self.evidence.get_asset(parse_run.artifact_asset_id)
        pdf = self.evidence.get_asset(parse_run.source_asset_id)
        revisions = (
            draft["task_version_evidence"][parse_id]
            if scoped
            else [
                revision.model_dump(mode="json")
                for revision in self.evidence.revision_family(pdf.asset_id)
            ]
        )
        evidence = self.evidence.get_evidence(requirement["evidence_id"], parse_run_id=parse_id)
        sheet = self.store.sheet_scope_metadata(sheet_id) if scoped else self._sheet(sheet_id)
        label = requirement["source_revision"]
        inputs = {
            "schema": "human_verification/1",
            "ruleset_version": PART_RULESET_VERSION
            if requirement.get("part_number") or declaration.get("part_number")
            else RULESET_VERSION,
            "draft_id": draft["draft_id"],
            "edge": edge,
            "fields": review_fields(requirement, declaration, edge["field_pairing"]),
            "requirement_risk": sorted(
                {
                    *requirement["risk"],
                    *evidence.risk,
                    *([ANNOTATION_RISK] if evidence.annotations else []),
                }
            ),
            "inclusion_status": requirement["inclusion_status"],
            "same_revision": bool(
                label
                and label == declaration["source_revision"]
                and all(revision["revision_label"] == label for revision in revisions)
            ),
            "source": {
                "pdf_asset_id": pdf.asset_id,
                "pdf_sha256": pdf.sha256,
                "artifact_asset_id": artifact.asset_id,
                "artifact_sha256": artifact.sha256,
                "evidence": evidence.model_dump(mode="json"),
                "parse_run": parse_run.model_dump(mode="json"),
                "registered_revisions": revisions,
            },
            "declaration": {
                "asset_id": sheet["asset_id"],
                "sha256": sheet["sha256"],
                "import_id": sheet["import_id"],
                "column_mapping": sheet["column_mapping"],
                "row": declaration["row_id"],
                "sheet": declaration["sheet_or_section"],
                "original_cells": declaration["original_cells"],
                "source_revision": declaration["source_revision"],
            },
        }
        if scoped:
            revision_state = draft["revision_states"][requirement["candidate_id"]]
            association = source_association(
                requirement,
                declaration,
                edge,
                evidence,
                binding_mismatch=parse_run.binding_state == "mismatch",
            )
            inputs.update(
                schema="human_verification/2",
                task_scope=scoped,
                task_assets=draft["task_assets"],
                task_revision_signature=draft["task_revision_signature"],
                revision_blocker=_revision_blocker(revision_state),
                effective_replacement=revision_state == "effective_replacement"
                and inputs["same_revision"],
                explicit_non_applicability=requirement.get("applicability") == "not_applicable"
                and not requirement.get("expr"),
                calculation_source_blocker=association.basis[0]
                if association.status == "conflict"
                else None,
            )
        snapshot_id = _identity("verification_snapshot", json.dumps(inputs, sort_keys=True))
        history = self.store.human_history(draft["draft_id"], edge["left_ref"], edge["right_ref"])
        gates = assess_trust(inputs, history, snapshot_id)
        previous = history[-1]["review_id"] if history else ""
        return {
            "fields": inputs["fields"],
            "inputs": inputs,
            "history": history,
            "gates": gates,
            "snapshot_id": snapshot_id,
            "active_review_ids": gates["review_ids"],
            "previous_review_id": previous,
            "review_token": _identity("human_review", snapshot_id, previous),
        }

    def human_review_context(self, draft_id: str, left_ref: str, right_ref: str) -> dict:
        draft = self._draft(draft_id)
        self._check_originals(draft)
        edge = next(
            (
                edge
                for edge in draft["candidate_edges"]
                if (edge["left_ref"] == left_ref and edge["right_ref"] == right_ref)
            ),
            None,
        )
        if edge is None:
            raise IraError("not_found")
        requirement = next(
            item for item in draft["requirements"] if item["candidate_id"] == left_ref
        )
        declaration = next(item for item in draft["declarations"] if item["item_id"] == right_ref)
        return self._human_context(draft, requirement, declaration, edge)

    def record_human_review(
        self,
        draft_id: str,
        left_ref: str,
        right_ref: str,
        review_token: str,
        category: str,
        targets: list[str],
        decision: str,
        reviewer: str,
        note: str,
    ) -> dict:
        if (
            not reviewer.strip()
            or len(reviewer) > 120
            or not note.strip()
            or len(note) > 2000
            or decision not in ("confirmed", "rejected", "insufficient", "not_applicable")
            or (decision == "not_applicable" and category != "applicability")
        ):
            raise IraError("invalid_input")
        context = self.human_review_context(draft_id, left_ref, right_ref)
        if review_token != context["review_token"]:
            raise IraError("verification_stale")
        if category == "fields":
            if (
                not targets
                or len(targets) != len(set(targets))
                or not set(targets) <= context["fields"].keys()
            ):
                raise IraError("invalid_input")
        elif category not in GATE_NAMES or targets != [category]:
            raise IraError("invalid_input")
        event = {
            "review_id": uuid4().hex,
            "snapshot_id": context["snapshot_id"],
            "draft_id": draft_id,
            "left_ref": left_ref,
            "right_ref": right_ref,
            "actor_kind": "local_human",
            "reviewer": reviewer.strip(),
            "time": _time(),
            "method": "local_workbench_original_source_review",
            "note": note.strip(),
            "category": category,
            "targets": targets,
            "decision": decision,
            "fields": [context["fields"][key] for key in targets] if category == "fields" else [],
            "source": {
                "parse_run": context["inputs"]["source"]["parse_run"],
                "pdf_sha256": context["inputs"]["source"]["pdf_sha256"],
                "artifact_sha256": context["inputs"]["source"]["artifact_sha256"],
                "revisions": context["inputs"]["source"]["registered_revisions"],
                "declaration_sha256": context["inputs"]["declaration"]["sha256"],
                "declaration_import_id": context["inputs"]["declaration"]["import_id"],
                "declaration_revision": context["inputs"]["declaration"]["source_revision"],
            },
        }
        self.store.append_human_review(event, context["previous_review_id"])
        return event

    def _check_originals(self, draft: dict) -> None:
        if scope := draft.get("task_scope"):
            runs, sheets = self._task_authority(scope)
            for parse_run in runs.values():
                self.evidence.get_pdf_bytes(parse_run.source_asset_id)
                self.evidence.check_parse_artifact(parse_run.parse_run_id)
            for sheet in sheets.values():
                self._check_sheet_bytes(sheet)
            return
        self.evidence.get_pdf_bytes(draft["source_asset_id"])
        self.evidence.check_parse_artifact(draft["parse_run_id"])
        self._check_sheet_bytes(self._sheet(draft["declaration_import_id"]))

    def _check_sheet_bytes(self, sheet):
        if _sha(self.store.get_sheet_data(sheet["asset_id"])) != sheet["sha256"]:
            raise IraError("source_mismatch")
        if sheet["locator_internal"]:
            _, data = self.evidence.files.read(
                Path(sheet["locator_internal"]), self.evidence.settings.max_json_bytes
            )
            if _sha(data) != sheet["sha256"]:
                raise IraError("source_mismatch")

    def semantic_binding(self, draft_id: str) -> dict:
        from ira_native.application.semantic_intake import task_binding

        draft = self._draft(draft_id)
        self._check_originals(draft)
        return task_binding(self, draft)

    def recalculate_pair(
        self, draft_id: str, left_ref: str, right_ref: str, reviewer: str, expected_run: str
    ) -> dict:
        latest = self.store.latest_selection(draft_id)
        if (latest["run_id"] if latest else "") != expected_run:
            raise IraError("selection_changed")
        if latest:
            selected = latest["applicability_snapshot"]
            if not any(
                (item["requirement_id"], item["declaration_id"]) == (left_ref, right_ref)
                for item in selected
            ):
                raise IraError("pair_not_selected")
            current_edges = {
                (edge["left_ref"], edge["right_ref"])
                for edge in self._draft(draft_id)["candidate_edges"]
            }
            selected = [
                item
                for item in selected
                if (item["requirement_id"], item["declaration_id"]) in current_edges
                or item["requirement_id"] == left_ref
            ]
        else:
            selected = [{"requirement_id": left_ref, "declaration_id": right_ref}]
        return self.verify(
            draft_id,
            selected,
            reviewer,
            use_human_reviews=True,
            conditional_preview=True,
            mapping_reviewed=bool(latest and latest["column_mapping_review"] == "user_reported"),
            expected_run=expected_run,
        )

    def verify(
        self,
        draft_id: str,
        confirmations: list[dict],
        reviewer: str,
        *,
        mapping_reviewed: bool = False,
        conditional_preview: bool = False,
        use_human_reviews: bool = False,
        semantic_proposal: dict | None = None,
        semantic_producer: str | None = None,
        expected_run: str | None = None,
    ) -> dict:
        if not reviewer.strip() or len(reviewer) > 120:
            raise IraError("invalid_input")
        draft = self._draft(draft_id)
        # Recheck registered bytes immediately before issuing a new decision snapshot.
        self._check_originals(draft)
        compilation = None
        if semantic_proposal is not None:
            from ira_native.application.semantic_intake import compile_proposal

            if confirmations or use_human_reviews or mapping_reviewed:
                raise IraError("invalid_input")
            confirmations, compilation = compile_proposal(
                self, draft, semantic_proposal, semantic_producer
            )
            conditional_preview = True
        elif semantic_producer is not None:
            raise IraError("invalid_input")
        parse_run = self.evidence.get_run(draft["parse_run_id"])
        scoped = draft.get("task_scope")
        requirements = {r["candidate_id"]: r for r in draft["requirements"]}
        declarations = {r["item_id"]: r for r in draft["declarations"]}
        edges = {(edge["left_ref"], edge["right_ref"]): edge for edge in draft["candidate_edges"]}
        matched = {}
        try:
            for value in confirmations:
                confirmation = Confirmation.model_validate(value)
                left, right = confirmation.requirement_id, confirmation.declaration_id
                if left in matched or (left, right) not in edges:
                    raise IraError("invalid_input")
                matched[left] = confirmation
        except (ValidationError, KeyError):
            raise IraError("invalid_input") from None
        run_id = uuid4().hex
        findings = []
        for candidate_id, requirement in requirements.items():
            confirmation = matched.get(candidate_id)
            edge = edges[candidate_id, confirmation.declaration_id] if confirmation else None
            pairing = edge["field_pairing"] if edge else None
            declaration = None
            if confirmation:
                declaration = paired_declaration(
                    requirement, declarations[confirmation.declaration_id], edge
                )
            actual_run = self.evidence.get_run(requirement["parse_run_id"]) if scoped else parse_run
            evidence = self.evidence.get_evidence(
                requirement["evidence_id"], parse_run_id=actual_run.parse_run_id
            )
            association = source_association(
                requirement,
                declaration,
                edge,
                evidence,
                binding_mismatch=actual_run.binding_state == "mismatch",
            )
            preconditions = {
                "entity_binding": "unconfirmed",
                "binding_claimed": confirmation is not None,
                "annotation_association_unverified": ANNOTATION_RISK in requirement["risk"],
                "source_assumption": "candidate_only",
                "revision_relation": "unknown",
                "record_completeness": "unknown",
            }
            revision_state = draft["revision_states"][candidate_id] if scoped else None
            revision_blocker = _revision_blocker(revision_state)
            if (
                scoped
                and declaration
                and not revision_blocker
                and (
                    requirement["source_revision"]
                    and requirement["source_revision"] == declaration["source_revision"]
                    and all(
                        rev["revision_label"] == requirement["source_revision"]
                        for rev in draft["task_version_evidence"][actual_run.parse_run_id]
                    )
                )
            ):
                preconditions["revision_relation"] = (
                    "explicit_supersedes"
                    if revision_state == "effective_replacement"
                    else "same_version"
                )
            if confirmation and not scoped:
                if confirmation.same_version and not (
                    requirement["source_revision"]
                    and declaration["source_revision"]
                    and requirement["source_revision"] != declaration["source_revision"]
                ):
                    preconditions["revision_relation"] = "same_version"
            formal_context = None
            effective_requirement = requirement
            preview_revision = preconditions["revision_relation"]
            if use_human_reviews and edge:
                formal_context = self._human_context(
                    draft, requirement, declarations[confirmation.declaration_id], edge
                )
                gates = formal_context["gates"]
                # These preconditions are derived from current records. The old
                # checkbox revision claim remains available to the preview only.
                preconditions["revision_relation"] = gates["revision_relation"]
                if preview_revision == "unknown":
                    preview_revision = gates["revision_relation"]
                if not gates["blockers"]:
                    applicable = gates["applicability_review"] == "confirmed"
                    preconditions.update(
                        source_assumption="verified_for_checked_fields",
                        entity_binding="confirmed",
                        applicability="applicable" if applicable else "not_applicable",
                    )
                    if applicable:
                        effective_requirement = requirement | {"inclusion_status": "in_scope"}
                    if gates["revision_relation"] == "explicit_supersedes":
                        preconditions.update(
                            amendment_scope_confirmed=True,
                            effective_requirement_revision=requirement["source_revision"],
                        )
            evaluation = evaluate(effective_requirement, declaration, preconditions)
            if revision_blocker:
                evaluation["reason_code"] = revision_blocker
            blockers = (
                formal_context["gates"]
                if formal_context
                else (
                    {"blockers": [revision_blocker or "PAIR_NOT_SELECTED"]}
                    if use_human_reviews or revision_blocker
                    else None
                )
            )
            preview = None
            if conditional_preview:
                preview = (
                    {"status": "undetermined", "reason_code": revision_blocker}
                    if revision_blocker
                    else _conditional_preview(
                        requirement, declaration, edge, preview_revision, association
                    )
                )
            evaluation_id = _identity(run_id, candidate_id, "requirement_evaluation")
            findings.append(
                {
                    "finding_id": _identity(run_id, candidate_id),
                    "evaluation_id": evaluation_id,
                    "kind": "requirement_evaluation",
                    "requirement": requirement,
                    "declaration": declaration,
                    "candidate_edge": edge,
                    "field_pairing": pairing,
                    "evaluation": evaluation,
                    "source_association": association.model_dump(mode="json"),
                    "conditional_calculation": preview,
                    **({"revision_state": revision_state} if scoped else {}),
                    "disposition": evaluation["finding_disposition"],
                    "trust_gates": blockers,
                    "formal_status": (
                        evaluation["requirement_evaluation"]
                        if preconditions["source_assumption"] == "verified_for_checked_fields"
                        else "undetermined"
                    ),
                    "verification_snapshot": {
                        "snapshot_id": formal_context["snapshot_id"],
                        "inputs": formal_context["inputs"],
                        "review_ids": formal_context["active_review_ids"],
                        "rule_preconditions": preconditions,
                        "effective_requirement": effective_requirement,
                        "effective_declaration": declaration,
                    }
                    if formal_context
                    else None,
                    "preconditions": preconditions,
                    "evidence_refs": [requirement["evidence_id"]]
                    + ([declaration["item_id"]] if declaration else []),
                    "review_state": "unreviewed",
                    "review_history": [],
                }
            )
        sheet = (
            self.store.sheet_scope_metadata(draft["declaration_import_id"])
            if scoped
            else self._sheet(draft["declaration_import_id"])
        )
        record = {
            "run_id": run_id,
            "draft_id": draft_id,
            "parse_run_id": draft["parse_run_id"],
            "source_asset_id": draft["source_asset_id"],
            "binding_state": parse_run.binding_state.value,
            "field_verification_state": "verified"
            if findings
            and all(
                (finding["trust_gates"] or {}).get("field_state") == "verified"
                for finding in findings
            )
            else "not_verified",
            "column_mapping_review": "user_reported" if mapping_reviewed else "not_recorded",
            "created_at": _time(),
            "reviewer": reviewer,
            "source_hashes": [
                self.evidence.get_asset(parse_run.source_asset_id).sha256,
                self.evidence.get_asset(parse_run.artifact_asset_id).sha256,
                sheet["sha256"],
            ],
            "ruleset_version": PART_RULESET_VERSION
            if any(
                finding["requirement"].get("part_number")
                or (finding["declaration"] or {}).get("part_number")
                for finding in findings
            )
            else RULESET_VERSION,
            "findings": findings,
            "applicability_snapshot": [value.model_dump() for value in matched.values()],
            "result_ids": [value["finding_id"] for value in findings],
            "status": "recorded" if findings else "no_evidence_found",
        }
        if scoped:
            record.update(
                task_scope=scoped,
                task_assets=draft["task_assets"],
                task_revision_signature=draft["task_revision_signature"],
                version_relations=draft["version_relations"],
                task_version_evidence=draft["task_version_evidence"],
                revision_conflicts=draft["revision_conflicts"],
            )
        if compilation is not None:
            record["host_compilation"] = compilation
        self.store.put_run(record, expected_run)
        return self.get_run(run_id) if use_human_reviews else record

    def observe_task(
        self, *, sources: dict[str, tuple[int, ...]], declarations: dict[str, tuple[int, ...]]
    ) -> TaskObservation:
        from ira_native.application.task_observation import observe_task

        return observe_task(self, sources=sources, declarations=declarations)

    def domain_result(self, run_id: str) -> TaskObservation:
        record = self.get_run(run_id)
        result = self.project_domain_result(record)
        if scope := record.get("task_scope"):
            from ira_native.application.task_observation import read_scoped_evidence

            draft = self._draft(record["draft_id"])
            runs, _ = self._task_authority(scope)
            evidence = read_scoped_evidence(self.evidence, scope["sources"], runs)
            facts = {fact.identity: fact for fact in result.facts}
            for row in draft["declarations"]:
                fact = fact_for(row)
                facts.setdefault(fact.identity, fact)
            result = result.model_copy(
                update={
                    "facts": list(facts.values()),
                    "requirements": draft["requirements"],
                    "declarations": draft["declarations"],
                    "candidate_edges": draft["candidate_edges"],
                    "evidence": evidence,
                    "source_versions": {
                        key: [DocumentRevision.model_validate(row) for row in rows]
                        for key, rows in record["task_version_evidence"].items()
                    },
                }
            )
        return result

    @staticmethod
    def project_domain_result(record: dict) -> TaskObservation:
        stale = record["status"] == "stale"
        facts = {}
        versions = {}
        for finding in record["findings"]:
            snapshot = finding.get("verification_snapshot") or {}
            source = snapshot.get("inputs", {}).get("source", {})
            if source:
                versions[record["parse_run_id"]] = source["registered_revisions"]
            for paired, item in enumerate((finding["requirement"], finding.get("declaration"))):
                if item:
                    fact = fact_for(item, paired=bool(paired))
                    facts[fact.identity] = fact
        return TaskObservation(
            run_id=record["run_id"],
            status=record["status"],
            ruleset_version=record["ruleset_version"],
            facts=list(facts.values()),
            findings=[persisted_finding(finding, stale=stale) for finding in record["findings"]],
            source_versions=versions,
            task_scope=record.get("task_scope"),
            version_relations=record.get("version_relations", []),
            host_compilation=record.get("host_compilation"),
            blockers=[record["stale_reason"]]
            if stale
            else (["NO_REQUIREMENT_OBSERVED"] if not record["findings"] else []),
        )

    def get_run(self, run_id: str) -> dict:
        record = self.store.get("run", run_id)
        try:
            draft = self._draft(record["draft_id"])
            self._check_originals(draft)
        except IraError as error:
            if error.code not in ("source_missing", "source_mismatch"):
                raise
            record["status"] = "stale"
            record["stale_reason"] = error.code
        if (
            record.get("task_scope")
            and record["status"] != "stale"
            and (
                draft["task_revision_signature"] != record["task_revision_signature"]
                or draft["task_assets"] != record["task_assets"]
            )
        ):
            record.update(status="stale", stale_reason="verification_stale")
        if (compilation := record.get("host_compilation")) and record["status"] != "stale":
            from ira_native.application.semantic_intake import task_binding

            current = task_binding(self, draft)
            if any(compilation[key] != current[key] for key in ("input_hash", "ref_map_hash")):
                record.update(status="stale", stale_reason="verification_stale")
        historical_field_state = record.get("field_verification_state", "not_verified")
        record["field_verification_state"] = "not_verified"
        record.setdefault("binding_state", "unknown")
        if record["status"] != "stale" and any(
            f.get("verification_snapshot") for f in record["findings"]
        ):
            requirements = {item["candidate_id"]: item for item in draft["requirements"]}
            declarations = {item["item_id"]: item for item in draft["declarations"]}
            edges = {
                (edge["left_ref"], edge["right_ref"]): edge for edge in draft["candidate_edges"]
            }
            verified_fields = 0
            for finding in record["findings"]:
                snapshot = finding.get("verification_snapshot")
                if snapshot is None:
                    continue
                pair = finding["candidate_edge"]
                left_ref, right_ref = pair["left_ref"], pair["right_ref"]
                edge = edges.get((left_ref, right_ref))
                current = (
                    self._human_context(
                        draft, requirements[left_ref], declarations[right_ref], edge
                    )
                    if edge
                    else None
                )
                if current is None or (
                    current["snapshot_id"] != snapshot["snapshot_id"]
                    or current["active_review_ids"] != snapshot["review_ids"]
                    or (
                        finding["preconditions"]["source_assumption"]
                        == "verified_for_checked_fields"
                        and current["gates"]["blockers"]
                    )
                ):
                    record.update(status="stale", stale_reason="verification_stale")
                    break
                verified_fields += current["gates"]["field_state"] == "verified"
            if verified_fields == len(record["findings"]):
                record["field_verification_state"] = "verified"
        for finding in record["findings"]:
            if (
                not finding.get("verification_snapshot")
                and finding["preconditions"].get("source_assumption")
                == "verified_for_checked_fields"
            ):
                if record["status"] != "stale":
                    record.update(status="stale", stale_reason="field_verification_missing")
                record["field_verification_state"] = "not_verified"
        if record["status"] == "stale":
            record["historical_field_verification_state"] = historical_field_state
            record["field_verification_state"] = "not_verified"
        history = self.store.events("review", run_id)
        for finding in record["findings"]:
            facts = finding["preconditions"]
            if record["status"] == "stale" and "formal_status" in finding:
                finding["historical_formal_status"] = finding["formal_status"]
                finding["formal_status"] = "undetermined"
            if (
                not finding.get("verification_snapshot")
                and facts.get("entity_binding") == "confirmed"
            ):
                facts.update(entity_binding="unconfirmed", binding_claimed=True)
            entries = [event for event in history if event["finding_id"] == finding["finding_id"]]
            if entries:
                finding["review_history"] = entries
                finding["review_state"] = entries[-1]["decision"]
        record["agent_history"] = self._agent_history(run_id)
        return record

    def review(self, run_id: str, finding_id: str, reviewer: str, decision: str, note: str):
        run = self.get_run(run_id)
        if run["status"] == "stale":
            raise IraError(run["stale_reason"])
        if (
            decision not in _REVIEW_DECISIONS
            or not reviewer.strip()
            or len(reviewer) > 120
            or not note.strip()
            or len(note) > 2000
        ):
            raise IraError("invalid_input")
        finding = next((f for f in run["findings"] if f["finding_id"] == finding_id), None)
        if finding is None:
            raise IraError("not_found")
        event = {
            "review_id": uuid4().hex,
            "finding_id": finding_id,
            "reviewer": reviewer,
            "decision": decision,
            "scope": finding["requirement"]["scope"],
            "note": note,
            "source_snapshot": run["source_hashes"],
            "time": _time(),
        }
        self.store.append("review", run_id, event)
        return event

    def export_csv(self, run_id: str) -> str:
        run = self.get_run(run_id)
        stream = io.StringIO()
        writer = csv.writer(stream)
        writer.writerow(
            [
                "finding_id",
                "run_status",
                "equipment_class",
                "scope",
                "requirement",
                "declaration",
                "disposition",
                "reason",
                "pdf_evidence",
                "sheet",
                "row",
                "review_state",
            ]
        )
        for finding in run["findings"]:
            left, right = finding["requirement"], finding["declaration"] or {}
            writer.writerow(
                [
                    _csv_value(value)
                    for value in (
                        finding["finding_id"],
                        run["status"],
                        left["equipment_class"],
                        left["scope"],
                        left["expr"],
                        right.get("expr"),
                        finding["disposition"],
                        finding["evaluation"].get("reason_code"),
                        left["evidence_id"],
                        right.get("sheet_or_section"),
                        right.get("row_id"),
                        finding["review_state"],
                    )
                ]
            )
        return stream.getvalue()

    def _agent_history(self, run_id: str) -> list[dict]:
        latest = {}
        for event in self.store.events("agent", run_id):
            latest[event.get("task_id") or str(len(latest))] = event
        return [
            event | {"task_state": "abandoned", "stop_reason": "session_ended"}
            if event.get("task_state") == "running"
            and event["task_id"] not in self._active_agent_tasks
            else event
            for event in latest.values()
        ]

    def get_agent_task(self, run_id: str, task_id: str) -> dict:
        run = self.get_run(run_id)
        for event in run["agent_history"]:
            if event.get("task_id") == task_id:
                return event
        raise IraError("not_found")

    def launch_investigation(self, run_id: str, **options) -> str:
        run = self.get_run(run_id)
        if run.get("task_scope"):
            raise IraError("permission_denied")
        if run["status"] == "stale":
            raise IraError(run["stale_reason"])
        task_id = uuid4().hex
        self.store.append(
            "agent",
            run_id,
            {
                "task_id": task_id,
                "task_state": "running",
                "status": "running",
                "time": _time(),
            },
        )
        self._active_agent_tasks.add(task_id)
        task = asyncio.create_task(self._run_investigation(run_id, task_id, options))
        self._background_agents.add(task)
        task.add_done_callback(self._background_agents.discard)
        return task_id

    async def _run_investigation(self, run_id: str, task_id: str, options: dict) -> None:
        try:
            result = await self.investigate(run_id, task_id=task_id, **options)
            if result["status"] != "no_gap":
                return  # The active workflow already persisted its full terminal record.
        except IraError as error:
            result = {"status": error.code, "task_state": "blocked", "stop_reason": error.code}
        except asyncio.CancelledError:
            result = {
                "status": "agent_cancelled",
                "task_state": "cancelled",
                "stop_reason": "cancelled",
            }
        finally:
            self._active_agent_tasks.discard(task_id)
        self.store.append("agent", run_id, result | {"task_id": task_id, "time": _time()})

    async def close_background_agents(self) -> None:
        tasks = tuple(self._background_agents)
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

    async def investigate(
        self,
        run_id: str,
        *,
        model=None,
        goal: str | None = None,
        finding_id: str | None = None,
        allow_reverification: bool = False,
        task_id: str | None = None,
    ) -> dict:
        from ira_native.agents.agent_policy import RECOMPUTE, SEARCH, WORKFLOW, TaskScope
        from ira_native.agents.evidence_agent import investigate
        from ira_native.agents.ollama import AgentLimits

        run = self.get_run(run_id)
        if run.get("task_scope"):
            raise IraError("permission_denied")
        if run["status"] == "stale":
            raise IraError(run["stale_reason"])
        findings = run["findings"]
        if finding_id is not None:
            target = next(
                (finding for finding in findings if finding["finding_id"] == finding_id), None
            )
            if target is None:
                raise IraError("not_found")
        else:
            target = next(
                (
                    finding
                    for finding in findings
                    if finding["disposition"] in ("review_required", "blocked")
                ),
                None,
            )
        if target is None and run["status"] != "no_evidence_found" and goal is None:
            return {
                "status": "no_gap",
                "task_state": "completed",
                "stop_reason": "no_gap",
                "events": [],
            }
        if goal is not None and (not goal.strip() or len(goal) > 256 or "\0" in goal):
            raise IraError("invalid_input")
        draft = self._draft(run["draft_id"])
        parse = self.evidence.get_run(run["parse_run_id"])
        task_id = task_id or uuid4().hex
        limits = AgentLimits.from_env()
        controls = frozenset({RECOMPUTE}) if allow_reverification and target else frozenset()
        candidate = (target or {}).get("requirement", {})
        gates = (target or {}).get("trust_gates") or {}
        snapshot = (target or {}).get("verification_snapshot") or {}
        scope = TaskScope(
            task_id,
            parse.parse_run_id,
            parse.source_asset_id,
            parse.artifact_asset_id,
            workflow_run_id=run_id,
            declaration_asset_id=draft["declaration_asset_id"],
            candidate_ids=(candidate["candidate_id"],) if candidate else (),
            data_tools=frozenset(SEARCH | WORKFLOW),
            control_tools=controls,
            physical_pages=tuple(draft["physical_pages"]) if draft.get("physical_pages") else None,
        )
        evaluation = (target or {}).get("evaluation") or {}
        conditional = (target or {}).get("conditional_calculation") or {}
        context = {
            "workflow_run_id": run_id,
            "status": run["status"],
            "candidate_ids": scope.candidate_ids,
            "field_names": gates.get("unverified_fields", [])[:20],
            "blockers": gates.get("blockers", [])[:16]
            or (
                [evaluation["reason_code"]]
                if target and target["disposition"] in ("review_required", "blocked")
                else []
            ),
            "formal_status": (target or {}).get("formal_status"),
            "conditional_calculation": conditional.get("status"),
            "snapshot_id": snapshot.get("snapshot_id"),
        }

        def reevaluate() -> dict:
            current = self.get_run(run_id)
            if current["status"] == "stale":
                raise IraError(current["stale_reason"])
            selected = next(
                item for item in current["findings"] if item["finding_id"] == target["finding_id"]
            )
            saved = selected.get("verification_snapshot")
            if saved:
                return evaluate(
                    saved["effective_requirement"],
                    saved["effective_declaration"],
                    saved["rule_preconditions"],
                )
            return evaluate(
                selected["requirement"],
                selected["declaration"],
                selected["preconditions"]
                | {"source_assumption": "candidate_only", "entity_binding": "unconfirmed"},
            )

        task_goal = goal or (
            f"Find evidence for the {candidate['equipment_class']} requirement and its verification gaps."
            if candidate
            else "Find GPON equipment requirements in the authorized source."
        )
        start = {
            "task_id": task_id,
            "task_state": "running",
            "status": "running",
            "time": _time(),
            "scope": scope.audit(),
            "budget": limits.model_dump(),
            "goal": task_goal,
        }
        self.store.append("agent", run_id, start)
        self._active_agent_tasks.add(task_id)
        try:
            result = await investigate(
                self.evidence,
                parse.parse_run_id,
                task_goal,
                model=model,
                scope=scope,
                reevaluate=reevaluate if controls else None,
                workflow_context=context,
                limits=limits,
            )
        except IraError as error:
            result = start | {
                "task_state": "blocked",
                "status": error.code,
                "stop_reason": error.code,
            }
        finally:
            self._active_agent_tasks.discard(task_id)
        result = start | result
        self.store.append("agent", run_id, result | {"time": _time()})
        return result
