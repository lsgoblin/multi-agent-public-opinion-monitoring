"""Tests for the human-annotation sheet and submission validation helpers.

The point of these tests is that the tooling refuses to become a label source:
sheets come out blank and blind, and a returned answer is only accepted when it
matches the frozen title hash, uses an allowed label, declares blinding and
carries a timezone-aware timestamp.
"""

from __future__ import annotations

import csv
import hashlib
import json
from pathlib import Path

import pytest

from riskshield import evaluation_annotation as ann

TEXT_A = "Homeowners insurance non-renewal"
TEXT_B = "Insurance Won’t Review Claim"


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _packet_payload() -> dict:
    def unit(unit_id: str, text: str, slots: int, required: int) -> dict:
        return {
            "unit_id": unit_id,
            "event_id_reference": unit_id,
            "source_record_id": f"reddit_archived_post_{unit_id}",
            "split": "holdout",
            "text_to_annotate": text,
            "text_sha256": _sha(text),
            "source_version": "arctic-shift-title-snapshot-v1",
            "source_url": f"https://www.reddit.com/comments/{unit_id}/",
            "post_created_at_utc": "2024-12-29T21:51:54Z",
            "archive_captured_at_utc": "2024-12-29T21:52:11Z",
            "cutoff_utc": "2024-12-29T22:22:11Z",
            "required_new_independent_annotations": required,
            "annotator_slots": [
                {"slot_id": f"{unit_id}-v19-{index + 1}", "annotator_id": None,
                 "submitted_at": None, "label": None, "rationale": None,
                 "confidence": None, "prediction_exposure": None,
                 "saw_other_annotator_answer": None}
                for index in range(slots)
            ],
        }

    return {
        "schema": ann.PACKET_SCHEMA,
        "dataset_version": "final-benchmark-sentiment-reannotation-v19-2026-10-08",
        "batch_id": "sentiment-title-reannotation-v19",
        "allowed_labels": ["positive", "neutral", "negative", "uncertain"],
        "label_definitions": {
            "negative": "标题明确表达不满、担忧、指责、损失或其他负向评价。",
            "mixed": "标题同时表达清楚的正向和负向评价，且不能归为单一方向。",
            "neutral": "标题主要陈述事件或引用诉讼指称，没有清晰的评价立场。",
            "positive": "标题明确表达赞许、支持、满意或其他正向评价。",
            "unclear": "标题过短、歧义过大或无足够表达线索，无法判断语气。",
        },
        "protocol_status": "mismatch unresolved",
        "status": "prepared_not_dispatched",
        "units": [unit("unit_one", TEXT_A, 2, 2), unit("unit_two", TEXT_B, 1, 1)],
    }


@pytest.fixture()
def packet() -> dict:
    return _packet_payload()


def _answer(unit_id: str, text: str, **overrides) -> dict:
    row = {
        "unit_id": unit_id,
        "event_id_reference": unit_id,
        "frozen_text": text,
        "text_sha256": _sha(text),
        "label": "negative",
        "rationale": "标题直接陈述了不利结果。",
        "confidence": 0.8,
        "annotator_id": "annotator-1",
        "submitted_at": "2026-10-12T10:00:00+08:00",
        "source_version": "arctic-shift-title-snapshot-v1",
        "prediction_exposure": False,
        "saw_other_annotator_answer": False,
    }
    row.update(overrides)
    return row


def _submission(rows: list[dict], key: str = "units", **overrides) -> dict:
    document = {
        "schema": ann.SUBMISSION_SCHEMA_FIRST,
        "dataset_version": "test",
        "annotator_id": "annotator-1",
        "submitted_at": "2026-10-12T10:00:00+08:00",
        "prediction_exposure": False,
        "saw_other_annotator_answer": False,
        key: rows,
    }
    document.update(overrides)
    return document


# --------------------------------------------------------------------------
# packet reporting
# --------------------------------------------------------------------------

def test_label_conflict_is_reported_not_resolved(packet):
    conflict = ann.packet_label_conflicts(packet)
    assert conflict["allowed_but_undefined"] == ["uncertain"]
    assert conflict["defined_but_not_allowed"] == ["mixed", "unclear"]
    assert conflict["blocks_formal_scoring"] is True


def test_slot_count_and_ids_are_reported(packet):
    assert ann.total_slots(packet) == 3
    assert ann.slot_ids(packet) == ["unit_one-v19-1", "unit_one-v19-2", "unit_two-v19-1"]
    assert ann._check_slot_collisions(packet) == []


def test_slot_count_mismatch_is_detected():
    payload = _packet_payload()
    payload["units"][0]["annotator_slots"].pop()
    assert "slot_count_mismatch_in_unit_one" in ann._check_slot_collisions(payload)


# --------------------------------------------------------------------------
# blind review sheets
# --------------------------------------------------------------------------

def test_review_sheet_is_blank_and_blind(packet, tmp_path):
    report = ann.write_review_sheet(packet, "A", tmp_path / "sheet-a.csv")
    assert report["rows"] == 2
    with (tmp_path / "sheet-a.csv").open(encoding="utf-8") as stream:
        rows = list(csv.DictReader(stream))
    for row in rows:
        for field in ("label", "rationale", "confidence", "annotator_id",
                      "submitted_at", "prediction_exposure",
                      "saw_other_annotator_answer"):
            assert row[field] == ""
    # Only frozen text and its provenance are exposed; no answer-ish column.
    assert set(rows[0]) == set(ann.SHEET_COLUMNS)
    assert not any("predict" in column for column in rows[0] if column != "prediction_exposure")
    assert not any("suggestion" in column for column in rows[0])


def test_role_b_sheet_uses_the_second_slot_and_skips_single_slot_units(packet):
    rows_a = ann.sheet_rows(packet, "A")
    rows_b = ann.sheet_rows(packet, "B")
    assert [row["unit_id"] for row in rows_a] == ["unit_one", "unit_two"]
    # unit_two needs only one independent label, so role B has no slot there.
    assert [row["unit_id"] for row in rows_b] == ["unit_one"]
    assert rows_a[0]["slot_id"] == "unit_one-v19-1"
    assert rows_b[0]["slot_id"] == "unit_one-v19-2"


def test_two_roles_never_share_a_slot_id(packet):
    slots_a = {row["slot_id"] for row in ann.sheet_rows(packet, "A")}
    slots_b = {row["slot_id"] for row in ann.sheet_rows(packet, "B")}
    assert slots_a & slots_b == set()


def test_sheet_rejects_unknown_role(packet):
    with pytest.raises(ann.AnnotationInputError):
        ann.sheet_rows(packet, "C")


def test_submission_shell_preserves_recorded_answer_shapes(packet, tmp_path):
    first = ann.write_submission_shell(packet, "A", tmp_path / "shell-a.json")
    second = ann.write_submission_shell(packet, "B", tmp_path / "shell-b.json")
    shell_a = json.loads((tmp_path / "shell-a.json").read_text(encoding="utf-8"))
    shell_b = json.loads((tmp_path / "shell-b.json").read_text(encoding="utf-8"))
    assert shell_a["schema"] == ann.SUBMISSION_SCHEMA_FIRST and "units" in shell_a
    assert shell_b["schema"] == ann.SUBMISSION_SCHEMA_SECOND and "records" in shell_b
    assert first["entries"] == 2 and second["entries"] == 1
    assert all(row["label"] is None for row in shell_a["units"])


# --------------------------------------------------------------------------
# validation
# --------------------------------------------------------------------------

def test_valid_single_pass_is_accepted(packet):
    result = ann.validate_submission(packet, _submission([_answer("unit_one", TEXT_A)]))
    assert result["counts"] == {"rows": 1, "accepted": 1, "rejected": 0}
    assert result["accepted"][0]["slot_id"] == "unit_one-v19-1"
    assert result["accepted"][0]["label"] == "negative"
    assert result["missing_units"] == ["unit_two"]
    assert result["complete"] is False


def test_complete_submission_is_complete(packet):
    rows = [_answer("unit_one", TEXT_A), _answer("unit_two", TEXT_B)]
    result = ann.validate_submission(packet, _submission(rows))
    assert result["complete"] is True
    assert result["missing_units"] == []


def test_text_hash_mismatch_is_rejected(packet):
    bad = _answer("unit_one", TEXT_A, text_sha256="0" * 64)
    result = ann.validate_submission(packet, _submission([bad]))
    assert "text_sha256_mismatch" in result["rejected"][0]["reasons"]


def test_answer_that_rewrote_the_title_is_rejected(packet):
    bad = _answer("unit_one", "Homeowners insurance renewal problem")
    result = ann.validate_submission(packet, _submission([bad]))
    assert "answer_did_not_use_the_frozen_title" in result["rejected"][0]["reasons"]


def test_missing_label_and_rationale_are_rejected(packet):
    bad = _answer("unit_one", TEXT_A, label=None, rationale=None)
    reasons = ann.validate_submission(packet, _submission([bad]))["rejected"][0]["reasons"]
    assert "missing_label" in reasons
    assert "missing_rationale" in reasons


@pytest.mark.parametrize("label,reason", [
    ("mixed", "label_mixed_is_defined_but_not_allowed"),
    ("unclear", "label_unclear_is_defined_but_not_allowed"),
    ("angry", "label_not_in_allowed_set"),
])
def test_labels_outside_the_allowed_set_are_rejected(packet, label, reason):
    bad = _answer("unit_one", TEXT_A, label=label)
    assert reason in ann.validate_submission(packet, _submission([bad]))["rejected"][0]["reasons"]


def test_uncertain_is_flagged_as_pending_definition(packet):
    """It is in allowed_labels but has no definition, so it must not score silently."""
    row = _answer("unit_one", TEXT_A, label="uncertain")
    result = ann.validate_submission(packet, _submission([row]))
    assert "label_pending_protocol_definition" in result["rejected"][0]["reasons"]


@pytest.mark.parametrize("field,reason", [
    ("prediction_exposure", "prediction_exposure_not_declared_false"),
    ("saw_other_annotator_answer", "other_annotator_answer_exposure_not_declared_false"),
])
def test_blinding_declarations_are_required(packet, field, reason):
    bad = _answer("unit_one", TEXT_A, **{field: True})
    assert reason in ann.validate_submission(packet, _submission([bad]))["rejected"][0]["reasons"]


def test_naive_timestamp_is_rejected(packet):
    bad = _answer("unit_one", TEXT_A, submitted_at="2026-10-12T10:00:00")
    reasons = ann.validate_submission(packet, _submission([bad]))["rejected"][0]["reasons"]
    assert "missing_or_naive_submitted_at" in reasons


def test_missing_confidence_and_out_of_range_are_rejected(packet):
    missing = ann.validate_submission(
        packet, _submission([_answer("unit_one", TEXT_A, confidence=None)]))
    assert "missing_or_non_numeric_confidence" in missing["rejected"][0]["reasons"]
    out_of_range = ann.validate_submission(
        packet, _submission([_answer("unit_one", TEXT_A, confidence=1.4)]))
    assert "confidence_out_of_range" in out_of_range["rejected"][0]["reasons"]


def test_duplicate_unit_in_one_submission_is_rejected(packet):
    rows = [_answer("unit_one", TEXT_A), _answer("unit_one", TEXT_A)]
    result = ann.validate_submission(packet, _submission(rows))
    assert result["counts"]["accepted"] == 1
    assert "duplicate_unit_in_submission" in result["rejected"][0]["reasons"]


def test_unknown_unit_is_rejected(packet):
    row = _answer("unit_missing", TEXT_A)
    result = ann.validate_submission(packet, _submission([row]))
    assert result["rejected"][0]["reasons"] == ["unit_id_not_in_packet"]


def test_slot_binding_must_belong_to_the_unit(packet):
    binding = {"unit_one": "unit_two-v19-1"}
    row = _answer("unit_one", TEXT_A)
    result = ann.validate_submission(packet, _submission([row]), slot_binding=binding)
    assert "slot_binding_not_a_slot_of_this_unit" in result["rejected"][0]["reasons"]


def test_records_key_is_accepted(packet):
    row = _answer("unit_one", TEXT_A)
    submission = _submission([row], key="records",
                             schema=ann.SUBMISSION_SCHEMA_SECOND)
    result = ann.validate_submission(packet, submission)
    assert result["submission_key"] == "records"
    assert result["counts"]["accepted"] == 1


# --------------------------------------------------------------------------
# cross check
# --------------------------------------------------------------------------

def test_agreement_is_computed_over_shared_units(packet):
    first = ann.validate_submission(packet, _submission(
        [_answer("unit_one", TEXT_A, label="negative")]))
    second = ann.validate_submission(packet, _submission(
        [_answer("unit_one", TEXT_A, label="negative", annotator_id="annotator-2")]))
    result = ann.cross_check(first, second)
    assert result["units_with_both_passes"] == 1
    assert result["agreement"] == {"numerator": 1, "denominator": 1, "value": 1.0}
    assert result["agreed_units"][0]["distinct_annotators"] is True


def test_disagreement_excludes_the_unit(packet):
    first = ann.validate_submission(packet, _submission(
        [_answer("unit_one", TEXT_A, label="negative")]))
    second = ann.validate_submission(packet, _submission(
        [_answer("unit_one", TEXT_A, label="neutral", annotator_id="annotator-2")]))
    result = ann.cross_check(first, second)
    assert result["agreement"]["numerator"] == 0
    assert "independent_labels_disagree_no_adjudication" in \
        result["excluded_units"][0]["reasons"]


def test_same_annotator_twice_is_not_two_independent_labels(packet):
    first = ann.validate_submission(packet, _submission([_answer("unit_one", TEXT_A)]))
    second = ann.validate_submission(packet, _submission([_answer("unit_one", TEXT_A)]))
    result = ann.cross_check(first, second)
    assert result["units_agreeing"] == 0
    assert "same_annotator_for_both_passes" in result["excluded_units"][0]["reasons"]


def test_cross_check_reports_no_units_when_disjoint(packet):
    first = ann.validate_submission(packet, _submission([_answer("unit_one", TEXT_A)]))
    second = ann.validate_submission(packet, _submission([_answer("unit_two", TEXT_B)]))
    result = ann.cross_check(first, second)
    assert result["units_with_both_passes"] == 0
    assert result["agreement"]["value"] is None


# --------------------------------------------------------------------------
# the tooling never produces a label
# --------------------------------------------------------------------------

def test_module_exposes_no_label_generation(packet):
    """Guard against this module ever growing a label-producing helper."""
    forbidden = ("predict", "suggest", "auto_label", "generate_label", "infer_label")
    names = [name for name in dir(ann) if not name.startswith("__")]
    assert not [name for name in names if any(word in name.lower() for word in forbidden)]


def test_load_packet_rejects_a_foreign_schema(tmp_path):
    path = tmp_path / "bad.json"
    path.write_text(json.dumps({"schema": "something.else", "units": [{}]}), encoding="utf-8")
    with pytest.raises(ann.AnnotationInputError):
        ann.load_packet(path)
