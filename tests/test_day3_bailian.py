import json
from decimal import Decimal

import pytest

from riskshield.day3 import (Day3Simulation, DecisionPricing, DecisionResult,
                             SimulationError)
from riskshield.day3_bailian import (BEIJING_BASE_URL, BailianBackend,
                                     BailianConfig, BailianRequestError,
                                     BailianRoute, ROLE_MODELS)
from riskshield.day3_deepseek import (CASE_ID, GRAPH_ID, RECORD_ID,
                                      prepare_synthetic_case)
from riskshield.schemas import AgentDecision
from riskshield.store import Store


def route(model, *, input_rate="2", cached_rate="1", output_rate="4",
          max_input=8192, max_output=160):
    return BailianRoute(
        model=model, price_version=f"{model}-test-price-v1",
        input_cny_per_million=Decimal(input_rate),
        cached_input_cny_per_million=Decimal(cached_rate),
        output_cny_per_million=Decimal(output_rate),
        max_input_tokens=max_input, max_output_tokens=max_output,
    )


def config(api_key="test-only-key"):
    return BailianConfig(
        api_key=api_key, base_url=BEIJING_BASE_URL,
        routes={"qwen-turbo": route("qwen-turbo"),
                "qwen-flash": route("qwen-flash", input_rate="1",
                                     cached_rate="0.5", output_rate="2")},
    )


def observation(role="netizen", round_number=1):
    return {
        "run_id": "must-not-leave", "round": round_number,
        "agent_id": "agent_0000",
        "persona": {"role": role, "susceptibility": 0.5, "influence": 0.4},
        "state": {"emotion": 0.1, "trust": 0.6},
        "neighbors": ["must-not-leave"], "cutoff": "must-not-leave",
        "data_mode": "synthetic",
        "evidence": [{"evidence_id": RECORD_ID, "title": "Fictional outage",
                      "text": "A fictional service is temporarily unavailable."}],
        "messages": [{"message_id": "msg_1", "sender_id": "agent_0001",
                      "action": "share", "content": "Fictional message",
                      "evidence_ids": [RECORD_ID]}],
        "memories": [{"memory_id": "mem_1", "content": "Prior fictional reaction"}],
    }


def test_dotenv_loading_and_secret_free_description(tmp_path, monkeypatch):
    names = ["DASHSCOPE_API_KEY", "BAILIAN_API_KEY", "RISKSHIELD_BAILIAN_BASE_URL"]
    names += [f"RISKSHIELD_DAY3_MODEL_{role.upper()}" for role in ROLE_MODELS]
    for prefix in ("TURBO", "FLASH"):
        names += [f"RISKSHIELD_BAILIAN_{prefix}_{suffix}" for suffix in (
            "PRICE_VERSION", "INPUT_CNY_PER_MILLION",
            "CACHED_INPUT_CNY_PER_MILLION", "OUTPUT_CNY_PER_MILLION",
            "MAX_INPUT_TOKENS", "MAX_OUTPUT_TOKENS")]
    for name in names:
        monkeypatch.delenv(name, raising=False)
    dotenv = tmp_path / ".env"
    dotenv.write_text("\n".join([
        "DASHSCOPE_API_KEY=local-secret-value",
        "RISKSHIELD_BAILIAN_TURBO_PRICE_VERSION=test-2026-10-06",
        "RISKSHIELD_BAILIAN_TURBO_INPUT_CNY_PER_MILLION=2",
        "RISKSHIELD_BAILIAN_TURBO_CACHED_INPUT_CNY_PER_MILLION=1",
        "RISKSHIELD_BAILIAN_TURBO_OUTPUT_CNY_PER_MILLION=4",
        "RISKSHIELD_BAILIAN_TURBO_MAX_INPUT_TOKENS=8192",
        "RISKSHIELD_BAILIAN_TURBO_MAX_OUTPUT_TOKENS=160",
        "RISKSHIELD_BAILIAN_FLASH_PRICE_VERSION=test-2026-10-06",
        "RISKSHIELD_BAILIAN_FLASH_INPUT_CNY_PER_MILLION=1",
        "RISKSHIELD_BAILIAN_FLASH_CACHED_INPUT_CNY_PER_MILLION=0.5",
        "RISKSHIELD_BAILIAN_FLASH_OUTPUT_CNY_PER_MILLION=2",
        "RISKSHIELD_BAILIAN_FLASH_MAX_INPUT_TOKENS=8192",
        "RISKSHIELD_BAILIAN_FLASH_MAX_OUTPUT_TOKENS=160",
    ]), encoding="utf-8")
    loaded = BailianConfig.from_env(dotenv, require_api_key=True)
    backend = BailianBackend(loaded)
    assert loaded.api_key == "local-secret-value"
    assert "local-secret-value" not in json.dumps(backend.describe())
    assert backend.pricing(observation("customer")).requested_model == "qwen-turbo"
    assert backend.pricing(observation("media")).requested_model == "qwen-flash"


def test_real_backend_rejects_zero_list_price():
    invalid = config()
    invalid.routes["qwen-turbo"] = route("qwen-turbo", cached_rate="0")
    with pytest.raises(ValueError, match="invalid Bailian route"):
        BailianBackend(invalid)


def test_single_model_probe_only_requires_its_verified_price_config(tmp_path, monkeypatch):
    for prefix in ("TURBO", "FLASH"):
        for suffix in ("PRICE_VERSION", "INPUT_CNY_PER_MILLION",
                       "CACHED_INPUT_CNY_PER_MILLION", "OUTPUT_CNY_PER_MILLION",
                       "MAX_INPUT_TOKENS", "MAX_OUTPUT_TOKENS"):
            monkeypatch.delenv(f"RISKSHIELD_BAILIAN_{prefix}_{suffix}", raising=False)
    monkeypatch.delenv("DASHSCOPE_API_KEY", raising=False)
    monkeypatch.delenv("BAILIAN_API_KEY", raising=False)
    dotenv = tmp_path / ".env"
    dotenv.write_text("\n".join([
        "DASHSCOPE_API_KEY=probe-only-secret",
        "RISKSHIELD_BAILIAN_TURBO_PRICE_VERSION=verified-test-v1",
        "RISKSHIELD_BAILIAN_TURBO_INPUT_CNY_PER_MILLION=0.3",
        "RISKSHIELD_BAILIAN_TURBO_CACHED_INPUT_CNY_PER_MILLION=0.06",
        "RISKSHIELD_BAILIAN_TURBO_OUTPUT_CNY_PER_MILLION=0.6",
        "RISKSHIELD_BAILIAN_TURBO_MAX_INPUT_TOKENS=8192",
        "RISKSHIELD_BAILIAN_TURBO_MAX_OUTPUT_TOKENS=160",
    ]), encoding="utf-8")
    loaded = BailianConfig.from_env(
        dotenv, require_api_key=True, required_models={"qwen-turbo"})
    assert set(loaded.routes) == {"qwen-turbo"}
    assert BailianBackend(loaded).pricing(observation()).requested_model == "qwen-turbo"


def test_live_is_off_by_default_and_payload_is_synthetic_only(monkeypatch):
    disabled = BailianBackend(config())
    with pytest.raises(BailianRequestError, match="disabled"):
        disabled.decide(observation())

    captured = {}

    class Response:
        status_code = 200

        @staticmethod
        def json():
            return {
                "model": "qwen-turbo-actual-test-version",
                "choices": [{"finish_reason": "stop", "message": {"content": json.dumps({
                    "agent_id": "agent_0000", "action": "comment",
                    "evidence_ids": ["msg_1"], "reason": "The new message changed my response.",
                })}}],
                "usage": {"prompt_tokens": 80, "completion_tokens": 20,
                          "total_tokens": 100,
                          "prompt_tokens_details": {"cached_tokens": 30}},
            }

    def fake_post(url, **kwargs):
        captured["url"] = url
        captured.update(kwargs)
        return Response()

    monkeypatch.setattr("riskshield.day3_bailian.httpx.post", fake_post)
    backend = BailianBackend(config(), enable_live=True, max_calls=1)
    result = backend.decide(observation())
    assert result.model == "qwen-turbo-actual-test-version"
    assert result.usage["cached_input_tokens"] == 30
    assert captured["url"] == BEIJING_BASE_URL + "/chat/completions"
    assert captured["follow_redirects"] is False
    payload = captured["json"]
    assert payload["model"] == "qwen-turbo"
    assert payload["enable_thinking"] is False
    assert payload["temperature"] == 0
    fields = json.loads(payload["messages"][1]["content"])
    assert set(fields) == {"agent", "round", "local_state", "fictional_evidence",
                           "received_simulated_messages", "own_simulated_memory",
                           "allowed_citation_ids"}
    serialized = json.dumps(fields)
    assert "must-not-leave" not in serialized
    assert "local-secret-value" not in json.dumps(payload)


def test_saved_1x1_decision_round_two_now_fits_reserved_bound():
    obs = observation(round_number=2)
    obs["persona"].update({"susceptibility": 0.572, "influence": 0.672})
    obs["state"] = {"emotion": 0.167, "trust": 0.569}
    obs["messages"] = []
    obs["evidence"] = [{
        "evidence_id": RECORD_ID,
        "title": "Fictional test service is temporarily unavailable",
        "text": ("In a fictional exercise, Northstar Insurance reports a temporary "
                 "test-service interruption and says technicians are investigating."),
    }]
    obs["memories"] = [{
        "memory_id": "mem_1_agent_0000",
        "content": ("observe The agent is observing the fictional service notice to gather "
                    "information about the temporary test-service interruption reported by "
                    "Northstar Insurance."),
    }]
    payload, upper_bound = BailianBackend(config()).prepare_request(obs)
    fields = json.loads(payload["messages"][1]["content"])
    assert upper_bound == 1244
    assert upper_bound > 1200
    assert upper_bound <= config().routes["qwen-turbo"].max_input_tokens
    assert fields["own_simulated_memory"][0]["memory_id"] == "mem_1_agent_0000"


def test_messages_and_memory_fit_through_round_30(tmp_path):
    adapter = BailianBackend(config())

    class SizingBackend:
        mode = "test_substitute"

        def __init__(self):
            self.prepared = []

        def describe(self):
            return {"type": "bailian-prompt-sizing-test"}

        def pricing(self, obs):
            return adapter.pricing(obs)

        def decide(self, obs):
            payload, upper_bound = adapter.prepare_request(obs)
            fields = json.loads(payload["messages"][1]["content"])
            self.prepared.append((obs["round"], obs["agent_id"], upper_bound, fields))
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
                              evidence_ids=refs,
                              reason="Bounded synthetic cross-round prompt test."),
                usage={"prompt_tokens": 20, "cached_input_tokens": 0,
                       "completion_tokens": 5},
                model=ROLE_MODELS[obs["persona"]["role"]] + "-test",
            )

    _, engine = synthetic_engine(tmp_path)
    backend = SizingBackend()
    run_id = engine.create_run(CASE_ID, GRAPH_ID, agent_count=2, rounds=30,
                               concurrency=2, budget_cny=Decimal("5"))
    result = engine.advance(run_id, backend)
    assert result["status"] == "complete"
    assert result["completed_rounds"] == 30
    round_two = next(item for item in backend.prepared
                     if item[0] == 2 and item[1] == "agent_0001")
    assert len(round_two[3]["received_simulated_messages"]) == 1
    assert len(round_two[3]["own_simulated_memory"]) >= 1
    round_thirty = next(item for item in backend.prepared
                        if item[0] == 30 and item[1] == "agent_0001")
    assert len(round_thirty[3]["received_simulated_messages"]) == 1
    assert len(round_thirty[3]["own_simulated_memory"]) == 3
    assert round_thirty[2] <= config().routes["qwen-turbo"].max_input_tokens


def test_round_30_maximum_retained_fields_fit_conservative_bound():
    obs = observation(round_number=30)
    obs["agent_id"] = "agent_0499"
    obs["evidence"] = [{
        "evidence_id": char * 100, "title": "中" * 100, "text": "中" * 300,
    } for char in ("e", "f")]
    obs["messages"] = [{
        "message_id": char * 100, "sender_id": "agent_0498",
        "action": "express_complaint_intent", "content": "中" * 200,
        "evidence_ids": ["e" * 100, "f" * 100],
    } for char in ("m", "n", "o")]
    obs["memories"] = [{
        "memory_id": char * 100, "content": "中" * 200,
    } for char in ("x", "y", "z")]
    payload, upper_bound = BailianBackend(config()).prepare_request(obs)
    fields = json.loads(payload["messages"][1]["content"])
    assert upper_bound > 7500
    assert len(fields["fictional_evidence"]) == 2
    assert len(fields["received_simulated_messages"]) == 3
    assert len(fields["own_simulated_memory"]) == 3
    assert upper_bound <= config().routes["qwen-turbo"].max_input_tokens


class MixedModelBackend:
    mode = "test_substitute"

    def __init__(self, *, missing_usage=False, fail_agent=None, reservations=None):
        self.missing_usage = missing_usage
        self.fail_agent = fail_agent
        self.calls = 0
        self.reservations = reservations or {
            "qwen-turbo": Decimal("0.00004"),
            "qwen-flash": Decimal("0.00002"),
        }

    def pricing(self, obs):
        model = ROLE_MODELS[obs["persona"]["role"]]
        item = config().routes[model]
        return DecisionPricing(
            requested_model=model, price_version=item.price_version,
            reservation_cny=self.reservations[model],
            input_cny_per_million=item.input_cny_per_million,
            cached_input_cny_per_million=item.cached_input_cny_per_million,
            output_cny_per_million=item.output_cny_per_million,
        )

    def describe(self):
        return {"type": "mixed-test-double", "role_models": ROLE_MODELS}

    def decide(self, obs):
        self.calls += 1
        if obs["agent_id"] == self.fail_agent:
            raise RuntimeError("details must not be persisted")
        return DecisionResult(
            AgentDecision(agent_id=obs["agent_id"], action="observe",
                          evidence_ids=[obs["evidence"][0]["evidence_id"]],
                          reason="Synthetic mixed-model accounting test."),
            usage=None if self.missing_usage else {
                "prompt_tokens": 8, "cached_input_tokens": 3, "completion_tokens": 2},
            model=ROLE_MODELS[obs["persona"]["role"]] + "-actual-test",
        )


def synthetic_engine(tmp_path):
    store = Store(tmp_path / "mixed.db")
    engine = Day3Simulation(store)
    prepare_synthetic_case(store)
    return store, engine


def test_500_agents_keep_fixed_routes_and_account_by_model(tmp_path):
    _, engine = synthetic_engine(tmp_path)
    run_id = engine.create_run(CASE_ID, GRAPH_ID, agent_count=500, rounds=2,
                               concurrency=32, budget_cny=Decimal("5"))
    result = engine.advance(run_id, MixedModelBackend())
    actions = engine.trajectory(run_id)
    assert result["status"] == "complete"
    assert result["usage"]["by_model"]["qwen-turbo"]["calls"] == 400
    assert result["usage"]["by_model"]["qwen-flash"]["calls"] == 600
    assert result["usage"]["cached_input_tokens"] == 3000
    models_by_agent = {}
    for action in actions:
        models_by_agent.setdefault(action["agent_id"], set()).add(action["requested_model"])
        assert action["price_version"].endswith("test-price-v1")
        assert Decimal(action["reserved_cny"]) > 0
    assert len(models_by_agent) == 500
    assert all(len(models) == 1 for models in models_by_agent.values())
    assert sum(models == {"qwen-turbo"} for models in models_by_agent.values()) == 200
    assert sum(models == {"qwen-flash"} for models in models_by_agent.values()) == 300
    assert result["rounds"][0]["reserved_by_model"] == {
        "qwen-turbo": "0.00800", "qwen-flash": "0.00600"}


def test_role_offset_can_isolate_qwen_flash_without_changing_full_distribution(tmp_path):
    _, engine = synthetic_engine(tmp_path)
    run_id = engine.create_run(CASE_ID, GRAPH_ID, agent_count=1, rounds=1,
                               role_offset=2)
    result = engine.advance(run_id, MixedModelBackend())
    action = engine.trajectory(run_id)[0]
    assert result["config"]["role_offset"] == 2
    assert action["requested_model"] == "qwen-flash"


def test_missing_usage_and_failure_keep_model_reservation(tmp_path):
    _, engine = synthetic_engine(tmp_path)
    run_id = engine.create_run(CASE_ID, GRAPH_ID, agent_count=2, rounds=1)
    result = engine.advance(run_id, MixedModelBackend(
        missing_usage=True, fail_agent="agent_0001"))
    actions = engine.trajectory(run_id)
    assert result["usage"]["unknown_usage_calls"] == 2
    assert result["usage"]["by_model"]["qwen-turbo"]["unknown_usage_calls"] == 2
    assert result["cost_cny"] == "0.00008"
    assert actions[0]["status"] == "valid" and actions[0]["usage"] is None
    assert actions[1]["status"] == "failed" and actions[1]["error"] == "RuntimeError"
    assert all(row["cost_cny"] == row["reserved_cny"] for row in actions)


def test_mixed_reservations_share_one_budget_gate(tmp_path):
    _, engine = synthetic_engine(tmp_path)
    backend = MixedModelBackend(reservations={
        "qwen-turbo": Decimal("0.20"), "qwen-flash": Decimal("0.10")})
    run_id = engine.create_run(CASE_ID, GRAPH_ID, agent_count=5, rounds=1,
                               budget_cny=Decimal("0.69"))
    result = engine.advance(run_id, backend)
    assert result["status"] == "partial"
    assert result["completed_rounds"] == 0
    assert backend.calls == 0

    exact_backend = MixedModelBackend(reservations={
        "qwen-turbo": Decimal("0.20"), "qwen-flash": Decimal("0.10")})
    exact_run = engine.create_run(CASE_ID, GRAPH_ID, agent_count=5, rounds=1,
                                  budget_cny=Decimal("0.70"))
    exact = engine.advance(exact_run, exact_backend)
    assert exact["status"] == "complete"
    assert exact_backend.calls == 5


def test_requested_model_cannot_change_after_restart(tmp_path):
    store, engine = synthetic_engine(tmp_path)
    run_id = engine.create_run(CASE_ID, GRAPH_ID, agent_count=1, rounds=2)
    engine.advance(run_id, MixedModelBackend(), max_rounds=1)

    class ChangedBackend(MixedModelBackend):
        def pricing(self, obs):
            quote = super().pricing(obs)
            return DecisionPricing("qwen-flash", quote.price_version, quote.reservation_cny,
                                   quote.input_cny_per_million,
                                   quote.cached_input_cny_per_million,
                                   quote.output_cny_per_million)

    with pytest.raises(SimulationError, match="cannot change"):
        Day3Simulation(Store(store.path)).advance(run_id, ChangedBackend())


def test_old_action_table_is_migrated_and_explicit_insert_still_works(tmp_path):
    store = Store(tmp_path / "old-actions.db")
    with store.connect() as db:
        db.execute("""
            CREATE TABLE simulation_actions (
                run_id TEXT NOT NULL, round_number INTEGER NOT NULL, agent_id TEXT NOT NULL,
                status TEXT NOT NULL, observation_refs TEXT NOT NULL, decision TEXT,
                usage TEXT, cost_cny TEXT NOT NULL, model TEXT, error TEXT,
                PRIMARY KEY (run_id, round_number, agent_id)
            )
        """)
    engine = Day3Simulation(store)
    prepare_synthetic_case(store)
    run_id = engine.create_run(CASE_ID, GRAPH_ID, agent_count=1, rounds=1)
    result = engine.advance(run_id, MixedModelBackend())
    row = engine.trajectory(run_id)[0]
    assert result["status"] == "complete"
    assert row["requested_model"] == "qwen-turbo"
    assert row["reserved_cny"] == "0.00004"
