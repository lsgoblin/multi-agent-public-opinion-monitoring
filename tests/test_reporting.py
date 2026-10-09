"""Report claims are bound to one persisted run and its cutoff-qualified graph."""

import json
import shutil
from decimal import Decimal
from pathlib import Path

import pytest

from riskshield.simulation import Day3Simulation, DecisionResult
from riskshield.alerts import Day4Alerts
from riskshield.reporting import Day4Reports, ReportError
from riskshield.schemas import AgentDecision
from riskshield.store import Store
from tests.fixtures.day3_synthetic_case import CASE_ID, GRAPH_ID, RECORD_ID, prepare_synthetic_case


class OfflineBackend:
    mode = "test_substitute"
    reservation_cny = Decimal("0")
    input_cny_per_million = Decimal("0")
    output_cny_per_million = Decimal("0")
    price_version = "offline-test"

    def decide(self, observation):
        if observation["messages"]:
            action = "comment"
            ref = observation["messages"][0]["message_id"]
        elif observation["agent_id"] == "agent_0000":
            action, ref = "share", RECORD_ID
        else:
            action, ref = "observe", RECORD_ID
        return DecisionResult(
            AgentDecision(agent_id=observation["agent_id"], action=action,
                          evidence_ids=[ref], reason="Synthetic offline test decision."),
            usage={"prompt_tokens": 1, "completion_tokens": 1}, model="offline-test")


def prepared(tmp_path):
    store = Store(tmp_path / "report.db")
    Day3Simulation(store)
    prepare_synthetic_case(store)
    engine = Day3Simulation(store)
    run_id = engine.create_run(CASE_ID, GRAPH_ID, agent_count=2, rounds=2)
    engine.advance(run_id, OfflineBackend())
    return store, run_id


def test_report_has_five_evidence_bound_modules_and_persists(tmp_path):
    store, run_id = prepared(tmp_path)
    reports = Day4Reports(store)
    report = reports.build(run_id)
    assert report["data_mode"] == "synthetic"
    assert report["execution_mode"] == "OfflineBackend"
    assert report["binding"] == {"run_id": run_id, "case_id": CASE_ID, "graph_id": GRAPH_ID}
    for module in report["modules"].values():
        assert {key: module[key] for key in ("run_id", "case_id", "graph_id")} == report["binding"]
    assert report["source_version"] == "day3-synthetic-v1"
    assert report["run_status"] == "complete"
    assert set(report["modules"]) == {"propagation", "emotion_evolution", "key_nodes",
                                       "risk", "recommendations"}
    propagation = report["modules"]["propagation"]
    assert propagation["kind"] == "simulated_runtime_messages"
    assert propagation["total_messages"] > 0
    assert all(edge["message_ids"] and edge["action_ids"] for edge in propagation["edges"])
    emotion = report["modules"]["emotion_evolution"]
    assert emotion["kind"] == "reconstructed_simulated_emotion_v1"
    assert emotion["rule_version"] == "day3_state_transition_v1"
    assert emotion["reconstruction_status"] == "validated_against_final_agent_state"
    assert emotion["emotion_state_available"] is True
    assert emotion["calculation_basis"]["measure"] == "mean_simulated_agent_emotion_state"
    assert emotion["calculation_basis"]["rounds"] == [1, 2]
    assert emotion["series"] == [
        {"round": 1, "mean_emotion": 0.0659, "min_emotion": 0.046,
         "max_emotion": 0.0858, "agent_count": 2},
        {"round": 2, "mean_emotion": 0.03185, "min_emotion": 0.0081,
         "max_emotion": 0.0556, "agent_count": 2},
    ]
    assert emotion["rounds"][0]["action_counts"]["share"] == 1
    assert report["modules"]["risk"]["status"] == "unassessed"
    assert report["overview"]["event_title"] == "Fictional Northstar Insurance test service interruption"
    assert report["overview"]["run_integrity"] == {
        "status": "complete", "completed_rounds": 2, "configured_rounds": 2,
        "requested_decisions": 4, "valid_decisions": 4, "failed_decisions": 0,
        "message_count": propagation["total_messages"],
        "integrity_label": "账本完整（不代表结果真实或关口通过）",
    }
    business = report["modules"]["recommendations"]["items"][0]
    assert business["source_record_ids"] == [RECORD_ID]
    assert business["applicable_when"] and business["responsible_role"]
    assert business["suggested_deadline"] and business["follow_up_metrics"]
    assert business["generated_by"] == "deterministic_workflow_template_not_model_output"
    assert report["modules"]["recommendations"]["technical_checks"]
    assert all(set(edge["source_record_ids"]) <= {RECORD_ID} for edge in propagation["edges"])
    assert any(edge["source_record_ids"] for edge in propagation["edges"])
    assert report["evidence"]["source_record_ids"] == [RECORD_ID]
    assert report["evidence"]["decision_cited_source_record_ids"] == [RECORD_ID]
    assert Day4Reports(Store(store.path)).get(report["report_id"]) == report
    assert reports.build(run_id)["report_id"] == report["report_id"]


def test_provisional_risk_is_bound_to_same_run_and_changes_report_identity(tmp_path):
    store, run_id = prepared(tmp_path)
    reports = Day4Reports(store)
    no_risk = reports.build(run_id)
    assessment = {"alert_id": "alert_fixture", "run_id": run_id, "case_id": CASE_ID,
                  "graph_id": GRAPH_ID, "data_mode": "synthetic", "level": "yellow",
                  "status": "provisional_offline_assessment", "rule_version": "test-v1",
                  "evidence_refs": {"source_record_ids": [RECORD_ID]}}
    with_risk = reports.build(run_id, assessment)
    assert with_risk["modules"]["risk"]["alert_id"] == "alert_fixture"
    assert with_risk["report_id"] != no_risk["report_id"]
    with pytest.raises(ReportError):
        reports.build(run_id, {**assessment, "run_id": "other_run"})
    with pytest.raises(ReportError):
        reports.build(run_id, {**assessment, "status": "final"})
    actual_assessment = Day4Alerts(store).assess(run_id)
    integrated = reports.build(run_id, actual_assessment)
    assert integrated["modules"]["risk"]["alert_id"] == actual_assessment["alert_id"]
    assert integrated["overview"]["risk_level"] == actual_assessment["level"]
    assert integrated["overview"]["main_basis"] == actual_assessment["metrics"]


def test_rejects_mismatched_graph_and_future_or_unknown_source(tmp_path):
    store, run_id = prepared(tmp_path)
    reports = Day4Reports(store)
    with store.connect() as db:
        db.execute("UPDATE simulation_runs SET graph_id='missing_graph' WHERE run_id=?", (run_id,))
    with pytest.raises(ReportError, match="graph"):
        reports.build(run_id)
    with store.connect() as db:
        db.execute("UPDATE simulation_runs SET graph_id=? WHERE run_id=?", (GRAPH_ID, run_id))
        row = db.execute("SELECT body FROM graphs WHERE graph_id=?", (GRAPH_ID,)).fetchone()
        graph = json.loads(row["body"])
        graph["claims"][0]["available_at"] = "2026-01-02T00:00:00+08:00"
        db.execute("UPDATE graphs SET body=? WHERE graph_id=?", (json.dumps(graph), GRAPH_ID))
    with pytest.raises(ReportError, match="future"):
        reports.build(run_id)


def test_rejects_foreign_message_reference_and_unfinished_run(tmp_path):
    store, run_id = prepared(tmp_path)
    reports = Day4Reports(store)
    with store.connect() as db:
        db.execute("UPDATE simulation_runs SET status='running' WHERE run_id=?", (run_id,))
    with pytest.raises(ReportError, match="completed"):
        reports.build(run_id)
    with store.connect() as db:
        db.execute("UPDATE simulation_runs SET status='complete' WHERE run_id=?", (run_id,))
        action = db.execute("SELECT * FROM simulation_actions WHERE run_id=? AND round_number=2 "
                            "AND agent_id='agent_0001'", (run_id,)).fetchone()
        refs = json.loads(action["observation_refs"])
        refs["messages"] = ["msg_from_another_run"]
        db.execute("UPDATE simulation_actions SET observation_refs=? WHERE run_id=? "
                   "AND round_number=2 AND agent_id='agent_0001'",
                   (json.dumps(refs), run_id))
    with pytest.raises(ReportError, match="foreign or future"):
        reports.build(run_id)


def test_missing_ids_raise_key_error(tmp_path):
    store, _ = prepared(tmp_path)
    reports = Day4Reports(store)
    with pytest.raises(KeyError):
        reports.build("missing")
    with pytest.raises(KeyError):
        reports.get("missing")


def test_tampered_final_state_falls_back_to_action_counts(tmp_path):
    store, run_id = prepared(tmp_path)
    with store.connect() as db:
        db.execute("UPDATE simulation_agents SET state=? WHERE run_id=? AND agent_id='agent_0000'",
                   (json.dumps({"emotion": 0.999, "trust": 0.999}), run_id))
    emotion = Day4Reports(store).build(run_id)["modules"]["emotion_evolution"]
    assert emotion["kind"] == "round_action_distribution_proxy"
    assert emotion["reconstruction_status"] == "unavailable"
    assert emotion["series"] == []
    assert "differs from persisted state" in emotion["reason"]
    assert emotion["rounds"][0]["action_counts"]["share"] == 1
    assert "状态重建未通过最终状态核验" in " ".join(
        check["text"] for check in Day4Reports(store).build(run_id)["modules"]["recommendations"]["technical_checks"]
    )


def test_report_surfaces_failed_decision_counts_and_partial_integrity(tmp_path):
    store, run_id = prepared(tmp_path)
    with store.connect() as db:
        db.execute("UPDATE simulation_runs SET status='partial' WHERE run_id=?", (run_id,))
        action = db.execute(
            "SELECT * FROM simulation_actions WHERE run_id=? AND round_number=1 AND agent_id='agent_0001'",
            (run_id,),
        ).fetchone()
        refs = json.loads(action["observation_refs"])
        db.execute("UPDATE simulation_actions SET status='failed', decision=NULL WHERE run_id=? "
                   "AND round_number=1 AND agent_id='agent_0001'", (run_id,))
        stats = db.execute("SELECT stats FROM simulation_rounds WHERE run_id=? AND round_number=1",
                           (run_id,)).fetchone()
        stats_value = json.loads(stats["stats"])
        stats_value["failed_decisions"] += 1
        db.execute("UPDATE simulation_rounds SET stats=? WHERE run_id=? AND round_number=1",
                   (json.dumps(stats_value), run_id))
    report = Day4Reports(store).build(run_id)
    integrity = report["overview"]["run_integrity"]
    assert report["run_status"] == "partial"
    assert integrity["requested_decisions"] == 4
    assert integrity["valid_decisions"] == 3
    assert integrity["failed_decisions"] == 1
    assert integrity["integrity_label"] == "存在失败或部分完成"
    assert len(report["evidence"]["failed_action_ids"]) == 1


def test_actual_500x30_ledger_reconstructs_on_temporary_copy(tmp_path):
    original = (Path(__file__).resolve().parents[1] / "data/research/"
                "day3_synthetic_bailian_500x30_20261007.sqlite3")
    copy = tmp_path / "large-run-copy.sqlite3"
    shutil.copy2(original, copy)
    report = Day4Reports(Store(copy)).build("run_50789c8d2dfe4260b682")
    emotion = report["modules"]["emotion_evolution"]
    assert emotion["kind"] == "reconstructed_simulated_emotion_v1"
    assert emotion["reconstruction_status"] == "validated_against_final_agent_state"
    assert len(emotion["series"]) == 30
    assert all(point["agent_count"] == 500 for point in emotion["series"])
    assert report["modules"]["propagation"]["total_messages"] == 20622
    assert len(report["evidence"]["failed_action_ids"]) == 6
