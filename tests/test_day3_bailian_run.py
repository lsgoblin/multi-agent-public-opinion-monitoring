import json
from decimal import Decimal

import pytest

import riskshield.day3_bailian_run as runner
from riskshield.day3 import DecisionResult, SimulationError
from riskshield.day3_bailian import (BEIJING_BASE_URL, BailianConfig,
                                     BailianRoute, ROLE_MODELS)
from riskshield.schemas import AgentDecision


def config(api_key="test-only-key"):
    def route(model):
        return BailianRoute(
            model=model, price_version=f"{model}-test-price-v1",
            input_cny_per_million=Decimal("0.3"),
            cached_input_cny_per_million=Decimal("0.06"),
            output_cny_per_million=Decimal("0.6"),
            max_input_tokens=8192, max_output_tokens=160,
        )

    return BailianConfig(
        api_key=api_key, base_url=BEIJING_BASE_URL,
        routes={"qwen-turbo": route("qwen-turbo"),
                "qwen-flash": route("qwen-flash")},
    )


class OfflineRunnerBackend:
    mode = "real_model"

    def __init__(self, configured, *, enable_live, max_calls):
        assert enable_live is True
        self.config = configured
        self.max_calls = max_calls
        self.calls = 0

    def describe(self):
        return {"type": "offline-runner-test", "role_models": ROLE_MODELS}

    def pricing(self, obs):
        return self.config.routes[ROLE_MODELS[obs["persona"]["role"]]].pricing()

    def decide(self, obs):
        self.calls += 1
        if obs["agent_id"] == "agent_0000":
            action = "share"
            refs = [obs["evidence"][0]["evidence_id"]]
        elif obs["messages"]:
            action = "comment"
            refs = [obs["messages"][0]["message_id"]]
        else:
            action = "observe"
            refs = [obs["evidence"][0]["evidence_id"]]
        return DecisionResult(
            AgentDecision(agent_id=obs["agent_id"], action=action,
                          evidence_ids=refs, reason="Synthetic runner audit test."),
            usage={"prompt_tokens": 20, "cached_input_tokens": 4,
                   "completion_tokens": 5},
            model=ROLE_MODELS[obs["persona"]["role"]] + "-test",
        )


def test_runner_budget_cap_is_30_cny():
    assert runner._budget("30.00") == Decimal("30.00")
    with pytest.raises(SimulationError, match="¥30.00"):
        runner._budget("30.01")


def test_runner_requires_explicit_live_flag_and_exact_call_limit(tmp_path):
    common = dict(
        db_path=tmp_path / "run.db", output_path=tmp_path / "run.json",
        env_path=tmp_path / ".env", agent_count=2, rounds=2, concurrency=2,
        max_calls=4, budget_cny="1", role_offset=0, purpose="offline test",
        authorization_label="test-only",
    )
    with pytest.raises(SimulationError, match="enable_live"):
        runner.run_synthetic_bailian(**common)
    with pytest.raises(SimulationError, match=r"agent_count \* rounds"):
        runner.run_synthetic_bailian(**(common | {"enable_live": True, "max_calls": 3}))
    assert not (tmp_path / "run.json").exists()


def test_runner_writes_sanitized_trajectory_and_separate_success_counts(tmp_path,
                                                                        monkeypatch):
    configured = config(api_key="runner-test-secret")
    monkeypatch.setattr(
        runner.BailianConfig, "from_env",
        classmethod(lambda cls, path, require_api_key, required_models: configured),
    )
    monkeypatch.setattr(runner, "BailianBackend", OfflineRunnerBackend)
    output = tmp_path / "result.json"
    report = runner.run_synthetic_bailian(
        db_path=tmp_path / "run.db", output_path=output, env_path=tmp_path / ".env",
        agent_count=2, rounds=2, concurrency=2, max_calls=4,
        budget_cny=Decimal("1"), role_offset=0,
        purpose="synthetic causal preparation",
        authorization_label="test-only-no-network", enable_live=True,
    )
    saved = json.loads(output.read_text(encoding="utf-8"))
    assert report["run_status"] == "complete"
    assert report["completed_rounds"] == 2
    assert report["valid_decisions"] == 4
    assert report["failed_decisions"] == 0
    assert report["completed_without_failures"] is True
    assert report["calls_attempted"] == 4
    assert len(report["trajectory"]) == 4
    assert report["messages"]
    message_id = report["messages"][0]["message_id"]
    receiver_round_two = next(row for row in report["trajectory"]
                              if row["round"] == 2 and row["agent_id"] == "agent_0001")
    assert message_id in receiver_round_two["observation_refs"]["messages"]
    assert "content" not in report["messages"][0]
    assert "runner-test-secret" not in json.dumps(saved)


def test_complete_status_does_not_hide_failed_decisions(tmp_path, monkeypatch):
    class FailingBackend(OfflineRunnerBackend):
        def decide(self, obs):
            self.calls += 1
            raise RuntimeError("provider body must stay suppressed")

    configured = config(api_key="runner-test-secret")
    monkeypatch.setattr(
        runner.BailianConfig, "from_env",
        classmethod(lambda cls, path, require_api_key, required_models: configured),
    )
    monkeypatch.setattr(runner, "BailianBackend", FailingBackend)
    report = runner.run_synthetic_bailian(
        db_path=tmp_path / "failed.db", output_path=tmp_path / "failed.json",
        env_path=tmp_path / ".env", agent_count=1, rounds=1, concurrency=1,
        max_calls=1, budget_cny="1", role_offset=0, purpose="failure accounting",
        authorization_label="test-only-no-network", enable_live=True,
    )
    assert report["run_status"] == "complete"
    assert report["completed_rounds"] == 1
    assert report["valid_decisions"] == 0
    assert report["failed_decisions"] == 1
    assert report["completed_without_failures"] is False
    assert report["trajectory"][0]["error"] == "RuntimeError"
