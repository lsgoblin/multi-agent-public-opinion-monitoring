"""Join the ten events' direction predictions with their two annotation passes.

Reads only frozen evidence:
  trajectory/event-*/prediction-v1.json        (formal_model_run_v1 predictions)
  annotation-direction/direction-submission-{a,b}-v1.json   (two independent passes)

Composition is reported as it is: the three-class reading includes `stable`, the
binary reading the brief defines can only express escalation/calming, and
disagreements are shown rather than resolved by choice.
"""

from __future__ import annotations

import json
from collections import Counter
from pathlib import Path

ROOT = Path(r"D:\test\多智能体仿真舆情监测与投诉预警")
TRAJ = ROOT / "artifacts/branch-work/data-evaluation/evidence/20261012/v07/trajectory"
ANN = ROOT / "artifacts/branch-work/data-evaluation/evidence/20261012/v07/annotation-direction"

predictions = {}
for folder in sorted(TRAJ.glob("event-*"), key=lambda p: int(p.name.split("-")[1])):
    doc = json.loads((folder / "prediction-v1.json").read_text(encoding="utf-8"))

    def hunt(node, key):
        if isinstance(node, dict):
            if isinstance(node.get(key), str):
                return node[key]
            for value in node.values():
                found = hunt(value, key)
                if found:
                    return found
        elif isinstance(node, list):
            for value in node:
                found = hunt(value, key)
                if found:
                    return found
        return None

    event_id = hunt(doc, "event_id")
    predictions[event_id] = {
        "label": hunt(doc, "label"),
        "mode": hunt(doc, "execution_mode"),
        "method": hunt(doc, "method_version"),
        "folder": folder.name,
    }


def labels(path):
    doc = json.loads(path.read_text(encoding="utf-8"))
    out = {}
    # submission A carries "units", submission B carries "records"; both are lists
    # of per-event rows, and both declare their annotator and blinding at the top.
    rows = doc.get("units") or doc.get("records") or []
    for row in rows:
        out[row["event_id"]] = {"label": row.get("direction_label"),
                                "annotator": row.get("annotator_id"),
                                "confidence": row.get("confidence"),
                                "exposure": row.get("prediction_exposure")}
    return doc.get("annotator_id"), out


who_a, pass_a = labels(ANN / "direction-submission-a-v1.json")
who_b, pass_b = labels(ANN / "direction-submission-b-v1.json")

# A late two-pass annotation closed the one event whose label the corpus itself recorded
# as "not collected". It is kept in its own file so the late collection stays visible.
LATE = ROOT / "docs/delivery/event-eval-20261014/direction-late-annotation-unh-change-v1.json"
late_events = []
if LATE.exists():
    late = json.loads(LATE.read_text(encoding="utf-8"))
    agreement = late["agreement"]
    if agreement["agreed"]:
        event_id = late["event_id"]
        for entry in late["passes"]:
            pass_a.setdefault(event_id, {"label": None})
            pass_b.setdefault(event_id, {"label": None})
        # one agreeing value is enough; record both channels for the audit trail
        pass_a[event_id]["label"] = late["passes"][0]["direction_label"]
        pass_b[event_id]["label"] = late["passes"][1]["direction_label"]
        late_events.append(event_id)

print("direction passes: A=%s  B=%s" % (who_a, who_b))
print("prediction modes : %s" % dict(Counter(v["mode"] for v in predictions.values())))
print("methods          : %s" % dict(Counter(v["method"] for v in predictions.values())))
print()
print("%-52s %-11s %-9s %-9s %-8s" % ("event_id", "predicted", "label A", "label B", "agree"))
print("-" * 96)
rows = []
for event_id in sorted(predictions):
    a = pass_a.get(event_id, {}).get("label")
    b = pass_b.get(event_id, {}).get("label")
    pred = predictions[event_id]["label"]
    agree = "yes" if a == b else "NO"
    rows.append({"event_id": event_id, "predicted": pred, "a": a, "b": b,
                 "truth": a if a == b else None})
    print("%-52s %-11s %-9s %-9s %-8s" % (event_id, pred, a, b, agree))

resolved = [r for r in rows if r["truth"]]
disagree = [r for r in rows if not r["truth"]]
print()
print("events with two passes     : %d" % len(rows))
print("passes agree (resolved)    : %d" % len(resolved))
print("passes disagree            : %d %s" % (len(disagree), [r["event_id"] for r in disagree]))
print("late-collected annotation  : %s" % (late_events or "none"))
print("observed label distribution: %s" % dict(Counter(r["truth"] for r in resolved)))
print("predicted distribution     : %s" % dict(Counter(r["predicted"] for r in rows)))
print()

# three-class reading over the resolved events
three = Counter()
for r in resolved:
    three[(r["truth"], r["predicted"])] += 1
correct3 = sum(n for (t, p), n in three.items() if t == p)
print("three-class over resolved events : %d/%d = %.4f"
      % (correct3, len(resolved), correct3 / len(resolved)))
for t in ("escalation", "calming", "stable"):
    print("   actual %-11s -> %s" % (t, {p: three[(t, p)] for p in
                                       ("escalation", "calming", "stable")}))

# binary reading, only where both truth and prediction are in the binary space
binary = [r for r in resolved if r["truth"] in ("escalation", "calming")
          and r["predicted"] in ("escalation", "calming")]
correct2 = sum(1 for r in binary if r["truth"] == r["predicted"])
print()
print("binary reading (escalation/calming only) : %d/%d = %s"
      % (correct2, len(binary), round(correct2 / len(binary), 4) if binary else "undefined"))
print("   events in the binary reading: %s" % [r["event_id"] for r in binary])
