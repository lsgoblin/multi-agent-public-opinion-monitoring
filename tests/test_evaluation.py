import hashlib
import json

import pytest

from riskshield.evaluation import (
    DIRECTIONS,
    DIRECTION_DIAGNOSTIC_CLASSES,
    INPUT_SCHEMA,
    LABEL_SCHEMA,
    _direction_three_class_diagnostic,
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
        adjudication = label_entry.get("adjudication")
        if isinstance(adjudication, dict):
            evidence.setdefault("adjudication", {})[label_entry["unit_id"]] = adjudication.get("final_label")
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


# --------------------------------------------------------------------------
# recorded adjudication of a label disagreement
# --------------------------------------------------------------------------

def _adjudicated(entry, final_label, **overrides):
    """Attach a recorded adjudication block to a label entry."""
    block = {
        "adjudicator_id": "project owner",
        "final_label": final_label,
        "rule_source": "label-rule-v1.json",
        "rule_arm": "negative_arm: the title states the subject's own adverse outcome",
        "decision": "resolved_in_favour_of_the_negative_arm_of_the_recorded_rule",
        "original_answers_preserved": True,
        "evidence_refs": [_ref(f"/adjudication/{entry['unit_id']}")],
    }
    block.update(overrides)
    adjudicated = dict(entry)
    adjudicated["adjudication"] = block
    return adjudicated


def test_a_recorded_adjudication_resolves_a_disagreement(tmp_path):
    case = _case("disputed", "event-disputed", "record-disputed", "negative")
    entry = _adjudicated(_labels(case, ("neutral", "negative")), "negative")

    result = _evaluate(tmp_path, [case], [entry])

    assert result["sentiment_three_class"]["denominator"] == 1
    assert result["sentiment_three_class"]["numerator"] == 1
    scored = result["sentiment_three_class"]["scored_units"][0]
    assert scored["truth"] == "negative"
    assert scored["label_basis"] == "adjudicated_disagreement"


def test_the_original_answers_survive_the_adjudication(tmp_path):
    case = _case("disputed", "event-disputed", "record-disputed", "negative")
    entry = _adjudicated(_labels(case, ("neutral", "negative")), "negative")

    result = _evaluate(tmp_path, [case], [entry])

    decision = result["sentiment_three_class"]["scored_units"][0]["label_decision"]
    assert decision["original_annotation_labels"] == ["neutral", "negative"]
    assert decision["original_annotator_ids"] == ["annotator-0", "annotator-1"]
    assert decision["adjudicated_by"] == "project owner"
    assert decision["adjudication_rule_arm"].startswith("negative_arm")


@pytest.mark.parametrize("overrides,reason", [
    ({"adjudicator_id": "  "}, "adjudication_missing_named_adjudicator"),
    ({"final_label": "mixed"}, "adjudication_final_label_not_in_formal_classes"),
    ({"original_answers_preserved": False}, "adjudication_original_answers_not_preserved"),
    ({"rule_source": ""}, "adjudication_missing_recorded_basis"),
    ({"evidence_refs": []}, "adjudication_evidence_missing_or_mismatched"),
])
def test_an_invalid_adjudication_keeps_the_unit_excluded(tmp_path, overrides, reason):
    case = _case("disputed", "event-disputed", "record-disputed", "negative")
    entry = _adjudicated(_labels(case, ("neutral", "negative")),
                         **{"final_label": "negative", **overrides})

    result = _evaluate(tmp_path, [case], [entry])

    assert result["sentiment_three_class"]["denominator"] == 0
    reasons = result["sentiment_three_class"]["exclusion_reasons"]
    assert "independent_labels_disagree_no_adjudication" in reasons
    assert reason in reasons


def test_an_adjudication_cannot_stand_in_for_a_missing_second_label(tmp_path):
    case = _case("single", "event-single", "record-single", "negative")
    entry = _adjudicated(_labels(case, ("negative",)), "negative")

    result = _evaluate(tmp_path, [case], [entry])

    assert result["sentiment_three_class"]["denominator"] == 0
    assert result["sentiment_three_class"]["exclusion_reasons"][
        "fewer_than_two_independent_human_labels"] == 1


def test_an_adjudication_is_ignored_when_the_labels_already_agree(tmp_path):
    case = _case("agreed", "event-agreed", "record-agreed", "neutral")
    entry = _adjudicated(_labels(case, ("neutral", "neutral")), "negative")

    result = _evaluate(tmp_path, [case], [entry])

    scored = result["sentiment_three_class"]["scored_units"][0]
    assert scored["truth"] == "neutral"
    assert scored["label_basis"] == "unanimous_independent_labels"


def test_label_basis_accounting_keeps_agreement_and_adjudication_apart(tmp_path):
    agreed = _case("agreed", "event-agreed", "record-agreed", "neutral")
    disputed = _case("disputed", "event-disputed", "record-disputed", "negative")
    entries = [_labels(agreed), _adjudicated(_labels(disputed, ("neutral", "negative")), "negative")]

    result = _evaluate(tmp_path, [agreed, disputed], entries)
    accounting = result["label_basis_accounting"]

    assert accounting["raw_agreement"]["units"] == 1
    assert accounting["adjudicated"]["units"] == 1
    assert accounting["coverage_after_adjudication"]["units"] == 2
    assert accounting["excluded_label_disagreement_unresolved"]["units"] == 0
    assert accounting["raw_agreement_share_of_covered"] == {
        "numerator": 1, "denominator": 2, "value": 0.5}


def test_label_basis_accounting_reports_an_unresolved_disagreement(tmp_path):
    case = _case("disputed", "event-disputed", "record-disputed", "negative")

    result = _evaluate(tmp_path, [case], [_labels(case, ("neutral", "negative"))])
    accounting = result["label_basis_accounting"]

    assert accounting["raw_agreement"]["units"] == 0
    assert accounting["adjudicated"]["units"] == 0
    assert accounting["coverage_after_adjudication"]["units"] == 0
    assert accounting["excluded_label_disagreement_unresolved"]["units"] == 1
    assert accounting["raw_agreement_share_of_covered"]["value"] is None
    assert accounting["raw_agreement_share_of_covered"]["undefined_reason"]

# --------------------------------------------------------------------------
# the three-class direction diagnostic
# --------------------------------------------------------------------------

def _diag_row(event_id, truth, prediction, *, split='holdout', resolved=True,
              validity_reasons=None):
    return {
        "evaluation_id": f"{event_id}__direction", "event_id": event_id,
        "unit_id": event_id, "truth": truth, "prediction": prediction, "split": split,
        "truth_is_a_class": truth in DIRECTION_DIAGNOSTIC_CLASSES,
        "truth_is_declared_unscorable": truth is not None
        and truth not in DIRECTION_DIAGNOSTIC_CLASSES,
        "prediction_resolved": resolved,
        "validity_reasons": list(validity_reasons or []),
        "binary_reasons": [],
    }


def test_stable_is_scored_as_its_own_class_and_never_mapped():
    """A prediction of calming for a stable event is wrong, not a near miss."""
    block = _direction_three_class_diagnostic([
        _diag_row("a", "stable", "calming"),
        _diag_row("b", "stable", "stable"),
    ])

    matrix = block["confusion_matrix_actual_by_predicted"]
    assert block["classes"] == list(DIRECTION_DIAGNOSTIC_CLASSES)
    assert matrix["stable"]["calming"] == 1
    assert matrix["stable"]["stable"] == 1
    assert matrix["calming"]["stable"] == 0, "calming must not absorb a stable event"
    assert block["denominator"] == 2 and block["numerator"] == 1
    assert block["replaces_the_binary_direction_metric"] is False


def test_a_wrong_prediction_stays_in_the_diagnostic_denominator():
    block = _direction_three_class_diagnostic([
        _diag_row("a", "calming", "escalation"),
        _diag_row("b", "escalation", "calming"),
    ])

    assert block["denominator"] == 2
    assert block["numerator"] == 0
    assert block["value"] == 0.0
    assert len(block["scored_events"]) == 2


def test_a_prediction_outside_the_three_classes_counts_as_wrong():
    block = _direction_three_class_diagnostic([_diag_row("a", "calming", "sideways")])

    assert block["denominator"] == 1
    assert block["numerator"] == 0
    assert block["predictions_outside_the_three_class_space"] == 1


def test_a_declared_cannot_judge_is_not_scored_and_is_not_stable():
    """A refusal to classify is not an observation of stability."""
    block = _direction_three_class_diagnostic([
        _diag_row("a", "insufficient_evidence", "calming"),
        _diag_row("b", "cannot_judge", "escalation"),
        _diag_row("c", "calming", "calming"),
    ])

    assert block["class_support"]["stable"] == 0
    assert block["denominator"] == 1
    assert block["not_scored"]["declared_cannot_judge"]["count"] == 2
    assert block["not_scored"]["declared_cannot_judge"]["event_ids"] == ["a", "b"]


def test_an_unlabelled_event_is_not_counted_as_stable_either():
    block = _direction_three_class_diagnostic([
        _diag_row("a", None, "calming"),
        _diag_row("b", "stable", "stable"),
    ])

    assert block["denominator"] == 1
    assert block["class_support"]["stable"] == 1
    assert block["not_scored"]["no_label_at_all"]["count"] == 1


def test_a_label_without_a_prediction_is_recorded_rather_than_filled_in():
    block = _direction_three_class_diagnostic([
        _diag_row("a", "stable", None, resolved=False),
        _diag_row("b", "stable", "stable"),
    ])

    assert block["denominator"] == 1
    coverage = block["coverage"]
    assert coverage["candidates_seen"] == 2
    assert coverage["valid_after_the_shared_checks"] == 2
    assert coverage["eligible_events"] == 2
    assert coverage["scored_events"] == 1
    assert coverage["share"] == 0.5
    assert block["not_scored"]["label_but_no_recorded_prediction"]["event_ids"] == ["a"]


def test_the_diagnostic_keeps_development_and_holdout_apart():
    block = _direction_three_class_diagnostic([
        _diag_row("a", "stable", "calming", split="development"),
        _diag_row("b", "stable", "stable", split="holdout"),
        _diag_row("c", "stable", "stable", split="holdout"),
    ])

    assert block["by_split"]["development"]["scored_events"] == 1
    assert block["by_split"]["development"]["numerator"] == 0
    assert block["by_split"]["holdout"]["scored_events"] == 2
    assert block["by_split"]["holdout"]["numerator"] == 2
    assert block["numerator"] == 2 and block["denominator"] == 3


def test_the_diagnostic_can_be_switched_off_without_changing_anything_else():
    block = _direction_three_class_diagnostic([_diag_row("a", "stable", "stable")],
                                              enabled=False)

    assert block["enabled"] is False
    assert block["value"] is None
    assert block["undefined_reason"]


def test_the_binary_direction_space_is_untouched_by_the_diagnostic():
    """The diagnostic must not widen the brief's own metric."""
    assert DIRECTIONS == ("escalation", "calming")
    assert "stable" not in DIRECTIONS

@pytest.mark.parametrize("reason", [
    "synthetic_or_nonhistorical_data_excluded",
    "source_visible_after_cutoff",
    "unknown_or_invalid_visible_time",
    "missing_future_outcome_evidence",
    "missing_or_invalid_cutoff",
    "missing_or_mismatched_source_record_evidence",
    "holdout_candidate_not_admitted",
    "not_an_original_title_or_post_unit",
    "original_text_hash_missing_or_mismatched",
])
def test_the_diagnostic_drops_events_that_fail_any_shared_check(reason):
    """The diagnostic relaxes the label space and nothing else.

    Each of these is a check the binary metric already applies. An event failing
    one must leave the diagnostic too, with its reason attached -- otherwise the
    wider label space would quietly become a wider set of admissible material.
    """
    block = _direction_three_class_diagnostic([
        _diag_row("kept", "stable", "escalation"),
        _diag_row("dropped", "stable", "stable", validity_reasons=[reason]),
    ])

    assert block["denominator"] == 1
    assert [row["event_id"] for row in block["scored_events"]] == ["kept"]
    assert block["coverage"]["candidates_seen"] == 2
    assert block["coverage"]["valid_after_the_shared_checks"] == 1
    assert block["gate_exclusions"]["reasons"] == {reason: 1}
    assert block["gate_exclusions"]["events_or_rows"][0]["event_id"] == "dropped"


def test_a_stable_label_needs_outcome_evidence_like_any_other():
    """stable is a class, so asserting it requires evidence just as the others do."""
    block = _direction_three_class_diagnostic([
        _diag_row("a", "stable", "stable"),
        _diag_row("b", "stable", "stable",
                  validity_reasons=["missing_future_outcome_evidence"]),
    ])

    assert block["denominator"] == 1
    assert block["numerator"] == 1
    assert block["gate_exclusions"]["reasons"]["missing_future_outcome_evidence"] == 1


def test_the_diagnostic_scores_one_event_once():
    """Two rows for one event are ambiguous, not two data points."""
    block = _direction_three_class_diagnostic([
        _diag_row("a", "stable", "stable"),
        _diag_row("a", "stable", "escalation"),
    ])

    assert block["denominator"] == 0
    assert block["coverage"]["candidates_seen"] == 1
    assert block["gate_exclusions"]["reasons"] == {
        "multiple_direction_predictions_for_one_event": 1}


def test_coverage_reports_every_stage_of_the_funnel():
    block = _direction_three_class_diagnostic([
        _diag_row("scored", "stable", "stable"),
        _diag_row("no_prediction", "stable", None, resolved=False),
        _diag_row("cannot_judge", "insufficient_evidence", "calming"),
        _diag_row("failed_a_check", "stable", "stable",
                  validity_reasons=["source_visible_after_cutoff"]),
    ])

    coverage = block["coverage"]
    assert coverage["candidates_seen"] == 4
    assert coverage["valid_after_the_shared_checks"] == 3
    assert coverage["eligible_events"] == 2
    assert coverage["scored_events"] == 1
    assert coverage["share"] == 0.25
    assert block["gate_exclusions"]["count"] == 1
    assert block["not_scored"]["declared_cannot_judge"]["count"] == 1
    assert block["not_scored"]["label_but_no_recorded_prediction"]["count"] == 1
