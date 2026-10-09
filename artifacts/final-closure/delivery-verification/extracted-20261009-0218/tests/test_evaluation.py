import hashlib
import json

import pytest

from riskshield.evaluation import (
    INPUT_SCHEMA,
    LABEL_SCHEMA,
    evaluate_documents,
)


CUTOFF = "2024-02-22T16:48:39-05:00"
VISIBLE = "2024-02-22T16:18:39-05:00"


def _ref(pointer):
    return {"path": "evidence.json", "pointer": pointer}


def _case(evaluation_id, event_id, unit_id, sentiment_prediction,
          direction_prediction=None, *, data_mode="real_historical", available_at=VISIBLE):
    return {
        "evaluation_id": evaluation_id,
        "event_id": event_id,
        "unit_id": unit_id,
        "unit_kind": "original_title",
        "text_sha256": hashlib.sha256(f"title:{unit_id}".encode()).hexdigest(),
        "title_sha256_ref": _ref(f"/sources/{unit_id}/title_sha256"),
        "target_record_id": unit_id,
        "data_mode": data_mode,
        "source_version": "fixture-v1",
        "cutoff": CUTOFF,
        "cutoff_evidence_ref": _ref("/cutoff"),
        "sources": [{
            "record_id": unit_id,
            "source_version": "fixture-v1",
            "available_at": available_at,
            "source_record_ref": _ref(f"/sources/{unit_id}/record_id"),
            "source_version_ref": _ref(f"/sources/{unit_id}/source_version"),
            "source_available_at_ref": _ref(f"/sources/{unit_id}/available_at"),
            "visibility_evidence_ref": _ref(f"/sources/{unit_id}/available_at"),
        }],
        "predictions": {
            "sentiment": {
                "label": sentiment_prediction,
                "execution_mode": "fixture",
                "result_evidence_refs": [_ref(f"/predictions/sentiment/{unit_id}")],
            },
            "direction": {
                "label": direction_prediction,
                "execution_mode": "fixture",
                "result_evidence_refs": ([_ref(f"/predictions/direction/{unit_id}")]
                                         if direction_prediction else []),
            },
        },
    }


def _labels(case, annotator_labels=("neutral", "neutral")):
    return {
        "evaluation_id": case["evaluation_id"],
        "event_id": case["event_id"],
        "unit_id": case["unit_id"],
        "text_sha256": case["text_sha256"],
        "source_record_id": case["target_record_id"],
        "sentiment_annotations": [
            {
                "annotator_id": f"annotator-{index}",
                "label": label,
                "independent": True,
                "prediction_seen": False,
                "evidence_refs": [_ref(f"/labels/{case['unit_id']}/annotator-{index}")],
            }
            for index, label in enumerate(annotator_labels)
        ],
    }


def _documents(tmp_path, cases, labels, directions=None, *, missing_outcome_evidence=False):
    directions = directions or {}
    evidence = {
        "cutoff": CUTOFF,
        "sources": {},
        "predictions": {"sentiment": {}, "direction": {}},
        "labels": {},
        "observed": {},
    }
    for case in cases:
        unit = case["unit_id"]
        evidence["sources"][unit] = {
            "record_id": unit,
            "source_version": case["sources"][0]["source_version"],
            "available_at": case["sources"][0]["available_at"],
            "title_sha256": case["text_sha256"],
        }
        sentiment = case["predictions"]["sentiment"]["label"]
        if sentiment:
            evidence["predictions"]["sentiment"][unit] = sentiment
        direction = case["predictions"]["direction"]["label"]
        if direction:
            evidence["predictions"]["direction"][unit] = direction
    for label_entry in labels:
        for index, annotation in enumerate(label_entry["sentiment_annotations"]):
            evidence["labels"].setdefault(label_entry["unit_id"], {})[f"annotator-{index}"] = annotation["label"]
    events = []
    for event_id, outcome in sorted(directions.items()):
        evidence["observed"][event_id] = {"direction": outcome}
        events.append({
            "event_id": event_id,
            "observed_direction": outcome,
            "evidence_refs": [] if missing_outcome_evidence else [_ref(f"/observed/{event_id}/direction")],
        })
    evidence_path = tmp_path / "evidence.json"
    evidence_path.write_text(json.dumps(evidence), encoding="utf-8")
    return (
        {"schema": INPUT_SCHEMA, "dataset_version": "test-v1", "cases": cases},
        {"schema": LABEL_SCHEMA, "label_version": "labels-v1",
         "sentiment_labels": labels, "events": events},
    )


def _evaluate(tmp_path, cases, labels, directions=None, **kwargs):
    ai_reference = kwargs.pop("ai_reference", None)
    inputs, outcomes = _documents(tmp_path, cases, labels, directions, **kwargs)
    return evaluate_documents(inputs, outcomes, evidence_root=tmp_path,
                              ai_reference=ai_reference)


def test_sentiment_and_negative_metrics_have_expected_counts(tmp_path):
    truth_prediction = [
        ("positive", "positive"),
        ("neutral", "negative"),
        ("negative", "neutral"),
        ("negative", "negative"),
    ]
    cases = []
    labels = []
    for index, (truth, prediction) in enumerate(truth_prediction):
        case = _case(f"eval-{index}", f"event-{index}", f"record-{index}", prediction)
        cases.append(case)
        labels.append(_labels(case, (truth, truth)))

    result = _evaluate(tmp_path, cases, labels)

    assert result["sentiment_three_class"]["numerator"] == 2
    assert result["sentiment_three_class"]["denominator"] == 4
    assert result["sentiment_three_class"]["confusion_matrix_actual_by_predicted"] == {
        "positive": {"positive": 1, "neutral": 0, "negative": 0},
        "neutral": {"positive": 0, "neutral": 0, "negative": 1},
        "negative": {"positive": 0, "neutral": 1, "negative": 1},
    }
    negative = result["negative_identification"]
    assert negative["counts"] == {
        "true_positive": 1, "false_positive": 1,
        "false_negative": 1, "true_negative": 1,
    }
    assert negative["precision"]["value"] == 0.5
    assert negative["recall"]["value"] == 0.5
    assert negative["binary_accuracy"]["value"] == 0.5
    assert len(result["sentiment_three_class"]["scored_units"]) == 4
    assert len(result["direction_agreement"]["scored_events"]) == 0


def test_missing_labels_predictions_and_time_evidence_are_not_scored(tmp_path):
    case = _case("incomplete", "event-incomplete", "record-incomplete", None,
                 available_at=None)
    result = _evaluate(tmp_path, [case], [], {"event-incomplete": None})

    assert result["sentiment_three_class"]["value"] is None
    assert result["sentiment_three_class"]["denominator"] == 0
    reasons = result["sentiment_three_class"]["exclusion_reasons"]
    assert reasons["missing_separate_evaluation_label"] == 1
    assert reasons["missing_or_invalid_sentiment_prediction"] == 1
    assert reasons["unknown_or_invalid_visible_time"] == 1
    assert reasons["missing_or_mismatched_visibility_evidence"] == 1
    assert result["direction_agreement"]["denominator"] == 0
    assert result["direction_agreement"]["value"] is None


def test_disagreeing_independent_labels_need_adjudication(tmp_path):
    case = _case("disputed", "event-disputed", "record-disputed", "neutral")
    result = _evaluate(tmp_path, [case], [_labels(case, ("neutral", "negative"))])

    assert result["sentiment_three_class"]["denominator"] == 0
    assert result["sentiment_three_class"]["exclusion_reasons"][
        "independent_labels_disagree_no_adjudication"] == 1


def test_same_event_sources_do_not_inflate_direction_event_count(tmp_path):
    first = _case("eval-source-a", "one-real-event", "source-a", "neutral", "escalation")
    second = _case("eval-source-b", "one-real-event", "source-b", "neutral", "escalation")
    synthetic = _case("eval-synthetic", "synthetic-event", "synthetic-source", "neutral",
                      "escalation", data_mode="synthetic")
    result = _evaluate(
        tmp_path,
        [first, second, synthetic],
        [_labels(first), _labels(second), _labels(synthetic)],
        {"one-real-event": "escalation", "synthetic-event": "escalation"},
    )

    assert result["sample_size"]["distinct_real_events"] == 1
    assert result["sample_size"]["distinct_real_source_records"] == 2
    assert result["sample_size"]["nonhistorical_rows_excluded"] == 1
    assert result["sentiment_three_class"]["denominator"] == 2
    assert result["direction_agreement"]["denominator"] == 0
    assert result["direction_agreement"]["exclusion_reasons"][
        "multiple_direction_predictions_for_one_event"] == 1
    assert result["input_exclusions"]["reasons"][
        "synthetic_or_nonhistorical_data_excluded"] == 1


def test_holdout_candidates_and_summary_units_are_excluded_from_formal_metrics(tmp_path):
    holdout = _case("eval-holdout", "event-holdout", "record-holdout", "neutral", "calming")
    holdout["split"] = "holdout"
    candidate = _case("eval-candidate", "event-candidate", "record-candidate", "neutral", "calming")
    candidate["split"] = "holdout_candidate"
    summary = _case("eval-summary", "event-summary", "event_cutoff_summary", "neutral")
    summary["unit_kind"] = "event_summary"
    cases = [holdout, candidate, summary]
    labels = [_labels(case) for case in cases]
    result = _evaluate(tmp_path, cases, labels, {
        "event-holdout": "calming", "event-candidate": "calming",
    })

    assert result["sample_size"]["holdout_split_events"] == 1
    assert result["sentiment_three_class"]["denominator"] == 1
    assert result["sentiment_three_class"]["exclusion_reasons"]["holdout_candidate_not_admitted"] == 1
    assert result["sentiment_three_class"]["exclusion_reasons"]["not_an_original_title_or_post_unit"] == 1
    assert result["direction_agreement"]["denominator"] == 1
    assert result["direction_agreement"]["exclusion_reasons"]["holdout_candidate_not_admitted"] == 1


def test_event_direction_result_is_emitted_for_independent_review(tmp_path):
    case = _case("eval-direction", "event-direction", "record-direction", "neutral", "calming")
    case["split"] = "holdout"
    result = _evaluate(tmp_path, [case], [_labels(case)], {"event-direction": "calming"})

    assert result["direction_agreement"]["numerator"] == 1
    assert result["direction_agreement"]["denominator"] == 1
    assert result["direction_agreement"]["scored_events"] == [{
        "event_id": "event-direction", "evaluation_id": "eval-direction",
        "unit_id": "record-direction", "prediction": "calming",
        "truth": "calming", "correct": True,
    }]


def test_ai_reference_agreement_is_separate_from_human_gold_metrics(tmp_path):
    case = _case("eval-ai", "event-ai", "record-ai", "negative")
    ai_reference = {
        "reference_model": "fixture-ai",
        "annotations": [{
            "evaluation_id": case["evaluation_id"],
            "event_id": case["event_id"],
            "unit_id": case["unit_id"],
            "source_record_id": case["target_record_id"],
            "text_sha256": case["text_sha256"],
            "label": "neutral",
            "human_verified": False,
            "formal_human_ground_truth_eligible": False,
            "needs_review": False,
        }],
    }
    result = _evaluate(tmp_path, [case], [], ai_reference=ai_reference)

    assert result["sentiment_three_class"]["denominator"] == 0
    assert result["sentiment_three_class"]["value"] is None
    assert result["ai_reference_agreement"]["numerator"] == 0
    assert result["ai_reference_agreement"]["denominator"] == 1
    assert result["ai_reference_agreement"]["value"] == 0
    assert result["ai_reference_agreement"]["formal_human_ground_truth"] is False
