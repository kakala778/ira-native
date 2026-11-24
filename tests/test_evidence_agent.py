import asyncio
import json

import pytest
from pydantic_ai.messages import ModelResponse, ToolCallPart, ToolReturnPart
from pydantic_ai.models.function import FunctionModel
from pydantic_ai.models.test import TestModel

from ira_native.agents.evidence_agent import investigate
from ira_native.application.service import EvidenceService
from ira_native.errors import IraError
from ira_native.settings import Settings


@pytest.fixture
def registered(pair, tmp_path):
    with EvidenceService(Settings(tmp_path / "agent.sqlite", (pair[0].parent,))) as service:
        run = service.ingest_pair(*pair[:2]).parse_run
        yield service, run.parse_run_id


def test_disabled_does_not_read_the_parse_run(monkeypatch):
    monkeypatch.delenv("IRA_ENABLE_LOCAL_AGENT", raising=False)
    result = asyncio.run(investigate(None, "invalid", "missing_evidence"))
    assert result["status"] == "model_disabled"
    assert result["events"] == []


def test_agent_finds_scoped_evidence_and_requests_fixed_reverification(registered, pair):
    service, parse_run_id = registered
    other = pair[1].with_name("other.json")
    changed = pair[2]
    changed["pages"][0]["blocks"][0]["content"] = "OLT SECOND_RUN_ONLY"
    other.write_text(json.dumps(changed), encoding="utf-8")
    service.ingest_pair(pair[0], other)
    calls = []

    def reevaluate():
        calls.append("deterministic")
        return {"disposition": "review_required", "private_context": "sensitive text"}

    def model(messages, info):
        returns = [
            part for msg in messages for part in msg.parts if isinstance(part, ToolReturnPart)
        ]
        if not returns:
            return ModelResponse(parts=[ToolCallPart("search_evidence", {"query": "OLT"})])
        if len(returns) == 1:
            found = returns[0].content
            assert len(found) == 1 and found[0]["parse_run_id"] == parse_run_id
            assert "SECOND_RUN_ONLY" not in str(found)
            return ModelResponse(parts=[ToolCallPart("request_reverification", {})])
        assert "sensitive text" not in str(returns[-1].content)
        return ModelResponse(
            parts=[
                ToolCallPart(
                    info.output_tools[0].name,
                    {"evidence_ids": [returns[0].content[0]["evidence_id"]]},
                )
            ]
        )

    result = asyncio.run(
        investigate(
            service,
            parse_run_id,
            "missing_evidence",
            model=FunctionModel(model),
            reevaluate=reevaluate,
        )
    )
    assert result["status"] == "proposals_available"
    assert result["model_mode"] == "test"
    assert calls == ["deterministic"]
    assert result["reverification"]["disposition"] == "review_required"
    assert len(result["evidence_ids"]) == 1
    assert [event["tool"] for event in result["events"]] == [
        "search_evidence",
        "request_reverification",
    ]
    assert "sensitive text" not in json.dumps(result)
    assert "OLT GPON" not in json.dumps(result)


def test_repeated_no_progress_stops(registered):
    service, parse_run_id = registered

    def model(messages, _info):
        return ModelResponse(parts=[ToolCallPart("search_evidence", {"query": "absent"})])

    result = asyncio.run(
        investigate(service, parse_run_id, "missing_evidence", model=FunctionModel(model))
    )
    assert result["status"] == "no_progress"
    assert len(result["events"]) == 2


def test_out_of_scope_proposal_is_discarded(registered):
    service, parse_run_id = registered

    def model(_messages, info):
        return ModelResponse(
            parts=[ToolCallPart(info.output_tools[0].name, {"evidence_ids": ["0" * 64]})]
        )

    result = asyncio.run(
        investigate(service, parse_run_id, "missing_evidence", model=FunctionModel(model))
    )
    assert result["status"] == "no_progress"
    assert result["evidence_ids"] == []


def test_model_failure_is_sanitized(registered):
    service, parse_run_id = registered

    def model(_messages, _info):
        raise RuntimeError("sensitive model exception")

    result = asyncio.run(
        investigate(service, parse_run_id, "missing_evidence", model=FunctionModel(model))
    )
    assert result["status"] == "model_error"
    assert "sensitive model exception" not in json.dumps(result)


def test_testmodel_cannot_promote_unsupported_evidence(registered):
    service, parse_run_id = registered
    model = TestModel(call_tools=[], custom_output_args={"evidence_ids": ["a" * 64]})
    result = asyncio.run(investigate(service, parse_run_id, "missing_evidence", model=model))
    assert result["status"] == "no_progress"
    assert result["evidence_ids"] == []


def test_batch_over_tool_budget_executes_no_tools(registered):
    service, parse_run_id = registered

    def model(_messages, _info):
        return ModelResponse(
            parts=[
                ToolCallPart("search_evidence", {"query": "OLT"}, tool_call_id=f"batch-{i}")
                for i in range(7)
            ]
        )

    result = asyncio.run(
        investigate(
            service,
            parse_run_id,
            "missing_evidence",
            model=FunctionModel(model),
        )
    )
    assert result["status"] == "agent_budget_exhausted"
    assert result["events"] == []


def test_local_runtime_rejects_cloud_model_alias_before_access(monkeypatch):
    monkeypatch.setenv("IRA_ENABLE_LOCAL_AGENT", "1")
    monkeypatch.setenv("IRA_LOCAL_OLLAMA_MODEL", "qwen3:cloud")
    with pytest.raises(IraError, match="permission_denied"):
        asyncio.run(investigate(None, "invalid", "missing_evidence"))
