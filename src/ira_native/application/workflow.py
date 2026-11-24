"""Compose persisted verification inputs, deterministic decisions and human reviews."""

import csv
import io
import json
from collections import defaultdict
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, ValidationError

from ira_native.adapters.spreadsheet import read_sheet
from ira_native.adapters.workflow_store import WorkflowStore
from ira_native.application.candidates import (
    attach_annotation_context,
    extract_requirements,
    normalize,
)
from ira_native.application.service import EvidenceService, _identity, _sha
from ira_native.domain.models import ANNOTATION_RISK
from ira_native.domain.rules import RULESET_VERSION, compare_requirements, evaluate
from ira_native.errors import IraError

_REVIEW_DECISIONS = {"accepted_with_scope", "rejected_with_scope", "needs_more_evidence"}
_MATCH_FIELDS = ("equipment_class", "scope", "config_variant", "site_group", "property")


class Confirmation(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    requirement_id: str
    declaration_id: str
    fields_checked: bool = False
    same_version: bool = False


def _time():
    return datetime.now(UTC).isoformat()


def _related(requirement: dict, row: dict) -> bool:
    return (
        bool(requirement["scope"] and requirement["config_variant"])
        and _match_key(requirement) == _match_key(row)
        and (not requirement["model"] or requirement["model"] == row["model"])
    )


def _match_key(record: dict) -> tuple:
    return tuple(record[field] for field in _MATCH_FIELDS)


def _csv_value(value):
    if value is None:
        return ""
    text = str(value)
    if text and (ord(text[0]) < 32 or text.lstrip().startswith(("=", "+", "-", "@"))):
        return "'" + text
    return text


class VerificationWorkflow:
    def __init__(self, evidence: EvidenceService):
        self.evidence = evidence
        self.store = WorkflowStore(evidence.repository)

    def ingest_pair_bytes(self, pdf: bytes, parsed: bytes):
        return self.evidence.ingest_uploaded_pair(pdf, parsed)

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
        declarations = []
        for row in rows:
            item_id = _identity(import_id, str(row["row_id"]))
            item = normalize(row["raw"], requirement=False)
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
            k: v for k, v in record.items() if k not in ("locator_internal", "declarations")
        } | {"row_count": len(declarations)}

    def _sheet(self, import_id):
        record = self.store.get("sheet", import_id)
        if record["locator_internal"]:
            self.evidence.files.authorize(Path(record["locator_internal"]))
        return record

    def prepare(self, parse_run_id: str, declaration_import_id: str) -> dict:
        source = self.evidence.get_run(parse_run_id)
        sheet = self._sheet(declaration_import_id)
        requirements = [
            candidate
            | {"candidate_id": _identity(item.evidence_id, str(candidate["row_in_block"]))}
            for item in self.evidence.list_run_evidence(parse_run_id)
            for candidate in extract_requirements(item)
        ]
        draft_id = _identity("draft/1", parse_run_id, declaration_import_id)
        record = {
            "draft_id": draft_id,
            "parse_run_id": parse_run_id,
            "declaration_asset_id": sheet["asset_id"],
            "declaration_import_id": declaration_import_id,
            "source_asset_id": source.source_asset_id,
            "requirements": requirements,
            "declarations": sheet["declarations"],
        }
        self.store.put(
            "draft",
            record,
            draft_id=draft_id,
            parse_run_id=parse_run_id,
            import_id=declaration_import_id,
        )
        return self._draft(draft_id)

    def _draft(self, draft_id: str) -> dict:
        draft = self.store.get("draft", draft_id)
        self.evidence.get_run(draft["parse_run_id"])
        self._sheet(draft["declaration_import_id"])
        # R1 drafts predate the annotation context. Reattach it on read only.
        if any("annotation_context" not in item for item in draft["requirements"]):
            evidence = {
                item.evidence_id: item
                for item in self.evidence.list_run_evidence(draft["parse_run_id"])
            }
            draft["requirements"] = [
                attach_annotation_context(item, evidence[item["evidence_id"]])
                for item in draft["requirements"]
            ]
        return draft

    def get_draft(self, draft_id: str) -> dict:
        return self._draft(draft_id)

    def diagnose_requirements(self, parse_run_id: str) -> dict:
        """Inspect the rule-based candidate funnel for a registered parse run."""
        from ira_native.application.candidates import diagnose_requirements

        return diagnose_requirements(self.evidence.list_run_evidence(parse_run_id))

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

    def _check_originals(self, draft: dict) -> None:
        self.evidence.get_pdf_bytes(draft["source_asset_id"])
        sheet = self._sheet(draft["declaration_import_id"])
        if sheet["locator_internal"]:
            _, data = self.evidence.files.read(
                Path(sheet["locator_internal"]), self.evidence.settings.max_json_bytes
            )
            if _sha(data) != sheet["sha256"]:
                raise IraError("source_mismatch")

    def verify(self, draft_id: str, confirmations: list[dict], reviewer: str) -> dict:
        if not reviewer.strip() or len(reviewer) > 120:
            raise IraError("invalid_input")
        draft = self._draft(draft_id)
        # Recheck registered bytes immediately before issuing a new decision snapshot.
        self._check_originals(draft)
        requirements = {r["candidate_id"]: r for r in draft["requirements"]}
        declarations = {r["item_id"]: r for r in draft["declarations"]}
        groups = defaultdict(list)
        for declaration in declarations.values():
            groups[_match_key(declaration)].append(declaration)
        matched = {}
        try:
            for value in confirmations:
                confirmation = Confirmation.model_validate(value)
                left, right = confirmation.requirement_id, confirmation.declaration_id
                if left in matched or not _related(requirements[left], declarations[right]):
                    raise IraError("invalid_input")
                matched[left] = confirmation
        except (ValidationError, KeyError):
            raise IraError("invalid_input") from None
        run_id = uuid4().hex
        findings = []
        for candidate_id, requirement in requirements.items():
            confirmation = matched.get(candidate_id)
            options = [
                row for row in groups.get(_match_key(requirement), ()) if _related(requirement, row)
            ]
            declaration = (
                declarations[confirmation.declaration_id]
                if confirmation
                else options[0]
                if len(options) == 1
                else None
            )
            preconditions = {
                "entity_binding": "unconfirmed",
                "binding_claimed": confirmation is not None,
                "annotation_association_unverified": ANNOTATION_RISK in requirement["risk"],
                "source_assumption": "candidate_only",
                "revision_relation": "unknown",
                "record_completeness": "unknown",
            }
            if confirmation:
                if confirmation.same_version and not (
                    requirement["source_revision"]
                    and declaration["source_revision"]
                    and requirement["source_revision"] != declaration["source_revision"]
                ):
                    preconditions["revision_relation"] = "same_version"
            evaluation = evaluate(requirement, declaration, preconditions)
            evaluation_id = _identity(run_id, candidate_id, "requirement_evaluation")
            findings.append(
                {
                    "finding_id": _identity(run_id, candidate_id),
                    "evaluation_id": evaluation_id,
                    "kind": "requirement_evaluation",
                    "requirement": requirement,
                    "declaration": declaration,
                    "evaluation": evaluation,
                    "disposition": evaluation["finding_disposition"],
                    "preconditions": preconditions,
                    "evidence_refs": [requirement["evidence_id"]]
                    + ([declaration["item_id"]] if declaration else []),
                    "review_state": "unreviewed",
                    "review_history": [],
                }
            )
        sheet = self._sheet(draft["declaration_import_id"])
        parse_run = self.evidence.get_run(draft["parse_run_id"])
        record = {
            "run_id": run_id,
            "draft_id": draft_id,
            "parse_run_id": draft["parse_run_id"],
            "source_asset_id": draft["source_asset_id"],
            "binding_state": parse_run.binding_state.value,
            "field_verification_state": "not_verified",
            "created_at": _time(),
            "reviewer": reviewer,
            "source_hashes": [
                self.evidence.get_asset(parse_run.source_asset_id).sha256,
                self.evidence.get_asset(parse_run.artifact_asset_id).sha256,
                sheet["sha256"],
            ],
            "ruleset_version": RULESET_VERSION,
            "findings": findings,
            "applicability_snapshot": [value.model_dump() for value in matched.values()],
            "result_ids": [value["finding_id"] for value in findings],
            "status": "recorded" if findings else "no_evidence_found",
        }
        self.store.put("run", record, run_id=run_id, draft_id=draft_id)
        return record

    def get_run(self, run_id: str) -> dict:
        record = self.store.get("run", run_id)
        draft = self._draft(record["draft_id"])
        try:
            self._check_originals(draft)
        except IraError as error:
            if error.code not in ("source_missing", "source_mismatch"):
                raise
            record["status"] = "stale"
            record["stale_reason"] = error.code
        record["field_verification_state"] = "not_verified"
        record.setdefault("binding_state", "unknown")
        if record["status"] != "stale" and any(
            finding["preconditions"].get("source_assumption") == "verified_for_checked_fields"
            for finding in record["findings"]
        ):
            record["status"] = "stale"
            record["stale_reason"] = "field_verification_missing"
        history = self.store.events("review", run_id)
        for finding in record["findings"]:
            facts = finding["preconditions"]
            if facts.get("entity_binding") == "confirmed":
                facts.update(entity_binding="unconfirmed", binding_claimed=True)
            entries = [event for event in history if event["finding_id"] == finding["finding_id"]]
            if entries:
                finding["review_history"] = entries
                finding["review_state"] = entries[-1]["decision"]
        record["agent_history"] = self.store.events("agent", run_id)
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

    async def investigate(self, run_id: str, *, model=None) -> dict:
        from ira_native.agents.evidence_agent import investigate

        run = self.get_run(run_id)
        if run["status"] == "stale":
            raise IraError(run["stale_reason"])
        target = next(
            (
                item
                for item in run["findings"]
                if item["disposition"] in ("review_required", "blocked")
            ),
            None,
        )
        if target is None and run["status"] != "no_evidence_found":
            return {"status": "no_gap", "events": []}
        gap = "Find GPON equipment requirements in the source"
        if target is not None:
            requirement = target["requirement"]
            category = requirement["equipment_class"]
            variant = (requirement["config_variant"] or "")[:48]
            scope = (requirement["scope"] or "")[:48]
            gap = (
                f'Start with the literal class term "{category}". Inspect hits for '
                f"configuration {variant!r} in scope {scope!r}."
            )

        def reevaluate() -> dict:
            self._check_originals(self._draft(run["draft_id"]))
            return evaluate(target["requirement"], target["declaration"], target["preconditions"])

        result = await investigate(
            self.evidence,
            run["parse_run_id"],
            gap,
            model=model,
            reevaluate=reevaluate if target is not None else None,
        )
        self.store.append("agent", run_id, result | {"time": _time()})
        return result
