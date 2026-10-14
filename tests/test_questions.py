"""Offline test doubles verify boundaries; real model evidence is recorded separately."""
import json
import threading
from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient

from riskshield.api import create_app
from riskshield.model_gateway import ModelUnavailable, ReportQuestionModel
from riskshield.store import Store
from riskshield.tasks import Day4Tasks, DEMO_CASE_ID


@pytest.fixture
def prepared(tmp_path, monkeypatch):
    store = Store(tmp_path / "questions.db")
    tasks = Day4Tasks(store)
    jobs = [tasks.run_sync(tasks.prepare_demo_run(agent_count=2, rounds=2, concurrency=2)["run_id"])
            for _ in range(2)]
    assert all(job["status"] == "complete" for job in jobs)
    monkeypatch.setenv("RISKSHIELD_ENABLE_REPORT_QA", "1")
    monkeypatch.setenv("RISKSHIELD_REPORT_QA_GRANT_ID", "offline-test-grant")
    monkeypatch.setenv("RISKSHIELD_REPORT_QA_ALLOWED_CASE_IDS", DEMO_CASE_ID)
    monkeypatch.setenv("DASHSCOPE_API_KEY", "offline-test-key-never-sent")
    return store, jobs


def cards_from(messages):
    return json.loads(messages[0]["content"].split("冻结资料：", 1)[1])["evidence_cards"]


def completion(messages, answer=None):
    card = cards_from(messages)[0]
    value = answer or {"answer_status": "supported", "answer": "这是受控离线回答，仅测试引用及对话契约。",
                       "citations": [{"evidence_id": card["evidence_id"], "quote": card["text"][:30]}]}
    return {"content": json.dumps(value), "model": "qwen-turbo-offline-fixture",
            "usage": {"prompt_tokens": 20, "completion_tokens": 10}, "cost_cny": "0.000012"}


def test_multiturn_history_and_cost_survive_api_restart(prepared, monkeypatch):
    store, jobs = prepared
    prompts = []

    def model(self, messages):
        prompts.append(messages)
        return completion(messages)

    monkeypatch.setattr(ReportQuestionModel, "complete", model)
    path = f"/reports/{jobs[0]['report_id']}/questions"
    with TestClient(create_app(store.path)) as api:
        first = api.post(path, json={"question": "来源讲了什么？"}).json()
        assert first["turn_state"] == "complete"
        assert first["citations"][0]["source_url"].startswith("https://example.com/")
        second = api.post(path, json={"question": "其中提到的调查有什么依据？",
                                    "conversation_id": first["conversation_id"], "expected_turn_count": 1}).json()
        assert second["turn_state"] == "complete"
        assert len(prompts[1]) == 4
        assert prompts[1][1]["content"] == "来源讲了什么？"
        assert first["answer"] in prompts[1][2]["content"]
        assert second["budget"]["calls_used"] == 2
        assert Decimal(second["budget"]["charged_or_reserved_cny"]) == Decimal("0.000024")
        mismatch = api.post(f"/reports/{jobs[1]['report_id']}/questions", json={
            "question": "别的报告可以沿用吗？", "conversation_id": first["conversation_id"], "expected_turn_count": 2})
        assert mismatch.status_code == 422
        conflict = api.post(path, json={"question": "旧页面并发提交", "conversation_id": first["conversation_id"],
                                       "expected_turn_count": 0})
        assert conflict.status_code == 409
        assert len(prompts) == 2
    with TestClient(create_app(store.path)) as restarted:
        history = restarted.get(path + "/" + first["conversation_id"]).json()
        assert history["turn_count"] == 2
        assert history["context_scope"]["report_id"] == jobs[0]["report_id"]
        assert history["budget"]["calls_used"] == 2


@pytest.mark.parametrize("bad", [
    {"answer_status": "supported", "answer": "坏引用", "citations": [{"evidence_id": "source:foreign", "quote": "foreign reference"}]},
    {"answer_status": "supported", "answer": "改写引用", "citations": [{"evidence_id": "source:day4-demo-fictional-notice", "quote": "invented unsupported quote"}]},
    {"answer_status": "supported", "answer": "没有引用", "citations": []},
    {"answer_status": "supported", "answer": "https://unapproved.example/", "citations": []},
])
def test_invalid_answers_are_failed_turns_not_fake_success(prepared, monkeypatch, bad):
    store, jobs = prepared
    monkeypatch.setattr(ReportQuestionModel, "complete", lambda self, messages: completion(messages, bad))
    with TestClient(create_app(store.path)) as api:
        result = api.post(f"/reports/{jobs[0]['report_id']}/questions", json={"question": "测试伪造引用"}).json()
        assert result["turn_state"] == "failed"
        assert "answer" not in result
        assert result["cost_basis"] == "provider_usage_at_recorded_prices"
        assert result["budget"]["calls_used"] == 1


def test_future_graph_tampering_rejected_before_model(prepared, monkeypatch):
    store, jobs = prepared
    prompts = []
    monkeypatch.setattr(ReportQuestionModel, "complete", lambda self, messages: prompts.append(messages))
    with store.connect() as db:
        graph = json.loads(db.execute("SELECT body FROM graphs WHERE graph_id=?", (jobs[0]["graph_id"],)).fetchone()[0])
        graph["claims"][0]["available_at"] = "2099-01-01T00:00:00+00:00"
        db.execute("UPDATE graphs SET body=? WHERE graph_id=?", (json.dumps(graph), jobs[0]["graph_id"]))
        before = db.execute("SELECT COUNT(*) FROM day4_reports").fetchone()[0]
    with TestClient(create_app(store.path)) as api:
        result = api.post(f"/reports/{jobs[0]['report_id']}/questions", json={"question": "来源事实是什么？"})
        assert result.status_code == 422 and "integrity" in result.json()["detail"]
        assert prompts == []
    with store.connect() as db:
        assert db.execute("SELECT COUNT(*) FROM day4_reports").fetchone()[0] == before


def test_default_off_and_case_whitelist_cannot_be_changed_in_request(prepared, monkeypatch):
    store, jobs = prepared
    path = f"/reports/{jobs[0]['report_id']}/questions"
    monkeypatch.setenv("RISKSHIELD_ENABLE_REPORT_QA", "0")
    with TestClient(create_app(store.path)) as api:
        assert api.get("/questions/config").json()["enabled"] is False
        assert api.post(path, json={"question": "默认不调用"}).status_code == 503
        assert api.post(path, json={"question": "篡改范围", "case_id": "other"}).status_code == 422
    monkeypatch.setenv("RISKSHIELD_ENABLE_REPORT_QA", "1")
    monkeypatch.setenv("RISKSHIELD_REPORT_QA_ALLOWED_CASE_IDS", "other")
    with TestClient(create_app(store.path)) as api:
        assert api.post(path, json={"question": "未经授权案例"}).status_code == 403


def test_unknown_provider_usage_retains_reservation(prepared, monkeypatch):
    store, jobs = prepared

    def failure(self, messages):
        raise ModelUnavailable("private-provider-response-must-not-be-exposed")

    monkeypatch.setattr(ReportQuestionModel, "complete", failure)
    with TestClient(create_app(store.path)) as api:
        result = api.post(f"/reports/{jobs[0]['report_id']}/questions", json={"question": "受控网络失败"}).json()
        assert result["turn_state"] == "failed"
        assert result["cost_basis"] == "reservation_retained_unknown_usage"
        assert Decimal(result["cost_cny"]) > 0
        assert "private-provider" not in json.dumps(result)


def test_pending_reservation_blocks_another_api_instance(prepared, monkeypatch):
    store, jobs = prepared
    monkeypatch.setenv("RISKSHIELD_REPORT_QA_BUDGET_CNY", "0.0105504")
    started, release = threading.Event(), threading.Event()
    prompts = []

    def waiting(self, messages):
        prompts.append(messages)
        started.set()
        assert release.wait(10)
        return completion(messages)

    monkeypatch.setattr(ReportQuestionModel, "complete", waiting)
    path = f"/reports/{jobs[0]['report_id']}/questions"
    with TestClient(create_app(store.path)) as first, TestClient(create_app(store.path)) as second:
        with ThreadPoolExecutor(max_workers=1) as executor:
            future = executor.submit(first.post, path, json={"question": "占用费用预留"})
            try:
                assert started.wait(10)
                assert second.get("/questions/config").json()["budget"]["calls_used"] == 1
                assert second.post(path, json={"question": "新会话不能绕过预算"}).status_code == 429
                assert len(prompts) == 1
            finally:
                release.set()
            assert future.result().json()["turn_state"] == "complete"


def test_gateway_requests_nonthinking_json_and_reports_usage(prepared, monkeypatch):
    class Response:
        status_code = 200

        def json(self):
            return {"choices": [{"finish_reason": "stop", "message": {"content": "{}"}}],
                    "model": "qwen-turbo", "usage": {"prompt_tokens": 20, "completion_tokens": 10}}

    calls = []

    def post(url, **kwargs):
        calls.append((url, kwargs))
        return Response()

    monkeypatch.setattr("riskshield.model_gateway.httpx.post", post)
    model = ReportQuestionModel()
    result = model.complete([{"role": "user", "content": "受控测试，不是真实业务模型调用"}])
    assert result["cost_cny"] == "0.000012"
    assert calls[0][0] == "https://dashscope.aliyuncs.com/compatible-mode/v1/chat/completions"
    payload = calls[0][1]["json"]
    assert payload["model"] == "qwen-turbo" and payload["enable_thinking"] is False
    assert payload["response_format"] == {"type": "json_object"}
    assert calls[0][1]["follow_redirects"] is False


@pytest.mark.parametrize("code, suffix", [("Throttling.AllocationQuota", "_Throttling.AllocationQuota"),
                                         ("private-provider-message", "")])
def test_provider_error_diagnostics_are_whitelisted(prepared, monkeypatch, code, suffix):
    class Response:
        status_code = 429

        def json(self):
            return {"error": {"code": code, "message": "private-provider-content"}}

    monkeypatch.setattr("riskshield.model_gateway.httpx.post", lambda *a, **k: Response())
    store, jobs = prepared
    with TestClient(create_app(store.path)) as api:
        result = api.post(f"/reports/{jobs[0]['report_id']}/questions", json={"question": "受控限流测试"}).json()
        assert result["failure_category"] == "question_provider_http_429" + suffix
        assert result["cost_basis"] == "reservation_retained_unknown_usage"
        assert "private-provider" not in json.dumps(result)
