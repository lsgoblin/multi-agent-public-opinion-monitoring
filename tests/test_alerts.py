import json
import socket

import pytest

from riskshield.evidence import EvidencePipeline
from riskshield.simulation import Day3Simulation, DecisionResult
from riskshield.alerts import AlertError, Day4Alerts, RULE_VERSION
from riskshield.schemas import AgentDecision, CaseImport
from riskshield.store import Store
from tests.fixtures.day3_synthetic_case import (
    CASE_ID, GRAPH_ID, RECORD_ID, prepare_synthetic_case,
)


class FixedActions:
    mode = "test_substitute"

    def __init__(self, complaint_count):
        self.complaint_count = complaint_count

    def decide(self, observation):
        index = int(observation["agent_id"].split("_")[-1])
        action = "express_complaint_intent" if index < self.complaint_count else "observe"
        return DecisionResult(AgentDecision(
            agent_id=observation["agent_id"], action=action,
            evidence_ids=[observation["evidence"][0]["evidence_id"]],
            reason="A deterministic offline test decision based on visible synthetic evidence.",
        ), usage={"prompt_tokens": 1, "completion_tokens": 1}, model="offline-test")


def _run(store, case_id=CASE_ID, graph_id=GRAPH_ID, complaint_count=0):
    sim = Day3Simulation(store)
    run_id = sim.create_run(case_id, graph_id, agent_count=20, rounds=1)
    assert sim.advance(run_id, FixedActions(complaint_count))["status"] == "complete"
    return run_id


def _prepared_store(path):
    store = Store(path)
    EvidencePipeline(store)
    prepare_synthetic_case(store)
    return store


@pytest.mark.parametrize("complaints,expected", [
    (8, "red"), (4, "orange"), (1, "yellow"), (0, "blue"),
])
def test_provisional_four_levels_and_traceability(tmp_path, complaints, expected):
    store = _prepared_store(tmp_path / "alerts.db")
    run_id = _run(store, complaint_count=complaints)
    alerts = Day4Alerts(store)
    alert = alerts.assess(run_id)

    assert alert["level"] == expected
    assert alert["rule_version"] == RULE_VERSION
    assert alert["data_mode"] == "synthetic"
    assert alert["metrics"]["simulated_complaint_intent_decisions"] == complaints
    assert alert["evidence_refs"]["source_record_ids"] == [RECORD_ID]
    assert alert["evidence_refs"]["action_refs"]
    assert {ref["run_id"] for ref in alert["evidence_refs"]["action_refs"]} == {run_id}
    assert alert["discovered_at"] == alert["assessed_at"]
    assert alert["sent_at"] is None
    assert alert["run_complete"] is True
    assert alert["decisions_all_valid"] is True
    assert alert["alert_is_success_result"] is True
    assert alerts.get(alert["alert_id"]) == alert
    assert alerts.assess(run_id) == alert

    for channel in ("wecom", "email"):
        preview = alerts.preview_delivery(alert["alert_id"], channel)
        assert preview["delivery_status"] == "dry_run"
        assert preview["network_requests"] == 0
        assert preview["sent_at"] is None and preview["recipient"] is None
        assert preview["alert_is_success_result"] is True
        assert preview["receipt_status"] == "not_requested"
        assert preview["receipt_at"] is None and preview["delivered_at"] is None
        assert preview["discovered_at"] == alert["discovered_at"]
        assert preview["discovery_to_preview_ms"] >= 0
        assert preview["latency_basis"] == "local_rule_assessment_to_preview_generation"
        if channel == "wecom":
            assert preview["payload"]["msgtype"] == "text"
            assert expected.upper() in preview["payload"]["text"]["content"]
            assert "OFFLINE PREVIEW ONLY" in preview["payload"]["text"]["content"]
        else:
            assert preview["payload"]["subject"].startswith(
                f"[OFFLINE PREVIEW] {expected.upper()}")
            assert preview["payload"]["to_alias"] is None
            assert "OFFLINE PREVIEW ONLY" in preview["payload"]["body_text"]


def test_changed_ledger_creates_new_alert_without_rewriting_old_assessment(tmp_path):
    store = _prepared_store(tmp_path / "alerts.db")
    run_id = _run(store, complaint_count=4)
    alerts = Day4Alerts(store)
    original = alerts.assess(run_id)
    with store.connect() as db:
        row = db.execute(
            "SELECT decision FROM simulation_actions WHERE run_id=? AND round_number=1 "
            "AND agent_id='agent_0004'", (run_id,)).fetchone()
        decision = json.loads(row["decision"])
        decision["action"] = "express_complaint_intent"
        db.execute(
            "UPDATE simulation_actions SET decision=? WHERE run_id=? AND round_number=1 "
            "AND agent_id='agent_0004'", (json.dumps(decision), run_id))
    revised = alerts.assess(run_id)
    assert revised["alert_id"] != original["alert_id"]
    assert revised["metrics"]["simulated_complaint_intent_decisions"] == 5
    assert alerts.get(original["alert_id"]) == original


def test_previews_persist_without_any_send_or_recipient(tmp_path, monkeypatch):
    store = _prepared_store(tmp_path / "alerts.db")
    run_id = _run(store, complaint_count=8)
    alerts = Day4Alerts(store)
    alert = alerts.assess(run_id)

    def forbidden_socket(*args, **kwargs):
        raise AssertionError("notification preview attempted a network call")

    monkeypatch.setattr(socket, "socket", forbidden_socket)
    for channel in ("wecom", "email"):
        preview = alerts.preview_delivery(alert["alert_id"], channel)
        assert preview["delivery_status"] == "dry_run"
        assert preview["network_requests"] == 0
        assert preview["recipient"] is None and preview["sent_at"] is None
        assert preview["discovered_at"] == alert["discovered_at"]
        assert preview["discovery_to_preview_ms"] >= 0
        if channel == "wecom":
            assert set(preview["payload"]) == {"msgtype", "text"}
            assert preview["payload"]["msgtype"] == "text"
            assert "OFFLINE PREVIEW ONLY" in preview["payload"]["text"]["content"]
        else:
            assert set(preview["payload"]) == {"subject", "body_text", "to_alias"}
            assert preview["payload"]["subject"].startswith("[OFFLINE PREVIEW]")
            assert preview["payload"]["to_alias"] is None
            assert "OFFLINE PREVIEW ONLY" in preview["payload"]["body_text"]
        with store.connect() as db:
            row = db.execute("SELECT body FROM day4_delivery_previews WHERE preview_id=?",
                             (preview["preview_id"],)).fetchone()
        assert json.loads(row["body"]) == preview
    with pytest.raises(AlertError):
        alerts.preview_delivery(alert["alert_id"], "webhook")
    with pytest.raises(KeyError):
        alerts.get("alert_missing")


def test_assessment_rejects_cross_case_graph_and_empty_run(tmp_path):
    store = _prepared_store(tmp_path / "alerts.db")
    sim = Day3Simulation(store)
    alerts = Day4Alerts(store)
    empty = sim.create_run(CASE_ID, GRAPH_ID, agent_count=1, rounds=1)
    with pytest.raises(AlertError, match="no completed round"):
        alerts.assess(empty)
    with pytest.raises(KeyError):
        alerts.assess("run_missing")

    cloned = store.get_case(CASE_ID)
    cloned["case_id"] = "different_synthetic_case"
    cloned["records"] = [r.model_dump(mode="json") for r in store.records(CASE_ID)]
    store.import_case(CaseImport.model_validate(cloned))
    own_graph = "different_synthetic_graph"
    with store.connect() as db:
        original = db.execute("SELECT body FROM graphs WHERE graph_id=?", (GRAPH_ID,)).fetchone()
        body = json.loads(original["body"])
        body["case_id"] = cloned["case_id"]
        db.execute("INSERT INTO graphs VALUES (?, ?, ?, ?, ?, ?)", (
            own_graph, cloned["case_id"], cloned["cutoff"], cloned["version"],
            "2026-10-07T00:00:00+00:00", json.dumps(body),
        ))
    run_id = _run(store, cloned["case_id"], own_graph, complaint_count=4)
    with store.connect() as db:
        db.execute("UPDATE simulation_runs SET graph_id=? WHERE run_id=?", (GRAPH_ID, run_id))
    with pytest.raises(AlertError, match="do not match"):
        alerts.assess(run_id)


@pytest.mark.parametrize("run_status,action_status,failed,undecided", [
    ("partial", "valid", 0, 0),
    ("complete", "failed", 1, 0),
    ("partial", "deadline_exceeded", 0, 1),
    ("partial", "not_dispatched", 0, 1),
    ("complete", "deadline_exceeded", 0, 1),
    ("complete", "unknown_contract_state", 0, 0),
])
def test_incomplete_alert_and_preview_never_claim_full_success(
        tmp_path, run_status, action_status, failed, undecided):
    store = _prepared_store(tmp_path / "alerts.db")
    run_id = _run(store, complaint_count=8)
    with store.connect() as db:
        db.execute("UPDATE simulation_runs SET status=? WHERE run_id=?", (run_status, run_id))
        if action_status != "valid":
            # Persist the additive simulation-scale action contract without a live call.
            db.execute("UPDATE simulation_actions SET status=?, decision=NULL WHERE run_id=? "
                       "AND agent_id='agent_0019'", (action_status, run_id))
    alerts = Day4Alerts(store)
    alert = alerts.assess(run_id)
    assert alert["status"] == "provisional_offline_assessment"
    assert alert["level"] == "red"
    assert alert["alert_is_success_result"] is False
    assert alert["metrics"]["run_status"] == run_status
    assert alert["metrics"]["run_failed_decisions"] == failed
    assert alert["metrics"]["latest_round_failed_decisions"] == failed
    assert alert["metrics"]["run_undecided_requests"] == undecided
    assert alert["metrics"]["latest_round_undecided_requests"] == undecided
    assert alert["run_action_counts"][action_status] >= 1
    for channel in ("wecom", "email"):
        preview = alerts.preview_delivery(alert["alert_id"], channel)
        assert preview["delivery_status"] == "dry_run"
        assert preview["alert_is_success_result"] is False
        assert preview["run_status"] == run_status
        assert preview["receipt_status"] == "not_requested"
        assert preview["receipt_at"] is None and preview["delivered_at"] is None
        message = (preview["payload"]["text"]["content"] if channel == "wecom"
                   else preview["payload"]["body_text"])
        assert "INCOMPLETE OR UNVERIFIED RUN" in message
        assert run_status in message


@pytest.mark.parametrize("incomplete_ledger", ["missing_action", "missing_round"])
def test_complete_status_requires_configured_rounds_and_full_action_count(tmp_path, incomplete_ledger):
    store = _prepared_store(tmp_path / "alerts.db")
    run_id = _run(store)
    with store.connect() as db:
        if incomplete_ledger == "missing_action":
            db.execute("DELETE FROM simulation_actions WHERE run_id=? AND agent_id='agent_0019'",
                       (run_id,))
        else:
            row = db.execute("SELECT config FROM simulation_runs WHERE run_id=?", (run_id,)).fetchone()
            config = json.loads(row["config"])
            config["rounds"] = 2
            db.execute("UPDATE simulation_runs SET config=? WHERE run_id=?",
                       (json.dumps(config), run_id))
    alert = Day4Alerts(store).assess(run_id)
    assert alert["alert_is_success_result"] is False


@pytest.mark.parametrize("run_status", ["running", "failed"])
def test_active_or_failed_run_cannot_be_assessed_as_a_finished_result(tmp_path, run_status):
    store = _prepared_store(tmp_path / "alerts.db")
    run_id = _run(store)
    with store.connect() as db:
        db.execute("UPDATE simulation_runs SET status=? WHERE run_id=?", (run_status, run_id))
    with pytest.raises(AlertError, match="complete or explicitly partial"):
        Day4Alerts(store).assess(run_id)


def test_legacy_assessment_without_integrity_flags_is_unverified_in_preview(tmp_path):
    store = _prepared_store(tmp_path / "alerts.db")
    alerts = Day4Alerts(store)
    alert = alerts.assess(_run(store))
    for key in ("run_complete", "decisions_all_valid", "alert_is_success_result"):
        alert.pop(key, None)
    with store.connect() as db:
        db.execute("UPDATE day4_alerts SET body=? WHERE alert_id=?",
                   (json.dumps(alert), alert["alert_id"]))
    preview = alerts.preview_delivery(alert["alert_id"], "wecom")
    assert preview["alert_is_success_result"] is False
    assert preview["run_complete"] is None and preview["decisions_all_valid"] is None
    assert "INCOMPLETE OR UNVERIFIED RUN" in preview["payload"]["text"]["content"]


def test_round_without_any_valid_decision_cannot_produce_a_blue_alert(tmp_path):
    store = _prepared_store(tmp_path / "alerts.db")
    run_id = _run(store)
    with store.connect() as db:
        db.execute("UPDATE simulation_runs SET status='partial' WHERE run_id=?", (run_id,))
        db.execute("UPDATE simulation_actions SET status='deadline_exceeded', decision=NULL "
                   "WHERE run_id=?", (run_id,))
    with pytest.raises(AlertError, match="no valid decisions"):
        Day4Alerts(store).assess(run_id)
