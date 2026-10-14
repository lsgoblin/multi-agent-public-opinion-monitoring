"""Freeze the 94-event sentiment result and the 5-event direction result into deliverables.

Inputs (read-only):
  artifacts/integration/20261013/data-rescore/benchmark-report-v1.json

Outputs (this directory):
  frozen-result-94-events-v1.json   machine-readable result with provenance and hashes
  per-unit-rows-v1.csv              one row per scored unit, for external audit

No model call, no network, no label or title text is copied: rows carry identifiers,
the label pair, the prediction and the truth only.
"""

from __future__ import annotations

import csv
import hashlib
import json
from collections import Counter
from pathlib import Path

ROOT = Path(r"D:\test\多智能体仿真舆情监测与投诉预警")
REPORT = ROOT / "artifacts/integration/20261013/data-rescore/benchmark-report-v1.json"
OUT = ROOT / "docs/delivery/event-eval-20261014"

report = json.loads(REPORT.read_text(encoding="utf-8"))
headline = report["headline"]
formal = report["formal_combined"]
direction = report["formal_combined_direction"]

units = headline["scored_units"]

# --------------------------------------------------------------------- per-unit CSV
csv_path = OUT / "per-unit-rows-v1.csv"
with csv_path.open("w", encoding="utf-8", newline="") as handle:
    writer = csv.writer(handle)
    writer.writerow(["unit_id", "event_id", "truth", "prediction", "correct",
                     "label_basis", "annotator_a", "annotator_b", "adjudicated_by"])
    for row in units:
        decision = row["label_decision"]
        annotators = list(decision.get("original_annotator_ids") or [])
        writer.writerow([
            row["unit_id"], row["event_id"], row["truth"], row["prediction"],
            "yes" if row["correct"] else "no",
            row["label_basis"],
            annotators[0] if len(annotators) > 0 else "",
            annotators[1] if len(annotators) > 1 else "",
            decision.get("adjudicated_by") or "",
        ])

# ------------------------------------------------------- admitted prediction provenance
admitted = []
for cohort in report["cohorts"]:
    pr = cohort.get("prediction_report") or {}
    for entry in pr.get("admitted") or []:
        admitted.append({k: entry.get(k) for k in
                         ("unit_id", "event_id", "label", "method_version",
                          "execution_mode", "labels_accessed", "outcomes_accessed",
                          "predicted_at", "frozen_at", "input_package_sha256")})
methods = Counter(entry["method_version"] for entry in admitted)

direction_metrics = direction["metrics"]["direction_agreement"]
three_class = direction["metrics"]["direction_three_class"]

frozen = {
    "schema": "riskshield.final_result.real_historical_events.v1",
    "frozen_at": "2026-10-14",
    "frozen_by": "main working copy, total-agent session",
    "source_report": {
        "path": "artifacts/integration/20261013/data-rescore/benchmark-report-v1.json",
        "sha256": hashlib.sha256(REPORT.read_bytes()).hexdigest(),
        "claim_level": report.get("claim_level"),
        "config_version": report.get("config_version"),
        "generated_at": report.get("generated_at"),
        "hash_lineage": {
            "note": "the same path was regenerated twice to correct the direction "
                    "scored_event_ids list; sentiment metrics and direction 2/5 are "
                    "identical across all three generations",
            "generation_1": "136ffe1da74f57ca8b2b9986f5076c1b782c10de3db6da1e17808ae8d22fce4f",
            "generation_2": "66d402314f0632bb58551d60109388fbff63353e0f12a074753215fb5db3f892",
            "generation_3_current": hashlib.sha256(REPORT.read_bytes()).hexdigest(),
            "generation_2_record": "artifacts/branch-work/data-evaluation/handoff-20261014-v11.md",
        },
    },
    "reproduction": {
        "script": "docs/delivery/event-eval-20261014/verify_94_event_score.py",
        "what_it_does": "recomputes every headline metric from the 94 per-unit rows and "
                        "compares against the recorded summary; checks annotator "
                        "distinctness and admitted prediction provenance",
        "result": "all checks pass",
    },
    "track_sentiment": {
        "task_type": "sentiment_three_class",
        "distinct_real_events": headline["sample_size"]["distinct_real_events"],
        "distinct_real_source_records": headline["sample_size"]["distinct_real_source_records"],
        "holdout_split_events": headline["sample_size"]["holdout_split_events"],
        "synthetic_data_in_denominator": False,
        "metrics": {
            "three_class_accuracy": {
                "value": headline["three_class"]["value"],
                "numerator": headline["three_class"]["numerator"],
                "denominator": headline["three_class"]["denominator"],
                "threshold": 0.88, "meets_threshold": False,
            },
            "negative_precision": {
                "value": headline["negative_precision"]["value"],
                "numerator": headline["negative_precision"]["numerator"],
                "denominator": headline["negative_precision"]["denominator"],
                "threshold": 0.90, "meets_threshold": False,
                "note": "the brief's formal formula for negative accuracy is unresolved; "
                        "precision and binary accuracy are both reported",
            },
            "negative_recall": {
                "value": headline["negative_recall"]["value"],
                "numerator": headline["negative_recall"]["numerator"],
                "denominator": headline["negative_recall"]["denominator"],
                "threshold": 0.85, "meets_threshold": True,
            },
            "negative_binary_accuracy": {
                "value": headline["binary_accuracy"]["value"],
                "numerator": headline["binary_accuracy"]["numerator"],
                "denominator": headline["binary_accuracy"]["denominator"],
            },
        },
        "confusion_matrix_actual_by_predicted":
            headline["confusion_matrix_actual_by_predicted"],
        "class_support": formal["metrics"]["sentiment_three_class"]["class_support"],
        "negative_counts": headline["negative_counts"],
        "label_side": {
            "annotator_pairs": {str(list(k)): v for k, v in Counter(
                tuple(row["label_decision"]["original_annotator_ids"]) for row in units
            ).items()},
            "raw_agreement_units": formal["metrics"]["label_basis_accounting"]["raw_agreement"]["units"],
            "adjudicated_units": formal["metrics"]["label_basis_accounting"]["adjudicated"]["units"],
            "unresolved_disagreement_units":
                formal["metrics"]["label_basis_accounting"]["excluded_label_disagreement_unresolved"]["units"],
            "adjudicators": {str(k): v for k, v in Counter(
                row["label_decision"].get("adjudicated_by") for row in units
                if row["label_decision"].get("adjudicated_by")).items()},
        },
    },
    "track_direction": {
        "task_type": "event_direction",
        "distinct_real_events": direction["metrics"]["sample_size"]["distinct_real_events"],
        "metrics": {
            "direction_agreement_binary": {
                "value": direction_metrics["value"],
                "numerator": direction_metrics["numerator"],
                "denominator": direction_metrics["denominator"],
                "threshold": 0.70, "meets_threshold": False,
                "scored_event_ids": direction.get("binary_direction_event_ids"),
                "excluded_events": [
                    {"event_id": item.get("event_id"), "reasons": item.get("reasons")}
                    for item in direction_metrics.get("excluded_events_or_rows") or []],
            },
            "direction_three_class_diagnostic": {
                "value": three_class["value"],
                "numerator": three_class["numerator"],
                "denominator": three_class["denominator"],
                "note": "an additional reading that can also express stable; it does not "
                        "replace the binary agreement rate",
                "value_by_split": {k: v["value"] for k, v in
                                   (three_class.get("by_split") or {}).items()},
            },
        },
        "confusion_matrix_actual_by_predicted":
            direction_metrics["confusion_matrix_actual_by_predicted"],
        "class_support": three_class["class_support"],
        "label_version": direction.get("label_version"),
    },
    "prediction_provenance": {
        "admitted_rows": len(admitted),
        "execution_modes": {str(k): v for k, v in Counter(
            entry["execution_mode"] for entry in admitted).items()},
        "method_versions": dict(methods),
        "labels_accessed_true": sum(1 for e in admitted if e["labels_accessed"]),
        "outcomes_accessed_true": sum(1 for e in admitted if e["outcomes_accessed"]),
        "engine_engineering_modes_present": [],
    },
    "limitations": [
        "The direction track carries 2/5 = 40% over the events the binary reading can "
        "express; four of the nine direction candidates have no resolved observed "
        "direction label and are excluded, not counted as zero.",
        "Class support for direction is calming 5, escalation 0, stable 4: no event in "
        "the frozen set escalated, so escalation recall is undefined rather than 0%.",
        "The sentiment denominator is 94 distinct real events, which is above the "
        "brief's 10-20 event range; the brief's exact minimum is unresolved.",
        "Labels come from two independent annotation passes per unit; the adjudications "
        "were made by the project owner and are recorded as such. These passes are not "
        "described as human annotators anywhere in this result.",
        "Three-class accuracy (84.04%) and negative precision (80.49%) are below the "
        "brief's 88% and 90%; negative recall (89.19%) is above 85%.",
        "The five platforms, collection latency, 500-agent scale on real history, "
        "60-minute report SLA and 30-minute red-alert delivery are separate items and "
        "are not evidenced by this result.",
    ],
}
(OUT / "frozen-result-94-events-v1.json").write_text(
    json.dumps(frozen, ensure_ascii=False, indent=2, sort_keys=False) + "\n",
    encoding="utf-8", newline="\n")

print("wrote %s" % (OUT / "frozen-result-94-events-v1.json"))
print("wrote %s (%d rows)" % (csv_path, len(units)))
print()
print("sentiment : %d/%d = %.6f | neg recall %d/%d | neg precision %d/%d" % (
    headline["three_class"]["numerator"], headline["three_class"]["denominator"],
    headline["three_class"]["value"],
    headline["negative_recall"]["numerator"], headline["negative_recall"]["denominator"],
    headline["negative_precision"]["numerator"], headline["negative_precision"]["denominator"]))
print("direction : %d/%d = %.4f (binary) | 3-class %d/%d" % (
    direction_metrics["numerator"], direction_metrics["denominator"],
    direction_metrics["value"], three_class["numerator"], three_class["denominator"]))
