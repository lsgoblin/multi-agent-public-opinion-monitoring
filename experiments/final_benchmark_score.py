"""Score an already sealed cutoff-only engineering batch against separate labels."""

from __future__ import annotations

import argparse
import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path

from riskshield.evaluation import INPUT_SCHEMA, LABEL_SCHEMA, evaluate_documents


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def read_json(path: Path) -> dict:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"Expected JSON object: {path}")
    return value


def checked(root: Path, relative: str, expected: str) -> Path:
    path = (root / relative).resolve()
    if not path.is_relative_to(root) or digest(path) != expected:
        raise ValueError(f"Sealed file changed: {relative}")
    return path


def run(root: Path, seal_path: Path, output_path: Path,
        ai_reference_path: Path | None = None,
        reconciliation_path: Path | None = None) -> dict:
    root = root.resolve()
    seal_path = seal_path.resolve()
    output_path = output_path.resolve()
    if not seal_path.is_relative_to(root) or not output_path.is_relative_to(root):
        raise ValueError("Score paths must be inside the workspace")
    seal = read_json(seal_path)
    if seal.get("schema") == "riskshield.final_benchmark.seal.v13":
        if ai_reference_path is not None:
            raise ValueError("AI references are not accepted by the v13/v24 formal score path")
        return _score_v13_against_v24(root, seal_path, output_path, seal,
                                      reconciliation_path)
    if seal.get("schema") != "riskshield.final_benchmark.prediction_seal.v1":
        raise ValueError("Unknown prediction seal")
    handoff_path = checked(root, seal["handoff_path"], seal["handoff_sha256"])
    checked(root, seal["input_path"], seal["input_sha256"])
    predictor_rel = seal.get("predictor_path", "experiments/final_benchmark_predict.py")
    protocol_rel = seal.get("protocol_path", "docs/records/final-scoring-protocol.md")
    checked(root, predictor_rel, seal["predictor_sha256"])
    checked(root, protocol_rel, seal["protocol_sha256"])
    prediction_path = checked(root, seal["prediction_path"], seal["prediction_sha256"])
    evaluation_path = checked(root, seal["evaluation_input_path"], seal["evaluation_input_sha256"])
    handoff = read_json(handoff_path)
    if handoff.get("input_sha256") != seal["input_sha256"]:
        raise ValueError("Handoff and seal input hashes differ")
    predictions = read_json(prediction_path)
    if predictions.get("input_sha256") != seal["input_sha256"]:
        raise ValueError("Prediction input hash differs from seal")
    evaluation = read_json(evaluation_path)
    if len(evaluation["cases"]) != seal["event_count"]:
        raise ValueError("Evaluation row count differs from seal")
    # Freeze integrity is checked only in this score-side process; predictor never opens it.
    version = handoff_path.stem.split("-v")[-1]
    freeze_path = root / f"data/evaluation/final-benchmark/manifests/freeze-v{version}.json"
    freeze = read_json(freeze_path)
    for entry in freeze["files"]:
        member = checked(root, entry["path"], entry["sha256"])
        if member.stat().st_size != entry["bytes"]:
            raise ValueError(f"Frozen file size changed: {entry['path']}")
    score_paths = handoff.get("score_side_may_read_after_prediction_freeze", [])
    if not isinstance(score_paths, list) or len(score_paths) not in {0, 2}:
        raise ValueError("Score-side evidence must be absent or include trajectory and sentiment files")
    frozen_paths = {entry["path"] for entry in freeze["files"]}
    if not set(score_paths) <= frozen_paths:
        raise ValueError("Score-side file is not in the freeze manifest")
    if score_paths:
        trajectory_path = (root / score_paths[0]).resolve()
        sentiment_path = (root / score_paths[1]).resolve()
        trajectory = read_json(trajectory_path)
        sentiment = read_json(sentiment_path)
    else:
        trajectory = {"dataset_version": "no-frozen-score-side-evidence-v1",
                      "statistics": {}, "events": []}
        sentiment = {"statistics": {}, "units": []}
    events = trajectory.get("events", [])
    units = sentiment.get("units", [])
    if not isinstance(events, list) or not isinstance(units, list):
        raise ValueError("Score-side evidence has no event or unit array")
    outcomes = {entry.get("event_id"): entry for entry in events}
    if len(outcomes) != len(events):
        raise ValueError("Duplicate event identity in score evidence")
    # Label families remain independent. A present sentiment packet does not
    # block trajectory scoring, and a trend label does not block sentiment.
    labels = {
        "schema": LABEL_SCHEMA,
        "label_version": trajectory["dataset_version"] + "__human_labels_only",
        "events": [],
        "sentiment_labels": _sentiment_label_entries(sentiment, evaluation["cases"]),
    }
    for case in evaluation["cases"]:
        evidence = outcomes.get(case["event_id"], {})
        direction = evidence.get("direction_label")
        evidence_refs = evidence.get("direction_label_evidence_refs",
                                     evidence.get("label_evidence_refs", []))
        eligible = (evidence.get("formal_trajectory_eligible") is True
                    and direction in {"escalation", "calming"})
        labels["events"].append({
            "event_id": case["event_id"],
            "observed_direction": direction if eligible else None,
            "evidence_refs": evidence_refs if eligible else [],
        })
    ai_reference = None
    ai_reference_hash = None
    if ai_reference_path is not None:
        ai_reference_path = ai_reference_path.resolve()
        if not ai_reference_path.is_relative_to(root):
            raise ValueError("AI reference path must be inside the workspace")
        ai_reference_hash = digest(ai_reference_path)
        ai_reference = read_json(ai_reference_path)
    metrics = evaluate_documents(
        evaluation, labels,
        input_sha256=digest(evaluation_path),
        labels_sha256=None,
        evidence_root=root,
        ai_reference=ai_reference,
    )
    per_event = []
    scored_directions = {row["event_id"]: row
                         for row in metrics["direction_agreement"]["scored_events"]}
    direction_exclusions = {event["event_id"]: event
                            for event in metrics["direction_agreement"]["excluded_events_or_rows"]}
    scored_sentiment = {row["evaluation_id"]: row
                        for row in metrics["sentiment_three_class"]["scored_units"]}
    sentiment_exclusions = {row["evaluation_id"]: row
                            for row in metrics["sentiment_three_class"]["excluded_rows"]}
    per_sentiment_unit = []
    for case in evaluation["cases"]:
        event_id = case["event_id"]
        trajectory_entry = outcomes.get(event_id)
        direction_row = scored_directions.get(event_id)
        direction_exclusion = direction_exclusions.get(event_id)
        sentiment_row = scored_sentiment.get(case["evaluation_id"])
        sentiment_exclusion = sentiment_exclusions.get(case["evaluation_id"])
        predictions = case.get("predictions")
        sentiment_prediction = (predictions.get("sentiment")
                                 if isinstance(predictions, dict) else None)
        per_sentiment_unit.append({
            "event_id": event_id,
            "unit_id": case["unit_id"],
            "unit_kind": case.get("unit_kind"),
            "prediction": sentiment_prediction.get("label")
            if isinstance(sentiment_prediction, dict) else None,
            "truth": sentiment_row["truth"] if sentiment_row else None,
            "scoreable": sentiment_row is not None,
            "correct": sentiment_row["correct"] if sentiment_row else None,
            "exclusion_reasons": (sentiment_exclusion["reasons"]
                                  if sentiment_exclusion else []),
        })
        per_event.append({
            "event_id": event_id,
            "case_id": case["evaluation_id"].split("__direction__")[0],
            "split": case["split"],
            "prediction": case["predictions"]["direction"]["label"],
            "prediction_mode": seal["execution_mode"],
            "observed_direction": (outcomes.get(event_id, {}).get("direction_label")
                                   if outcomes.get(event_id, {}).get("formal_trajectory_eligible") is True
                                   else None),
            "direction_scoreable": direction_row is not None,
            "direction_correct": direction_row["correct"] if direction_row else None,
            "direction_exclusion_reasons": (
                ["candidate_not_admitted_to_formal_split"] if case["split"] == "holdout_candidate" else
                ["missing_score_side_event"] if trajectory_entry is None else
                direction_exclusion["reasons"] if direction_exclusion else
                ["no_frozen_independent_human_direction_label"]),
            "sentiment_scoreable_units": int(sentiment_row is not None),
            "sentiment_exclusion_reasons": (sentiment_exclusion["reasons"]
                                            if sentiment_exclusion else []),
        })
    report = {
        "schema": "riskshield.final_benchmark.pilot_score.v1",
        "status": "engineering_flow_verified_formal_metrics_unavailable",
        "prediction_seal_path": seal_path.relative_to(root).as_posix(),
        "prediction_seal_sha256": digest(seal_path),
        "freeze_path": freeze_path.relative_to(root).as_posix(),
        "freeze_sha256": digest(freeze_path),
        "score_side_hashes": {path: digest(root / path) for path in score_paths},
        "ai_reference_sha256": ai_reference_hash,
        "event_count": len(per_event),
        "trend_human_labels": trajectory.get("statistics", {}).get("independent_human_trend_labels_completed"),
        "sentiment_human_labels": sentiment.get("statistics", {}).get("completed_human_labels"),
        "per_event": per_event,
        "per_sentiment_unit": per_sentiment_unit,
        "metrics": metrics,
        "limits": ["Fixed offline constant baseline is not a model forecast"
                   if seal.get("execution_mode", "").startswith("offline_constant")
                   else "Local-model predictions are engineering results until formally admitted",
                   "Only evidence-backed, formally eligible independent human labels enter official metrics",
                   "Event summaries are not original title-level sentiment units; AI references are exploratory only"],
    }
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with output_path.open("x", encoding="utf-8", newline="\n") as stream:
        stream.write(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n")
    return {"report": str(output_path), "events": len(per_event),
            "direction_denominator": metrics["direction_agreement"]["denominator"],
            "sentiment_denominator": metrics["sentiment_three_class"]["denominator"]}


def _sentiment_label_entries(sentiment: dict, cases: list[dict]) -> list[dict]:
    """Join label units to exact original-title/post cases without inferring labels."""
    case_by_unit = {}
    for case in cases:
        key = (case.get("event_id"), case.get("unit_id"))
        if (not all(isinstance(value, str) and value for value in key)
                or not isinstance(case.get("evaluation_id"), str)
                or not case["evaluation_id"]
                or key in case_by_unit):
            raise ValueError("Evaluation cases have missing or duplicate event/unit identity")
        case_by_unit[key] = case

    labels = []
    seen_units = set()
    for unit in sentiment.get("units", []):
        if not isinstance(unit, dict):
            continue
        unit_id = unit.get("unit_id")
        event_ref = unit.get("event_id_reference")
        if isinstance(event_ref, str):
            event_id = event_ref
            referenced_case_id = None
        elif isinstance(event_ref, dict):
            event_id = event_ref.get("event_id")
            referenced_case_id = event_ref.get("case_id")
        else:
            event_id = unit.get("event_id")
            referenced_case_id = unit.get("case_id")
        if not isinstance(event_id, str) or not event_id.strip():
            continue
        if unit.get("event_id") not in (None, event_id):
            continue
        case = case_by_unit.get((event_id, unit_id))
        if case is None:
            continue
        if (case.get("unit_kind") not in {"original_title", "original_post"}
                or not isinstance(case.get("target_record_id"), str)
                or not case["target_record_id"].strip()):
            continue
        if referenced_case_id is not None and referenced_case_id != case.get("case_id"):
            continue
        if unit.get("case_id") is not None and unit.get("case_id") != case.get("case_id"):
            continue
        if unit.get("split") is not None and unit.get("split") != case.get("split"):
            continue

        source = unit.get("source")
        unit_record_id = unit.get("source_record_id", unit.get("record_id"))
        if unit_record_id is None and isinstance(source, dict):
            unit_record_id = source.get("record_id")
        if unit_record_id is not None and unit_record_id != case["target_record_id"]:
            continue

        frozen_text = unit.get("frozen_text")
        frozen_hash = (hashlib.sha256(frozen_text.encode("utf-8")).hexdigest()
                       if isinstance(frozen_text, str) else None)
        declared_hash = unit.get("text_sha256", unit.get("title_sha256"))
        if (declared_hash is not None and frozen_hash is not None
                and declared_hash != frozen_hash):
            continue
        text_hash = declared_hash or frozen_hash
        if (not isinstance(text_hash, str) or len(text_hash) != 64
                or any(char not in "0123456789abcdef" for char in text_hash)
                or case.get("text_sha256") != text_hash):
            continue

        unit_key = (event_id, unit_id)
        if unit_key in seen_units:
            raise ValueError("Duplicate sentiment event/unit identity")
        seen_units.add(unit_key)

        annotations = unit.get("sentiment_annotations")
        if not isinstance(annotations, list):
            slots = unit.get("slots")
            if isinstance(slots, list):
                annotations = [slot for slot in slots
                               if isinstance(slot, dict) and slot.get("label") is not None]
            elif unit.get("label") is not None:
                # Preserve a real single-annotator submission as one annotation.
                # Do not synthesize independence, blinding, or evidence metadata.
                fields = ("annotator_id", "label", "independent", "prediction_seen",
                          "evidence_refs", "prediction_exposure",
                          "saw_other_annotator_answer", "source_version", "submitted_at")
                annotations = [{key: unit[key] for key in fields if key in unit}]
            else:
                annotations = []
        labels.append({
            "evaluation_id": case.get("evaluation_id"),
            "event_id": event_id,
            "unit_id": unit_id,
            "source_record_id": case.get("target_record_id"),
            "text_sha256": text_hash,
            "sentiment_annotations": annotations,
        })
    return labels


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, default=Path.cwd())
    parser.add_argument("--seal", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--ai-reference", type=Path,
                        help="Normalized AI-only reference labels; exploratory comparison after sealing only.")
    args = parser.parse_args()
    print(json.dumps(run(args.root, args.seal, args.out, args.ai_reference), ensure_ascii=False))


if __name__ == "__main__":
    main()
