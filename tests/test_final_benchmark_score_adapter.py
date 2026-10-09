import copy
import hashlib
import json
from pathlib import Path

import pytest

from experiments.final_benchmark_score import _sentiment_label_entries
from experiments.verify_final_benchmark_freezes import verify


ROOT = Path(__file__).resolve().parents[1]
PACKET = ROOT / "data/evaluation/final-benchmark/labels/blind-sentiment-packet-v6.json"
FREEZE = ROOT / "data/evaluation/final-benchmark/manifests/freeze-v6.json"


def _v6_packet():
    packet_bytes = PACKET.read_bytes()
    freeze = json.loads(FREEZE.read_text(encoding="utf-8"))
    entry = next(item for item in freeze["files"]
                 if item["path"] == PACKET.relative_to(ROOT).as_posix())
    assert len(packet_bytes) == entry["bytes"]
    assert hashlib.sha256(packet_bytes).hexdigest() == entry["sha256"]
    packet = json.loads(packet_bytes)
    assert len(packet["units"]) == 5
    return packet


def _matching_cases(packet):
    # Case shells are test fixtures only. The frozen v6 packet has five blank
    # annotation units; no human labels or predictions are created here.
    cases = []
    for index, unit in enumerate(packet["units"]):
        event_ref = unit["event_id_reference"]
        event_id = event_ref if isinstance(event_ref, str) else event_ref["event_id"]
        text_hash = hashlib.sha256(unit["frozen_text"].encode("utf-8")).hexdigest()
        cases.append({
            "evaluation_id": f"fixture-eval-{index}",
            "event_id": event_id,
            "unit_id": unit["unit_id"],
            "unit_kind": "original_title",
            "text_sha256": text_hash,
            "target_record_id": f"fixture-source-{index}",
        })
    return cases


def test_actual_c_v6_string_event_references_map_five_blank_units():
    packet = _v6_packet()
    cases = _matching_cases(packet)

    mapped = _sentiment_label_entries(packet, cases)

    assert len(mapped) == 5
    assert [(row["event_id"], row["unit_id"]) for row in mapped] == [
        (case["event_id"], case["unit_id"]) for case in cases
    ]
    assert [row["text_sha256"] for row in mapped] == [
        case["text_sha256"] for case in cases
    ]
    # The frozen v6 packet contains empty annotation slots. Mapping it must not
    # invent submissions or turn the units into scoreable human ground truth.
    assert all(row["sentiment_annotations"] == [] for row in mapped)


def test_object_reference_is_supported_and_mismatched_identity_or_text_is_rejected():
    packet = _v6_packet()
    unit = copy.deepcopy(packet["units"][0])
    event_id = unit["event_id_reference"]
    unit["event_id_reference"] = {"event_id": event_id}
    case = _matching_cases({"units": [unit]})[0]

    assert len(_sentiment_label_entries({"units": [unit]}, [case])) == 1

    wrong_hash = {**case, "text_sha256": "0" * 64}
    assert _sentiment_label_entries({"units": [unit]}, [wrong_hash]) == []
    wrong_event = {**case, "event_id": "another-event"}
    assert _sentiment_label_entries({"units": [unit]}, [wrong_event]) == []
    wrong_case = {**case, "case_id": "case-a"}
    unit["event_id_reference"] = {"event_id": event_id, "case_id": "case-b"}
    assert _sentiment_label_entries({"units": [unit]}, [wrong_case]) == []


def test_duplicate_case_identity_is_not_ambiguously_joined():
    packet = _v6_packet()
    case = _matching_cases(packet)[0]
    with pytest.raises(ValueError, match="duplicate event/unit identity"):
        _sentiment_label_entries(packet, [case, dict(case)])


def test_current_frozen_reconciliation_reports_zero_formal_score_denominator():
    result = verify(ROOT)

    assert result["all_freeze_members_and_parent_hashes_ok"] is True
    assert result["latest_complete_handoff"] is not None
    assert result["formal_score_ready_holdout_total"] == 0
    counts = result["latest_status_counts"]
    assert counts["formal_score_ready_holdout_events"] == 0
    assert counts["sentiment_units_with_paired_E_prediction"] == 0
    assert counts["computed_sentiment_score_units"] == 0
    assert counts["independent_human_trajectory_labels"] == 0
    assert "payloads were not opened" in result["status_payload_policy"]
