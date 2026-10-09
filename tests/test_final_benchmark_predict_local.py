from pathlib import Path

import pytest

from experiments.final_benchmark_predict_local import (
    PredictionUnavailable,
    _parse_direction,
    _validated_identity,
    local_chat_url,
    preflight,
    run,
)


ROOT = Path(__file__).resolve().parents[1]
HANDOFF = ROOT / "data/evaluation/final-benchmark/manifests/e-handoff-v13.json"


def test_local_preflight_checks_only_handoff_input_and_stops_without_model(tmp_path):
    before = HANDOFF.read_bytes()
    result = preflight(ROOT, HANDOFF, base_url="", model_name="")

    assert result["status"] == "blocked_not_configured"
    assert result["input_sha256"] == "01193167bc59ec4e6d59c12922ad7a9e09d9886deb520c255a5e963aaa9b772c"
    assert result["score_side_files_opened"] == 0
    assert result["model_calls"] == result["network_requests"] == 0
    assert HANDOFF.read_bytes() == before
    with pytest.raises(PredictionUnavailable):
        run(ROOT, HANDOFF, tmp_path / "must-not-exist", base_url="", model_name="")
    assert not (tmp_path / "must-not-exist").exists()


def test_endpoint_is_loopback_only_and_redirect_free_destination_is_fixed():
    assert local_chat_url("http://127.0.0.1:11434/v1") == \
        "http://127.0.0.1:11434/v1/chat/completions"
    with pytest.raises(ValueError):
        local_chat_url("https://model.example/v1")
    with pytest.raises(ValueError):
        local_chat_url("http://localhost.evil.example/v1")
    with pytest.raises(ValueError):
        local_chat_url("http://localhost:8000/v1")


def test_prediction_parser_accepts_only_the_frozen_direction_vocabulary():
    assert _parse_direction('{"direction":"escalation"}') == "escalation"
    assert _parse_direction('{"direction":"unknown"}') == "unknown"
    with pytest.raises(PredictionUnavailable):
        _parse_direction('{"direction":"stable"}')
    with pytest.raises(PredictionUnavailable):
        _parse_direction("not json")


def test_event_identity_allows_handoff_without_a_case_id_but_binds_event_and_split():
    seen = set()
    event = {"event_id": "event-1", "case_id": "case-v13", "split": "holdout_candidate"}
    assert _validated_identity(event, {
        "event_id": "event-1", "split": "holdout_candidate",
    }, seen) == ("event-1", "case-v13")
    with pytest.raises(ValueError):
        _validated_identity(event, {
            "event_id": "event-1", "split": "holdout",
        }, seen)
