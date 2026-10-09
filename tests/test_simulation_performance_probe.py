import copy
import threading
import time
from collections import Counter

import pytest

from riskshield.simulation import Day3Simulation, DecisionResult, SafeDecisionError
from riskshield.schemas import AgentDecision
from riskshield.store import Store
from tests.fixtures.day3_synthetic_case import CASE_ID, GRAPH_ID, prepare_synthetic_case


class DelayedBackend:
    mode = "test_substitute"
    model = "controlled-delay-test"
    price_version = "controlled-delay-v1"
    reservation_cny = 0
    input_cny_per_million = 0
    output_cny_per_million = 0

    def __init__(self, delay=0.01, stop_event=None, stop_after=None, fail_agent=None):
        self.delay = delay
        self.stop_event = stop_event
        self.stop_after = stop_after
        self.fail_agent = fail_agent
        self.lock = threading.Lock()
        self.in_flight = 0
        self.max_in_flight = 0
        self.calls = []
        self.observations = []
        self.call_counts = {}
        self.stop_triggered = False

    def decide(self, observation):
        with self.lock:
            self.in_flight += 1
            self.max_in_flight = max(self.max_in_flight, self.in_flight)
            self.calls.append(observation["agent_id"])
            self.call_counts[observation["agent_id"]] = (
                self.call_counts.get(observation["agent_id"], 0) + 1)
            self.observations.append(copy.deepcopy(observation))
            if (self.stop_event is not None and not self.stop_triggered and
                    len(self.calls) >= self.stop_after):
                self.stop_event.set()
                self.stop_triggered = True
        try:
            time.sleep(self.delay)
            if observation["agent_id"] == self.fail_agent:
                raise RuntimeError("controlled failure; response suppressed")
            if observation["messages"]:
                action, ref = "comment", observation["messages"][0]["message_id"]
                reason = "Respond to the visible simulated message."
            elif observation["memories"]:
                action, ref = "seek_clarification", observation["memories"][0]["memory_id"]
                reason = "Use only this agent's own prior memory."
            elif observation["persona"]["role"] == "netizen":
                action, ref = "share", observation["evidence"][0]["evidence_id"]
                reason = "Share currently visible fictional evidence."
            else:
                action, ref = "observe", observation["evidence"][0]["evidence_id"]
                reason = "Observe currently visible fictional evidence."
            return DecisionResult(AgentDecision(
                agent_id=observation["agent_id"], action=action,
                evidence_ids=[ref], reason=reason,
            ), usage={"prompt_tokens": 3, "completion_tokens": 2}, model=self.model)
        finally:
            with self.lock:
                self.in_flight -= 1


def make_engine(tmp_path, *, agents, rounds, concurrency):
    store = Store(tmp_path / "performance.db")
    engine = Day3Simulation(store)
    prepare_synthetic_case(store)
    run_id = engine.create_run(CASE_ID, GRAPH_ID, agent_count=agents,
                               rounds=rounds, concurrency=concurrency, seed=47)
    return store, engine, run_id


@pytest.mark.parametrize("concurrency", [4, 8])
def test_controlled_latency_observes_configured_concurrency_and_frozen_inputs(
        tmp_path, concurrency):
    store, engine, run_id = make_engine(
        tmp_path, agents=24, rounds=2, concurrency=concurrency)
    frozen_by_round = {}
    original_context = engine._context

    def capture_context(run, agents, round_number, **kwargs):
        observations = original_context(run, agents, round_number, **kwargs)
        frozen_by_round[round_number] = copy.deepcopy(observations)
        return observations

    engine._context = capture_context
    backend = DelayedBackend(delay=0.03)
    result = engine.advance(run_id, backend, collect_performance=True)

    assert result["status"] == "complete"
    assert result["completed_rounds"] == 2
    assert len(backend.calls) == 48
    assert Counter(backend.calls) == {f"agent_{index:04d}": 2 for index in range(24)}
    assert backend.max_in_flight == concurrency
    assert result["performance"]["maximum_observed_in_flight"] == concurrency
    assert result["performance"]["completed_requests"] == 48
    assert result["performance"]["valid_decisions"] == 48
    assert result["performance"]["unknown_usage_calls"] == 0
    assert result["performance"]["request_elapsed_seconds"]["p50"] > 0
    assert result["performance"]["by_model"][backend.model]["valid"] == 48
    assert len(frozen_by_round[1]) == 24
    assert len(frozen_by_round[2]) == 24
    assert all(row["performance"]["dispatched_this_round"] == 24
               for row in result["rounds"])

    with store.connect() as db:
        stored_messages = {row["message_id"]: dict(row) for row in db.execute(
            "SELECT * FROM simulation_messages WHERE run_id=?", (run_id,))}
    for observation in backend.observations:
        round_inputs = frozen_by_round[observation["round"]]
        assert observation in round_inputs
        assert all(observation["agent_id"] in memory["memory_id"]
                   for memory in observation["memories"])
        assert all(memory["round"] < observation["round"]
                   for memory in observation["memories"])
        assert all(message_id.startswith("msg_")
                   for message_id in [m["message_id"] for m in observation["messages"]])
        for message in observation["messages"]:
            stored = stored_messages[message["message_id"]]
            assert stored["recipient_id"] == observation["agent_id"]
            assert stored["round_number"] == observation["round"] - 1
        expected_message_ids = {
            row["message_id"] for row in stored_messages.values()
            if row["recipient_id"] == observation["agent_id"] and
            row["round_number"] == observation["round"] - 1
        }
        assert {message["message_id"] for message in observation["messages"]} == (
            expected_message_ids)

    assert all(row["valid_decisions"] == 24 for row in result["rounds"])
    assert all("performance" in row for row in result["rounds"])


def test_performance_capture_does_not_change_backend_observations(tmp_path):
    store = Store(tmp_path / "performance.db")
    engine = Day3Simulation(store)
    prepare_synthetic_case(store)
    run_without_metrics = engine.create_run(
        CASE_ID, GRAPH_ID, agent_count=10, rounds=2, concurrency=4, seed=47)
    run_with_metrics = engine.create_run(
        CASE_ID, GRAPH_ID, agent_count=10, rounds=2, concurrency=4, seed=47)
    backend_without_metrics = DelayedBackend(delay=0)
    backend_with_metrics = DelayedBackend(delay=0)

    plain = engine.advance(run_without_metrics, backend_without_metrics)
    measured = engine.advance(run_with_metrics, backend_with_metrics,
                              collect_performance=True)

    assert plain["status"] == measured["status"] == "complete"
    assert "performance" not in plain
    assert "performance" in measured
    normalized_plain = [
        {key: value for key, value in observation.items() if key != "run_id"}
        for observation in backend_without_metrics.observations
    ]
    normalized_measured = [
        {key: value for key, value in observation.items() if key != "run_id"}
        for observation in backend_with_metrics.observations
    ]
    assert normalized_plain == normalized_measured


def test_stop_drains_launched_work_and_resume_skips_completed_decisions(tmp_path):
    store, engine, run_id = make_engine(
        tmp_path, agents=12, rounds=1, concurrency=4)
    event = threading.Event()
    backend = DelayedBackend(delay=0.02, stop_event=event, stop_after=1)
    first = engine.advance(run_id, backend, collect_performance=True, stop_event=event)

    assert first["status"] == "partial"
    assert first["completed_rounds"] == 0
    assert 1 <= len(backend.calls) <= 4
    assert first["action_counts"]["not_dispatched"] == 12 - len(backend.calls)
    assert len(backend.calls) == len(set(backend.calls))
    with store.connect() as db:
        assert db.execute("SELECT COUNT(*) FROM simulation_rounds WHERE run_id=?",
                          (run_id,)).fetchone()[0] == 0
        assert db.execute("SELECT COUNT(*) FROM simulation_messages WHERE run_id=?",
                          (run_id,)).fetchone()[0] == 0
        assert db.execute("SELECT COUNT(*) FROM simulation_memories WHERE run_id=?",
                          (run_id,)).fetchone()[0] == 0

    event.clear()
    second = engine.advance(run_id, backend, collect_performance=True, stop_event=event)
    assert second["status"] == "complete"
    assert second["completed_rounds"] == 1
    assert len(backend.calls) == 12
    assert set(backend.call_counts.values()) == {1}
    assert second["performance"]["completed_requests"] == 12
    assert second["action_counts"] == {"valid": 12}


def test_deadline_and_failure_stop_are_distinct_and_do_not_retry_failures(tmp_path):
    _, engine, run_id = make_engine(tmp_path, agents=8, rounds=1, concurrency=4)
    never_called = DelayedBackend()
    stopped = engine.advance(run_id, never_called,
                             deadline_monotonic=time.monotonic() - 1,
                             collect_performance=True)
    assert stopped["status"] == "partial"
    assert stopped["action_counts"] == {"not_dispatched": 8}
    assert never_called.calls == []

    recovering_backend = DelayedBackend(delay=0.005, fail_agent="agent_0000")
    partial = engine.advance(run_id, recovering_backend, stop_on_failure=True,
                             collect_performance=True)
    assert partial["status"] == "partial"
    assert partial["action_counts"]["failed"] == 1
    assert len(recovering_backend.calls) <= 8
    assert "agent_0000" in recovering_backend.calls
    calls_before_resume = len(recovering_backend.calls)
    complete_rounds = engine.advance(run_id, recovering_backend, collect_performance=True)
    assert complete_rounds["status"] == "complete"
    assert complete_rounds["completed_rounds"] == 1
    assert recovering_backend.call_counts["agent_0000"] == 1
    assert len(recovering_backend.calls) > calls_before_resume
    assert complete_rounds["action_counts"]["failed"] == 1
    assert complete_rounds["action_counts"]["valid"] == 7
    assert complete_rounds["all_decisions_valid"] is False


def test_backend_call_limit_is_not_recorded_as_a_sent_or_billed_request(tmp_path):
    from decimal import Decimal

    class LimitedBackend(DelayedBackend):
        mode = "real_model"
        reservation_cny = Decimal("0.01")
        input_cny_per_million = Decimal("1")
        output_cny_per_million = Decimal("1")

        def decide(self, observation):
            with self.lock:
                self.calls.append(observation["agent_id"])
                self.call_counts[observation["agent_id"]] = 1
            raise SafeDecisionError("call cap reached", category="call_limit")

    _, engine, run_id = make_engine(tmp_path, agents=6, rounds=1, concurrency=1)
    backend = LimitedBackend()
    result = engine.advance(run_id, backend, collect_performance=True)
    assert result["status"] == "partial"
    assert result["completed_rounds"] == 0
    assert len(backend.calls) == 1
    assert result["action_counts"] == {"not_dispatched": 6}
    assert result["cost_cny"] == "0"
    assert result["usage"]["unknown_usage_calls"] == 0
    assert result["performance"]["completed_requests"] == 0
    assert result["performance"]["not_dispatched"] == 6
    assert result["performance"]["unknown_usage_calls"] == 0
