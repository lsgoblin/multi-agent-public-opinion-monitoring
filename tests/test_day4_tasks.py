import json
import socket
import threading

import pytest

from riskshield.day4_tasks import (
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


def test_real_historical_run_is_rejected_before_a_job_is_created(tmp_path):
    tasks = _tasks(tmp_path)
    case = CaseImport.model_validate({
        "case_id": "day4_real_rejection_case",
        "title": "Historical source kept outside offline demo execution",
        "scope": "Validation only",
        "cutoff": "2024-01-01T10:30:00+08:00",
        "cutoff_basis": "Archived source time plus 30 minutes.",
        "data_mode": "real_historical",
        "version": "v1",
        "records": [{
            "record_id": "archived_source",
            "channel": "news",
            "source_url": "https://example.com/archived-source",
            "publisher": "Example archive",
            "title": "Archived source",
            "summary": "An archived historical source used only to verify rejection.",
            "data_mode": "real_historical",
            "published_at": "2024-01-01T10:00:00+08:00",
            "available_at": "2024-01-01T10:00:00+08:00",
            "collected_at": "2024-01-01T10:01:00+08:00",
            "availability_basis": "Archived timestamp.",
            "historical_integrity": "archived",
            "acquisition_method": "manual_web_review",
            "role": "input_candidate",
        }],
    })
    tasks.store.import_case(case)
    snapshot = tasks.store.snapshot(case.case_id)
    graph_id = "day4_real_rejection_graph"
    graph = {
        "case_id": case.case_id,
        "cutoff": snapshot["cutoff"],
        "source_version": snapshot["version"],
        "nodes": [],
        "edges": [],
        "claims": [{
            "claim_id": "claim:archived_source",
            "record_id": "archived_source",
            "text": case.records[0].summary,
            "title": case.records[0].title,
            "source_url": str(case.records[0].source_url),
            "available_at": case.records[0].available_at.isoformat(),
            "source_node_id": "source:archived_source",
            "publisher_node_id": "publisher:example",
            "observation_ids": [],
            "observation_node_ids": [],
            "evidence_adapters": ["archive"],
        }],
        "excluded_counts": snapshot["excluded_counts"],
    }
    with tasks.store.connect() as db:
        db.execute("INSERT INTO graphs VALUES (?, ?, ?, ?, ?, ?)", (
            graph_id, case.case_id, snapshot["cutoff"], snapshot["version"],
            "2026-10-07T00:00:00+00:00", json.dumps(graph),
        ))
    run_id = tasks.simulation.create_run(
        case.case_id, graph_id, agent_count=1, rounds=1, concurrency=1
    )

    with pytest.raises(TaskError, match="synthetic"):
        tasks.start(run_id)
    assert tasks.list() == []
