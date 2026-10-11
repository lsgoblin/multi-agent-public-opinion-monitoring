"""Runtime failure policy and per-round scale ledger contracts.

These tests are entirely local: the substitute backends below never touch a
network or a model. They pin the two facts that a scale run must report
separately -- how many rounds actually ran, and how many decisions were valid.
"""

import json
import threading
import time
from pathlib import Path

import pytest

from riskshield.simulation import Day3Simulation, DecisionResult, SimulationError
from riskshield.schemas import AgentDecision
from riskshield.store import Store
from tests.fixtures.day3_synthetic_case import CASE_ID, GRAPH_ID, prepare_synthetic_case


def make_engine(tmp_path, *, agents, rounds, concurrency, budget="5.00", seed=47):
    store = Store(tmp_path / "ledger.db")
    engine = Day3Simulation(store)
    prepare_synthetic_case(store)
    run_id = engine.create_run(CASE_ID, GRAPH_ID, agent_count=agents, rounds=rounds,
                               concurrency=concurrency, seed=seed, budget_cny=budget)
    return store, engine, run_id


class FailingBackend:
    """Every selected agent fails; all other agents decide normally."""

    mode = "test_substitute"
    reservation_cny = 0
    input_cny_per_million = 0
    output_cny_per_million = 0
    price_version = "failure-policy-v1"
    model = "failure-policy"

    def __init__(self, failing=frozenset()):
        self.failing = set(failing)
        self.calls = []

    def decide(self, observation):
        self.calls.append(observation["agent_id"])
        if observation["agent_id"] in self.failing:
            raise RuntimeError("controlled failure; provider text must not persist")
        reference = (observation["messages"][0]["message_id"] if observation["messages"]
                     else observation["evidence"][0]["evidence_id"])
        return DecisionResult(AgentDecision(
            agent_id=observation["agent_id"], action="observe",
            evidence_ids=[reference], reason="Observed the visible evidence."),
            usage={"prompt_tokens": 2, "completion_tokens": 1}, model=self.model)


def test_default_policy_tolerates_one_failed_decision_and_completes_every_round(tmp_path):
    """One transient failure must not turn a whole multi-round run into `partial`."""
    _, engine, run_id = make_engine(tmp_path, agents=2, rounds=2, concurrency=2)
    backend = FailingBackend(failing={"agent_0000"})
    result = engine.advance(run_id, backend)

    assert result["status"] == "complete"
    assert result["completed_rounds"] == 2
    assert [row["round"] for row in result["rounds"]] == [1, 2]
    assert result["action_counts"] == {"valid": 2, "failed": 2}
    assert result["all_decisions_valid"] is False
    assert result["actual_participants"] == 1
    for row in result["rounds"]:
        assert row["decision_requests"] == 2
        assert row["model_decision_agents"] == 2
        assert row["valid_decisions"] == 1
        assert row["failed_decisions"] == 1
        assert row["not_dispatched_requests"] == 0
        assert row["failure_policy"] == {
            "mode": "continue", "tolerated_failures": 1, "stopped_by_failure": False,
            "failure_categories": {"RuntimeError": 1},
        }
    # The failed agent produced no decision at all; it was never backfilled.
    failed_rows = [a for a in engine.trajectory(run_id) if a["status"] == "failed"]
    assert len(failed_rows) == 2
    assert all(row["decision"] is None for row in failed_rows)
    assert all(row["error"] == "RuntimeError" for row in failed_rows)
    assert "controlled failure" not in json.dumps(result)


def test_explicit_stop_policy_keeps_the_historical_partial_semantics(tmp_path):
    """`stop_on_failure=True` keeps aborting the run, and says which failure did it."""
    _, engine, run_id = make_engine(tmp_path, agents=4, rounds=2, concurrency=1)
    backend = FailingBackend(failing={"agent_0000"})
    result = engine.advance(run_id, backend, stop_on_failure=True)

    assert result["status"] == "partial"
    assert result["completed_rounds"] == 0
    # The abandoned round still records its accounting, but it is not a completed round.
    assert [row["round"] for row in result["rounds"]] == [1]
    assert [row["round_abandoned"] for row in result["rounds"]] == [True]
    assert result["opportunities"]["completed_rounds"] == 0
    assert result["opportunities"]["abandoned_rounds"] == 1
    assert result["opportunities"]["fully_reconciled"] is True
    assert result["opportunities"]["buckets"] == {
        "valid": 0, "failed": 1, "not_dispatched_current_round": 3,
        "not_started_future_rounds": 4}
    # The failed decision stays a failed decision; the rest of the round is stranded.
    assert result["action_counts"] == {"not_dispatched": 3, "failed": 1}
    # Only the first agent was submitted; concurrency 1 prevents a wider drain.
    assert backend.calls == ["agent_0000"]
    with engine.store.connect() as db:
        rows = {row["agent_id"]: (row["status"], row["error"]) for row in db.execute(
            "SELECT agent_id, status, error FROM simulation_actions WHERE run_id=?",
            (run_id,))}
    assert rows["agent_0000"] == ("failed", "RuntimeError")
    assert {status for aid, (status, _) in rows.items() if aid != "agent_0000"} == {
        "not_dispatched"}
    assert {error for aid, (_, error) in rows.items() if aid != "agent_0000"} == {
        "StoppedAfterFailure"}


def test_failure_policy_argument_is_validated(tmp_path):
    _, engine, run_id = make_engine(tmp_path, agents=1, rounds=1, concurrency=1)
    with pytest.raises(SimulationError, match="not both"):
        engine.advance(run_id, FailingBackend(), failure_policy="stop",
                       stop_on_failure=True)
    with pytest.raises(SimulationError, match="must be 'stop' or 'continue'"):
        engine.advance(run_id, FailingBackend(), failure_policy="retry")


def test_undispatched_ledger_writes_every_opportunity_in_one_transaction(tmp_path):
    """A stopped round must still account for all its opportunities exactly once."""
    _, engine, run_id = make_engine(tmp_path, agents=40, rounds=1, concurrency=8,
                                    budget="1.00")

    class Costed(FailingBackend):
        mode = "real_model"
        reservation_cny = 0.20
        input_cny_per_million = 1
        cached_input_cny_per_million = 1
        output_cny_per_million = 1

    result = engine.advance(run_id, Costed(), collect_performance=True)
    assert result["status"] == "partial"
    assert result["completed_rounds"] == 0
    assert result["action_counts"] == {"not_dispatched": 40}
    assert len(engine.trajectory(run_id)) == 40
    assert {a["status"] for a in engine.trajectory(run_id)} == {"not_dispatched"}
    assert result["performance"]["not_dispatched"] == 40
    assert result["performance"]["completed_requests"] == 0
    assert result["cost_cny"] == "0"
    with engine.store.connect() as db:
        assert db.execute(
            "SELECT COUNT(*) FROM simulation_request_metrics WHERE run_id=?",
            (run_id,)).fetchone()[0] == 40


def test_round_ledger_records_opportunities_that_are_not_decisions(tmp_path):
    """Request opportunities, dispatched requests and valid decisions stay distinct."""
    _, engine, run_id = make_engine(tmp_path, agents=5, rounds=2, concurrency=4)
    result = engine.advance(run_id, FailingBackend(failing={"agent_0003"}))

    for row in result["rounds"]:
        assert row["decision_requests"] == row["configured_agents"] == 5
        assert row["dispatched_requests"] == 5
        assert row["model_decision_agents"] == 5
        assert row["failed_decisions"] == 1
        assert row["valid_decisions"] == 4
        assert row["valid_decisions"] + row["failed_decisions"] == (
            row["decision_requests"] - row["not_dispatched_requests"])
        assert row["local_state_update_agents"] == 5
        assert row["elapsed_seconds"] > 0
    # Failed decisions reserve cost but contribute no usage.
    assert result["usage"]["by_model"]["failure-policy"]["calls"] == 10
    assert result["usage"]["unknown_usage_calls"] == 2
    assert result["usage"]["prompt_tokens"] == 16
    assert result["all_decisions_valid"] is False


@pytest.mark.parametrize("agents,rounds,concurrency", [(25, 4, 8)])
def test_graded_scale_ledger_is_reproducible_for_the_same_seed(
        tmp_path, agents, rounds, concurrency):
    """The same seed and configuration must give the same per-round ledger."""
    def run(store_dir):
        store = Store(Path(store_dir) / "repeat.db")
        engine = Day3Simulation(store)
        prepare_synthetic_case(store)
        run_id = engine.create_run(CASE_ID, GRAPH_ID, agent_count=agents, rounds=rounds,
                                   concurrency=concurrency, seed=20261011)
        result = engine.advance(run_id, DelayedBackend(), collect_performance=True)
        return result

    first = run(tmp_path / "a")
    second = run(tmp_path / "b")
    assert first["status"] == second["status"] == "complete"
    assert first["action_counts"] == second["action_counts"] == {
        "valid": agents * rounds}
    assert first["all_decisions_valid"] is True
    keys = ("decision_requests", "dispatched_requests", "valid_decisions",
            "failed_decisions", "not_dispatched_requests", "messages",
            "failure_policy")
    assert [{k: row[k] for k in keys} for row in first["rounds"]] == [
        {k: row[k] for k in keys} for row in second["rounds"]]


class DelayedBackend:
    """Controlled-latency substitute that also reports whether the pool really overlapped."""

    mode = "test_substitute"
    reservation_cny = 0
    input_cny_per_million = 0
    output_cny_per_million = 0
    price_version = "stability-v1"
    model = "stability"

    def __init__(self, delay=0.01):
        self.delay = delay
        self._lock = threading.Lock()
        self.in_flight = 0
        self.peak_in_flight = 0
        self.calls = 0

    def decide(self, observation):
        with self._lock:
            self.calls += 1
            self.in_flight += 1
            self.peak_in_flight = max(self.peak_in_flight, self.in_flight)
        try:
            time.sleep(self.delay)
            reference = (observation["messages"][0]["message_id"]
                         if observation["messages"] else
                         observation["evidence"][0]["evidence_id"])
            return DecisionResult(AgentDecision(
                agent_id=observation["agent_id"], action="observe",
                evidence_ids=[reference], reason="Stability probe decision."),
                usage={"prompt_tokens": 1, "completion_tokens": 1},
                model=self.model)
        finally:
            with self._lock:
                self.in_flight -= 1


@pytest.mark.parametrize(("agents", "rounds", "concurrency"), [
    (20, 3, 4), (20, 3, 8), (40, 6, 10), (60, 10, 16),
])
def test_small_concurrency_is_stable_and_fully_accounted(
        tmp_path, agents, rounds, concurrency):
    """Repeatable small-scale concurrency runs that complete every round."""
    store, engine, run_id = make_engine(tmp_path, agents=agents, rounds=rounds,
                                        concurrency=concurrency, seed=991)
    backend = DelayedBackend()
    result = engine.advance(run_id, backend, collect_performance=True)

    assert result["status"] == "complete"
    assert result["completed_rounds"] == rounds
    assert result["all_decisions_valid"] is True
    assert result["action_counts"] == {"valid": agents * rounds}
    assert backend.calls == agents * rounds
    assert backend.peak_in_flight == concurrency
    assert result["performance"]["maximum_observed_in_flight"] == concurrency
    assert result["performance"]["completed_requests"] == agents * rounds
    assert result["performance"]["not_dispatched"] == 0
    assert result["performance"]["failed_dispatched_requests"] == 0
    assert result["cost_cny"] == "0"
    assert result["usage"]["unknown_usage_calls"] == 0
    assert len(result["rounds"]) == rounds
    for row in result["rounds"]:
        # Every opportunity of every round is accounted for in exactly one bucket.
        assert row["valid_decisions"] + row["failed_decisions"] + (
            row["not_dispatched_requests"]) == row["decision_requests"] == agents
        assert row["valid_decisions"] == agents
        assert row["failure_policy"]["tolerated_failures"] == 0
        assert row["failure_policy"]["stopped_by_failure"] is False
        assert row["elapsed_seconds"] > 0
    with engine.store.connect() as db:
        stored = {row["status"]: row["n"] for row in db.execute(
            "SELECT status, COUNT(*) AS n FROM simulation_actions WHERE run_id=? "
            "GROUP BY status", (run_id,))}
    assert stored == {"valid": agents * rounds}
