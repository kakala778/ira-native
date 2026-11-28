"""Immutable task scope and an in-process ledger for one bounded Agent invocation."""

import hashlib
import json
import re
from dataclasses import dataclass
from time import monotonic

from pydantic_ai.messages import RetryPromptPart, TextPart, ToolCallPart
from pydantic_ai.models.wrapper import WrapperModel

from .ollama import AgentLimits

SEARCH = {"search_evidence", "get_evidence", "inspect_neighbor_context"}
WORKFLOW = {"get_verification"}
RECOMPUTE = "request_reverification"

_PARAMETERS = {
    "search_evidence": {"query", "parse_run_id", "source_asset_id"},
    "get_evidence": {"evidence_id", "parse_run_id", "source_asset_id"},
    "inspect_neighbor_context": {"evidence_id", "query"},
    "get_verification": {"workflow_run_id", "declaration_asset_id", "candidate_id"},
    RECOMPUTE: {"workflow_run_id"},
}


@dataclass(frozen=True)
class TaskScope:
    task_id: str
    parse_run_id: str
    source_asset_id: str
    artifact_asset_id: str
    workflow_run_id: str | None = None
    declaration_asset_id: str | None = None
    candidate_ids: tuple[str, ...] = ()
    data_tools: frozenset[str] = frozenset(SEARCH)
    control_tools: frozenset[str] = frozenset()
    physical_pages: tuple[int, int] | None = None

    def audit(self):
        return {
            "parse_run_id": self.parse_run_id,
            "source_asset_id": self.source_asset_id,
            "artifact_asset_id": self.artifact_asset_id,
            "workflow_run_id": self.workflow_run_id,
            "declaration_asset_id": self.declaration_asset_id,
            "candidate_ids": self.candidate_ids,
            "data_tools": sorted(self.data_tools),
            "control_tools": sorted(self.control_tools),
            "physical_pages": self.physical_pages,
        }


class StopTask(Exception):
    pass


def _audit_arguments(args: dict) -> dict:
    audited = {}
    for key, value in args.items():
        if key not in {
            "query",
            "parse_run_id",
            "source_asset_id",
            "evidence_id",
            "workflow_run_id",
            "declaration_asset_id",
            "candidate_id",
        }:
            continue
        if key != "query" and isinstance(value, str) and re.fullmatch("[a-f0-9]{32,64}", value):
            audited[key] = value
        elif isinstance(value, str):
            audited[key] = {
                "chars": len(value),
                "sha256": hashlib.sha256(value.encode("utf-8", errors="replace")).hexdigest(),
            }
    return audited


class Ledger:
    def __init__(self, scope: TaskScope, limits: AgentLimits):
        self.scope = scope
        self.limits = limits
        self.started = monotonic()
        self.attempts = []
        self.events = []
        self.requests = []
        self.discovered = {}
        self.detailed = set()
        self.executions = self.attempt_count = self.bytes = self.rows = self.out_of_scope_reads = 0
        self.no_progress = 0
        self.closed = False
        self.current = []

    def check(self):
        if self.closed or monotonic() - self.started >= self.limits.seconds:
            raise StopTask("task_deadline")

    def denied(self, tool: str, args: dict) -> bool:
        wanted = {
            "source_asset_id": self.scope.source_asset_id,
            "parse_run_id": self.scope.parse_run_id,
            "workflow_run_id": self.scope.workflow_run_id,
            "declaration_asset_id": self.scope.declaration_asset_id,
        }
        return (
            tool not in self.scope.data_tools | self.scope.control_tools
            or tool not in _PARAMETERS
            or not args.keys() <= _PARAMETERS[tool]
            or any(
                value is not None and value != wanted[key]
                for key, value in args.items()
                if key in wanted
            )
            or (
                "candidate_id" in args
                and args["candidate_id"] is not None
                and args["candidate_id"] not in self.scope.candidate_ids
            )
            or (
                tool in ("get_evidence", "inspect_neighbor_context")
                and isinstance(args.get("evidence_id"), str)
                and args["evidence_id"] not in self.discovered
            )
        )

    def accept_batch(self, parts, output_names):
        # Function tool validation runs later. Unexecuted attempts remain visible.
        self.current = []
        calls = [
            part
            for part in parts
            if isinstance(part, ToolCallPart) and part.tool_name not in output_names
        ]
        if not calls:
            return
        self.attempt_count += len(calls)
        over = self.attempt_count > self.limits.attempts
        blocked = False
        for part in calls[: self.limits.attempts + 1]:
            try:
                args = part.args_as_dict()
            except (ValueError, AssertionError, TypeError):
                args = {}
            denied = self.denied(part.tool_name, args)
            blocked |= denied
            event = {
                "tool": part.tool_name if part.tool_name in _PARAMETERS else "unregistered",
                "arguments": _audit_arguments(args),
                "status": "blocked_scope"
                if denied
                else "blocked_budget"
                if over
                else "not_executed",
                "executed": False,
            }
            self.attempts.append(event)
            self.current.append(event)
        if blocked:
            raise StopTask("scope_denied")
        if over:
            raise StopTask("tool_attempt_budget")

    def execute(self, tool, operation):
        self.check()
        attempt = next(
            item for item in self.current if item["tool"] == tool and not item["executed"]
        )
        if self.executions >= self.limits.executions:
            attempt["status"] = "blocked_execution_budget"
            raise StopTask("tool_execution_budget")
        attempt["executed"] = True
        self.executions += 1
        try:
            value, ids, row_count, progress = operation()
            self.check()
        except (StopTask,):
            attempt["status"] = "interrupted"
            raise
        except Exception as error:
            from ira_native.errors import IraError

            if not isinstance(error, (IraError, RuntimeError)):
                raise
            value, ids, row_count, progress = {"code": "tool_error"}, [], 0, False
        encoded = len(json.dumps(value, ensure_ascii=False).encode("utf-8"))
        if encoded + self.bytes > self.limits.bytes or self.rows + row_count > self.limits.rows:
            attempt["status"] = "blocked_output_budget"
            raise StopTask("tool_output_budget")
        self.bytes += encoded
        self.rows += row_count
        status = (
            "tool_error"
            if isinstance(value, dict) and value.get("code") == "tool_error"
            else "ok"
            if progress
            else "no_new_evidence"
        )
        attempt.update(status=status, returned_records=row_count)
        self.events.append({"tool": tool, "evidence_ids": ids, "status": status})
        self.no_progress = 0 if progress else self.no_progress + 1
        if self.no_progress >= 2:
            raise StopTask("no_progress")
        return value

    def remaining(self):
        return {
            "attempts": max(0, self.limits.attempts - self.attempt_count),
            "executions": self.limits.executions - self.executions,
            "requests": self.limits.requests - len(self.requests),
            "tokens": max(
                0,
                self.limits.tokens
                - sum(
                    call.get("input_tokens", 0) + call.get("output_tokens", 0)
                    for call in self.requests
                ),
            ),
            "rows": self.limits.rows - self.rows,
            "bytes": self.limits.bytes - self.bytes,
            "seconds": round(max(0, self.limits.seconds - (monotonic() - self.started)), 3),
        }


class AuditedModel(WrapperModel):
    def __init__(self, wrapped, ledger):
        super().__init__(wrapped)
        self.ledger = ledger

    async def request(self, messages, model_settings, model_request_parameters):
        ledger = self.ledger
        ledger.check()
        started = monotonic()
        call = {
            "index": len(ledger.requests) + 1,
            "status": "started",
            "retry_messages": sum(isinstance(part, RetryPromptPart) for part in messages[-1].parts)
            if messages
            else 0,
        }
        ledger.requests.append(call)
        try:
            response = await super().request(messages, model_settings, model_request_parameters)
        except BaseException as error:
            call["status"] = type(error).__name__
            raise
        finally:
            call["duration_seconds"] = round(monotonic() - started, 3)
        call.update(
            status="completed",
            input_tokens=response.usage.input_tokens,
            output_tokens=response.usage.output_tokens,
            text_parts=sum(isinstance(part, TextPart) for part in response.parts),
        )
        if ledger.remaining()["tokens"] == 0:
            raise StopTask("model_token_budget")
        if response.usage.input_tokens > ledger.limits.context_tokens:
            raise StopTask("model_context_budget")
        ledger.accept_batch(
            response.parts, {tool.name for tool in model_request_parameters.output_tools}
        )
        return response
