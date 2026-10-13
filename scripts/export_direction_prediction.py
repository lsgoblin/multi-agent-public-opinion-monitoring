"""Export one simulation run as an evaluation-inputs document plus its freeze record.

Offline and deterministic: it reads the run ledger and the case snapshot, applies the frozen
direction method, and writes the documents. It never calls a model, never reads a label, and
never modifies the evaluator, which belongs to the data-evaluation branch.

Run it from the repository root, because the evaluator resolves evidence references against
the current working directory:

    python scripts/export_direction_prediction.py --db runtime/day4-tasks.db \\
        --run-id run_xxx --outdir artifacts/branch-work/simulation-scale/evidence/<date>/direction

Then the existing evaluator can score the document with synthetic labels:

    python -m riskshield.evaluation --inputs <inputs.json> --future-labels <labels.json> \\
        --out <report.json>
"""
import argparse
import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from riskshield.direction_prediction import (  # noqa: E402
    PREDICTION_SOURCE, DirectionPredictionError, load_method,
    preflight_event_level_uniqueness, preflight_import_references, write_documents)
from riskshield.store import Store  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", required=True, help="store that holds the run")
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--outdir", required=True,
                        help="directory for the prediction documents; an existing "
                             "prediction document there is never overwritten")
    parser.add_argument("--material-root", required=True,
                        help="absolute directory every reference is resolved against; it "
                             "must contain the output directory")
    parser.add_argument("--source-label", default=PREDICTION_SOURCE,
                        help="label of this prediction source, used when checking that an "
                             "event carries exactly one prediction")
    parser.add_argument("--unit-kind", default="original_title",
                        choices=["original_title", "original_post"],
                        help="what the predicted unit is; a title uses the record's title, "
                             "a post uses its summary")
    parser.add_argument("--method", default=None,
                        help="method document (default: the frozen one shipped with the code)")
    parser.add_argument("--dataset-version", default=None)
    args = parser.parse_args()

    db_path = Path(args.db).expanduser().resolve()
    if not db_path.is_file():
        raise SystemExit(f"store not found: {db_path}")
    method_path = Path(args.method).expanduser() if args.method else None
    try:
        method, method_sha = load_method(method_path)
        written = write_documents(Store(db_path), args.run_id, Path(args.outdir),
                                  material_root=Path(args.material_root),
                                  source_label=args.source_label,
                                  unit_kind=args.unit_kind,
                                  text_source=("title" if args.unit_kind == "original_title"
                                               else "summary"),
                                  method_path=method_path,
                                  dataset_version=args.dataset_version)
    except DirectionPredictionError as exc:
        raise SystemExit(f"cannot export a prediction: {exc}") from None

    summary = written["summary"]
    preflight = preflight_import_references(written["import_document"],
                                            written["material_root"])
    uniqueness = preflight_event_level_uniqueness(
        [{"event_id": summary["event_id"], "unit_id": summary["unit_id"],
          "label": summary["label"], "run_id": summary["run_id"],
          "source_label": args.source_label,
          "qualification": summary["qualification"]}],
        declared_events={args.source_label: {summary["event_id"]}})
    print(f"method            : {method['method_version']}  sha256={method_sha[:16]}...")
    print(f"prediction source : {args.source_label}")
    print(f"run               : {summary['run_id']}  case={summary['case_id']}")
    print(f"round used        : {summary['selected_round']} of "
          f"{summary['recorded_rounds']} recorded "
          f"(configured {summary['run_completeness']['configured_rounds']})")
    print(f"qualification     : {summary['qualification']}")
    print(f"classified agents : escalating={summary['escalating_agents']} "
          f"calming={summary['calming_agents']} undecided={summary['undecided_agents']}")
    print(f"escalation share  : {summary['escalation_share']} "
          f"(threshold {summary['threshold']})")
    print(f"direction label   : {summary['label']}")
    print(f"material root     : {written['material_root']}")
    print(f"inputs            : {written['inputs']}")
    print(f"evidence          : {written['evidence']}")
    print(f"input package     : {written['input_package']}")
    print(f"import document   : {written['import']}")
    print(f"import freeze     : {written['import_freeze']}")
    print(f"reference check   : {'ok' if preflight['ok'] else preflight['problems']}")
    print(f"event uniqueness  : {'ok' if uniqueness['ok'] else uniqueness}")
    if not preflight["ok"] or not uniqueness["ok"]:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
