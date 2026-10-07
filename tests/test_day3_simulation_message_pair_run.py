"""All comparison-run tests intercept HTTP and use a synthetic frozen pair."""

import copy
import json
from decimal import Decimal

import pytest

import experiments.day3_simulation_message_pair_run as message_pair_run
from experiments.day3_bailian_synthetic_backend import (BEIJING_BASE_URL, BailianBackend,
                                     BailianConfig, BailianRoute)


def _configuration():
    route = BailianRoute(
        model="qwen-flash", price_version="offline-verified-label",
        input_cny_per_million=Decimal("0.15"),
        cached_input_cny_per_million=Decimal("0.03"),
        output_cny_per_million=Decimal("1.5"),
        max_input_tokens=8192, max_output_tokens=256,
    )
    return BailianConfig("api-key-must-never-appear", BEIJING_BASE_URL,
                         {"qwen-flash": route})


def _pair():
    treatment = {
        "run_id": "historical-run", "agent_id": message_pair_run.AGENT_ID,
        "round": message_pair_run.ROUND_NUMBER, "data_mode": "synthetic",
        "persona": {"role": "media", "susceptibility": 0.5, "influence": 0.4},
        "state": {"emotion": 0.1, "trust": 0.6},
        "evidence": [{"evidence_id": "fictional_ev_1", "title": "Fictional event",
                      "text": "Synthetic description only"}],
        "messages": [{"message_id": message_pair_run.MESSAGE_ID,
                      "sender_id": "agent_0018", "action": "share",
                      "content": "synthetic-message-body-must-stay-out-of-report",
                      "evidence_ids": ["fictional_ev_1"]}],
        "memories": [{"memory_id": "mem_own_1",
                      "content": "synthetic-private-memory-must-stay-out-of-report"}],
    }
    control = copy.deepcopy(treatment)
    control["messages"] = []
    adapter = BailianBackend(_configuration())
    tp = adapter.prepare_request(treatment)[0]
    cp = adapter.prepare_request(control)[0]
    report = {
        "run_id": "historical-run", "recipient_id": message_pair_run.AGENT_ID,
        "recipient_round": message_pair_run.ROUND_NUMBER,
        "message_id": message_pair_run.MESSAGE_ID, "model": "qwen-flash",
        "recipient_action": "share",  # Selection only; never a new T result.
        "reconstructed_treatment_payload_canonical_json_sha256":
            adapter._request_audit(tp)["prepared_payload_canonical_json_sha256"],
        "reconstructed_control_payload_canonical_json_sha256":
            adapter._request_audit(cp)["prepared_payload_canonical_json_sha256"],
    }
    adapter.close()
    return report, treatment, control


@pytest.fixture
def offline_pair(monkeypatch):
    monkeypatch.setattr(message_pair_run, "load_verified_frozen_pair",
                        lambda *args, **kwargs: copy.deepcopy(_pair()))
    monkeypatch.setattr(message_pair_run.BailianConfig, "from_env",
                        classmethod(lambda cls, *args, **kwargs: _configuration()))


def test_default_dry_run_has_no_http_and_pairs_only_one_message(
        tmp_path, monkeypatch, offline_pair):
    monkeypatch.setattr("experiments.day3_bailian_synthetic_backend.httpx.Client", lambda **kwargs:
                        pytest.fail("dry-run constructed an HTTP client"))
    db = tmp_path / "untouched.sqlite3"
    report = message_pair_run.dry_run(db_path=db)
    assert report["status"] == "dry_run_ready"
    assert report["network_requests"] == report["model_calls"] == 0
    assert report["sequence"] == ["T1", "C1", "C2", "T2"]
    assert report["four_call_reservation_cny"] == "0.0064512"
    assert report["historical_decision_used_as_new_result"] is False
    assert report["pair_field_differences"] == [
        "received_simulated_messages", "allowed_citation_ids"]
    assert report["treatment_request_audit"]["prepared_payload_canonical_json_sha256"] != (
        report["control_request_audit"]["prepared_payload_canonical_json_sha256"])
    assert report["treatment_request_audit"]["frozen_local_state_sha256"] == (
        report["control_request_audit"]["frozen_local_state_sha256"])
    assert not db.exists()
    serialized = json.dumps(report)
    assert "api-key-must-never-appear" not in serialized
    assert "synthetic-message-body" not in serialized
    assert "synthetic-private-memory" not in serialized


def test_budget_gate_rejects_four_calls_before_client_creation(
        tmp_path, monkeypatch, offline_pair):
    monkeypatch.setattr("experiments.day3_bailian_synthetic_backend.httpx.Client", lambda **kwargs:
                        pytest.fail("budget failure constructed HTTP client"))
    with pytest.raises(message_pair_run.MessagePairRunError,
                       match="four_call_reservation_exceeds_budget"):
        message_pair_run.dry_run(db_path=tmp_path / "not-created.db", budget_cny="0.005")
    with pytest.raises(message_pair_run.MessagePairRunError, match="budget_outside_0_01_cap"):
        message_pair_run.dry_run(db_path=tmp_path / "not-created.db", budget_cny="0.02")


def test_live_offline_http_double_runs_t1_c1_c2_t2_and_sanitizes_output(
        tmp_path, monkeypatch, offline_pair):
    posts = []

    class Client:
        def __init__(self, **kwargs):
            pass

        def post(self, url, **kwargs):
            payload = kwargs["json"]
            fields = json.loads(payload["messages"][1]["content"])
            treated = bool(fields["received_simulated_messages"])
            posts.append((url, treated, payload))
            refs = [message_pair_run.MESSAGE_ID if treated else "fictional_ev_1"]

            class Response:
                status_code = 200

                def json(self):
                    return {"model": "qwen-flash-2025-07-28", "choices": [{
                        "finish_reason": "stop", "message": {"content": json.dumps({
                            "agent_id": message_pair_run.AGENT_ID,
                            "action": "comment" if treated else "observe",
                            "evidence_ids": refs,
                            "reason": "provider-response-body-must-stay-out-of-report",
                        })}}], "usage": {"prompt_tokens": 20,
                                        "completion_tokens": 5,
                                        "provider_secret": "usage-extra-must-stay-out"}}

            return Response()

        def close(self):
            pass

    monkeypatch.setattr("experiments.day3_bailian_synthetic_backend.httpx.Client", Client)
    output = tmp_path / "new-comparison.json"
    report = message_pair_run.run_live(
        db_path=tmp_path / "history.db", output_path=output,
        enable_live=True, authorization_label="user-authorized-four-calls",
        price_checked_on=message_pair_run._beijing_today())
    assert report["status"] == "complete"
    assert "in_flight" not in report
    assert report["calls_attempted"] == len(posts) == 4
    assert [treated for _, treated, _ in posts] == [True, False, False, True]
    assert [row["label"] for row in report["results"]] == ["T1", "C1", "C2", "T2"]
    assert report["verdict"]["controlled_message_effect_observed"] is True
    assert report["verdict"]["g3_passed"] is False
    assert "recipient_action" not in report
    saved = output.read_text(encoding="utf-8")
    assert json.loads(saved) == report
    for forbidden in ("api-key-must-never-appear", "synthetic-message-body",
                      "synthetic-private-memory", "provider-response-body",
                      "usage-extra-must-stay-out", "user-authorized-four-calls"):
        assert forbidden not in saved
    assert all(set(row["usage"]) == {"prompt_tokens", "cached_input_tokens",
                                     "completion_tokens"} for row in report["results"])


@pytest.mark.parametrize("failure", ["bad_schema", "unknown_usage"])
def test_failure_stops_without_retry_and_reserves_unknown_cost(
        tmp_path, monkeypatch, offline_pair, failure):
    posts = []

    class Client:
        def __init__(self, **kwargs):
            pass

        def post(self, url, **kwargs):
            posts.append(kwargs["json"])

            class Response:
                status_code = 200

                def json(self):
                    return {"model": "qwen-flash", "choices": [{
                        "finish_reason": "stop", "message": {"content": json.dumps({
                            "agent_id": message_pair_run.AGENT_ID,
                            "action": "invalid" if failure == "bad_schema" else "observe",
                            "evidence_ids": ["fictional_ev_1"],
                            "reason": "secret response text",
                        })}}], "usage": {"prompt_tokens": "invalid" if
                                        failure == "unknown_usage" else 20,
                                        "completion_tokens": 5}}

            return Response()

        def close(self):
            pass

    monkeypatch.setattr("experiments.day3_bailian_synthetic_backend.httpx.Client", Client)
    output = tmp_path / "partial.json"
    report = message_pair_run.run_live(
        db_path=tmp_path / "history.db", output_path=output,
        enable_live=True, authorization_label="authorized",
        price_checked_on=message_pair_run._beijing_today())
    assert report["status"] == "partial"
    assert "in_flight" not in report
    assert report["calls_attempted"] == len(posts) == 1
    assert len(report["results"]) == 1
    assert report["results"][0]["status"] == "failed"
    assert report["results"][0]["estimated_cost_cny"] == "0.0016128"
    assert report["results"][0]["request_audit"]["stage"] == "prepared_for_dispatch"
    assert report["results"][0]["usage"] is None
    if failure == "bad_schema":
        assert report["results"][0]["schema_diagnostics"] is not None
    assert "secret response text" not in output.read_text(encoding="utf-8")


def test_live_requires_explicit_flags_before_any_client(tmp_path, monkeypatch, offline_pair):
    monkeypatch.setattr("experiments.day3_bailian_synthetic_backend.httpx.Client", lambda **kwargs:
                        pytest.fail("authorization failure constructed HTTP client"))
    output = tmp_path / "not-created.json"
    with pytest.raises(message_pair_run.MessagePairRunError,
                       match="explicit_live_authorization_required"):
        message_pair_run.run_live(db_path=tmp_path / "history.db", output_path=output)
    with pytest.raises(message_pair_run.MessagePairRunError,
                       match="same_day_official_price_check_required"):
        message_pair_run.run_live(db_path=tmp_path / "history.db", output_path=output,
                            enable_live=True, authorization_label="authorized")
    assert not output.exists()


def test_price_date_uses_beijing_calendar_day(monkeypatch):
    class Clock:
        @staticmethod
        def now(tz):
            assert tz.tzname(None) == "Asia/Shanghai"
            assert tz.utcoffset(None).total_seconds() == 8 * 3600
            return message_pair_run.datetime_original(2026, 10, 7, 0, 30,
                                                 tzinfo=tz)

    from datetime import datetime as real_datetime
    monkeypatch.setattr(message_pair_run, "datetime_original", real_datetime, raising=False)
    monkeypatch.setattr(message_pair_run, "datetime", Clock)
    assert message_pair_run._beijing_today() == "2026-10-07"


@pytest.mark.parametrize(("failure", "prompt_tokens", "completion_tokens", "ref",
                          "expected_cost", "expected_error"), [
    ("invalid_citation", 20, 5, "not_an_allowed_id", "0.0000105",
     "CausalRunError:invalid_decision_citation"),
    ("over_reservation", 10000, 300, "fictional_ev_1", "0.0019500",
     "CausalRunError:actual_cost_exceeds_reservation"),
])
def test_known_usage_and_actual_cost_survive_local_failure(
        tmp_path, monkeypatch, offline_pair, failure, prompt_tokens,
        completion_tokens, ref, expected_cost, expected_error):
    posts = []

    class Client:
        def __init__(self, **kwargs):
            pass

        def post(self, url, **kwargs):
            posts.append(1)

            class Response:
                status_code = 200

                def json(self):
                    return {"model": "qwen-flash", "choices": [{
                        "finish_reason": "stop", "message": {"content": json.dumps({
                            "agent_id": message_pair_run.AGENT_ID,
                            "action": "observe", "evidence_ids": [ref],
                            "reason": "do-not-persist-response-body",
                        })}}], "usage": {"prompt_tokens": prompt_tokens,
                                        "completion_tokens": completion_tokens}}

            return Response()

        def close(self):
            pass

    monkeypatch.setattr("experiments.day3_bailian_synthetic_backend.httpx.Client", Client)
    output = tmp_path / (failure + ".json")
    report = message_pair_run.run_live(
        db_path=tmp_path / "history.db", output_path=output,
        enable_live=True, authorization_label="authorized",
        price_checked_on=message_pair_run._beijing_today())
    assert report["status"] == "partial"
    assert report["calls_attempted"] == len(posts) == 1
    row = report["results"][0]
    assert row["error"] == expected_error
    assert row["usage"] == {"prompt_tokens": prompt_tokens,
                            "cached_input_tokens": 0,
                            "completion_tokens": completion_tokens}
    assert Decimal(row["estimated_cost_cny"]) == Decimal(expected_cost)
    assert "do-not-persist-response-body" not in output.read_text(encoding="utf-8")


def test_interruption_leaves_in_flight_intent_and_existing_output_blocks_rerun(
        tmp_path, monkeypatch, offline_pair):
    posts = []

    class Client:
        def __init__(self, **kwargs):
            pass

        def post(self, url, **kwargs):
            posts.append(1)
            raise KeyboardInterrupt()

        def close(self):
            pass

    monkeypatch.setattr("experiments.day3_bailian_synthetic_backend.httpx.Client", Client)
    output = tmp_path / "interrupted.json"
    options = dict(db_path=tmp_path / "history.db", output_path=output,
                   enable_live=True, authorization_label="authorized",
                   price_checked_on=message_pair_run._beijing_today())
    with pytest.raises(KeyboardInterrupt):
        message_pair_run.run_live(**options)
    saved = json.loads(output.read_text(encoding="utf-8"))
    assert saved["in_flight"] == {"label": "T1", "arm": "treatment",
                                   "reservation_cny": "0.0016128"}
    assert saved["results"] == []
    assert len(posts) == 1
    with pytest.raises(message_pair_run.MessagePairRunError, match="output_already_exists"):
        message_pair_run.run_live(**options)
    assert len(posts) == 1
