"""Strict import and eligibility rules for frozen evaluation predictions.

This module is the single admission point for predictions that may enter a
**formal** metric denominator. It exists because the earlier audit could only
ask "is this row absent from a list of known engineering stand-ins?", which is
not the same question as "is this a traceable, formally produced prediction that
was frozen before the labels were read?".

Admission is decided per prediction row, on evidence that must resolve, not on a
single ``qualification`` string. A row is admitted only when all of the
following hold:

1. the unit identity matches a candidate unit (``unit_id`` and ``event_id``);
2. the task type matches the metric being scored (a sentiment task cannot fill a
   direction slot and vice versa);
3. the execution mode is on an explicit allow-list of formal production modes
   (unknown or missing modes are rejected, never defaulted to formal);
4. the producing method carries a version string;
5. the prediction binds to the exact frozen input package by resolvable
   reference and SHA-256, and the declared cutoff equals the unit's cutoff;
6. the producing run is identified (``run_id``) and carries a resolvable
   production-evidence reference and a resolvable freeze record;
7. the freeze record binds the digest of the prediction document and states
   whether labels or later outcomes were consulted, when it was frozen, and the
   digest of the frozen inputs;
8. the row declares that it did not read labels or later outcomes;
9. the row is unique for its (unit, task): duplicate or conflicting rows are
   rejected rather than reconciled.

Anything that fails is retained in the report with a machine-readable reason.
Nothing that fails is ever substituted by a constant baseline, an AI label, a
synthetic row or an "execution_mode not in blacklist" shortcut.

The module deliberately does not decide *business* quality: it decides
*traceability and isolation*. A formally produced prediction may still be wrong.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

PREDICTION_IMPORT_SCHEMA = "riskshield.data_evaluation.prediction_import.v1"
IMPORT_REPORT_SCHEMA = "riskshield.data_evaluation.prediction_import_report.v1"
FREEZE_RECORD_SCHEMA = "riskshield.data_evaluation.freeze_record.v1"

# Keeps synthetic fixtures and engineering controls structurally separable from
# material that may contribute to a formal denominator.
MATERIAL_CLASS_REAL = "real_frozen_material"
MATERIAL_CLASS_CONTROL = "synthetic_fixture_control"

SENTIMENT_LABELS = ("positive", "neutral", "negative")
DIRECTION_LABELS = ("escalation", "calming")

TASK_SENTIMENT = "sentiment_three_class"
TASK_DIRECTION = "event_direction"

# Execution modes produced by a real model or a real production pipeline. A mode
# absent from this list is rejected: the rules never admit by default.
FORMAL_EXECUTION_MODES = (
    "formal_model_run_v1",
    "sealed_model_run_v1",
)

# Modes that are, by construction, engineering stand-ins. They are listed so the
# report can name exactly why they were rejected.
ENGINEERING_EXECUTION_MODES = (
    "offline_constant_escalation_baseline_v1",
    "offline_constant_escalation_baseline_v13",
    "offline_dynamic_substitute",
    "offline_constant_engineering_control_v1",
    "synthetic_fixture_control_v1",
)

# Fields every admitted row must carry.
_REQUIRED_FIELDS = (
    "unit_id",
    "event_id",
    "task_type",
    "label",
    "execution_mode",
    "method_version",
    "run_id",
    "cutoff",
    "input_binding",
    "production_evidence_ref",
    "freeze_record_ref",
    "labels_accessed",
    "outcomes_accessed",
)


class PredictionImportError(ValueError):
    """Raised when a prediction document cannot be read at all."""


class MaterialRootError(ValueError):
    """Raised when a reference escapes the declared material root."""


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_file(path: Path) -> str:
    return sha256_bytes(Path(path).read_bytes())


def canonical_digest(value: Any) -> str:
    """Digest a JSON value deterministically for run-to-run comparison."""
    payload = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return sha256_bytes(payload.encode("utf-8"))


def _parse_time(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        return None
    return parsed.astimezone(timezone.utc)


def _resolve_ref(ref: Any, root: Path) -> tuple[Any, str | None]:
    """Resolve a ``{"path", "pointer"}`` reference inside ``root``.

    Returns ``(value, None)`` on success and ``(None, reason)`` on failure. A
    reference that points outside the material root, at a missing file, at a
    malformed pointer or at a missing key is a failure, never a warning.
    """
    if not isinstance(ref, dict):
        return None, "reference_not_an_object"
    path = ref.get("path")
    pointer = ref.get("pointer", "")
    if not isinstance(path, str) or not path.strip():
        return None, "reference_missing_path"
    if not isinstance(pointer, str):
        return None, "reference_bad_pointer_type"
    try:
        target = (root / path).resolve()
    except (OSError, ValueError):
        return None, "reference_path_unresolvable"
    try:
        target.relative_to(root.resolve())
    except ValueError:
        return None, "reference_escapes_material_root"
    if not target.is_file():
        return None, "reference_file_missing"
    try:
        document = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return None, "reference_file_unreadable"
    if pointer in ("", "/"):
        return document, None
    if not isinstance(pointer, str) or not pointer.startswith("/"):
        return None, "reference_bad_pointer"
    current = document
    for raw_part in pointer[1:].split("/"):
        part = raw_part.replace("~1", "/").replace("~0", "~")
        if isinstance(current, list):
            try:
                current = current[int(part)]
            except (ValueError, IndexError):
                return None, "reference_pointer_missing"
        elif isinstance(current, dict):
            if part not in current:
                return None, "reference_pointer_missing"
            current = current[part]
        else:
            return None, "reference_pointer_missing"
    return current, None


def _reject(entry: dict[str, Any], reasons: list[str]) -> dict[str, Any]:
    return {
        "unit_id": entry.get("unit_id"),
        "event_id": entry.get("event_id"),
        "task_type": entry.get("task_type"),
        "execution_mode": entry.get("execution_mode"),
        "label": entry.get("label"),
        "run_id": entry.get("run_id"),
        "admitted": False,
        "reasons": sorted(set(reasons)),
        "row_digest": canonical_digest(entry),
    }


def _check_execution_mode(entry: dict[str, Any], reasons: list[str]) -> None:
    mode = entry.get("execution_mode")
    if not isinstance(mode, str) or not mode.strip():
        reasons.append("missing_execution_mode")
    elif mode in ENGINEERING_EXECUTION_MODES:
        reasons.append("rejected_engineering_stand_in_mode")
    elif mode not in FORMAL_EXECUTION_MODES:
        reasons.append("unknown_execution_mode_not_admitted")
    if not isinstance(entry.get("method_version"), str) or not entry["method_version"].strip():
        reasons.append("missing_method_version")
    if not isinstance(entry.get("run_id"), str) or not entry["run_id"].strip():
        reasons.append("missing_run_id")


def _check_label(entry: dict[str, Any], task_type: str, reasons: list[str]) -> None:
    label = entry.get("label")
    if task_type == TASK_SENTIMENT:
        if label not in SENTIMENT_LABELS:
            reasons.append("label_not_in_formal_sentiment_classes")
    elif task_type == TASK_DIRECTION:
        if label not in DIRECTION_LABELS:
            reasons.append("label_not_in_formal_direction_classes")
    else:
        reasons.append("unsupported_task_type")


def _check_input_binding(entry: dict[str, Any], root: Path, unit: dict[str, Any],
                         reasons: list[str]) -> dict[str, Any]:
    """Verify the row binds to the exact frozen input package it claims."""
    binding = entry.get("input_binding")
    if not isinstance(binding, dict):
        reasons.append("missing_input_binding")
        return {}
    package_ref = binding.get("input_package_ref")
    resolved, failure = _resolve_ref(package_ref, root)
    if failure is not None:
        reasons.append(f"input_package_evidence_{failure}")
    claimed_hash = binding.get("input_package_sha256")
    if not isinstance(claimed_hash, str) or len(claimed_hash) != 64:
        reasons.append("missing_input_package_hash")
    elif resolved is not None and canonical_digest(resolved) != claimed_hash:
        reasons.append("input_package_hash_mismatch")

    unit_cutoff = _parse_time(unit.get("cutoff"))
    claimed_cutoff = _parse_time(binding.get("cutoff"))
    if claimed_cutoff is None:
        reasons.append("missing_prediction_cutoff")
    elif unit_cutoff is None or claimed_cutoff != unit_cutoff:
        reasons.append("prediction_cutoff_mismatch")

    if binding.get("cutoff_before_label_read") is not True:
        reasons.append("cutoff_not_declared_before_label_read")

    text_hash = binding.get("text_sha256")
    if not isinstance(text_hash, str) or len(text_hash) != 64:
        reasons.append("missing_prediction_text_hash")
    elif text_hash != unit.get("text_sha256"):
        reasons.append("prediction_text_hash_mismatch")

    source_version = binding.get("source_version")
    if not isinstance(source_version, str) or not source_version.strip():
        reasons.append("missing_prediction_source_version")
    elif source_version != unit.get("source_version"):
        reasons.append("prediction_source_version_mismatch")
    return binding


def _check_production_evidence(entry: dict[str, Any], root: Path,
                               reasons: list[str]) -> None:
    ref = entry.get("production_evidence_ref")
    resolved, failure = _resolve_ref(ref, root)
    if failure is not None:
        reasons.append(f"production_evidence_{failure}")
        return
    if not isinstance(resolved, dict):
        reasons.append("production_evidence_not_an_object")
        return
    if resolved.get("run_id") != entry.get("run_id"):
        reasons.append("production_evidence_run_id_mismatch")
    if resolved.get("execution_mode") != entry.get("execution_mode"):
        reasons.append("production_evidence_execution_mode_mismatch")
    status = resolved.get("run_status")
    if status != "complete":
        reasons.append("production_run_not_complete")


def _check_freeze(entry: dict[str, Any], root: Path, document_digest: str,
                  reasons: list[str]) -> dict[str, Any]:
    """Verify the freeze record exists, binds this document and states isolation."""
    ref = entry.get("freeze_record_ref")
    resolved, failure = _resolve_ref(ref, root)
    if failure is not None:
        reasons.append(f"freeze_record_{failure}")
        return {}
    if not isinstance(resolved, dict):
        reasons.append("freeze_record_not_an_object")
        return {}
    if resolved.get("prediction_document_sha256") != document_digest:
        reasons.append("freeze_record_document_hash_mismatch")
    frozen_at = _parse_time(resolved.get("frozen_at"))
    if frozen_at is None:
        reasons.append("freeze_record_missing_frozen_at")
    if resolved.get("labels_read_before_freeze") is not False:
        reasons.append("freeze_record_labels_read_before_freeze")
    if resolved.get("outcomes_read_before_freeze") is not False:
        reasons.append("freeze_record_outcomes_read_before_freeze")
    if not isinstance(resolved.get("frozen_inputs_sha256"), str):
        reasons.append("freeze_record_missing_frozen_input_hash")
    return resolved


def _check_isolation_declarations(entry: dict[str, Any], reasons: list[str]) -> None:
    if entry.get("labels_accessed") is not False:
        reasons.append("prediction_labels_access_not_declared_false")
    if entry.get("outcomes_accessed") is not False:
        reasons.append("prediction_outcomes_access_not_declared_false")


def load_prediction_document(path: Path) -> dict[str, Any]:
    try:
        document = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise PredictionImportError(f"Cannot read prediction document: {path}") from exc
    if not isinstance(document, dict):
        raise PredictionImportError("Prediction document must be a JSON object.")
    return document


def import_formal_predictions(
    document: dict[str, Any],
    *,
    task_type: str,
    units: list[dict[str, Any]],
    material_root: Path,
) -> dict[str, Any]:
    """Admit or reject each prediction row against the frozen candidate units.

    Returns an import report. ``admitted`` rows are the only rows any caller may
    pass to the metric engine for a formal score; ``rejected`` rows stay in the
    report with their reasons, and ``unmatched`` rows are rows that name no
    candidate unit at all.

    The document digest used to verify freeze records is the canonical digest of
    the in-memory document, so a freeze record can be produced and verified
    without the caller having to serialize the document first.
    """
    root = Path(material_root).resolve()
    if not isinstance(document, dict):
        raise PredictionImportError("Prediction document must be a JSON object.")
    if document.get("schema") != PREDICTION_IMPORT_SCHEMA:
        raise PredictionImportError(
            f"Expected prediction schema {PREDICTION_IMPORT_SCHEMA}.")
    if document.get("task_type") != task_type:
        raise PredictionImportError(
            f"Document task_type {document.get('task_type')!r} does not match {task_type!r}.")
    rows = document.get("predictions")
    if isinstance(rows, dict):
        # Keyed by unit id: the label evidence pointer is then deterministic.
        entries = []
        for key, value in rows.items():
            if not isinstance(value, dict):
                raise PredictionImportError(f"Prediction entry {key!r} must be an object.")
            entry = dict(value)
            entry.setdefault("unit_id", key)
            entries.append(entry)
    elif isinstance(rows, list):
        entries = rows
    else:
        raise PredictionImportError(
            "Prediction document needs a predictions object or array.")

    document_digest = canonical_digest(document)

    units_by_id = {unit["unit_id"]: unit for unit in units
                   if isinstance(unit.get("unit_id"), str)}

    # Pass 1: group rows by (unit, task) so a duplicate is decided before any
    # single row can be admitted. A conflicting or repeated pair is rejected as
    # a pair; there is deliberately no first-wins or best-match selection.
    grouped: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        unit_id = entry.get("unit_id")
        if isinstance(unit_id, str) and unit_id:
            grouped.setdefault((unit_id, task_type), []).append(entry)
    conflicted_keys = {key for key, rows in grouped.items() if len(rows) > 1}
    conflicted_pairs = {
        key for key, rows in grouped.items()
        if len(rows) > 1 and len({(row.get("label"), row.get("run_id")) for row in rows}) > 1
    }

    admitted: list[dict[str, Any]] = []
    rejected: list[dict[str, Any]] = []
    unmatched: list[dict[str, Any]] = []

    for entry in entries:
        if not isinstance(entry, dict):
            rejected.append(_reject({}, ["prediction_row_not_an_object"]))
            continue
        reasons: list[str] = []
        for field in _REQUIRED_FIELDS:
            if field not in entry:
                reasons.append(f"missing_field_{field}")

        unit = units_by_id.get(entry.get("unit_id"))
        if unit is None:
            unmatched.append(_reject(entry, ["unit_id_not_a_candidate_unit"]))
            continue
        if entry.get("event_id") != unit.get("event_id"):
            reasons.append("event_id_does_not_match_unit")

        key = (unit["unit_id"], task_type)
        if key in conflicted_keys:
            reasons.append("duplicate_conflicting_prediction_for_unit"
                           if key in conflicted_pairs
                           else "duplicate_identical_prediction_for_unit")

        _check_execution_mode(entry, reasons)
        _check_label(entry, task_type, reasons)
        binding = _check_input_binding(entry, root, unit, reasons)
        _check_production_evidence(entry, root, reasons)
        freeze_record = _check_freeze(entry, root, document_digest, reasons)
        _check_isolation_declarations(entry, reasons)

        if reasons:
            rejected.append(_reject(entry, reasons))
            continue

        admitted.append({
            "unit_id": unit["unit_id"],
            "event_id": unit["event_id"],
            "task_type": task_type,
            "label": entry["label"],
            "execution_mode": entry["execution_mode"],
            "method_version": entry["method_version"],
            "run_id": entry["run_id"],
            "cutoff": binding.get("cutoff"),
            "input_package_sha256": binding.get("input_package_sha256"),
            "text_sha256": binding.get("text_sha256"),
            "predicted_at": entry.get("predicted_at"),
            "frozen_at": (freeze_record or {}).get("frozen_at"),
            "labels_accessed": entry.get("labels_accessed"),
            "outcomes_accessed": entry.get("outcomes_accessed"),
            "freeze_record_ref": entry.get("freeze_record_ref"),
            "production_evidence_ref": entry.get("production_evidence_ref"),
            "admitted": True,
            "reasons": [],
            "row_digest": canonical_digest(entry),
        })

    return {
        "schema": IMPORT_REPORT_SCHEMA,
        "task_type": task_type,
        "document_digest": document_digest,
        "document_task_type": document.get("task_type"),
        "formal_execution_modes": list(FORMAL_EXECUTION_MODES),
        "admitted": admitted,
        "rejected": rejected,
        "unmatched": unmatched,
        "counts": {
            "rows": len(entries),
            "admitted": len(admitted),
            "rejected": len(rejected),
            "unmatched": len(unmatched),
        },
        "rejection_reasons": _reason_counts(rejected + unmatched),
        "admitted_unit_ids": sorted(row["unit_id"] for row in admitted),
    }


def _reason_counts(rows: list[dict[str, Any]]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for row in rows:
        for reason in row.get("reasons", []):
            counts[reason] = counts.get(reason, 0) + 1
    return dict(sorted(counts.items()))


def import_constant_control(
    units: list[dict[str, Any]],
    *,
    task_type: str,
    label: str,
    predictions_path: str,
    evidence_path: str,
    freeze_path: str,
    material_root: Path,
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    """Build the disclosed non-formal control and its supporting documents.

    The control exists to prove that the gates and the metric engine do count.
    It uses an engineering execution mode on purpose, so
    ``import_formal_predictions`` rejects every one of its rows; callers must
    place its result in a separate control section that never touches a formal
    denominator.

    Every reference in the returned documents resolves inside ``material_root``
    (the callers write them there), so the control exercises the same reference
    resolution path the formal import uses.
    """
    root = Path(material_root).resolve()
    predictions: dict[str, Any] = {}
    evidence: dict[str, Any] = {}
    for unit in units:
        unit_id = unit["unit_id"]
        input_ref = unit["unit_hash_ref"]
        resolved, failure = _resolve_ref(input_ref, root)
        if failure is not None:
            raise PredictionImportError(
                f"control input reference for {unit_id} does not resolve: {failure}")
        predictions[unit_id] = {
            "unit_id": unit_id,
            "event_id": unit.get("event_id"),
            "task_type": task_type,
            "label": label,
            "execution_mode": "synthetic_fixture_control_v1",
            "method_version": "control-constant-v1",
            "run_id": f"control-run-{unit_id}",
            "predicted_at": "2026-10-11T00:00:00Z",
            "cutoff": unit.get("cutoff"),
            "input_binding": {
                "input_package_ref": input_ref,
                "input_package_sha256": canonical_digest(resolved),
                "cutoff": unit.get("cutoff"),
                "cutoff_before_label_read": True,
                "text_sha256": unit.get("text_sha256"),
                "source_version": unit.get("source_version"),
            },
            "production_evidence_ref": {"path": evidence_path,
                                        "pointer": f"/runs/{unit_id}"},
            "freeze_record_ref": {"path": freeze_path, "pointer": ""},
            "labels_accessed": False,
            "outcomes_accessed": False,
            "qualification": "engineering_control_only_not_a_formal_prediction",
        }
        evidence[unit_id] = {
            "run_id": f"control-run-{unit_id}",
            "execution_mode": "synthetic_fixture_control_v1",
            "run_status": "complete",
            "model_calls": 0,
            "network_requests": 0,
            "note": "engineering control; not a model run",
        }
    document = {
        "schema": PREDICTION_IMPORT_SCHEMA,
        "task_type": task_type,
        "document_version": "control-constant-v1",
        "material_class": "synthetic_fixture_control",
        "formal_score_eligible": False,
        "policy": "constant label; no model, no human labels, no future evidence",
        "predictions": predictions,
    }
    evidence_document = {
        "schema": "riskshield.data_evaluation.control_production_evidence.v1",
        "material_class": "synthetic_fixture_control",
        "runs": evidence,
    }
    return document, evidence_document, {
        "schema": "riskshield.data_evaluation.freeze_record.v1",
        "material_class": "synthetic_fixture_control",
        "frozen_at": "2026-10-11T00:00:00Z",
        "frozen_by": "data-evaluation control generator",
        "prediction_document_sha256": None,
        "frozen_inputs_sha256": None,
        "labels_read_before_freeze": False,
        "outcomes_read_before_freeze": False,
        "note": "control record; digests are filled once the documents are written",
    }


def finalize_freeze_record(document: dict[str, Any], freeze: dict[str, Any],
                           *, frozen_inputs: Any) -> dict[str, Any]:
    """Fill the digests a freeze record must bind.

    The digest is computed over the canonical JSON of the prediction document so
    the record cannot be re-pointed at different predictions later.
    """
    payload = json.dumps(document, ensure_ascii=False, sort_keys=True,
                         separators=(",", ":")).encode("utf-8")
    record = dict(freeze)
    record["prediction_document_sha256"] = sha256_bytes(payload)
    record["frozen_inputs_sha256"] = canonical_digest(frozen_inputs)
    return record
