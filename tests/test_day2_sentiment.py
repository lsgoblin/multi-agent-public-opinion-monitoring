import json
from pathlib import Path

import httpx
import pytest

from riskshield.day2_sentiment import SentimentError, run_once


PACKAGE = Path(__file__).resolve().parents[1] / "data/public/unh_change_20240222_event_v2.json"


def _package():
    return json.loads(PACKAGE.read_text(encoding="utf-8"))


def test_only_approved_cutoff_fields_are_sent_after_synthetic_validation(monkeypatch):
    outbound = []

    def post(url, *, json, headers, timeout, follow_redirects):
        assert url == "https://api.deepseek.com/chat/completions"
        assert headers["Authorization"] == "Bearer test-key"
        assert timeout == 30 and follow_redirects is False
        assert json["model"] == "deepseek-flash"
        assert json["thinking"] == {"type": "disabled"}
        assert json["response_format"] == {"type": "json_object"}
        outbound.append(json)
        return httpx.Response(200, json={
            "model": "DeepSeek-V4.1-Flash", "choices": [{
                "finish_reason": "stop",
                "message": {"content": '{"label":"neutral","reason":"事实陈述。"}'},
            }],
            "usage": {"prompt_tokens": 100, "completion_tokens": 20, "total_tokens": 120},
        })

    monkeypatch.setattr(httpx, "post", post)
    result = run_once(_package(), api_key="test-key")
    assert result["status"] == "completed_two_calls"
    assert len(outbound) == 2
    assert "Example Insurance" in outbound[0]["messages"][1]["content"]
    real = json.loads(outbound[1]["messages"][1]["content"])
    assert set(real) == {"company", "title", "summary"}
    assert real["company"] == "UnitedHealth Group"
    assert "未经授权访问" in real["summary"]
    assert "unh-sec-20240222-initial" not in json.dumps(outbound[1], ensure_ascii=False)
    assert "sec.gov" not in json.dumps(outbound[1], ensure_ascii=False)
    assert "资金支持" not in json.dumps(outbound[1], ensure_ascii=False)
    assert result["estimated_total_peak_cny_ceiling"] < 2.38


def test_synthetic_invalid_json_stops_before_real_request(monkeypatch):
    calls = []

    def post(*_args, **_kwargs):
        calls.append(1)
        return httpx.Response(200, json={
            "model": "DeepSeek-V4.1-Flash", "choices": [{
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
