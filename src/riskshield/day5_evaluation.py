"""Offline, source-grounded Day 5 evaluation.

Evaluation labels are read from a separate file and are never passed to the
case store, graph, or simulation code. This module uses only the standard
library and treats evidence references as inert local JSON pointers.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


INPUT_SCHEMA = "riskshield.day5.evaluation-inputs.v1"
LABEL_SCHEMA = "riskshield.day5.future-labels.v1"
REPORT_SCHEMA = "riskshield.day5.evaluation-report.v1"
SENTIMENTS = ("positive", "neutral", "negative")
DIRECTIONS = ("escalation", "calming")
_MISSING = object()


class EvaluationInputError(ValueError):
    """Raised when an evaluation document cannot be safely interpreted."""


def _read_json(path: Path) -> tuple[dict[str, Any], str]:
    raw = path.read_bytes()
    try:
        value = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise EvaluationInputError(f"Invalid UTF-8 JSON: {path}") from exc
    if not isinstance(value, dict):
        raise EvaluationInputError(f"Top-level JSON value must be an object: {path}")
    return value, hashlib.sha256(raw).hexdigest()


def _pointer_value(document: Any, pointer: str) -> Any:
    if pointer in ("", "/"):
        return document
    if not isinstance(pointer, str) or not pointer.startswith("/"):
        raise ValueError("JSON pointer must be empty or start with '/'.")
    current = document
    for raw_part in pointer[1:].split("/"):
        part = raw_part.replace("~1", "/").replace("~0", "~")
        if isinstance(current, list):
            current = current[int(part)]
        elif isinstance(current, dict):
            current = current[part]
        else:
            raise KeyError(pointer)
    return current


def _parse_time(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None and parsed.utcoffset() is not None else None


def _resolve_ref(ref: Any, root: Path) -> Any:
    if not isinstance(ref, dict):
        return _MISSING
    path = ref.get("path")
    pointer = ref.get("pointer", "")
    if not isinstance(path, str) or not path.strip() or not isinstance(pointer, str):
        return _MISSING
    file_path = (root / path).resolve()
    try:
        value = json.loads(file_path.read_text(encoding="utf-8"))
        return _pointer_value(value, pointer)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, KeyError,
            IndexError, TypeError, ValueError):
        return _MISSING


def _ref_matches(ref: Any, expected: Any, root: Path) -> bool:
    actual = _resolve_ref(ref, root)
    if actual is _MISSING:
        return False
    if expected is _MISSING:
        return actual is not None and actual != ""
    if expected is None:
        return False
    if isinstance(actual, (int, float)) and not isinstance(actual, bool):
        if ref.get("format") == "unix_seconds":
            try:
                actual = datetime.fromtimestamp(actual, timezone.utc)
                target = _parse_time(expected)
                return target is not None and actual == target.astimezone(timezone.utc)
            except (OverflowError, OSError, TypeError, ValueError):
                return False
    if isinstance(actual, str) and isinstance(expected, str):
        actual_time, expected_time = _parse_time(actual), _parse_time(expected)
        if actual_time is not None and expected_time is not None:
            return actual_time == expected_time
    return actual == expected


def _ref_object(ref: Any) -> bool:
    return isinstance(ref, dict) and bool(ref.get("path")) and isinstance(ref.get("pointer", ""), str)


def _source_reasons(case: dict[str, Any], root: Path) -> list[str]:
    reasons: list[str] = []
    if case.get("data_mode") != "real_historical":
        reasons.append("synthetic_or_nonhistorical_data_excluded")
    if not isinstance(case.get("source_version"), str) or not case["source_version"].strip():
        reasons.append("missing_case_source_version")
    cutoff = _parse_time(case.get("cutoff"))
    if cutoff is None:
        reasons.append("missing_or_invalid_cutoff")
    if not _ref_matches(case.get("cutoff_evidence_ref"), case.get("cutoff"), root):
        reasons.append("missing_or_mismatched_cutoff_evidence")
    sources = case.get("sources")
    if not isinstance(sources, list) or not sources:
        reasons.append("missing_source_record")
        return reasons
    source_record_ids = {s.get("record_id") for s in sources if isinstance(s, dict)}
    target_record_id = case.get("target_record_id")
    if target_record_id and target_record_id not in source_record_ids:
        reasons.append("target_record_missing_from_cutoff_sources")
    for source in sources:
        if not isinstance(source, dict):
            reasons.append("invalid_source_record")
            continue
        if not isinstance(source.get("source_version"), str) or not source["source_version"].strip():
            reasons.append("missing_source_record_version")
        if not _ref_matches(source.get("source_record_ref"), source.get("record_id"), root):
            reasons.append("missing_or_mismatched_source_record_evidence")
        if not _ref_matches(source.get("source_version_ref"), source.get("source_version"), root):
            reasons.append("missing_or_mismatched_source_version_evidence")
        available_at = _parse_time(source.get("available_at"))
        if available_at is None:
            reasons.append("unknown_or_invalid_visible_time")
        elif cutoff is not None and available_at > cutoff:
            reasons.append("source_visible_after_cutoff")
        if not _ref_matches(source.get("source_available_at_ref"), source.get("available_at"), root):
            reasons.append("missing_or_mismatched_source_record_time")
        if not _ref_matches(source.get("visibility_evidence_ref"), source.get("available_at"), root):
            reasons.append("missing_or_mismatched_visibility_evidence")
    return sorted(set(reasons))


def _labels_for_case(case: dict[str, Any], label_map: dict[str, dict[str, Any]], root: Path) -> tuple[str | None, list[str]]:
    reasons: list[str] = []
    entry = label_map.get(case.get("evaluation_id", ""))
    if entry is None:
        return None, ["missing_separate_evaluation_label"]
    if entry.get("event_id") != case.get("event_id") or entry.get("unit_id") != case.get("unit_id"):
        return None, ["label_input_identity_mismatch"]
    if entry.get("source_record_id") != case.get("target_record_id"):
        return None, ["label_source_record_mismatch"]
    annotations = entry.get("sentiment_annotations", [])
    if not isinstance(annotations, list):
        return None, ["invalid_human_annotation_list"]
    eligible: list[dict[str, Any]] = []
    seen_annotators: set[str] = set()
    for annotation in annotations:
        if not isinstance(annotation, dict):
            continue
        annotator = annotation.get("annotator_id")
        if (annotation.get("independent") is not True
                or annotation.get("prediction_seen") is not False
                or not isinstance(annotator, str) or not annotator.strip()
                or annotator in seen_annotators):
            continue
        if annotation.get("label") not in SENTIMENTS:
            continue
        refs = annotation.get("evidence_refs", [])
        if not isinstance(refs, list) or not any(
                _ref_matches(ref, annotation["label"], root) for ref in refs):
            continue
        seen_annotators.add(annotator)
        eligible.append(annotation)
    if len(eligible) < 2:
        reasons.append("fewer_than_two_independent_human_labels")
        if any(isinstance(a, dict) and a.get("independent") is not True for a in annotations):
            reasons.append("one_or_more_annotations_not_independent_for_event")
        if any(isinstance(a, dict) and a.get("prediction_seen") is not False for a in annotations):
            reasons.append("annotation_prediction_blinding_not_established")
    labels = {a["label"] for a in eligible}
    if len(eligible) >= 2 and len(labels) != 1:
        reasons.append("independent_labels_disagree_no_adjudication")
    if reasons:
        return None, sorted(set(reasons))
    return eligible[0]["label"], []


def _prediction_label(predictions: Any, metric: str, root: Path) -> tuple[str | None, list[str]]:
    reasons: list[str] = []
    prediction = predictions.get(metric) if isinstance(predictions, dict) else None
    if not isinstance(prediction, dict):
        return None, [f"missing_{metric}_prediction"]
    if not isinstance(prediction.get("execution_mode"), str) or not prediction["execution_mode"].strip():
        reasons.append(f"missing_{metric}_execution_mode")
    label = prediction.get("label")
    allowed = SENTIMENTS if metric == "sentiment" else DIRECTIONS
    if label not in allowed:
        reasons.append(f"missing_or_invalid_{metric}_prediction")
    refs = prediction.get("result_evidence_refs", [])
    if label in allowed and (not isinstance(refs, list) or not any(
            _ref_matches(ref, label, root) for ref in refs)):
        reasons.append(f"missing_or_mismatched_{metric}_result_evidence")
    if reasons:
        return None, sorted(set(reasons))
    return label, []


def _ratio(numerator: int, denominator: int, reason: str) -> dict[str, Any]:
    return {
        "numerator": numerator,
        "denominator": denominator,
        "value": round(numerator / denominator, 6) if denominator else None,
        "undefined_reason": None if denominator else reason,
    }


def _reason_counts(exclusions: list[dict[str, Any]]) -> dict[str, int]:
    counts: Counter[str] = Counter()
    for exclusion in exclusions:
        counts.update(exclusion["reasons"])
    return dict(sorted(counts.items()))


def evaluate_documents(
    inputs: dict[str, Any],
    labels: dict[str, Any],
    *,
    input_sha256: str | None = None,
    labels_sha256: str | None = None,
    evidence_root: Path | str = ".",
) -> dict[str, Any]:
    """Compute metrics from separated inputs and labels; never touches simulation state."""
    if inputs.get("schema") != INPUT_SCHEMA:
        raise EvaluationInputError(f"Expected input schema {INPUT_SCHEMA}.")
    if labels.get("schema") != LABEL_SCHEMA:
        raise EvaluationInputError(f"Expected label schema {LABEL_SCHEMA}.")
    if not isinstance(inputs.get("dataset_version"), str) or not inputs["dataset_version"].strip():
        raise EvaluationInputError("A nonempty dataset_version is required.")
    if not isinstance(labels.get("label_version"), str) or not labels["label_version"].strip():
        raise EvaluationInputError("A nonempty label_version is required.")
    cases = inputs.get("cases")
    outcomes = labels.get("events")
    if not isinstance(cases, list) or not isinstance(outcomes, list):
        raise EvaluationInputError("Input cases and label events must be arrays.")
    if any(not isinstance(case, dict) for case in cases):
        raise EvaluationInputError("Every input case must be an object.")
    if any(not isinstance(entry, dict) for entry in outcomes):
        raise EvaluationInputError("Every future-label event must be an object.")
    sentiment_label_entries = labels.get("sentiment_labels", [])
    if not isinstance(sentiment_label_entries, list):
        raise EvaluationInputError("sentiment_labels must be an array.")
    root = Path(evidence_root).resolve()

    case_ids = [case.get("evaluation_id") for case in cases if isinstance(case, dict)]
    if len(case_ids) != len(cases) or any(not isinstance(value, str) or not value for value in case_ids):
        raise EvaluationInputError("Every case must have a nonempty evaluation_id.")
    if any(not isinstance(case.get("event_id"), str) or not case["event_id"]
           or not isinstance(case.get("unit_id"), str) or not case["unit_id"] for case in cases):
        raise EvaluationInputError("Every case must have a nonempty event_id and unit_id.")
    if len(set(case_ids)) != len(case_ids):
        raise EvaluationInputError("evaluation_id values must be unique.")
    outcome_ids = [entry.get("event_id") for entry in outcomes if isinstance(entry, dict)]
    if len(outcome_ids) != len(outcomes) or any(not isinstance(value, str) or not value for value in outcome_ids):
        raise EvaluationInputError("Every future-label event must have a nonempty event_id.")
    if len(set(outcome_ids)) != len(outcome_ids):
        raise EvaluationInputError("Future direction labels must be unique by event_id.")

    label_map: dict[str, dict[str, Any]] = {}
    for entry in sentiment_label_entries:
        if not isinstance(entry, dict) or not isinstance(entry.get("evaluation_id"), str):
            raise EvaluationInputError("Every sentiment label entry must have an evaluation_id.")
        if entry["evaluation_id"] in label_map:
            raise EvaluationInputError("Sentiment labels must be unique by evaluation_id.")
        label_map[entry["evaluation_id"]] = entry
    outcome_map = {entry["event_id"]: entry for entry in outcomes}
    record_units = [(case.get("event_id"), case.get("unit_id")) for case in cases]
    if len(set(record_units)) != len(record_units):
        raise EvaluationInputError("The same source record cannot appear twice for one event.")

    sentiment_rows: list[dict[str, Any]] = []
    sentiment_exclusions: list[dict[str, Any]] = []
    direction_candidates: dict[str, list[dict[str, Any]]] = defaultdict(list)
    direction_invalid_rows: dict[str, list[dict[str, Any]]] = defaultdict(list)
    direction_exclusions: list[dict[str, Any]] = []
    input_exclusions: list[dict[str, Any]] = []
    real_event_ids: set[str] = set()
    cutoff_qualified_event_ids: set[str] = set()
    real_record_ids: set[tuple[str, str]] = set()
    nonreal_rows = 0

    for case in cases:
        event_id = case.get("event_id")
        unit_id = case.get("unit_id")
        if case.get("data_mode") == "real_historical" and isinstance(event_id, str) and event_id:
            real_event_ids.add(event_id)
            if isinstance(unit_id, str) and unit_id:
                real_record_ids.add((event_id, unit_id))
        else:
            nonreal_rows += 1
            input_exclusions.append({
                "evaluation_id": case["evaluation_id"], "event_id": event_id,
                "unit_id": unit_id, "reasons": ["synthetic_or_nonhistorical_data_excluded"],
            })
        base_reasons = _source_reasons(case, root)
        if case.get("data_mode") == "real_historical" and not base_reasons:
            cutoff_qualified_event_ids.add(event_id)
        if not isinstance(event_id, str) or not event_id:
            base_reasons.append("missing_event_id")
        if not isinstance(unit_id, str) or not unit_id:
            base_reasons.append("missing_unit_id")

        gold, label_reasons = _labels_for_case(case, label_map, root)
        predicted, prediction_reasons = _prediction_label(case.get("predictions"), "sentiment", root)
        s_reasons = sorted(set(base_reasons + label_reasons + prediction_reasons))
        if not s_reasons:
            sentiment_rows.append({
                "evaluation_id": case["evaluation_id"], "event_id": event_id,
                "unit_id": unit_id, "truth": gold, "prediction": predicted,
            })
        else:
            sentiment_exclusions.append({"evaluation_id": case["evaluation_id"], "event_id": event_id,
                                         "unit_id": unit_id, "reasons": s_reasons})

        predicted_direction, direction_reasons = _prediction_label(
            case.get("predictions"), "direction", root)
        outcome = outcome_map.get(event_id, {})
        observed = outcome.get("observed_direction") if isinstance(outcome, dict) else None
        if observed not in DIRECTIONS:
            direction_reasons.append("missing_or_unresolved_observed_direction_label")
        else:
            refs = outcome.get("evidence_refs", [])
            if not isinstance(refs, list) or not any(_ref_matches(ref, observed, root) for ref in refs):
                direction_reasons.append("missing_future_outcome_evidence")
        d_reasons = sorted(set(base_reasons + direction_reasons))
        if not d_reasons:
            direction_candidates[event_id].append({
                "evaluation_id": case["evaluation_id"], "unit_id": unit_id,
                "prediction": predicted_direction, "truth": observed,
            })
        elif event_id and case.get("data_mode") == "real_historical":
            direction_invalid_rows[event_id].append({
                "evaluation_id": case["evaluation_id"], "unit_id": unit_id,
                "reasons": d_reasons,
            })

    # A direction score is an event-level result. Multiple source rows or runs
    # for one event cannot increase its denominator.
    direction_rows: list[dict[str, Any]] = []
    all_direction_event_ids = set(direction_candidates) | set(direction_invalid_rows)
    for event_id in sorted(all_direction_event_ids):
        candidates = direction_candidates.get(event_id, [])
        if len(candidates) != 1:
            if len(candidates) > 1:
                direction_exclusions.append({
                    "event_id": event_id,
                    "evaluation_ids": [row["evaluation_id"] for row in candidates],
                    "reasons": ["multiple_direction_predictions_for_one_event"],
                })
            else:
                invalid_rows = direction_invalid_rows.get(event_id, [])
                direction_exclusions.append({
                    "event_id": event_id,
                    "evaluation_ids": [row["evaluation_id"] for row in invalid_rows],
                    "reasons": sorted({reason for row in invalid_rows for reason in row["reasons"]}),
                })
            continue
        direction_rows.append({"event_id": event_id, **candidates[0]})

    matrix = {truth: {prediction: 0 for prediction in SENTIMENTS} for truth in SENTIMENTS}
    for row in sentiment_rows:
        matrix[row["truth"]][row["prediction"]] += 1
    sentiment_correct = sum(matrix[label][label] for label in SENTIMENTS)
    n_sentiment = len(sentiment_rows)

    tp = fp = fn = tn = 0
    for row in sentiment_rows:
        truth_negative = row["truth"] == "negative"
        prediction_negative = row["prediction"] == "negative"
        if truth_negative and prediction_negative:
            tp += 1
        elif not truth_negative and prediction_negative:
            fp += 1
        elif truth_negative and not prediction_negative:
            fn += 1
        else:
            tn += 1
    binary_matrix = {
        "actual_negative": {"predicted_negative": tp, "predicted_nonnegative": fn},
        "actual_nonnegative": {"predicted_negative": fp, "predicted_nonnegative": tn},
    }
    direction_matrix = {truth: {prediction: 0 for prediction in DIRECTIONS} for truth in DIRECTIONS}
    for row in direction_rows:
        direction_matrix[row["truth"]][row["prediction"]] += 1
    direction_correct = sum(direction_matrix[label][label] for label in DIRECTIONS)
    n_direction = len(direction_rows)

    return {
        "schema": REPORT_SCHEMA,
        "evaluation_version": inputs.get("dataset_version", "unspecified"),
        "label_version": labels.get("label_version", "unspecified"),
        "source_metrics": {
            "mapping_document": "docs/design/05-赛题指标映射表.md",
            "definition_status": {
                "negative_identification_accuracy": "unresolved; report negative precision and binary accuracy separately",
                "historical_event_count": "unresolved; source range remains 10–20 distinct real events",
                "direction_stability_and_abstention": "unresolved; only explicit escalation/calming pairs are scoreable",
            },
            "thresholds_preserved": {
                "negative_identification_accuracy": "at least 90% (formal formula unresolved)",
                "negative_recall": "at least 85%",
                "three_class_sentiment_accuracy": "at least 88%",
                "direction_agreement": "at least 70%",
                "historical_real_events": "10–20; exact minimum unresolved",
            },
        },
        "sample_size": {
            "input_rows": len(cases),
            "nonhistorical_rows_excluded": nonreal_rows,
            "distinct_real_events": len(real_event_ids),
            "distinct_cutoff_qualified_real_events": len(cutoff_qualified_event_ids),
            "distinct_real_source_records": len(real_record_ids),
            "sentiment_evaluable_records": n_sentiment,
            "direction_evaluable_events": n_direction,
        },
        "input_exclusions": {
            "count": len(input_exclusions),
            "reasons": _reason_counts(input_exclusions),
            "excluded_rows": input_exclusions,
        },
        "sentiment_three_class": {
            **_ratio(sentiment_correct, n_sentiment, "no records have a valid prediction and unanimous independent labels"),
            "sample_size": n_sentiment,
            "confusion_matrix_actual_by_predicted": matrix,
            "class_support": {label: sum(matrix[label].values()) for label in SENTIMENTS},
            "exclusion_count": len(sentiment_exclusions),
            "exclusion_reasons": _reason_counts(sentiment_exclusions),
            "excluded_rows": sentiment_exclusions,
        },
        "negative_identification": {
            "definition_status": "unresolved by V2-Q03; both candidate definitions are reported",
            "negative_confusion_matrix": binary_matrix,
            "counts": {"true_positive": tp, "false_positive": fp, "false_negative": fn, "true_negative": tn},
            "precision": {
                **_ratio(tp, tp + fp, "no predictions were classified as negative"),
                "formula": "TP / (TP + FP)",
            },
            "recall": {
                **_ratio(tp, tp + fn, "no human-labeled negative records"),
                "formula": "TP / (TP + FN)",
            },
            "binary_accuracy": {
                **_ratio(tp + tn, tp + fp + fn + tn, "no sentiment records are evaluable"),
                "formula": "(TP + TN) / (TP + FP + FN + TN)",
            },
            "sample_size": n_sentiment,
            "exclusion_count": len(sentiment_exclusions),
            "exclusion_reasons": _reason_counts(sentiment_exclusions),
        },
        "direction_agreement": {
            **_ratio(direction_correct, n_direction, "no distinct event has both a direction prediction and an evidence-backed observed direction label"),
            "sample_size_events": n_direction,
            "confusion_matrix_actual_by_predicted": direction_matrix,
            "exclusion_count": len(direction_exclusions),
            "exclusion_reasons": _reason_counts(direction_exclusions),
            "excluded_events_or_rows": direction_exclusions,
        },
        "provenance": {
            "input_sha256": input_sha256,
            "future_labels_sha256": labels_sha256,
            "future_labels_used_only_for_metric_calculation": True,
            "synthetic_data_in_formal_denominators": False,
            "multiple_sources_same_event_counted_as_distinct_direction_events": False,
        },
    }


def evaluate_files(inputs_path: Path, labels_path: Path) -> dict[str, Any]:
    inputs, input_hash = _read_json(inputs_path)
    labels, labels_hash = _read_json(labels_path)
    return evaluate_documents(inputs, labels, input_sha256=input_hash,
                              labels_sha256=labels_hash, evidence_root=Path.cwd())


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Compute source-grounded offline Day 5 evaluation metrics.")
    parser.add_argument("--inputs", required=True, type=Path, help="Versioned cutoff-only evaluation inputs JSON.")
    parser.add_argument("--future-labels", required=True, type=Path, help="Separate evaluation-label JSON; never passed to agents.")
    parser.add_argument("--out", required=True, type=Path, help="Path for the reproducible JSON report.")
    args = parser.parse_args(argv)
    try:
        report = evaluate_files(args.inputs, args.future_labels)
    except (EvaluationInputError, OSError) as exc:
        print(str(exc), file=sys.stderr)
        return 2
    args.out.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    args.out.write_text(payload, encoding="utf-8", newline="\n")
    print(json.dumps({
        "report": str(args.out),
        "three_class": report["sentiment_three_class"]["value"],
        "negative_precision": report["negative_identification"]["precision"]["value"],
        "negative_recall": report["negative_identification"]["recall"]["value"],
        "direction_agreement": report["direction_agreement"]["value"],
        "distinct_real_events": report["sample_size"]["distinct_real_events"],
    }, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
