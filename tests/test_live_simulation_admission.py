"""The simulation producer's import shape must pass only with real run evidence."""

from __future__ import annotations

import copy
import hashlib
import json

import pytest

from riskshield import evaluation_prediction_import as imp


def _write(root, name, value):
    path = root / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")


def _fixture():
    cutoff = "2025-01-02T00:00:00Z"
    text_hash = hashlib.sha256(b"Example event title").hexdigest()
    package = {
        "case_id": "event-1", "cutoff": cutoff, "source_version": "snapshot-1",
        "data_mode": "real_historical",
        "records": [{"record_id": "record-1", "available_at": "2025-01-01T00:00:00Z",
                     "title": "Example event title"}],
    }
    unit = {"unit_id": "record-1", "event_id": "event-1", "cutoff": cutoff,
            "source_version": "snapshot-1", "text_sha256": text_hash}
    complete = {"run_status": "complete", "selected_round": 2,
                "configured_rounds": 2, "recorded_rounds": 2,
                "abandoned_rounds": 0, "state_basis": "run_final_state",
                "state_is_at_selected_round": True, "qualification": "formal_candidate"}
    evidence = {
        "schema": "riskshield.simulation_scale.ledger_evidence.v1",
        "run_id": "run-1", "case_id": "event-1",
        "execution_mode": imp.LIVE_SIMULATION_MODE, "run_status": "complete",
        "data_mode": "real_historical", "config_cutoff": cutoff,
        "config_source_version": "snapshot-1", "model_calls": 6,
        "model_calls_by_model": {"qwen-flash": 6}, "requests_with_usage": 6,
        "unknown_usage_calls": 0, "prompt_tokens": 120,
        "completion_tokens": 40, "cached_input_tokens": 0,
        "valid_decisions": 6, "rows_without_eligible_evidence": 0,
        "configured_agent_count": 3, "not_dispatched_requests": 0,
        "configured_rounds": 2, "completed_rounds": 2,
        "recorded_round_rows": 2, "selected_round": 2,
        "recorded_rounds": 2, "cost_cny": "0.01",
        "price_version": "test-price-v1",
    }
    row = {
        "unit_id": "record-1", "event_id": "event-1", "task_type": imp.TASK_DIRECTION,
        "label": "calming", "execution_mode": imp.LIVE_SIMULATION_MODE,
        "method_version": "direction-v1", "run_id": "run-1", "cutoff": cutoff,
        "input_binding": {
            "input_package_ref": {"path": "inputs.json", "pointer": ""},
            "input_package_sha256": imp.canonical_digest(package),
            "cutoff": cutoff, "cutoff_before_label_read": True,
            "text_sha256": text_hash, "source_version": "snapshot-1",
        },
        "production_evidence_ref": {"path": "evidence.json", "pointer": "/production_evidence"},
        "freeze_record_ref": {"path": "freeze.json", "pointer": ""},
        "labels_accessed": False, "outcomes_accessed": False,
        "prediction_source": "simulation_derived", "qualification": "formal_candidate",
        "run_completeness": complete,
    }
    document = {"schema": imp.PREDICTION_IMPORT_SCHEMA,
                "task_type": imp.TASK_DIRECTION, "material_class": imp.MATERIAL_CLASS_REAL,
                "formal_score_eligible": True, "predictions": {"record-1": row}}
    freeze = {"prediction_document_sha256": imp.canonical_digest(document),
              "frozen_at": "2025-01-03T00:00:00Z",
              "frozen_inputs_sha256": imp.canonical_digest(package),
              "labels_read_before_freeze": False,
              "outcomes_read_before_freeze": False}
    return unit, package, document, evidence, freeze


def _ledger():
    actions = []
    for round_number in (1, 2):
        for agent_number in (1, 2, 3):
            index = len(actions)
            actions.append({
                "run_id": "run-1", "round_number": round_number,
                "agent_id": f"agent-{agent_number}", "status": "valid",
                "decision": {"action": "observe", "evidence_ids": ["record-1"]},
                "observation_refs": {"evidence": ["record-1"], "messages": []},
                "requested_model": "qwen-flash", "price_version": "test-price-v1",
                "usage": {"prompt_tokens": 20,
                          "completion_tokens": 7 if index < 4 else 6,
                          "cached_input_tokens": 0},
                "cost_cny": "0.002" if index < 4 else "0.001",
            })
    return {
        "schema": "riskshield.simulation_scale.run_ledger.v1",
        "summary": {
            "run_id": "run-1", "case_id": "event-1", "status": "complete",
            "config": {"backend_mode": "real_model", "data_mode": "real_historical",
                       "cutoff": "2025-01-02T00:00:00Z", "source_version": "snapshot-1",
                       "agent_count": 3, "rounds": 2},
            "completed_rounds": 2, "rounds": [{"round": 1}, {"round": 2}],
            "all_decisions_valid": True, "action_counts": {"valid": 6},
            "cost_cny": "0.010",
            "usage": {"prompt_tokens": 120, "completion_tokens": 40,
                      "cached_input_tokens": 0, "unknown_usage_calls": 0,
                      "by_model": {"qwen-flash": {"calls": 6}}},
            "billing": {"cost_reconciles": True, "recorded_requests": 6,
                        "states": {"settled": 6, "not_sent": 0, "released": 0,
                                   "unsettled_reserved": 0},
                        "charged_cost_cny": "0.010"},
        },
        "trajectory": actions,
    }


def _score(tmp_path, unit, package, document, evidence, freeze, *, ledger_change=None):
    freeze["prediction_document_sha256"] = imp.canonical_digest(document)
    ledger = _ledger()
    if ledger_change is not None:
        ledger_change(ledger)
    evidence["run_ledger_ref"] = {"path": "ledger.json", "pointer": ""}
    evidence["run_ledger_sha256"] = imp.canonical_digest(ledger)
    evidence["run_config_sha256"] = imp.canonical_digest(ledger["summary"]["config"])
    _write(tmp_path, "inputs.json", package)
    _write(tmp_path, "evidence.json", {"production_evidence": evidence})
    _write(tmp_path, "freeze.json", freeze)
    _write(tmp_path, "ledger.json", ledger)
    return imp.import_formal_predictions(document, task_type=imp.TASK_DIRECTION,
                                         units=[unit], material_root=tmp_path)


def test_complete_real_simulation_export_admitted(tmp_path):
    unit, package, document, evidence, freeze = _fixture()
    report = _score(tmp_path, unit, package, document, evidence, freeze)
    assert report["counts"] == {"rows": 1, "admitted": 1, "rejected": 0, "unmatched": 0}


@pytest.mark.parametrize("change,reason", [
    (lambda p, d, e, f: e.update(model_calls=None), "simulation_model_call_ledger_invalid"),
    (lambda p, d, e, f: e.update(model_calls_by_model={"qwen-flash": 5}),
     "simulation_model_call_ledger_invalid"),
    (lambda p, d, e, f: e.update(unknown_usage_calls=1),
     "simulation_unverified_usage_or_evidence"),
    (lambda p, d, e, f: e.update(cost_cny=None), "simulation_cost_ledger_invalid"),
    (lambda p, d, e, f: e.update(completed_rounds=1),
     "simulation_run_not_complete_at_selected_round"),
    (lambda p, d, e, f: e.update(configured_agent_count=2),
     "simulation_scale_ledger_invalid"),
    (lambda p, d, e, f: d["predictions"]["record-1"]["run_completeness"].update(
        state_is_at_selected_round=False), "simulation_run_not_complete_at_selected_round"),
    (lambda p, d, e, f: p.update(data_mode="synthetic"),
     "simulation_input_not_real_historical"),
    (lambda p, d, e, f: p["records"][0].update(available_at="2025-01-03T00:00:00Z"),
     "simulation_input_has_unverified_or_late_record"),
    (lambda p, d, e, f: p["records"][0].update(title="Changed title"),
     "simulation_target_text_hash_mismatch"),
    (lambda p, d, e, f: f.update(frozen_inputs_sha256="0" * 64),
     "simulation_freeze_input_hash_mismatch"),
    (lambda p, d, e, f: d.update(formal_score_eligible=False),
     "simulation_not_real_formal_material"),
    (lambda p, d, e, f: d["predictions"]["record-1"].update(qualification="tentative_partial_run"),
     "simulation_prediction_not_formal_candidate"),
])
def test_invalid_simulation_export_rejected(tmp_path, change, reason):
    unit, package, document, evidence, freeze = copy.deepcopy(_fixture())
    change(package, document, evidence, freeze)
    report = _score(tmp_path, unit, package, document, evidence, freeze)
    assert report["counts"]["admitted"] == 0
    assert reason in report["rejected"][0]["reasons"]


def test_generic_simulation_mode_still_rejected(tmp_path):
    unit, package, document, evidence, freeze = _fixture()
    document["predictions"]["record-1"]["execution_mode"] = "multi_agent_simulation"
    evidence["execution_mode"] = "multi_agent_simulation"
    report = _score(tmp_path, unit, package, document, evidence, freeze)
    assert "unknown_execution_mode_not_admitted" in report["rejected"][0]["reasons"]


@pytest.mark.parametrize("change,reason", [
    (lambda l: l["summary"]["config"].update(backend_mode="test_substitute"),
     "simulation_run_ledger_identity_or_backend_mismatch"),
    (lambda l: l["trajectory"].pop(),
     "simulation_run_ledger_action_count_mismatch"),
    (lambda l: l["trajectory"][0].update(agent_id="agent-4"),
     "simulation_run_ledger_agent_rounds_mismatch"),
    (lambda l: l["trajectory"][0].update(cost_cny="0.004"),
     "simulation_run_ledger_totals_mismatch"),
    (lambda l: l["trajectory"][0].update(usage=None),
     "simulation_run_ledger_invalid_action"),
    (lambda l: l["summary"].update(all_decisions_valid=False),
     "simulation_run_ledger_totals_mismatch"),
])
def test_live_run_ledger_discrepancy_rejected(tmp_path, change, reason):
    unit, package, document, evidence, freeze = _fixture()
    report = _score(tmp_path, unit, package, document, evidence, freeze,
                    ledger_change=change)
    assert report["counts"]["admitted"] == 0
    assert reason in report["rejected"][0]["reasons"]


def test_live_run_requires_resolvable_ledger_and_digest(tmp_path):
    unit, package, document, evidence, freeze = _fixture()
    _score(tmp_path, unit, package, document, evidence, freeze)
    (tmp_path / "ledger.json").unlink()
    report = imp.import_formal_predictions(document, task_type=imp.TASK_DIRECTION,
                                           units=[unit], material_root=tmp_path)
    assert "simulation_run_ledger_reference_file_missing" in report["rejected"][0]["reasons"]

    ledger = _ledger()
    _write(tmp_path, "ledger.json", ledger)
    ledger["trajectory"][0]["cost_cny"] = "0.003"
    _write(tmp_path, "ledger.json", ledger)
    report = imp.import_formal_predictions(document, task_type=imp.TASK_DIRECTION,
                                           units=[unit], material_root=tmp_path)
    assert "simulation_run_ledger_hash_mismatch" in report["rejected"][0]["reasons"]
