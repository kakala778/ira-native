"""Planner-level regression for RET-001 K1."""

import asyncio
from dataclasses import replace

import pytest
from pydantic_ai.messages import ModelResponse, ToolCallPart
from pydantic_ai.models.function import FunctionModel

from ira_native.agents.agent_policy import TaskScope
from ira_native.agents.evidence_agent import investigate
from ira_native.application.retrieval import (
    _model_in,
    _plan,
    make_scope,
    search_k1,
    should_decompose,
)
from ira_native.application.service import EvidenceService
from ira_native.errors import IraError
from ira_native.settings import Settings


def test_structured_queries_are_decomposed_but_literal_queries_are_not():
    assert should_decompose("NX-4800 最大功耗")
    assert should_decompose("NX-4800 与 NX-4800S 的 GPON 端口数")
    assert should_decompose("表 7-1 最大功耗")
    assert should_decompose("5.3.1 接收灵敏度")
    assert not should_decompose("OLT")
    assert not should_decompose("10G SFP+")


@pytest.mark.parametrize(
    ("query", "expected"),
    [
        ("ON-240G 4 个 GE 电口", True),
        ("NX-4800S 2个 SFP+ 光口", True),
        ("SS-320 8网口", True),
        ("4 个 GE 电口", False),
        ("NX-4800 是否支持 25G PON", False),
        ("NX-4800 是否支持 25G接口", False),
    ],
)
def test_numbered_interface_clue_requires_a_recognized_model(query, expected):
    assert should_decompose(query) is expected


def test_model_boundaries_do_not_equate_nx_4800_and_nx_4800s():
    assert _model_in("NX-4800S 最大功耗 150 W", "NX-4800S")
    assert not _model_in("NX-4800S 最大功耗 150 W", "NX-4800")
    assert not _model_in("NX-4800 最大功耗 210 W", "NX-4800S")
    assert _plan("NX-4800S 最大功耗")["models"] == ["NX-4800S"]
    assert _plan("NX-4800 与 NX-4800S 的端口数")["models"] == ["NX-4800", "NX-4800S"]


def test_chinese_and_english_attributes_are_mapped_without_model_aliases():
    assert [item.canonical for item in _plan("NX-4800 power consumption")["attributes"]] == [
        "最大功耗"
    ]
    assert [item.canonical for item in _plan("NX-4800 耗电多少")["attributes"]] == ["最大功耗"]
    assert _plan("NX4800 最大功耗")["models"] == []


def test_decomposed_search_validates_the_original_query(pair, tmp_path):
    with EvidenceService(Settings(tmp_path / "retrieval.sqlite", (pair[0].parent,))) as service:
        run = service.ingest_pair(*pair[:2]).parse_run
        scope = make_scope(service, run.parse_run_id)
        with pytest.raises(IraError, match="invalid_input"):
            search_k1(service, scope, "NX-4800\0 最大功耗")
        with pytest.raises(PermissionError, match="scope_mismatch"):
            search_k1(service, replace(scope, artifact_asset_id="a" * 64), "NX-4800 最大功耗")


@pytest.mark.parametrize(
    "field", ["parse_run_id", "source_asset_id", "artifact_asset_id", "physical_page"]
)
def test_decomposed_tool_blocks_malformed_backend_results(pair, tmp_path, monkeypatch, field):
    with EvidenceService(Settings(tmp_path / "retrieval.sqlite", (pair[0].parent,))) as service:
        run = service.ingest_pair(*pair[:2]).parse_run
        response = service.search_evidence("OLT", parse_run_id=run.parse_run_id)
        value = 3 if field == "physical_page" else "a" * 64
        outside = response.items[0].model_copy(update={field: value})
        malformed = response.model_copy(update={"items": [outside]})
        monkeypatch.setattr(service, "search_evidence", lambda *_args, **_kwargs: malformed)
        scope = TaskScope(
            "synthetic",
            run.parse_run_id,
            run.source_asset_id,
            run.artifact_asset_id,
            physical_pages=(1, 1),
        )

        def model(_messages, _info):
            return ModelResponse(
                parts=[ToolCallPart("search_evidence", {"query": "NX-4800 最大功耗"})]
            )

        result = asyncio.run(
            investigate(
                service,
                run.parse_run_id,
                "synthetic",
                scope=scope,
                model=FunctionModel(model),
            )
        )
        assert result["status"] == "scope_denied"
        assert result["stop_reason"] == "scope_invariant_failed"
        assert result["security"]["out_of_scope_reads"] == 1
        assert result["evidence_ids"] == result["discovered_evidence_ids"] == []
