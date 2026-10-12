"""Per-unit pairing audit for the frozen historical event benchmark.

This module answers one question with evidence instead of assertion: for every
candidate evaluation unit in the frozen final-benchmark materials, are all of
the admission conditions simultaneously true, and which ones are missing?

It replaces the earlier version's single six-condition checklist with two
explicit tracks, because sentiment labelling and event-trajectory scoring need
different evidence:

* **sentiment track** — source attribution, event identity and frozen text hash,
  a historical input cutoff that the visible source does not exceed, at least two
  independent human labels that did not see a prediction, and one **admitted**
  prediction from ``evaluation_prediction_import``;
* **direction track** — the same identity/cutoff conditions plus a qualified
  post-cutoff observation window and an independent human direction label. A
  title being scoreable for sentiment does **not** make its event scoreable for
  trajectory, and this module never lets one stand in for the other.

Two invariants are enforced by construction rather than by documentation:

1. **The eligibility table and the scored denominator are the same set.** The
   scored run is built from exactly the units whose ``gates['passed']`` is true,
   and the report asserts the two agree; a unit shown as excluded can never
   contribute to the denominator.
2. **Non-formal material cannot reach a formal number.** Engineering stand-ins,
   AI labels and synthetic fixtures live in separately labelled sections, and
   the headline summary is computed from the formal run only.

The module is read-only with respect to the frozen materials: it hashes them for
provenance and never rewrites them. Labels live only in the label document and
are never copied into case inputs.
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
from riskshield.evaluation_prediction_import import (
    ENGINEERING_EXECUTION_MODES,
    MATERIAL_CLASS_CONTROL,
    MATERIAL_CLASS_REAL,
    TASK_DIRECTION,
    TASK_SENTIMENT,
    canonical_digest,
    finalize_freeze_record,
    import_constant_control,
    import_formal_predictions,
    sha256_file as import_sha256_file,
)

PAIRING_SCHEMA = "riskshield.data_evaluation.pairing_report.v2"

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

# The project applies a fixed 30-minute margin after archive capture. This is a
# derived backtest window rule, NOT proof that a historical cutoff freeze exists.
CUTOFF_MARGIN_SECONDS = 1800

SENTIMENT_LABELS = ("positive", "neutral", "negative")
DIRECTION_LABELS = ("escalation", "calming")

INPUT_SCHEMA = "riskshield.day5.evaluation-inputs.v1"
LABEL_SCHEMA = "riskshield.day5.future-labels.v1"

# Identities the命题 asks to evaluate on. Recorded as unresolved in the frozen
# protocol, so the audit reports both ends of the range rather than one number.
TARGET_EVENT_RANGE = (10, 20)


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


def _unit_hash_ref(*, archive_position: int | None, packet_relative: str,
                   packet_index: int, packet_key: str, use_packet: bool) -> dict[str, Any]:
    """The reference that resolves to this unit's frozen text hash."""
    if not use_packet and archive_position is not None:
        return {"path": ARCHIVE_EVIDENCE_V1,
                "pointer": f"/records/{archive_position}/title_sha256"}
    return {"path": packet_relative, "pointer": f"/units/{packet_index}/{packet_key}"}


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
    cutoff = declared_cutoff
    if cutoff is None and visible_at is not None:
        cutoff = visible_at + timedelta(seconds=CUTOFF_MARGIN_SECONDS)
    # The packet proves a visibility time, not that a historical cutoff freeze
    # ever existed. The report says so explicitly instead of implying otherwise.
    cutoff_provenance = "derived_backtest_window_rule_not_historical_freeze"

    text_sha256 = sha256_text(frozen_text) if isinstance(frozen_text, str) else None
    archive_title_sha256 = archive.get("title_sha256")
    title_hash_match = bool(
        text_sha256
        and archive_title_sha256 == text_sha256
        and reconciliation_unit.get("frozen_text_sha256") == text_sha256
        and reconciliation_unit.get("source_title_sha256_match") is True
    )
    use_packet_hash = not isinstance(archive_title_sha256, str)

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
        if row.get("label") not in SENTIMENT_LABELS:
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
            "source_class": "recorded_human_submission",
        })

    labels = {row["label"] for row in annotations}
    return {
        "unit_id": unit_id,
        "event_id": event_id,
        "unit_kind": "original_title",
        "unit_scope": "exact_frozen_title",
        "source_document": SENTIMENT_PACKET_V6,
        "source_row_key": "units",
        "source_row_index": packet_index,
        "source_record_id": source_record_id,
        "post_id": post_id,
        "publisher": (
            f"Reddit r/{archive['subreddit']} archived post" if archive.get("subreddit")
            else source.get("publisher")),
        "source_version": source.get("source_version"),
        "source_version_pointer": f"/units/{packet_index}/source/source_version",
        "source_url": url,
        "archive_pointer": _archive_pointer(archive_position),
        "attribution_class": (
            "archived_public_post_with_archive_record" if archive_position is not None
            else "dated_public_headline_without_archive_record"),
        "permission_basis": (
            "Public Reddit post archived through the documented Arctic Shift API; "
            "immutable title only, no post body, comment or account data retained."
            if archive_position is not None else
            "Original dated public headline; no archived record in the frozen material."),
        "permission_evidence_ref": None,
        "frozen_text": frozen_text,
        "text_sha256": text_sha256,
        "archive_title_sha256": archive_title_sha256,
        "title_hash_match": title_hash_match,
        "unit_hash_ref": _unit_hash_ref(
            archive_position=archive_position, packet_relative=SENTIMENT_PACKET_V6,
            packet_index=packet_index, packet_key="frozen_text",
            use_packet=use_packet_hash),
        "visible_at": _iso(visible_at),
        "visible_at_pointer": visible_at_pointer,
        "cutoff": _iso(cutoff),
        "cutoff_provenance": cutoff_provenance,
        "cutoff_margin_seconds": (
            int((cutoff - visible_at).total_seconds())
            if cutoff is not None and visible_at is not None else None),
        "source_record_pointer": (
            {"path": ARCHIVE_EVIDENCE_V1, "pointer": f"/records/{archive_position}/source_ref"}
            if archive_position is not None
            else {"path": SENTIMENT_PACKET_V6, "pointer": f"/units/{packet_index}/unit_id"}),
        "annotations": annotations,
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
        units.append(_sentiment_unit(
            packet_unit,
            packet_index=index,
            archive_by_post=archive_by_post,
            reconciliation_unit=reconciliation_units.get(packet_unit.get("unit_id"), {}),
            submissions=submissions,
        ))
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
            "attribution_class": "archived_public_post_with_archive_record",
            "permission_basis": (
                "Public Reddit post archived through the documented Arctic Shift API; "
                "immutable title only, no post body, comment or account data retained."),
            "permission_evidence_ref": None,
            "frozen_text": packet_unit.get("text_to_annotate"),
            "text_sha256": text_sha256,
            "archive_title_sha256": archive.get("title_sha256"),
            "title_hash_match": bool(
                text_sha256
                and archive.get("title_sha256") == text_sha256
                and verified_unit.get("original_title_utf8_sha256") == text_sha256),
            "unit_hash_ref": {"path": ARCHIVE_EVIDENCE_V1,
                              "pointer": f"/records/{archive_position}/title_sha256"}
            if archive_position is not None
            else {"path": V19_PACKET, "pointer": f"/units/{index}/text_sha256"},
            "visible_at": packet_unit.get("archive_captured_at_utc") or archive.get("retrieved_on_utc"),
            "visible_at_pointer": {"path": V19_PACKET,
                                   "pointer": f"/units/{index}/archive_captured_at_utc"},
            "cutoff": packet_unit.get("cutoff_utc"),
            "cutoff_provenance": "declared_in_packet",
            "cutoff_margin_seconds": CUTOFF_MARGIN_SECONDS,
            "source_record_pointer": {"path": V19_PACKET,
                                      "pointer": f"/units/{index}/source_record_id"},
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
            "observation_window": {
                "baseline": event.get("baseline_window_utc"),
                "observation": event.get("observation_window_utc"),
                "window_status": event.get("window_status"),
            },
            "source_access_failures": event.get("source_access_failures", []),
            "recorded_observation_metrics": _observation_metrics(event),
        })
    return rows


def _observation_metrics(event: dict[str, Any]) -> dict[str, Any]:
    """Read whatever observation-window measure the frozen record actually has."""
    same_basis = event.get("same_basis_public_discussion_source")
    if not isinstance(same_basis, dict):
        return {"available": False, "reason": "no_same_basis_source_series_recorded"}
    totals = same_basis.get("window_totals")
    if not isinstance(totals, dict):
        return {"available": False, "reason": "no_window_totals_recorded"}
    cross_checks = event.get("independent_source_cross_checks")
    second_comparable = False
    if isinstance(cross_checks, list):
        for check in cross_checks:
            if (isinstance(check, dict)
                    and "comparable" in str(check.get("role", ""))
                    and "not a comparable" not in str(check.get("role", ""))):
                second_comparable = True
    return {
        "available": True,
        "source_family": same_basis.get("source_family"),
        "baseline": totals.get("baseline"),
        "observation": totals.get("observation"),
        "notable_signal": same_basis.get("notable_signal"),
        "second_comparable_family": second_comparable,
        "comparability": ("single same-basis family only; the other families are explicitly "
                          "recorded as corroborative examples, not comparable series"),
    }


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
    """List every frozen prediction row and why it cannot be formal."""
    pilot = _load(root, PILOT_PREDICTIONS_V8)
    v13 = _load(root, PREDICTIONS_V13)
    rows: list[dict[str, Any]] = []
    for document, payload in ((PILOT_PREDICTIONS_V8, pilot), (PREDICTIONS_V13, v13)):
        for index, row in enumerate(payload.get("predictions", [])):
            execution_mode = row.get("execution_mode")
            rows.append({
                "source_document": document,
                "pointer": f"/predictions/{index}",
                "event_id": row.get("event_id"),
                "split": row.get("split"),
                "direction": row.get("direction"),
                "sentiment": row.get("sentiment"),
                "execution_mode": execution_mode,
                "qualification": row.get("qualification"),
                "declared_model_calls": row.get("model_calls"),
                "task_type_available": (
                    TASK_SENTIMENT if row.get("sentiment") in SENTIMENT_LABELS
                    else (TASK_DIRECTION if row.get("direction") in DIRECTION_LABELS else None)),
                "rejection_reason": _inventory_rejection(execution_mode, row),
            })
    return {
        "rows": rows,
        "formal_eligible_rows": sum(1 for row in rows if row["rejection_reason"] is None),
        "title_level_sentiment_predictions": sum(
            1 for row in rows if row.get("sentiment") in SENTIMENT_LABELS),
        "direction_rows": sum(1 for row in rows if row.get("direction") in DIRECTION_LABELS),
        "note": "listing only; admission is decided by import_formal_predictions on evidence",
    }


def _inventory_rejection(execution_mode: Any, row: dict[str, Any]) -> str | None:
    if not isinstance(execution_mode, str) or not execution_mode.strip():
        return "missing_execution_mode"
    if execution_mode in ENGINEERING_EXECUTION_MODES:
        return "rejected_engineering_stand_in_mode"
    if not isinstance(row.get("run_id"), str):
        return "no_run_id_in_frozen_row"
    if row.get("qualification") == "engineering_baseline_only_not_formal_forecast":
        return "declared_engineering_baseline_only"
    return None


def unit_gates(unit: dict[str, Any], *, require_direction: bool = False) -> dict[str, Any]:
    """Evaluate the admission conditions for one candidate unit.

    ``unit['prediction']`` must already be an admitted sentiment prediction from
    ``evaluation_prediction_import``: this function does not re-implement the
    prediction rules, so the eligibility table and the scored denominator cannot
    drift apart.
    """
    reasons: list[str] = []
    source_record_id = unit.get("source_record_id")
    if not isinstance(source_record_id, str) or not source_record_id.strip():
        reasons.append("missing_source_record_id")
    if not isinstance(unit.get("publisher"), str) or not unit["publisher"].strip():
        reasons.append("missing_publisher_attribution")
    if not isinstance(unit.get("source_version"), str) or not unit["source_version"].strip():
        reasons.append("missing_source_version")
    if unit.get("permission_evidence_ref") is None:
        reasons.append("no_recorded_source_permission_evidence")

    if not unit.get("event_id") or not unit.get("unit_id"):
        reasons.append("missing_event_or_unit_identity")
    if not unit.get("title_hash_match"):
        reasons.append("original_text_hash_missing_or_mismatched")
    if not isinstance(unit.get("archive_title_sha256"), str):
        reasons.append("no_archived_title_hash_evidence")

    visible_at = _parse_time(unit.get("visible_at"))
    if visible_at is None:
        reasons.append("unknown_or_invalid_visible_time")
    cutoff = _parse_time(unit.get("cutoff"))
    if cutoff is None:
        reasons.append("missing_or_invalid_cutoff")
    elif visible_at is not None and visible_at > cutoff:
        reasons.append("source_visible_after_cutoff")

    annotations = unit.get("annotations", [])
    adjudication = unit.get("adjudication")
    has_adjudication = isinstance(adjudication, dict) and adjudication.get("valid") is True
    if len(annotations) < 2:
        reasons.append("fewer_than_two_independent_human_labels")
    elif len({row["annotator_id"] for row in annotations}) != len(annotations):
        reasons.append("annotator_ids_not_distinct")
    elif not unit.get("labels_agree") and not has_adjudication:
        # A recorded adjudication resolves a disagreement; it never replaces the
        # original answers, which stay on the unit and are what the agreement
        # audit reads.
        reasons.append("independent_labels_disagree_no_adjudication")

    prediction = unit.get("prediction")
    if not isinstance(prediction, dict) or prediction.get("admitted") is not True:
        reasons.append("no_admitted_formal_sentiment_prediction")
    elif prediction.get("task_type") != TASK_SENTIMENT:
        reasons.append("admitted_prediction_task_type_mismatch")

    if require_direction:
        if unit.get("frozen_observed_direction") not in DIRECTION_LABELS:
            reasons.append("no_qualified_observed_direction")
        if unit.get("human_direction_label") not in DIRECTION_LABELS:
            reasons.append("no_independent_human_direction_label")
        direction_prediction = unit.get("direction_prediction")
        if not isinstance(direction_prediction, dict) or direction_prediction.get("admitted") is not True:
            reasons.append("no_admitted_formal_direction_prediction")

    return {
        "passed": not reasons,
        "reasons": sorted(set(reasons)),
        "label_basis": (
            "adjudicated_disagreement" if has_adjudication and not unit.get("labels_agree")
            else "unanimous_independent_labels" if unit.get("labels_agree") else None),
        "gates": {
            "source_attribution": not any(
                reason in reasons for reason in (
                    "missing_source_record_id", "missing_publisher_attribution",
                    "missing_source_version")),
            "source_permission_evidence": "no_recorded_source_permission_evidence" not in reasons,
            "event_identity_and_text_hash": not any(
                reason in reasons for reason in (
                    "missing_event_or_unit_identity",
                    "original_text_hash_missing_or_mismatched",
                    "no_archived_title_hash_evidence")),
            "cutoff_after_visible_time": not any(
                reason in reasons for reason in (
                    "unknown_or_invalid_visible_time", "missing_or_invalid_cutoff",
                    "source_visible_after_cutoff")),
            "independent_human_labels": not any(
                reason in reasons for reason in (
                    "fewer_than_two_independent_human_labels", "annotator_ids_not_distinct",
                    "independent_labels_disagree_no_adjudication")),
            "adjudicated_disagreement": bool(has_adjudication and not unit.get("labels_agree")),
            "admitted_formal_prediction": not any(
                reason in reasons for reason in (
                    "no_admitted_formal_sentiment_prediction",
                    "admitted_prediction_task_type_mismatch")),
        },
    }


def unit_gaps(unit: dict[str, Any], *, require_direction: bool = False) -> dict[str, Any]:
    """List every missing piece per unit, so no summary hides a second gap."""
    gates = unit_gates(unit, require_direction=require_direction)
    human = [row for row in unit.get("annotations", [])]
    return {
        "unit_id": unit.get("unit_id"),
        "event_id": unit.get("event_id"),
        "missing": gates["reasons"],
        "sentiment_ready": not gates["reasons"],
        "has_two_independent_human_labels": gates["gates"]["independent_human_labels"],
        "has_source_permission_evidence": gates["gates"]["source_permission_evidence"],
        "has_archived_title_hash": isinstance(unit.get("archive_title_sha256"), str),
        "has_admitted_prediction": gates["gates"]["admitted_formal_prediction"],
        "annotator_count": len({row.get("annotator_id") for row in human}),
        "label_distribution": _label_distribution(human),
    }


def _label_distribution(annotations: list[dict[str, Any]]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for row in annotations:
        label = row.get("label")
        if isinstance(label, str):
            counts[label] = counts.get(label, 0) + 1
    return dict(sorted(counts.items()))


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

    The document states the derivation rule and points back at the frozen visible
    time. It is a *derived backtest window*, not evidence of a historical freeze.
    """
    entries: dict[str, Any] = {}
    for unit in units:
        if unit.get("cutoff_provenance") != "derived_backtest_window_rule_not_historical_freeze":
            continue
        entries[unit["unit_id"]] = {
            "unit_id": unit["unit_id"],
            "event_id": unit["event_id"],
            "cutoff": unit["cutoff"],
            "cutoff_rule": "frozen source visible_at + 30 minutes (project archive margin)",
            "cutoff_provenance": unit["cutoff_provenance"],
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
        "provenance_warning": (
            "derived backtest window rule; this document is generated now and is NOT proof "
            "that a historical input cutoff freeze existed at the time"),
        "source_document": relative_path,
        "units": entries,
    }


def build_input_package(units: list[dict[str, Any]], relative_path: str) -> dict[str, Any]:
    """Generate the frozen input package the predictions must bind to.

    The package records, per unit, the exact cutoff-only inputs a predictor was
    allowed to see. A prediction that does not bind to this package by digest and
    cutoff is rejected by ``import_formal_predictions``.
    """
    entries: dict[str, Any] = {}
    for unit in units:
        entries[unit["unit_id"]] = {
            "unit_id": unit["unit_id"],
            "event_id": unit["event_id"],
            "source_record_id": unit["source_record_id"],
            "source_version": unit["source_version"],
            "text_sha256": unit["text_sha256"],
            "cutoff": unit["cutoff"],
            "visible_at": unit["visible_at"],
            "cutoff_provenance": unit.get("cutoff_provenance"),
            "allowed_inputs": ["frozen_title_text", "source_attribution", "cutoff"],
            "forbidden_inputs": ["labels", "future_outcomes", "post_cutoff_sources"],
            "unit_hash_ref": unit["unit_hash_ref"],
        }
    return {
        "schema": "riskshield.data_evaluation.input_package.v1",
        "material_class": MATERIAL_CLASS_REAL,
        "package_version": "frozen-title-inputs-v1",
        "note": "cutoff-only inputs; label and outcome material is deliberately absent",
        "source_document": relative_path,
        "units": entries,
    }


def bind_input_package(units: list[dict[str, Any]], package: dict[str, Any],
                       package_relative: str) -> None:
    """Point every unit's hash reference at the generated input package entry.

    Prediction rows bind to the package by ``{"path", "pointer": ""}`` so the
    digest covers the whole package; the unit-level references used by the
    metric engine keep resolving through the package for consistency.
    """
    for unit in units:
        unit_id = unit["unit_id"]
        if unit_id in package.get("units", {}):
            unit["input_package_ref"] = {"path": package_relative, "pointer": ""}


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
            else unit["unit_hash_ref"]),
        "sources": [source],
        "predictions": predictions if predictions is not None
        else {"sentiment": None, "direction": None},
    }


def _label_entry(unit: dict[str, Any]) -> dict[str, Any]:
    entry = {
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
    adjudication = unit.get("adjudication")
    if isinstance(adjudication, dict):
        # Emitted alongside the original annotations, never instead of them.
        entry["adjudication"] = {
            "adjudicator_id": adjudication.get("adjudicator_id"),
            "final_label": adjudication.get("final_label"),
            "rule_source": adjudication.get("rule_source"),
            "rule_arm": adjudication.get("rule_arm"),
            "decision": adjudication.get("decision"),
            "adjudicated_at": adjudication.get("adjudicated_at"),
            "original_answers_preserved": adjudication.get("original_answers_preserved"),
            "evidence_refs": adjudication.get("evidence_refs", []),
        }
        entry["label_origin"] = "independent_human_double_annotation_with_recorded_adjudication"
    return entry


def _prediction_payload(unit: dict[str, Any], admitted: dict[str, Any] | None,
                        document_relative: str) -> dict[str, Any]:
    """The metric-keyed prediction object for one unit, or an empty slot."""
    if not isinstance(admitted, dict):
        return {"sentiment": None, "direction": None}
    pointer = f"/predictions/{unit['unit_id']}/label"
    return {
        "sentiment": {
            "label": admitted["label"],
            "execution_mode": admitted["execution_mode"],
            "result_evidence_refs": [{"path": document_relative, "pointer": pointer}],
            "run_id": admitted.get("run_id"),
            "method_version": admitted.get("method_version"),
        },
        "direction": None,
    }


def score_sentiment_set(units: list[dict[str, Any]], *, dataset_version: str,
                        evidence_root: Path, cutoffs_document: str,
                        admitted_by_unit: dict[str, dict[str, Any]],
                        predictions_document: str,
                        existing_direction_cases: list[dict[str, Any]] | None = None,
                        case_builder=None) -> dict[str, Any]:
    """Score exactly ``units`` for sentiment and report the resulting metrics.

    Callers pass the same list they gated, which is what keeps the eligibility
    table and the denominator identical.
    """
    build = case_builder or _case_for_unit
    cases = [
        build(unit,
              predictions=_prediction_payload(unit, admitted_by_unit.get(unit["unit_id"]),
                                              predictions_document),
              dataset_version=dataset_version, split="holdout",
              cutoffs_document=cutoffs_document)
        for unit in units
    ]
    cases.extend(existing_direction_cases or [])
    labels = {
        "schema": LABEL_SCHEMA,
        "label_version": "v24-human-sentiment-double-labels-only",
        "events": [],
        "sentiment_labels": [_label_entry(unit) for unit in units],
    }
    metrics = evaluate_documents(
        {"schema": INPUT_SCHEMA, "dataset_version": dataset_version, "cases": cases},
        labels, evidence_root=evidence_root)
    return {
        "dataset_version": dataset_version,
        "scored_unit_ids": sorted(unit["unit_id"] for unit in units),
        "cases": cases,
        "labels": labels,
        "metrics": metrics,
    }


def timeline_audit(units: list[dict[str, Any]], admission: dict[str, Any],
                   scored_at: str) -> dict[str, Any]:
    """Record the distinct time concepts without inventing any timestamp.

    Six different times matter and are routinely conflated. This audit keeps them
    separate and marks which are *recorded in frozen material* versus *derived by
    this round* versus *not available at all*.
    """
    admitted_by_unit = {row["unit_id"]: row for row in admission.get("admitted", [])}
    entries: list[dict[str, Any]] = []
    for unit in units:
        row = admitted_by_unit.get(unit["unit_id"])
        entries.append({
            "unit_id": unit["unit_id"],
            "historical_material_visible_at": {
                "value": unit.get("visible_at"),
                "origin": "recorded_in_frozen_material",
                "ref": unit.get("visible_at_pointer"),
            },
            "backtest_input_cutoff": {
                "value": unit.get("cutoff"),
                "origin": unit.get("cutoff_provenance"),
                "note": "derived backtest window; not proof of a historical freeze",
            },
            "input_package_frozen_at": {
                "value": None,
                "origin": "not_available",
                "note": "no frozen input-package timestamp exists in the frozen material",
            },
            "prediction_generated_at": {
                "value": (row or {}).get("predicted_at"),
                "origin": "recorded_in_prediction_row" if row else "no_admitted_prediction",
            },
            "prediction_frozen_at": {
                "value": (row or {}).get("frozen_at"),
                "origin": "recorded_in_freeze_record" if row else "no_admitted_prediction",
            },
            "labels_read_by_scoring_at": {
                "value": scored_at,
                "origin": "this_round_scoring_process",
                "note": "labels are read only by this scoring step, after any admitted freeze",
            },
            "labels_accessed_by_predictor_declared": (
                (row or {}).get("labels_accessed") if row else None),
            "outcomes_accessed_by_predictor_declared": (
                (row or {}).get("outcomes_accessed") if row else None),
        })
    return {
        "concepts": [
            "historical_material_visible_at",
            "backtest_input_cutoff",
            "input_package_frozen_at",
            "prediction_generated_at",
            "prediction_frozen_at",
            "labels_read_by_scoring_at",
        ],
        "no_fabricated_timestamps": True,
        "entries": entries,
    }


def event_target_gap(sentiment_units: list[dict[str, Any]],
                     title_units: list[dict[str, Any]],
                     trajectory: list[dict[str, Any]]) -> dict[str, Any]:
    """Inventory what reaching the 10–20 independent real events still needs."""
    reconciliation = None
    all_units = sentiment_units + title_units
    events: dict[str, dict[str, Any]] = {}
    for unit in all_units:
        event_id = unit.get("event_id")
        if not isinstance(event_id, str):
            continue
        entry = events.setdefault(event_id, {
            "event_id": event_id,
            "title_units": [],
            "sentiment_ready_units": 0,
            "human_double_labeled_units": 0,
            "slots_remaining": 0,
        })
        entry["title_units"].append(unit["unit_id"])
        gates = unit_gates(unit)
        if gates["gates"]["independent_human_labels"]:
            entry["human_double_labeled_units"] += 1
        if gates["passed"]:
            entry["sentiment_ready_units"] += 1
        entry["slots_remaining"] += max(
            0, (unit.get("slots_required") or 0) - (unit.get("slots_filled") or 0))
    low, high = TARGET_EVENT_RANGE
    return {
        "target_event_range": list(TARGET_EVENT_RANGE),
        "candidate_event_identities": len(events),
        "sentiment_scoreable_events": sum(
            1 for entry in events.values() if entry["sentiment_ready_units"] > 0),
        "trajectory_qualified_events": sum(
            1 for row in trajectory
            if row.get("frozen_observed_direction") in DIRECTION_LABELS
            and row.get("human_direction_label") in DIRECTION_LABELS),
        "additional_identities_needed_for_minimum": max(0, low - len(events)),
        "additional_identities_needed_for_target": max(0, high - len(events)),
        "human_annotation_slots_remaining": sum(
            entry["slots_remaining"] for entry in events.values()),
        "events": [events[key] for key in sorted(events)],
        "note": ("event identities are not scoreable samples; each additional event needs its own "
                 "source, cutoff, two independent human labels and an admitted prediction"),
    }


def _freeze_record_from_source(root: Path, relative: str) -> dict[str, Any]:
    """Read a *provided* freeze record; never synthesise one for real material."""
    path = (root / relative).resolve()
    if not path.is_file():
        return {"provided": False, "path": relative,
                "reason": "no_freeze_record_provided_for_this_round"}
    return {"provided": True, "path": relative,
            "sha256": import_sha256_file(path), "document": _load(root, relative)}


def build_pairing_report(root: Path, *, out_dir_relative: str,
                         predictions_relative: str | None = None,
                         freeze_relative: str | None = None) -> dict[str, Any]:
    """Assemble the v2 pairing report: gaps, admission, scoring and controls."""
    root = Path(root).resolve()
    sentiment_units = _sentiment_units(root)
    title_units = _title_units(root)
    inventory = prediction_inventory(root)
    trajectory = trajectory_rows(root)
    trajectory_counts = trajectory_qualification(trajectory)

    cutoffs_relative = f"{out_dir_relative}/derived-cutoff-attestation-v2.json"
    package_relative = f"{out_dir_relative}/input-package-v2.json"
    control_relative = f"{out_dir_relative}/control-predictions-v2.json"
    control_evidence_relative = f"{out_dir_relative}/control-production-evidence-v2.json"
    control_freeze_relative = f"{out_dir_relative}/control-freeze-record-v2.json"

    input_package = build_input_package(sentiment_units, package_relative)
    bind_input_package(sentiment_units, input_package, package_relative)
    cutoffs_document = build_cutoff_attestation(sentiment_units, cutoffs_relative)
    # Written now, because both the formal and the control cases resolve their
    # cutoff evidence through these files.
    _write_json(root / package_relative, input_package)
    _write_json(root / cutoffs_relative, cutoffs_document)

    # --- formal admission -------------------------------------------------
    admission: dict[str, Any]
    predictions_document = predictions_relative
    if predictions_relative is None:
        admission = {
            "schema": "riskshield.data_evaluation.prediction_import_report.v1",
            "task_type": TASK_SENTIMENT,
            "document_digest": None,
            "admitted": [], "rejected": [], "unmatched": [],
            "counts": {"rows": 0, "admitted": 0, "rejected": 0, "unmatched": 0},
            "rejection_reasons": {},
            "admitted_unit_ids": [],
            "no_candidate_supplied": True,
            "note": ("no prediction document was supplied; import capability was exercised with "
                     "the synthetic test fixtures only, and no formal prediction may be assumed"),
        }
    else:
        document = _load(root, predictions_relative)
        admission = import_formal_predictions(
            document, task_type=TASK_SENTIMENT, units=sentiment_units,
            material_root=root)

    admitted_by_unit = {row["unit_id"]: row for row in admission["admitted"]}
    for unit in sentiment_units:
        unit["prediction"] = admitted_by_unit.get(unit["unit_id"])
        if unit["prediction"] is not None:
            unit["prediction_source"] = predictions_document

    freeze_proof = (
        _freeze_record_from_source(root, freeze_relative) if freeze_relative is not None
        else {"provided": False, "reason": "no_freeze_record_relative_path_supplied"})

    # --- gates and gaps ---------------------------------------------------
    for unit in sentiment_units:
        unit["gates"] = unit_gates(unit)
        unit["gaps"] = unit_gaps(unit)
    for unit in title_units:
        unit["gates"] = unit_gates(unit)
        unit["gaps"] = unit_gaps(unit)

    qualified_units = [unit for unit in sentiment_units if unit["gates"]["passed"]]

    # --- formal scoring uses exactly the qualified set --------------------
    formal = score_sentiment_set(
        qualified_units, dataset_version="final-benchmark-v24-formal-scoring-v2",
        evidence_root=root, cutoffs_document=cutoffs_relative,
        admitted_by_unit=admitted_by_unit, predictions_document=predictions_document or "")

    # --- disclosed non-formal control ------------------------------------
    # The control documents are written first so the control admission runs the
    # same reference-resolution path the formal import uses. Every one of its
    # rows is expected to be rejected as an engineering stand-in.
    control_document, control_evidence, control_freeze_blank = import_constant_control(
        sentiment_units, task_type=TASK_SENTIMENT, label="neutral",
        predictions_path=control_relative, evidence_path=control_evidence_relative,
        freeze_path=control_freeze_relative, material_root=root)
    _write_json(root / control_evidence_relative, control_evidence)
    control_freeze = _finalize_control_freeze(control_document, control_freeze_blank,
                                              input_package)
    _write_json(root / control_freeze_relative, control_freeze)
    _write_json(root / control_relative, control_document)
    control_admission = import_formal_predictions(
        control_document, task_type=TASK_SENTIMENT, units=sentiment_units,
        material_root=root)

    control_units = list(sentiment_units)
    control_admitted = {
        unit_id: dict(row, admitted=True)
        for unit_id, row in control_document["predictions"].items()
    }
    control_run = score_sentiment_set(
        control_units, dataset_version="final-benchmark-v24-control-v2",
        evidence_root=root, cutoffs_document=cutoffs_relative,
        admitted_by_unit=control_admitted,
        predictions_document=control_relative,
        case_builder=_control_case_for_unit)

    sentiment_metrics = formal["metrics"]["sentiment_three_class"]
    negative = formal["metrics"]["negative_identification"]
    direction_metrics = formal["metrics"]["direction_agreement"]
    control_metrics = control_run["metrics"]["sentiment_three_class"]

    summary = {
        "formal_sentiment_numerator": sentiment_metrics["numerator"],
        "formal_sentiment_denominator": sentiment_metrics["denominator"],
        "formal_sentiment_value": sentiment_metrics["value"],
        "formal_sentiment_undefined_reason": sentiment_metrics["undefined_reason"],
        "formal_negative_counts": negative["counts"],
        "formal_negative_precision": negative["precision"]["value"],
        "formal_negative_precision_denominator": negative["precision"]["denominator"],
        "formal_negative_recall": negative["recall"]["value"],
        "formal_negative_recall_denominator": negative["recall"]["denominator"],
        "formal_negative_binary_accuracy": negative["binary_accuracy"]["value"],
        "formal_direction_numerator": direction_metrics["numerator"],
        "formal_direction_denominator": direction_metrics["denominator"],
        "formal_direction_value": direction_metrics["value"],
        "candidate_sentiment_units": len(sentiment_units) + len(title_units),
        "distinct_candidate_events": len({unit["event_id"] for unit in sentiment_units + title_units}),
        "sentiment_qualified_units": len(qualified_units),
        "sentiment_units_fully_labeled": sum(
            1 for unit in sentiment_units if unit["gates"]["gates"]["independent_human_labels"]),
        "admitted_formal_predictions": admission["counts"]["admitted"],
        "control_numerator": control_metrics["numerator"],
        "control_denominator": control_metrics["denominator"],
        "control_claim_level": "not_formal_control_only",
    }

    report = {
        "schema": PAIRING_SCHEMA,
        "material_root": str(root),
        "material_class": MATERIAL_CLASS_REAL,
        "generated_at": _iso(datetime.now(timezone.utc)),
        "documents": {
            "derived_cutoff_attestation": cutoffs_relative,
            "input_package": package_relative,
            "control_predictions": control_relative,
            "control_production_evidence": control_evidence_relative,
            "control_freeze_record": control_freeze_relative,
            "formal_predictions": predictions_document,
        },
        "freeze_verification": verify_freeze_members(root),
        "material_hashes": {
            relative: sha256_file(root, relative)
            for relative in (SENTIMENT_PACKET_V6, HUMAN_SUBMISSIONS_V16, HUMAN_SUBMISSIONS_V18,
                             V19_PACKET, VERIFIED_TITLES_V19, ARCHIVE_EVIDENCE_V1,
                             FINAL_RECONCILIATION_V24, TRAJECTORY_EVIDENCE_V13,
                             INPUTS_V8, INPUTS_V13, PILOT_PREDICTIONS_V8, PREDICTIONS_V13)
        },
        "audit_level_gaps": {
            "source_permission_evidence": (
                "The frozen materials record attribution (post id, subreddit, archive API, "
                "immutable title) but no per-source usage-scope or license record. Attribution "
                "is not permission evidence, so this stays an open gap for every unit; no "
                "authorisation was inferred from the source name."),
            "unresolved_definitions": [
                "negative_identification_accuracy_formula",
                "minimum_event_count_within_10_to_20",
                "direction_abstention_rule",
            ],
        },
        "sentiment_units": sentiment_units,
        "title_units": title_units,
        "trajectory_events": trajectory,
        "trajectory_counts": trajectory_counts,
        "sentiment_gap_inventory": [
            unit_gaps(unit) for unit in sentiment_units + title_units
        ],
        "trajectory_gap_inventory": _trajectory_gap_inventory(trajectory),
        "event_target_gap": event_target_gap(sentiment_units, title_units, trajectory),
        "prediction_inventory": inventory,
        "prediction_admission": admission,
        "freeze_record": freeze_proof,
        "timeline_audit": timeline_audit(sentiment_units,
                                         admission,
                                         _iso(datetime.now(timezone.utc))),
        "formal_run": formal,
        "control_run": control_run,
        "control_admission": control_admission,
        "control_freeze_record": control_freeze,
        "consistency_check": {
            "gated_unit_ids": sorted(unit["unit_id"] for unit in qualified_units),
            "scored_unit_ids": formal["scored_unit_ids"],
            "agree": sorted(unit["unit_id"] for unit in qualified_units)
            == formal["scored_unit_ids"],
            "excluded_units_present_in_denominator": False,
        },
        "summary": summary,
        "_generated_documents": {
            "cutoff_attestation_document": build_cutoff_attestation(sentiment_units,
                                                                   cutoffs_relative),
            "input_package_document": input_package,
            "control_document": control_document,
            "control_evidence_document": control_evidence,
            "control_freeze_document": control_freeze,
        },
    }
    return report


def _finalize_control_freeze(document: dict[str, Any], freeze: dict[str, Any],
                             frozen_inputs: Any) -> dict[str, Any]:
    return finalize_freeze_record(document, freeze, frozen_inputs=frozen_inputs)


def _write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
                    encoding="utf-8", newline="\n")


def _control_case_for_unit(unit: dict[str, Any], predictions: dict[str, Any] | None,
                           dataset_version: str, split: str,
                           cutoffs_document: str | None) -> dict[str, Any]:
    """Case builder for the disclosed control.

    It uses the same cutoff attestation as the formal run so the control isolates
    one variable (whether a prediction is present). Gates are deliberately not
    consulted here: the control exists to show that the metric engine counts when
    a prediction is present, and it is never reported as a formal result.
    """
    return _case_for_unit(unit, predictions, dataset_version, split, cutoffs_document)


def _trajectory_gap_inventory(trajectory: list[dict[str, Any]]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for row in trajectory:
        missing: list[str] = []
        if row.get("human_direction_label") not in DIRECTION_LABELS:
            missing.append("no_independent_human_direction_label")
        if row.get("frozen_observed_direction") not in DIRECTION_LABELS:
            missing.append("no_evidence_backed_observed_direction")
        metrics = row.get("recorded_observation_metrics") or {}
        if not metrics.get("available"):
            missing.append("no_comparable_observation_series")
        sufficiency = row.get("evidence_sufficiency")
        if isinstance(sufficiency, str) and "not formally trend-eligible" in sufficiency:
            missing.append("frozen_evidence_self_assessed_not_trend_eligible")
        domain = row.get("domain_scope")
        if isinstance(domain, str) and "do not count" in domain:
            missing.append("domain_scope_not_approved_for_insurance_denominator")
        if metrics.get("available") and not metrics.get("second_comparable_family"):
            missing.append("single_source_family_only_no_second_comparable_series")
        if row.get("source_access_failures"):
            missing.append("recorded_source_access_failures")
        rows.append({
            "event_id": row.get("event_id"),
            "split": row.get("split"),
            "missing": missing,
            "observation_metrics": metrics,
            "source_access_failures": row.get("source_access_failures"),
            "annotation_rule_needed": (
                "escalation/calming must be defined on a fixed observation window over a "
                "comparable source family before any label is collected"),
            "disagreement_rule_needed": (
                "two independent labellers plus a recorded adjudication step; abstention kept"),
        })
    return rows


def write_report(root: Path, report: dict[str, Any], out_dir: Path) -> list[Path]:
    out_dir.mkdir(parents=True, exist_ok=True)
    written: list[Path] = []
    documents = report.pop("_generated_documents")
    mapping = {
        "cutoff_attestation_document": "derived-cutoff-attestation-v2.json",
        "input_package_document": "input-package-v2.json",
        "control_document": "control-predictions-v2.json",
        "control_evidence_document": "control-production-evidence-v2.json",
        "control_freeze_document": "control-freeze-record-v2.json",
    }
    for key, filename in mapping.items():
        path = out_dir / filename
        path.write_text(json.dumps(documents[key], ensure_ascii=False, indent=2, sort_keys=True) + "\n",
                        encoding="utf-8", newline="\n")
        written.append(path)
    report_path = out_dir / "pairing-report-v2.json"
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
                           encoding="utf-8", newline="\n")
    written.append(report_path)
    return written


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Build the per-unit pairing and admission report for the frozen benchmark.")
    parser.add_argument("--material-root", required=True, type=Path,
                        help="Repository root holding the git-ignored frozen materials.")
    parser.add_argument("--out-dir", required=True, type=Path,
                        help="Directory for the generated documents and the report.")
    parser.add_argument("--predictions", type=Path, default=None,
                        help="Optional frozen formal prediction document to import.")
    parser.add_argument("--freeze-record", type=Path, default=None,
                        help="Optional freeze record that the prediction document must bind.")
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

    def _relative(path: Path | None) -> str | None:
        if path is None:
            return None
        resolved = path.resolve()
        try:
            return resolved.relative_to(material_root).as_posix()
        except ValueError:
            print(f"--predictions/--freeze-record must live inside --material-root: {path}",
                  file=sys.stderr)
            return None

    predictions_relative = _relative(args.predictions)
    freeze_relative = _relative(args.freeze_record)
    if args.predictions is not None and predictions_relative is None:
        return 2
    if args.freeze_record is not None and freeze_relative is None:
        return 2
    try:
        report = build_pairing_report(
            material_root, out_dir_relative=relative_out,
            predictions_relative=predictions_relative, freeze_relative=freeze_relative)
    except AlignmentInputError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    write_report(material_root, report, out_dir)
    print(json.dumps(report["summary"], ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
