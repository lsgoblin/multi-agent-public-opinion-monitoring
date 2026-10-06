import json

import httpx
import pytest

from riskshield.day3_deepseek import (
    AUTHORIZED_BUDGET_CNY,
    Day3DeepSeekBackend,
    DeepSeekSimulationError,
    MAX_CALLS,
    run_synthetic_validation,
)


def observation(data_mode="synthetic"):
    return {
        "run_id": "must-not-leave", "round": 1, "agent_id": "agent_0000",
        "persona": {"role": "netizen", "focus": "unused",
                    "susceptibility": 0.5, "influence": 0.6},
        "state": {"emotion": -0.1, "trust": 0.5},
        "neighbors": ["must-not-leave"],
        "evidence": [{"evidence_id": "fictional-service-notice",
                      "title": "Fictional notice", "text": "A fictional service is unavailable.",
                      "available_at": "2026-01-01T10:00:00+08:00"}],
        "messages": [], "memories": [], "cutoff": "must-not-leave",
        "data_mode": data_mode,
    }


def test_backend_rejects_non_synthetic_observation_before_network(monkeypatch):
    monkeypatch.setattr(httpx, "post", lambda *_args, **_kwargs: pytest.fail("network called"))
    backend = Day3DeepSeekBackend("test-key")
    with pytest.raises(DeepSeekSimulationError, match="synthetic input only"):
        backend.decide(observation("real_historical"))
    assert backend.calls == 0


def test_sanitized_payload_and_usage_contract(monkeypatch):
    captured = []

    def post(_url, **kwargs):
        captured.append(kwargs["json"])
        return httpx.Response(200, json={
            "model": "deepseek-flash",
            "choices": [{"finish_reason": "stop", "message": {"content": json.dumps({
                "agent_id": "agent_0000", "action": "observe",
                "evidence_ids": ["fictional-service-notice"], "reason": "Fictional evidence only.",
            })}}],
            "usage": {"prompt_tokens": 500, "completion_tokens": 20, "total_tokens": 520},
        })

    monkeypatch.setattr(httpx, "post", post)
    backend = Day3DeepSeekBackend("test-key")
    outbound_observation = observation()
    outbound_observation["messages"] = [{
        "message_id": "msg_1_agent_0009_agent_0000",
        "sender_id": "agent_0009", "action": "share",
        "content": "Runtime fictional service report.",
        "evidence_ids": ["fictional-service-notice"],
    }]
    result = backend.decide(outbound_observation)
    assert result.decision.agent_id == "agent_0000"
    assert backend.provider_usage == {"prompt_tokens": 500,
                                      "completion_tokens": 20, "total_tokens": 520}
    payload = captured[0]
    assert payload["thinking"] == {"type": "disabled"}
    assert payload["max_tokens"] == 160
    outbound = json.loads(payload["messages"][1]["content"])
    assert set(outbound) == {"agent", "round", "local_state", "fictional_evidence",
                             "received_simulated_messages", "own_simulated_memory",
                             "allowed_citation_ids"}
    serialized = json.dumps(outbound).lower()
    assert "run_id" not in serialized
    assert "cutoff" not in serialized
    assert "neighbor" not in serialized
    assert outbound["received_simulated_messages"] == [{
        "message_id": "msg_1_agent_0009_agent_0000",
        "sender_id": "agent_0009", "action": "share",
        "content": "Runtime fictional service report.",
        "evidence_ids": ["fictional-service-notice"],
    }]


def test_full_10x3_validation_obeys_call_and_budget_boundaries(tmp_path, monkeypatch):
    payloads = []

    def post(_url, **kwargs):
        payload = kwargs["json"]
        fields = json.loads(payload["messages"][1]["content"])
        payloads.append(fields)
        citation = fields["allowed_citation_ids"][0]
        if fields["received_simulated_messages"]:
            action = "comment"
        elif fields["own_simulated_memory"]:
            action = "seek_clarification"
        else:
            action = "share"
        return httpx.Response(200, json={
            "model": "deepseek-flash",
            "choices": [{"finish_reason": "stop", "message": {"content": json.dumps({
                "agent_id": fields["agent"]["agent_id"], "action": action,
                "evidence_ids": [citation], "reason": "Synthetic runtime choice.",
            })}}],
            "usage": {"prompt_tokens": 400, "completion_tokens": 30, "total_tokens": 430},
        })

    monkeypatch.setattr(httpx, "post", post)
    result = run_synthetic_validation(tmp_path / "synthetic-model.db", api_key="test-key")
    assert result["calls_attempted"] == MAX_CALLS == 30
    assert len(payloads) == 30
    assert result["run"]["status"] == "complete"
    assert result["run"]["actual_participants"] == 10
    assert result["run"]["completed_rounds"] == 3
    assert result["provider_usage"] == {"prompt_tokens": 12_000,
                                        "completion_tokens": 900, "total_tokens": 12_900}
    assert float(result["provider_estimated_peak_cny"]) <= float(AUTHORIZED_BUDGET_CNY)
    outbound_text = json.dumps(payloads, ensure_ascii=False).lower()
    assert "unitedhealth" not in outbound_text
    assert "optum" not in outbound_text
    assert "sec.gov" not in outbound_text
    assert "must-not-leave" not in outbound_text
