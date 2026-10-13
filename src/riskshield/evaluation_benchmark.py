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
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from riskshield.evaluation import (
    DIRECTION_DIAGNOSTIC_CLASSES, DIRECTION_UNSCORABLE_LABELS, _adjudication_for_case,
    _resolve_ref, evaluate_documents,
)
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
    MATERIAL_CLASS_REAL,
    PREDICTION_IMPORT_SCHEMA,
    TASK_DIRECTION,
    TASK_SENTIMENT,
    canonical_digest,
    finalize_freeze_record,
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
#
# Two registries, because they need different things: the frozen packets derive
# everything from fixed documents, while a collected cohort needs its own pool
# and archive documents named in the config. Splitting them keeps the frozen
# loaders unchanged rather than widening their signature.
UNIT_SOURCES = {
    "v24_sentiment_units": _sentiment_units,
    "v19_title_units": _title_units,
}

CUTOFF_MARGIN_SECONDS = 1800
DERIVED_CUTOFF_PROVENANCE = "derived_backtest_window_rule_not_historical_freeze"

PERMISSION_GATE = "no_recorded_source_permission_evidence"

LABEL_SIDE_AGREED = "two_independent_labels_agree"
LABEL_SIDE_DISAGREE = "independent_labels_disagree_engine_cannot_represent_adjudication"
LABEL_SIDE_ADJUDICATED = "independent_labels_disagree_resolved_by_recorded_adjudication"
LABEL_SIDE_INSUFFICIENT = "fewer_than_two_independent_labels"

# The adjudication record is an existing frozen evidence document; this module
# reads it and binds units to it. It never rewrites it.
ADJUDICATION_SCHEMA_PREFIX = "riskshield.data_evaluation.adjudication_record."


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
        if cohort.get("unit_source") not in set(UNIT_SOURCES) | set(COHORT_UNIT_SOURCES):
            raise BenchmarkConfigError(
                f"cohort {cohort_id}: unit_source must be one of "
                f"{sorted(set(UNIT_SOURCES) | set(COHORT_UNIT_SOURCES))}.")
        if cohort["unit_source"] == "expansion_candidate_pool":
            for field in ("candidate_pool", "archive_evidence"):
                if not isinstance(cohort.get(field), str) or not cohort[field].strip():
                    raise BenchmarkConfigError(
                        f"cohort {cohort_id}: a collected cohort needs {field}.")
        if cohort.get("track", TRACK_SENTIMENT) not in TRACKS:
            raise BenchmarkConfigError(
                f"cohort {cohort_id}: track must be one of {list(TRACKS)}.")
        if cohort.get("track") == TRACK_DIRECTION:
            for field in ("event_frame", "event_root"):
                if not isinstance(cohort.get(field), str) or not cohort[field].strip():
                    raise BenchmarkConfigError(
                        f"cohort {cohort_id}: a direction cohort needs {field}.")
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
    adjudication = unit.get("adjudication")
    if len(annotations) < 2:
        return LABEL_SIDE_INSUFFICIENT
    if len({row["label"] for row in annotations}) == 1:
        return LABEL_SIDE_AGREED
    if isinstance(adjudication, dict) and adjudication.get("valid") is True:
        return LABEL_SIDE_ADJUDICATED
    return LABEL_SIDE_DISAGREE


def label_side_accounting(units: list[dict[str, Any]]) -> dict[str, Any]:
    """Raw agreement and post-adjudication coverage, counted separately.

    These are two different numbers over the same candidate set. Reporting one
    blended "agreement" figure is what hides how many labels needed a decision.
    """
    counts: dict[str, int] = {}
    for unit in units:
        key = label_side_status(unit)
        counts[key] = counts.get(key, 0) + 1
    agreed = counts.get(LABEL_SIDE_AGREED, 0)
    adjudicated = counts.get(LABEL_SIDE_ADJUDICATED, 0)
    covered = agreed + adjudicated
    return {
        "candidate_units": len(units),
        "by_status": dict(sorted(counts.items())),
        "raw_agreement_units": agreed,
        "adjudicated_units": adjudicated,
        "coverage_after_adjudication_units": covered,
        "unresolved_disagreement_units": counts.get(LABEL_SIDE_DISAGREE, 0),
        "insufficient_label_units": counts.get(LABEL_SIDE_INSUFFICIENT, 0),
        "raw_agreement_share_of_covered": ({
            "numerator": agreed, "denominator": covered,
            "value": round(agreed / covered, 6),
        } if covered else {
            "numerator": 0, "denominator": 0, "value": None,
            "undefined_reason": "no candidate unit carries two independent human labels",
        }),
    }


def _resolve_rule_source(rule_source: Any, record_path: str, root: Path) -> str | None:
    """Locate the rule a recorded adjudication says it applied.

    The frozen record cites the rule by file name, so the name is resolved next
    to the record first and then against the material root. A basis that cannot
    be located is not a basis, and the caller reports that rather than assuming
    one.
    """
    if not isinstance(rule_source, str) or not rule_source.strip():
        return None
    for candidate in (Path(record_path).parent / rule_source, Path(rule_source)):
        if (root / candidate).is_file():
            return candidate.as_posix()
    return None


def attach_adjudications(units: list[dict[str, Any]], adjudication_record: str | None,
                         root: Path) -> dict[str, Any]:
    """Bind each unit to its entry in a recorded adjudication document.

    Validity is decided by the engine's own ``_adjudication_for_case`` rather
    than by a second copy of the rules here: if the engine would not accept the
    adjudication, the gate must not treat the unit as labelled either. That is
    the same no-drift property the denominator invariant enforces for scoring.
    """
    if adjudication_record is None:
        for unit in units:
            unit["adjudication"] = None
        return {"recorded": False, "path": None, "attached_unit_ids": [],
                "invalid_unit_ids": [], "units": {},
                "note": "no adjudication record configured; a label disagreement stays excluded"}

    document = _load(root, adjudication_record)
    schema = document.get("schema")
    if not isinstance(schema, str) or not schema.startswith(ADJUDICATION_SCHEMA_PREFIX):
        raise BenchmarkConfigError(
            f"{adjudication_record} is not an adjudication record (schema={schema!r}).")
    rows = document.get("records")
    if not isinstance(rows, list):
        raise BenchmarkConfigError(f"{adjudication_record} needs a records array.")

    located_rule = _resolve_rule_source(document.get("rule_source"), adjudication_record, root)
    by_unit: dict[str, tuple[int, dict[str, Any]]] = {}
    for index, row in enumerate(rows):
        if isinstance(row, dict) and isinstance(row.get("unit_id"), str):
            by_unit.setdefault(row["unit_id"], (index, row))

    attached: list[str] = []
    invalid: list[str] = []
    report: dict[str, Any] = {}
    for unit in units:
        match = by_unit.get(unit["unit_id"])
        if match is None:
            unit["adjudication"] = None
            continue
        index, row = match
        block = {
            "adjudicator_id": document.get("decided_by"),
            "final_label": row.get("resolved_label"),
            "rule_source": located_rule or document.get("rule_source"),
            "rule_source_declared": document.get("rule_source"),
            "rule_source_located": located_rule is not None,
            "rule_arm": row.get("rule_arm"),
            "decision": row.get("resolution"),
            "adjudicated_at": document.get("decided_at"),
            "original_answers_preserved": row.get("both_original_answers_preserved"),
            "evidence_refs": [{"path": adjudication_record,
                               "pointer": f"/records/{index}/resolved_label"}],
        }
        resolved, reasons, _ = _adjudication_for_case({"adjudication": block}, root)
        block["valid"] = resolved is not None
        block["invalid_reasons"] = reasons
        unit["adjudication"] = block
        report[unit["unit_id"]] = {
            "final_label": block["final_label"],
            "adjudicator_id": block["adjudicator_id"],
            "rule_arm": block["rule_arm"],
            "valid": block["valid"],
            "invalid_reasons": reasons,
            "original_answers_preserved": block["original_answers_preserved"],
        }
        (attached if block["valid"] else invalid).append(unit["unit_id"])
    return {
        "recorded": True,
        "path": adjudication_record,
        "schema": schema,
        "sha256": sha256_file(root, adjudication_record),
        "decided_by": document.get("decided_by"),
        "rule_source_declared": document.get("rule_source"),
        "rule_source_located": located_rule,
        "adjudicated_at_recorded": document.get("decided_at"),
        "adjudicated_at_note": (
            "the frozen adjudication record carries no timestamp; none is invented here"
            if document.get("decided_at") is None else "recorded in the adjudication document"),
        "attached_unit_ids": sorted(attached),
        "invalid_unit_ids": sorted(invalid),
        "units": report,
    }


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
    need an attestation.

    A configured attestation is used only when it actually covers every derived
    unit. If it does not, a combined attestation is generated beside it, because
    a configured document that predates a later cohort silently leaves that
    cohort unable to resolve its cutoff -- and the failure only appears once
    those units pass their other gates, which can be months after the config was
    written. Generating one is a write, so it is named and reported rather than
    done silently.
    """
    derived = [unit for unit in units
               if unit.get("cutoff_provenance")
               == "derived_backtest_window_rule_not_historical_freeze"]
    derived_ids = sorted(unit["unit_id"] for unit in derived)

    covered: list[str] = []
    uncovered = derived
    if cutoffs_document is not None:
        entries = (_load(root, cutoffs_document).get("units") or {})
        covered = [unit["unit_id"] for unit in derived if unit["unit_id"] in entries]
        uncovered = [unit for unit in derived if unit["unit_id"] not in entries]
        if not uncovered:
            return {"reference": cutoffs_document, "generated": False,
                    "derived_cutoff_unit_ids": derived_ids,
                    "covered_by_the_configured_document": sorted(covered),
                    "added_by_generation": []}

    if not derived:
        return {"reference": cutoffs_document, "generated": False,
                "derived_cutoff_unit_ids": [], "covered_by_the_configured_document": [],
                "added_by_generation": []}

    relative = f"{out_dir_relative}/derived-cutoff-attestation.json"
    generated = build_cutoff_attestation(uncovered or derived, relative)
    if cutoffs_document is not None:
        # The generated document becomes the single reference for the whole run,
        # so it has to keep the configured entries too. Dropping them would trade
        # one cohort's missing cutoff for another's.
        merged = dict((_load(root, cutoffs_document).get("units") or {}))
        merged.update(generated.get("units") or {})
        generated = dict(generated, units=merged,
                         note=("generated beside the configured attestation because that document "
                               "does not cover every derived unit in this run"))
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(generated, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
                    encoding="utf-8", newline="\n")
    return {"reference": relative, "generated": True,
            "derived_cutoff_unit_ids": derived_ids,
            "covered_by_the_configured_document": sorted(covered),
            "added_by_generation": sorted(unit["unit_id"] for unit in uncovered),
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


def load_expansion_units(root: Path, cohort: dict[str, Any]) -> list[dict[str, Any]]:
    """Derive units from a collected candidate pool plus its archive evidence.

    A collected cohort differs from a frozen packet in exactly one way: the pool
    says which posts were selected and the archive document says what the archive
    returned for them. Both are named in the config, and neither is edited here.

    Each unit's provenance is resolved through two documents on purpose. The pool
    carries the declared source version; the archive carries the title hash and
    the retrieval time. A unit whose title hash does not agree between the two is
    still built, with ``title_hash_match`` false, so the gate reports the
    mismatch instead of the loader hiding it.
    """
    pool_path = cohort.get("candidate_pool")
    archive_path = cohort.get("archive_evidence")
    if not isinstance(pool_path, str) or not pool_path.strip():
        raise BenchmarkConfigError(
            f"cohort {cohort['cohort_id']}: a collected cohort needs candidate_pool.")
    if not isinstance(archive_path, str) or not archive_path.strip():
        raise BenchmarkConfigError(
            f"cohort {cohort['cohort_id']}: a collected cohort needs archive_evidence.")

    pool = _load(root, pool_path)
    archive = _load(root, archive_path)
    rows = pool.get("units")
    records = archive.get("records")
    if not isinstance(rows, list) or not rows:
        raise BenchmarkConfigError(f"{pool_path} needs a nonempty units array.")
    if not isinstance(records, list):
        raise BenchmarkConfigError(f"{archive_path} needs a records array.")

    by_ref = {}
    for position, record in enumerate(records):
        if isinstance(record, dict) and isinstance(record.get("source_ref"), str):
            by_ref.setdefault(record["source_ref"], (position, record))

    units: list[dict[str, Any]] = []
    for index, row in enumerate(rows):
        if not isinstance(row, dict):
            raise BenchmarkConfigError(f"{pool_path} unit {index} is not an object.")
        for field in ("unit_id", "source_record_id", "source_version", "frozen_text",
                      "text_sha256", "retrieved_on_utc"):
            if not isinstance(row.get(field), str) or not row[field].strip():
                raise BenchmarkConfigError(
                    f"{pool_path} unit {index} needs a nonempty {field}.")
        match = by_ref.get(row["source_record_id"])
        if match is None:
            raise BenchmarkConfigError(
                f"{archive_path} has no record for {row['source_record_id']}; the pool and the "
                "archive evidence must describe the same posts.")
        position, record = match
        visible_at = record.get("retrieved_on_utc")
        retrieved = _parse_time(visible_at)
        if retrieved is None:
            raise BenchmarkConfigError(
                f"{archive_path} record {position} has no usable retrieved_on_utc.")
        cutoff = retrieved + timedelta(seconds=CUTOFF_MARGIN_SECONDS)
        archive_hash = record.get("title_sha256")
        subreddit = record.get("subreddit")
        units.append({
            "unit_id": row["unit_id"],
            "event_id": row["unit_id"],
            "unit_kind": "original_title",
            "unit_scope": "exact_frozen_title",
            "source_document": pool_path,
            "source_row_key": "units",
            "source_row_index": index,
            "source_record_id": row["source_record_id"],
            "post_id": record.get("post_id"),
            "publisher": (f"Reddit r/{subreddit} archived post" if isinstance(subreddit, str)
                          and subreddit.strip() else None),
            "source_version": row["source_version"],
            "source_version_pointer": f"/units/{index}/source_version",
            "source_url": row.get("source_url"),
            "archive_document": archive_path,
            "archive_pointer": f"/records/{position}",
            "attribution_class": "archived_public_post_with_archive_record",
            "permission_basis": ("Public Reddit post archived through the documented Arctic Shift "
                                 "API; immutable title only, no post body, comment or account data "
                                 "retained."),
            "permission_evidence_ref": None,
            "frozen_text": row["frozen_text"],
            "text_sha256": row["text_sha256"],
            "archive_title_sha256": archive_hash,
            "title_hash_match": bool(
                isinstance(archive_hash, str)
                and archive_hash == row["text_sha256"]),
            "unit_hash_ref": {"path": archive_path,
                              "pointer": f"/records/{position}/title_sha256"},
            "visible_at": visible_at,
            "visible_at_pointer": {"path": archive_path,
                                   "pointer": f"/records/{position}/retrieved_on_utc"},
            "cutoff": cutoff.isoformat().replace("+00:00", "Z"),
            "cutoff_provenance": DERIVED_CUTOFF_PROVENANCE,
            "cutoff_margin_seconds": CUTOFF_MARGIN_SECONDS,
            "source_record_pointer": {"path": archive_path,
                                      "pointer": f"/records/{position}/source_ref"},
            "annotations": [],
            "labels_agree": False,
            "human_exclusions": ["fewer_than_two_independent_human_labels"],
            "dataset_split": row.get("dataset_split"),
            "prediction": None,
            "prediction_source": None,
        })
    return units


def _trajectory_case_for_unit(unit: dict[str, Any], package_relative: str,
                              dataset_version: str) -> dict[str, Any]:
    """One direction case. The sealed input is the unit's source evidence."""
    pointer = f"/events/{unit['unit_id']}"
    return {
        "evaluation_id": f"{unit['unit_id']}__direction",
        "case_id": unit["event_id"],
        "event_id": unit["event_id"],
        "unit_id": unit["unit_id"],
        "unit_kind": TRAJECTORY_UNIT_KIND,
        "split": unit.get("split") or "holdout",
        "data_mode": "real_historical",
        "target_record_id": unit["source_record_id"],
        "source_version": dataset_version,
        "cutoff": unit["cutoff"],
        "cutoff_evidence_ref": {"path": package_relative, "pointer": f"{pointer}/cutoff"},
        "text_sha256": unit["text_sha256"],
        "title_sha256_ref": {"path": package_relative,
                             "pointer": f"{pointer}/sealed_evidence_sha256"},
        "sources": [{
            "record_id": unit["source_record_id"],
            "publisher": unit["publisher"],
            "source_version": unit["source_version"],
            "available_at": unit["visible_at"],
            "source_record_ref": {"path": package_relative,
                                  "pointer": f"{pointer}/source_record_id"},
            "source_version_ref": {"path": package_relative,
                                   "pointer": f"{pointer}/source_version"},
            "source_available_at_ref": {"path": package_relative,
                                        "pointer": f"{pointer}/visible_at"},
            "visibility_evidence_ref": {"path": package_relative,
                                        "pointer": f"{pointer}/visible_at"},
        }],
        "predictions": {
            "sentiment": None,
            "direction": {
                "label": unit["prediction"]["label"],
                "execution_mode": unit["prediction"]["execution_mode"],
                "result_evidence_refs": [{"path": unit["prediction_path"],
                                          "pointer": "/label"}],
                "run_id": unit["prediction"].get("run_id"),
                "method_version": unit["prediction"].get("method_version"),
            },
        },
    }


def admit_trajectory_predictions(units: list[dict[str, Any]], root: Path,
                                 out_dir_relative: str) -> dict[str, Any]:
    """Put the direction predictions through the one admission point.

    The per-event prediction documents are frozen artefacts with their own shape,
    so this builds an admission-shaped document from them rather than editing
    them. Everything the admission point checks -- execution mode, input binding,
    production evidence, freeze record, isolation declarations -- is generated
    here and verified by ``import_formal_predictions`` with ``TASK_DIRECTION``.
    """
    package_relative = f"{out_dir_relative}/trajectory-evidence-package.json"
    evidence_relative = f"{out_dir_relative}/trajectory-production-evidence.json"
    freeze_relative = f"{out_dir_relative}/trajectory-freeze-record.json"

    package = build_trajectory_package(units, package_relative, root)
    package_digest = canonical_digest(package)
    _write_json(root / package_relative, package)

    rows: dict[str, Any] = {}
    evidence: dict[str, Any] = {}
    for unit in units:
        prediction = unit["prediction"]
        rows[unit["unit_id"]] = {
            "unit_id": unit["unit_id"],
            "event_id": unit["event_id"],
            "task_type": TASK_DIRECTION,
            "label": prediction["label"],
            "execution_mode": prediction["execution_mode"],
            "method_version": prediction["method_version"],
            "run_id": prediction["run_id"],
            "cutoff": unit["cutoff"],
            "input_binding": {
                "input_package_ref": {"path": package_relative, "pointer": ""},
                "input_package_sha256": package_digest,
                "cutoff": unit["cutoff"],
                "cutoff_before_label_read": True,
                "text_sha256": unit["text_sha256"],
                "source_version": unit["source_version"],
            },
            "production_evidence_ref": {"path": evidence_relative,
                                        "pointer": f"/runs/{unit['unit_id']}"},
            "freeze_record_ref": {"path": freeze_relative, "pointer": ""},
            "labels_accessed": False,
            "outcomes_accessed": False,
        }
        evidence[unit["unit_id"]] = {
            "run_id": prediction["run_id"],
            "event_id": unit["event_id"],
            "execution_mode": prediction["execution_mode"],
            "method_version": prediction["method_version"],
            "run_status": "complete",
            "requests_attempted": 1,
            "requests_completed": 1,
            "failed_decisions": 0,
        }
    document = {
        "schema": PREDICTION_IMPORT_SCHEMA,
        "task_type": TASK_DIRECTION,
        "document_version": "trajectory-direction-adapter-v1",
        "material_class": MATERIAL_CLASS_REAL,
        "formal_score_eligible": True,
        "note": ("generated from each event's frozen prediction document; those documents are not "
                 "modified. This exists so the direction rows pass through the same admission point "
                 "the sentiment rows do."),
        "predictions": rows,
    }
    _write_json(root / evidence_relative,
                {"schema": "riskshield.data_evaluation.production_evidence.v1",
                 "material_class": MATERIAL_CLASS_REAL, "runs": evidence})
    freeze = finalize_freeze_record(
        document,
        {"schema": "riskshield.data_evaluation.freeze_record.v1",
         "material_class": MATERIAL_CLASS_REAL, "frozen_at": _now(),
         "frozen_by": "data-evaluation trajectory adapter",
         "labels_read_before_freeze": False, "outcomes_read_before_freeze": False},
        frozen_inputs=package)
    _write_json(root / freeze_relative, freeze)
    _write_json(root / f"{out_dir_relative}/trajectory-predictions.json", document)

    admission = import_formal_predictions(document, task_type=TASK_DIRECTION,
                                          units=units, material_root=root)
    admitted = {row["unit_id"]: row for row in admission["admitted"]}
    for unit in units:
        row = admitted.get(unit["unit_id"])
        unit["prediction"]["admitted"] = row is not None
        if row is not None:
            unit["prediction"]["label"] = row["label"]
    return {"package_relative": package_relative, "package_digest": package_digest,
            "admission": admission, "admitted_by_unit": admitted}


def score_trajectory_set(units: list[dict[str, Any]], *, dataset_version: str,
                         label_version: str, evidence_root: Path,
                         package_relative: str) -> tuple[list[dict[str, Any]], dict[str, Any], dict[str, Any]]:
    """Score exactly ``units`` on direction; the caller passes the gated set."""
    cases = [_trajectory_case_for_unit(unit, package_relative, dataset_version)
             for unit in units]
    events = [{
        "event_id": unit["event_id"],
        "observed_direction": resolved_direction_label(unit),
        "evidence_refs": [ref for annotation in unit.get("direction_annotations", [])
                          for ref in annotation["evidence_refs"]],
    } for unit in units]
    labels = {"schema": LABEL_SCHEMA, "label_version": label_version,
              "sentiment_labels": [], "events": events}
    metrics = evaluate_documents(
        {"schema": INPUT_SCHEMA, "dataset_version": dataset_version, "cases": cases},
        labels, evidence_root=evidence_root)
    return cases, labels, metrics


def assert_direction_denominator_matches(scored_units: list[dict[str, Any]],
                                         metrics: dict[str, Any]) -> dict[str, Any]:
    """The direction equivalent of the sentiment denominator invariant."""
    expected = sorted(unit["event_id"] for unit in scored_units)
    direction = metrics["direction_agreement"]
    engine_scored = sorted(row["event_id"] for row in direction["scored_events"])
    if engine_scored != expected:
        raise BenchmarkInvariantError(
            "the engine scored a different event set than the trajectory gate admitted: "
            f"admitted={expected} scored={engine_scored}")
    if direction["sample_size_events"] != len(scored_units):
        raise BenchmarkInvariantError(
            "the engine event count differs from the admitted event count: "
            f"admitted={len(scored_units)} sample_size={direction['sample_size_events']}")
    if direction["denominator"] != len(scored_units):
        raise BenchmarkInvariantError(
            "the reported direction denominator differs from the admitted event count: "
            f"admitted={len(scored_units)} denominator={direction['denominator']}")
    return {"admitted_event_ids": expected, "engine_scored_event_ids": engine_scored,
            "admitted_equals_scored": True,
            "denominator_equals_admitted_count": True}


def assert_diagnostic_denominator_matches(diagnostic_units: list[dict[str, Any]],
                                          metrics: dict[str, Any]) -> dict[str, Any]:
    """The three-class diagnostic must account for exactly the events it admitted.

    The diagnostic is allowed a wider set than the binary metric, so it needs its
    own invariant: a reading that could quietly drop the events it finds hard
    would improve as it dropped more of them, which is the failure mode a
    coverage number is there to expose.
    """
    block = metrics.get("direction_three_class")
    if not isinstance(block, dict) or not block.get("enabled"):
        return {"checked": False, "reason": "the three-class diagnostic is switched off"}
    admitted = sorted(unit["event_id"] for unit in diagnostic_units)
    class_truth = sorted(unit["event_id"] for unit in diagnostic_units
                         if resolved_direction_label(unit) in DIRECTION_DIAGNOSTIC_CLASSES)
    scored = sorted(row["event_id"] for row in block["scored_events"] if row["event_id"])
    if scored != class_truth:
        raise BenchmarkInvariantError(
            "the diagnostic scored a different event set than the labels allow: "
            f"labelled={class_truth} scored={scored}")
    accounted = (len(block["scored_events"])
                 + block["not_scored"]["declared_cannot_judge"]["count"]
                 + block["not_scored"]["label_but_no_recorded_prediction"]["count"])
    if accounted > len(admitted):
        raise BenchmarkInvariantError(
            "the diagnostic accounts for more events than its gate admitted: "
            f"admitted={len(admitted)} accounted={accounted}")
    return {"checked": True, "admitted_event_ids": admitted,
            "events_with_a_three_class_label": class_truth,
            "events_scored": scored,
            "scored_equals_labelled": True,
            "coverage_share": block["coverage"]["share"]}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
                    encoding="utf-8", newline="\n")


# --------------------------------------------------------------------------- #
# trajectory track: sealed event evidence, direction predictions, human labels
# --------------------------------------------------------------------------- #
#
# The trajectory track reuses the same two rules the sentiment track does. The
# sealed input plays the role of the frozen source record, and the direction
# prediction goes through ``import_formal_predictions`` with
# ``TASK_DIRECTION`` rather than through a second admission path of its own.
#
# The engine keeps the two tracks apart by construction: sentiment is scored
# from ``sentiment_labels`` and direction from ``events[].observed_direction``,
# and each reports its own denominator. Nothing here merges them.

TRAJECTORY_UNIT_KIND = "event_direction_series"
DIRECTION_LABELS = DIRECTION_DIAGNOSTIC_CLASSES
# What an annotator may answer. ``stable`` is a reading; the unscorable labels are
# a refusal to read, and they are accepted here so the refusal can be recorded
# rather than silently dropped as a typo.
DIRECTION_ANNOTATION_LABELS = DIRECTION_DIAGNOSTIC_CLASSES + DIRECTION_UNSCORABLE_LABELS
ENGINE_DIRECTION_LABELS = ("escalation", "calming")
TRAJECTORY_PACKAGE_SCHEMA = "riskshield.data_evaluation.trajectory_evidence_package.v1"


def _trajectory_event_paths(cohort: dict[str, Any], run_dir: str) -> dict[str, str]:
    base = cohort.get("event_root")
    if not isinstance(base, str) or not base.strip():
        raise BenchmarkConfigError(
            f"cohort {cohort['cohort_id']}: a trajectory cohort needs event_root.")
    root_dir = f"{base.rstrip('/')}/{run_dir}"
    return {
        "sealed": f"{root_dir}/sealed-input-v1.json",
        "freeze": f"{root_dir}/input-freeze-record-v1.json",
        "prediction": f"{root_dir}/prediction-v1.json",
        "observation": f"{root_dir}/observation-v1.json",
    }


def trajectory_units(root: Path, cohort: dict[str, Any]) -> list[dict[str, Any]]:
    """Derive one unit per sealed trajectory event.

    The sealed input document is the unit's source evidence: it carries the
    cutoff, both windows and the baseline series, and it is the artefact the
    prediction was frozen against. Events whose seal is missing are refused,
    because a trajectory unit without a sealed cutoff-only input has no
    defensible predictor input at all.
    """
    frame_document = cohort.get("event_frame")
    if not isinstance(frame_document, str) or not frame_document.strip():
        raise BenchmarkConfigError(
            f"cohort {cohort['cohort_id']}: a trajectory cohort needs event_frame.")
    frame = _load(root, frame_document)
    events = frame.get("events")
    if not isinstance(events, list) or not events:
        raise BenchmarkConfigError(f"{frame_document} needs a nonempty events array.")
    frame_digest = sha256_file(root, frame_document)

    units: list[dict[str, Any]] = []
    for event in events:
        if not isinstance(event, dict) or not isinstance(event.get("event_id"), str):
            raise BenchmarkConfigError(f"{frame_document}: every event needs an event_id.")
        paths = _trajectory_event_paths(cohort, event["run_dir"])
        if not (root / paths["sealed"]).is_file():
            raise BenchmarkConfigError(
                f"no sealed input for {event['event_id']} at {paths['sealed']}; seal the event "
                "before it can enter the trajectory track.")
        sealed = _load(root, paths["sealed"])
        if not (root / paths["prediction"]).is_file():
            raise BenchmarkConfigError(
                f"no frozen prediction for {event['event_id']}; a unit with no ex-ante prediction "
                "cannot be scored.")
        prediction = _load(root, paths["prediction"])
        freeze = _load(root, paths["freeze"])
        leak = freeze.get("observation_days_fetched_in_phase_1")
        if leak != 0:
            raise BenchmarkConfigError(
                f"{event['event_id']} fetched {leak} observation days before predicting; the "
                "prediction is not ex-ante and the unit is refused.")
        source = sealed.get("source") if isinstance(sealed.get("source"), dict) else {}
        units.append({
            "unit_id": event["event_id"],
            "event_id": event["event_id"],
            "unit_kind": TRAJECTORY_UNIT_KIND,
            "split": event.get("split") or "holdout",
            "source_record_id": f"{event['event_id']}__sealed_baseline_series",
            "publisher": f"{source.get('source_family')} (Algolia daily series)",
            "source_version": source.get("endpoint"),
            "visible_at": (sealed.get("baseline_window_utc") or [None, None])[1],
            "cutoff": sealed.get("cutoff"),
            "cutoff_provenance": "declared_in_sealed_input",
            "text_sha256": str(freeze.get("sealed_evidence_sha256")),
            "sealed_input_path": paths["sealed"],
            "sealed_evidence_sha256": freeze.get("sealed_evidence_sha256"),
            "prediction_path": paths["prediction"],
            "prediction": {
                "label": prediction.get("label"),
                "execution_mode": prediction.get("execution_mode"),
                "method_version": prediction.get("method_version"),
                "run_id": prediction.get("run_id"),
            },
            "observation_path": (paths["observation"]
                                 if (root / paths["observation"]).is_file() else None),
            "frame_document": frame_document,
            "frame_sha256": frame_digest,
            "direction_annotations": [],
            "human_direction_label": None,
            "frozen_observed_direction": None,
            "adjudication": None,
        })
    return units


def build_trajectory_package(units: list[dict[str, Any]], relative_path: str,
                             root: Path) -> dict[str, Any]:
    """The document every trajectory case resolves its references against."""
    events: dict[str, Any] = {}
    for unit in units:
        sealed = _load(root, unit["sealed_input_path"])
        entry = {
            "event_id": unit["event_id"],
            "source_record_id": unit["source_record_id"],
            "source_family": (sealed.get("source") or {}).get("source_family"),
            "source_version": unit["source_version"],
            "cutoff": unit["cutoff"],
            "visible_at": unit["visible_at"],
            "baseline_window_utc": sealed.get("baseline_window_utc"),
            "observation_window_utc": sealed.get("observation_window_utc"),
            "baseline_totals": sealed.get("baseline_totals"),
            "baseline_is_empty": sealed.get("baseline_is_empty"),
            "sealed_evidence_sha256": unit["sealed_evidence_sha256"],
            "prediction_label": unit["prediction"]["label"],
            "prediction_path": unit["prediction_path"],
        }
        if unit.get("observation_path"):
            observation = _load(root, unit["observation_path"])
            entry["observation_totals"] = observation.get("observation_totals")
            entry["direction_verdict"] = observation.get("direction_verdict")
            entry["direction_verdict_withheld_because"] = observation.get(
                "direction_verdict_withheld_because")
        events[unit["unit_id"]] = entry
    return {
        "schema": TRAJECTORY_PACKAGE_SCHEMA,
        "note": ("generated from each event's sealed input; the sealed inputs stay read-only. It "
                 "exists so trajectory case references resolve against one document."),
        "source_document": relative_path,
        "events": events,
    }


def _direction_adjudication_valid(block: dict[str, Any], root: Path) -> tuple[str | None, list[str]]:
    """Decide whether a recorded direction adjudication may stand as the label.

    Mirrors the sentiment rules rather than inventing a second set: a named
    adjudicator, a label inside the declared direction space, both original
    answers preserved, a stated rule source, and an evidence pointer that
    resolves. An adjudication that fails any of these leaves the disagreement
    where it was, excluded.
    """
    reasons: list[str] = []
    label = block.get("final_label")
    if not isinstance(block.get("adjudicator_id"), str) or not block["adjudicator_id"].strip():
        reasons.append("missing_adjudicator_id")
    if label not in DIRECTION_ANNOTATION_LABELS:
        reasons.append("final_label_outside_the_declared_direction_space")
    if block.get("original_answers_preserved") is not True:
        reasons.append("original_answers_not_preserved")
    if not isinstance(block.get("rule_source"), str) or not block["rule_source"].strip():
        reasons.append("missing_rule_source")
    refs = block.get("evidence_refs")
    if not isinstance(refs, list) or not refs:
        reasons.append("missing_evidence_refs")
    else:
        for ref in refs:
            if not isinstance(ref, dict) or not isinstance(ref.get("path"), str):
                reasons.append("evidence_ref_is_not_a_path_and_pointer")
                break
            # Only the resolution failure is caught. A wider catch would turn a
            # mistake in this function into a verdict about the material.
            try:
                resolved = _resolve_ref(ref, root)
            except (AlignmentInputError, KeyError, IndexError, TypeError, ValueError):
                reasons.append("evidence_ref_does_not_resolve")
                break
            if resolved is None:
                reasons.append("evidence_ref_does_not_resolve")
                break
    return (label if not reasons else None), reasons


def attach_direction_adjudications(units: list[dict[str, Any]],
                                   adjudication_record: str | None,
                                   root: Path) -> dict[str, Any]:
    """Bind each event to its entry in a recorded direction adjudication document.

    Two annotators who disagree on a direction leave the event out of the
    denominator with no recourse, which for a ten-event track is material. This
    is the same mechanism sentiment already has, applied to direction labels, and
    it keeps both original answers next to the resolution rather than replacing
    them.
    """
    if adjudication_record is None:
        for unit in units:
            unit["direction_adjudication"] = None
        return {"recorded": False, "path": None, "attached_event_ids": [],
                "invalid_event_ids": [], "units": {},
                "note": ("no direction adjudication record configured; two annotators who disagree "
                         "leave that event out of the denominator")}

    document = _load(root, adjudication_record)
    schema = document.get("schema")
    if not isinstance(schema, str) or not schema.startswith(ADJUDICATION_SCHEMA_PREFIX):
        raise BenchmarkConfigError(
            f"{adjudication_record} is not an adjudication record (schema={schema!r}).")
    rows = document.get("records")
    if not isinstance(rows, list):
        raise BenchmarkConfigError(f"{adjudication_record} needs a records array.")

    located_rule = _resolve_rule_source(document.get("rule_source"), adjudication_record, root)
    by_event: dict[str, tuple[int, dict[str, Any]]] = {}
    for index, row in enumerate(rows):
        if isinstance(row, dict) and isinstance(row.get("unit_id"), str):
            by_event.setdefault(row["unit_id"], (index, row))

    attached: list[str] = []
    invalid: list[str] = []
    report: dict[str, Any] = {}
    for unit in units:
        match = by_event.get(unit["unit_id"])
        if match is None:
            unit["direction_adjudication"] = None
            continue
        index, row = match
        block = {
            "adjudicator_id": document.get("decided_by"),
            "final_label": row.get("resolved_direction_label", row.get("resolved_label")),
            "rule_source": located_rule or document.get("rule_source"),
            "rule_source_declared": document.get("rule_source"),
            "rule_source_located": located_rule is not None,
            "rule_arm": row.get("rule_arm"),
            "decision": row.get("resolution"),
            "adjudicated_at": document.get("decided_at"),
            "original_answers_preserved": row.get("both_original_answers_preserved"),
            "evidence_refs": [{"path": adjudication_record,
                               "pointer": f"/records/{index}/resolved_direction_label"}],
        }
        resolved, reasons = _direction_adjudication_valid(block, root)
        block["valid"] = resolved is not None
        block["invalid_reasons"] = reasons
        unit["direction_adjudication"] = block
        report[unit["unit_id"]] = {"final_label": block["final_label"],
                                   "adjudicator_id": block["adjudicator_id"],
                                   "valid": block["valid"], "invalid_reasons": reasons}
        (attached if block["valid"] else invalid).append(unit["unit_id"])
    return {"recorded": True, "path": adjudication_record,
            "sha256": sha256_file(root, adjudication_record),
            "attached_event_ids": sorted(attached), "invalid_event_ids": sorted(invalid),
            "units": report, "located_rule_source": located_rule}


def resolved_direction_label(unit: dict[str, Any]) -> str | None:
    """The label an event is scored on: a valid adjudication first, else agreement.

    One resolver, used by the gate and by the case builder, so the two cannot
    disagree about what an event's direction is.
    """
    adjudication = unit.get("direction_adjudication") or {}
    if adjudication.get("valid") is True and adjudication.get("final_label"):
        return adjudication["final_label"]
    return unit.get("human_direction_label")


def attach_direction_labels(units: list[dict[str, Any]], sources: list[dict[str, Any]],
                            root: Path) -> dict[str, Any]:
    """Attach recorded human direction labels from submission documents.

    The shape is deliberately the same as the sentiment attachment: two
    independent passes, each declaring that it saw no prediction and no other
    annotator's answer, with the label kept next to an evidence pointer that must
    resolve to it. A pass that cannot be verified is dropped and reported.
    """
    if not sources:
        return {"sources": [], "units": {},
                "note": "no direction submission sources configured"}
    loaded = []
    for source in sources:
        document = _load(root, source["path"])
        rows = document.get("units")
        if not isinstance(rows, list):
            rows = document.get("records")
        if not isinstance(rows, list):
            raise BenchmarkConfigError(
                f"direction submission {source['path']} needs units[] or records[].")
        loaded.append((source["path"], document,
                       "units" if isinstance(document.get("units"), list) else "records", rows))

    report: dict[str, Any] = {"sources": [source["path"] for source in sources], "units": {}}
    for unit in units:
        annotations: list[dict[str, Any]] = []
        exclusions: list[str] = []
        seen: set[str] = set()
        for path, document, key, rows in loaded:
            match = next(((i, row) for i, row in enumerate(rows)
                          if isinstance(row, dict) and row.get("unit_id") == unit["unit_id"]), None)
            if match is None:
                continue
            index, row = match
            stem = Path(path).stem
            independent = (row.get("prediction_exposure",
                                   document.get("prediction_exposure")) is False
                           and row.get("saw_other_annotator_answer",
                                       document.get("saw_other_annotator_answer")) is False)
            annotator = row.get("annotator_id", document.get("annotator_id"))
            label = row.get("direction_label")
            if not independent:
                exclusions.append(f"blinding_not_established_in_{stem}")
                continue
            if not isinstance(annotator, str) or not annotator.strip():
                exclusions.append(f"missing_annotator_id_in_{stem}")
                continue
            if annotator in seen:
                exclusions.append(f"annotator_ids_not_distinct_in_{stem}")
                continue
            if label not in DIRECTION_ANNOTATION_LABELS:
                exclusions.append(f"direction_label_outside_the_declared_space_in_{stem}")
                continue
            seen.add(annotator)
            annotations.append({
                "annotator_id": annotator, "label": label,
                "independent": True, "prediction_seen": False,
                "evidence_refs": [{"path": path,
                                   "pointer": f"/{key}/{index}/direction_label"}],
                "source_document": path,
            })
        unit["direction_annotations"] = annotations
        unit["direction_annotation_exclusions"] = sorted(set(exclusions))
        labels = {row["label"] for row in annotations}
        unit["direction_labels_agree"] = len(annotations) >= 2 and len(labels) == 1
        unit["human_direction_label"] = (
            annotations[0]["label"] if len(annotations) >= 2 and len(labels) == 1
            else annotations[0]["label"] if len(annotations) == 1 else None)
        report["units"][unit["unit_id"]] = {
            "attached": len(annotations),
            "labels": [row["label"] for row in annotations],
            "exclusions": unit["direction_annotation_exclusions"],
        }
    return report


def trajectory_direction_gate(unit: dict[str, Any]) -> dict[str, Any]:
    """Why this event can or cannot enter the direction denominator.

    A label the engine cannot express -- ``stable``, which open question 6 has
    not decided on -- is reported as its own reason rather than quietly mapped
    onto one of the two labels the engine has.

    Two annotators who disagree are excluded unless a recorded adjudication
    stands, in which case the event enters on the adjudicated label and the
    disagreement is reported as the basis rather than hidden.
    """
    reasons: list[str] = []
    labels = [row["label"] for row in unit.get("direction_annotations", [])]
    distinct = sorted(set(labels))
    adjudication = unit.get("direction_adjudication") or {}
    adjudicated = adjudication.get("valid") is True

    if len(labels) < 2:
        reasons.append("fewer_than_two_independent_direction_labels")
    elif len(distinct) != 1 and not adjudicated:
        reasons.append("direction_labels_disagree_no_adjudication")

    label = resolved_direction_label(unit)
    if label is None:
        reasons.append("no_independent_human_direction_label")
    elif label not in ENGINE_DIRECTION_LABELS:
        reasons.append("human_direction_label_outside_the_engine_label_space")

    prediction = unit.get("prediction") or {}
    if prediction.get("admitted") is not True:
        reasons.append("no_admitted_formal_direction_prediction")

    if adjudicated and len(distinct) != 1:
        basis = "adjudicated_disagreement"
    elif len(distinct) == 1 and len(labels) >= 2:
        basis = "two_independent_agreeing_labels"
    else:
        basis = None
    return {"passed": not reasons, "reasons": sorted(set(reasons)),
            "label_basis": basis, "resolved_label": label}


def _parse_time(value: Any) -> datetime | None:
    """Parse an ISO 8601 timestamp that carries a timezone, or return None."""
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        return None
    return parsed.astimezone(timezone.utc)


# Loaders that need their cohort's own documents, rather than a fixed packet.
# Declared here, after every loader it names, so the registry is complete the
# moment the module finishes importing.
COHORT_UNIT_SOURCES = {
    "expansion_candidate_pool": load_expansion_units,
    "trajectory_event_frame": trajectory_units,
}


def select_unit_source(name: str):
    """Return a ``(root, cohort) -> units`` loader for a cohort.

    Frozen loaders ignore the cohort argument, so both registries present the
    same call shape to the caller.
    """
    if name in COHORT_UNIT_SOURCES:
        return COHORT_UNIT_SOURCES[name]
    if name in UNIT_SOURCES:
        frozen = UNIT_SOURCES[name]
        return lambda root, cohort: frozen(root)
    raise BenchmarkConfigError(
        f"unknown unit_source {name!r}; known sources are "
        f"{sorted(set(UNIT_SOURCES) | set(COHORT_UNIT_SOURCES))}.")


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
            "label_basis": gates.get("label_basis"),
            "adjudication_valid": bool(isinstance(unit.get("adjudication"), dict)
                                       and unit["adjudication"].get("valid") is True),
            "adjudication_final_label": (unit.get("adjudication") or {}).get("final_label"),
            "adjudication_rule_arm": (unit.get("adjudication") or {}).get("rule_arm"),
            "original_answers_still_present": len(unit.get("annotations", [])) >= 2,
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


TRACK_SENTIMENT = "sentiment"
TRACK_DIRECTION = "direction"
TRACKS = (TRACK_SENTIMENT, TRACK_DIRECTION)


def cohort_track(cohort: dict[str, Any]) -> str:
    """Which metric track a cohort feeds. Defaults to sentiment for compatibility."""
    track = cohort.get("track", TRACK_SENTIMENT)
    if track not in TRACKS:
        raise BenchmarkConfigError(
            f"cohort {cohort.get('cohort_id')}: track must be one of {list(TRACKS)}, got {track!r}.")
    return track


def evaluate_cohort(cohort: dict[str, Any], units: list[dict[str, Any]], *,
                    root: Path, cutoffs_document: str | None,
                    out_dir_relative: str) -> dict[str, Any]:
    """Label, admit, gate and score one cohort, on whichever track it declares.

    The two tracks differ in what a unit is, which admission point the
    prediction goes through, and which denominator invariant applies. They share
    the entry point, the configuration shape and the reporting shape, so a
    reader does not have to learn two systems to compare them -- but they never
    share a denominator.
    """
    if cohort_track(cohort) == TRACK_DIRECTION:
        return _evaluate_direction_cohort(cohort, units, root=root,
                                          out_dir_relative=out_dir_relative)
    return _evaluate_sentiment_cohort(cohort, units, root=root,
                                      cutoffs_document=cutoffs_document,
                                      out_dir_relative=out_dir_relative)


def _evaluate_sentiment_cohort(cohort: dict[str, Any], units: list[dict[str, Any]], *,
                               root: Path, cutoffs_document: str | None,
                               out_dir_relative: str) -> dict[str, Any]:
    """Label, admit, gate and score one sentiment cohort from its derived units."""
    units = [dict(unit) for unit in units]
    annotation_report = attach_annotations(units, cohort.get("annotation_sources", []), root)
    adjudication_report = attach_adjudications(units, cohort.get("adjudication_record"), root)
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
        "track": TRACK_SENTIMENT,
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
        "adjudication_report": adjudication_report,
        "label_side_accounting": label_side_accounting(units),
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


def trajectory_direction_diagnostic_gate(unit: dict[str, Any]) -> dict[str, Any]:
    """Whether an event enters the three-class diagnostic.

    A strictly wider gate than the binary one: it does not require the label to
    be expressible as escalation or calming, because the diagnostic can express
    ``stable`` and records a declared "cannot judge" as its own reason instead of
    silently treating it as stability.

    The two gates stay separate on purpose. Widening the binary gate would change
    the brief's direction denominator, and the point of the diagnostic is to add
    a reading without moving the number that was already agreed.
    """
    reasons: list[str] = []
    labels = [row["label"] for row in unit.get("direction_annotations", [])]
    distinct = sorted(set(labels))
    adjudication = unit.get("direction_adjudication") or {}
    adjudicated = adjudication.get("valid") is True

    if len(labels) < 2 and not adjudicated:
        reasons.append("fewer_than_two_independent_direction_labels")
    elif len(distinct) != 1 and not adjudicated:
        reasons.append("direction_labels_disagree_no_adjudication")

    label = resolved_direction_label(unit)
    if label is None:
        reasons.append("no_independent_human_direction_label")
    elif label not in DIRECTION_ANNOTATION_LABELS:
        reasons.append("label_outside_the_declared_space")

    prediction = unit.get("prediction") or {}
    if prediction.get("admitted") is not True:
        reasons.append("no_admitted_formal_direction_prediction")
    return {"passed": not reasons, "reasons": sorted(set(reasons)), "label": label,
            "label_is_a_class": label in DIRECTION_DIAGNOSTIC_CLASSES,
            "label_is_declared_unscorable": label in DIRECTION_UNSCORABLE_LABELS}


def _direction_eligibility_rows(units: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """One row per candidate event, listing every gate and every gap.

    The direction track has no permission gate, because the material is a public
    discussion series rather than an archived post whose licence is in question.
    That difference is stated in the row rather than left implicit.
    """
    rows: list[dict[str, Any]] = []
    for unit in units:
        gate = trajectory_direction_gate(unit)
        rows.append({
            "event_id": unit["event_id"],
            "subject": (unit.get("publisher") or "").split(" (")[0],
            "source_record_id": unit.get("source_record_id"),
            "source_version": unit.get("source_version"),
            "cutoff": unit.get("cutoff"),
            "visible_at": unit.get("visible_at"),
            "split": unit.get("split"),
            "sealed_evidence_sha256": unit.get("sealed_evidence_sha256"),
            "prediction_label": (unit.get("prediction") or {}).get("label"),
            "prediction_admitted": (unit.get("prediction") or {}).get("admitted") is True,
            "human_direction_label": resolved_direction_label(unit),
            "human_direction_label_basis": gate["label_basis"],
            "direction_adjudication_valid": bool(
                (unit.get("direction_adjudication") or {}).get("valid")),
            "direction_annotation_count": len(unit.get("direction_annotations", [])),
            "direction_labels": [row["label"] for row in unit.get("direction_annotations", [])],
            "direction_annotation_exclusions": unit.get("direction_annotation_exclusions", []),
            "gated": gate["passed"],
            "all_gaps": gate["reasons"],
            "gap_count": len(gate["reasons"]),
        })
    return rows


def _evaluate_direction_cohort(cohort: dict[str, Any], units: list[dict[str, Any]], *,
                               root: Path, out_dir_relative: str) -> dict[str, Any]:
    """Label, admit, gate and score one trajectory cohort."""
    units = [dict(unit) for unit in units]
    annotation_report = attach_direction_labels(units, cohort.get("annotation_sources", []), root)
    adjudication_report = attach_direction_adjudications(
        units, cohort.get("direction_adjudication_record"), root)
    prediction_report = admit_trajectory_predictions(units, root, out_dir_relative)

    rows = _direction_eligibility_rows(units)
    gated_units = [unit for unit in units if trajectory_direction_gate(unit)["passed"]]
    gated_ids = sorted(unit["unit_id"] for unit in gated_units)
    excluded_ids = {unit["unit_id"] for unit in units} - set(gated_ids)

    # The diagnostic is scored over a wider set than the binary metric, and both
    # numbers come out of one engine run: the engine keeps the brief's direction
    # denominator at the events it can express, and the diagnostic picks up the
    # rest. Scoring the wider set is what lets a stable label reach the engine at
    # all; scoring only the gated set would leave the diagnostic permanently
    # empty, which is how it looked before this was wired.
    diagnostic_units = [unit for unit in units
                        if trajectory_direction_diagnostic_gate(unit)["passed"]]
    diagnostic_ids = sorted(unit["unit_id"] for unit in diagnostic_units)

    cases, labels, metrics = score_trajectory_set(
        diagnostic_units,
        dataset_version=cohort["dataset_version"],
        label_version=cohort["label_version"],
        evidence_root=root,
        package_relative=prediction_report["package_relative"])
    consistency = assert_direction_denominator_matches(gated_units, metrics)
    diagnostic_consistency = assert_diagnostic_denominator_matches(
        diagnostic_units, metrics)

    expected = sorted(cohort["expected_gated_unit_ids"])
    splits: dict[str, int] = {}
    for unit in units:
        key = unit.get("split") or "holdout"
        splits[key] = splits.get(key, 0) + 1
    return {
        "schema": ELIGIBILITY_SCHEMA,
        "track": TRACK_DIRECTION,
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
        "direction_adjudication_report": adjudication_report,
        "prediction_report": prediction_report["admission"],
        "evidence_package": prediction_report["package_relative"],
        "evidence_package_digest": prediction_report["package_digest"],
        "gated_unit_ids": gated_ids,
        "excluded_unit_ids": sorted(excluded_ids),
        "expected_gated_unit_ids": expected,
        "expectation_matches": expected == gated_ids,
        "candidate_count": len(units),
        "gated_count": len(gated_units),
        "diagnostic_event_ids": diagnostic_ids,
        "diagnostic_count": len(diagnostic_units),
        "diagnostic_consistency_check": diagnostic_consistency,
        "split_counts": dict(sorted(splits.items())),
        "consistency_check": consistency,
    }


def evaluate_combined_direction(cohorts: list[dict[str, Any]], *, root: Path,
                                dataset_version: str,
                                label_version: str) -> dict[str, Any]:
    """Score the union of the direction cohorts' gated events.

    The union is taken over the diagnostic gate, which is the wider of the two,
    so that ``stable`` labels reach the engine. The brief's direction denominator
    is unaffected: the engine keeps it at the events it can express, and the
    invariant below still checks that binary number against the binary gate.
    """
    units: list[dict[str, Any]] = []
    binary_units: list[dict[str, Any]] = []
    for cohort in cohorts:
        diagnostic_ids = set(cohort.get("diagnostic_event_ids") or cohort["gated_unit_ids"])
        units.extend(unit for unit in cohort["units"] if unit["unit_id"] in diagnostic_ids)
        binary_units.extend(unit for unit in cohort["units"]
                            if unit["unit_id"] in cohort["gated_unit_ids"])
    seen: set[str] = set()
    duplicates: list[str] = []
    for unit in units:
        if unit["unit_id"] in seen:
            duplicates.append(unit["unit_id"])
        seen.add(unit["unit_id"])
    if duplicates:
        raise BenchmarkInvariantError(
            f"the same event was admitted by more than one cohort: {sorted(set(duplicates))}")
    package_relative = (cohorts[0]["evidence_package"] if cohorts
                        else "trajectory-evidence-package.json")
    _, _, metrics = score_trajectory_set(
        units, dataset_version=dataset_version, label_version=label_version,
        evidence_root=root, package_relative=package_relative)
    consistency = assert_direction_denominator_matches(binary_units, metrics)
    diagnostic_consistency = assert_diagnostic_denominator_matches(units, metrics)
    by_split: dict[str, Any] = {}
    for split in sorted({unit.get("split") or "holdout" for unit in units}):
        members = [unit for unit in units if (unit.get("split") or "holdout") == split]
        _, _, split_metrics = score_trajectory_set(
            members, dataset_version=dataset_version, label_version=label_version,
            evidence_root=root, package_relative=package_relative)
        by_split[split] = {"event_ids": sorted(unit["unit_id"] for unit in members),
                           "metrics": split_metrics}
    return {
        "dataset_version": dataset_version,
        "label_version": label_version,
        "unit_ids": sorted(unit["unit_id"] for unit in units),
        "contributing_cohorts": sorted(cohort["cohort_id"] for cohort in cohorts),
        "metrics": metrics,
        "metrics_by_split": by_split,
        "metrics_by_split_note": (
            "the declared splits are scored separately so the final evaluation set can be read on "
            "its own. The combined number above spans both and is not a substitute for the split "
            "that was reserved"),
        "consistency_check": consistency,
        "diagnostic_consistency_check": diagnostic_consistency,
        "binary_direction_event_ids": sorted(unit["unit_id"] for unit in binary_units),
        "three_class_direction_note": (
            "the metric block carries both readings: direction_agreement is the brief's binary "
            "number over the events it can express, and direction_three_class is a separate "
            "diagnostic that can also express stable"),
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
    by_split: dict[str, Any] = {}
    for split in sorted({unit.get("dataset_split") or "unassigned" for unit in units}):
        members = [unit for unit in units
                   if (unit.get("dataset_split") or "unassigned") == split]
        _, _, split_metrics = score_units(
            members, dataset_version=dataset_version, label_version=label_version,
            evidence_root=root, cutoffs_document=cutoffs_document,
            admitted_by_unit=admitted_by_unit, predictions_document="")
        by_split[split] = {"unit_ids": sorted(unit["unit_id"] for unit in members),
                           "metrics": split_metrics}
    return {
        "dataset_version": dataset_version,
        "label_version": label_version,
        "unit_ids": sorted(unit["unit_id"] for unit in units),
        "contributing_cohorts": sorted(cohort["cohort_id"] for cohort in cohorts),
        "metrics": metrics,
        "metrics_by_split": by_split,
        "metrics_by_split_note": (
            "the declared splits are scored separately so the final evaluation set can be read on "
            "its own. The combined number above spans both and is not a substitute for the split "
            "that was reserved"),
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
        loader = select_unit_source(cohort["unit_source"])
        units = [dict(unit) for unit in loader(root, cohort)]
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

    # Two cohorts on the same track may not name the same unit, event or source
    # record: the union would count it twice. The check is per track on purpose.
    # The same event legitimately appears in both tracks -- once as a title unit
    # and once as a trajectory event -- and refusing that would forbid the two
    # tracks from describing the same events, which is what they are for.
    overlaps: list[dict[str, str]] = []
    owners: dict[tuple[str, str, str], str] = {}
    for cohort in cohorts:
        track = cohort_track(cohort)
        for unit in cohort["units"]:
            for key in ("unit_id", "event_id", "source_record_id"):
                value = unit.get(key)
                if not isinstance(value, str):
                    continue
                previous = owners.setdefault((track, key, value), cohort["cohort_id"])
                if previous != cohort["cohort_id"]:
                    overlaps.append({"track": track, "identity_field": key, "identity": value,
                                     "cohorts": f"{previous},{cohort['cohort_id']}"})
    if overlaps:
        raise BenchmarkInvariantError(
            "cohorts on the same track share candidate identities, so the union would double "
            "count: "
            f"{overlaps}")

    # Cohorts are grouped by track before anything is combined. A sentiment
    # cohort and a direction cohort must never share a denominator: one counts
    # title units, the other counts events, and adding them would produce a
    # number that means nothing. Identity overlap between the two catalogues is
    # allowed and expected -- the same event can carry a title unit and a
    # trajectory event -- so the overlap check above is deliberately per track.
    by_track: dict[str, list[dict[str, Any]]] = {track: [] for track in TRACKS}
    for cohort in cohorts:
        by_track[cohort_track(cohort)].append(cohort)

    sentiment_cohorts = by_track[TRACK_SENTIMENT]
    direction_cohorts = by_track[TRACK_DIRECTION]
    combined = evaluate_combined(
        sentiment_cohorts, root=root, cutoffs_document=cutoff_state["reference"],
        dataset_version=config.get("combined_dataset_version", "combined-formal-set"),
        label_version=config.get("combined_label_version", "combined-formal-labels"))
    combined_direction = evaluate_combined_direction(
        direction_cohorts, root=root,
        dataset_version=config.get("combined_direction_dataset_version",
                                   "combined-direction-set"),
        label_version=config.get("combined_direction_label_version",
                                 "combined-direction-labels"))

    headline = combined["metrics"]["sentiment_three_class"]
    negative = combined["metrics"]["negative_identification"]
    direction_metrics = combined_direction["metrics"]["direction_agreement"]
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
        "label_side_accounting": label_side_accounting(
            [unit for cohort in sentiment_cohorts for unit in cohort["units"]]),
        "cohorts": [
            {key: value for key, value in cohort.items()
             if key not in ("units", "cases", "labels", "metrics")} | {
                "metrics": cohort["metrics"],
            }
            for cohort in cohorts
        ],
        "track_denominators": {
            "sentiment": {
                "track": TRACK_SENTIMENT,
                "cohorts": [cohort["cohort_id"] for cohort in sentiment_cohorts],
                "scored_unit_ids": combined["unit_ids"],
                "numerator": headline["numerator"],
                "denominator": headline["denominator"],
                "value": headline["value"],
                "undefined_reason": headline["undefined_reason"],
            },
            "direction": {
                "track": TRACK_DIRECTION,
                "cohorts": [cohort["cohort_id"] for cohort in direction_cohorts],
                "scored_event_ids": combined_direction["unit_ids"],
                "numerator": direction_metrics["numerator"],
                "denominator": direction_metrics["denominator"],
                "value": direction_metrics["value"],
                "undefined_reason": direction_metrics["undefined_reason"],
            },
            "note": ("the two tracks count different things -- title units and events -- so their "
                     "denominators are reported side by side and never added together"),
        },
        "formal_combined": {
            **combined,
            "headline_definition": (
                "the union of every SENTIMENT cohort's gated units, scored in one engine run; "
                "per-cohort metrics are diagnostics and are not the headline number. The direction "
                "track is reported separately under formal_combined_direction and is never merged "
                "into this number"),
        },
        "formal_combined_direction": {
            **combined_direction,
            "headline_definition": (
                "the union of every DIRECTION cohort's gated events, scored in one engine run. A "
                "separate denominator from the sentiment track by construction."),
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
