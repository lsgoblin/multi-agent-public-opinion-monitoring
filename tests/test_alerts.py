import json
import socket
import threading

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
    assert alerts.get(alert["alert_id"]) == alert
    assert alerts.assess(run_id) == alert

    for channel in ("wecom", "email"):
        preview = alerts.preview_delivery(alert["alert_id"], channel)
        assert preview["delivery_status"] == "dry_run"
        assert preview["network_requests"] == 0
        assert preview["sent_at"] is None and preview["recipient"] is None
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


class TimesOutAfterFirstRound(FixedActions):
    """Complains in round 1, then blocks forever once an agent has memory (round 2+)."""

    def __init__(self, gate):
        super().__init__(complaint_count=4)
        self.gate = gate

    def decide(self, observation):
        if observation.get("memories"):
            self.gate.wait()
        return super().decide(observation)


def _run_with_timed_out_last_round(store):
    """Round 1 decides; round 2 completes but every one of its requests times out.

    ``concurrency`` equals the agent count on purpose: every agent of round 2 is dispatched
    and then abandoned, so the round is *recorded* (not stranded) and holds zero valid
    decisions. That is the shape that needs a fallback.
    """
    sim = Day3Simulation(store)
    run_id = sim.create_run(CASE_ID, GRAPH_ID, agent_count=4, rounds=2, concurrency=4, seed=4)
    gate = threading.Event()
    try:
        summary = sim.advance(run_id, TimesOutAfterFirstRound(gate),
                              collect_performance=True, call_timeout_seconds=0.2)
    finally:
        gate.set()
    return run_id, summary


def test_the_alert_falls_back_to_the_last_round_that_actually_decided(tmp_path):
    """An empty latest round must not be read as if it had decisions.

    The latest recorded round holds only ``deadline_exceeded`` requests. Producing a risk
    level from an empty round would be a level with nothing behind it, so the assessment
    falls back to round 1 and says so, and it is never presented as the current result.
    """
    store = _prepared_store(tmp_path / "fallback.db")
    run_id, summary = _run_with_timed_out_last_round(store)
    assert summary["status"] == "partial"
    assert summary["completed_rounds"] == 2
    round_two = [row for row in summary["rounds"] if row["round"] == 2][0]
    assert round_two["valid_decisions"] == 0
    assert round_two["deadline_exceeded_requests"] == 4

    alert = Day4Alerts(store).assess(run_id)
    assert alert["assessed_round"] == 1
    assert alert["latest_recorded_round"] == 2
    assert alert["assessment_basis"] == "earlier_round_with_valid_decisions"
    assert alert["round_fallback"]["latest_recorded_round"] == 2
    assert "第 1 轮" in alert["assessment_basis_label"]
    assert "不完整" in alert["assessment_basis_label"]
    # A fallback can never read as a successful assessment.
    assert alert["alert_is_success_result"] is False
    assert alert["run_complete"] is False
    assert alert["decisions_all_valid"] is False
    # The level is still a four-level classification computed by the existing rule.
    assert alert["level"] in {"red", "orange", "yellow", "blue"}
    # The metrics state which round they describe, instead of silently shifting.
    assert alert["metrics"]["assessed_round"] == 1
    assert alert["metrics"]["assessed_round_valid_decisions"] > 0
    assert any("must not be read as the current complete result" in item
               for item in alert["limitations"])


def test_a_run_with_no_valid_decision_anywhere_produces_no_risk_level(tmp_path):
    """Nowhere to fall back to: no level is invented, and the reason is explicit."""
    store = _prepared_store(tmp_path / "no-level.db")
    sim = Day3Simulation(store)
    run_id = sim.create_run(CASE_ID, GRAPH_ID, agent_count=2, rounds=1, concurrency=2, seed=5)
    gate = threading.Event()

    class AlwaysHangs:
        mode = "test_substitute"

        def decide(self, observation):
            gate.wait()
            raise AssertionError("unreachable")

    try:
        summary = sim.advance(run_id, AlwaysHangs(), collect_performance=True,
                              call_timeout_seconds=0.2)
    finally:
        gate.set()
    assert summary["action_counts"].get("valid", 0) == 0

    with pytest.raises(AlertError, match="no round with a valid decision"):
        Day4Alerts(store).assess(run_id)
    # No alert row may exist, so nothing downstream can read a fabricated level.
    with store.connect() as db:
        assert db.execute("SELECT COUNT(*) AS n FROM day4_alerts WHERE run_id=?",
                          (run_id,)).fetchone()["n"] == 0


def test_a_report_can_be_built_from_a_fallback_alert(tmp_path):
    """The fallback alert and the report agree: end to end, no refusal in between."""
    from riskshield.reporting import Day4Reports

    store = _prepared_store(tmp_path / "fallback-report.db")
    run_id, _ = _run_with_timed_out_last_round(store)
    alert = Day4Alerts(store).assess(run_id)
    report = Day4Reports(store).build(run_id, risk_assessment=alert)
    assert report["overview"]["risk_level"] == alert["level"]
    assert report["overview"]["main_basis"]["assessed_round"] == 1
    assert report["overview"]["run_integrity"]["complete"] is False
