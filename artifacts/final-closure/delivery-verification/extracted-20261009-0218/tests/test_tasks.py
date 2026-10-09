import json
import socket
import threading
from pathlib import Path

import pytest

import pytest

from riskshield.tasks import (
    DEMO_CASE_ID,
    DEMO_GRAPH_ID,
    TASK_MODE,
    Day4Tasks,
    OfflineDynamicSubstitute,
    TaskError,
)
from riskshield.schemas import CaseImport
from riskshield.store import Store


def _tasks(tmp_path):
    return Day4Tasks(Store(tmp_path / "day4-tasks.db"))


def test_prepare_demo_run_is_stable_but_returns_a_fresh_created_run(tmp_path):
    tasks = _tasks(tmp_path)
    first = tasks.prepare_demo_run(agent_count=3, rounds=2, concurrency=2)
    second = tasks.prepare_demo_run(agent_count=3, rounds=2, concurrency=2)

    assert first["run_id"] != second["run_id"]
    assert first["case_id"] == second["case_id"] == DEMO_CASE_ID
    assert first["graph_id"] == second["graph_id"] == DEMO_GRAPH_ID
    assert first["status"] == second["status"] == "created"
    assert first["config"]["data_mode"] == "synthetic"
    with tasks.store.connect() as db:
        assert db.execute(
            "SELECT COUNT(*) AS n FROM cases WHERE case_id=?", (DEMO_CASE_ID,)
        ).fetchone()["n"] == 1
        assert db.execute(
            "SELECT COUNT(*) AS n FROM graphs WHERE graph_id=?", (DEMO_GRAPH_ID,)
        ).fetchone()["n"] == 1


def test_sync_task_uses_dynamic_messages_and_memory_then_builds_alert_and_report(
        tmp_path, monkeypatch):
    tasks = _tasks(tmp_path)
    run = tasks.prepare_demo_run(agent_count=10, rounds=3, concurrency=4)

    def forbidden_socket(*args, **kwargs):
        raise AssertionError("offline Day 4 task attempted a network request")

    monkeypatch.setattr(socket, "socket", forbidden_socket)
    job = tasks.run_sync(run["run_id"])

    assert job["status"] == "complete"
    assert job["mode"] == TASK_MODE
    assert job["model_calls"] == 0
    assert job["network_requests"] == 0
    assert job["completed_rounds"] == job["target_rounds"] == 3
    assert job["progress"] == 1.0
    assert job["simulation_status"] == "complete"
    assert job["alert_id"].startswith("alert_")
    assert job["report_id"].startswith("report_")

    trajectory = tasks.simulation.trajectory(run["run_id"])
    round_two = [row for row in trajectory if row["round_number"] == 2]
    assert any(row["observation_refs"]["messages"] for row in round_two)
    assert any(row["decision"]["action"] == "comment" and
               row["decision"]["evidence_ids"][0].startswith("msg_")
               for row in round_two)
    assert any(row["observation_refs"]["memories"] for row in round_two)
    assert any(row["decision"]["action"] == "seek_clarification" and
               row["decision"]["evidence_ids"][0].startswith("mem_")
               for row in round_two)
    report = tasks.reports.get(job["report_id"])
    assert report["modules"]["risk"]["alert_id"] == job["alert_id"]
    assert report["modules"]["propagation"]["total_messages"] > 0
    assert tasks.alerts.get(job["alert_id"])["data_mode"] == "synthetic"


def test_duplicate_start_is_idempotent_while_active_and_after_completion(tmp_path, monkeypatch):
    tasks = _tasks(tmp_path)
    run = tasks.prepare_demo_run(agent_count=3, rounds=2, concurrency=3)
    entered = threading.Event()
    release = threading.Event()
    original = OfflineDynamicSubstitute.decide

    def paused_decision(self, observation):
        entered.set()
        assert release.wait(5)
        return original(self, observation)

    monkeypatch.setattr(OfflineDynamicSubstitute, "decide", paused_decision)
    first = tasks.start(run["run_id"])
    assert entered.wait(5)
    active_duplicate = tasks.start(run["run_id"])
    assert active_duplicate["job_id"] == first["job_id"]
    assert active_duplicate["status"] in {"queued", "running"}
    release.set()
    completed = tasks.wait(first["job_id"], timeout=10)
    assert completed["status"] == "complete"
    completed_duplicate = tasks.start(run["run_id"])
    assert completed_duplicate["job_id"] == first["job_id"]
    assert completed_duplicate["status"] == "complete"
    assert len(tasks.list()) == 1


def test_failure_is_visible_and_only_a_bounded_category_is_persisted(tmp_path):
    tasks = _tasks(tmp_path)
    run = tasks.prepare_demo_run(agent_count=2, rounds=1, concurrency=2)
    with tasks.store.connect() as db:
        db.execute("UPDATE graphs SET body=? WHERE graph_id=?",
                   (json.dumps({"private": "must not appear in failure"}), DEMO_GRAPH_ID))

    job = tasks.run_sync(run["run_id"])

    assert job["status"] == "failed"
    assert job["failure_category"] == "data_integrity_error"
    assert job["error"] == "data_integrity_error"
    assert 0.0 <= job["progress"] <= 1.0
    assert job["alert_id"] is None and job["report_id"] is None
    assert "private" not in json.dumps(job)


def test_completed_round_with_failed_decision_is_not_a_successful_job(tmp_path, monkeypatch):
    tasks = _tasks(tmp_path)
    run = tasks.prepare_demo_run(agent_count=2, rounds=1, concurrency=2)
    original = OfflineDynamicSubstitute.decide

    def one_failed_decision(self, observation):
        if observation["agent_id"].endswith("0000"):
            raise RuntimeError("controlled offline failure")
        return original(self, observation)

    monkeypatch.setattr(OfflineDynamicSubstitute, "decide", one_failed_decision)
    job = tasks.run_sync(run["run_id"])

    assert job["simulation_status"] == "complete"
    assert job["all_decisions_valid"] is False
    assert job["action_counts"].get("failed", 0) == 1
    assert job["status"] == "failed"
    assert job["result_state"] == "partial"
    assert job["report_id"] is None and job["alert_id"] is None


def test_stopped_simulation_is_distinct_from_execution_failure(tmp_path, monkeypatch):
    tasks = _tasks(tmp_path)
    run = tasks.prepare_demo_run(agent_count=2, rounds=2, concurrency=2)
    queued, _ = tasks._queue(run["run_id"])
    with tasks.store.connect() as db:
        db.execute("UPDATE day4_tasks SET status='failed', "
                   "failure_category='simulation_error' WHERE job_id=?",
                   (queued["job_id"],))
    summary = tasks.simulation.summary(run["run_id"])
    summary["status"] = "partial"
    monkeypatch.setattr(tasks.simulation, "summary", lambda _run_id: summary)

    job = tasks.get(queued["job_id"])

    assert job["status"] == "failed"
    assert job["result_state"] == "stopped"
    assert job["all_decisions_valid"] is False


def test_completed_jobs_persist_and_stale_active_jobs_fail_on_restart(tmp_path):
    path = tmp_path / "restart.db"
    first = Day4Tasks(Store(path))
    complete_run = first.prepare_demo_run(agent_count=2, rounds=1, concurrency=2)
    completed = first.run_sync(complete_run["run_id"])
    stale_run = first.prepare_demo_run(agent_count=2, rounds=1, concurrency=2)
    stale, created = first._queue(stale_run["run_id"])
    assert created and stale["status"] == "queued"

    restarted = Day4Tasks(Store(path))

    assert restarted.get(completed["job_id"])["status"] == "complete"
    interrupted = restarted.get(stale["job_id"])
    assert interrupted["status"] == "failed"
    assert interrupted["failure_category"] == "interrupted_on_restart"
    assert interrupted["completed_at"] is not None


def test_real_historical_case_runs_only_from_archived_cutoff_inputs(tmp_path, monkeypatch):
    tasks = _tasks(tmp_path)
    package_path = (Path(__file__).resolve().parents[1] / "data/public/"
                    "unh_change_20240222_day2_multisource_event.json")
    package = json.loads(package_path.read_text(encoding="utf-8"))
    projection = CaseImport.model_validate(package["case_import_projection"])
    tasks.store.import_case(projection)

    def forbidden_socket(*args, **kwargs):
        raise AssertionError("historical substitute attempted a network request")

    monkeypatch.setattr(socket, "socket", forbidden_socket)
    job = tasks.start_historical_case(projection.case_id, agent_count=5, rounds=3,
                                      concurrency=2)
    job = tasks.wait(job["job_id"], timeout=10)
    assert job["status"] == "complete", job
    assert job["mode"] == TASK_MODE
    assert job["execution_mode"] == "offline_dynamic_substitute"
    assert job["data_mode"] == "real_historical"
    assert job["model_calls"] == job["network_requests"] == 0
    assert job["input_received_at"] and job["started_at"] and job["report_generated_at"]
    assert job["elapsed_seconds"] >= 0

    report = tasks.reports.get(job["report_id"])
    allowed_sources = {"unh-sec-20240222-initial", "optum-status-20240221-cyber-update"}
    assert report["case_id"] == projection.case_id == job["case_id"]
    assert report["graph_id"] == job["graph_id"]
    assert report["run_id"] == job["run_id"]
    assert report["data_mode"] == "real_historical"
    assert report["execution_mode"] == "offline_dynamic_substitute"
    assert report["graph_build_mode"] == "offline_archived_record_projection"
    assert report["evidence"]["source_record_ids"] == sorted(allowed_sources)
    assert "unh-sec-20240308-update" not in json.dumps(report)
    assert any("离线替身" in item and "不证明真实模型" in item
               for item in report["limitations"])
    graph = tasks.evidence.graph(job["graph_id"])
    assert {claim["record_id"] for claim in graph["claims"]} == allowed_sources
    assert graph["excluded_counts"]["not_input"] == 1

    trajectory = tasks.simulation.trajectory(job["run_id"])
    assert any(row["observation_refs"]["messages"] for row in trajectory
               if row["round_number"] > 1)
    assert any(row["observation_refs"]["memories"] for row in trajectory
               if row["round_number"] > 1)
    assert all(set(row["observation_refs"]["evidence"]) <= allowed_sources
               for row in trajectory)
    assert tasks.start(job["run_id"])["job_id"] == job["job_id"]
    assert len(tasks.list()) == 1


def test_future_graph_claim_fails_before_any_agent_observes_it(tmp_path):
    tasks = _tasks(tmp_path)
    package_path = (Path(__file__).resolve().parents[1] / "data/public/"
                    "unh_change_20240222_day2_multisource_event.json")
    package = json.loads(package_path.read_text(encoding="utf-8"))
    case = CaseImport.model_validate(package["case_import_projection"])
    tasks.store.import_case(case)
    graph = tasks._build_archived_projection(case.case_id)
    run_id = tasks.simulation.create_run(
        case.case_id, graph["graph_id"], agent_count=1, rounds=1, concurrency=1)
    graph["claims"][0]["available_at"] = "2027-01-01T00:00:00+00:00"
    with tasks.store.connect() as db:
        db.execute("UPDATE graphs SET body=? WHERE graph_id=?",
                   (json.dumps({key: value for key, value in graph.items() if key != "graph_id"}),
                    graph["graph_id"]))

    job = tasks.start(run_id)
    job = tasks.wait(job["job_id"], timeout=10)
    assert job["status"] == "failed"
    assert job["failure_category"] == "data_integrity_error"
    assert tasks.simulation.trajectory(run_id) == []
