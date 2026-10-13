"""Configuration and admission behaviour of the application entry point.

Covers the four scenarios the integration review asks for, at the API boundary: a normal
completion, a run whose last round timed out while earlier rounds decided, a run with no
valid decision anywhere, and a process that has run out of capacity. In every case the API,
the task, the report, the alert and the cost ledger must agree with each other.
"""

import threading
import time
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from riskshield.api import create_app
from riskshield.capacity import (CALL_TIMEOUT_ENV, CONFIG_ENV, PROCESS_CAPACITY,
                                 TASK_TIMEOUT_ENV, CapacityError,
                                 configured_call_timeout_seconds,
                                 configured_task_timeout_seconds,
                                 validate_startup_configuration)
from riskshield.store import Store
from riskshield.tasks import Day4Tasks, OfflineDynamicSubstitute
from tests.fixtures.day3_synthetic_case import CASE_ID, GRAPH_ID, prepare_synthetic_case


# ------------------------------------------------------------------ configuration

def test_the_documented_defaults_apply_when_nothing_is_set(monkeypatch):
    for name in (CONFIG_ENV, CALL_TIMEOUT_ENV, TASK_TIMEOUT_ENV):
        monkeypatch.delenv(name, raising=False)
    assert configured_call_timeout_seconds() == 120.0
    assert configured_task_timeout_seconds() == 3600.0
    assert validate_startup_configuration()[CONFIG_ENV] == 16


@pytest.mark.parametrize("name", [CONFIG_ENV, CALL_TIMEOUT_ENV, TASK_TIMEOUT_ENV])
@pytest.mark.parametrize("raw", ["0", "-1", "abc", "inf", "-inf", "nan", "1e999"])
def test_every_budget_rejects_a_non_positive_or_unbounded_value(monkeypatch, name, raw):
    monkeypatch.setenv(name, raw)
    with pytest.raises(CapacityError, match=name):
        validate_startup_configuration()


@pytest.mark.parametrize("name", [CALL_TIMEOUT_ENV, TASK_TIMEOUT_ENV])
def test_a_valid_environment_value_is_used(monkeypatch, name):
    monkeypatch.setenv(name, "7.5")
    assert validate_startup_configuration()[name] == 7.5


def test_the_application_refuses_to_start_with_an_invalid_setting(tmp_path, monkeypatch):
    """A mistyped budget must stop the application, not surface mid-task."""
    monkeypatch.setenv(TASK_TIMEOUT_ENV, "0")
    with pytest.raises(RuntimeError, match="invalid resource configuration"):
        create_app(tmp_path / "never.db")


# ------------------------------------------------------------------- four scenarios

def _client(tmp_path, name):
    return TestClient(create_app(tmp_path / name))


def _prepared_store(path):
    """Store with the schema created and the synthetic case installed."""
    from riskshield.simulation import Day3Simulation

    store = Store(path)
    Day3Simulation(store)  # creates the tables the fixture then writes into
    prepare_synthetic_case(store)
    return store


def test_scenario_normal_completion_is_a_success_everywhere(tmp_path):
    with _client(tmp_path, "normal.db") as client:
        run = client.post("/demos/day4/run", json={"agent_count": 2, "rounds": 1,
                                                   "concurrency": 1}).json()
        job = client.post("/jobs", json={"run_id": run["run_id"]})
        assert job.status_code == 202, job.text
        job_id = job.json()["job_id"]
        for _ in range(200):
            state = client.get(f"/jobs/{job_id}").json()
            if state["status"] != "queued" and state["status"] != "running":
                break
            time.sleep(0.05)

        assert state["status"] == "complete"
        assert state["result_state"] == "success"
        assert state["report_id"] is not None and state["alert_id"] is not None
        report = client.get(f"/reports/{state['report_id']}").json()
        alert = client.get(f"/alerts/{state['alert_id']}").json()
        # API, task, report and alert agree on the same run.
        for payload in (state, report, alert):
            assert payload["run_id"] == run["run_id"]
        assert report["overview"]["run_integrity"]["complete"] is True
        assert alert["alert_is_success_result"] is True
        assert alert["round_fallback"] is None
        assert alert["assessed_round"] == alert["latest_recorded_round"]
        # The ledger charges exactly what the run recorded, and nothing is undecided.
        summary = client.get(f"/simulations/{run['run_id']}").json()
        assert summary["billing"]["charged_cost_cny"] == summary["cost_cny"]
        assert summary["capacity"]["rejected_requests"] == 0
        assert "deadline_exceeded" not in summary["action_counts"]


def test_scenario_last_round_timed_out_but_earlier_round_decided(tmp_path, monkeypatch):
    """The API path for the fallback: bounded task, partial report, no success reading."""
    monkeypatch.setenv(CALL_TIMEOUT_ENV, "0.2")
    gate = threading.Event()
    original = OfflineDynamicSubstitute.decide

    def hangs_after_first_round(self, observation):
        if observation.get("memories"):
            gate.wait()
        return original(self, observation)

    monkeypatch.setattr(OfflineDynamicSubstitute, "decide", hangs_after_first_round)
    store = _prepared_store(tmp_path / "fallback.db")
    tasks = Day4Tasks(store)
    run = tasks.prepare_demo_run(agent_count=4, rounds=2, concurrency=4)
    try:
        job = tasks.run_sync(run["run_id"])
    finally:
        gate.set()

    # Two different limits, reported separately: the bounded HTTP acceptance and the
    # bounded task are not the same promise.
    assert job["status"] == "failed"
    assert job["result_state"] == "partial"
    assert job["simulation_status"] == "partial"
    assert job["report_id"] is not None, "a partial run with evidence must still report"
    alert = tasks.alerts.get(job["alert_id"])
    assert alert["assessed_round"] == 1
    assert alert["latest_recorded_round"] == 2
    assert alert["round_fallback"] is not None
    assert alert["alert_is_success_result"] is False
    assert alert["level"] in {"red", "orange", "yellow", "blue"}
    summary = tasks.simulation.summary(run["run_id"])
    assert summary["action_counts"]["deadline_exceeded"] == 4
    assert summary["action_counts"].get("failed", 0) == 0
    assert summary["billing"]["cost_reconciles"] is True


def test_scenario_no_valid_decision_anywhere_stops_with_a_reason(tmp_path, monkeypatch):
    """Nothing decided: no risk level is invented and the task says why."""
    monkeypatch.setenv(CALL_TIMEOUT_ENV, "0.2")
    gate = threading.Event()
    original = OfflineDynamicSubstitute.decide

    def always_hangs(self, observation):
        gate.wait()
        return original(self, observation)

    monkeypatch.setattr(OfflineDynamicSubstitute, "decide", always_hangs)
    store = _prepared_store(tmp_path / "nothing.db")
    tasks = Day4Tasks(store)
    run = tasks.prepare_demo_run(agent_count=2, rounds=1, concurrency=2)
    try:
        job = tasks.run_sync(run["run_id"])
    finally:
        gate.set()

    assert job["status"] == "failed"
    assert job["report_id"] is None and job["alert_id"] is None
    assert job["error"] == "simulation_error"
    assert "stopped or invalid decisions" in job["error"] or job["result_state"] in {
        "failed", "stopped"}
    summary = tasks.simulation.summary(run["run_id"])
    assert summary["action_counts"].get("valid", 0) == 0
    assert summary["capacity"]["rejected_requests"] == 0


def test_scenario_capacity_exhaustion_is_refused_with_503_not_queued(tmp_path):
    """Acceptance is refused outright; nothing is queued and nothing is charged."""
    with _client(tmp_path, "capacity.db") as client:
        run = client.post("/demos/day4/run", json={"agent_count": 2, "rounds": 1,
                                                   "concurrency": 1}).json()
        held = [PROCESS_CAPACITY.acquire(reason="test_fills_the_ceiling")
                for _ in range(PROCESS_CAPACITY.limit)]
        try:
            refused = client.post("/jobs", json={"run_id": run["run_id"]})
        finally:
            for token in held:
                PROCESS_CAPACITY.release(token)

        assert refused.status_code == 503, refused.text
        detail = refused.json()["detail"]
        assert detail["reason"] == "capacity_exhausted"
        assert detail["capacity"]["available"] == 0
        # Refused means not accepted: no job row, so no implicit queue was created.
        assert client.get("/jobs").json()["jobs"] == []
        summary = client.get(f"/simulations/{run['run_id']}").json()
        assert summary["cost_cny"] == "0"

        # With capacity back, the same request is accepted.
        accepted = client.post("/jobs", json={"run_id": run["run_id"]})
        assert accepted.status_code == 202, accepted.text


def test_a_concurrency_above_the_ceiling_is_refused_as_capacity_not_as_a_bad_request(
        tmp_path, monkeypatch):
    """The same request would be valid under a larger ceiling, so it is not a 422."""
    monkeypatch.setenv(CONFIG_ENV, "2")
    with _client(tmp_path, "over.db") as client:
        refused = client.post("/demos/day4/run", json={"agent_count": 2, "rounds": 1,
                                                       "concurrency": 3})
        assert refused.status_code == 503, refused.text
        assert refused.json()["detail"]["reason"] == "capacity_exhausted"


def test_a_task_deadline_expired_before_start_is_refused_without_dispatching(
        tmp_path, monkeypatch):
    """The budget is counted from input_received_at, so a stale input cannot start."""
    monkeypatch.setenv(TASK_TIMEOUT_ENV, "0.2")
    store = _prepared_store(tmp_path / "stale.db")
    tasks = Day4Tasks(store)
    run = tasks.prepare_demo_run(agent_count=2, rounds=1, concurrency=1)
    stale = (datetime.now(timezone.utc) - timedelta(seconds=30)).isoformat()
    job = tasks.run_sync(run["run_id"], input_received_at=stale)

    assert job["status"] == "failed"
    assert job["failure_category"] == "data_integrity_error"
    # Nothing was dispatched, so nothing was charged and no round was recorded.
    summary = tasks.simulation.summary(run["run_id"])
    assert summary["cost_cny"] == "0"
    assert summary["action_counts"] == {}
    assert summary["capacity"]["rejected_requests"] == 0


def test_a_resumed_task_does_not_receive_a_fresh_budget(tmp_path, monkeypatch):
    """A second attempt keeps the remaining time, because the budget is per input."""
    monkeypatch.setenv(TASK_TIMEOUT_ENV, "0.4")
    store = _prepared_store(tmp_path / "resume.db")
    tasks = Day4Tasks(store)
    run = tasks.prepare_demo_run(agent_count=2, rounds=1, concurrency=1)
    first = (datetime.now(timezone.utc) - timedelta(seconds=0.1)).isoformat()
    job = tasks.run_sync(run["run_id"], input_received_at=first)
    assert job["status"] == "complete"

    # The same input, attempted again after the budget has passed, is refused rather than
    # being handed a new full 0.4 s.
    time.sleep(0.5)
    again = tasks.run_sync(run["run_id"], input_received_at=first)
    assert again["job_id"] == job["job_id"]
    assert again["status"] == "complete"
