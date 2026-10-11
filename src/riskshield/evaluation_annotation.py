"""Turning the frozen reannotation packet into human-fillable sheets, and checking
the answers that come back.

This module does **not** produce labels. The frozen protocol only admits a
sentiment annotation when a human declares ``independent is True`` and
``prediction_seen is False``, so an agent-filled label would be a fabricated
human ground truth. What this module does instead:

* render one **blind** review sheet per annotator, containing only the frozen
  text, its hash and the frozen cutoff — no predictions, no AI suggestions, no
  prior answers, no prior rationales;
* render the submission shells that preserve the recorded answer shape
  (``units[]`` for the first pass, ``records[]`` for the second);
* validate a returned submission against the frozen packet: completeness, unit
  identity, original-text hash, label set, annotator distinctness, blinding
  declarations and timezone-aware timestamps.

Blinding is enforced by construction: the review sheet builder only ever reads
the packet's ``text_to_annotate`` / ``text_sha256`` / ``source_version`` /
time fields, and never opens a prediction, suggestion or previous-submission
document.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

PACKET_SCHEMA = "riskshield.final_benchmark.blind_sentiment_reannotation_packet.v19"
SUBMISSION_SCHEMA_FIRST = "riskshield.final_benchmark.human_sentiment_submissions.v16"
SUBMISSION_SCHEMA_SECOND = "riskshield.final_benchmark.human_sentiment_submissions.v18"
VALIDATION_SCHEMA = "riskshield.data_evaluation.annotation_validation.v1"

DEFAULT_MATERIAL = "data/evaluation/final-benchmark/labels/blind-sentiment-reannotation-packet-v19.json"

# Labels that may ever appear in a submission. `uncertain` stays listed because
# the packet's allowed_labels includes it, but it is flagged as pending a
# definition rather than silently accepted.
PROTOCOL_LABELS = ("positive", "neutral", "negative", "uncertain")
FORMAL_LABELS = ("positive", "neutral", "negative")
DEFINED_ONLY_LABELS = ("mixed", "unclear")

SHEET_COLUMNS = (
    "slot_id",
    "unit_id",
    "text_to_annotate",
    "text_sha256",
    "source_version",
    "archive_captured_at_utc",
    "cutoff_utc",
    "label",
    "rationale",
    "confidence",
    "annotator_id",
    "submitted_at",
    "prediction_exposure",
    "saw_other_annotator_answer",
)


class AnnotationInputError(ValueError):
    """Raised when the packet or a submission cannot be interpreted."""


def _read_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise AnnotationInputError(f"Cannot read JSON: {path}") from exc
    if not isinstance(value, dict):
        raise AnnotationInputError(f"Top-level JSON must be an object: {path}")
    return value


def sha256_file(path: Path) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def load_packet(path: Path) -> dict[str, Any]:
    packet = _read_json(path)
    if packet.get("schema") != PACKET_SCHEMA:
        raise AnnotationInputError(f"Expected packet schema {PACKET_SCHEMA}.")
    units = packet.get("units")
    if not isinstance(units, list) or not units:
        raise AnnotationInputError("Packet needs a nonempty units[] array.")
    return packet


def packet_units(packet: dict[str, Any]) -> list[dict[str, Any]]:
    return [unit for unit in packet["units"] if isinstance(unit, dict)]


def total_slots(packet: dict[str, Any]) -> int:
    return sum(len([s for s in unit.get("annotator_slots", []) if isinstance(s, dict)])
               for unit in packet_units(packet))


def slot_ids(packet: dict[str, Any]) -> list[str]:
    ids: list[str] = []
    for unit in packet_units(packet):
        for slot in unit.get("annotator_slots", []):
            if isinstance(slot, dict) and isinstance(slot.get("slot_id"), str):
                ids.append(slot["slot_id"])
    return ids


def packet_label_conflicts(packet: dict[str, Any]) -> dict[str, Any]:
    """Report the recorded allowed-label / definition mismatch instead of guessing."""
    allowed = packet.get("allowed_labels", [])
    defined = sorted((packet.get("label_definitions") or {}).keys())
    return {
        "allowed_labels": allowed,
        "defined_labels": defined,
        "allowed_but_undefined": sorted(set(allowed) - set(defined)),
        "defined_but_not_allowed": sorted(set(defined) - set(allowed)),
        "blocks_formal_scoring": sorted(set(allowed) - set(defined)) != [],
        "status": packet.get("protocol_status"),
    }


def _check_slot_collisions(packet: dict[str, Any]) -> list[str]:
    """Every unit must have at least two distinct slot ids when it needs two labels."""
    problems: list[str] = []
    for unit in packet_units(packet):
        slots = [s for s in unit.get("annotator_slots", []) if isinstance(s, dict)]
        ids = [s.get("slot_id") for s in slots]
        if len(set(ids)) != len(ids):
            problems.append(f"duplicate_slot_id_in_{unit.get('unit_id')}")
        required = unit.get("required_new_independent_annotations")
        if isinstance(required, int) and len(slots) != required:
            problems.append(f"slot_count_mismatch_in_{unit.get('unit_id')}")
    return sorted(problems)


def sheet_rows(packet: dict[str, Any], role: str) -> list[dict[str, str]]:
    """Blind rows for one annotator role.

    ``role`` selects which slot of each unit this person will fill, so two
    annotators can be handed two different files without either seeing which
    slot belongs to the other. Only frozen text and its provenance are exposed.
    """
    if role not in ("A", "B"):
        raise AnnotationInputError("role must be 'A' or 'B'.")
    rows: list[dict[str, str]] = []
    for unit in packet_units(packet):
        slots = [s for s in unit.get("annotator_slots", []) if isinstance(s, dict)]
        if not slots:
            continue
        index = 0 if role == "A" else min(1, len(slots) - 1)
        if role == "B" and len(slots) < 2:
            # A unit that needs only one independent label has no B slot.
            continue
        slot = slots[index]
        rows.append({
            "slot_id": str(slot.get("slot_id", "")),
            "unit_id": str(unit.get("unit_id", "")),
            "text_to_annotate": str(unit.get("text_to_annotate", "")),
            "text_sha256": str(unit.get("text_sha256", "")),
            "source_version": str(unit.get("source_version", "")),
            "archive_captured_at_utc": str(unit.get("archive_captured_at_utc", "")),
            "cutoff_utc": str(unit.get("cutoff_utc", "")),
            "label": "",
            "rationale": "",
            "confidence": "",
            "annotator_id": "",
            "submitted_at": "",
            "prediction_exposure": "",
            "saw_other_annotator_answer": "",
        })
    return rows


def write_review_sheet(packet: dict[str, Any], role: str, path: Path) -> dict[str, Any]:
    rows = sheet_rows(packet, role)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=list(SHEET_COLUMNS))
        writer.writeheader()
        writer.writerows(rows)
    return {
        "path": str(path),
        "role": role,
        "rows": len(rows),
        "columns": list(SHEET_COLUMNS),
        "blank_fields": ["label", "rationale", "confidence", "annotator_id",
                         "submitted_at", "prediction_exposure",
                         "saw_other_annotator_answer"],
        "blindness": ("contains only frozen text and its provenance; no predictions, "
                      "no AI suggestions, no prior answers or rationales"),
        "sha256": sha256_file(path),
    }


def submission_shell(packet: dict[str, Any], role: str, annotator_id: str | None) -> dict[str, Any]:
    """An empty submission document, preserving the recorded answer shapes."""
    first = role == "A"
    entries = []
    for unit in packet_units(packet):
        slots = [s for s in unit.get("annotator_slots", []) if isinstance(s, dict)]
        if not slots:
            continue
        if role == "B" and len(slots) < 2:
            continue
        entries.append({
            "unit_id": unit.get("unit_id"),
            "event_id_reference": unit.get("event_id_reference"),
            "frozen_text": unit.get("text_to_annotate"),
            "text_sha256": unit.get("text_sha256"),
            "label": None,
            "rationale": None,
            "confidence": None,
            "annotator_id": annotator_id,
            "submitted_at": None,
            "source_version": unit.get("source_version"),
            "prediction_exposure": None,
            "saw_other_annotator_answer": None,
        })
    shell: dict[str, Any] = {
        "schema": SUBMISSION_SCHEMA_FIRST if first else SUBMISSION_SCHEMA_SECOND,
        "dataset_version": f"final-benchmark-human-annotations-v19-{role.lower()}",
        "batch_id": packet.get("batch_id", "sentiment-title-reannotation-v19"),
        "submission_scope": (
            f"One blind human annotator pass ({role}) for frozen title units; "
            "two independent passes are required per unit"),
        "annotator_id": annotator_id,
        "submitted_at": None,
        "material_version": f"{DEFAULT_MATERIAL}",
        "prediction_exposure": None,
        "saw_other_annotator_answer": None,
        "role": role,
        "status": "shell_not_submitted",
    }
    shell["units" if first else "records"] = entries
    return shell


def write_submission_shell(packet: dict[str, Any], role: str, path: Path,
                           annotator_id: str | None = None) -> dict[str, Any]:
    shell = submission_shell(packet, role, annotator_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(shell, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
                    encoding="utf-8", newline="\n")
    return {"path": str(path), "role": role, "entries": len(shell.get("units", shell.get("records", []))),
            "sha256": sha256_file(path)}


def _submission_entries(submission: dict[str, Any]) -> tuple[str, list[dict[str, Any]]]:
    for key in ("units", "records"):
        rows = submission.get(key)
        if isinstance(rows, list):
            return key, [row for row in rows if isinstance(row, dict)]
    raise AnnotationInputError("A submission needs units[] or records[].")


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


def _is_filled(value: Any) -> bool:
    return value is not None and (not isinstance(value, str) or value.strip() != "")


def validate_submission(packet: dict[str, Any], submission: dict[str, Any], *,
                        slot_binding: dict[str, str] | None = None) -> dict[str, Any]:
    """Check one returned submission against the frozen packet.

    ``slot_binding`` maps ``unit_id`` to the slot id this submission is claiming,
    which lets the caller pin an answer to a specific slot instead of trusting the
    submitter's own numbering.
    """
    key, entries = _submission_entries(submission)
    units_by_id = {unit.get("unit_id"): unit for unit in packet_units(packet)}
    allowed = set(packet.get("allowed_labels", PROTOCOL_LABELS))
    document_defaults = {
        "annotator_id": submission.get("annotator_id"),
        "prediction_exposure": submission.get("prediction_exposure"),
        "saw_other_annotator_answer": submission.get("saw_other_annotator_answer"),
    }

    accepted: list[dict[str, Any]] = []
    rejected: list[dict[str, Any]] = []
    seen_units: set[str] = set()

    for entry in entries:
        reasons: list[str] = []
        unit_id = entry.get("unit_id")
        unit = units_by_id.get(unit_id)
        if unit is None:
            rejected.append({"unit_id": unit_id, "reasons": ["unit_id_not_in_packet"]})
            continue
        if unit_id in seen_units:
            reasons.append("duplicate_unit_in_submission")
        seen_units.add(unit_id)

        if entry.get("text_sha256") != unit.get("text_sha256"):
            reasons.append("text_sha256_mismatch")
        if _is_filled(entry.get("frozen_text")) and entry.get("frozen_text") != unit.get("text_to_annotate"):
            reasons.append("answer_did_not_use_the_frozen_title")
        if not _is_filled(entry.get("frozen_text")):
            reasons.append("frozen_text_missing")

        label = entry.get("label")
        if not _is_filled(label):
            reasons.append("missing_label")
        elif label in DEFINED_ONLY_LABELS:
            reasons.append(f"label_{label}_is_defined_but_not_allowed")
        elif label not in allowed:
            reasons.append("label_not_in_allowed_set")
        elif label not in FORMAL_LABELS:
            reasons.append("label_pending_protocol_definition")

        if not _is_filled(entry.get("rationale")):
            reasons.append("missing_rationale")
        confidence = entry.get("confidence")
        if not isinstance(confidence, (int, float)) or isinstance(confidence, bool):
            reasons.append("missing_or_non_numeric_confidence")
        elif not 0.0 <= float(confidence) <= 1.0:
            reasons.append("confidence_out_of_range")

        annotator = entry.get("annotator_id", document_defaults["annotator_id"])
        if not isinstance(annotator, str) or not annotator.strip():
            reasons.append("missing_annotator_id")
        if _parse_time(entry.get("submitted_at",
                                submission.get("submitted_at"))) is None:
            reasons.append("missing_or_naive_submitted_at")

        exposure = entry.get("prediction_exposure", document_defaults["prediction_exposure"])
        other = entry.get("saw_other_annotator_answer",
                          document_defaults["saw_other_annotator_answer"])
        if exposure is not False:
            reasons.append("prediction_exposure_not_declared_false")
        if other is not False:
            reasons.append("other_annotator_answer_exposure_not_declared_false")

        slot_id = None
        valid_slots = [s.get("slot_id") for s in unit.get("annotator_slots", [])
                       if isinstance(s, dict)]
        if slot_binding is not None:
            slot_id = slot_binding.get(unit_id)
            if slot_id not in valid_slots:
                reasons.append("slot_binding_not_a_slot_of_this_unit")
        elif valid_slots:
            # Without an explicit binding, record which slot this answer claims by
            # position, so a later merge cannot silently reuse one slot twice.
            claimed = entry.get("slot_id")
            slot_id = claimed if claimed in valid_slots else valid_slots[0]

        if reasons:
            rejected.append({"unit_id": unit_id, "slot_id": slot_id,
                             "reasons": sorted(set(reasons))})
            continue
        accepted.append({
            "unit_id": unit_id,
            "slot_id": slot_id,
            "event_id": unit.get("event_id_reference"),
            "label": label,
            "annotator_id": annotator,
            "confidence": float(confidence),
            "text_sha256": unit.get("text_sha256"),
        })

    expected_units = {unit.get("unit_id") for unit in packet_units(packet)
                      if isinstance(slot_binding, dict) is False}
    missing = sorted(
        unit_id for unit_id in (slot_binding or {}).keys()
        if unit_id not in {row["unit_id"] for row in accepted + rejected})
    if not slot_binding:
        missing = sorted({unit.get("unit_id") for unit in packet_units(packet)}
                         - {row["unit_id"] for row in accepted + rejected})

    annotators = {row["annotator_id"] for row in accepted}
    return {
        "schema": VALIDATION_SCHEMA,
        "submission_key": key,
        "accepted": accepted,
        "rejected": rejected,
        "counts": {"rows": len(entries), "accepted": len(accepted), "rejected": len(rejected)},
        "missing_units": missing,
        "complete": not missing and not rejected,
        "distinct_annotators": sorted(annotators),
        "packet_units": len(expected_units) if expected_units else len(units_by_id),
        "label_distribution": _distribution(accepted),
    }


def _distribution(rows: list[dict[str, Any]]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for row in rows:
        counts[row["label"]] = counts.get(row["label"], 0) + 1
    return dict(sorted(counts.items()))


def cross_check(first: dict[str, Any], second: dict[str, Any]) -> dict[str, Any]:
    """Compare two validated passes: agreement is what admits a unit."""
    by_unit_first = {row["unit_id"]: row for row in first.get("accepted", [])}
    by_unit_second = {row["unit_id"]: row for row in second.get("accepted", [])}
    shared = sorted(set(by_unit_first) & set(by_unit_second))
    agree, disagree = [], []
    for unit_id in shared:
        left, right = by_unit_first[unit_id], by_unit_second[unit_id]
        same_annotator = left["annotator_id"] == right["annotator_id"]
        record = {
            "unit_id": unit_id,
            "first": left["label"],
            "second": right["label"],
            "first_annotator": left["annotator_id"],
            "second_annotator": right["annotator_id"],
            "distinct_annotators": not same_annotator,
        }
        if same_annotator:
            record["reasons"] = ["same_annotator_for_both_passes"]
            disagree.append(record)
        elif left["label"] == right["label"]:
            agree.append(record)
        else:
            record["reasons"] = ["independent_labels_disagree_no_adjudication"]
            disagree.append(record)
    return {
        "units_with_both_passes": len(shared),
        "units_agreeing": len(agree),
        "units_disagreeing_or_invalid": len(disagree),
        "agreement": {"numerator": len(agree), "denominator": len(shared),
                      "value": round(len(agree) / len(shared), 6) if shared else None},
        "agreed_units": agree,
        "excluded_units": disagree,
        "note": ("agreement is not an accuracy metric; it only establishes that a unit has "
                 "two independent labels and may enter a denominator"),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Render blind annotation sheets and validate returned answers.")
    parser.add_argument("--material-root", required=True, type=Path)
    parser.add_argument("--packet", type=Path, default=None,
                        help=f"Packet path, default {DEFAULT_MATERIAL}")
    parser.add_argument("--out-dir", required=False, type=Path, default=None)
    parser.add_argument("--write-sheets", action="store_true")
    parser.add_argument("--validate", type=Path, default=None)
    parser.add_argument("--validate-second", type=Path, default=None)
    args = parser.parse_args(argv)

    root = args.material_root.resolve()
    packet_path = args.packet or (root / DEFAULT_MATERIAL)
    packet = load_packet(packet_path)

    if args.write_sheets:
        if args.out_dir is None:
            print("--write-sheets needs --out-dir", file=sys.stderr)
            return 2
        report = {
            "packet": str(packet_path),
            "packet_sha256": sha256_file(packet_path),
            "packet_label_conflicts": packet_label_conflicts(packet),
            "slot_collisions": _check_slot_collisions(packet),
            "total_slots": total_slots(packet),
            "sheets": [
                write_review_sheet(packet, role, args.out_dir / f"review-sheet-{role.lower()}-v1.csv")
                for role in ("A", "B")
            ],
            "shells": [
                write_submission_shell(packet, role,
                                       args.out_dir / f"submission-shell-{role.lower()}-v1.json")
                for role in ("A", "B")
            ],
        }
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 0

    if args.validate is not None:
        first = validate_submission(packet, _read_json(args.validate))
        output: dict[str, Any] = {"first_pass": first}
        if args.validate_second is not None:
            second = validate_submission(packet, _read_json(args.validate_second))
            output["second_pass"] = second
            output["cross_check"] = cross_check(first, second)
        print(json.dumps(output, ensure_ascii=False, indent=2))
        return 0

    print("nothing to do: pass --write-sheets or --validate", file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
