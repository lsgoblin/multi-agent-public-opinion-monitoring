"""Result accounting, failure/stop semantics, billing states and performance capture.

Four facts must stay separable:

1. how many rounds actually ran (``completed_rounds``);
2. how many request opportunities were handled (``decision_requests``);
3. how many decisions were valid (``valid_decisions`` / ``all_decisions_valid``);
4. whether the run may be delivered as a complete success.

Whole-task opportunity reconciliation is checked here against the *configured* total
(agents x rounds), never against the number of persisted rows: a run stopped at round 1
of 30 owes an accounting for the 14,000 opportunities it never reached, and those can
never appear as rows.

Billing is checked with non-zero simulated prices so that reservation, settlement and
release are actually exercised. A request that was dispatched but whose usage is unknown
is reported as ``unsettled_reserved`` and keeps its reservation as cost; it is never
reported as free.

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

    def __init__(self, *, fail_agents=(), fail_at_call=None, delay=0.0,
                 block_calls=0, block_seconds=0.0, usage=None):
        self.fail_agents = set(fail_agents)
        self.fail_at_call = fail_at_call
        self.delay = delay
        self.block_calls = block_calls
        self.block_seconds = block_seconds
        self.usage = usage if usage is not None else {"prompt_tokens": 1,
                                                      "completion_tokens": 1}
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
            if call_index <= self.block_calls and self.block_seconds:
                time.sleep(self.block_seconds)
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
                usage=dict(self.usage), model=self.model)
        finally:
            with self._lock:
                self.in_flight -= 1


class CostedSubstitute(ObservableSubstitute):
    """Non-zero prices and reservation so budget and billing paths are exercised."""

    mode = "real_model"
    reservation_cny = Decimal("0.20")
    input_cny_per_million = Decimal("3.00")
    cached_input_cny_per_million = Decimal("0.60")
    output_cny_per_million = Decimal("6.00")
    price_version = "simulated-prices-v1"


def persisted_counts(store, run_id):
    with store.connect() as db:
        rows = [dict(row) for row in db.execute(
            "SELECT round_number, agent_id, status FROM simulation_actions "
            "WHERE run_id=? ORDER BY round_number, agent_id", (run_id,))]
        duplicates = [dict(row) for row in db.execute(
            "SELECT round_number, agent_id, COUNT(*) AS n FROM simulation_actions "
            "WHERE run_id=? GROUP BY round_number, agent_id HAVING n > 1", (run_id,))]
    return rows, duplicates


def assert_reconciles(store, run_id, result, *, agents, expected_rounds,
                      expected=None, deadline_exceeded=0):
    """Persisted rows are exclusive, and the four buckets sum to the task total."""
    rows, duplicates = persisted_counts(store, run_id)
    assert duplicates == []
    per_round = {}
    for row in rows:
        per_round.setdefault(row["round_number"], []).append(row["status"])
    for round_number, statuses in per_round.items():
        counts = {status: statuses.count(status) for status in set(statuses)}
        assert sum(counts.values()) == len(statuses) <= agents, round_number
        assert set(counts) <= {"valid", "failed", "not_dispatched", "deadline_exceeded"}
    valid = sum(1 for row in rows if row["status"] == "valid")
    failed = sum(1 for row in rows if row["status"] == "failed")
    stranded_rows = sum(1 for row in rows if row["status"] == "not_dispatched")
    abandoned_rows = sum(1 for row in rows if row["status"] == "deadline_exceeded")
    assert valid + failed + stranded_rows + abandoned_rows == len(rows)
    assert result["action_counts"].get("valid", 0) == valid
    assert result["action_counts"].get("failed", 0) == failed
    assert result["action_counts"].get("deadline_exceeded", 0) == abandoned_rows

    completed = [row for row in result["rounds"] if not row.get("round_abandoned")]
    assert len(completed) <= expected_rounds
    for row in completed:
        assert row["decision_requests"] == row["configured_agents"] == agents
        assert (row["valid_decisions"] + row["failed_decisions"] +
                row["not_dispatched_requests"]) == row["decision_requests"]

    opportunities = result["opportunities"]
    buckets = opportunities["buckets"]
    assert set(buckets) == {"valid", "failed", "deadline_exceeded",
                            "not_dispatched_current_round",
                            "not_started_future_rounds"}
    assert opportunities["total_opportunities"] == agents * expected_rounds
    assert opportunities["fully_reconciled"] is True, opportunities
    assert sum(buckets.values()) == opportunities["total_opportunities"]
    assert buckets["valid"] == valid
    assert buckets["failed"] == failed
    assert opportunities["completed_rounds"] == len(completed)
    if expected is not None:
        # Callers spell out only the buckets their scenario exercises; a request the
        # engine stopped waiting for is absent from those cases by construction.
        full = {"deadline_exceeded": deadline_exceeded, **expected}
        assert buckets == full, buckets
    return {"rows": len(rows), "rounds_in_db": len(per_round), "buckets": buckets}


def test_success_path_accounts_for_every_opportunity(tmp_path):
    store, engine, run_id = make_engine(tmp_path, agents=8, rounds=3, concurrency=4)
    backend = ObservableSubstitute()
    result = engine.advance(run_id, backend, collect_performance=True)

    assert result["status"] == "complete"
    assert result["completed_rounds"] == 3
    assert result["all_decisions_valid"] is True
    totals = assert_reconciles(
        store, run_id, result, agents=8, expected_rounds=3,
        expected={"valid": 24, "failed": 0, "not_dispatched_current_round": 0,
                  "not_started_future_rounds": 0})
    assert totals["rows"] == 24
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
    assert_reconciles(store, run_id, result, agents=8, expected_rounds=3,
                      expected={"valid": 21, "failed": 3,
                                "not_dispatched_current_round": 0,
                                "not_started_future_rounds": 0})
    for row in result["rounds"]:
        assert row["failure_policy"] == {
            "mode": "continue", "tolerated_failures": 1, "stopped_by_failure": False,
            "failure_categories": {"RuntimeError": 1},
        }
        assert row["dispatched_requests"] == 8
    # The failing agent was asked once per round and never silently retried.
    for round_number in (1, 2, 3):
        assert backend.calls.count((round_number, "agent_0000")) == 1


def test_stop_policy_strands_the_remainder_and_records_one_failure(tmp_path):
    store, engine, run_id = make_engine(tmp_path, agents=8, rounds=3, concurrency=1)
    backend = ObservableSubstitute(fail_agents={"agent_0000"})
    result = engine.advance(run_id, backend, collect_performance=True,
                            stop_on_failure=True)

    assert result["status"] == "partial"
    assert result["completed_rounds"] == 0
    assert result["all_decisions_valid"] is False
    totals = assert_reconciles(
        store, run_id, result, agents=8, expected_rounds=3,
        expected={"valid": 0, "failed": 1, "not_dispatched_current_round": 7,
                  "not_started_future_rounds": 16})
    assert totals["rows"] == 8
    # Concurrency 1 means nothing else was already in flight when the stop landed.
    assert backend.calls == [(1, "agent_0000")]
    # The abandoned round is recorded as abandoned, not as a completed round.
    abandoned = [row for row in result["rounds"] if row.get("round_abandoned")]
    assert len(abandoned) == 1
    assert abandoned[0]["not_dispatched_current_round"] == 7
    assert result["opportunities"]["abandoned_rounds"] == 1


@pytest.mark.parametrize(("concurrency", "expected_valid", "expected_stranded"), [
    (1, 2, 5),
    (2, 3, 4),
    (4, 5, 2),
    (8, 7, 8),
])
def test_stop_policy_strands_only_what_was_not_already_submitted(
        tmp_path, concurrency, expected_valid, expected_stranded):
    """`stop` prevents further submission; it cannot recall a launched request.

    The substitute fails on its third call. When the stop lands, in-flight requests are
    recorded as timed out rather than waited for, so a request that already returned
    counts as valid and the rest of the round is stranded. Higher concurrency therefore
    means more requests completed before the stop and fewer opportunities stranded; at
    concurrency 8 the whole first round was submitted, so round 1 completes and round 2
    is stranded whole.
    """
    store, engine, run_id = make_engine(tmp_path, agents=8, rounds=3,
                                        concurrency=concurrency)
    backend = ObservableSubstitute(fail_at_call=3)
    result = engine.advance(run_id, backend, collect_performance=True,
                            stop_on_failure=True)

    assert result["status"] == "partial"
    assert result["all_decisions_valid"] is False
    totals = assert_reconciles(store, run_id, result, agents=8, expected_rounds=3)
    buckets = totals["buckets"]
    assert buckets["valid"] == expected_valid
    assert buckets["failed"] == 1
    assert buckets["not_dispatched_current_round"] == expected_stranded
    assert len(backend.calls) >= expected_valid + 1
    assert len(backend.calls) == len(set(backend.calls))
    assert buckets["valid"] + buckets["failed"] == len(backend.calls) - (
        buckets["deadline_exceeded"])
    assert result["performance"]["completed_requests"] == len(backend.calls) - (
        buckets["deadline_exceeded"])
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
    completed = [row for row in result["rounds"] if not row.get("round_abandoned")]
    assert [row["round"] for row in completed] == [1]
    assert completed[0]["valid_decisions"] == 7
    assert completed[0]["failed_decisions"] == 1
    assert result["completed_rounds"] == 1
    assert_reconciles(store, run_id, result, agents=8, expected_rounds=3,
                      expected={"valid": 7, "failed": 1,
                                "not_dispatched_current_round": 8,
                                "not_started_future_rounds": 8})


def test_budget_preflight_strands_the_round_before_any_dispatch(tmp_path):
    store, engine, run_id = make_engine(tmp_path, agents=8, rounds=2, concurrency=4,
                                        budget="1.00")
    backend = CostedSubstitute()
    result = engine.advance(run_id, backend, collect_performance=True)

    assert result["status"] == "partial"
    assert result["completed_rounds"] == 0
    assert backend.calls == []
    assert_reconciles(store, run_id, result, agents=8, expected_rounds=2,
                      expected={"valid": 0, "failed": 0,
                                "not_dispatched_current_round": 8,
                                "not_started_future_rounds": 8})
    assert result["cost_cny"] == "0"
    assert result["usage"]["unknown_usage_calls"] == 0
    # Nothing was sent, so nothing was reserved and nothing was billed.
    assert result["billing"]["states"] == {"not_sent": 8, "released": 0, "settled": 0,
                                           "unsettled_reserved": 0}


def test_partial_round_limit_leaves_later_rounds_unstarted(tmp_path):
    store, engine, run_id = make_engine(tmp_path, agents=6, rounds=4, concurrency=3)
    backend = ObservableSubstitute()
    result = engine.advance(run_id, backend, collect_performance=True, max_rounds=2)

    assert result["status"] == "running"
    assert result["completed_rounds"] == 2
    assert result["all_decisions_valid"] is False  # rounds remain
    assert_reconciles(store, run_id, result, agents=6, expected_rounds=4,
                      expected={"valid": 12, "failed": 0,
                                "not_dispatched_current_round": 0,
                                "not_started_future_rounds": 12})
    assert result["action_counts"] == {"valid": 12}


# --------------------------------------------------------------------------- billing

def test_billing_settles_known_usage_at_frozen_rates(tmp_path):
    """A successful priced request settles to the priced amount, not the reservation."""
    store, engine, run_id = make_engine(tmp_path, agents=4, rounds=2, concurrency=2,
                                        budget="30.00")
    backend = CostedSubstitute(usage={"prompt_tokens": 1000, "completion_tokens": 200})
    result = engine.advance(run_id, backend, collect_performance=True)

    assert result["status"] == "complete"
    billing = result["billing"]
    assert billing["states"] == {"not_sent": 0, "released": 0, "settled": 8,
                                 "unsettled_reserved": 0}
    # 1000 uncached input at 3.00/M plus 200 output at 6.00/M = 0.003 + 0.0012.
    expected_each = (Decimal("1000") * Decimal("3.00") +
                     Decimal("200") * Decimal("6.00")) / Decimal(1_000_000)
    assert Decimal(billing["settled_cost_cny"]) == expected_each * 8
    assert Decimal(billing["charged_cost_cny"]) == Decimal(result["cost_cny"])
    assert billing["cost_reconciles"] is True
    assert Decimal(billing["reservation"]["reserved"]) == Decimal("0.20") * 8
    assert Decimal(billing["reservation"]["released"]) == 0


def test_billing_keeps_the_reservation_when_usage_is_unknown(tmp_path):
    """A dispatched request with unknown usage is never reported as free."""
    store, engine, run_id = make_engine(tmp_path, agents=4, rounds=2, concurrency=2,
                                        budget="30.00")
    backend = CostedSubstitute(fail_agents={"agent_0000"})
    result = engine.advance(run_id, backend, collect_performance=True)

    billing = result["billing"]
    assert billing["states"] == {"not_sent": 0, "released": 0, "settled": 6,
                                 "unsettled_reserved": 2}
    assert Decimal(billing["unsettled_reserved_cost_cny"]) == Decimal("0.20") * 2
    assert Decimal(billing["charged_cost_cny"]) == Decimal(result["cost_cny"])
    assert billing["cost_reconciles"] is True
    assert result["all_decisions_valid"] is False
    assert result["usage"]["unknown_usage_calls"] == 2


def test_billing_reports_released_when_nothing_was_sent(tmp_path):
    """Budget preflight reserves nothing, so the requests are not_sent and cost 0."""
    store, engine, run_id = make_engine(tmp_path, agents=6, rounds=2, concurrency=2,
                                        budget="0.50")
    backend = CostedSubstitute()
    result = engine.advance(run_id, backend, collect_performance=True)

    assert result["status"] == "partial"
    billing = result["billing"]
    assert billing["states"]["not_sent"] == 6
    assert billing["states"]["settled"] == 0
    assert billing["states"]["unsettled_reserved"] == 0
    assert Decimal(billing["charged_cost_cny"]) == 0
    assert billing["cost_reconciles"] is True


def test_billing_covers_an_abandoned_round(tmp_path):
    """A round that aborts before writing stats still contributes its cost."""
    store, engine, run_id = make_engine(tmp_path, agents=4, rounds=3, concurrency=1,
                                        budget="30.00")
    backend = CostedSubstitute(fail_agents={"agent_0000"})
    result = engine.advance(run_id, backend, collect_performance=True,
                            stop_on_failure=True)

    assert result["status"] == "partial"
    billing = result["billing"]
    assert billing["states"]["unsettled_reserved"] == 1
    assert billing["states"]["not_sent"] == 3
    assert Decimal(billing["charged_cost_cny"]) == Decimal(result["cost_cny"])
    assert billing["cost_reconciles"] is True


@pytest.mark.parametrize("scenario", ["complete", "failed", "stopped"])
def test_billing_always_reconciles_with_run_cost(tmp_path, scenario):
    store, engine, run_id = make_engine(tmp_path, agents=4, rounds=2, concurrency=2,
                                        budget="30.00")
    failing = {"agent_0000"} if scenario != "complete" else set()
    backend = CostedSubstitute(fail_agents=failing)
    extra = {"stop_on_failure": True} if scenario == "stopped" else {}
    result = engine.advance(run_id, backend, collect_performance=True, **extra)

    billing = result["billing"]
    charged = (Decimal(billing["cost_by_state"]["settled"]) +
               Decimal(billing["cost_by_state"]["unsettled_reserved"]))
    assert charged == Decimal(result["cost_cny"])
    assert billing["cost_reconciles"] is True
    assert billing["recorded_requests"] == sum(billing["states"].values())


# ------------------------------------------------------------------- performance

@pytest.mark.parametrize(("label", "kwargs", "expected_reason"), [
    ("budget_preflight", {"budget": "1.00", "costed": True}, "budget_preflight"),
    ("stop_signal", {"stop": True}, "stop_signal"),
    ("stop_on_failure", {"fail": True, "concurrency": 1, "stop": "failure"},
     "stop_on_failure"),
])
def test_stopped_runs_still_publish_a_performance_record(
        tmp_path, label, kwargs, expected_reason):
    """A stopped run must not lose its timing/throughput record."""
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


# ------------------------------------------------- deadline and in-flight boundaries

def test_deadline_stops_waiting_on_held_calls_and_records_them(tmp_path):
    """A held call must not extend the run, and must not be recorded as free or unsent.

    Four requests are held for 0.5 s while the deadline is 0.2 s away. The engine submits
    one batch and then stops waiting once the deadline passes, so the run returns in about
    the deadline rather than after the held calls. Those four requests were genuinely
    dispatched and never answered, so they are recorded as ``deadline_exceeded`` -- not
    ``not_dispatched`` (they were sent) and not free (their usage is unknown). Only the
    unstarted tail of the round is stranded.
    """
    store, engine, run_id = make_engine(tmp_path, agents=20, rounds=3, concurrency=4)
    backend = ObservableSubstitute(block_calls=4, block_seconds=0.5)
    started = time.perf_counter()
    result = engine.advance(run_id, backend, collect_performance=True,
                            deadline_monotonic=time.monotonic() + 0.2)
    wall = time.perf_counter() - started

    assert result["status"] == "partial"
    performance = result["performance"]
    assert performance["invocations"][0]["stop_reason"] == "deadline"
    # At most the first batch was ever submitted.
    assert len(backend.calls) == 4
    # The run came back near the deadline instead of waiting for the held calls.
    assert wall < 0.5
    # Nothing came back, so no request completed and all four are recorded as abandoned.
    assert performance["completed_requests"] == 0
    assert performance["deadline_exceeded_requests"] == 4
    assert performance["not_dispatched"] == 16
    assert result["action_counts"] == {"deadline_exceeded": 4, "not_dispatched": 16}
    assert_reconciles(store, run_id, result, agents=20, expected_rounds=3,
                      deadline_exceeded=4,
                      expected={"valid": 0, "failed": 0,
                                "not_dispatched_current_round": 16,
                                "not_started_future_rounds": 40})


def test_expired_deadline_dispatches_nothing_and_returns_at_once(tmp_path):
    store, engine, run_id = make_engine(tmp_path, agents=6, rounds=3, concurrency=2)
    backend = ObservableSubstitute()
    result = engine.advance(run_id, backend, collect_performance=True,
                            deadline_monotonic=time.monotonic() - 1)

    assert result["status"] == "partial"
    assert backend.calls == []
    assert result["action_counts"] == {"not_dispatched": 6}
    assert result["performance"]["invocations"][0]["stop_reason"] == "deadline"
    assert_reconciles(store, run_id, result, agents=6, expected_rounds=3,
                      expected={"valid": 0, "failed": 0,
                                "not_dispatched_current_round": 6,
                                "not_started_future_rounds": 12})


def test_stop_signal_mid_flight_drains_and_accounts_for_the_tail(tmp_path):
    """A stop signal landing mid-round drains the batch and strands the rest."""
    store, engine, run_id = make_engine(tmp_path, agents=12, rounds=2, concurrency=3)
    event = threading.Event()
    backend = ObservableSubstitute(delay=0.05)
    original = backend.decide

    def decide_then_signal(observation):
        outcome = original(observation)
        event.set()
        return outcome

    backend.decide = decide_then_signal
    result = engine.advance(run_id, backend, collect_performance=True, stop_event=event)

    assert result["status"] == "partial"
    assert result["performance"]["invocations"][0]["stop_reason"] == "stop_signal"
    assert 0 < len(backend.calls) <= 3
    assert_reconciles(store, run_id, result, agents=12, expected_rounds=2)
    buckets = result["opportunities"]["buckets"]
    assert buckets["valid"] == len(backend.calls)
    assert buckets["not_dispatched_current_round"] == 12 - len(backend.calls)


# ------------------------------------------------------------------- concurrency

@pytest.mark.parametrize("concurrency", [1, 2, 4, 8])
def test_observed_in_flight_reaches_the_configured_concurrency(tmp_path, concurrency):
    """Concurrency is only observable with non-zero latency."""
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
    # Still never a success, but the incomplete run now yields a report labelled incomplete
    # instead of no evidence at all.
    assert job["report_id"] is not None and job["alert_id"] is not None
    assert job["error"] == "incomplete_run_reported"
    with tasks.store.connect() as db:
        body = json.loads(db.execute("SELECT body FROM day4_reports WHERE report_id=?",
                                     (job["report_id"],)).fetchone()["body"])
    assert body["overview"]["run_integrity"]["complete"] is False


def test_failure_policy_rejects_conflicting_arguments(tmp_path):
    _, engine, run_id = make_engine(tmp_path, agents=1, rounds=1, concurrency=1)
    backend = ObservableSubstitute()
    with pytest.raises(SimulationError, match="not both"):
        engine.advance(run_id, backend, failure_policy="continue",
                       stop_on_failure=True)
    with pytest.raises(SimulationError, match="must be 'stop' or 'continue'"):
        engine.advance(run_id, backend, failure_policy="retry")
