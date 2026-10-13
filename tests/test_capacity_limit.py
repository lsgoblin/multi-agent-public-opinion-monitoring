"""Process-wide in-flight capacity: configuration, admission and the release protocol.

The behaviour under test is the one that cannot be enforced per run: a stuck worker keeps
holding its slot, so starting another task adds nothing and cannot bypass the ceiling.
"""

import threading
import time
from decimal import Decimal

import pytest

from riskshield.capacity import (CONFIG_ENV, DEFAULT_MAX_INFLIGHT_CALLS, CapacityError,
                                 InFlightCapacity, PROCESS_CAPACITY, configured_limit)
from riskshield.simulation import (CapacityExhaustedError, Day3Simulation, DecisionResult,
                                   SimulationError)
from riskshield.schemas import AgentDecision
from riskshield.store import Store
from riskshield.tasks import Day4Tasks, OfflineDynamicSubstitute
from tests.fixtures.day3_synthetic_case import (CASE_ID, GRAPH_ID, prepare_synthetic_case)


# ----------------------------------------------------------------- configuration

def test_the_ceiling_defaults_to_sixteen_when_unset(monkeypatch):
    monkeypatch.delenv(CONFIG_ENV, raising=False)
    assert configured_limit() == DEFAULT_MAX_INFLIGHT_CALLS == 16


@pytest.mark.parametrize("raw", ["1", "16", " 32 ", "128"])
def test_a_positive_integer_is_accepted(monkeypatch, raw):
    monkeypatch.setenv(CONFIG_ENV, raw)
    assert configured_limit() == int(raw.strip())


@pytest.mark.parametrize("raw", ["0", "-1", "1.5", "16.0", "abc", "", "  ", "+16",
                                 "1e3", "0x10", "16,", "True"])
def test_anything_but_a_positive_integer_is_rejected(monkeypatch, raw):
    monkeypatch.setenv(CONFIG_ENV, raw)
    if raw.strip() == "":
        # An empty value is treated as unset, not as a configuration error.
        assert configured_limit() == DEFAULT_MAX_INFLIGHT_CALLS
        return
    with pytest.raises(CapacityError, match=CONFIG_ENV):
        configured_limit()


def test_an_invalid_setting_fails_before_any_call_is_made(tmp_path, monkeypatch):
    """A bad ceiling must stop the run at its entry point, not mid-round.

    ``create_run`` is already an engine entry point that can lead to dispatch, so it
    validates the setting too: refusing at creation is strictly earlier than refusing at
    the first dispatch, and it is the point where the requested concurrency has to fit.
    """
    monkeypatch.setenv(CONFIG_ENV, "0")
    store = Store(tmp_path / "invalid.db")
    engine = Day3Simulation(store)
    prepare_synthetic_case(store)
    backend = _RecordingSubstitute()
    with pytest.raises(SimulationError, match=CONFIG_ENV):
        engine.create_run(CASE_ID, GRAPH_ID, agent_count=2, rounds=1, concurrency=2)
    assert backend.calls == 0


def test_an_invalid_setting_also_stops_an_existing_run_before_dispatch(tmp_path,
                                                                      monkeypatch):
    """An already-created run must not start dispatching under a broken setting.

    The setting is re-read at this entry point rather than trusted from a value cached
    earlier, so a typo introduced after the run was created still cannot lead to calls.
    """
    store = Store(tmp_path / "invalid-existing.db")
    engine = Day3Simulation(store)
    prepare_synthetic_case(store)
    run_id = engine.create_run(CASE_ID, GRAPH_ID, agent_count=2, rounds=1, concurrency=2)
    assert PROCESS_CAPACITY.limit == 16, "the setting must have been read by now"
    backend = _RecordingSubstitute()
    monkeypatch.setenv(CONFIG_ENV, "not-a-number")
    with pytest.raises(SimulationError, match=CONFIG_ENV):
        engine.advance(run_id, backend)
    assert backend.calls == 0


# ------------------------------------------------------------- the release protocol

def test_a_slot_is_only_released_when_the_backend_returns():
    """The core invariant: a wait timeout does not free the slot the worker still holds."""
    capacity = InFlightCapacity(limit=2)
    first = capacity.acquire(reason="call")
    second = capacity.acquire(reason="call")
    assert capacity.used() == 2 and capacity.available() == 0
    with pytest.raises(CapacityError, match="instead of queueing"):
        capacity.acquire(reason="call")
    # "The caller stopped waiting" is not a release: nothing changes.
    assert capacity.used() == 2
    assert capacity.release(first) is True
    assert capacity.available() == 1
    # A second release of the same token must not invent capacity out of nothing.
    assert capacity.release(first) is False
    assert capacity.available() == 1
    assert capacity.release(second) is True
    assert capacity.available() == 2


def test_a_slot_never_leaks_when_released_from_another_thread():
    capacity = InFlightCapacity(limit=1)
    token = capacity.acquire(reason="call")
    assert capacity.available() == 0

    def finish_late():
        time.sleep(0.05)
        capacity.release(token)

    worker = threading.Thread(target=finish_late)
    worker.start()
    worker.join(timeout=5)
    assert capacity.available() == 1


# ------------------------------------------------------------------- engine level

class _RecordingSubstitute:
    """Answers immediately and counts how many calls actually reached the backend."""

    mode = "test_substitute"
    reservation_cny = Decimal("0")
    input_cny_per_million = Decimal("0")
    cached_input_cny_per_million = Decimal("0")
    output_cny_per_million = Decimal("0")
    price_version = "capacity-test-v1"
    model = "capacity-test"

    def __init__(self, gate=None, blocked=()):
        self.gate = gate
        self.blocked = set(blocked)
        self.calls = 0

    def decide(self, observation):
        self.calls += 1
        if observation["agent_id"] in self.blocked and self.gate is not None:
            self.gate.wait(timeout=10)
        reference = observation["evidence"][0]["evidence_id"]
        return DecisionResult(AgentDecision(
            agent_id=observation["agent_id"], action="observe",
            evidence_ids=[reference], reason="capacity test decision"),
            usage={"prompt_tokens": 1, "completion_tokens": 1}, model=self.model)


def _engine(tmp_path, name):
    store = Store(tmp_path / name)
    engine = Day3Simulation(store)
    prepare_synthetic_case(store)
    return store, engine


def test_a_run_whose_concurrency_exceeds_the_ceiling_is_refused(tmp_path, monkeypatch):
    """Checked at creation: the ceiling is never raised to fit the run."""
    monkeypatch.setenv(CONFIG_ENV, "4")
    _, engine = _engine(tmp_path, "over.db")
    with pytest.raises(SimulationError, match="exceeds the process in-flight limit"):
        engine.create_run(CASE_ID, GRAPH_ID, agent_count=8, rounds=1, concurrency=5)


def test_a_stuck_call_keeps_its_slot_while_the_run_gives_up_waiting(tmp_path):
    """A timed-out request still occupies process capacity until the backend returns."""
    store, engine = _engine(tmp_path, "stuck.db")
    run_id = engine.create_run(CASE_ID, GRAPH_ID, agent_count=3, rounds=1, concurrency=2,
                               seed=7)
    gate = threading.Event()
    backend = _RecordingSubstitute(gate=gate, blocked=("agent_0000",))
    try:
        summary = engine.advance(run_id, backend, collect_performance=True,
                                 call_timeout_seconds=0.2)
    finally:
        used_before_release = PROCESS_CAPACITY.used()
        gate.set()

    # The run returned on its own budget...
    assert summary["action_counts"].get("deadline_exceeded", 0) >= 1
    # ...but the worker is still inside the call, so the slot is still held.
    assert used_before_release == 1, (
        "the engine released a slot for a call that had not returned")
    deadline = time.monotonic() + 5
    while PROCESS_CAPACITY.used() and time.monotonic() < deadline:
        time.sleep(0.02)
    assert PROCESS_CAPACITY.used() == 0, "the slot was never released after the call ended"


def test_an_exhausted_process_refuses_to_dispatch_and_charges_nothing(tmp_path):
    """With the process full, a task is refused before any request is sent."""
    limit = PROCESS_CAPACITY.limit
    held = [PROCESS_CAPACITY.acquire(reason="test_fills_the_process_ceiling")
            for _ in range(limit)]
    assert PROCESS_CAPACITY.available() == 0
    store, engine = _engine(tmp_path, "refused.db")
    backend = _RecordingSubstitute()
    try:
        # A run that already exists and has one round to go: admission happens at dispatch,
        # which is exactly where the refusal must appear.
        run_id = engine.create_run(CASE_ID, GRAPH_ID, agent_count=1, rounds=1,
                                   concurrency=1, seed=3)
        with pytest.raises(CapacityExhaustedError) as caught:
            engine.advance(run_id, backend, collect_performance=True)
    finally:
        for token in held:
            PROCESS_CAPACITY.release(token)

    assert caught.value.rejected_requests == 1
    # Zero calls: a refused request is never sent, so it can never be charged.
    assert backend.calls == 0
    summary = engine.summary(run_id)
    assert str(summary["cost_cny"]) == "0"
    assert summary["action_counts"].get("not_dispatched", 0) == 1
    assert summary["billing"]["states"]["not_sent"] == 1
    assert summary["capacity"]["rejected_requests"] == 1
    with store.connect() as db:
        rows = [(row["status"], row["error"], row["cost_cny"], row["reserved_cny"])
                for row in db.execute("SELECT status, error, cost_cny, reserved_cny "
                                      "FROM simulation_actions WHERE run_id=?", (run_id,))]
    assert rows == [("not_dispatched", "CapacityExhausted", "0", "0")]


def test_a_clean_run_returns_every_slot_it_took(tmp_path):
    """The ceiling must not drift: a successful run ends with nothing occupied."""
    _, engine = _engine(tmp_path, "clean.db")
    run_id = engine.create_run(CASE_ID, GRAPH_ID, agent_count=4, rounds=2, concurrency=2,
                               seed=11)
    result = engine.advance(run_id, _RecordingSubstitute(), collect_performance=True)
    assert result["status"] == "complete"
    assert PROCESS_CAPACITY.used() == 0
    assert result["capacity"]["rejected_requests"] == 0


# --------------------------------------------------------------------- task level

def test_a_refused_task_fails_with_capacity_exhausted_and_no_retry(tmp_path, monkeypatch):
    held = []
    for _ in range(16):
        held.append(PROCESS_CAPACITY.acquire(reason="test_fills_the_process_ceiling"))
    tasks = Day4Tasks(Store(tmp_path / "capacity-task.db"))
    run = tasks.prepare_demo_run(agent_count=2, rounds=1, concurrency=1)
    try:
        job = tasks.run_sync(run["run_id"])
    finally:
        for token in held:
            PROCESS_CAPACITY.release(token)

    assert job["status"] == "failed"
    assert job["failure_category"] == "capacity_exhausted"
    assert job["error"] == "capacity_exhausted"
    # No report and no alert: there was no decision to assess.
    assert job["report_id"] is None and job["alert_id"] is None
    assert job["result_state"] in {"failed", "stopped"}


def test_four_blocked_tasks_never_exceed_the_ceiling(tmp_path, monkeypatch):
    """The acceptance case: four permanently blocked tasks cannot pile up.

    Before the process ceiling, each task added its own set of stuck workers (3, 6, 9, 12
    at concurrency 3) because nothing outside a run counted them. The gate here is released
    only after all four tasks have been attempted, so every worker that enters it stays
    inside its call: live threads and occupied slots must stay flat at the ceiling, and the
    tasks that cannot be admitted must be refused rather than queued.
    """
    monkeypatch.setenv(CONFIG_ENV, "3")
    store = Store(tmp_path / "four-tasks.db")
    # The task needs its own wait budget, otherwise the first round would block forever on
    # the held gate and the test could never observe four attempted tasks. The budget only
    # stops the engine from *waiting*; the workers stay inside their calls and keep their
    # slots, which is the state under test.
    tasks = Day4Tasks(store, simulation_call_timeout_seconds=0.3)
    gate = threading.Event()
    original = OfflineDynamicSubstitute.decide

    def hangs(self, observation):
        gate.wait()  # never released until every task has been attempted
        return original(self, observation)

    monkeypatch.setattr(OfflineDynamicSubstitute, "decide", hangs)

    def live_workers():
        return len([item for item in threading.enumerate()
                    if item.name.startswith("riskshield-decision")])

    observed = []
    try:
        for index in range(4):
            run = tasks.prepare_demo_run(agent_count=3, rounds=2, concurrency=3)
            try:
                job = tasks.run_sync(run["run_id"])
                category = job["failure_category"]
            except SimulationError as exc:
                # Refused at a later entry point (for example creation of the next run);
                # still a refusal, never a queue.
                category = "refused_at_entry" if "limit" in str(exc) else "unexpected"
            observed.append((category, live_workers(), PROCESS_CAPACITY.used()))
            # The totals must never grow with the number of tasks.
            assert observed[-1][1] <= 3, observed
            assert observed[-1][2] <= 3, observed

        categories = [category for category, _, _ in observed]
        # The first task fills the ceiling; the rest cannot be admitted and are refused
        # outright rather than waiting for capacity or starting another run.
        assert categories[0] != "unexpected", observed
        assert any(category in {"capacity_exhausted", "refused_at_entry"}
                   for category in categories), observed
        # Flat, not growing: the whole point of the fix.
        assert observed[-1][1] <= 3 and observed[-1][2] <= 3, observed
    finally:
        gate.set()
        deadline = time.monotonic() + 10
        while PROCESS_CAPACITY.used() and time.monotonic() < deadline:
            time.sleep(0.05)
    assert PROCESS_CAPACITY.used() == 0, "slots must come back once the calls return"
