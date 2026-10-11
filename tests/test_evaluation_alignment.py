"""Tests for the frozen-benchmark prediction import and per-unit pairing audit.

Required coverage (round 2):

* a qualifying prediction imports and pairs;
* unknown/missing execution modes and non-formal artefacts are excluded;
* wrong event, wrong input version and duplicate conflicting predictions;
* missing reference, bad pointer and hash mismatch;
* a source visible after the cutoff;
* missing human labels, label disagreement and weak independence evidence;
* the eligibility table and the scored denominator are the same set;
* a zero denominator reports "not computable", not 0%;
* the same frozen input scores identically twice;
* synthetic controls never enter a formal result.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from riskshield import evaluation_prediction_import as imp
from riskshield.evaluation_alignment import (
    build_cutoff_attestation,
    build_input_package,
    build_pairing_report,
    cutoff_evidence_ref,
    prediction_inventory,
    score_sentiment_set,
    trajectory_qualification,
    unit_gaps,
    unit_gates,
    verify_freeze_members,
)

TITLE = "Fidelity National Financial shuts down network in wake of cybersecurity incident"
TEXT_SHA = hashlib.sha256(TITLE.encode("utf-8")).hexdigest()
SOURCE_VERSION = "arctic-shift-title-snapshot-v1"
CUTOFF = "2023-11-23T14:29:55Z"
VISIBLE_AT = "2023-11-23T13:59:55Z"


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------

def _write(root: Path, relative: str, payload) -> Path:
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
                    encoding="utf-8")
    return path


def _annotation(annotator: str, label: str, *, document: str = "labels/v16.json",
                position: int = 0) -> dict:
    """An annotation whose evidence pointer resolves to the submitted label."""
    key = "units" if document.endswith("v16.json") else "records"
    return {
        "annotator_id": annotator,
        "label": label,
        "independent": True,
        "prediction_seen": False,
        "evidence_refs": [{"path": document, "pointer": f"/{key}/{position}/label"}],
        "submitted_at": "2026-10-08T22:09:36+08:00",
    }


def _unit(**overrides) -> dict:
    unit = {
        "unit_id": "sent-unit-1",
        "event_id": "event_1",
        "source_document": "frozen/packet.json",
        "source_record_id": "reddit_archived_post_1821s44",
        "publisher": "Reddit r/technology archived post",
        "source_version": SOURCE_VERSION,
        "source_version_pointer": "/units/0/source/source_version",
        "declared_cutoff_pointer": "/units/0/cutoff_utc",
        "visible_at_pointer": {"path": "frozen/packet.json",
                               "pointer": "/units/0/source/captured_at"},
        "source_record_pointer": {"path": "frozen/archive.json",
                                  "pointer": "/records/0/source_ref"},
        "unit_hash_ref": {"path": "frozen/archive.json", "pointer": "/records/0/title_sha256"},
        "text_sha256": TEXT_SHA,
        "archive_title_sha256": TEXT_SHA,
        "title_hash_match": True,
        "visible_at": VISIBLE_AT,
        "cutoff": CUTOFF,
        "cutoff_provenance": "derived_backtest_window_rule_not_historical_freeze",
        "cutoff_margin_seconds": 1800,
        "permission_evidence_ref": None,
        "annotations": [_annotation("leos", "neutral"), _annotation("Dick", "neutral",
                                                                   document="labels/v18.json")],
        "labels_agree": True,
        "prediction": None,
    }
    unit.update(overrides)
    return unit


def _material_root(tmp_path: Path) -> Path:
    """Create the minimal frozen material a unit's references point at."""
    _write(tmp_path, "frozen/archive.json", {
        "records": [{"source_ref": "reddit_archived_post_1821s44",
                     "post_id": "1821s44",
                     "title_sha256": TEXT_SHA,
                     "retrieved_on_utc": VISIBLE_AT}]})
    _write(tmp_path, "frozen/packet.json", {
        "units": [{"unit_id": "sent-unit-1",
                   "source": {"source_version": SOURCE_VERSION,
                              "captured_at": VISIBLE_AT},
                   "cutoff_utc": CUTOFF}]})
    _write(tmp_path, "labels/v16.json", {"units": [{"unit_id": "sent-unit-1", "label": "neutral"}]})
    _write(tmp_path, "labels/v18.json", {"records": [{"unit_id": "sent-unit-1", "label": "neutral"}]})
    return tmp_path


def _prediction_document(unit: dict, *, label: str = "neutral",
                         execution_mode: str = "formal_model_run_v1",
                         input_ref: dict | None = None, **overrides) -> dict:
    root = Path(unit["_root"])
    ref = input_ref or unit["unit_hash_ref"]
    resolved, failure = imp._resolve_ref(ref, root)
    assert failure is None, f"fixture input reference must resolve: {failure}"
    row = {
        "unit_id": unit["unit_id"],
        "event_id": unit["event_id"],
        "task_type": imp.TASK_SENTIMENT,
        "label": label,
        "execution_mode": execution_mode,
        "method_version": "sentiment-classifier-v1",
        "run_id": "run_formal_0001",
        "predicted_at": "2026-10-11T01:00:00Z",
        "cutoff": unit["cutoff"],
        "input_binding": {
            "input_package_ref": ref,
            "input_package_sha256": imp.canonical_digest(resolved),
            "cutoff": unit["cutoff"],
            "cutoff_before_label_read": True,
            "text_sha256": unit["text_sha256"],
            "source_version": unit["source_version"],
        },
        "production_evidence_ref": {"path": "pred/production-evidence.json",
                                    "pointer": "/runs/run_formal_0001"},
        "freeze_record_ref": {"path": "pred/freeze-record.json", "pointer": ""},
        "labels_accessed": False,
        "outcomes_accessed": False,
    }
    row.update(overrides)
    return {"schema": imp.PREDICTION_IMPORT_SCHEMA,
            "task_type": imp.TASK_SENTIMENT,
            "document_version": "formal-test-v1",
            "predictions": {unit["unit_id"]: row}}


def _write_prediction_support(root: Path, document: dict) -> None:
    """Write the production evidence, freeze record and the prediction document."""
    _write(root, "pred/production-evidence.json", {
        "runs": {"run_formal_0001": {"run_id": "run_formal_0001",
                                     "execution_mode": "formal_model_run_v1",
                                     "run_status": "complete",
                                     "model_calls": 5,
                                     "network_requests": 0}}})
    freeze = {
        "schema": imp.FREEZE_RECORD_SCHEMA,
        "material_class": imp.MATERIAL_CLASS_REAL,
        "frozen_at": "2026-10-11T01:05:00Z",
        "frozen_by": "test fixture",
        "frozen_inputs_sha256": imp.canonical_digest({"fixture": True}),
        "labels_read_before_freeze": False,
        "outcomes_read_before_freeze": False,
    }
    freeze["prediction_document_sha256"] = imp.canonical_digest(document)
    _write(root, "pred/freeze-record.json", freeze)
    _write(root, "pred/predictions.json", document)


def _import(document: dict, unit: dict, root: Path):
    return imp.import_formal_predictions(document, task_type=imp.TASK_SENTIMENT,
                                         units=[unit], material_root=root)


# --------------------------------------------------------------------------
# group 1: a qualifying prediction imports and pairs
# --------------------------------------------------------------------------

def test_qualifying_prediction_is_admitted(tmp_path):
    root = _material_root(tmp_path)
    unit = _unit(_root=str(root))
    document = _prediction_document(unit)
    _write_prediction_support(root, document)

    report = _import(document, unit, root)
    assert report["counts"] == {"rows": 1, "admitted": 1, "rejected": 0, "unmatched": 0}
    assert report["admitted_unit_ids"] == ["sent-unit-1"]
    admitted = report["admitted"][0]
    assert admitted["label"] == "neutral"
    assert admitted["frozen_at"] == "2026-10-11T01:05:00Z"
    assert admitted["row_digest"]


def test_admitted_prediction_makes_the_unit_scoreable(tmp_path):
    root = _material_root(tmp_path)
    unit = _unit(_root=str(root))
    document = _prediction_document(unit)
    _write_prediction_support(root, document)
    unit["prediction"] = _import(document, unit, root)["admitted"][0]

    gates = unit_gates(unit)
    assert gates["gates"]["admitted_formal_prediction"] is True
    # The only remaining gap is the audit-level permission evidence, which is
    # expected for every unit in this frozen corpus.
    assert gates["reasons"] == ["no_recorded_source_permission_evidence"]


def test_admitted_prediction_scores_non_zero_denominator(tmp_path):
    root = _material_root(tmp_path)
    unit = _unit(_root=str(root))
    document = _prediction_document(unit)
    _write_prediction_support(root, document)
    unit["prediction"] = _import(document, unit, root)["admitted"][0]

    cutoffs = build_cutoff_attestation([unit], "out/cutoffs.json")
    _write(root, "out/cutoffs.json", cutoffs)
    run = score_sentiment_set(
        [unit], dataset_version="unit-test", evidence_root=root,
        cutoffs_document="out/cutoffs.json",
        admitted_by_unit={unit["unit_id"]: unit["prediction"]},
        predictions_document="pred/predictions.json")
    metrics = run["metrics"]["sentiment_three_class"]
    assert metrics["exclusion_reasons"] == {}, metrics["excluded_rows"]
    assert metrics["denominator"] == 1
    assert metrics["numerator"] == 1
    assert metrics["value"] == 1.0


# --------------------------------------------------------------------------
# group 2: execution modes and non-formal artefacts
# --------------------------------------------------------------------------

@pytest.mark.parametrize("mode,reason", [
    (None, "missing_execution_mode"),
    ("", "missing_execution_mode"),
    ("some_unregistered_mode_v9", "unknown_execution_mode_not_admitted"),
    ("offline_constant_escalation_baseline_v1", "rejected_engineering_stand_in_mode"),
    ("offline_dynamic_substitute", "rejected_engineering_stand_in_mode"),
    ("synthetic_fixture_control_v1", "rejected_engineering_stand_in_mode"),
])
def test_execution_modes_are_admitted_only_by_allow_list(tmp_path, mode, reason):
    root = _material_root(tmp_path)
    unit = _unit(_root=str(root))
    row = _prediction_document(unit)["predictions"]["sent-unit-1"]
    if mode is None:
        row.pop("execution_mode")
    else:
        row["execution_mode"] = mode
    document = {"schema": imp.PREDICTION_IMPORT_SCHEMA,
                "task_type": imp.TASK_SENTIMENT,
                "predictions": {"sent-unit-1": row}}
    _write_prediction_support(root, document)

    report = _import(document, unit, root)
    assert report["counts"]["admitted"] == 0
    assert reason in report["rejected"][0]["reasons"]


def test_qualification_string_alone_cannot_admit(tmp_path):
    """A row claiming to be formal still needs mode, run and freeze evidence."""
    root = _material_root(tmp_path)
    unit = _unit(_root=str(root))
    row = _prediction_document(unit)["predictions"]["sent-unit-1"]
    row["qualification"] = "formal_forecast_ready"
    row["execution_mode"] = "unregistered_mode_v2"
    document = {"schema": imp.PREDICTION_IMPORT_SCHEMA,
                "task_type": imp.TASK_SENTIMENT,
                "predictions": {"sent-unit-1": row}}
    _write_prediction_support(root, document)

    report = _import(document, unit, root)
    assert report["counts"]["admitted"] == 0
    assert "unknown_execution_mode_not_admitted" in report["rejected"][0]["reasons"]


def test_missing_method_version_and_run_id_are_rejected(tmp_path):
    root = _material_root(tmp_path)
    unit = _unit(_root=str(root))
    row = _prediction_document(unit)["predictions"]["sent-unit-1"]
    row.pop("method_version")
    row.pop("run_id")
    document = {"schema": imp.PREDICTION_IMPORT_SCHEMA,
                "task_type": imp.TASK_SENTIMENT,
                "predictions": {"sent-unit-1": row}}
    _write_prediction_support(root, document)

    reasons = _import(document, unit, root)["rejected"][0]["reasons"]
    assert "missing_field_method_version" in reasons or "missing_method_version" in reasons
    assert "missing_field_run_id" in reasons or "missing_run_id" in reasons


def test_incomplete_production_run_is_rejected(tmp_path):
    root = _material_root(tmp_path)
    unit = _unit(_root=str(root))
    document = _prediction_document(unit)
    _write(root, "pred/production-evidence.json", {
        "runs": {"run_formal_0001": {"run_id": "run_formal_0001",
                                     "execution_mode": "formal_model_run_v1",
                                     "run_status": "partial"}}})
    _write(root, "pred/freeze-record.json", {
        "prediction_document_sha256": imp.canonical_digest(document),
        "frozen_at": "2026-10-11T01:05:00Z",
        "frozen_inputs_sha256": "x",
        "labels_read_before_freeze": False,
        "outcomes_read_before_freeze": False})

    reasons = _import(document, unit, root)["rejected"][0]["reasons"]
    assert "production_run_not_complete" in reasons


# --------------------------------------------------------------------------
# group 3: wrong event, wrong input version, duplicate conflicts
# --------------------------------------------------------------------------

def test_wrong_event_id_is_rejected(tmp_path):
    root = _material_root(tmp_path)
    unit = _unit(_root=str(root))
    document = _prediction_document(unit, event_id="some_other_event")
    _write_prediction_support(root, document)
    assert "event_id_does_not_match_unit" in _import(document, unit, root)["rejected"][0]["reasons"]


def test_wrong_source_version_is_rejected(tmp_path):
    root = _material_root(tmp_path)
    unit = _unit(_root=str(root))
    document = _prediction_document(unit)
    document["predictions"]["sent-unit-1"]["input_binding"]["source_version"] = "other-snapshot-v9"
    _write_prediction_support(root, document)
    assert "prediction_source_version_mismatch" in _import(document, unit, root)["rejected"][0]["reasons"]


def test_wrong_cutoff_is_rejected(tmp_path):
    root = _material_root(tmp_path)
    unit = _unit(_root=str(root))
    document = _prediction_document(unit)
    document["predictions"]["sent-unit-1"]["input_binding"]["cutoff"] = "2023-11-24T00:00:00Z"
    _write_prediction_support(root, document)
    assert "prediction_cutoff_mismatch" in _import(document, unit, root)["rejected"][0]["reasons"]


def test_duplicate_conflicting_predictions_are_rejected(tmp_path):
    root = _material_root(tmp_path)
    unit = _unit(_root=str(root))
    first = _prediction_document(unit)["predictions"]["sent-unit-1"]
    second = json.loads(json.dumps(first))
    second["label"] = "negative"
    second["run_id"] = "run_formal_0002"
    document = {"schema": imp.PREDICTION_IMPORT_SCHEMA,
                "task_type": imp.TASK_SENTIMENT,
                "predictions": {"row_a": first, "row_b": second}}
    _write_prediction_support(root, document)

    report = _import(document, unit, root)
    assert report["counts"]["admitted"] == 0
    reasons = {reason for row in report["rejected"] for reason in row["reasons"]}
    assert "duplicate_conflicting_prediction_for_unit" in reasons


def test_prediction_for_unknown_unit_is_unmatched(tmp_path):
    root = _material_root(tmp_path)
    unit = _unit(_root=str(root))
    document = _prediction_document(unit)
    row = document["predictions"].pop("sent-unit-1")
    row["unit_id"] = "not_a_candidate"
    document["predictions"]["not_a_candidate"] = row
    _write_prediction_support(root, document)

    report = _import(document, unit, root)
    assert report["counts"]["unmatched"] == 1
    assert report["unmatched"][0]["reasons"] == ["unit_id_not_a_candidate_unit"]


def test_task_type_mismatch_is_rejected_at_document_level(tmp_path):
    root = _material_root(tmp_path)
    unit = _unit(_root=str(root))
    document = _prediction_document(unit)
    document["task_type"] = imp.TASK_DIRECTION
    with pytest.raises(imp.PredictionImportError):
        _import(document, unit, root)


def test_direction_label_cannot_fill_a_sentiment_slot(tmp_path):
    """An event-direction value must not be scored as a sentiment class."""
    root = _material_root(tmp_path)
    unit = _unit(_root=str(root))
    document = _prediction_document(unit, label="escalation")
    _write_prediction_support(root, document)
    assert "label_not_in_formal_sentiment_classes" in _import(
        document, unit, root)["rejected"][0]["reasons"]


# --------------------------------------------------------------------------
# group 4: references, pointers, hashes
# --------------------------------------------------------------------------

def test_missing_reference_file_is_rejected(tmp_path):
    root = _material_root(tmp_path)
    unit = _unit(_root=str(root))
    document = _prediction_document(unit)
    row = document["predictions"]["sent-unit-1"]
    row["production_evidence_ref"] = {"path": "pred/does-not-exist.json", "pointer": ""}
    _write_prediction_support(root, document)
    reasons = _import(document, unit, root)["rejected"][0]["reasons"]
    assert any(reason.startswith("production_evidence_reference_file_missing")
               for reason in reasons)


def test_bad_pointer_is_rejected(tmp_path):
    root = _material_root(tmp_path)
    unit = _unit(_root=str(root))
    document = _prediction_document(unit)
    document["predictions"]["sent-unit-1"]["production_evidence_ref"] = {
        "path": "pred/production-evidence.json", "pointer": "/runs/run_absent"}
    _write_prediction_support(root, document)
    reasons = _import(document, unit, root)["rejected"][0]["reasons"]
    assert "production_evidence_reference_pointer_missing" in reasons


def test_reference_escaping_material_root_is_rejected(tmp_path):
    root = _material_root(tmp_path)
    unit = _unit(_root=str(root))
    document = _prediction_document(unit)
    document["predictions"]["sent-unit-1"]["production_evidence_ref"] = {
        "path": "../outside.json", "pointer": ""}
    _write_prediction_support(root, document)
    reasons = _import(document, unit, root)["rejected"][0]["reasons"]
    assert any("reference_escapes_material_root" in reason or "reference_file_missing" in reason
               for reason in reasons)


def test_input_package_hash_mismatch_is_rejected(tmp_path):
    root = _material_root(tmp_path)
    unit = _unit(_root=str(root))
    document = _prediction_document(unit)
    document["predictions"]["sent-unit-1"]["input_binding"]["input_package_sha256"] = "0" * 64
    _write_prediction_support(root, document)
    assert "input_package_hash_mismatch" in _import(document, unit, root)["rejected"][0]["reasons"]


def test_text_hash_mismatch_is_rejected(tmp_path):
    root = _material_root(tmp_path)
    unit = _unit(_root=str(root))
    document = _prediction_document(unit)
    document["predictions"]["sent-unit-1"]["input_binding"]["text_sha256"] = "1" * 64
    _write_prediction_support(root, document)
    assert "prediction_text_hash_mismatch" in _import(document, unit, root)["rejected"][0]["reasons"]


def test_freeze_record_document_hash_mismatch_is_rejected(tmp_path):
    root = _material_root(tmp_path)
    unit = _unit(_root=str(root))
    document = _prediction_document(unit)
    _write_prediction_support(root, document)
    freeze = json.loads((root / "pred/freeze-record.json").read_text(encoding="utf-8"))
    freeze["prediction_document_sha256"] = "2" * 64
    _write(root, "pred/freeze-record.json", freeze)
    assert "freeze_record_document_hash_mismatch" in _import(
        document, unit, root)["rejected"][0]["reasons"]


def test_missing_freeze_record_is_rejected(tmp_path):
    root = _material_root(tmp_path)
    unit = _unit(_root=str(root))
    document = _prediction_document(unit)
    _write(root, "pred/production-evidence.json", {
        "runs": {"run_formal_0001": {"run_id": "run_formal_0001",
                                     "execution_mode": "formal_model_run_v1",
                                     "run_status": "complete"}}})
    reasons = _import(document, unit, root)["rejected"][0]["reasons"]
    assert "freeze_record_reference_file_missing" in reasons


@pytest.mark.parametrize("field,reason", [
    ("labels_read_before_freeze", "freeze_record_labels_read_before_freeze"),
    ("outcomes_read_before_freeze", "freeze_record_outcomes_read_before_freeze"),
])
def test_freeze_record_must_state_isolation(tmp_path, field, reason):
    root = _material_root(tmp_path)
    unit = _unit(_root=str(root))
    document = _prediction_document(unit)
    _write_prediction_support(root, document)
    freeze = json.loads((root / "pred/freeze-record.json").read_text(encoding="utf-8"))
    freeze[field] = True
    _write(root, "pred/freeze-record.json", freeze)
    assert reason in _import(document, unit, root)["rejected"][0]["reasons"]


@pytest.mark.parametrize("field,reason", [
    ("labels_accessed", "prediction_labels_access_not_declared_false"),
    ("outcomes_accessed", "prediction_outcomes_access_not_declared_false"),
])
def test_predictor_must_declare_no_label_or_outcome_access(tmp_path, field, reason):
    root = _material_root(tmp_path)
    unit = _unit(_root=str(root))
    document = _prediction_document(unit)
    document["predictions"]["sent-unit-1"][field] = True
    _write_prediction_support(root, document)
    assert reason in _import(document, unit, root)["rejected"][0]["reasons"]


# --------------------------------------------------------------------------
# group 5: gates
# --------------------------------------------------------------------------

def test_source_visible_after_cutoff_is_excluded():
    unit = _unit(visible_at="2023-11-23T15:00:00Z")
    assert "source_visible_after_cutoff" in unit_gates(unit)["reasons"]


def test_single_human_label_is_not_enough():
    unit = _unit(annotations=[_annotation("leos", "neutral")], labels_agree=False)
    reasons = unit_gates(unit)["reasons"]
    assert "fewer_than_two_independent_human_labels" in reasons
    assert unit_gates(unit)["gates"]["independent_human_labels"] is False


def test_disagreeing_labels_are_excluded_without_adjudication():
    unit = _unit(annotations=[_annotation("leos", "neutral"),
                              _annotation("Dick", "negative", document="labels/v18.json")],
                 labels_agree=False)
    assert "independent_labels_disagree_no_adjudication" in unit_gates(unit)["reasons"]


def test_unverified_title_hash_is_excluded():
    unit = _unit(archive_title_sha256=None, title_hash_match=False)
    reasons = unit_gates(unit)["reasons"]
    assert "original_text_hash_missing_or_mismatched" in reasons
    assert "no_archived_title_hash_evidence" in reasons


def test_missing_permission_evidence_is_reported_separately():
    """Attribution is not permission evidence; the gap is named, not assumed."""
    unit = _unit()
    gaps = unit_gaps(unit)
    assert gaps["has_source_permission_evidence"] is False
    assert "no_recorded_source_permission_evidence" in gaps["missing"]
    # and it does not silently pass the attribution gate
    assert unit_gates(unit)["gates"]["source_attribution"] is True


def test_gap_inventory_lists_every_missing_piece():
    """No summary may hide a second gap behind the first one."""
    unit = _unit(archive_title_sha256=None, title_hash_match=False, prediction=None)
    gaps = unit_gaps(unit)
    assert "no_archived_title_hash_evidence" in gaps["missing"]
    assert "no_admitted_formal_sentiment_prediction" in gaps["missing"]
    assert gaps["has_archived_title_hash"] is False
    assert gaps["has_admitted_prediction"] is False
    assert gaps["annotator_count"] == 2
    assert gaps["label_distribution"] == {"neutral": 2}


def test_direction_track_needs_its_own_evidence():
    """A sentiment-ready title is not automatically trajectory-scoreable."""
    unit = _unit(frozen_observed_direction=None, human_direction_label=None)
    unit["prediction"] = {"admitted": True, "task_type": imp.TASK_SENTIMENT,
                          "label": "neutral"}
    assert unit_gates(unit)["passed"] is False  # permission gap still open
    direction = unit_gates(unit, require_direction=True)
    assert "no_qualified_observed_direction" in direction["reasons"]
    assert "no_independent_human_direction_label" in direction["reasons"]
    assert "no_admitted_formal_direction_prediction" in direction["reasons"]


# --------------------------------------------------------------------------
# group 6: eligibility table and denominator are the same set
# --------------------------------------------------------------------------

def test_gate_table_and_scored_denominator_agree(tmp_path):
    root = _material_root(tmp_path)
    unit = _unit(_root=str(root))
    document = _prediction_document(unit)
    _write_prediction_support(root, document)
    unit["prediction"] = _import(document, unit, root)["admitted"][0]
    cutoffs = build_cutoff_attestation([unit], "out/cutoffs.json")
    _write(root, "out/cutoffs.json", cutoffs)

    qualified = [unit] if unit_gates(unit)["gates"]["admitted_formal_prediction"] else []
    run = score_sentiment_set(
        qualified, dataset_version="unit-test", evidence_root=root,
        cutoffs_document="out/cutoffs.json",
        admitted_by_unit={unit["unit_id"]: unit["prediction"]},
        predictions_document="pred/predictions.json")
    assert run["scored_unit_ids"] == sorted(item["unit_id"] for item in qualified)
    assert run["metrics"]["sentiment_three_class"]["denominator"] == len(qualified)


def test_report_consistency_check_is_present(tmp_path):
    root = _material_root(tmp_path)
    for relative, payload in _report_fixture().items():
        _write(root, relative, payload)
    report = build_pairing_report(root, out_dir_relative="out")
    check = report["consistency_check"]
    assert check["agree"] is True
    assert check["excluded_units_present_in_denominator"] is False
    assert check["gated_unit_ids"] == check["scored_unit_ids"]


# --------------------------------------------------------------------------
# group 7: zero denominator means "not computable"
# --------------------------------------------------------------------------

def test_zero_denominator_reports_not_computable(tmp_path):
    root = _material_root(tmp_path)
    for relative, payload in _report_fixture().items():
        _write(root, relative, payload)
    report = build_pairing_report(root, out_dir_relative="out")
    metrics = report["formal_run"]["metrics"]["sentiment_three_class"]
    assert metrics["denominator"] == 0
    assert metrics["numerator"] == 0
    assert metrics["value"] is None
    assert metrics["undefined_reason"] == (
        "no records have a valid prediction and unanimous independent labels")
    assert report["summary"]["formal_sentiment_value"] is None


def test_counts_are_reported_separately(tmp_path):
    root = _material_root(tmp_path)
    for relative, payload in _report_fixture().items():
        _write(root, relative, payload)
    summary = build_pairing_report(root, out_dir_relative="out")["summary"]
    assert summary["candidate_sentiment_units"] >= summary["distinct_candidate_events"]
    assert summary["sentiment_qualified_units"] == 0
    assert summary["admitted_formal_predictions"] == 0
    assert summary["formal_direction_denominator"] == 0


# --------------------------------------------------------------------------
# group 8: reproducibility
# --------------------------------------------------------------------------

def test_same_frozen_input_scores_identically_twice(tmp_path):
    root = _material_root(tmp_path)
    for relative, payload in _report_fixture().items():
        _write(root, relative, payload)
    first = build_pairing_report(root, out_dir_relative="out")
    second = build_pairing_report(root, out_dir_relative="out")
    assert first["summary"] == second["summary"]
    assert first["formal_run"]["metrics"] == second["formal_run"]["metrics"]
    assert first["prediction_admission"] == second["prediction_admission"]
    assert (first["sentiment_gap_inventory"] == second["sentiment_gap_inventory"])
    assert first["consistency_check"] == second["consistency_check"]


def test_input_package_digest_is_stable(tmp_path):
    units = [_unit()]
    package = build_input_package(units, "out/input-package.json")
    again = build_input_package([_unit()], "out/input-package.json")
    assert imp.canonical_digest(package) == imp.canonical_digest(again)


# --------------------------------------------------------------------------
# group 9: controls never reach a formal result
# --------------------------------------------------------------------------

def test_control_rows_are_rejected_by_the_formal_importer(tmp_path):
    root = _material_root(tmp_path)
    units = [_unit(_root=str(root))]
    _write(root, "out/input-package.json", build_input_package(units, "out/input-package.json"))
    document, evidence, freeze = imp.import_constant_control(
        units, task_type=imp.TASK_SENTIMENT, label="neutral",
        predictions_path="out/control.json", evidence_path="out/control-evidence.json",
        freeze_path="out/control-freeze.json", material_root=root)
    _write(root, "out/control-evidence.json", evidence)
    _write(root, "out/control-freeze.json", imp.finalize_freeze_record(
        document, freeze, frozen_inputs={"fixture": True}))
    _write(root, "out/control.json", document)

    admission = imp.import_formal_predictions(
        document, task_type=imp.TASK_SENTIMENT, units=units, material_root=root)
    assert admission["counts"]["admitted"] == 0
    assert admission["rejection_reasons"] == {"rejected_engineering_stand_in_mode": 1}


def test_control_material_class_is_not_real():
    assert imp.MATERIAL_CLASS_CONTROL != imp.MATERIAL_CLASS_REAL


# --------------------------------------------------------------------------
# retained round-1 coverage
# --------------------------------------------------------------------------

def test_derived_cutoff_attestation_records_the_rule_and_frozen_time():
    unit = _unit(cutoff_provenance="derived_backtest_window_rule_not_historical_freeze",
                 cutoff_margin_seconds=1800)
    document = build_cutoff_attestation([unit], "evidence/derived.json")
    entry = document["units"]["sent-unit-1"]
    assert entry["cutoff"] == CUTOFF
    assert entry["frozen_visible_at"] == VISIBLE_AT
    assert entry["frozen_visible_at_ref"] == unit["visible_at_pointer"]
    assert entry["cutoff_provenance"] == "derived_backtest_window_rule_not_historical_freeze"
    assert "NOT proof" in document["provenance_warning"]


def test_declared_cutoff_uses_the_frozen_packet_reference():
    unit = _unit(declared_cutoff_pointer="/units/0/cutoff_utc")
    assert cutoff_evidence_ref(unit, None) == {
        "path": unit["source_document"], "pointer": "/units/0/cutoff_utc"}
    assert cutoff_evidence_ref(unit, "evidence/derived.json") == {
        "path": "evidence/derived.json", "pointer": "/units/sent-unit-1/cutoff"}


def test_freeze_verification_reports_members_and_mismatches(tmp_path):
    member = tmp_path / "member.json"
    member.write_text('{"value": 1}', encoding="utf-8")
    digest = hashlib.sha256(member.read_bytes()).hexdigest()
    _write(tmp_path, "freeze.json", {
        "freeze_is_self_hashed": False,
        "files": [{"path": "member.json", "sha256": digest},
                  {"path": "missing.json", "sha256": "0" * 64}]})
    result = verify_freeze_members(tmp_path, "freeze.json")
    assert result["all_members_match"] is False
    by_path = {row["path"]: row for row in result["members"]}
    assert by_path["member.json"]["match"] is True
    assert by_path["missing.json"]["reason"] == "missing_file"


def test_frozen_prediction_inventory_marks_stand_ins_ineligible(tmp_path):
    for relative, payload in _report_fixture().items():
        _write(tmp_path, relative, payload)
    inventory = prediction_inventory(tmp_path)
    assert inventory["formal_eligible_rows"] == 0
    assert inventory["title_level_sentiment_predictions"] == 0
    assert all(row["rejection_reason"] for row in inventory["rows"])


def test_trajectory_counts_are_derived_not_assumed():
    rows = [
        {"event_id": "event_a", "human_direction_label": None,
         "frozen_observed_direction": None},
        {"event_id": "event_b", "human_direction_label": "escalation",
         "frozen_observed_direction": None},
        {"event_id": "event_c", "human_direction_label": "calming",
         "frozen_observed_direction": "calming"},
    ]
    counts = trajectory_qualification(rows)
    assert counts["events_with_human_direction_label"] == 2
    assert counts["evidence_qualified_events"] == 1
    assert counts["qualified_event_ids"] == ["event_c"]


# --------------------------------------------------------------------------
# report fixture shared by the report-level tests
# --------------------------------------------------------------------------

def _report_fixture() -> dict[str, dict]:
    """A one-unit frozen corpus with no prediction document at all."""
    return {
        "data/evaluation/final-benchmark/manifests/freeze-v24.json": {"files": []},
        "data/evaluation/final-benchmark/manifests/final-reconciliation-v24.json": {
            "sentiment_double_labels": {"units": [
                {"unit_id": "sent-unit-1", "frozen_text_sha256": TEXT_SHA,
                 "source_title_sha256_match": True}]}},
        "data/evaluation/final-benchmark/manifests/archive-post-evidence-v1.json": {
            "records": [{"source_ref": "reddit_archived_post_1821s44",
                         "post_id": "1821s44", "subreddit": "technology",
                         "title_sha256": TEXT_SHA,
                         "retrieved_on_utc": VISIBLE_AT}]},
        "data/evaluation/final-benchmark/labels/blind-sentiment-packet-v6.json": {
            "units": [{"unit_id": "sent-unit-1",
                       "event_id_reference": "event_1",
                       "frozen_text": TITLE,
                       "source": {"publisher": "Public Reddit post archived by Arctic Shift",
                                  "url": "https://www.reddit.com/r/technology/comments/1821s44/",
                                  "source_version": SOURCE_VERSION,
                                  "captured_at": VISIBLE_AT}}]},
        "data/evaluation/final-benchmark/labels/human-sentiment-submissions-v16.json": {
            "annotator_id": "leos", "prediction_exposure": False,
            "saw_other_annotator_answer": False,
            "units": [{"unit_id": "sent-unit-1", "label": "neutral",
                       "annotator_id": "leos", "submitted_at": "2026-10-08T22:09:36+08:00",
                       "source_version": SOURCE_VERSION}]},
        "data/evaluation/final-benchmark/labels/human-sentiment-submissions-v18.json": {
            "annotator_id": "Dick", "prediction_exposure": False,
            "saw_other_annotator_answer": False,
            "records": [{"unit_id": "sent-unit-1", "label": "neutral",
                         "annotator_id": "Dick", "submitted_at": "2026-10-08T22:29:45+08:00",
                         "source_version": SOURCE_VERSION}]},
        "data/evaluation/final-benchmark/labels/blind-sentiment-reannotation-packet-v19.json": {
            "units": []},
        "data/evaluation/final-benchmark/labels/verified-original-titles-v19.json": {
            "records": []},
        "data/evaluation/final-benchmark/labels/trajectory-evidence-v13.json": {"events": []},
        "data/evaluation/final-benchmark/inputs/qualified-inputs-v8.json": {"events": []},
        "data/evaluation/final-benchmark/inputs/qualified-inputs-v13.json": {"events": []},
        "artifacts/final-integration-review/v8-pilot/pilot-predictions-e-handoff-v8.json": {
            "predictions": [{"event_id": "event_1", "split": "holdout",
                             "direction": "escalation", "sentiment": None,
                             "execution_mode": "offline_constant_escalation_baseline_v1",
                             "qualification": "engineering_baseline_only_not_formal_forecast",
                             "model_calls": 0}]},
        "artifacts/final-integration-review/v13-pilot/predictions-v13.json": {"predictions": []},
    }
