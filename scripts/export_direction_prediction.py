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
    PREDICTION_SOURCE, DirectionPredictionError, load_method, write_documents)
from riskshield.store import Store  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", required=True, help="store that holds the run")
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--outdir", required=True,
                        help="directory for the prediction documents; an existing "
                             "prediction document there is never overwritten")
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
                                  method_path=method_path,
                                  dataset_version=args.dataset_version)
    except DirectionPredictionError as exc:
        raise SystemExit(f"cannot export a prediction: {exc}") from None

    summary = written["summary"]
    print(f"method            : {method['method_version']}  sha256={method_sha[:16]}...")
    print(f"prediction source : {PREDICTION_SOURCE}")
    print(f"run               : {summary['run_id']}  case={summary['case_id']}")
    print(f"round used        : {summary['selected_round']} of "
          f"{summary['recorded_rounds']} recorded")
    print(f"classified agents : escalating={summary['escalating_agents']} "
          f"calming={summary['calming_agents']} undecided={summary['undecided_agents']}")
    print(f"escalation share  : {summary['escalation_share']} "
          f"(threshold {summary['threshold']})")
    print(f"direction label   : {summary['label']}")
    print(f"direction predictions for this event: "
          f"{summary['direction_predictions_for_this_event']}")
    print(f"inputs            : {written['inputs']}")
    print(f"evidence          : {written['evidence']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
