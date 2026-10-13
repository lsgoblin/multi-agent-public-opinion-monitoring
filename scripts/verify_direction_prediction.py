"""End-to-end check of the simulation-derived direction prediction against the evaluator.

Offline, deterministic, zero model calls. It answers four questions with evidence:

1. does the produced inputs document pass the evaluator's admission gates at all;
2. does one event carry exactly one direction prediction;
3. does every evidence reference resolve to the value the case claims;
4. does a **wrong** prediction stay inside the denominator instead of being excluded.

The evaluator is imported from ``riskshield.evaluation`` and is never modified. The labels
built here are synthetic and carry a two-annotator, prediction-blind shape so the label gate
can be exercised; the evaluator excludes a synthetic case from formal denominators by
itself, which is exactly why this harness cannot and does not claim any metric is achieved.

Usage, from the repository root (the evaluator resolves references against the CWD):

    python scripts/verify_direction_prediction.py --db <store.db> --run-id <run_id> \
        --outdir artifacts/branch-work/simulation-scale/evidence/<date>/direction-check

The store must already hold a run whose case is ``real_historical`` for the direction path
to be admitted; with any other data mode the report will show the case excluded, and this
script says so rather than pretending the path was exercised.
"""
import argparse
import json
import os
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from riskshield import evaluation  # noqa: E402
from riskshield.direction_prediction import (  # noqa: E402
    DirectionPredictionError, sha256_bytes, write_documents,
    preflight_event_level_uniqueness, preflight_import_references)
from riskshield.store import Store  # noqa: E402

LABEL_SCHEMA = "riskshield.day5.future-labels.v1"
# Read from the evaluator itself rather than restated, so a schema change cannot go unnoticed.
EVALUATION_INPUT_SCHEMA = evaluation.INPUT_SCHEMA


def build_synthetic_labels(inputs: dict, *, observed_direction: str,
                           label_evidence_relative: str) -> dict:
    """Labels shaped like the evaluator's own schema, for the offline path check only.

    Two independent, prediction-blind annotations carry the same sentiment label so the
    label gate is satisfied; the sentiment label itself is a placeholder, because this
    method makes no sentiment claim. The observed direction is what the caller supplies:
    supplying the opposite of the prediction is how the "wrong prediction stays in the
    denominator" behaviour gets exercised.

    The labels point at their **own** evidence document. Pointing an observed outcome at the
    prediction's evidence would compare a value with itself and prove nothing.
    """
    return {
        "schema": LABEL_SCHEMA,
        "label_version": "synthetic-offline-path-check-v1",
        "sentiment_labels": [{
            "evaluation_id": case["evaluation_id"], "event_id": case["event_id"],
            "unit_id": case["unit_id"], "source_record_id": case["target_record_id"],
            "text_sha256": case["text_sha256"],
            "sentiment_annotations": [
                {"annotator_id": f"synth-annotator-{index}", "independent": True,
                 "prediction_seen": False, "label": "neutral",
                 "evidence_refs": [{"path": label_evidence_relative,
                                    "pointer": "/sentiment_label_by_evaluation_id/"
                                               + case["evaluation_id"]}]}
                for index in (1, 2)
            ],
        } for case in inputs["cases"]],
        "events": [{"event_id": event_id, "observed_direction": observed_direction,
                    "evidence_refs": [{"path": label_evidence_relative,
                                       "pointer": "/observed_direction"}]}
                   for event_id in sorted({case["event_id"] for case in inputs["cases"]})],
    }


def write_label_evidence(path: Path, inputs: dict, observed_direction: str,
                         material_root: Path) -> str:
    path.write_text(json.dumps({
        "observed_direction": observed_direction,
        "sentiment_label_by_evaluation_id": {case["evaluation_id"]: "neutral"
                                             for case in inputs["cases"]},
    }, ensure_ascii=False, indent=1), encoding="utf-8")
    return str(path.relative_to(material_root)).replace("\\", "/")


def reference_problems(inputs: dict, evidence_root: Path) -> list:
    """Resolve every reference the case makes, the way the evaluator does."""
    problems = []
    for case in inputs["cases"]:
        refs = [("cutoff_evidence_ref", case.get("cutoff_evidence_ref"), case.get("cutoff")),
                ("title_sha256_ref", case.get("title_sha256_ref"), case.get("text_sha256"))]
        for source in case.get("sources", []):
            refs.extend([
                ("source_record_ref", source.get("source_record_ref"),
                 source.get("record_id")),
                ("source_version_ref", source.get("source_version_ref"),
                 source.get("source_version")),
                ("source_available_at_ref", source.get("source_available_at_ref"),
                 source.get("available_at")),
                ("visibility_evidence_ref", source.get("visibility_evidence_ref"),
                 source.get("available_at")),
            ])
        direction = case.get("predictions", {}).get("direction", {})
        for index, ref in enumerate(direction.get("result_evidence_refs", [])):
            refs.append((f"direction_evidence[{index}]", ref, direction.get("label")))
        for name, ref, expected in refs:
            if not evaluation._ref_matches(ref, expected, evidence_root):
                problems.append({"evaluation_id": case["evaluation_id"], "reference": name,
                                 "pointer": (ref or {}).get("pointer"),
                                 "expected": expected})
    return problems


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", required=True)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--outdir", required=True)
    parser.add_argument("--observed-direction", default=None,
                        help="observed direction to pair with; default is the opposite of "
                             "the prediction, so the wrong-prediction path is exercised")
    parser.add_argument("--unit-kind", default="original_title",
                        choices=["original_title", "original_post"])
    parser.add_argument("--material-root", default=str(PROJECT_ROOT),
                        help="absolute directory every reference is resolved against; the "
                             "evaluator is run from it, and it must contain the output dir")
    args = parser.parse_args()

    outdir = Path(args.outdir).expanduser().resolve()
    outdir.mkdir(parents=True, exist_ok=True)
    material_root = Path(args.material_root).expanduser().resolve()
    store = Store(Path(args.db).expanduser().resolve())
    try:
        written = write_documents(store, args.run_id, outdir, material_root=material_root,
                                 unit_kind=args.unit_kind)
    except DirectionPredictionError as exc:
        raise SystemExit(f"cannot export a prediction: {exc}") from None

    inputs_path = written["inputs"]
    inputs = json.loads(inputs_path.read_text(encoding="utf-8"))
    if inputs.get("schema") != EVALUATION_INPUT_SCHEMA:
        raise SystemExit(f"inputs schema is {inputs.get('schema')!r}, expected "
                         f"{EVALUATION_INPUT_SCHEMA!r}")

    predicted = inputs["cases"][0]["predictions"]["direction"]["label"]
    observed = args.observed_direction or ("calming" if predicted == "escalation"
                                           else "escalation")
    label_evidence = outdir / "synthetic-label-evidence-v1.json"
    label_evidence_relative = write_label_evidence(label_evidence, inputs, observed,
                                                  material_root)
    labels_path = outdir / "synthetic-future-labels-v1.json"
    labels_path.write_text(json.dumps(build_synthetic_labels(
        inputs, observed_direction=observed,
        label_evidence_relative=label_evidence_relative), ensure_ascii=False, indent=1),
        encoding="utf-8")

    # Resolve references the way the evaluator does, so a broken pointer is reported here.
    problems = reference_problems(inputs, material_root)

    # ``riskshield.evaluation.evaluate_files`` hardcodes its evidence root to the process
    # working directory, so the evaluator is run from the material root here. That coupling
    # is a shared-interface question raised in the handoff; this script does not work around
    # it silently, it states where it stands.
    previous_cwd = Path.cwd()
    os.chdir(material_root)
    try:
        report = evaluation.evaluate_files(inputs_path, labels_path)
    finally:
        os.chdir(previous_cwd)
    direction = report["direction_agreement"]
    invalid = direction["excluded_events_or_rows"]
    reasons = sorted({reason for row in invalid for reason in row.get("reasons", [])})
    uniqueness = preflight_event_level_uniqueness(
        [{"event_id": inputs["cases"][0]["event_id"],
          "unit_id": inputs["cases"][0]["unit_id"], "label": predicted,
          "run_id": inputs["cases"][0]["predictions"]["direction"]["run_id"],
          "source_label": "simulation",
          "qualification": inputs["cases"][0]["run_completeness"]["qualification"]}],
        declared_events={"simulation": {inputs["cases"][0]["event_id"]}})
    import_check = preflight_import_references(written["import_document"], material_root)

    print(f"inputs            : {inputs_path}")
    print(f"labels (synthetic): {labels_path}")
    print(f"material root     : {material_root}")
    print(f"prediction        : {predicted} (source="
          f"{inputs['cases'][0]['predictions']['direction'].get('prediction_source')})")
    print(f"qualification     : {inputs['cases'][0]['run_completeness']['qualification']}")
    print(f"observed (synthetic): {observed}")
    print(f"cases             : {len(inputs['cases'])}")
    print(f"unresolved evidence references: {len(problems)}")
    for row in problems:
        print(f"  BROKEN {row}")
    print(f"import reference check: {'ok' if import_check['ok'] else import_check['problems']}")
    print(f"event uniqueness  : {'ok' if uniqueness['ok'] else uniqueness}")
    print(f"direction numerator/denominator: "
          f"{direction['numerator']}/{direction['denominator']}")
    print(f"direction scored events: {direction['scored_events']}")
    print(f"direction exclusion reasons: {reasons or 'none'}")
    print(f"input exclusions  : {report['input_exclusions']['count']} "
          f"{sorted(report['input_exclusions']['reasons'])}")

    ok = (not problems
          and import_check["ok"]
          and uniqueness["ok"]
          and len(inputs["cases"]) >= 1
          and all(list(case["predictions"]) == ["direction"] for case in inputs["cases"])
          and direction["denominator"] >= 1)
    if not ok:
        print("\nRESULT: NOT VERIFIED — see the lines above")
        return 1
    print("\nRESULT: verified offline — one direction prediction per event, every "
          "reference resolves, and the wrong prediction stayed in the denominator")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
