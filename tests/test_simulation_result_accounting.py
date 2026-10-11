"""Result accounting, failure/stop semantics and performance capture.

These tests pin the four facts that must stay separable:

1. how many rounds actually ran (``completed_rounds``);
2. how many request opportunities were handled (``decision_requests``);
3. how many decisions were valid (``valid_decisions`` / ``all_decisions_valid``);
4. whether the run may be delivered as a complete success.

A run whose rounds all ran but which contains failed decisions is ``complete`` yet
``all_decisions_valid=False``; it is explicitly not a successful job. Downstream
consumers (``tasks``, ``reporting``, ``alerts``) already reject that combination, and
the tests below keep the distinction visible rather than collapsing it into ``partial``.

Everything here is offline: the substitute backends never touch a network or a model.
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


def make_engine(tmp_path, *, agents, rounds, concurrency, budget="5.00", seed=17):
    store = Store(tmp_path / "reconcile.db")
    engine = Day3Simulation(store)
    prepare_synthetic_case(store)
    run_id = engine.create_run(CASE_ID, GRAPH_ID, agent_count=agents, rounds=rounds,
                               concurrency=concurrency, seed=seed, budget_cny=budget)
    return store, engine, run_id


class ObservableSubstitute:
    """Records every request it actually received, so double dispatch is detectable."""

    mode = "test_substitute"
    reservation_cny = Decimal("0")
    input_cny_per_million = Decimal("0")
    cached_input_cny_per_million = Decimal("0")
    output_cny_per_million = Decimal("0")
    price_version = "reconcile-v1"
    model = "reconcile"

    def __init__(self, *, fail_agents=(), fail_at_call=None, delay=0.0):
        self.fail_agents = set(fail_agents)
        self.fail_at_call = fail_at_call
        self.delay = delay
        self._lock = threading.Lock()
        self.calls = []
        self.in_flight = 0
        self.peak_in_flight = 0

    def decide(self, observation):
        with self._lock:
            self.calls.append((observation["round"], observation["agent_id"]))
            call_index = len(self.calls)
            self.in_flight += 1
            self.peak_in_flight = max(self.peak_in_flight, self.in_flight)
        try:
            if self.delay:
                time.sleep(self.delay)
            if (observation["agent_id"] in self.fail_agents or
                    (self.fail_at_call is not None and call_index == self.fail_at_call)):
                raise RuntimeError("substitute-controlled-failure")
            reference = (observation["messages"][0]["message_id"]
                         if observation["messages"] else
                         observation["evidence"][0]["evidence_id"])
            return DecisionResult(AgentDecision(
                agent_id=observation["agent_id"], action="observe",
                evidence_ids=[reference], reason="recorded substitute decision"),
                usage={"prompt_tokens": 1, "completion_tokens": 1}, model=self.model)
        finally:
            with self._lock:
                self.in_flight -= 1


class CostedSubstitute(ObservableSubstitute):
    """Non-zero prices and reservation so the budget preflight can reject a round."""

    mode = "real_model"
    reservation_cny = Decimal("0.20")
    input_cny_per_million = Decimal("1")
    cached_input_cny_per_million = Decimal("1")
    output_cny_per_million = Decimal("1")


def persisted_counts(store, run_id):
    with store.connect() as db:
        rows = [dict(row) for row in db.execute(
            "SELECT round_number, agent_id, status FROM simulation_actions "
            "WHERE run_id=? ORDER BY round_number, agent_id", (run_id,))]
        duplicates = [dict(row) for row in db.execute(
            "SELECT round_number, agent_id, COUNT(*) AS n FROM simulation_actions "
            "WHERE run_id=? GROUP BY round_number, agent_id HAVING n > 1", (run_id,))]
    return rows, duplicates


def assert_reconciles(store, run_id, result, *, agents, expected_rounds):
    """Every opportunity is in exactly one bucket, and the buckets are exclusive."""
    rows, duplicates = persisted_counts(store, run_id)
    assert duplicates == []
    per_round = {}
    for row in rows:
        per_round.setdefault(row["round_number"], []).append(row["status"])
    for round_number, statuses in per_round.items():
        counts = {status: statuses.count(status) for status in set(statuses)}
        total = sum(counts.values())
        assert total == len(statuses) == agents, round_number
        assert set(counts) <= {"valid", "failed", "not_dispatched"}
    valid = sum(1 for row in rows if row["status"] == "valid")
    failed = sum(1 for row in rows if row["status"] == "failed")
    not_dispatched = sum(1 for row in rows if row["status"] == "not_dispatched")
    assert valid + failed + not_dispatched == len(rows)
    assert result["action_counts"].get("valid", 0) == valid
    assert result["action_counts"].get("failed", 0) == failed
    assert result["action_counts"].get("not_dispatched", 0) == not_dispatched
    # Completed rounds each contribute exactly one full round of opportunities.
    assert len(result["rounds"]) <= expected_rounds
    for row in result["rounds"]:
        assert row["decision_requests"] == row["configured_agents"] == agents
        assert (row["valid_decisions"] + row["failed_decisions"] +
                row["not_dispatched_requests"]) == row["decision_requests"]
    assert len(rows) == agents * (len(result["rounds"]) + (
        1 if len(per_round) > len(result["rounds"]) else 0))
    return {"valid": valid, "failed": failed, "not_dispatched": not_dispatched,
            "rows": len(rows), "rounds_in_db": len(per_round)}


def test_success_path_accounts_for_every_opportunity(tmp_path):
    store, engine, run_id = make_engine(tmp_path, agents=8, rounds=3, concurrency=4)
    backend = ObservableSubstitute()
    result = engine.advance(run_id, backend, collect_performance=True)

    assert result["status"] == "complete"
    assert result["completed_rounds"] == 3
    assert result["all_decisions_valid"] is True
    totals = assert_reconciles(store, run_id, result, agents=8, expected_rounds=3)
    assert totals == {"valid": 24, "failed": 0, "not_dispatched": 0,
                      "rows": 24, "rounds_in_db": 3}
    assert len(backend.calls) == len(set(backend.calls)) == 24
    assert result["performance"]["completed_requests"] == 24
    assert result["performance"]["not_dispatched"] == 0


def test_failed_decisions_continue_and_do_not_hide_round_completion(tmp_path):
    """All rounds ran, but the run is not all-valid: both facts stay visible."""
    store, engine, run_id = make_engine(tmp_path, agents=8, rounds=3, concurrency=4)
    backend = ObservableSubstitute(fail_agents={"agent_0000"})
    result = engine.advance(run_id, backend, collect_performance=True)

    assert result["status"] == "complete"
    assert result["completed_rounds"] == 3
    assert result["all_decisions_valid"] is False
    totals = assert_reconciles(store, run_id, result, agents=8, expected_rounds=3)
    assert totals == {"valid": 21, "failed": 3, "not_dispatched": 0,
                      "rows": 24, "rounds_in_db": 3}
    for row in result["rounds"]:
        assert row["failure_policy"] == {
            "mode": "continue", "tolerated_failures": 1, "stopped_by_failure": False,
            "failure_categories": {"RuntimeError": 1},
        }
        assert row["dispatched_requests"] == 8
    # The failing agent was asked once per round and never silently retried.
    assert backend.calls.count((1, "agent_0000")) == 1
    assert backend.calls.count((2, "agent_0000")) == 1
    assert backend.calls.count((3, "agent_0000")) == 1


def test_stop_policy_strands_the_remainder_and_records_one_failure(tmp_path):
    store, engine, run_id = make_engine(tmp_path, agents=8, rounds=3, concurrency=1)
    backend = ObservableSubstitute(fail_agents={"agent_0000"})
    result = engine.advance(run_id, backend, collect_performance=True,
                            stop_on_failure=True)

    assert result["status"] == "partial"
    assert result["completed_rounds"] == 0
    assert result["rounds"] == []
    assert result["all_decisions_valid"] is False
    totals = assert_reconciles(store, run_id, result, agents=8, expected_rounds=3)
    assert totals == {"valid": 0, "failed": 1, "not_dispatched": 7,
                      "rows": 8, "rounds_in_db": 1}
    # Concurrency 1 means nothing else was already in flight when the stop landed.
    assert backend.calls == [(1, "agent_0000")]


@pytest.mark.parametrize(("concurrency", "expected_valid", "expected_stranded"), [
    (1, 2, 5),
    (2, 2, 5),
    (4, 3, 4),
    (8, 7, 8),
])
def test_stop_policy_strands_only_what_was_not_already_submitted(
        tmp_path, concurrency, expected_valid, expected_stranded):
    """`stop` prevents further submission; it cannot recall a launched request.

    The substitute fails on its third call, so the first two decisions are already
    valid before any failure exists to react to. Raising concurrency lets more requests
    be submitted before that failure is observed, so fewer opportunities are stranded:
    at concurrency 1 five remain stranded, at concurrency 8 the whole first round had
    already been submitted, so round 1 completes and only round 2 is stranded.
    `peak_in_flight` stays 1 because an instantaneous substitute never actually
    overlaps -- which is exactly why an instantaneous substitute cannot be used to
    demonstrate concurrency, and why the concurrency tests below use a real delay.
    """
    store, engine, run_id = make_engine(tmp_path, agents=8, rounds=3,
                                        concurrency=concurrency)
    backend = ObservableSubstitute(fail_at_call=3)
    result = engine.advance(run_id, backend, collect_performance=True,
                            stop_on_failure=True)

    assert result["status"] == "partial"
    assert result["all_decisions_valid"] is False
    totals = assert_reconciles(store, run_id, result, agents=8, expected_rounds=3)
    assert totals["valid"] == expected_valid
    assert totals["failed"] == 1
    assert totals["not_dispatched"] == expected_stranded
    # Every opportunity is either a real request or explicitly stranded.
    assert len(backend.calls) == expected_valid + 1
    assert len(backend.calls) == len(set(backend.calls))
    assert totals["valid"] + totals["failed"] == len(backend.calls)
    assert result["performance"]["completed_requests"] == len(backend.calls)
    assert result["performance"]["not_dispatched"] == expected_stranded
    assert backend.peak_in_flight <= concurrency


def test_a_launched_but_later_round_is_never_double_counted(tmp_path):
    """Stranding a later round must not overwrite the round that did complete."""
    store, engine, run_id = make_engine(tmp_path, agents=8, rounds=3, concurrency=8)
    backend = ObservableSubstitute(fail_at_call=3)
    result = engine.advance(run_id, backend, collect_performance=True,
                            stop_on_failure=True)

    rows, duplicates = persisted_counts(store, run_id)
    assert duplicates == []
    round_one = [row for row in rows if row["round_number"] == 1]
    round_two = [row for row in rows if row["round_number"] == 2]
    assert len(round_one) == 8 and len(round_two) == 8
    assert {row["status"] for row in round_one} == {"valid", "failed"}
    assert {row["status"] for row in round_two} == {"not_dispatched"}
    # Round 1 is a completed round in the ledger; round 2 was stranded.
    assert [row["round"] for row in result["rounds"]] == [1]
    assert result["rounds"][0]["valid_decisions"] == 7
    assert result["rounds"][0]["failed_decisions"] == 1
    assert result["completed_rounds"] == 1


def test_budget_preflight_strands_the_round_before_any_dispatch(tmp_path):
    store, engine, run_id = make_engine(tmp_path, agents=8, rounds=2, concurrency=4,
                                        budget="1.00")
    backend = CostedSubstitute()
    result = engine.advance(run_id, backend, collect_performance=True)

    assert result["status"] == "partial"
    assert result["completed_rounds"] == 0
    assert backend.calls == []
    totals = assert_reconciles(store, run_id, result, agents=8, expected_rounds=2)
    assert totals == {"valid": 0, "failed": 0, "not_dispatched": 8,
                      "rows": 8, "rounds_in_db": 1}
    assert result["cost_cny"] == "0"
    assert result["usage"]["unknown_usage_calls"] == 0


def test_partial_round_limit_leaves_later_rounds_unstarted(tmp_path):
    store, engine, run_id = make_engine(tmp_path, agents=6, rounds=4, concurrency=3)
    backend = ObservableSubstitute()
    result = engine.advance(run_id, backend, collect_performance=True, max_rounds=2)

    assert result["status"] == "running"
    assert result["completed_rounds"] == 2
    assert result["all_decisions_valid"] is False  # rounds remain
    totals = assert_reconciles(store, run_id, result, agents=6, expected_rounds=4)
    assert totals == {"valid": 12, "failed": 0, "not_dispatched": 0,
                      "rows": 12, "rounds_in_db": 2}
    assert result["action_counts"] == {"valid": 12}


@pytest.mark.parametrize(("label", "kwargs", "expected_reason"), [
    ("budget_preflight", {"budget": "1.00", "costed": True}, "budget_preflight"),
    ("stop_signal", {"stop": True}, "stop_signal"),
    ("stop_on_failure", {"fail": True, "concurrency": 1, "stop": "failure"},
     "stop_on_failure"),
])
def test_stopped_runs_still_publish_a_performance_record(
        tmp_path, label, kwargs, expected_reason):
    """A stopped run must not lose its timing/throughput record.

    Before this was fixed, every abort path returned before writing the invocation row,
    so `_performance_summary` returned None and the one run whose concurrency and drain
    behaviour matters most had no performance evidence at all.
    """
    agents = 6
    concurrency = kwargs.get("concurrency", 2)
    budget = kwargs.get("budget", "5.00")
    store, engine, run_id = make_engine(tmp_path, agents=agents, rounds=2,
                                        concurrency=concurrency, budget=budget)
    backend_cls = CostedSubstitute if kwargs.get("costed") else ObservableSubstitute
    backend = backend_cls(fail_agents={"agent_0000"} if kwargs.get("fail") else ())
    extra = {}
    if kwargs.get("stop") is True:
        event = threading.Event()
        event.set()
        extra["stop_event"] = event
    if kwargs.get("stop") == "failure":
        extra["stop_on_failure"] = True

    result = engine.advance(run_id, backend, collect_performance=True, **extra)

    assert result["status"] == "partial", label
    performance = result.get("performance")
    assert performance is not None, label
    assert performance["advance_invocations"] == 1
    assert performance["invocations"][0]["stop_reason"] == expected_reason
    assert performance["completed_requests"] + performance["not_dispatched"] == agents
    assert performance["not_dispatched"] >= 1
    assert performance["maximum_observed_in_flight"] is not None


def test_performance_is_absent_when_capture_is_not_requested(tmp_path):
    store, engine, run_id = make_engine(tmp_path, agents=4, rounds=1, concurrency=2)
    result = engine.advance(run_id, ObservableSubstitute())
    assert result["status"] == "complete"
    assert "performance" not in result
    with store.connect() as db:
        assert db.execute("SELECT COUNT(*) FROM simulation_performance_calls "
                          "WHERE run_id=?", (run_id,)).fetchone()[0] == 0


def test_already_complete_run_does_not_record_a_second_invocation(tmp_path):
    store, engine, run_id = make_engine(tmp_path, agents=4, rounds=1, concurrency=2)
    first = engine.advance(run_id, ObservableSubstitute(), collect_performance=True)
    assert first["performance"]["advance_invocations"] == 1
    second = engine.advance(run_id, ObservableSubstitute(), collect_performance=True)
    assert second["status"] == "complete"
    assert second["performance"]["advance_invocations"] == 2
    assert second["action_counts"] == {"valid": 4}


@pytest.mark.parametrize("concurrency", [1, 2, 4, 8])
def test_observed_in_flight_reaches_the_configured_concurrency(tmp_path, concurrency):
    """Concurrency is only observable with non-zero latency.

    An instantaneous substitute finishes each request before the scheduler can overlap
    it, so this uses a real delay and asserts the pool genuinely fanned out.
    """
    agents = 16
    store, engine, run_id = make_engine(tmp_path, agents=agents, rounds=1,
                                        concurrency=concurrency)
    backend = ObservableSubstitute(delay=0.01)
    result = engine.advance(run_id, backend, collect_performance=True)

    assert result["status"] == "complete"
    assert backend.peak_in_flight == concurrency
    assert result["performance"]["maximum_observed_in_flight"] == concurrency
    assert len(backend.calls) == len(set(backend.calls)) == agents
    assert result["performance"]["completed_requests"] == agents


def test_failure_policy_never_exceeds_configured_concurrency(tmp_path):
    """A failing decision must not release extra workers or duplicate a request."""
    agents, concurrency = 24, 4
    store, engine, run_id = make_engine(tmp_path, agents=agents, rounds=2,
                                        concurrency=concurrency)
    backend = ObservableSubstitute(fail_at_call=5, delay=0.005)
    result = engine.advance(run_id, backend, collect_performance=True)

    assert backend.peak_in_flight <= concurrency
    assert result["performance"]["maximum_observed_in_flight"] <= concurrency
    assert len(backend.calls) == len(set(backend.calls)) == agents * 2
    assert_reconciles(store, run_id, result, agents=agents, expected_rounds=2)


def test_ledger_round_stats_do_not_carry_backend_text(tmp_path):
    """Failure accounting stores a bounded category, never a response body."""
    store, engine, run_id = make_engine(tmp_path, agents=3, rounds=1, concurrency=2)
    backend = ObservableSubstitute(fail_agents={"agent_0001"})
    result = engine.advance(run_id, backend, collect_performance=True)

    serialized = json.dumps(result, ensure_ascii=False)
    assert "substitute-controlled-failure" not in serialized
    assert result["rounds"][0]["failure_policy"]["failure_categories"] == {
        "RuntimeError": 1}
    with engine.store.connect() as db:
        categories = {row["failure_category"] for row in db.execute(
            "SELECT failure_category FROM simulation_request_metrics "
            "WHERE run_id=? AND status='failed'", (run_id,))}
    assert categories == {"RuntimeError"}


def test_incomplete_run_is_not_reported_as_a_successful_job(tmp_path):
    """The downstream gate must keep rejecting complete-but-invalid runs."""
    from riskshield.tasks import Day4Tasks, OfflineDynamicSubstitute

    tasks = Day4Tasks(Store(tmp_path / "tasks.db"))
    run = tasks.prepare_demo_run(agent_count=2, rounds=1, concurrency=2)
    original = OfflineDynamicSubstitute.decide

    def one_failed(self, observation):
        if observation["agent_id"].endswith("0000"):
            raise RuntimeError("substitute-controlled-failure")
        return original(self, observation)

    import pytest as _pytest
    monkeypatch = _pytest.MonkeyPatch()
    monkeypatch.setattr(OfflineDynamicSubstitute, "decide", one_failed)
    try:
        job = tasks.run_sync(run["run_id"])
    finally:
        monkeypatch.undo()

    assert job["simulation_status"] == "complete"
    assert job["all_decisions_valid"] is False
    assert job["status"] == "failed"
    assert job["result_state"] == "partial"
    assert job["report_id"] is None and job["alert_id"] is None


def test_failure_policy_rejects_conflicting_arguments(tmp_path):
    _, engine, run_id = make_engine(tmp_path, agents=1, rounds=1, concurrency=1)
    backend = ObservableSubstitute()
    with pytest.raises(SimulationError, match="not both"):
        engine.advance(run_id, backend, failure_policy="continue",
                       stop_on_failure=True)
    with pytest.raises(SimulationError, match="must be 'stop' or 'continue'"):
        engine.advance(run_id, backend, failure_policy="retry")
