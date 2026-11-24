"""Opt-in local-model acceptance. Inputs are entirely synthetic."""

import asyncio
import os

import pytest

from ira_native.agents.evidence_agent import investigate
from ira_native.application.service import EvidenceService
from ira_native.settings import Settings

pytestmark = pytest.mark.skipif(
    os.getenv("IRA_RUN_LIVE_AGENT_TESTS") != "1",
    reason="requires an explicitly configured local tool-capable model",
)


@pytest.mark.parametrize("query", ["GPON", "OLT", "ONU", "GE(0)", "设备清单"])
def test_real_model_uses_evidence_tool(pair, tmp_path, query):
    with EvidenceService(Settings(tmp_path / "live.sqlite", (pair[0].parent,))) as service:
        run = service.ingest_pair(*pair[:2]).parse_run
        outcome = asyncio.run(
            investigate(
                service,
                run.parse_run_id,
                f"Find relevant evidence for the literal identifier {query}. Use search_evidence. /no_think",
            )
        )
        assert outcome["status"] == "proposals_available", outcome
        assert outcome["model_mode"] != "test"
        assert any(event["tool"] == "search_evidence" for event in outcome["events"])
        assert outcome["evidence_ids"]
        assert all(
            service.get_evidence(key).parse_run_id == run.parse_run_id
            for key in outcome["evidence_ids"]
        )
        assert any(
            query.casefold() in str(service.get_evidence(key).raw_content).casefold()
            for key in outcome["evidence_ids"]
        )
