"""Independently recompute every headline metric from the per-unit rows.

The report carries both a summary block and 94 per-unit rows. This script ignores the
summary block's arithmetic and derives the numbers from the rows alone, then compares.
That is the difference between "the report says 79/94" and "79 of the 94 recorded rows
are correct".

Run:  python verify_94_event_score.py
"""

from __future__ import annotations

import hashlib
import json
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(r"D:\test\多智能体仿真舆情监测与投诉预警")
REPORT = ROOT / "artifacts/integration/20261013/data-rescore/benchmark-report-v1.json"

LABELS = ("negative", "neutral", "positive")
failures: list[str] = []


def check(name, recomputed, recorded):
    ok = recomputed == recorded
    if not ok:
        failures.append("%s: recomputed=%r recorded=%r" % (name, recomputed, recorded))
    print("   %-38s recomputed=%-16s recorded=%-16s %s"
          % (name, recomputed, recorded, "OK" if ok else "MISMATCH"))


report = json.loads(REPORT.read_text(encoding="utf-8"))
headline = report["headline"]
formal = report["formal_combined"]
units = headline["scored_units"]

print("source report : %s" % REPORT)
print("sha256        : %s" % hashlib.sha256(REPORT.read_bytes()).hexdigest())
print("claim_level   : %s" % report.get("claim_level"))
print("config_version: %s" % report.get("config_version"))
print("scored rows   : %d" % len(units))
print()

# ------------------------------------------------------------------ identity checks
unit_ids = [row["unit_id"] for row in units]
event_ids = [row["event_id"] for row in units]
print("identity:")
print("   distinct unit_id           : %d" % len(set(unit_ids)))
print("   distinct event_id          : %d" % len(set(event_ids)))
for row in units:
    if "label_decision" not in row:
        failures.append("row without label_decision: %s" % row.get("unit_id"))
    if row.get("truth") not in LABELS:
        failures.append("row truth outside the three classes: %s" % row.get("unit_id"))
    if row.get("prediction") not in LABELS:
        failures.append("row prediction outside the three classes: %s" % row.get("unit_id"))
print()

# ------------------------------------------------------ recompute from rows only
confusion = {actual: {predicted: 0 for predicted in LABELS} for actual in LABELS}
correct = 0
for row in units:
    actual, predicted = row["truth"], row["prediction"]
    confusion[actual][predicted] += 1
    if actual == predicted:
        correct += 1

tp = confusion["negative"]["negative"]
fp = confusion["neutral"]["negative"] + confusion["positive"]["negative"]
fn = confusion["negative"]["neutral"] + confusion["negative"]["positive"]
tn = len(units) - tp - fp - fn

values = {
    "sample_size": len(units),
    "three_class_correct": correct,
    "confusion_matrix": confusion,
    "negative_tp": tp, "negative_fp": fp, "negative_fn": fn, "negative_tn": tn,
    "negative_precision": round(tp / (tp + fp), 6),
    "negative_recall": round(tp / (tp + fn), 6),
    "binary_accuracy": round((tp + tn) / (tp + fp + fn + tn), 6),
    "three_class_accuracy": round(correct / len(units), 6),
}

recorded = {
    "sample_size": headline["sample_size"]["distinct_real_events"],
    "three_class_correct": headline["three_class"]["numerator"],
    "confusion_matrix": headline["confusion_matrix_actual_by_predicted"],
    "negative_tp": headline["negative_counts"]["true_positive"],
    "negative_fp": headline["negative_counts"]["false_positive"],
    "negative_fn": headline["negative_counts"]["false_negative"],
    "negative_tn": headline["negative_counts"]["true_negative"],
    "negative_precision": headline["negative_precision"]["value"],
    "negative_recall": headline["negative_recall"]["value"],
    "binary_accuracy": headline["binary_accuracy"]["value"],
    "three_class_accuracy": headline["three_class"]["value"],
}

print("recomputed vs recorded:")
for key in values:
    check(key, values[key], recorded[key])
print()

# ------------------------------------------------------------- label-side integrity
pairs = Counter(tuple(row["label_decision"]["original_annotator_ids"]) for row in units)
basis = Counter(row["label_basis"] for row in units)
adjudicators = Counter(row["label_decision"].get("adjudicated_by") for row in units
                       if row["label_decision"].get("adjudicated_by"))
print("label side:")
print("   annotator pairs            : %s" % dict(pairs))
print("   label_basis                : %s" % dict(basis))
print("   adjudicators               : %s" % dict(adjudicators))
for pair in pairs:
    if len(set(pair)) != 2:
        failures.append("annotator pair not distinct: %r" % (pair,))
if set(basis) - {"unanimous_independent_labels", "adjudicated_disagreement"}:
    failures.append("unexpected label_basis: %r" % (set(basis),))
recorded_basis = formal["metrics"]["label_basis_accounting"]
check("raw_agreement_units", basis["unanimous_independent_labels"],
      recorded_basis["raw_agreement"]["units"])
check("adjudicated_units", basis["adjudicated_disagreement"],
      recorded_basis["adjudicated"]["units"])
print()

# --------------------------------------------------------------- provenance checks
admitted = []
for cohort in report["cohorts"]:
    pr = cohort.get("prediction_report") or {}
    admitted.extend(pr.get("admitted") or [])
modes = Counter(entry.get("execution_mode") for entry in admitted)
methods = Counter(entry.get("method_version") for entry in admitted)
labels_accessed = Counter(entry.get("labels_accessed") for entry in admitted)
outcomes_accessed = Counter(entry.get("outcomes_accessed") for entry in admitted)
print("prediction provenance (%d admitted rows):" % len(admitted))
print("   execution_mode             : %s" % dict(modes))
print("   method_version             : %s" % dict(methods))
print("   labels_accessed            : %s" % dict(labels_accessed))
print("   outcomes_accessed          : %s" % dict(outcomes_accessed))
if set(modes) != {"formal_model_run_v1"}:
    failures.append("unexpected execution modes: %r" % (set(modes),))
if labels_accessed.get(True):
    failures.append("a prediction row reports labels_accessed=True")
if outcomes_accessed.get(True):
    failures.append("a prediction row reports outcomes_accessed=True")
print()

# ------------------------------------------------------------- direction-track note
direction = formal["metrics"]["direction_agreement"]
print("direction track (reported separately, must not be merged):")
print("   denominator                : %s" % direction.get("denominator"))
print("   numerator                  : %s" % direction.get("numerator"))
print("   evaluated events           : %s"
      % formal["metrics"]["sample_size"].get("direction_evaluable_events"))
print()

print("=" * 78)
if failures:
    print("VERDICT: %d check(s) failed" % len(failures))
    for item in failures:
        print("   - %s" % item)
    sys.exit(1)
print("VERDICT: all checks pass -- the headline numbers follow from the 94 rows,")
print("         every row has two distinct annotator slots, and every admitted")
print("         prediction is a real formal_model_run_v1 with no label or outcome access.")
