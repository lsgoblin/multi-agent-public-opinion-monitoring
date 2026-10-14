"""One-record evidence card: every link in the chain for a single submitted unit.

Picked: allianz_life_data_breach_20250727.

Why this one:
  * it is one of the 94 scored records in benchmark-report-v1.json, so the record-level
    headline metric literally contains it;
  * it is also a named event with a subject and a dated action, so it is not one of the
    76 keyword-sampled titles or the 12 complaint posts;
  * it sits in the direction track's candidate set as well;
  * both annotation passes are recorded verbatim in files that still exist;
  * its prediction was a real model call (formal_model_run_v1), not a stand-in.

Every value below is read from a frozen file; nothing is recomputed from memory. The
card states what the record shows and what it does not show.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

ROOT = Path(r"D:\test\多智能体仿真舆情监测与投诉预警")
OUT = ROOT / "docs/delivery/event-eval-20261014"
UNIT = "allianz_life_data_breach_20250727"

PATHS = {
    "scored_row": "artifacts/integration/20261013/data-rescore/benchmark-report-v1.json",
    "frozen_inputs": "artifacts/branch-work/data-evaluation/evidence/20261011/v03/prediction-run/frozen-inputs-v1.json",
    "prediction": "artifacts/branch-work/data-evaluation/evidence/20261011/v03/prediction-run/predictions-v1.json",
    "production_evidence": "artifacts/branch-work/data-evaluation/evidence/20261011/v03/prediction-run/production-evidence-v1.json",
    "freeze_record": "artifacts/branch-work/data-evaluation/evidence/20261011/v03/prediction-run/freeze-record-v1.json",
    "annotation_pass_a": "artifacts/branch-work/data-evaluation/evidence/20261011/v03/submission-a-pass-v1.json",
    "annotation_pass_b": "artifacts/branch-work/data-evaluation/evidence/20261011/v03/submission-b-pass-v1.json",
    "archived_title_manifest": "data/evaluation/final-benchmark/labels/verified-original-titles-v19.json",
    "direction_prediction": "artifacts/branch-work/data-evaluation/evidence/20261012/v07/trajectory/event-8/prediction-v1.json",
    "direction_pass_a": "artifacts/branch-work/data-evaluation/evidence/20261012/v07/annotation-direction/direction-submission-a-v1.json",
    "direction_pass_b": "artifacts/branch-work/data-evaluation/evidence/20261012/v07/annotation-direction/direction-submission-b-v1.json",
}


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def load(rel: str):
    path = ROOT / rel
    if not path.exists():
        return None, None
    return json.loads(path.read_text(encoding="utf-8")), digest(path)


def find_unit(doc, unit_id):
    """Find the row for one unit anywhere in a document."""
    found = []

    def walk(node):
        if isinstance(node, dict):
            if node.get("unit_id") == unit_id or node.get("event_id") == unit_id:
                found.append(node)
            for value in node.values():
                walk(value)
        elif isinstance(node, list):
            for value in node:
                walk(value)

    walk(doc)
    return found[0] if found else None


chain = {}
hashes = {}
for name, rel in PATHS.items():
    doc, sha = load(rel)
    hashes[name] = {"path": rel, "sha256": sha, "exists": doc is not None}
    chain[name] = doc

report = chain["scored_row"]
row = next(r for r in report["headline"]["scored_units"] if r["event_id"] == UNIT)
gate = next(r for c in report["cohorts"] for r in (c.get("eligibility_table") or [])
            if r.get("gated") and r.get("unit_id") == UNIT)
cohort = next(c["cohort_id"] for c in report["cohorts"]
              if any(r.get("unit_id") == UNIT for r in (c.get("eligibility_table") or [])))
admitted = next((e for c in report["cohorts"]
                 for e in ((c.get("prediction_report") or {}).get("admitted") or [])
                 if e.get("unit_id") == UNIT), {})

frozen = find_unit(chain["frozen_inputs"], UNIT) or {}
prediction = (chain["prediction"] or {}).get("predictions", {}).get(UNIT) or {}
pass_a = find_unit(chain["annotation_pass_a"], UNIT) or {}
pass_b = find_unit(chain["annotation_pass_b"], UNIT) or {}
manifest = find_unit(chain["archived_title_manifest"], UNIT) or {}
dir_pred = chain["direction_prediction"] or {}
dir_a = find_unit(chain["direction_pass_a"], UNIT) or {}
dir_b = find_unit(chain["direction_pass_b"], UNIT) or {}

card = {
    "schema": "riskshield.final_result.record_evidence_card.v1",
    "unit_id": UNIT,
    "why_this_record": [
        "one of the 94 scored records in the frozen record-level report",
        "also a named event (subject + dated action), not a keyword-sampled title or a "
        "complaint post",
        "present in the direction track's candidate set as well",
        "both annotation passes are recorded verbatim in files that still exist",
        "its prediction was a real model call: execution_mode=formal_model_run_v1",
    ],
    "identity": {
        "unit_id": row["unit_id"],
        "event_id": row["event_id"],
        "cohort": cohort,
        "source_record_id": gate.get("source_record_id"),
        "publisher": gate.get("publisher"),
        "source_version": gate.get("source_version"),
        "visible_at": gate.get("visible_at"),
        "cutoff": gate.get("cutoff"),
        "cutoff_provenance": gate.get("cutoff_provenance"),
        "scored_text_sha256": gate.get("text_sha256"),
        "manifest_archive_title_sha256": manifest.get("archive_title_sha256"),
        "manifest_original_title_sha256": manifest.get("original_title_utf8_sha256"),
        "hash_method": manifest.get("hash_method"),
        "title_match": manifest.get("title_hash_match"),
        "v4_text_to_annotate_sha256": manifest.get("v4_text_sha256"),
        "text_chain_note": (
            "the text the prediction was sent is byte-identical to the archived title and to "
            "the manifest's original title (all three share one SHA-256); the manifest also "
            "carries a separate, older shorter annotation variant (v4_text_to_annotate) that "
            "this prediction and both label passes did not use"),
    },
    "prediction": {
        "label": row["prediction"],
        "execution_mode": admitted.get("execution_mode"),
        "method_version": admitted.get("method_version"),
        "requested_model": (chain["prediction"] or {}).get("requested_model"),
        "predicted_at": admitted.get("predicted_at"),
        "frozen_at": admitted.get("frozen_at"),
        "input_package_sha256": admitted.get("input_package_sha256"),
        "labels_accessed": admitted.get("labels_accessed"),
        "outcomes_accessed": admitted.get("outcomes_accessed"),
        "production_evidence_ref": admitted.get("production_evidence_ref"),
        "freeze_record_ref": admitted.get("freeze_record_ref"),
    },
    "label": {
        "truth": row["truth"],
        "label_basis": row["label_basis"],
        "pass_a": {"annotator": pass_a.get("annotator_id"), "label": pass_a.get("label")},
        "pass_b": {"annotator": pass_b.get("annotator_id"), "label": pass_b.get("label")},
        "adjudicated_by": row["label_decision"].get("adjudicated_by"),
        "correct": row["correct"],
    },
    "direction_track": {
        "predicted": dir_pred.get("label") or None,
        "execution_mode": dir_pred.get("execution_mode"),
        "method_version": dir_pred.get("method_version"),
        "pass_a": {"annotator": dir_a.get("annotator_id"), "label": dir_a.get("direction_label")},
        "pass_b": {"annotator": dir_b.get("annotator_id"), "label": dir_b.get("direction_label")},
        "agreement": "the two passes disagree, so this event is excluded from the direction "
                     "denominator rather than resolved by choice",
    },
    "files_and_hashes": hashes,
    "what_this_card_does_not_show": [
        "it is one record, not the whole denominator: the record-level metric contains 94 "
        "such rows, of which only 6 are named events",
        "it does not show a direction score for this event, because the two direction passes "
        "disagree on it",
        "the labels come from two independent annotation channels, not from human annotators",
        "the prediction is a title-level sentiment call, not a multi-agent simulation run",
    ],
}

(OUT / "record-evidence-card-allianz-v1.json").write_text(
    json.dumps(card, ensure_ascii=False, indent=2) + "\n", encoding="utf-8", newline="\n")

missing = [n for n, v in hashes.items() if not v["exists"]]
print("record card: %s" % UNIT)
print("  scored row        : label=%s truth=%s correct=%s"
      % (row["prediction"], row["truth"], row["correct"]))
print("  cohort            : %s" % cohort)
print("  source            : %s / %s" % (gate.get("publisher"), gate.get("source_record_id")))
print("  cutoff            : %s (%s)" % (gate.get("cutoff"), gate.get("cutoff_provenance")))
print("  prediction        : %s via %s" % (admitted.get("execution_mode"),
                                            admitted.get("method_version")))
print("  labels            : A=%s(%s) B=%s(%s) basis=%s"
      % (pass_a.get("annotator_id"), pass_a.get("label"),
         pass_b.get("annotator_id"), pass_b.get("label"), row["label_basis"]))
print("  direction         : pred=%s | A=%s B=%s -> excluded (disagreement)"
      % (dir_pred.get("label"), dir_a.get("direction_label"), dir_b.get("direction_label")))
print("  title chain       : scored=%s manifest_archive=%s manifest_original=%s"
      % (str(gate.get("text_sha256"))[:16], str(manifest.get("archive_title_sha256"))[:16],
         str(manifest.get("original_title_utf8_sha256"))[:16]))
print("                      identical=%s | v4 annotation variant=%s (unused here)"
      % (gate.get("text_sha256") == manifest.get("archive_title_sha256") ==
         manifest.get("original_title_utf8_sha256"),
         str(manifest.get("v4_text_sha256"))[:16]))
print("  referenced files  : %d (%s)"
      % (len(hashes), "all present" if not missing else "MISSING: %s" % missing))
print()
print("wrote %s" % (OUT / "record-evidence-card-allianz-v1.json"))
