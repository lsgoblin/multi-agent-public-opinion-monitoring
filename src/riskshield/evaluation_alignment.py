"""Per-unit pairing audit for the frozen historical event benchmark.

This module answers one question with evidence instead of assertion: for every
candidate evaluation unit in the frozen final-benchmark materials, are all of
the following simultaneously true?

1. source permission/attribution with a verifiable version and public time;
2. event identity that is stable across the source, label and prediction files;
3. a historical input cutoff that the visible sources do not exceed;
4. evidence-backed follow-up (observation) outcome for trajectory scoring;
5. at least two independent human labels that did not see predictions;
6. a prediction that was frozen before the labels/outcomes were read.

Only units where every gate passes may enter a formal metric denominator. The
module reports the exact numerator, denominator, confusion matrix and exclusion
reason per unit; it never substitutes an AI label, a constant offline baseline
or a synthetic row for a missing human truth or a missing formal prediction.

The module is read-only with respect to the frozen materials: it hashes them for
provenance and never rewrites them. Labels live only in the label document and
are never copied into the case inputs. Outcome evidence is read only for metric
calculation.

Design constraints kept from the frozen v24 protocol:

* the five exact-title sentiment units live outside the v19 14-title packet;
* a human annotation counts only when ``independent is True`` and
  ``prediction_seen is False`` and its evidence reference resolves;
* trajectory scoring is event level, so several sources for one event cannot
  inflate the denominator.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from riskshield.evaluation import evaluate_documents

PAIRING_SCHEMA = "riskshield.data_evaluation.pairing_report.v1"

# Frozen final-benchmark materials. All of these are local, git-ignored evidence.
FREEZE_V24 = "data/evaluation/final-benchmark/manifests/freeze-v24.json"
FINAL_RECONCILIATION_V24 = "data/evaluation/final-benchmark/manifests/final-reconciliation-v24.json"
ARCHIVE_EVIDENCE_V1 = "data/evaluation/final-benchmark/manifests/archive-post-evidence-v1.json"
SENTIMENT_PACKET_V6 = "data/evaluation/final-benchmark/labels/blind-sentiment-packet-v6.json"
HUMAN_SUBMISSIONS_V16 = "data/evaluation/final-benchmark/labels/human-sentiment-submissions-v16.json"
HUMAN_SUBMISSIONS_V18 = "data/evaluation/final-benchmark/labels/human-sentiment-submissions-v18.json"
V19_PACKET = "data/evaluation/final-benchmark/labels/blind-sentiment-reannotation-packet-v19.json"
VERIFIED_TITLES_V19 = "data/evaluation/final-benchmark/labels/verified-original-titles-v19.json"
TRAJECTORY_EVIDENCE_V13 = "data/evaluation/final-benchmark/labels/trajectory-evidence-v13.json"
INPUTS_V8 = "data/evaluation/final-benchmark/inputs/qualified-inputs-v8.json"
INPUTS_V13 = "data/evaluation/final-benchmark/inputs/qualified-inputs-v13.json"
PILOT_PREDICTIONS_V8 = "artifacts/final-integration-review/v8-pilot/pilot-predictions-e-handoff-v8.json"
PREDICTIONS_V13 = "artifacts/final-integration-review/v13-pilot/predictions-v13.json"

# The project applies a fixed 30-minute margin after archive capture.
CUTOFF_MARGIN_SECONDS = 1800

SENTIMENT_LABELS = ("positive", "neutral", "negative")
DIRECTION_LABELS = ("escalation", "calming")

# Engineering stand-ins that may never enter a formal denominator.
NON_FORMAL_EXECUTION_MODES = (
    "offline_constant_escalation_baseline_v1",
    "offline_constant_escalation_baseline_v13",
    "offline_dynamic_substitute",
    "offline_constant_engineering_control_v1",
)

INPUT_SCHEMA = "riskshield.day5.evaluation-inputs.v1"
LABEL_SCHEMA = "riskshield.day5.future-labels.v1"


class AlignmentInputError(ValueError):
    """Raised when a frozen material cannot be safely interpreted."""


def _load(root: Path, relative: str) -> dict[str, Any]:
    path = (root / relative).resolve()
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise AlignmentInputError(f"Cannot read frozen material: {relative}") from exc
    if not isinstance(value, dict):
        raise AlignmentInputError(f"Frozen material must be a JSON object: {relative}")
    return value


def sha256_file(root: Path, relative: str) -> str:
    return hashlib.sha256((root / relative).read_bytes()).hexdigest()


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


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


def _iso(value: datetime | None) -> str | None:
    return None if value is None else value.isoformat().replace("+00:00", "Z")


def verify_freeze_members(root: Path, freeze_relative: str = FREEZE_V24) -> dict[str, Any]:
    """Recompute the SHA-256 of every member declared by a freeze document."""
    freeze = _load(root, freeze_relative)
    members: list[dict[str, Any]] = []
    for entry in freeze.get("files", []):
        relative = entry.get("path")
        expected = entry.get("sha256")
        if not isinstance(relative, str) or not isinstance(expected, str):
            raise AlignmentInputError("Freeze members need path and sha256.")
        target = (root / relative).resolve()
        if not target.is_file():
            members.append({"path": relative, "expected_sha256": expected,
                            "actual_sha256": None, "match": False,
                            "reason": "missing_file"})
            continue
        actual = hashlib.sha256(target.read_bytes()).hexdigest()
        members.append({"path": relative, "expected_sha256": expected,
                        "actual_sha256": actual, "match": actual == expected})
    return {
        "freeze": freeze_relative,
        "freeze_sha256": sha256_file(root, freeze_relative),
        "is_self_hashed": bool(freeze.get("freeze_is_self_hashed")),
        "members": members,
        "all_members_match": bool(members) and all(row["match"] for row in members),
    }


def submission_rows(submissions: dict[str, Any]) -> list[dict[str, Any]]:
    """Return the per-unit rows of one human submission document.

    The frozen corpus records the same content under ``units`` (v16) and
    ``records`` (v18); both are accepted so the audit can compare them.
    """
    rows = submissions.get("units")
    if not isinstance(rows, list):
        rows = submissions.get("records")
    if not isinstance(rows, list):
        raise AlignmentInputError("A human submission document needs units[] or records[].")
    return [row for row in rows if isinstance(row, dict)]


def submission_row_key(submissions: dict[str, Any]) -> str:
    return "units" if isinstance(submissions.get("units"), list) else "records"


def _archive_index(records: list[dict[str, Any]], key: str) -> dict[str, tuple[int, dict[str, Any]]]:
    indexed: dict[str, tuple[int, dict[str, Any]]] = {}
    for position, row in enumerate(records):
        value = row.get(key)
        if isinstance(value, str) and value and value not in indexed:
            indexed[value] = (position, row)
    return indexed


def _archive_pointer(position: int | None) -> str:
    return f"/records/{position}" if position is not None else ""


def _has_usable_label(row: dict[str, Any]) -> bool:
    return row.get("label") in SENTIMENT_LABELS


def _row_is_independent(row: dict[str, Any], document_defaults: dict[str, Any]) -> bool:
    exposure = row.get("prediction_exposure", document_defaults.get("prediction_exposure"))
    other = row.get("saw_other_annotator_answer",
                    document_defaults.get("saw_other_annotator_answer"))
    return exposure is False and other is False


def _annotator_id(row: dict[str, Any], document_defaults: dict[str, Any]) -> Any:
    return row.get("annotator_id", document_defaults.get("annotator_id"))


def _submission_defaults(submissions: dict[str, Any]) -> dict[str, Any]:
    return {
        "annotator_id": submissions.get("annotator_id"),
        "prediction_exposure": submissions.get("prediction_exposure"),
        "saw_other_annotator_answer": submissions.get("saw_other_annotator_answer"),
    }


def _sentiment_unit(
    packet_unit: dict[str, Any],
    *,
    packet_index: int,
    archive_by_post: dict[str, tuple[int, dict[str, Any]]],
    reconciliation_unit: dict[str, Any],
    submissions: list[tuple[str, dict[str, Any]]],
) -> dict[str, Any]:
    unit_id = packet_unit.get("unit_id")
    event_id = packet_unit.get("event_id_reference")
    source = packet_unit.get("source") if isinstance(packet_unit.get("source"), dict) else {}
    frozen_text = packet_unit.get("frozen_text")
    url = source.get("url")
    post_id = None
    if isinstance(url, str) and "/comments/" in url:
        post_id = url.split("/comments/", 1)[1].strip("/").split("/", 1)[0]

    archive_position: int | None = None
    archive: dict[str, Any] = {}
    if post_id is not None and post_id in archive_by_post:
        archive_position, archive = archive_by_post[post_id]
    source_record_id = archive.get("source_ref") or (
        f"reddit_archived_post_{post_id}" if post_id else unit_id)

    # Visibility evidence: the archived post capture time when the unit is a
    # Reddit post, otherwise the headline's own dated page.
    visible_at = _parse_time(source.get("captured_at")) or _parse_time(archive.get("retrieved_on_utc"))
    visible_at_pointer: dict[str, Any] | None = None
    if _parse_time(source.get("captured_at")) is not None:
        visible_at = _parse_time(source.get("captured_at"))
        visible_at_pointer = {"path": SENTIMENT_PACKET_V6,
                              "pointer": f"/units/{packet_index}/source/captured_at"}
    elif _parse_time(source.get("published_at")) is not None:
        visible_at = _parse_time(source.get("published_at"))
        visible_at_pointer = {"path": SENTIMENT_PACKET_V6,
                              "pointer": f"/units/{packet_index}/source/published_at"}
    elif archive_position is not None:
        visible_at = _parse_time(archive.get("retrieved_on_utc"))
        visible_at_pointer = {"path": ARCHIVE_EVIDENCE_V1,
                              "pointer": f"/records/{archive_position}/retrieved_on_utc"}

    declared_cutoff = _parse_time(source.get("cutoff_utc"))
    cutoff_basis = "declared_in_packet_source"
    cutoff = declared_cutoff
    if cutoff is None and visible_at is not None:
        cutoff = visible_at + timedelta(seconds=CUTOFF_MARGIN_SECONDS)
        cutoff_basis = "derived_visible_at_plus_30_minutes"
    if cutoff is None:
        cutoff_basis = "unknown_visible_time"

    text_sha256 = sha256_text(frozen_text) if isinstance(frozen_text, str) else None
    title_hash_match = bool(
        text_sha256
        and archive.get("title_sha256") == text_sha256
        and reconciliation_unit.get("frozen_text_sha256") == text_sha256
        and reconciliation_unit.get("source_title_sha256_match") is True
    )

    annotations: list[dict[str, Any]] = []
    human_exclusions: list[str] = []
    seen_annotators: set[str] = set()
    for document, submissions_doc in submissions:
        key = submission_row_key(submissions_doc)
        defaults = _submission_defaults(submissions_doc)
        row = next((item for item in submission_rows(submissions_doc)
                    if item.get("unit_id") == unit_id), None)
        if row is None:
            human_exclusions.append(f"missing_human_label_in_{Path(document).stem}")
            continue
        if not _row_is_independent(row, defaults):
            human_exclusions.append(f"blinding_not_established_in_{Path(document).stem}")
            continue
        annotator = _annotator_id(row, defaults)
        if not isinstance(annotator, str) or not annotator.strip():
            human_exclusions.append(f"missing_annotator_id_in_{Path(document).stem}")
            continue
        if annotator in seen_annotators:
            human_exclusions.append(f"annotator_ids_not_distinct_in_{Path(document).stem}")
            continue
        if not _has_usable_label(row):
            human_exclusions.append(f"label_outside_formal_three_class_scope_in_{Path(document).stem}")
            continue
        seen_annotators.add(annotator)
        position = submission_rows(submissions_doc).index(row)
        annotations.append({
            "annotator_id": annotator,
            "label": row["label"],
            "independent": True,
            "prediction_seen": False,
            "evidence_refs": [{"path": document, "pointer": f"/{key}/{position}/label"}],
            "source_document": document,
            "pointer": f"/{key}/{position}/label",
            "submitted_at": row.get("submitted_at", submissions_doc.get("submitted_at")),
            "source_version": row.get("source_version") or submissions_doc.get("source_version"),
        })

    labels = {row["label"] for row in annotations}
    return {
        "unit_id": unit_id,
        "event_id": event_id,
        "unit_kind": "original_title",
        "unit_scope": "exact_frozen_title",
        "source_document": SENTIMENT_PACKET_V6,
        "source_row_key": "units",
        "source_row_index": None,
        "source_record_id": source_record_id,
        "post_id": post_id,
        "publisher": (
            f"Reddit r/{archive['subreddit']} archived post" if archive.get("subreddit")
            else source.get("publisher")),
        "source_version": source.get("source_version"),
        "source_version_pointer": f"/units/{packet_index}/source/source_version",
        "source_url": url,
        "archive_pointer": _archive_pointer(archive_position),
        "permission_basis": (
            "Public Reddit post archived through the documented Arctic Shift API; "
            "immutable title only, no post body, comment or account data retained."),
        "frozen_text": frozen_text,
        "text_sha256": text_sha256,
        "archive_title_sha256": archive.get("title_sha256"),
        "title_hash_match": title_hash_match,
        "visible_at": _iso(visible_at),
        "visible_at_pointer": visible_at_pointer,
        "cutoff": _iso(cutoff),
        "cutoff_basis": cutoff_basis,
        "cutoff_margin_seconds": (
            int((cutoff - visible_at).total_seconds())
            if cutoff is not None and visible_at is not None else None),
        "source_record_pointer": (
            {"path": ARCHIVE_EVIDENCE_V1, "pointer": f"/records/{archive_position}/source_ref"}
            if archive_position is not None
            else {"path": SENTIMENT_PACKET_V6,
                  "pointer": f"/units/{packet_index}/unit_id"}),
        "text_hash_pointer": (
            {"path": ARCHIVE_EVIDENCE_V1, "pointer": f"/records/{archive_position}/title_sha256"}
            if archive_position is not None
            else {"path": SENTIMENT_PACKET_V6,
                  "pointer": f"/units/{packet_index}/frozen_text"}),        "annotations": annotations,
        "labels_agree": len(labels) == 1 and len(annotations) >= 2,
        "human_exclusions": sorted(set(human_exclusions)),
        "reconciliation_status": reconciliation_unit.get("status"),
        "prediction": None,
        "prediction_source": None,
    }


def _sentiment_units(root: Path) -> list[dict[str, Any]]:
    packet = _load(root, SENTIMENT_PACKET_V6)
    reconciliation = _load(root, FINAL_RECONCILIATION_V24)
    archive_doc = _load(root, ARCHIVE_EVIDENCE_V1)
    archive_by_post = _archive_index(archive_doc.get("records", []), "post_id")
    reconciliation_units = {
        row.get("unit_id"): row
        for row in reconciliation.get("sentiment_double_labels", {}).get("units", [])
    }
    submissions = [
        (HUMAN_SUBMISSIONS_V16, _load(root, HUMAN_SUBMISSIONS_V16)),
        (HUMAN_SUBMISSIONS_V18, _load(root, HUMAN_SUBMISSIONS_V18)),
    ]
    units: list[dict[str, Any]] = []
    for index, packet_unit in enumerate(packet.get("units", [])):
        unit = _sentiment_unit(
            packet_unit,
            packet_index=index,
            archive_by_post=archive_by_post,
            reconciliation_unit=reconciliation_units.get(packet_unit.get("unit_id"), {}),
            submissions=submissions,
        )
        unit["source_row_index"] = index
        units.append(unit)
    return units


def _title_units(root: Path) -> list[dict[str, Any]]:
    """The v19 14-title packet: identities and cutoffs exist, labels do not."""
    packet = _load(root, V19_PACKET)
    verified = _load(root, VERIFIED_TITLES_V19)
    archive_doc = _load(root, ARCHIVE_EVIDENCE_V1)
    verified_by_unit = {row.get("unit_id"): row for row in verified.get("records", [])}
    archive_by_ref = {
        row.get("source_ref"): (position, row)
        for position, row in enumerate(archive_doc.get("records", []))
    }
    units: list[dict[str, Any]] = []
    for index, packet_unit in enumerate(packet.get("units", [])):
        unit_id = packet_unit.get("unit_id")
        source_record_id = packet_unit.get("source_record_id")
        archive_position, archive = archive_by_ref.get(source_record_id, (None, {}))
        verified_unit = verified_by_unit.get(unit_id, {})
        text_sha256 = packet_unit.get("text_sha256")
        slots = [slot for slot in packet_unit.get("annotator_slots", []) if isinstance(slot, dict)]
        units.append({
            "unit_id": unit_id,
            "event_id": packet_unit.get("event_id_reference"),
            "unit_kind": "original_title",
            "unit_scope": "exact_frozen_title",
            "source_document": V19_PACKET,
            "source_row_key": "units",
            "source_row_index": index,
            "source_record_id": source_record_id,
            "post_id": archive.get("post_id") or verified_unit.get("post_id"),
            "publisher": (
                f"Reddit r/{archive['subreddit']} archived post" if archive.get("subreddit")
                else None),
            "source_version": packet_unit.get("source_version"),
            "source_version_pointer": f"/units/{index}/source_version",
            "source_url": packet_unit.get("source_url"),
            "archive_pointer": _archive_pointer(archive_position),
            "permission_basis": (
                "Public Reddit post archived through the documented Arctic Shift API; "
                "immutable title only, no post body, comment or account data retained."),
            "frozen_text": packet_unit.get("text_to_annotate"),
            "text_sha256": text_sha256,
            "archive_title_sha256": archive.get("title_sha256"),
            "title_hash_match": bool(
                text_sha256
                and archive.get("title_sha256") == text_sha256
                and verified_unit.get("original_title_utf8_sha256") == text_sha256),
            "visible_at": packet_unit.get("archive_captured_at_utc") or archive.get("retrieved_on_utc"),
            "visible_at_pointer": {"path": V19_PACKET,
                                   "pointer": f"/units/{index}/archive_captured_at_utc"},
            "cutoff": packet_unit.get("cutoff_utc"),
            "cutoff_basis": "declared_in_packet",
            "cutoff_margin_seconds": CUTOFF_MARGIN_SECONDS,
            "source_record_pointer": {"path": V19_PACKET, "pointer": f"/units/{index}/source_record_id"},
            "text_hash_pointer": {"path": V19_PACKET, "pointer": f"/units/{index}/text_sha256"},
            "annotations": [],
            "labels_agree": False,
            "human_exclusions": ["fewer_than_two_independent_human_labels"],
            "slots_required": packet_unit.get("required_new_independent_annotations"),
            "slots_filled": len([slot for slot in slots if slot.get("label")]),
            "split": packet_unit.get("split") or verified_unit.get("split"),
            "prediction": None,
            "prediction_source": None,
        })
    return units


def trajectory_rows(root: Path) -> list[dict[str, Any]]:
    """Summarize every event that carries frozen trajectory (follow-up) evidence."""
    trajectory = _load(root, TRAJECTORY_EVIDENCE_V13)
    rows: list[dict[str, Any]] = []
    for event in trajectory.get("events", []):
        label = event.get("human_direction_label")
        rows.append({
            "event_id": event.get("event_id"),
            "split": event.get("split"),
            "domain_scope": event.get("domain_scope"),
            "cutoff": event.get("cutoff"),
            "human_direction_label": label,
            "direction_label_status": event.get("direction_label_status"),
            "evidence_sufficiency": event.get("evidence_sufficiency"),
            "frozen_observed_direction": label if label in DIRECTION_LABELS else None,
        })
    return rows


def trajectory_qualification(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """Derive the trajectory counts from the frozen evidence instead of assuming.

    A trajectory event is formally scoreable only when it has both an
    evidence-backed observed direction and an independent human direction label.
    """
    with_label = [row for row in rows
                  if row.get("human_direction_label") in DIRECTION_LABELS]
    qualified = [row for row in with_label
                 if row.get("frozen_observed_direction") in DIRECTION_LABELS]
    return {
        "events_with_human_direction_label": len(with_label),
        "events_with_qualified_observed_direction": len(qualified),
        "qualified_event_ids": sorted(row["event_id"] for row in qualified),
        "evidence_qualified_events": sum(
            1 for row in rows if row.get("frozen_observed_direction") in DIRECTION_LABELS),
    }


def prediction_inventory(root: Path) -> dict[str, Any]:
    """List every frozen prediction and whether it may enter a formal denominator."""
    pilot = _load(root, PILOT_PREDICTIONS_V8)
    v13 = _load(root, PREDICTIONS_V13)
    rows: list[dict[str, Any]] = []
    for index, row in enumerate(pilot.get("predictions", [])):
        execution_mode = row.get("execution_mode")
        rows.append({
            "source_document": PILOT_PREDICTIONS_V8,
            "pointer": f"/predictions/{index}",
            "event_id": row.get("event_id"),
            "split": row.get("split"),
            "direction": row.get("direction"),
            "sentiment": row.get("sentiment"),
            "execution_mode": execution_mode,
            "qualification": row.get("qualification"),
            "declared_model_calls": row.get("model_calls"),
            "formal_eligible": (
                execution_mode not in NON_FORMAL_EXECUTION_MODES
                and row.get("qualification") != "engineering_baseline_only_not_formal_forecast"),
        })
    for index, row in enumerate(v13.get("predictions", [])):
        execution_mode = row.get("execution_mode")
        rows.append({
            "source_document": PREDICTIONS_V13,
            "pointer": f"/predictions/{index}",
            "event_id": row.get("event_id"),
            "split": row.get("split"),
            "direction": row.get("direction"),
            "sentiment": row.get("sentiment"),
            "execution_mode": execution_mode,
            "qualification": row.get("qualification"),
            "declared_model_calls": None,
            "formal_eligible": (
                execution_mode not in NON_FORMAL_EXECUTION_MODES
                and row.get("qualification") != "engineering_baseline_only_not_formal_forecast"),
        })
    return {
        "rows": rows,
        "formal_eligible_rows": sum(1 for row in rows if row["formal_eligible"]),
        "title_level_sentiment_predictions": sum(
            1 for row in rows if row.get("sentiment") in SENTIMENT_LABELS),
        "direction_rows": sum(1 for row in rows if row.get("direction") in DIRECTION_LABELS),
    }


def unit_gates(unit: dict[str, Any]) -> dict[str, Any]:
    """Evaluate the formal conditions for one candidate sentiment unit.

    ``unit['prediction']`` is the unit's own sentiment prediction, not the
    engine's metric-keyed case payload; ``_case_for_unit`` assembles that payload
    separately so the two shapes cannot be confused.
    """
    reasons: list[str] = []
    if not unit.get("source_record_id"):
        reasons.append("missing_source_record")
    if not isinstance(unit.get("source_version"), str) or not unit["source_version"].strip():
        reasons.append("missing_source_version")
    if not isinstance(unit.get("publisher"), str) or not unit["publisher"].strip():
        reasons.append("missing_publisher")
    if not unit.get("title_hash_match"):
        reasons.append("original_text_hash_missing_or_mismatched")
    if not isinstance(unit.get("archive_title_sha256"), str):
        # A unit whose frozen text has no independently archived title hash
        # cannot be re-verified from source, so it stays out of the denominator.
        reasons.append("no_archived_title_hash_evidence")
    visible_at = _parse_time(unit.get("visible_at"))
    if visible_at is None:
        reasons.append("unknown_or_invalid_visible_time")
    cutoff = _parse_time(unit.get("cutoff"))
    if cutoff is None:
        reasons.append("missing_or_invalid_cutoff")
    elif visible_at is not None and visible_at > cutoff:
        reasons.append("source_visible_after_cutoff")
    if not unit.get("event_id"):
        reasons.append("missing_event_id")

    annotations = unit.get("annotations", [])
    if len(annotations) < 2:
        reasons.append("fewer_than_two_independent_human_labels")
    elif len({row["annotator_id"] for row in annotations}) != len(annotations):
        reasons.append("annotator_ids_not_distinct")
    elif not unit.get("labels_agree"):
        reasons.append("independent_labels_disagree_no_adjudication")

    prediction = unit.get("prediction")
    if not isinstance(prediction, dict) or prediction.get("label") not in SENTIMENT_LABELS:
        reasons.append("missing_paired_sentiment_prediction")
    elif prediction.get("execution_mode") in NON_FORMAL_EXECUTION_MODES:
        reasons.append("prediction_is_offline_baseline_not_a_formal_model_output")

    return {
        "passed": not reasons,
        "reasons": sorted(set(reasons)),
        "gates": {
            "source_permission_and_version": not any(
                reason in reasons for reason in (
                    "missing_source_record", "missing_source_version", "missing_publisher")),
            "event_identity_and_text_hash": not any(
                reason in reasons for reason in (
                    "original_text_hash_missing_or_mismatched",
                    "no_archived_title_hash_evidence", "missing_event_id")),
            "cutoff_after_visible_time": not any(
                reason in reasons for reason in (
                    "unknown_or_invalid_visible_time", "missing_or_invalid_cutoff",
                    "source_visible_after_cutoff")),
            "independent_human_labels": not any(
                reason in reasons for reason in (
                    "fewer_than_two_independent_human_labels", "annotator_ids_not_distinct",
                    "independent_labels_disagree_no_adjudication")),
            "paired_formal_prediction": not any(
                reason in reasons for reason in (
                    "missing_paired_sentiment_prediction",
                    "prediction_is_offline_baseline_not_a_formal_model_output")),
        },
    }


def cutoff_evidence_ref(unit: dict[str, Any], cutoffs_document: str | None) -> dict[str, Any]:
    """Where the case cutoff is attested.

    A packet that declares ``cutoff_utc`` attests it directly. When the packet
    only proves the source's public visibility time, the project's fixed
    30-minute margin is applied and the result is recorded in a generated
    attestation document that also points back to the frozen visible time.
    """
    if cutoffs_document is not None:
        return {"path": cutoffs_document, "pointer": f"/units/{unit['unit_id']}/cutoff"}
    return {"path": unit["source_document"], "pointer": unit.get("declared_cutoff_pointer", "")}


def build_cutoff_attestation(units: list[dict[str, Any]], relative_path: str) -> dict[str, Any]:
    """Generate the derived-cutoff attestation consumed by the metric engine.

    The document also re-states the frozen text hash each unit was paired on, so
    a unit whose source packet carries no archived title hash still has a
    verifiable hash reference inside the audit trail.
    """
    entries: dict[str, Any] = {}
    for unit in units:
        if unit.get("cutoff_basis") != "derived_visible_at_plus_30_minutes":
            continue
        entries[unit["unit_id"]] = {
            "unit_id": unit["unit_id"],
            "event_id": unit["event_id"],
            "cutoff": unit["cutoff"],
            "cutoff_rule": "frozen source visible_at + 30 minutes (project archive margin)",
            "cutoff_margin_seconds": unit["cutoff_margin_seconds"],
            "frozen_visible_at": unit["visible_at"],
            "frozen_visible_at_ref": unit["visible_at_pointer"],
            "frozen_source_document": unit["source_document"],
            "frozen_text_sha256": unit["text_sha256"],
            "archived_title_hash_available": isinstance(unit.get("archive_title_sha256"), str),
        }
    return {
        "schema": "riskshield.data_evaluation.derived_cutoff_attestation.v1",
        "rule": "cutoff = frozen source visible_at + 30 minutes; the frozen visible time is unchanged",
        "source_document": relative_path,
        "units": entries,
    }


def _case_for_unit(unit: dict[str, Any], predictions: dict[str, Any] | None,
                   dataset_version: str, split: str,
                   cutoffs_document: str | None) -> dict[str, Any]:
    """Build one engine case from a frozen unit; labels never appear here."""
    visible_ref = unit.get("visible_at_pointer") or unit.get("source_record_pointer")
    source = {
        "record_id": unit["source_record_id"],
        "publisher": unit["publisher"],
        "source_version": unit["source_version"],
        "available_at": unit["visible_at"],
        "source_record_ref": unit["source_record_pointer"],
        "source_version_ref": {"path": unit["source_document"],
                               "pointer": unit["source_version_pointer"]},
        "source_available_at_ref": visible_ref,
        "visibility_evidence_ref": visible_ref,
    }
    return {
        "evaluation_id": f"{unit['source_record_id']}__sentiment",
        "case_id": unit["event_id"],
        "event_id": unit["event_id"],
        "unit_id": unit["unit_id"],
        "unit_kind": "original_title",
        "split": split,
        "data_mode": "real_historical",
        "target_record_id": unit["source_record_id"],
        "source_version": dataset_version,
        "cutoff": unit["cutoff"],
        "cutoff_evidence_ref": cutoff_evidence_ref(unit, cutoffs_document),
        "text_sha256": unit["text_sha256"],
        "title_sha256_ref": (
            {"path": cutoffs_document, "pointer": f"/units/{unit['unit_id']}/frozen_text_sha256"}
            if cutoffs_document is not None
            and not isinstance(unit.get("archive_title_sha256"), str)
            else unit["text_hash_pointer"]),
        "sources": [source],
        "predictions": predictions if predictions is not None
        else {"sentiment": None, "direction": None},
    }


def _label_entry(unit: dict[str, Any]) -> dict[str, Any]:
    return {
        "evaluation_id": f"{unit['source_record_id']}__sentiment",
        "event_id": unit["event_id"],
        "unit_id": unit["unit_id"],
        "source_record_id": unit["source_record_id"],
        "text_sha256": unit["text_sha256"],
        "label_origin": "independent_human_double_annotation",
        "sentiment_annotations": [
            {
                "annotator_id": row["annotator_id"],
                "label": row["label"],
                "independent": True,
                "prediction_seen": False,
                "evidence_refs": row["evidence_refs"],
                "submitted_at": row["submitted_at"],
            }
            for row in unit["annotations"]
        ],
    }


def _evaluate(units: list[dict[str, Any]], *, dataset_version: str,
              predictions_by_unit: dict[str, dict[str, Any]] | None,
              evidence_root: Path, cutoffs_document: str) -> dict[str, Any]:
    """Score one set of units. ``predictions_by_unit`` maps a unit id to the
    metric-keyed prediction object the engine expects, for example
    ``{"sentiment": {...}, "direction": None}``."""
    cases = [
        _case_for_unit(unit, predictions=(predictions_by_unit or {}).get(unit["unit_id"]),
                       dataset_version=dataset_version, split="holdout",
                       cutoffs_document=cutoffs_document)
        for unit in units
    ]
    labels = {
        "schema": LABEL_SCHEMA,
        "label_version": "v24-human-sentiment-double-labels-only",
        "events": [],
        "sentiment_labels": [_label_entry(unit) for unit in units],
    }
    metrics = evaluate_documents(
        {"schema": INPUT_SCHEMA, "dataset_version": dataset_version, "cases": cases},
        labels, evidence_root=evidence_root)
    return {"dataset_version": dataset_version, "cases": cases, "labels": labels,
            "metrics": metrics}


def build_pairing_report(root: Path, control_predictions_relative: str,
                         cutoffs_relative: str) -> dict[str, Any]:
    """Assemble the pairing table plus the formal and control metric runs."""
    root = Path(root).resolve()
    sentiment_units = _sentiment_units(root)
    title_units = _title_units(root)
    inventory = prediction_inventory(root)

    for unit in sentiment_units + title_units:
        unit["gates"] = unit_gates(unit)

    control_predictions = {
        unit["unit_id"]: _control_prediction_refs(control_predictions_relative, unit["unit_id"])
        for unit in sentiment_units
    }
    formal = _evaluate(sentiment_units, dataset_version="final-benchmark-v24-pairing-audit-v1",
                       predictions_by_unit=None, evidence_root=root,
                       cutoffs_document=cutoffs_relative)
    control = _evaluate(
        sentiment_units, dataset_version="final-benchmark-v24-negative-control-v1",
        predictions_by_unit=control_predictions, evidence_root=root,
        cutoffs_document=cutoffs_relative)
    control_labels = control_label_swap(root, sentiment_units, control_predictions_relative,
                                        cutoffs_relative)

    trajectory = trajectory_rows(root)
    trajectory_counts = trajectory_qualification(trajectory)
    sentiment_metrics = formal["metrics"]["sentiment_three_class"]
    return {
        "schema": PAIRING_SCHEMA,
        "material_root": str(root),
        "control_predictions_document": control_predictions_relative,
        "derived_cutoff_document": cutoffs_relative,
        "freeze_verification": verify_freeze_members(root),
        "material_hashes": {
            relative: sha256_file(root, relative)
            for relative in (SENTIMENT_PACKET_V6, HUMAN_SUBMISSIONS_V16, HUMAN_SUBMISSIONS_V18,
                             V19_PACKET, VERIFIED_TITLES_V19, ARCHIVE_EVIDENCE_V1,
                             FINAL_RECONCILIATION_V24, TRAJECTORY_EVIDENCE_V13,
                             INPUTS_V8, INPUTS_V13, PILOT_PREDICTIONS_V8, PREDICTIONS_V13)
        },
        "sentiment_units": sentiment_units,
        "title_units": title_units,
        "trajectory_events": trajectory,
        "trajectory_counts": trajectory_counts,
        "prediction_inventory": inventory,
        "formal_run": formal,
        "control_run": control,
        "control_label_swap": control_labels,
        "summary": {
            "formal_sentiment_denominator": sentiment_metrics["denominator"],
            "formal_sentiment_numerator": sentiment_metrics["numerator"],
            "formal_direction_denominator": formal["metrics"]["direction_agreement"]["denominator"],
            "sentiment_candidate_units": len(sentiment_units) + len(title_units),
            "sentiment_units_fully_paired": sum(
                1 for unit in sentiment_units + title_units if unit["gates"]["passed"]),
            "human_double_labeled_units": sum(
                1 for unit in sentiment_units
                if unit["gates"]["gates"]["independent_human_labels"]),
            "title_level_formal_predictions": inventory["title_level_sentiment_predictions"],
            "formal_eligible_predictions": inventory["formal_eligible_rows"],
            "trajectory_evidence_qualified_events": trajectory_counts[
                "evidence_qualified_events"],
            "independent_human_trajectory_labels": trajectory_counts[
                "events_with_human_direction_label"],
            "control_denominator": control["metrics"]["sentiment_three_class"]["denominator"],
            "control_numerator": control["metrics"]["sentiment_three_class"]["numerator"],
            "control_label_swap_numerator": control_labels["numerator"],
            "control_label_swap_denominator": control_labels["denominator"],
        },
    }


def build_control_predictions(units: list[dict[str, Any]], relative_path: str) -> dict[str, Any]:
    """Disclosed engineering control: a constant offline stand-in prediction.

    The point of the control is to show that the gates and the metric engine do
    count when a prediction is present. The document is written to disk so every
    evidence reference resolves, and every row declares itself a non-formal
    control. It is never a model result and never enters a formal denominator.
    """
    predictions: dict[str, Any] = {}
    for unit in units:
        predictions[unit["unit_id"]] = {
            "unit_id": unit["unit_id"],
            "event_id": unit["event_id"],
            "label": "neutral",
            "execution_mode": "offline_constant_engineering_control_v1",
            "qualification": "engineering_control_only_not_a_formal_prediction",
            "model_calls": 0,
            "network_requests": 0,
        }
    return {
        "schema": "riskshield.data_evaluation.control_predictions.v1",
        "policy": "constant 'neutral' label; no model, no human labels, no future evidence",
        "source_document": relative_path,
        "predictions": predictions,
    }


def _control_prediction_refs(relative_path: str, unit_id: str) -> dict[str, Any]:
    """The metric-keyed prediction object for one control unit."""
    return {
        "sentiment": {
            "label": "neutral",
            "execution_mode": "offline_constant_engineering_control_v1",
            "result_evidence_refs": [{"path": relative_path,
                                      "pointer": f"/predictions/{unit_id}/label"}],
        },
        "direction": None,
    }


def control_label_swap(root: Path, units: list[dict[str, Any]], relative_path: str,
                       cutoffs_relative: str) -> dict[str, Any]:
    """Second half of the negative control: substitute one human label and show
    that the metric engine penalises the disagreement.

    The substituted label exists only inside this control report. It is never
    written back to any frozen material and it can never be reported as a human
    truth or a formal score.
    """
    if not units:
        return {"status": "not_run", "reason": "no candidate units", "numerator": 0,
                "denominator": 0}
    swapped_units = [dict(unit) for unit in units]
    first_unit = swapped_units[0]
    annotations = [dict(row) for row in first_unit["annotations"]]
    if not annotations:
        return {"status": "not_run", "reason": "first unit has no human labels",
                "numerator": 0, "denominator": 0}
    original_label = annotations[0]["label"]
    annotations[0]["label"] = "positive" if original_label != "positive" else "negative"
    first_unit["annotations"] = annotations
    first_unit["labels_agree"] = len({row["label"] for row in annotations}) == 1
    swapped_units[0] = first_unit
    run = _evaluate(swapped_units, dataset_version="final-benchmark-v24-control-label-swap-v1",
                    predictions_by_unit={unit["unit_id"]: _control_prediction_refs(
                        relative_path, unit["unit_id"]) for unit in units},
                    evidence_root=Path(root).resolve(), cutoffs_document=cutoffs_relative)
    metrics = run["metrics"]["sentiment_three_class"]
    return {
        "status": "control_only_not_a_result",
        "substituted_unit_id": first_unit["unit_id"],
        "original_label": original_label,
        "substituted_label": annotations[0]["label"],
        "numerator": metrics["numerator"],
        "denominator": metrics["denominator"],
        "note": "proves the engine counts a disagreement; not human truth and not a score",
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Build the per-unit pairing table for the frozen historical benchmark.")
    parser.add_argument("--material-root", required=True, type=Path,
                        help="Repository root holding the git-ignored frozen materials.")
    parser.add_argument("--out-dir", required=True, type=Path,
                        help="Directory for the control predictions and the pairing report.")
    args = parser.parse_args(argv)
    out_dir = args.out_dir.resolve()
    material_root = args.material_root.resolve()
    out_dir.mkdir(parents=True, exist_ok=True)
    try:
        relative_out = out_dir.relative_to(material_root).as_posix()
    except ValueError:
        print("--out-dir must live inside --material-root so evidence refs resolve.",
              file=sys.stderr)
        return 2
    control_relative = f"{relative_out}/control-predictions-v1.json"
    cutoffs_relative = f"{relative_out}/derived-cutoff-attestation-v1.json"
    try:
        units = _sentiment_units(material_root)
        control_document = build_control_predictions(units, control_relative)
        cutoff_document = build_cutoff_attestation(units, cutoffs_relative)
        report = build_pairing_report(material_root, control_relative, cutoffs_relative)
    except AlignmentInputError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    (out_dir / "control-predictions-v1.json").write_text(
        json.dumps(control_document, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8", newline="\n")
    (out_dir / "derived-cutoff-attestation-v1.json").write_text(
        json.dumps(cutoff_document, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8", newline="\n")
    (out_dir / "pairing-report-v1.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8", newline="\n")
    print(json.dumps(report["summary"], ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
