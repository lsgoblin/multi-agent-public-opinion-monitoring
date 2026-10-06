import json
from decimal import Decimal

import httpx

from riskshield.day3_budget_probe import (BudgetProbe, RESERVE_CNY,
                                          _initial_agents, _observation, run_probe)
from riskshield.day3_deepseek import PROMPT_VERSION


def test_probe_pre_reserves_and_sends_only_synthetic_fields(monkeypatch):
    sent = []

    def post(_url, **kwargs):
        sent.append(kwargs["json"])
        fields = json.loads(kwargs["json"]["messages"][1]["content"])
        return httpx.Response(200, json={
            "model": "deepseek-flash",
            "choices": [{"finish_reason": "stop", "message": {"content": json.dumps({
                "agent_id": fields["agent"]["agent_id"], "action": "observe",
                "evidence_ids": [fields["allowed_citation_ids"][0]],
                "reason": "Fictional test only.",
            })}}],
            "usage": {"prompt_tokens": 300, "completion_tokens": 30,
                      "total_tokens": 330, "prompt_cache_hit_tokens": 100,
                      "prompt_cache_miss_tokens": 200},
        })

    monkeypatch.setattr(httpx, "post", post)
    probe = BudgetProbe("test-key", budget_cny=RESERVE_CNY)
    obs = _observation(_initial_agents()[0], 1)
    result = probe.call(obs)
    assert result["action"] == "observe"
    assert probe.call(obs) is None
    assert probe.summary()["calls_sent"] == 1
    assert Decimal(probe.summary()["protected_peak_cny"]) <= RESERVE_CNY
    outbound = sent[0]["messages"][1]["content"].lower()
    for forbidden in ("unitedhealth", "optum", "sec.gov", "local-only-budget-probe"):
        assert forbidden not in outbound


def test_unknown_response_consumes_reservation_and_halts(monkeypatch):
    monkeypatch.setattr(httpx, "post", lambda *_args, **_kwargs:
                        httpx.Response(503, text="private provider response"))
    probe = BudgetProbe("test-key")
    obs = _observation(_initial_agents()[0], 1)
    assert probe.call(obs)["error"] == "HTTP503"
    assert probe.call(obs) is None
    summary = probe.summary()
    assert summary["calls_sent"] == 1
    assert summary["protected_peak_cny"] == str(RESERVE_CNY)
    assert summary["failures"] == {"HTTP503": 1}


def test_resume_keeps_previous_spend_and_invalid_decision_count():
    previous = {"purpose": "synthetic_call_capacity_not_G3",
                "budget_cny": "2.36", "prompt_version": PROMPT_VERSION,
                "protected_peak_cny": "2.35", "protected_holiday_cny": "2.35",
                "calls_sent": 100,
                "valid_decisions": 99, "failures": {"ValidationError": 1},
                "actions": {"observe": 99}, "models": {"deepseek-flash": 99},
                "usage": {"total_tokens": 42100}}
    probe = BudgetProbe("test-key", previous=previous)
    assert probe.call(_observation(_initial_agents()[0], 1)) is None
    summary = probe.summary()
    assert summary["calls_sent"] == 100
    assert summary["failures"] == {"ValidationError": 1}
    assert summary["protected_peak_cny"] == "2.35"


def test_probe_finishes_smaller_final_batch_without_repeating_calls(tmp_path, monkeypatch):
    monkeypatch.setattr("riskshield.day3_budget_probe._balance", lambda _key: None)

    def post(_url, **kwargs):
        fields = json.loads(kwargs["json"]["messages"][1]["content"])
        return httpx.Response(200, json={
            "model": "deepseek-flash",
            "choices": [{"finish_reason": "stop", "message": {"content": json.dumps({
                "agent_id": fields["agent"]["agent_id"], "action": "observe",
                "evidence_ids": [fields["allowed_citation_ids"][0]],
                "reason": "Fictional test only.",
            })}}],
            "usage": {"prompt_tokens": 6000, "completion_tokens": 160,
                      "total_tokens": 6160},
        })

    monkeypatch.setattr(httpx, "post", post)
    result = run_probe(tmp_path / "probe.json", api_key="test-key",
                       budget_cny=Decimal("0.023"), enforce_holiday_date=False)
    assert result["calls_sent"] == result["valid_decisions"] == 1
    assert result["rounds_started"] == 1
    assert result["full_10_agent_rounds"] == 0
    assert Decimal(result["protected_peak_cny"]) < Decimal("0.023")
