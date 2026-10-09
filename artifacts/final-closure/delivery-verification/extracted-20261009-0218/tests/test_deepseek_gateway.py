import json

import httpx
import pytest

from riskshield.model_gateway import ModelUnavailable, probe_model


@pytest.fixture
def deepseek_env(monkeypatch):
    monkeypatch.setenv("RISKSHIELD_MODEL_BASE_URL", "https://api.deepseek.com")
    monkeypatch.setenv("RISKSHIELD_MODEL_NAME", "deepseek-flash")
    monkeypatch.setenv("RISKSHIELD_MODEL_API_KEY", "offline-test-key")
    monkeypatch.setenv("RISKSHIELD_ENABLE_MODEL_PROBE", "1")
    monkeypatch.delenv("RISKSHIELD_DEEPSEEK_THINKING_MODE", raising=False)
    monkeypatch.delenv("RISKSHIELD_DEEPSEEK_REASONING_EFFORT", raising=False)


def response(*, finish_reason="stop", content=None, model="deepseek-flash"):
    decision = {"agent_id": "probe_agent", "action": "observe", "reason": "等待更多证据",
                "evidence_ids": ["sample_1"]}
    return httpx.Response(200, json={
        "model": model,
        "choices": [{"finish_reason": finish_reason,
                     "message": {"content": json.dumps(decision) if content is None else content}}],
        "usage": {"prompt_tokens": 20, "completion_tokens": 12, "total_tokens": 32,
                  "completion_tokens_details": {"reasoning_tokens": 4}},
    })


def test_deepseek_probe_uses_explicit_default_and_records_provider_metadata(
    deepseek_env, monkeypatch
):
    def fake_post(url, **kwargs):
        assert url == "https://api.deepseek.com/chat/completions"
        assert kwargs["json"]["thinking"] == {"type": "disabled"}
        assert kwargs["json"]["max_tokens"] == 512
        assert kwargs["timeout"] == 30
        assert kwargs["follow_redirects"] is False
        return response()

    monkeypatch.setattr(httpx, "post", fake_post)
    result = probe_model()
    assert result["model"] == "deepseek-flash"
    assert result["requested_model"] == "deepseek-flash"
    assert result["token_usage"] == {
        "prompt_tokens": 20, "completion_tokens": 12, "total_tokens": 32,
        "reasoning_tokens": 4,
    }


def test_deepseek_probe_can_enable_thinking_and_effort(deepseek_env, monkeypatch):
    monkeypatch.setenv("RISKSHIELD_DEEPSEEK_THINKING_MODE", "enabled")
    monkeypatch.setenv("RISKSHIELD_DEEPSEEK_REASONING_EFFORT", "low")

    def fake_post(url, **kwargs):
        assert kwargs["json"]["thinking"] == {"type": "enabled"}
        assert kwargs["json"]["reasoning_effort"] == "low"
        return response()

    monkeypatch.setattr(httpx, "post", fake_post)
    assert probe_model()["status"] == "passed"


@pytest.mark.parametrize("mode", ["auto", "true", ""])
def test_invalid_thinking_mode_is_rejected_before_network(deepseek_env, monkeypatch, mode):
    monkeypatch.setenv("RISKSHIELD_DEEPSEEK_THINKING_MODE", mode)
    monkeypatch.setattr(httpx, "post", lambda *args, **kwargs: pytest.fail("unexpected request"))
    with pytest.raises(ModelUnavailable, match="思考模式"):
        probe_model()


@pytest.mark.parametrize("finish_reason,content", [
    ("length", '{"agent_id":"probe_agent"}'),
    ("stop", ""),
    ("stop", "   "),
])
def test_incomplete_or_empty_response_is_rejected(deepseek_env, monkeypatch, finish_reason, content):
    monkeypatch.setattr(httpx, "post", lambda *args, **kwargs: response(
        finish_reason=finish_reason, content=content
    ))
    with pytest.raises(ModelUnavailable):
        probe_model()


def test_invalid_evidence_is_rejected(deepseek_env, monkeypatch):
    bad = {"agent_id": "probe_agent", "action": "observe", "reason": "x",
           "evidence_ids": ["unknown"]}
    monkeypatch.setattr(httpx, "post", lambda *args, **kwargs: httpx.Response(200, json={
        "model": "deepseek-flash",
        "choices": [{"finish_reason": "stop", "message": {"content": json.dumps(bad)}}],
    }))
    with pytest.raises(ModelUnavailable, match="证据引用"):
        probe_model()


def test_other_provider_does_not_receive_deepseek_parameters(deepseek_env, monkeypatch):
    monkeypatch.setenv("RISKSHIELD_MODEL_BASE_URL", "https://provider.example/v1")
    monkeypatch.setenv("RISKSHIELD_DEEPSEEK_THINKING_MODE", "enabled")

    def fake_post(url, **kwargs):
        assert "thinking" not in kwargs["json"]
        assert "reasoning_effort" not in kwargs["json"]
        return httpx.Response(200, json={
            "choices": [{"finish_reason": "stop", "message": {
                "content": json.dumps({"agent_id": "probe_agent", "action": "observe",
                                       "reason": "等待更多证据", "evidence_ids": ["sample_1"]})
            }}]
        })

    monkeypatch.setattr(httpx, "post", fake_post)
    assert probe_model()["status"] == "passed"
