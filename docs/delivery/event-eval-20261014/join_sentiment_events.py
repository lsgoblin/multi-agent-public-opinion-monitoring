"""Sentiment track on the event-identity subset, from frozen predictions and labels.

Every number here is a join of two frozen documents; nothing is re-predicted and no
label is re-read:

  predictions  artifacts/branch-work/data-evaluation/evidence/20261011/v04/formal-run/
               predictions-formal-v1.json           (5 units, sent-* ids)
               artifacts/branch-work/data-evaluation/evidence/20261011/v03/prediction-run/
               predictions-v1.json                  (14 units, of which 2 are named events)
  labels       data/evaluation/final-benchmark/labels/human-sentiment-submissions-v16.json
               and -v18.json
"""

from __future__ import annotations

import json
from collections import Counter
from pathlib import Path

ROOT = Path(r"D:\test\多智能体仿真舆情监测与投诉预警")
V04 = ROOT / "artifacts/branch-work/data-evaluation/evidence/20261011/v04/formal-run/predictions-formal-v1.json"
V03 = ROOT / "artifacts/branch-work/data-evaluation/evidence/20261011/v03/prediction-run/predictions-v1.json"
LABELS = [ROOT / "data/evaluation/final-benchmark/labels/human-sentiment-submissions-v16.json",
          ROOT / "data/evaluation/final-benchmark/labels/human-sentiment-submissions-v18.json"]
# The two v19 named events carry their two agreeing passes in the v03 annotation round:
# allianz  Leo=negative / Dick=negative;  unitedhealth  Leo=negative / nike=negative.
# A third pass (leos, neutral) exists for both but is a later review round, so the
# pair recorded in the freeze is the one used here.
V19_PASSES = {
    "allianz_life_data_breach_20250727": ["Leo", "Dick"],
    "unitedhealth_algorithm_care_lawsuit_20231115": ["Leo", "nike"],
}
V19_FILES = {
    "Leo": ROOT / "artifacts/branch-work/data-evaluation/evidence/20261011/v03/submission-a-pass-v1.json",
    "Dick": ROOT / "artifacts/branch-work/data-evaluation/evidence/20261011/v03/submission-b-pass-v1.json",
    "nike": ROOT / "artifacts/branch-work/data-evaluation/evidence/20261011/v03/submission-unitedhealth-second-v1.json",
}
LABEL_SPACE = ("negative", "neutral", "positive")

# Late run: three events that carried a direction result but had no archived title, so
# they were predicted in a later real model run and annotated by two fresh passes.
HERE = Path(__file__).resolve().parent
WORK = HERE / "sentiment-late-run"
LATE_PREDICTIONS = WORK / "predictions-late-three-v1.json"
LATE_ANNOTATIONS = HERE / "sentiment-late-annotation-three-v1.json"
ADJUDICATION = HERE / "sentiment-adjudication-unitedhealth-ceo-v1.json"

# A recorded adjudication with a named decider overrides a disagreement; without it the
# event would stay out of the denominator.
adjudicated = {}
if ADJUDICATION.exists():
    adj = json.loads(ADJUDICATION.read_text(encoding="utf-8"))
    for record in adj["records"]:
        adjudicated[record["event_id"]] = {
            "label": record["resolved_label"], "decided_by": adj["decided_by"],
            "originals": record["original_annotation_labels"]}

predictions = {}
for path in (V04, V03):
    doc = json.loads(path.read_text(encoding="utf-8"))
    for unit_id, row in doc["predictions"].items():
        predictions[unit_id] = {"event_id": row["event_id"], "label": row["label"],
                                "mode": row["execution_mode"],
                                "method": row["method_version"], "source": path.name}

late_units = set()
if LATE_PREDICTIONS.exists():
    late_doc = json.loads(LATE_PREDICTIONS.read_text(encoding="utf-8"))
    for unit_id, row in late_doc["predictions"].items():
        predictions[unit_id] = {"event_id": row["event_id"], "label": row["label"],
                                "mode": row["execution_mode"],
                                "method": row["method_version"],
                                "source": LATE_PREDICTIONS.name}
        late_units.add(unit_id)

labels = {}
for path in LABELS:
    doc = json.loads(path.read_text(encoding="utf-8"))
    for row in doc.get("units") or doc.get("records") or []:
        unit_id = row.get("unit_id")
        if not unit_id:
            continue
        labels.setdefault(unit_id, {"passes": {}, "frozen_text": row.get("frozen_text")})
        labels[unit_id]["passes"][doc.get("annotator_id") or path.stem] = \
            row.get("label") or row.get("sentiment_label") or row.get("answer")

# attach the two v19 events' agreeing pairs under the event id, which is also the
# unit id used by their prediction rows
for event_id, annotators in V19_PASSES.items():
    passes = {}
    for annotator in annotators:
        doc = json.loads(V19_FILES[annotator].read_text(encoding="utf-8"))
        for row in doc.get("units") or doc.get("records") or []:
            if row.get("unit_id") == event_id:
                passes[annotator] = row.get("label") or row.get("sentiment_label")
    labels.setdefault(event_id, {"passes": {}, "frozen_text": None})["passes"].update(passes)

# the late three: only the ones whose two fresh passes agreed carry a truth here
late_excluded = []
if LATE_ANNOTATIONS.exists():
    late_doc = json.loads(LATE_ANNOTATIONS.read_text(encoding="utf-8"))
    for record in late_doc["records"]:
        labels.setdefault(record["event_id"], {"passes": {}, "frozen_text": None})
        for entry in record["passes"]:
            labels[record["event_id"]]["passes"][entry["annotator_id"]] = entry["label"]
        if not record["labels_agree"] and record["event_id"] not in adjudicated:
            late_excluded.append(record["event_id"])

print("prediction modes : %s" % dict(Counter(v["mode"] for v in predictions.values())))
print("methods          : %s" % dict(Counter(v["method"] for v in predictions.values())))
print()
print("%-22s %-46s %-9s %s" % ("unit_id", "event_id", "predicted", "label passes"))
print("-" * 110)
rows = []
for unit_id, pred in sorted(predictions.items()):
    # the two v19 events are keyed by event id in the label map; everything else by unit id
    entry = labels.get(unit_id) or labels.get(pred["event_id"])
    if not entry:
        continue
    passes = [v for v in entry["passes"].values() if v in LABEL_SPACE]
    if not passes:
        continue
    decision = adjudicated.get(pred["event_id"])
    if decision is not None:
        truth, basis = decision["label"], "adjudicated_disagreement"
    elif len(set(passes)) == 1:
        truth, basis = passes[0], "unanimous_independent_labels"
    else:
        truth, basis = None, "unresolved_disagreement"
    rows.append({"unit_id": unit_id, "event_id": pred["event_id"],
                 "predicted": pred["label"], "passes": passes, "truth": truth,
                 "basis": basis, "late": unit_id in late_units})
    print("%-22s %-46s %-9s %-28s %s%s" % (unit_id, pred["event_id"], pred["label"],
                                           basis, passes,
                                           "  [late]" if unit_id in late_units else ""))

resolved = [r for r in rows if r["truth"]]
print()
print("units with a prediction and a settled label   : %d" % len(resolved))
print("   settled by unanimous passes                : %d"
      % sum(1 for r in resolved if r["basis"] == "unanimous_independent_labels"))
print("   settled by a recorded adjudication         : %d"
      % sum(1 for r in resolved if r["basis"] == "adjudicated_disagreement"))
print("   of which from the late run                 : %d"
      % sum(1 for r in resolved if r["late"]))
print("excluded for an unresolved label disagreement : %d %s"
      % (len(late_excluded), late_excluded))
print("observed label distribution : %s" % dict(Counter(r["truth"] for r in resolved)))
print("predicted distribution      : %s" % dict(Counter(r["predicted"] for r in resolved)))
correct = sum(1 for r in resolved if r["truth"] == r["predicted"])
print("three-class accuracy        : %d/%d = %.4f"
      % (correct, len(resolved), correct / len(resolved)))
tp = sum(1 for r in resolved if r["truth"] == "negative" and r["predicted"] == "negative")
fp = sum(1 for r in resolved if r["truth"] != "negative" and r["predicted"] == "negative")
fn = sum(1 for r in resolved if r["truth"] == "negative" and r["predicted"] != "negative")
tn = len(resolved) - tp - fp - fn
print("negative TP/FP/FN/TN        : %d/%d/%d/%d" % (tp, fp, fn, tn))
print("negative recall             : %d/%d" % (tp, tp + fn))
print("negative precision          : %d/%d" % (tp, tp + fp))
