import json
from pathlib import Path

import httpx
import pytest

from riskshield import sentiment
from riskshield.sentiment import SentimentError, run_once


PACKAGE = Path(__file__).resolve().parents[1] / "data/public/unh_change_20240222_event_v2.json"
ENDPOINT = "https://dashscope.aliyuncs.com/compatible-mode/v1/chat/completions"


def _package():
    return json.loads(PACKAGE.read_text(encoding="utf-8"))


@pytest.fixture(autouse=True)
def bailian_environment(monkeypatch):
    """Configure the Bailian provider so configuration is never the thing under test."""
    monkeypatch.setenv("DASHSCOPE_API_KEY", "test-key")
    monkeypatch.setenv("RISKSHIELD_BAILIAN_BASE_URL",
                       "https://dashscope.aliyuncs.com/compatible-mode/v1")
    monkeypatch.setenv("RISKSHIELD_DAY3_MODEL_MEDIA", "qwen-flash")
    monkeypatch.setenv("RISKSHIELD_BAILIAN_FLASH_PRICE_VERSION", "test-price-version")
    monkeypatch.setenv("RISKSHIELD_BAILIAN_FLASH_INPUT_CNY_PER_MILLION", "0.15")
    monkeypatch.setenv("RISKSHIELD_BAILIAN_FLASH_OUTPUT_CNY_PER_MILLION", "1.5")


def _ok_response():
    return httpx.Response(200, json={
        "model": "qwen-flash-2025-09-01", "choices": [{
            "finish_reason": "stop",
            "message": {"content": '{"label":"neutral","reason":"事实陈述。"}'},
        }],
        "usage": {"prompt_tokens": 100, "completion_tokens": 20, "total_tokens": 120},
    })


def test_only_approved_cutoff_fields_are_sent_after_synthetic_validation(monkeypatch):
    outbound = []

    def post(url, *, json, headers, timeout, follow_redirects):
        assert url == ENDPOINT
        assert headers["Authorization"] == "Bearer test-key"
        assert timeout == 30 and follow_redirects is False
        assert json["model"] == "qwen-flash"
        assert json["response_format"] == {"type": "json_object"}
        # The DeepSeek-only reasoning switches must not be sent to Bailian.
        assert "thinking" not in json
        assert "reasoning_effort" not in json
        outbound.append(json)
        return _ok_response()

    monkeypatch.setattr(httpx, "post", post)
    result = run_once(_package(), api_key="test-key")
    assert result["status"] == "completed_two_calls"
    assert result["endpoint"] == ENDPOINT
    assert result["requested_model"] == "qwen-flash"
    assert result["pricing"]["price_version"] == "test-price-version"
    assert len(outbound) == 2
    assert "Example Insurance" in outbound[0]["messages"][1]["content"]
    real = json.loads(outbound[1]["messages"][1]["content"])
    assert set(real) == {"company", "title", "summary"}
    assert real["company"] == "UnitedHealth Group"
    assert "未经授权访问" in real["summary"]
    assert "unh-sec-20240222-initial" not in json.dumps(outbound[1], ensure_ascii=False)
    assert "sec.gov" not in json.dumps(outbound[1], ensure_ascii=False)
    assert "资金支持" not in json.dumps(outbound[1], ensure_ascii=False)
    assert result["estimated_total_peak_cny_ceiling"] < 2.36


def test_ceiling_uses_the_configured_cny_price_not_a_usd_guess(monkeypatch):
    monkeypatch.setattr(httpx, "post", lambda *a, **k: _ok_response())
    result = run_once(_package(), api_key="test-key")
    expected = round((100 * 0.15 + 20 * 1.5) / 1_000_000, 8)
    assert result["real"]["estimated_peak_cny_ceiling"] == expected


def test_synthetic_invalid_json_stops_before_real_request(monkeypatch):
    calls = []

    def post(*_args, **_kwargs):
        calls.append(1)
        return httpx.Response(200, json={
            "model": "qwen-flash-2025-09-01", "choices": [{
                "finish_reason": "stop", "message": {"content": '{"label":"unknown"}'},
            }],
            "usage": {"prompt_tokens": 100, "completion_tokens": 20, "total_tokens": 120},
        })

    monkeypatch.setattr(httpx, "post", post)
    with pytest.raises(SentimentError):
        run_once(_package(), api_key="test-key")
    assert len(calls) == 1


def test_later_record_cannot_replace_approved_snapshot_input(monkeypatch):
    package = _package()
    package["case_import_projection"]["records"][0]["role"] = "evaluation_only"
    monkeypatch.setattr(httpx, "post", lambda *_args, **_kwargs: pytest.fail("network called"))
    with pytest.raises(SentimentError, match="唯一获准输入"):
        run_once(package, api_key="test-key")


def test_missing_credential_stops_before_any_request(monkeypatch):
    monkeypatch.delenv("DASHSCOPE_API_KEY", raising=False)
    monkeypatch.setattr(httpx, "post", lambda *_args, **_kwargs: pytest.fail("network called"))
    with pytest.raises(SentimentError, match="DASHSCOPE_API_KEY"):
        run_once(_package())


def test_missing_price_version_stops_before_any_request(monkeypatch):
    monkeypatch.delenv("RISKSHIELD_BAILIAN_FLASH_PRICE_VERSION", raising=False)
    monkeypatch.setattr(httpx, "post", lambda *_args, **_kwargs: pytest.fail("network called"))
    with pytest.raises(SentimentError, match="价格版本"):
        run_once(_package())


def test_non_numeric_price_stops_before_any_request(monkeypatch):
    monkeypatch.setenv("RISKSHIELD_BAILIAN_FLASH_INPUT_CNY_PER_MILLION", "not-a-number")
    monkeypatch.setattr(httpx, "post", lambda *_args, **_kwargs: pytest.fail("network called"))
    with pytest.raises(SentimentError, match="必须是数字"):
        run_once(_package())


def test_configuration_keeps_the_credential_out_of_reportable_fields(monkeypatch):
    configuration = sentiment.model_configuration()
    assert configuration["api_key"] == "test-key"
    reportable = {k: v for k, v in configuration.items() if k != "api_key"}
    assert "test-key" not in json.dumps(reportable, ensure_ascii=False)
