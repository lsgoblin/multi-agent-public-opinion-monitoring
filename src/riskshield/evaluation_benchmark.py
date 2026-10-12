"""One configurable entry point for the formal benchmark evaluation.

Before this module existed, each batch of frozen material was scored by its own
hand-assembled script (``v04/score_formal_five.py``, ``v03/_score_v19.py``),
which built the engine cases and label entries inline. That worked, but it meant
the admission rules lived in several places at once and the "eligibility table
equals the scoring denominator" property was asserted only inside whichever
script happened to run.

This module replaces those scripts with a single declarative entry point:

* a **config document** names the cohorts, the label sources, the prediction
  document, the authorisation record, the cutoff attestation and the frozen
  admission rules;
* the **unit derivation** stays in :mod:`riskshield.evaluation_alignment`
  (``_sentiment_units`` for the v24 packet, ``_title_units`` for the v19 packet)
  and the **admission rules** stay in
  :mod:`riskshield.evaluation_prediction_import`. Nothing about either rule set
  is re-implemented here, so the table and the denominator cannot drift apart by
  construction;
* the **denominator invariant** is enforced, not documented: the scored set is
  built from exactly the units whose gates pass, and the run raises
  :class:`BenchmarkInvariantError` if the metric engine scores a different set,
  scores fewer rows than the gated set, or scores a unit the table excluded.

Two rules from the round that commissioned this module are encoded directly:

1. every candidate unit reports **all** of its gaps, never a summary that hides a
   second gap;
2. the denominator is **never** filtered by whether the prediction was correct.
   The report restates the frozen rules and the selection-bias disclosure so the
   numbers cannot be re-read as a post-hoc sample choice.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from riskshield.evaluation import evaluate_documents
from riskshield.evaluation_alignment import (
    INPUT_SCHEMA,
    LABEL_SCHEMA,
    SENTIMENT_LABELS,
    _annotator_id,
    _case_for_unit,
    _label_entry,
    _load,
    _prediction_payload,
    _row_is_independent,
    _sentiment_units,
    _submission_defaults,
    _title_units,
    build_cutoff_attestation,
    sha256_file,
    submission_row_key,
    submission_rows,
    unit_gaps,
    unit_gates,
)
from riskshield.evaluation_prediction_import import (
    TASK_SENTIMENT,
    import_formal_predictions,
)

# The two helpers above are private in ``evaluation_alignment`` on purpose: that
# module owns the definitions of "independent annotation" and "engine case", and
# duplicating them here is exactly the drift this module exists to prevent. They
# are imported rather than copied.

CONFIG_SCHEMA = "riskshield.data_evaluation.benchmark_config.v1"
REPORT_SCHEMA = "riskshield.data_evaluation.benchmark_report.v1"
ELIGIBILITY_SCHEMA = "riskshield.data_evaluation.benchmark_eligibility.v1"

# A cohort's units are derived by the loader named in the config. Adding a new
# frozen packet means adding a loader here, not editing the scoring path.
UNIT_SOURCES = {
    "v24_sentiment_units": _sentiment_units,
    "v19_title_units": _title_units,
}

PERMISSION_GATE = "no_recorded_source_permission_evidence"

LABEL_SIDE_AGREED = "two_independent_labels_agree"
LABEL_SIDE_DISAGREE = "independent_labels_disagree_engine_cannot_represent_adjudication"
LABEL_SIDE_INSUFFICIENT = "fewer_than_two_independent_labels"


class BenchmarkConfigError(ValueError):
    """Raised when a benchmark config cannot be safely interpreted."""


class BenchmarkInvariantError(RuntimeError):
    """Raised when the eligibility table and the scoring denominator disagree."""


# --------------------------------------------------------------------------- #
# config
# --------------------------------------------------------------------------- #

def load_config(path: Path) -> dict[str, Any]:
    try:
        config = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise BenchmarkConfigError(f"Cannot read benchmark config: {path}") from exc
    if not isinstance(config, dict):
        raise BenchmarkConfigError("Benchmark config must be a JSON object.")
    validate_config(config)
    return config


def validate_config(config: dict[str, Any]) -> None:
    if config.get("schema") != CONFIG_SCHEMA:
        raise BenchmarkConfigError(f"Expected config schema {CONFIG_SCHEMA}.")
    if not isinstance(config.get("config_version"), str) or not config["config_version"].strip():
        raise BenchmarkConfigError("A nonempty config_version is required.")
    cohorts = config.get("cohorts")
    if not isinstance(cohorts, list) or not cohorts:
        raise BenchmarkConfigError("At least one cohort is required.")
    seen_cohorts: set[str] = set()
    for cohort in cohorts:
        if not isinstance(cohort, dict):
            raise BenchmarkConfigError("Every cohort must be an object.")
        cohort_id = cohort.get("cohort_id")
        if not isinstance(cohort_id, str) or not cohort_id.strip():
            raise BenchmarkConfigError("Every cohort needs a nonempty cohort_id.")
        if cohort_id in seen_cohorts:
            raise BenchmarkConfigError(f"Duplicate cohort_id: {cohort_id}")
        seen_cohorts.add(cohort_id)
        if cohort.get("unit_source") not in UNIT_SOURCES:
            raise BenchmarkConfigError(
                f"cohort {cohort_id}: unit_source must be one of {sorted(UNIT_SOURCES)}.")
        for field in ("dataset_version", "label_version"):
            if not isinstance(cohort.get(field), str) or not cohort[field].strip():
                raise BenchmarkConfigError(f"cohort {cohort_id}: {field} is required.")
        sources = cohort.get("annotation_sources", [])
        if not isinstance(sources, list):
            raise BenchmarkConfigError(f"cohort {cohort_id}: annotation_sources must be an array.")
        for source in sources:
            if not isinstance(source, dict) or not isinstance(source.get("path"), str):
                raise BenchmarkConfigError(
                    f"cohort {cohort_id}: every annotation source needs a path.")
        expected = cohort.get("expected_gated_unit_ids")
        if not isinstance(expected, list) or any(not isinstance(v, str) for v in expected):
            raise BenchmarkConfigError(
                f"cohort {cohort_id}: expected_gated_unit_ids must be a frozen array of unit ids.")


def config_digest(config: dict[str, Any]) -> str:
    payload = json.dumps(config, ensure_ascii=False, sort_keys=True,
                         separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


# --------------------------------------------------------------------------- #
# label side: attach recorded human annotations
# --------------------------------------------------------------------------- #

def attach_annotations(units: list[dict[str, Any]], sources: list[dict[str, Any]],
                       root: Path) -> dict[str, Any]:
    """Attach every eligible independent human annotation to its unit.

    Eligibility is decided by the same helpers the alignment audit uses
    (``_row_is_independent``, ``_annotator_id``, ``_submission_defaults``), so a
    document that is acceptable to the audit is acceptable here. A document that
    is missing a unit simply does not contribute one; it is not an error, because
    the per-unit gate already reports "fewer than two independent labels".

    A cohort with no configured sources keeps whatever annotations its unit
    loader already derived — ``_sentiment_units`` reads the v16/v18 submissions
    itself, and clearing them here would silently empty a cohort that is already
    labelled.
    """
    if not sources:
        return {"sources": [], "units": {},
                "note": "no annotation sources configured; the unit loader's own "
                        "annotations were kept unchanged"}

    loaded: list[tuple[str, dict[str, Any]]] = []
    for source in sources:
        document = _load(root, source["path"])
        rows = submission_rows(document)
        loaded.append((source["path"], document, rows, _submission_defaults(document)))

    report: dict[str, Any] = {"sources": [source["path"] for source in sources], "units": {}}
    for unit in units:
        unit_id = unit["unit_id"]
        annotations: list[dict[str, Any]] = []
        exclusions: list[str] = []
        seen_annotators: set[str] = set()
        for path, document, rows, defaults in loaded:
            match = next(((index, row) for index, row in enumerate(rows)
                          if row.get("unit_id") == unit_id), None)
            if match is None:
                continue
            index, row = match
            stem = Path(path).stem
            if not _row_is_independent(row, defaults):
                exclusions.append(f"blinding_not_established_in_{stem}")
                continue
            annotator = _annotator_id(row, defaults)
            if not isinstance(annotator, str) or not annotator.strip():
                exclusions.append(f"missing_annotator_id_in_{stem}")
                continue
            if annotator in seen_annotators:
                exclusions.append(f"annotator_ids_not_distinct_in_{stem}")
                continue
            if row.get("label") not in SENTIMENT_LABELS:
                exclusions.append(f"label_outside_formal_three_class_scope_in_{stem}")
                continue
            seen_annotators.add(annotator)
            key = submission_row_key(document)
            annotations.append({
                "annotator_id": annotator,
                "label": row["label"],
                "independent": True,
                "prediction_seen": False,
                "evidence_refs": [{"path": path, "pointer": f"/{key}/{index}/label"}],
                "source_document": path,
                "pointer": f"/{key}/{index}/label",
                "submitted_at": row.get("submitted_at", document.get("submitted_at")),
                "source_version": row.get("source_version") or document.get("source_version"),
                "source_class": "recorded_human_submission",
            })
        unit["annotations_initial"] = list(unit.get("annotations", []))
        unit["annotation_exclusions"] = sorted(set(exclusions))
        unit["annotations"] = annotations
        unit["labels_agree"] = (
            len(annotations) >= 2 and len({row["label"] for row in annotations}) == 1)
        report["units"][unit_id] = {
            "attached": len(annotations),
            "annotator_ids": [row["annotator_id"] for row in annotations],
            "labels": [row["label"] for row in annotations],
            "exclusions": unit["annotation_exclusions"],
        }
    return report


def label_side_status(unit: dict[str, Any]) -> str:
    annotations = unit.get("annotations", [])
    if len(annotations) < 2:
        return LABEL_SIDE_INSUFFICIENT
    if len({row["label"] for row in annotations}) == 1:
        return LABEL_SIDE_AGREED
    return LABEL_SIDE_DISAGREE


# --------------------------------------------------------------------------- #
# source side: authorisation record and cutoff attestation
# --------------------------------------------------------------------------- #

def load_permission_record(units: list[dict[str, Any]], permission_record: str | None,
                           root: Path) -> dict[str, Any]:
    """Point each unit at its entry in the owner authorisation record, if any.

    The record is created by the round that owns the material; this function only
    binds units to it. When no record is supplied every unit keeps a null
    reference and the permission gate stays open, which is the honest state of
    the frozen corpus (attribution is not permission).
    """
    if permission_record is None:
        for unit in units:
            unit["permission_evidence_ref"] = None
        return {"recorded": False, "path": None, "bound_unit_ids": []}
    document = _load(root, permission_record)
    entries = document.get("units")
    if not isinstance(entries, dict):
        raise BenchmarkConfigError(
            f"Permission record {permission_record} needs a units object.")
    bound: list[str] = []
    for unit in units:
        if unit["unit_id"] in entries:
            unit["permission_evidence_ref"] = {
                "path": permission_record, "pointer": f"/units/{unit['unit_id']}"}
            bound.append(unit["unit_id"])
        else:
            unit["permission_evidence_ref"] = None
    return {
        "recorded": True,
        "path": permission_record,
        "status": document.get("status"),
        "sha256": sha256_file(root, permission_record),
        "bound_unit_ids": sorted(bound),
        "unbound_unit_ids": sorted(unit["unit_id"] for unit in units
                                   if unit["unit_id"] not in entries),
        "limit_note": document.get("note"),
    }


def ensure_cutoff_attestation(units: list[dict[str, Any]], cutoffs_document: str | None,
                              out_dir_relative: str, root: Path) -> dict[str, Any]:
    """Return the cutoff attestation reference the engine cases should use.

    A packet that declares ``cutoff_utc`` is attested by the packet itself; only
    units whose cutoff is the project's derived ``visible_at + 30 minutes`` rule
    need a generated attestation. Generating one is a write, so it is named and
    reported rather than done silently.
    """
    derived = [unit for unit in units
               if unit.get("cutoff_provenance")
               == "derived_backtest_window_rule_not_historical_freeze"]
    if cutoffs_document is not None:
        return {"reference": cutoffs_document, "generated": False,
                "derived_cutoff_unit_ids": sorted(unit["unit_id"] for unit in derived)}
    if not derived:
        return {"reference": None, "generated": False, "derived_cutoff_unit_ids": []}
    relative = f"{out_dir_relative}/derived-cutoff-attestation.json"
    document = build_cutoff_attestation(derived, relative)
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(document, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
                    encoding="utf-8", newline="\n")
    return {"reference": relative, "generated": True,
            "derived_cutoff_unit_ids": sorted(unit["unit_id"] for unit in derived),
            "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}


def prepare_declared_cutoff_pointers(units: list[dict[str, Any]]) -> None:
    """Let a unit whose cutoff is declared in its own packet attest that cutoff.

    ``_case_for_unit`` resolves a declared cutoff through
    ``unit['declared_cutoff_pointer']`` when no generated attestation is passed.
    The v19 packet declares both ``cutoff_utc`` and the row index, so the pointer
    is derivable instead of hand-written.
    """
    for unit in units:
        if unit.get("cutoff_provenance") != "declared_in_packet":
            continue
        unit["declared_cutoff_pointer"] = (
            f"/{unit['source_row_key']}/{unit['source_row_index']}/cutoff_utc")


# --------------------------------------------------------------------------- #
# prediction side
# --------------------------------------------------------------------------- #

def admit_predictions(units: list[dict[str, Any]], prediction_document: str | None,
                      root: Path) -> dict[str, Any]:
    """Run the single admission point and attach its verdict to each unit."""
    if prediction_document is None:
        admission = {
            "schema": "riskshield.data_evaluation.prediction_import_report.v1",
            "task_type": TASK_SENTIMENT,
            "document_digest": None,
            "admitted": [], "rejected": [], "unmatched": [],
            "counts": {"rows": 0, "admitted": 0, "rejected": 0, "unmatched": 0},
            "rejection_reasons": {},
            "admitted_unit_ids": [],
            "no_candidate_supplied": True,
            "note": "no prediction document was configured for this cohort",
        }
    else:
        document = _load(root, prediction_document)
        admission = import_formal_predictions(document, task_type=TASK_SENTIMENT,
                                              units=units, material_root=root)
    admitted_by_unit = {row["unit_id"]: row for row in admission["admitted"]}
    for unit in units:
        unit["prediction"] = admitted_by_unit.get(unit["unit_id"])
        unit["prediction_source"] = prediction_document
        unit["direction_prediction"] = None
    return {"document": prediction_document, "admission": admission,
            "admitted_by_unit": admitted_by_unit}


# --------------------------------------------------------------------------- #
# scoring and the denominator invariant
# --------------------------------------------------------------------------- #

def _cohort_case(unit: dict[str, Any], predictions: dict[str, Any] | None,
                 dataset_version: str, split: str,
                 cutoffs_document: str | None) -> dict[str, Any]:
    """Build one engine case, choosing the cutoff evidence the unit actually has."""
    if unit.get("cutoff_provenance") == "declared_in_packet":
        return _case_for_unit(unit, predictions, dataset_version, split, None)
    return _case_for_unit(unit, predictions, dataset_version, split, cutoffs_document)


def score_units(units: list[dict[str, Any]], *, dataset_version: str, label_version: str,
                evidence_root: Path, cutoffs_document: str | None,
                admitted_by_unit: dict[str, dict[str, Any]],
                predictions_document: str) -> tuple[list[dict[str, Any]], dict[str, Any], dict[str, Any]]:
    """Score exactly ``units``; the caller passes the gated set and nothing else.

    Each unit resolves its prediction evidence through the document its own
    admitted row came from, so a combined run that merges cohorts with different
    prediction documents still points every reference at a file that exists.
    """
    cases = [
        _cohort_case(unit,
                     _prediction_payload(unit, admitted_by_unit.get(unit["unit_id"]),
                                         unit.get("prediction_source") or predictions_document),
                     dataset_version, unit.get("split") or "holdout", cutoffs_document)
        for unit in units
    ]
    labels = {
        "schema": LABEL_SCHEMA,
        "label_version": label_version,
        "events": [],
        "sentiment_labels": [_label_entry(unit) for unit in units],
    }
    metrics = evaluate_documents(
        {"schema": INPUT_SCHEMA, "dataset_version": dataset_version, "cases": cases},
        labels, evidence_root=evidence_root)
    return cases, labels, metrics


def assert_denominator_matches(scored_units: list[dict[str, Any]],
                               excluded_unit_ids: set[str],
                               metrics: dict[str, Any]) -> dict[str, Any]:
    """Fail loudly when the eligibility table and the denominator disagree.

    Three separate ways the two can drift are checked, because "the scored set
    came from the gated list" is an argument about the code, not a measurement of
    the run. The measurement is the metric report itself.
    """
    expected = sorted(unit["unit_id"] for unit in scored_units)
    sentiment = metrics["sentiment_three_class"]
    engine_scored = sorted(row["unit_id"] for row in sentiment["scored_units"])
    if engine_scored != expected:
        raise BenchmarkInvariantError(
            "the engine scored a different unit set than the eligibility table admitted: "
            f"admitted={expected} scored={engine_scored}")
    if sentiment["sample_size"] != len(scored_units):
        raise BenchmarkInvariantError(
            "the engine sample size differs from the admitted unit count: "
            f"admitted={len(scored_units)} sample_size={sentiment['sample_size']}")
    if sentiment["denominator"] != len(scored_units):
        raise BenchmarkInvariantError(
            "the reported denominator differs from the admitted unit count: "
            f"admitted={len(scored_units)} denominator={sentiment['denominator']}")
    leaked = sorted(excluded_unit_ids & set(engine_scored))
    if leaked:
        raise BenchmarkInvariantError(
            f"units the eligibility table excluded contributed to the denominator: {leaked}")
    return {
        "admitted_unit_ids": expected,
        "engine_scored_unit_ids": engine_scored,
        "excluded_unit_ids": sorted(excluded_unit_ids),
        "admitted_equals_scored": True,
        "denominator_equals_admitted_count": True,
        "excluded_units_present_in_denominator": False,
    }


def eligibility_rows(units: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """One row per candidate unit, listing every gate and every gap."""
    rows: list[dict[str, Any]] = []
    for unit in units:
        gates = unit_gates(unit)
        gaps = unit_gaps(unit)
        rows.append({
            "unit_id": unit["unit_id"],
            "event_id": unit.get("event_id"),
            "source_record_id": unit.get("source_record_id"),
            "publisher": unit.get("publisher"),
            "source_version": unit.get("source_version"),
            "attribution_class": unit.get("attribution_class"),
            "text_sha256": unit.get("text_sha256"),
            "visible_at": unit.get("visible_at"),
            "cutoff": unit.get("cutoff"),
            "cutoff_provenance": unit.get("cutoff_provenance"),
            "split": unit.get("split"),
            "label_side_status": label_side_status(unit),
            "annotation_count": len(unit.get("annotations", [])),
            "annotator_ids": [row.get("annotator_id") for row in unit.get("annotations", [])],
            "annotation_labels": [row.get("label") for row in unit.get("annotations", [])],
            "annotation_exclusions": unit.get("annotation_exclusions", []),
            "prediction_label": (unit.get("prediction") or {}).get("label"),
            "prediction_admitted": bool((unit.get("prediction") or {}).get("admitted")),
            "gated": gates["passed"],
            "gates": gates["gates"],
            # Every gap, not a summary: a unit can miss several things at once.
            "all_gaps": gaps["missing"],
            "gap_count": len(gaps["missing"]),
        })
    return rows


def evaluate_cohort(cohort: dict[str, Any], units: list[dict[str, Any]], *,
                    root: Path, cutoffs_document: str | None,
                    out_dir_relative: str) -> dict[str, Any]:
    """Label, admit, gate and score one cohort from its already-derived units."""
    units = [dict(unit) for unit in units]
    annotation_report = attach_annotations(units, cohort.get("annotation_sources", []), root)
    permission_report = load_permission_record(units, cohort.get("permission_record"), root)
    prediction_report = admit_predictions(units, cohort.get("prediction_document"), root)

    # Two disclosed variants: what the frozen corpus alone supports (attribution
    # only, so the permission gate is open for every unit) and what the owner
    # authorisation record supports. Neither is inferred from the source name.
    permission_refs = {unit["unit_id"]: unit["permission_evidence_ref"] for unit in units}
    for unit in units:
        unit["permission_evidence_ref"] = None
    without_permission = [unit["unit_id"] for unit in units if unit_gates(unit)["passed"]]
    for unit in units:
        unit["permission_evidence_ref"] = permission_refs[unit["unit_id"]]

    rows = eligibility_rows(units)
    gated_units = [unit for unit in units if unit_gates(unit)["passed"]]
    gated_ids = sorted(unit["unit_id"] for unit in gated_units)
    excluded_ids = {unit["unit_id"] for unit in units} - set(gated_ids)

    cases, labels, metrics = score_units(
        gated_units,
        dataset_version=cohort["dataset_version"],
        label_version=cohort["label_version"],
        evidence_root=root,
        cutoffs_document=cutoffs_document,
        admitted_by_unit=prediction_report["admitted_by_unit"],
        predictions_document=cohort.get("prediction_document") or "",
    )
    consistency = assert_denominator_matches(gated_units, excluded_ids, metrics)

    expected = sorted(cohort["expected_gated_unit_ids"])
    return {
        "schema": ELIGIBILITY_SCHEMA,
        "cohort_id": cohort["cohort_id"],
        "unit_source": cohort["unit_source"],
        "dataset_version": cohort["dataset_version"],
        "label_version": cohort["label_version"],
        "units": units,
        "cases": cases,
        "labels": labels,
        "metrics": metrics,
        "eligibility_table": rows,
        "annotation_report": annotation_report,
        "permission_report": permission_report,
        "prediction_report": prediction_report["admission"],
        "gated_unit_ids": gated_ids,
        "excluded_unit_ids": sorted(excluded_ids),
        "expected_gated_unit_ids": expected,
        "expectation_matches": expected == gated_ids,
        "candidate_count": len(units),
        "gated_count": len(gated_units),
        "scoreable_without_permission_record": sorted(without_permission),
        "consistency_check": consistency,
    }


def evaluate_combined(cohorts: list[dict[str, Any]], *, root: Path,
                      cutoffs_document: str | None, dataset_version: str,
                      label_version: str) -> dict[str, Any]:
    """Score the union of every cohort's gated units as the new formal set."""
    units: list[dict[str, Any]] = []
    admitted_by_unit: dict[str, dict[str, Any]] = {}
    for cohort in cohorts:
        units.extend(unit for unit in cohort["units"] if unit["unit_id"] in cohort["gated_unit_ids"])
        admitted_by_unit.update({row["unit_id"]: row
                                 for row in cohort["prediction_report"]["admitted"]})
    seen: set[str] = set()
    duplicates: list[str] = []
    for unit in units:
        if unit["unit_id"] in seen:
            duplicates.append(unit["unit_id"])
        seen.add(unit["unit_id"])
    if duplicates:
        raise BenchmarkInvariantError(
            f"the same unit was admitted by more than one cohort: {sorted(set(duplicates))}")
    all_excluded = {unit_id for cohort in cohorts for unit_id in cohort["excluded_unit_ids"]}
    _, _, metrics = score_units(
        units, dataset_version=dataset_version, label_version=label_version,
        evidence_root=root, cutoffs_document=cutoffs_document,
        admitted_by_unit=admitted_by_unit, predictions_document="")
    consistency = assert_denominator_matches(units, all_excluded, metrics)
    return {
        "dataset_version": dataset_version,
        "label_version": label_version,
        "unit_ids": sorted(unit["unit_id"] for unit in units),
        "contributing_cohorts": sorted(cohort["cohort_id"] for cohort in cohorts),
        "metrics": metrics,
        "consistency_check": consistency,
    }


# --------------------------------------------------------------------------- #
# report
# --------------------------------------------------------------------------- #

def run_benchmark(config: dict[str, Any], root: Path,
                  *, out_dir_relative: str | None = None) -> dict[str, Any]:
    validate_config(config)
    root = Path(root).resolve()
    outputs = config.get("outputs", {})
    relative_out = out_dir_relative or outputs.get("dir")
    if not isinstance(relative_out, str) or not relative_out.strip():
        raise BenchmarkConfigError("An output directory is required (config outputs.dir).")
    try:
        # Resolved against the material root, not the process working directory:
        # every evidence reference in the report is relative to that root.
        (root / relative_out).resolve().relative_to(root)
    except ValueError as exc:
        raise BenchmarkConfigError(
            f"the output directory must live inside the material root: {relative_out}") from exc

    # Derive every cohort's units first, so the cutoff attestation can be
    # resolved once for units that need a derived window instead of one that
    # declares its own cutoff in the frozen packet.
    cohort_units: list[tuple[dict[str, Any], list[dict[str, Any]]]] = []
    for cohort in config["cohorts"]:
        units = [dict(unit) for unit in UNIT_SOURCES[cohort["unit_source"]](root)]
        prepare_declared_cutoff_pointers(units)
        cohort_units.append((cohort, units))

    cutoff_state = ensure_cutoff_attestation(
        [unit for _, units in cohort_units for unit in units],
        config.get("cutoffs_document"), relative_out, root)

    cohorts: list[dict[str, Any]] = []
    for cohort, units in cohort_units:
        cohorts.append(evaluate_cohort(cohort, units, root=root,
                                       cutoffs_document=cutoff_state["reference"],
                                       out_dir_relative=relative_out))

    # Two cohorts may not name the same unit, event or source record. Overlapping
    # identities would make the union count one event twice, so this is reported
    # and refused rather than silently merged.
    overlaps: list[dict[str, str]] = []
    owners: dict[tuple[str, str], str] = {}
    for cohort in cohorts:
        for unit in cohort["units"]:
            for key in ("unit_id", "event_id", "source_record_id"):
                value = unit.get(key)
                if not isinstance(value, str):
                    continue
                previous = owners.setdefault((key, value), cohort["cohort_id"])
                if previous != cohort["cohort_id"]:
                    overlaps.append({"identity_field": key, "identity": value,
                                     "cohorts": f"{previous},{cohort['cohort_id']}"})
    if overlaps:
        raise BenchmarkInvariantError(
            "cohorts share candidate identities, so the union would double count: "
            f"{overlaps}")

    combined = evaluate_combined(
        cohorts, root=root, cutoffs_document=cutoff_state["reference"],
        dataset_version=config.get("combined_dataset_version", "combined-formal-set"),
        label_version=config.get("combined_label_version", "combined-formal-labels"))

    headline = combined["metrics"]["sentiment_three_class"]
    negative = combined["metrics"]["negative_identification"]
    return {
        "schema": REPORT_SCHEMA,
        "config_version": config["config_version"],
        "config_sha256": config_digest(config),
        "generated_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "generated_at_note": "generated now; not a record of when the material was frozen",
        "material_root": str(root),
        "output_dir": relative_out,
        "rules": config.get("rules", {}),
        "cutoffs_document": config.get("cutoffs_document"),
        "cutoff_attestation": cutoff_state,
        "identity_overlap_between_cohorts": overlaps,
        "expectations_all_match": all(cohort["expectation_matches"] for cohort in cohorts),
        "cohorts": [
            {key: value for key, value in cohort.items()
             if key not in ("units", "cases", "labels", "metrics")} | {
                "metrics": cohort["metrics"],
            }
            for cohort in cohorts
        ],
        "formal_combined": {
            **combined,
            "headline_definition": (
                "the union of every cohort's gated units, scored in one engine run; "
                "per-cohort metrics are diagnostics and are not the headline number"),
        },
        "headline": {
            "three_class": {"numerator": headline["numerator"],
                            "denominator": headline["denominator"],
                            "value": headline["value"],
                            "undefined_reason": headline["undefined_reason"]},
            "confusion_matrix_actual_by_predicted":
                headline["confusion_matrix_actual_by_predicted"],
            "negative_counts": negative["counts"],
            "negative_precision": negative["precision"],
            "negative_recall": negative["recall"],
            "binary_accuracy": negative["binary_accuracy"],
            "exclusion_reasons": headline["exclusion_reasons"],
            "sample_size": combined["metrics"]["sample_size"],
            "scored_units": headline["scored_units"],
            "against_the_proclamation_thresholds": {
                "three_class_accuracy": {"required": 0.88, "measured": headline["value"]},
                "negative_precision": {"required": 0.90,
                                       "measured": negative["precision"]["value"]},
                "negative_recall": {"required": 0.85,
                                    "measured": negative["recall"]["value"]},
            },
        },
        "claim_level": "formal_offline_metric_on_a_frozen_gated_denominator",
    }


def write_report(report: dict[str, Any], out_dir: Path) -> list[Path]:
    out_dir.mkdir(parents=True, exist_ok=True)
    written: list[Path] = []
    report_path = out_dir / "benchmark-report-v1.json"
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
                           encoding="utf-8", newline="\n")
    written.append(report_path)
    for cohort in report["cohorts"]:
        path = out_dir / f"eligibility-{cohort['cohort_id']}.json"
        path.write_text(json.dumps(cohort["eligibility_table"], ensure_ascii=False,
                                   indent=2, sort_keys=True) + "\n",
                        encoding="utf-8", newline="\n")
        written.append(path)
    return written


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Run the configured formal benchmark through one entry point.")
    parser.add_argument("--config", required=True, type=Path,
                        help="Benchmark config document.")
    parser.add_argument("--material-root", type=Path, default=None,
                        help="Repository root holding the git-ignored frozen material.")
    parser.add_argument("--out-dir", type=Path, default=None,
                        help="Override the config output directory.")
    args = parser.parse_args(argv)
    try:
        config = load_config(args.config)
        root = (args.material_root or Path(config.get("material_root") or ".")).resolve()
        relative_out = None
        if args.out_dir is not None:
            relative_out = args.out_dir.resolve().relative_to(root).as_posix()
        report = run_benchmark(config, root, out_dir_relative=relative_out)
        written = write_report(report, root / report["output_dir"])
    except (BenchmarkConfigError, BenchmarkInvariantError, ValueError) as exc:
        print(str(exc), file=sys.stderr)
        return 2
    print(json.dumps({"report": str(written[0]), "headline": report["headline"]["three_class"],
                      "negative_precision": report["headline"]["negative_precision"]["value"],
                      "negative_recall": report["headline"]["negative_recall"]["value"],
                      "scored_units": len(report["headline"]["scored_units"])},
                     ensure_ascii=False))
    # A frozen expectation is a pre-commitment, not a target. The report is
    # written either way so the evidence survives, but a mismatch is loud.
    mismatched = [cohort["cohort_id"] for cohort in report["cohorts"]
                  if not cohort["expectation_matches"]]
    if mismatched:
        print("the frozen expectations do not match the run for: "
              + ", ".join(mismatched), file=sys.stderr)
        return 3
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
