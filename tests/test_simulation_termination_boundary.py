"""Regression tests for the two problems found in review.

Problem 1 -- a permanently blocked backend kept the whole task from ever returning. The
deadline only gated *new* submissions, and every round waited on its launched requests
through a ``ThreadPoolExecutor`` whose workers cannot be abandoned, so one hung call
blocked the run and the process. The boundary is now a per-round wait budget plus daemon
workers, and a request the engine stopped waiting for is recorded as
``deadline_exceeded``: it was dispatched (so not ``not_dispatched``) and its usage is
unknown (so never free).

Problem 2 -- a run that finished round 1 and then aborted could not produce a report at
all: the report ledger check expected one row per *completed* round, and the abandoned
round now records a row too. The gate now accepts a contiguous completed-round prefix
followed by abandoned rounds, and the report says it is incomplete.

Offline only: the substitutes here never touch a network or a model.
"""

import json
import threading
import time
from decimal import Decimal

import pytest

from riskshield.simulation import Day3Simulation, DecisionResult, SimulationError
from riskshield.schemas import AgentDecision
from riskshield.store import Store
from tests.fixtures.day3_synthetic_case import CASE_ID, GRAPH_ID, prepare_synthetic_case


def make_engine(tmp_path, *, agents, rounds, concurrency, budget="30.00", seed=23):
    store = Store(tmp_path / "regress.db")
    engine = Day3Simulation(store)
    prepare_synthetic_case(store)
    run_id = engine.create_run(CASE_ID, GRAPH_ID, agent_count=agents, rounds=rounds,
                               concurrency=concurrency, seed=seed, budget_cny=budget)
    return store, engine, run_id


class PricedSubstitute:
    """Non-zero simulated prices, so an abandoned request must actually cost something."""

    mode = "real_model"
    reservation_cny = Decimal("0.20")
    input_cny_per_million = Decimal("3.00")
    cached_input_cny_per_million = Decimal("0.60")
    output_cny_per_million = Decimal("6.00")
    price_version = "regression-prices-v1"
    model = "regression-substitute"

    def decide(self, observation):
        reference = observation["evidence"][0]["evidence_id"]
        return DecisionResult(AgentDecision(
            agent_id=observation["agent_id"], action="observe",
            evidence_ids=[reference], reason="recorded substitute decision"),
            usage={"prompt_tokens": 1, "completion_tokens": 1}, model=self.model)


class HangsForever(PricedSubstitute):
    """Blocks on a gate that is never released during the test."""

    def __init__(self, gate, blocked_agents=("agent_0001",)):
        self.gate = gate
        self.blocked_agents = set(blocked_agents)
        self.calls = 0

    def decide(self, observation):
        self.calls += 1
        if observation["agent_id"] in self.blocked_agents:
            self.gate.wait()  # models a socket read with no working timeout
        return super().decide(observation)


def test_permanently_blocked_call_cannot_stop_the_run_from_returning(tmp_path):
    """The task must return within its budget even when a call never comes back."""
    store, engine, run_id = make_engine(tmp_path, agents=4, rounds=2, concurrency=2)
    gate = threading.Event()
    backend = HangsForever(gate)
    started = time.perf_counter()
    result = engine.advance(run_id, backend, collect_performance=True,
                            call_timeout_seconds=0.2)
    elapsed = time.perf_counter() - started

    # Bounded: it returned on its own budget rather than waiting for the hung call.
    assert elapsed < 1.5, elapsed
    assert result["status"] == "partial"
    # Round 1 finished; round 2 exhausted the wait budget on the hung call.
    assert result["completed_rounds"] == 1
    assert result["action_counts"] == {"valid": 3, "deadline_exceeded": 1,
                                       "not_dispatched": 4}
    performance = result["performance"]
    assert performance["invocations"][0]["stop_reason"] == "call_timeout"
    assert performance["deadline_exceeded_requests"] == 1
    assert performance["completed_requests"] == 3

    opportunities = result["opportunities"]
    assert opportunities["fully_reconciled"] is True
    assert opportunities["total_opportunities"] == 8
    # The abandoned round is the last one, so nothing lies beyond it.
    assert opportunities["buckets"] == {
        "valid": 3, "failed": 0, "deadline_exceeded": 1,
        "not_dispatched_current_round": 4, "not_started_future_rounds": 0}

    billing = result["billing"]
    assert billing["cost_reconciles"] is True
    assert billing["states"] == {"not_sent": 4, "released": 0, "settled": 3,
                                 "unsettled_reserved": 1}
    # The abandoned request was dispatched, so it is charged rather than reported free.
    assert Decimal(billing["unsettled_reserved_cost_cny"]) == Decimal("0.20")

    # The hung worker must not be able to keep the process alive; releasing it lets this
    # test finish cleanly.
    gate.set()


def test_abandoned_request_is_recorded_distinctly_from_an_unsent_one(tmp_path):
    """`deadline_exceeded` and `not_dispatched` must not be conflated."""
    store, engine, run_id = make_engine(tmp_path, agents=2, rounds=1, concurrency=1)
    gate = threading.Event()
    backend = HangsForever(gate, blocked_agents=("agent_0000",))
    result = engine.advance(run_id, backend, collect_performance=True,
                            call_timeout_seconds=0.15)
    gate.set()

    with store.connect() as db:
        rows = {row["agent_id"]: row["status"] for row in db.execute(
            "SELECT agent_id, status FROM simulation_actions WHERE run_id=?", (run_id,))}
    assert rows["agent_0000"] == "deadline_exceeded"
    assert rows["agent_0001"] == "not_dispatched"
    # The abandoned row keeps its dispatch evidence and its reservation.
    with store.connect() as db:
        abandoned = db.execute(
            "SELECT status, reserved_cny, cost_cny, error FROM simulation_actions "
            "WHERE run_id=? AND agent_id='agent_0000'", (run_id,)).fetchone()
    assert abandoned["status"] == "deadline_exceeded"
    assert Decimal(abandoned["reserved_cny"]) == Decimal("0.20")
    assert Decimal(abandoned["cost_cny"]) == Decimal("0.20")
    assert abandoned["error"] == "CallTimeout"
    assert result["billing"]["states"]["unsettled_reserved"] == 1


def test_call_timeout_must_be_a_finite_positive_number(tmp_path):
    _, engine, run_id = make_engine(tmp_path, agents=1, rounds=1, concurrency=1)
    backend = PricedSubstitute()
    for bad in (0, -1, float("inf"), float("nan"), True):
        with pytest.raises(SimulationError, match="call_timeout_seconds"):
            engine.advance(run_id, backend, call_timeout_seconds=bad)


@pytest.mark.parametrize(("deadline_ms", "budget_ms", "expected_ms"), [
    (100.0, 500.0, 100.0),   # the task deadline is earlier and must win
    (500.0, 100.0, 100.0),   # the per-round budget is earlier and must win
    (100.0, 100.0, 100.0),   # equal
])
def test_combined_limits_obey_the_earlier_one(tmp_path, deadline_ms, budget_ms,
                                             expected_ms):
    """A larger per-round budget must never extend the task deadline.

    Regression: the budget helper returned ``call_timeout_seconds`` immediately whenever
    it was set, so a task deadline of 0.1 s combined with a per-round budget of 0.5 s
    returned after ~0.54 s instead of ~0.1 s.
    """
    _, engine, run_id = make_engine(tmp_path, agents=20, rounds=3, concurrency=4)
    gate = threading.Event()
    backend = HangsForever(gate, blocked_agents=tuple(
        f"agent_{index:04d}" for index in range(20)))
    started = time.perf_counter()
    result = engine.advance(run_id, backend, collect_performance=True,
                            deadline_monotonic=time.monotonic() + deadline_ms / 1000.0,
                            call_timeout_seconds=budget_ms / 1000.0)
    elapsed_ms = (time.perf_counter() - started) * 1000.0
    gate.set()

    # It must come back on the earlier limit, with an allowance for bookkeeping.
    assert elapsed_ms < expected_ms + 250.0, (elapsed_ms, expected_ms)
    # When the two limits differ, the stricter one must not have been silently replaced
    # by the looser one.
    if deadline_ms != budget_ms:
        assert elapsed_ms < max(deadline_ms, budget_ms) - 50.0, (elapsed_ms, deadline_ms,
                                                                budget_ms)
    assert result["status"] == "partial"
    assert result["opportunities"]["fully_reconciled"] is True
    assert result["billing"]["cost_reconciles"] is True
    assert set(result["opportunities"]["buckets"]) == {
        "valid", "failed", "deadline_exceeded", "not_dispatched_current_round",
        "not_started_future_rounds"}
    # Only the first batch was dispatched; nothing new was submitted after the stop.
    assert result["action_counts"].get("deadline_exceeded", 0) == 4
    assert result["performance"]["invocations"][0]["stop_reason"] in {
        "deadline", "call_timeout"}


def test_completed_round_then_abandoned_round_still_produces_a_report(tmp_path):
    """Repro of the report failure: round 1 completes, round 2 is abandoned."""
    from riskshield.alerts import Day4Alerts
    from riskshield.reporting import Day4Reports

    store, engine, run_id = make_engine(tmp_path, agents=4, rounds=3, concurrency=1)

    class FailsRound2(PricedSubstitute):
        def decide(self, observation):
            if observation["round"] >= 2:
                raise RuntimeError("controlled round-2 failure")
            return super().decide(observation)

    result = engine.advance(run_id, FailsRound2(), collect_performance=True,
                            stop_on_failure=True)
    assert result["status"] == "partial"
    assert result["completed_rounds"] == 1
    assert [row.get("round_abandoned") for row in result["rounds"]] == [False, True]

    # Before the fix this raised ReportError("Run ledger is incomplete or inconsistent").
    report = Day4Reports(store).build(run_id)
    integrity = report["overview"]["run_integrity"]
    assert report["run_status"] == "partial"
    assert integrity["status"] == "partial"
    assert integrity["completed_rounds"] == 1
    assert integrity["configured_rounds"] == 3
    assert integrity["abandoned_rounds"] == [2]
    assert integrity["complete"] is False
    assert integrity["integrity_label"] == "不完整：存在失败决策或中止轮次"
    assert integrity["failed_decisions"] == 1
    assert integrity["deadline_exceeded_decisions"] == 0
    assert integrity["requested_decisions"] == 8
    assert integrity["valid_decisions"] == 7
    # The abandoned round is reported as abandoned, not as a completed round.
    distributions = report["modules"]["emotion_evolution"]["rounds"]
    flags = {row["round"]: row["round_abandoned"] for row in distributions}
    assert flags == {1: False, 2: True}


def test_an_incomplete_run_is_never_marked_as_a_success_result(tmp_path):
    """The alert may exist, but it must not be readable as a successful assessment."""
    from riskshield.alerts import Day4Alerts
    from riskshield.reporting import Day4Reports

    store, engine, run_id = make_engine(tmp_path, agents=4, rounds=3, concurrency=1)

    class FailsRound2(PricedSubstitute):
        def decide(self, observation):
            if observation["round"] >= 2:
                raise RuntimeError("controlled round-2 failure")
            return super().decide(observation)

    engine.advance(run_id, FailsRound2(), collect_performance=True, stop_on_failure=True)
    alert = Day4Alerts(store).assess(run_id)
    assert alert["level"] in {"red", "orange", "yellow", "blue"}
    assert alert["run_complete"] is False
    assert alert["decisions_all_valid"] is False
    assert alert["alert_is_success_result"] is False
    assert any("incomplete" in item.lower() for item in alert["limitations"])

    # A clean run, by contrast, is marked as complete.
    store2, engine2, run_id2 = make_engine(tmp_path, agents=4, rounds=2, concurrency=2,
                                           seed=99)
    engine2.advance(run_id2, PricedSubstitute(), collect_performance=True)
    clean = Day4Alerts(store2).assess(run_id2)
    assert clean["run_complete"] is True
    assert clean["decisions_all_valid"] is True
    assert clean["alert_is_success_result"] is True
    clean_report = Day4Reports(store2).build(run_id2, clean)
    assert clean_report["overview"]["run_integrity"]["integrity_label"] == (
        "账本完整（不代表结果真实或关口通过）")


# ------------------------------------------------------- residue and late answers

class LateResponder(PricedSubstitute):
    """Blocks until released, then produces a perfectly valid decision.

    This is the dangerous shape: the engine already recorded the request as
    ``deadline_exceeded``, and the worker later really does return a decision.
    """

    def __init__(self, gate, hold_agents):
        self.gate = gate
        self.hold_agents = set(hold_agents)
        self.returned_after_release = 0

    def decide(self, observation):
        if observation["agent_id"] in self.hold_agents:
            self.gate.wait()
            self.returned_after_release += 1
        return super().decide(observation)


def live_worker_threads():
    return [t for t in threading.enumerate() if t.name.startswith("riskshield-decision")]


def test_repeated_timeouts_do_not_accumulate_threads(tmp_path):
    """Worker threads are bounded and daemon, so repeated stops cannot pile up."""
    baseline = len(live_worker_threads())
    for _ in range(3):
        _, engine, run_id = make_engine(tmp_path, agents=6, rounds=2, concurrency=3)
        gate = threading.Event()
        backend = LateResponder(gate, hold_agents=tuple(
            f"agent_{index:04d}" for index in range(1, 4)))
        engine.advance(run_id, backend, collect_performance=True,
                       call_timeout_seconds=0.1)
        gate.set()
        time.sleep(0.05)
        assert len(live_worker_threads()) <= baseline + 3 + 1
    assert all(thread.daemon for thread in live_worker_threads())


def test_a_late_response_never_touches_the_ledger(tmp_path):
    """A result arriving after the stop must not reach the run at all."""
    store, engine, run_id = make_engine(tmp_path, agents=6, rounds=2, concurrency=3)
    gate = threading.Event()
    held = tuple(f"agent_{index:04d}" for index in range(1, 4))
    backend = LateResponder(gate, hold_agents=held)
    result = engine.advance(run_id, backend, collect_performance=True,
                            call_timeout_seconds=0.1)

    def snapshot():
        with store.connect() as db:
            actions = {row["agent_id"]: dict(row) for row in db.execute(
                "SELECT agent_id, status, decision, usage, cost_cny FROM "
                "simulation_actions WHERE run_id=?", (run_id,))}
            messages = db.execute("SELECT COUNT(*) FROM simulation_messages "
                                  "WHERE run_id=?", (run_id,)).fetchone()[0]
            memories = db.execute("SELECT COUNT(*) FROM simulation_memories "
                                  "WHERE run_id=?", (run_id,)).fetchone()[0]
        return actions, messages, memories

    before = snapshot()
    cost_before = result["cost_cny"]
    # Let the abandoned workers finish; they produce valid decisions that must be dropped.
    gate.set()
    time.sleep(0.4)
    after = snapshot()

    assert backend.returned_after_release == len(held)
    assert before[0] == after[0], "a late result changed the action ledger"
    assert before[1] == after[1] and before[2] == after[2], (
        "a late result wrote messages or memories")
    for agent_id in held:
        row = after[0][agent_id]
        assert row["status"] == "deadline_exceeded"
        assert row["decision"] is None and row["usage"] is None
    summary = engine.summary(run_id)
    assert summary["cost_cny"] == cost_before
    assert summary["billing"]["cost_reconciles"] is True
    assert summary["opportunities"]["fully_reconciled"] is True


def test_a_stopped_run_can_be_resumed_without_a_ledger_collision(tmp_path):
    """Retrying an abandoned round must replace its ledger row, not collide with it.

    Regression: the abandoned round leaves a ``simulation_rounds`` row behind, so the
    resumed attempt hit ``UNIQUE constraint failed: simulation_rounds.run_id,
    simulation_rounds.round_number``.
    """
    store, engine, run_id = make_engine(tmp_path, agents=4, rounds=2, concurrency=2)
    gate = threading.Event()
    first = engine.advance(run_id, LateResponder(gate, hold_agents=("agent_0001",)),
                           collect_performance=True, call_timeout_seconds=0.1)
    gate.set()
    time.sleep(0.2)
    assert first["status"] == "partial"

    # A fresh engine instance proves the retry reads only what was persisted.
    restarted = Day3Simulation(Store(store.path))
    second = restarted.advance(run_id, PricedSubstitute(), collect_performance=True)
    assert second["status"] == "complete"
    assert second["completed_rounds"] == 2
    # The abandoned request keeps its own outcome and is not silently retried.
    assert second["action_counts"].get("deadline_exceeded", 0) == 1
    assert second["opportunities"]["fully_reconciled"] is True
    assert second["opportunities"]["buckets"]["not_dispatched_current_round"] == 0
    assert second["billing"]["cost_reconciles"] is True
    with store.connect() as db:
        rounds = [row["round_number"] for row in db.execute(
            "SELECT round_number FROM simulation_rounds WHERE run_id=? "
            "ORDER BY round_number", (run_id,))]
    assert rounds == [1, 2]


def test_report_rejects_a_ledger_whose_completed_rounds_are_not_a_prefix(tmp_path):
    """The relaxed gate must still reject a genuinely inconsistent ledger."""
    from riskshield.reporting import Day4Reports, ReportError

    store, engine, run_id = make_engine(tmp_path, agents=2, rounds=2, concurrency=1)
    engine.advance(run_id, PricedSubstitute(), collect_performance=True)
    # Mark round 1 as abandoned while both rounds completed: the completed rounds then are
    # not a prefix from round 1, which the checker must refuse.
    with store.connect() as db:
        stats = json.loads(db.execute(
            "SELECT stats FROM simulation_rounds WHERE run_id=? AND round_number=1",
            (run_id,)).fetchone()["stats"])
        stats["round_abandoned"] = True
        db.execute("UPDATE simulation_rounds SET stats=? WHERE run_id=? AND round_number=1",
                   (json.dumps(stats), run_id))
    with pytest.raises(ReportError, match="incomplete or inconsistent"):
        Day4Reports(store).build(run_id)
