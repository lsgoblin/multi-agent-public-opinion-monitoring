"""The Day 4 API joins one synthetic run, report, alert, and dry-run preview."""

from decimal import Decimal
import time

from fastapi.testclient import TestClient

from riskshield.api import create_app
from riskshield.day3 import Day3Simulation, DecisionResult
from riskshield.schemas import AgentDecision
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
        assert report["data_mode"] == "synthetic"
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
