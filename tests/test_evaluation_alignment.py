"""Tests for the frozen-benchmark per-unit pairing audit.

The audit must keep a unit out of a formal denominator whenever any of the six
conditions (source attribution, event identity/text hash, cutoff, independent
human labels, paired formal prediction) is missing, and it must never let an AI
label, an offline constant baseline or a synthetic row stand in for a real one.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

from riskshield.evaluation_alignment import (
    build_cutoff_attestation,
    build_pairing_report,
    cutoff_evidence_ref,
    prediction_inventory,
    trajectory_qualification,
    unit_gates,
    verify_freeze_members,
)


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


TITLE = "Fidelity National Financial shuts down network in wake of cybersecurity incident"


def _annotation(annotator: str, label: str) -> dict:
    return {
        "annotator_id": annotator,
        "label": label,
        "independent": True,
        "prediction_seen": False,
        "evidence_refs": [{"path": "labels/v16.json", "pointer": "/units/0/label"}],
        "source_document": "labels/v16.json",
        "pointer": "/units/0/label",
        "submitted_at": "2026-10-08T22:09:36+08:00",
        "source_version": "arctic-shift-title-snapshot-v1",
    }


def _unit(**overrides) -> dict:
    unit = {
        "unit_id": "sent-unit-1",
        "event_id": "event_1",
        "source_document": "data/evaluation/final-benchmark/labels/blind-sentiment-packet-v6.json",
        "source_record_id": "reddit_archived_post_1821s44",
        "publisher": "Reddit r/technology archived post",
        "source_version": "arctic-shift-title-snapshot-v1",
        "source_version_pointer": "/units/0/source/source_version",
        "declared_cutoff_pointer": "/units/0/cutoff_utc",
        "visible_at_pointer": {"path": "data/evaluation/final-benchmark/labels/blind-sentiment-packet-v6.json",
                               "pointer": "/units/0/source/captured_at"},
        "text_sha256": _sha(TITLE),
        "archive_title_sha256": _sha(TITLE),
        "title_hash_match": True,
        "visible_at": "2023-11-23T13:59:55Z",
        "cutoff": "2023-11-23T14:29:55Z",
        "annotations": [_annotation("leos", "neutral"), _annotation("Dick", "neutral")],
        "labels_agree": True,
        "prediction": {
            "label": "neutral",
            "execution_mode": "deepseek_flash_sentiment_v1",
            "result_evidence_refs": [{"path": "predictions.json", "pointer": "/0/label"}],
        },
    }
    unit.update(overrides)
    return unit


def test_complete_unit_passes_every_gate():
    """A unit with all six conditions present is formally paired."""
    result = unit_gates(_unit())
    assert result["passed"] is True
    assert result["reasons"] == []
    assert all(result["gates"].values())


def test_missing_paired_prediction_keeps_unit_out():
    """The five frozen human double-label units have no paired prediction."""
    result = unit_gates(_unit(prediction=None))
    assert result["passed"] is False
    assert result["reasons"] == ["missing_paired_sentiment_prediction"]
    assert result["gates"]["independent_human_labels"] is True


def test_offline_constant_baseline_is_not_a_formal_prediction():
    """A constant engineering baseline may never enter a formal denominator."""
    unit = _unit(prediction={
        "label": "neutral",
        "execution_mode": "offline_constant_escalation_baseline_v1",
        "result_evidence_refs": [{"path": "predictions.json", "pointer": "/0/label"}],
    })
    result = unit_gates(unit)
    assert result["passed"] is False
    assert "prediction_is_offline_baseline_not_a_formal_model_output" in result["reasons"]
    assert result["gates"]["paired_formal_prediction"] is False


def test_stale_prediction_label_is_rejected():
    unit = _unit(prediction={"label": "escalation", "execution_mode": "model_v1",
                             "result_evidence_refs": []})
    assert unit_gates(unit)["reasons"] == ["missing_paired_sentiment_prediction"]


def test_single_human_label_is_not_enough():
    unit = _unit(annotations=[_annotation("leos", "neutral")], labels_agree=False)
    reasons = unit_gates(unit)["reasons"]
    assert "fewer_than_two_independent_human_labels" in reasons
    assert unit_gates(unit)["gates"]["independent_human_labels"] is False


def test_disagreeing_independent_labels_are_excluded_without_adjudication():
    unit = _unit(
        annotations=[_annotation("leos", "neutral"), _annotation("Dick", "negative")],
        labels_agree=False,
    )
    assert "independent_labels_disagree_no_adjudication" in unit_gates(unit)["reasons"]


def test_unverified_title_hash_is_excluded():
    """No archived title hash means the frozen text cannot be re-verified."""
    unit = _unit(archive_title_sha256=None, title_hash_match=False)
    reasons = unit_gates(unit)["reasons"]
    assert "original_text_hash_missing_or_mismatched" in reasons
    assert "no_archived_title_hash_evidence" in reasons


def test_source_visible_after_cutoff_is_excluded():
    """A source that became visible after the cutoff is not a cutoff input."""
    unit = _unit(visible_at="2023-11-23T15:00:00Z")
    assert "source_visible_after_cutoff" in unit_gates(unit)["reasons"]


def test_unknown_visible_time_is_excluded():
    unit = _unit(visible_at=None)
    reasons = unit_gates(unit)["reasons"]
    assert "unknown_or_invalid_visible_time" in reasons
    assert unit_gates(unit)["gates"]["cutoff_after_visible_time"] is False


def test_derived_cutoff_attestation_records_the_rule_and_frozen_time(tmp_path):
    """The generated cutoff evidence must point back at the frozen visible time."""
    unit = _unit(cutoff_basis="derived_visible_at_plus_30_minutes",
                 cutoff_margin_seconds=1800,
                 visible_at_pointer={"path": "labels/v6.json",
                                     "pointer": "/units/0/source/captured_at"})
    document = build_cutoff_attestation([unit], "evidence/derived.json")
    entry = document["units"]["sent-unit-1"]
    assert entry["cutoff"] == unit["cutoff"]
    assert entry["cutoff_margin_seconds"] == 1800
    assert entry["frozen_visible_at"] == unit["visible_at"]
    assert entry["frozen_visible_at_ref"] == unit["visible_at_pointer"]
    assert entry["frozen_text_sha256"] == unit["text_sha256"]

    written = tmp_path / "derived.json"
    written.write_text(json.dumps(document), encoding="utf-8")
    assert json.loads(written.read_text(encoding="utf-8"))["units"]["sent-unit-1"]["cutoff"] \
        == "2023-11-23T14:29:55Z"


def test_declared_cutoff_uses_the_frozen_packet_reference():
    """A packet that declares its own cutoff is not rewritten by an attestation."""
    unit = _unit(declared_cutoff_pointer="/units/0/cutoff_utc")
    ref = cutoff_evidence_ref(unit, None)
    assert ref == {"path": unit["source_document"],
                   "pointer": "/units/0/cutoff_utc"}
    derived = cutoff_evidence_ref(unit, "evidence/derived.json")
    assert derived == {"path": "evidence/derived.json",
                       "pointer": "/units/sent-unit-1/cutoff"}


def test_freeze_verification_reports_members_and_mismatches(tmp_path):
    member = tmp_path / "member.json"
    member.write_text('{"value": 1}', encoding="utf-8")
    digest = hashlib.sha256(member.read_bytes()).hexdigest()
    freeze = {
        "schema": "test",
        "freeze_is_self_hashed": False,
        "files": [
            {"path": "member.json", "sha256": digest},
            {"path": "missing.json", "sha256": "0" * 64},
        ],
    }
    (tmp_path / "freeze.json").write_text(json.dumps(freeze), encoding="utf-8")

    result = verify_freeze_members(tmp_path, "freeze.json")
    assert result["all_members_match"] is False
    by_path = {row["path"]: row for row in result["members"]}
    assert by_path["member.json"]["match"] is True
    assert by_path["missing.json"]["match"] is False
    assert by_path["missing.json"]["reason"] == "missing_file"


def test_prediction_inventory_excludes_engineering_baselines(tmp_path):
    """Every frozen prediction is listed, and stand-ins are marked ineligible."""
    pilot = {
        "execution_mode": "offline_constant_escalation_baseline_v1",
        "predictions": [
            {"event_id": "event_a", "split": "holdout", "direction": "escalation",
             "sentiment": None, "execution_mode": "offline_constant_escalation_baseline_v1",
             "qualification": "engineering_baseline_only_not_formal_forecast",
             "model_calls": 0},
        ],
    }
    model = {
        "predictions": [
            {"event_id": "event_b", "split": "holdout", "direction": "calming",
             "sentiment": "negative", "execution_mode": "sealed_model_run_v1"},
        ],
    }
    paths = {
        "data/evaluation/final-benchmark/inputs/qualified-inputs-v8.json": {"data": True},
        "data/evaluation/final-benchmark/inputs/qualified-inputs-v13.json": {"data": True},
        "artifacts/final-integration-review/v8-pilot/pilot-predictions-e-handoff-v8.json": pilot,
        "artifacts/final-integration-review/v13-pilot/predictions-v13.json": model,
        "data/evaluation/final-benchmark/manifests/archive-post-evidence-v1.json": {"records": []},
        "data/evaluation/final-benchmark/manifests/freeze-v24.json": {"files": []},
        "data/evaluation/final-benchmark/manifests/final-reconciliation-v24.json":
            {"sentiment_double_labels": {"units": []}},
        "data/evaluation/final-benchmark/labels/blind-sentiment-packet-v6.json": {"units": []},
        "data/evaluation/final-benchmark/labels/human-sentiment-submissions-v16.json": {"units": []},
        "data/evaluation/final-benchmark/labels/human-sentiment-submissions-v18.json": {"records": []},
        "data/evaluation/final-benchmark/labels/blind-sentiment-reannotation-packet-v19.json":
            {"units": []},
        "data/evaluation/final-benchmark/labels/verified-original-titles-v19.json": {"records": []},
        "data/evaluation/final-benchmark/labels/trajectory-evidence-v13.json": {"events": []},
    }
    for relative, document in paths.items():
        target = tmp_path / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(document), encoding="utf-8")

    inventory = prediction_inventory(tmp_path)
    assert inventory["direction_rows"] == 2
    assert inventory["formal_eligible_rows"] == 1
    assert inventory["title_level_sentiment_predictions"] == 1
    eligibility = {(row["event_id"]): row["formal_eligible"] for row in inventory["rows"]}
    assert eligibility == {"event_a": False, "event_b": True}


def test_trajectory_counts_are_derived_not_assumed():
    """Trajectory counts come from the frozen evidence, never from a constant."""
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
    assert counts["events_with_qualified_observed_direction"] == 1
    assert counts["qualified_event_ids"] == ["event_c"]


def test_trajectory_counts_are_zero_for_the_frozen_v13_evidence():
    """The one frozen trajectory record has no human direction label."""
    rows = [{"event_id": "windows_recall_privacy_controversy_20240607",
             "human_direction_label": None, "frozen_observed_direction": None}]
    counts = trajectory_qualification(rows)
    assert counts["events_with_human_direction_label"] == 0
    assert counts["evidence_qualified_events"] == 0


def test_pairing_report_on_empty_benchmark_reports_zero_denominators(tmp_path):
    """An empty corpus with no predictions must yield 0/0, never 0%."""
    documents = {
        "data/evaluation/final-benchmark/manifests/freeze-v24.json": {"files": []},
        "data/evaluation/final-benchmark/manifests/final-reconciliation-v24.json":
            {"sentiment_double_labels": {"units": []}},
        "data/evaluation/final-benchmark/manifests/archive-post-evidence-v1.json":
            {"records": []},
        "data/evaluation/final-benchmark/labels/blind-sentiment-packet-v6.json": {"units": []},
        "data/evaluation/final-benchmark/labels/human-sentiment-submissions-v16.json": {"units": []},
        "data/evaluation/final-benchmark/labels/human-sentiment-submissions-v18.json":
            {"records": []},
        "data/evaluation/final-benchmark/labels/blind-sentiment-reannotation-packet-v19.json":
            {"units": []},
        "data/evaluation/final-benchmark/labels/verified-original-titles-v19.json": {"records": []},
        "data/evaluation/final-benchmark/labels/trajectory-evidence-v13.json": {"events": []},
        "data/evaluation/final-benchmark/inputs/qualified-inputs-v8.json": {"events": []},
        "data/evaluation/final-benchmark/inputs/qualified-inputs-v13.json": {"events": []},
        "artifacts/final-integration-review/v8-pilot/pilot-predictions-e-handoff-v8.json":
            {"predictions": []},
        "artifacts/final-integration-review/v13-pilot/predictions-v13.json":
            {"predictions": []},
    }
    for relative, document in documents.items():
        target = tmp_path / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(document), encoding="utf-8")

    report = build_pairing_report(tmp_path, "evidence/control.json",
                                 "evidence/derived.json")
    summary = report["summary"]
    assert summary["formal_sentiment_denominator"] == 0
    assert summary["formal_sentiment_numerator"] == 0
    assert summary["formal_direction_denominator"] == 0
    assert summary["sentiment_units_fully_paired"] == 0
    metrics = report["formal_run"]["metrics"]["sentiment_three_class"]
    assert metrics["value"] is None
    assert metrics["undefined_reason"] == (
        "no records have a valid prediction and unanimous independent labels")
