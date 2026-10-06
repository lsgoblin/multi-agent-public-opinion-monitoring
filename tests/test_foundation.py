import json
from copy import deepcopy
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from riskshield.api import create_app
from riskshield.model_gateway import ModelUnavailable, model_status, probe_model
from riskshield.schemas import CaseImport, DailyComplaint
from riskshield.store import Store


@pytest.fixture
def payload():
    path = Path(__file__).resolve().parents[1] / "data/public/za_marketing_2024.json"
    return json.loads(path.read_text(encoding="utf-8"))


@pytest.fixture
def client(tmp_path):
    with TestClient(create_app(tmp_path / "test.db")) as api:
        yield api


def test_import_is_idempotent_and_persistent(client, payload, tmp_path):
    assert client.post("/cases", json=payload).json()["status"] == "imported"
    reversed_payload = deepcopy(payload)
    reversed_payload["records"].reverse()
    assert client.post("/cases", json=reversed_payload).json()["status"] == "unchanged"
    reopened = Store(tmp_path / "test.db")
    assert len(reopened.records(payload["case_id"])) == 3
    changed = deepcopy(payload)
    changed["records"][0]["summary"] = "不能静默覆盖历史版本"
    assert client.post("/cases", json=changed).status_code == 409
    assert len(client.get("/cases").json()) == 1


def test_future_and_labels_never_enter_snapshot(client, payload):
    # Even a misclassified future record must be excluded by time, not just its role.
    # Supply a known time only inside this filter test; the historical fixture has none.
    payload["records"][0]["available_at"] = "2024-03-14T11:12:02+08:00"
    payload["records"][1]["available_at"] = "2024-03-15T13:35:59+08:00"
    payload["records"][1]["role"] = "input_candidate"
    assert client.post("/cases", json=payload).status_code == 200
    snapshot = client.get(f"/cases/{payload['case_id']}/snapshot").json()
    assert [item["record_id"] for item in snapshot["records"]] == ["sina_20240314"]
    assert snapshot["excluded_counts"] == {"after_cutoff": 1, "not_input": 1}
    assert "sina_20240320" not in json.dumps(snapshot)
    assert snapshot["backtest_integrity"] == "approximate"
    evaluation = client.get(f"/evaluations/cases/{payload['case_id']}/evidence").json()
    assert [item["record_id"] for item in evaluation["records"]] == ["sina_20240320"]


def test_unknown_availability_is_excluded(client, payload):
    client.post("/cases", json=payload)
    snapshot = client.get(f"/cases/{payload['case_id']}/snapshot").json()
    assert snapshot["records"] == []
    assert snapshot["excluded_counts"] == {"availability_unknown": 1, "not_input": 2}
    assert snapshot["backtest_integrity"] == "no_eligible_inputs"


@pytest.mark.parametrize("mutation", ["naive", "mixed", "duplicates", "bad_time", "extra"])
def test_invalid_import_is_rejected_atomically(client, payload, mutation):
    if mutation == "naive":
        payload["cutoff"] = "2024-03-14T23:59:59"
    elif mutation == "mixed":
        payload["data_mode"] = "synthetic"
    elif mutation == "duplicates":
        payload["records"].append(payload["records"][0])
    elif mutation == "bad_time":
        payload["records"][0]["available_at"] = "2024-03-13T00:00:00+08:00"
    else:
        payload["future_complaints"] = [99] * 7
    assert client.post("/cases", json=payload).status_code == 422
    assert client.get("/cases").json() == []


def test_current_capabilities_and_missing_case(client):
    readiness = client.get("/readiness").json()
    assert readiness["g1_passed"] is True
    assert readiness["capabilities"]["dynamic_simulation_engine"] is True
    assert readiness["capabilities"]["real_model_simulation_verified"] is False
    assert readiness["capabilities"]["forecast"] is False
    assert client.get("/cases/missing/snapshot").status_code == 404
    assert client.get("/contracts").json()["daily_complaint"]


def test_missing_complaints_are_not_zero():
    row = dict(business_date="2024-03-15", scope="demo", count=None, coverage="missing",
               available_at="2024-03-16T00:00:00+08:00", source_reference="not_received",
               definition_version="draft", data_mode="synthetic")
    assert DailyComplaint(**row).count is None
    with pytest.raises(ValidationError):
        DailyComplaint(**{**row, "count": 0})
    assert DailyComplaint(**{**row, "coverage": "complete", "count": 0}).count == 0
    with pytest.raises(ValidationError):
        DailyComplaint(**{**row, "coverage": "complete", "count": -1})


@pytest.fixture
def model_env(monkeypatch):
    for key in ("BASE_URL", "NAME", "API_KEY"):
        monkeypatch.delenv("RISKSHIELD_MODEL_" + key, raising=False)
    monkeypatch.delenv("RISKSHIELD_ENABLE_MODEL_PROBE", raising=False)


def test_no_model_never_returns_fake_success(client, model_env, monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("network must not be called without model configuration")
    monkeypatch.setattr(httpx, "post", forbidden)
    assert model_status()["status"] == "not_configured"
    assert client.post("/models/probe").status_code == 503


def configure_probe(monkeypatch):
    monkeypatch.setenv("RISKSHIELD_MODEL_BASE_URL", "https://provider.invalid/v1")
    monkeypatch.setenv("RISKSHIELD_MODEL_NAME", "test-model")
    monkeypatch.setenv("RISKSHIELD_MODEL_API_KEY", "test-secret-not-real")


def test_probe_requires_explicit_enable(model_env, monkeypatch):
    configure_probe(monkeypatch)
    with pytest.raises(ModelUnavailable, match="尚未启用"):
        probe_model()
    assert "test-secret" not in json.dumps(model_status())


@pytest.mark.parametrize("mode", ["ok", "bad_evidence", "truncated", "provider_error"])
def test_probe_validates_response_without_exposing_provider_body(model_env, monkeypatch, mode):
    configure_probe(monkeypatch)
    monkeypatch.setenv("RISKSHIELD_ENABLE_MODEL_PROBE", "1")

    def fake_post(url, **kwargs):
        assert url == "https://provider.invalid/v1/chat/completions"
        assert kwargs["follow_redirects"] is False
        assert "纯合成" in kwargs["json"]["messages"][0]["content"]
        if mode == "provider_error":
            return httpx.Response(401, text="test-secret-not-real must never be exposed")
        decision = {"agent_id": "probe_agent", "action": "observe", "reason": "等待更多证据",
                    "evidence_ids": ["invented" if mode == "bad_evidence" else "sample_1"]}
        return httpx.Response(200, json={"choices": [{"finish_reason": "length" if mode == "truncated" else "stop",
                                                       "message": {"content": json.dumps(decision)}}]})

    monkeypatch.setattr(httpx, "post", fake_post)
    if mode == "ok":
        assert probe_model()["status"] == "passed"
    else:
        with pytest.raises(ModelUnavailable) as caught:
            probe_model()
        assert "test-secret" not in str(caught.value)
