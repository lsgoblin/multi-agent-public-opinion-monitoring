import json
from decimal import Decimal
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from riskshield.api import create_app
from riskshield.day3 import (Day3Simulation, DecisionResult, SafeDecisionError,
                             SimulationError)
from tests.fixtures.day3_synthetic_case import CASE_ID, GRAPH_ID, RECORD_ID, prepare_synthetic_case
from riskshield.schemas import AgentDecision, CaseImport
from riskshield.store import Store


ROOT = Path(__file__).resolve().parents[1]
EVENT = ROOT / "data/public/unh_change_20240222_event_v2.json"


def prepared(tmp_path):
    store = Store(tmp_path / "day3.db")
    payload = json.loads(EVENT.read_text(encoding="utf-8"))["case_import_projection"]
    store.import_case(CaseImport.model_validate(payload))
    engine = Day3Simulation(store)
    snapshot = store.snapshot(payload["case_id"])
    source = snapshot["records"][0]
    graph_id = "day3_test_graph"
    body = {
        "case_id": payload["case_id"], "cutoff": snapshot["cutoff"],
        "source_version": snapshot["version"], "nodes": [], "edges": [],
        "claims": [{
            "claim_id": "claim:" + source["record_id"],
            "record_id": source["record_id"], "text": source["summary"],
            "title": source["title"], "source_url": source["source_url"],
            "available_at": source["available_at"],
            "source_node_id": "source:" + source["record_id"],
            "publisher_node_id": "publisher:test", "observation_ids": [],
            "observation_node_ids": [], "evidence_adapters": ["test_fixture"],
        }],
        "excluded_counts": snapshot["excluded_counts"],
    }
    with store.connect() as db:
        db.execute("INSERT INTO graphs VALUES (?, ?, ?, ?, ?, ?)", (
            graph_id, payload["case_id"], snapshot["cutoff"], snapshot["version"],
            "2026-10-05T00:00:00+08:00", json.dumps(body, ensure_ascii=False)))
    return store, engine, payload["case_id"], graph_id, source["record_id"]


class CausalBackend:
    mode = "test_substitute"
    reservation_cny = Decimal("0")
    input_cny_per_million = Decimal("0")
    output_cny_per_million = Decimal("0")
    price_version = "zero-cost-test-v1"

    def __init__(self):
        self.observations = []

    def decide(self, observation):
        self.observations.append(observation)
        if observation["messages"]:
            action = "comment"
            refs = [observation["messages"][0]["message_id"]]
            reason = "A newly received message changed this decision."
        elif observation["memories"]:
            action = "seek_clarification"
            refs = [observation["memories"][0]["memory_id"]]
            reason = "The agent's own prior memory changed this decision."
        elif observation["agent_id"] == "agent_0000":
            action = "share"
            refs = [observation["evidence"][0]["evidence_id"]]
            reason = "Share the currently visible evidence."
        else:
            action = "observe"
            refs = [observation["evidence"][0]["evidence_id"]]
            reason = "Observe until a message or memory becomes available."
        return DecisionResult(AgentDecision(
            agent_id=observation["agent_id"], action=action,
            evidence_ids=refs, reason=reason,
        ), usage={"prompt_tokens": 10, "completion_tokens": 5}, model="causal-test")


class ReactiveScaleBackend:
    mode = "test_substitute"
    reservation_cny = Decimal("0")
    input_cny_per_million = Decimal("0")
    output_cny_per_million = Decimal("0")
    price_version = "zero-cost-scale-test-v1"

    def decide(self, observation):
        if observation["messages"]:
            action = "comment"
            refs = [observation["messages"][0]["message_id"]]
            reason = "React to a newly propagated message."
        elif observation["memories"]:
            action = "seek_clarification"
            refs = [observation["memories"][0]["memory_id"]]
            reason = "Reconsider based on this agent's persisted memory."
        elif observation["persona"]["role"] in {"netizen", "customer"}:
            action = "share" if observation["persona"]["role"] == "netizen" else "comment"
            refs = [observation["evidence"][0]["evidence_id"]]
            reason = "Initial role-specific reaction to visible evidence."
        else:
            action = "observe"
            refs = [observation["evidence"][0]["evidence_id"]]
            reason = "Initial role-specific observation of visible evidence."
        return DecisionResult(AgentDecision(
            agent_id=observation["agent_id"], action=action,
            evidence_ids=refs, reason=reason,
        ), usage={"prompt_tokens": 1, "completion_tokens": 1}, model="scale-test")


def test_existing_message_table_is_migrated_for_content(tmp_path):
    store = Store(tmp_path / "old-message-schema.db")
    with store.connect() as db:
        db.execute("""
            CREATE TABLE simulation_messages (
                run_id TEXT NOT NULL, message_id TEXT NOT NULL, round_number INTEGER NOT NULL,
                sender_id TEXT NOT NULL, recipient_id TEXT NOT NULL, action TEXT NOT NULL,
                evidence_ids TEXT NOT NULL, PRIMARY KEY (run_id, message_id)
            )
        """)
    Day3Simulation(store)
    with store.connect() as db:
        columns = {row["name"] for row in db.execute("PRAGMA table_info(simulation_messages)")}
    assert "content" in columns


def test_messages_and_own_memory_change_behavior_and_survive_restart(tmp_path):
    store, engine, case_id, graph_id, _ = prepared(tmp_path)
    run_id = engine.create_run(case_id, graph_id, agent_count=3, rounds=2, concurrency=3)
    first_backend = CausalBackend()
    first = engine.advance(run_id, first_backend, max_rounds=1)
    assert first["status"] == "running"
    assert [a["decision"]["action"] for a in engine.trajectory(run_id)] == [
        "share", "observe", "observe"]

    # A fresh engine instance proves that state and memory are read from SQLite.
    restarted = Day3Simulation(Store(store.path))
    second_backend = CausalBackend()
    result = restarted.advance(run_id, second_backend)
    assert result["status"] == "complete"
    assert result["actual_participants"] == 3
    round_two = [a for a in restarted.trajectory(run_id) if a["round_number"] == 2]
    assert [a["decision"]["action"] for a in round_two] == [
        "seek_clarification", "comment", "comment"]
    assert round_two[0]["observation_refs"]["memories"]
    assert round_two[1]["observation_refs"]["messages"]

    for observation in second_backend.observations:
        assert all(observation["agent_id"] in m["memory_id"]
                   for m in observation["memories"])

    # A different run starts without messages or memories from the completed run.
    other = restarted.create_run(case_id, graph_id, agent_count=1, rounds=1)
    isolated_backend = CausalBackend()
    restarted.advance(other, isolated_backend)
    assert isolated_backend.observations[0]["messages"] == []
    assert isolated_backend.observations[0]["memories"] == []


def test_runtime_message_treatment_changes_next_round_against_same_seed_control(tmp_path):
    store = Store(tmp_path / "message-control.db")
    engine = Day3Simulation(store)
    prepare_synthetic_case(store)
    case_id, graph_id, source_id = CASE_ID, GRAPH_ID, RECORD_ID

    class ContrastBackend(ReactiveScaleBackend):
        def __init__(self, treatment):
            self.treatment = treatment
            self.observations = []

        def decide(self, obs):
            self.observations.append(obs)
            if obs["round"] == 1:
                action = "share" if self.treatment and obs["agent_id"] == "agent_0000" else "observe"
                reason = ("Runtime sender reports the fictional service interruption."
                          if action == "share" else "No social message is sent in the control run.")
                ref = source_id
            elif obs["agent_id"] == "agent_0001" and obs["messages"]:
                message = obs["messages"][0]
                action = "comment"
                ref = message["message_id"]
                reason = f"Received {ref}; comment to ask whether the fictional report is confirmed."
            else:
                action = "seek_clarification"
                ref = obs["memories"][0]["memory_id"]
                reason = "Own prior observation alone leads to clarification, not a comment."
            return DecisionResult(AgentDecision(
                agent_id=obs["agent_id"], action=action,
                evidence_ids=[ref], reason=reason,
            ), usage={"prompt_tokens": 1, "completion_tokens": 1}, model="contrast-test")

    treatment_backend = ContrastBackend(True)
    treatment_run = engine.create_run(case_id, graph_id, agent_count=2, rounds=2,
                                      concurrency=2, seed=31415)
    engine.advance(treatment_run, treatment_backend)
    control_backend = ContrastBackend(False)
    control_run = engine.create_run(case_id, graph_id, agent_count=2, rounds=2,
                                    concurrency=2, seed=31415)
    engine.advance(control_run, control_backend)

    treatment_round_two = next(o for o in treatment_backend.observations
                               if o["round"] == 2 and o["agent_id"] == "agent_0001")
    control_round_two = next(o for o in control_backend.observations
                             if o["round"] == 2 and o["agent_id"] == "agent_0001")
    message = treatment_round_two["messages"][0]
    assert message == {
        "message_id": "msg_1_agent_0000_agent_0001",
        "sender_id": "agent_0000", "action": "share",
        "evidence_ids": [source_id],
        "content": "Runtime sender reports the fictional service interruption.",
    }
    assert control_round_two["messages"] == []
    treatment_action = next(a for a in engine.trajectory(treatment_run)
                            if a["round_number"] == 2 and a["agent_id"] == "agent_0001")
    control_action = next(a for a in engine.trajectory(control_run)
                          if a["round_number"] == 2 and a["agent_id"] == "agent_0001")
    assert treatment_action["decision"] == {
        "agent_id": "agent_0001", "action": "comment",
        "evidence_ids": [message["message_id"]],
        "reason": ("Received msg_1_agent_0000_agent_0001; comment to ask whether "
                   "the fictional report is confirmed."),
    }
    assert control_action["decision"]["action"] == "seek_clarification"
    treatment_round_one = next(o for o in treatment_backend.observations
                               if o["round"] == 1 and o["agent_id"] == "agent_0001")
    control_round_one = next(o for o in control_backend.observations
                             if o["round"] == 1 and o["agent_id"] == "agent_0001")
    assert treatment_round_one["persona"] == control_round_one["persona"]
    assert treatment_round_one["state"] == control_round_one["state"]
    assert treatment_round_one["evidence"] == control_round_one["evidence"]


def test_future_or_non_input_claim_cannot_enter_a_run(tmp_path):
    store, engine, case_id, _, _ = prepared(tmp_path)
    snapshot = store.snapshot(case_id)
    graph_id = "future_graph"
    body = {
        "case_id": case_id, "cutoff": snapshot["cutoff"],
        "source_version": snapshot["version"], "nodes": [], "edges": [],
        "claims": [{"claim_id": "claim:future", "record_id": "unh-sec-20240308-update",
                    "title": "Future update", "text": "Future result",
                    "available_at": "2024-03-08T12:00:00-05:00"}],
        "excluded_counts": snapshot["excluded_counts"],
    }
    with store.connect() as db:
        db.execute("INSERT INTO graphs VALUES (?, ?, ?, ?, ?, ?)", (
            graph_id, case_id, snapshot["cutoff"], snapshot["version"],
            "2026-10-05T00:00:00+08:00", json.dumps(body)))
    with pytest.raises(SimulationError, match="future material"):
        engine.create_run(case_id, graph_id, agent_count=1, rounds=1)


def test_invalid_private_or_unknown_citation_is_a_visible_failure(tmp_path):
    _, engine, case_id, graph_id, _ = prepared(tmp_path)

    class LeakingBackend(ReactiveScaleBackend):
        def decide(self, observation):
            return DecisionResult(AgentDecision(
                agent_id=observation["agent_id"], action="comment",
                evidence_ids=["private_memory_from_another_agent"], reason="invalid citation"))

    run_id = engine.create_run(case_id, graph_id, agent_count=2, rounds=1)
    result = engine.advance(run_id, LeakingBackend())
    assert result["status"] == "complete"
    assert result["rounds"][0]["failed_decisions"] == 2
    assert result["actual_participants"] == 0
    assert {a["error"] for a in engine.trajectory(run_id)} == {"SimulationError"}


@pytest.mark.parametrize(("error", "expected"), [
    (RuntimeError("private response and key must stay out"), "RuntimeError"),
    (SafeDecisionError("private payload", category="untrusted_secret"),
     "SafeDecisionError"),
    (SafeDecisionError("private payload", category="http_status", http_status=700),
     "SafeDecisionError:http_status"),
])
def test_arbitrary_failure_details_are_not_persisted(tmp_path, error, expected):
    _, engine, case_id, graph_id, _ = prepared(tmp_path)

    class FailingBackend(ReactiveScaleBackend):
        def decide(self, observation):
            raise error

    run_id = engine.create_run(case_id, graph_id, agent_count=1, rounds=1)
    result = engine.advance(run_id, FailingBackend())
    action = engine.trajectory(run_id)[0]
    assert action["error"] == expected
    assert "private" not in json.dumps(action)
    assert result["rounds"][0]["failed_decisions"] == 1


def test_budget_gate_stops_before_dispatch_and_marks_partial(tmp_path):
    _, engine, case_id, graph_id, _ = prepared(tmp_path)

    class CostedBackend(ReactiveScaleBackend):
        mode = "real_model"
        reservation_cny = Decimal("0.20")
        input_cny_per_million = Decimal("1")
        output_cny_per_million = Decimal("1")

    run_id = engine.create_run(case_id, graph_id, agent_count=3, rounds=1,
                               budget_cny=Decimal("0.50"))
    result = engine.advance(run_id, CostedBackend())
    assert result["status"] == "partial"
    assert result["completed_rounds"] == 0
    assert result["actual_participants"] == 0


def test_api_creates_and_reads_a_validated_run(tmp_path):
    store, _, case_id, graph_id, _ = prepared(tmp_path)
    with TestClient(create_app(store.path)) as client:
        created = client.post("/simulations", json={
            "case_id": case_id, "graph_id": graph_id, "agent_count": 5,
            "rounds": 3, "seed": 7, "concurrency": 2, "budget_cny": "30.00",
        })
        assert created.status_code == 201, created.text
        run = created.json()
        assert run["status"] == "created"
        assert run["config"]["agent_count"] == 5
        assert run["config"]["budget_cny"] == "30.00"
        assert client.get(f"/simulations/{run['run_id']}").json()["completed_rounds"] == 0
        assert client.get(f"/simulations/{run['run_id']}/trajectory").json()["actions"] == []
        rejected = client.post("/simulations", json={
            "case_id": case_id, "graph_id": graph_id, "agent_count": 5,
            "rounds": 3, "budget_cny": "30.01",
        })
        assert rejected.status_code == 422
        defaulted = client.post("/simulations", json={
            "case_id": case_id, "graph_id": graph_id, "agent_count": 1,
            "rounds": 1,
        })
        assert defaulted.status_code == 201
        assert defaulted.json()["config"]["budget_cny"] == "5.00"


@pytest.mark.parametrize(("agent_count", "rounds", "concurrency"), [
    (1, 1, 1), (10, 3, 4), (50, 5, 8), (100, 10, 16), (500, 30, 64),
], ids=["1x1", "10x3", "50x5", "100x10", "500x30"])
def test_graded_scale_with_test_substitute(tmp_path, agent_count, rounds, concurrency):
    _, engine, case_id, graph_id, _ = prepared(tmp_path)
    run_id = engine.create_run(case_id, graph_id, agent_count=agent_count, rounds=rounds,
                               concurrency=concurrency, seed=20261005)
    result = engine.advance(run_id, ReactiveScaleBackend())
    assert result["status"] == "complete"
    assert result["completed_rounds"] == rounds
    assert result["actual_participants"] == agent_count
    assert len(result["rounds"]) == rounds
    expected = agent_count * rounds
    assert sum(row["decision_requests"] for row in result["rounds"]) == expected
    assert all(row["model_decision_agents"] == agent_count for row in result["rounds"])
    assert all(row["local_state_update_agents"] == agent_count for row in result["rounds"])
    assert sum(row["valid_decisions"] for row in result["rounds"]) == expected
    assert sum(row["failed_decisions"] for row in result["rounds"]) == 0
    assert all(row["active_decisions"] > 0 for row in result["rounds"])
    assert result["usage"]["prompt_tokens"] == expected
    assert result["usage"]["cached_input_tokens"] == 0
    assert result["usage"]["completion_tokens"] == expected
    assert result["usage"]["unknown_usage_calls"] == 0
    assert result["usage"]["by_model"]["ReactiveScaleBackend"]["calls"] == expected
    assert result["config"]["backend_mode"] == "test_substitute"
