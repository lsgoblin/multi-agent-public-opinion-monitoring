"""The Day 4 API joins one synthetic run, report, alert, and dry-run preview."""

from decimal import Decimal
import json
import time
from datetime import datetime
from pathlib import Path

from fastapi.testclient import TestClient

from riskshield.api import create_app
from riskshield.day3 import Day3Simulation, DecisionResult
from riskshield.schemas import AgentDecision, CaseImport
from riskshield.store import Store
from tests.fixtures.day3_synthetic_case import CASE_ID, GRAPH_ID, prepare_synthetic_case


class OfflineBackend:
    mode = "test_substitute"
    reservation_cny = Decimal("0")
    input_cny_per_million = Decimal("0")
    output_cny_per_million = Decimal("0")
    price_version = "offline-test"

    def decide(self, observation):
        return DecisionResult(AgentDecision(
            agent_id=observation["agent_id"], action="share",
            evidence_ids=[observation["evidence"][0]["evidence_id"]],
            reason="Share the fictional notice in this offline test.",
        ), usage={"prompt_tokens": 1, "completion_tokens": 1}, model="offline-test")


def test_report_alert_and_notification_preview_share_one_synthetic_run(tmp_path):
    store = Store(tmp_path / "day4.db")
    simulation = Day3Simulation(store)
    prepare_synthetic_case(store)
    run_id = simulation.create_run(CASE_ID, GRAPH_ID, agent_count=3, rounds=2)
    simulation.advance(run_id, OfflineBackend())

    with TestClient(create_app(store.path)) as client:
        readiness = client.get("/readiness").json()
        assert readiness["g3_v2_passed"] is False
        assert readiness["g4_v2_passed"] is False
        listed = client.get("/simulations")
        assert listed.status_code == 200
        assert [run["run_id"] for run in listed.json()["runs"]] == [run_id]

        created = client.post("/reports", json={"run_id": run_id})
        assert created.status_code == 201, created.text
        report = created.json()
        assert report["run_id"] == run_id
        assert report["case_id"] == CASE_ID
        assert report["graph_id"] == GRAPH_ID
        assert report["data_mode"] == "synthetic"
        for module in report["modules"].values():
            assert module["run_id"] == run_id
            assert module["case_id"] == CASE_ID
            assert module["graph_id"] == GRAPH_ID
        assert set(report["modules"]) == {
            "propagation", "emotion_evolution", "key_nodes", "risk", "recommendations",
        }
        assert report["modules"]["propagation"]["kind"] == "simulated_runtime_messages"
        emotion = report["modules"]["emotion_evolution"]
        assert emotion["reconstruction_status"] == "validated_against_final_agent_state"
        assert len(emotion["series"]) == 2
        alert = report["modules"]["risk"]
        assert alert["level"] in {"red", "orange", "yellow", "blue"}
        assert alert["status"] == "provisional_offline_assessment"
        assert client.get(f"/reports/{report['report_id']}").json() == report
        assert client.get(f"/alerts/{alert['alert_id']}").json() == alert

        for channel in ("wecom", "email"):
            preview = client.post(f"/alerts/{alert['alert_id']}/previews",
                                  json={"channel": channel})
            assert preview.status_code == 201, preview.text
            body = preview.json()
            assert body["delivery_status"] == "dry_run"
            assert body["network_requests"] == 0
            assert body["sent_at"] is None
            assert body["recipient"] is None
        assert client.post(f"/alerts/{alert['alert_id']}/previews",
                           json={"channel": "sms"}).status_code == 422


def test_report_requires_a_completed_or_partial_run(tmp_path):
    store = Store(tmp_path / "day4.db")
    simulation = Day3Simulation(store)
    prepare_synthetic_case(store)
    run_id = simulation.create_run(CASE_ID, GRAPH_ID, agent_count=1, rounds=1)
    with TestClient(create_app(store.path)) as client:
        assert client.post("/reports", json={"run_id": run_id}).status_code == 422
        assert client.post("/alerts/assess", json={"run_id": run_id}).status_code == 422


def test_api_prepares_runs_and_completes_offline_demo_job(tmp_path):
    with TestClient(create_app(tmp_path / "day4-job.db")) as client:
        prepared = client.post("/demos/day4/run", json={
            "agent_count": 5, "rounds": 2, "concurrency": 2,
        })
        assert prepared.status_code == 201, prepared.text
        run = prepared.json()
        assert run["status"] == "created"
        assert run["config"]["data_mode"] == "synthetic"

        started = client.post("/jobs", json={"run_id": run["run_id"]})
        assert started.status_code == 202, started.text
        job_id = started.json()["job_id"]
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            job = client.get(f"/jobs/{job_id}").json()
            if job["status"] in {"complete", "failed"}:
                break
            time.sleep(0.02)
        assert job["status"] == "complete", job
        assert job["completed_rounds"] == job["target_rounds"] == 2
        assert job["progress"] == 1.0
        assert job["mode"] == "offline_dynamic_substitute"
        assert job["model_calls"] == 0
        assert job["network_requests"] == 0
        assert client.get(f"/reports/{job['report_id']}").status_code == 200
        assert client.get(f"/alerts/{job['alert_id']}").status_code == 200
        listed = client.get("/jobs").json()["jobs"]
        assert [row["job_id"] for row in listed] == [job_id]

        duplicate = client.post("/jobs", json={"run_id": run["run_id"]})
        assert duplicate.status_code == 202
        assert duplicate.json()["job_id"] == job_id


def test_historical_api_runs_archived_case_end_to_end_with_offline_substitute(
        tmp_path):
    db_path = tmp_path / "historical-day4.db"
    package_path = (Path(__file__).resolve().parents[1] / "data/public/"
                    "unh_change_20240222_day2_multisource_event.json")
    package = json.loads(package_path.read_text(encoding="utf-8"))
    case = CaseImport.model_validate(package["case_import_projection"])
    Store(db_path).import_case(case)

    with TestClient(create_app(db_path)) as client:
        snapshot = client.get(f"/cases/{case.case_id}/snapshot").json()
        assert {row["record_id"] for row in snapshot["records"]} == {
            "unh-sec-20240222-initial", "optum-status-20240221-cyber-update",
        }
        assert snapshot["excluded_counts"]["not_input"] == 1

        started = client.post("/day4/historical/jobs", json={
            "case_id": case.case_id, "agent_count": 5, "rounds": 3, "concurrency": 2,
        })
        assert started.status_code == 202, started.text
        job = started.json()
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            job = client.get(f"/jobs/{job['job_id']}").json()
            if job["status"] in {"complete", "failed"}:
                break
            time.sleep(0.01)
        assert job["status"] == "complete", job
        assert job["data_mode"] == "real_historical"
        assert job["execution_mode"] == "offline_dynamic_substitute"
        assert job["model_calls"] == job["network_requests"] == 0
        assert job["completed_rounds"] == job["target_rounds"] == 3
        assert job["elapsed_seconds"] >= 0
        assert job["report_generated_at"]
        times = [datetime.fromisoformat(job[key]) for key in
                 ("input_received_at", "started_at", "report_generated_at", "completed_at")]
        assert times == sorted(times)

        report = client.get(f"/reports/{job['report_id']}").json()
        assert report["binding"] == {
            "run_id": job["run_id"], "case_id": case.case_id, "graph_id": job["graph_id"],
        }
        assert report["data_mode"] == "real_historical"
        assert report["execution_mode"] == "offline_dynamic_substitute"
        assert report["graph_build_mode"] == "offline_archived_record_projection"
        for module in report["modules"].values():
            assert module["run_id"] == job["run_id"]
            assert module["case_id"] == case.case_id
            assert module["graph_id"] == job["graph_id"]
        assert set(report["evidence"]["source_record_ids"]) == {
            "unh-sec-20240222-initial", "optum-status-20240221-cyber-update",
        }
        assert "unh-sec-20240308-update" not in json.dumps(report)
        assert any("does not prove real-model" in item for item in report["limitations"])
        trajectory = client.get(f"/simulations/{job['run_id']}/trajectory").json()["actions"]
        allowed = set(report["evidence"]["source_record_ids"])
        assert trajectory and all(set(row["observation_refs"]["evidence"]) <= allowed
                                  for row in trajectory)
        assert any(row["observation_refs"]["messages"] for row in trajectory
                   if row["round_number"] > 1)
        assert any(row["observation_refs"]["memories"] for row in trajectory
                   if row["round_number"] > 1)

        alert = client.get(f"/alerts/{job['alert_id']}").json()
        assert alert["run_id"] == job["run_id"]
        assert alert["case_id"] == case.case_id
        assert alert["graph_id"] == job["graph_id"]
        for channel in ("wecom", "email"):
            preview_response = client.post(f"/alerts/{job['alert_id']}/previews",
                                            json={"channel": channel})
            assert preview_response.status_code == 201, preview_response.text
            preview = preview_response.json()
            assert preview["delivery_status"] == "dry_run"
            assert preview["network_requests"] == 0
            assert preview["recipient"] is None and preview["sent_at"] is None
            assert preview["discovery_to_preview_ms"] >= 0
            if channel == "wecom":
                assert preview["payload"]["msgtype"] == "text"
                assert alert["level"].upper() in preview["payload"]["text"]["content"]
            else:
                assert alert["level"].upper() in preview["payload"]["subject"]
                assert preview["payload"]["to_alias"] is None

        duplicate = client.post("/jobs", json={"run_id": job["run_id"]})
        assert duplicate.status_code == 202
        assert duplicate.json()["job_id"] == job["job_id"]
        assert len(client.get("/jobs").json()["jobs"]) == 1
