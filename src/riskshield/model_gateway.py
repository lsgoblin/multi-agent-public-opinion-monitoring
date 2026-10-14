"""A single, opt-in structured-output probe. No fake successful model responses."""

import json
import os
import time
from decimal import Decimal
from urllib.parse import urlparse

import httpx

from riskshield.schemas import AgentDecision


class ModelUnavailable(RuntimeError):
    pass


class ReportQuestionModel:
    """Opt-in Beijing qwen-turbo answers; never activated by UI input or a probe."""

    max_input_tokens = 32768
    max_output_tokens = 1200
    input_price = Decimal("0.3")
    output_price = Decimal("0.6")
    price_version = "aliyun-qwen-turbo-beijing-nonthinking-20261014"
    safe_error_codes = {"Throttling", "Throttling.RateQuota", "Throttling.AllocationQuota",
                        "insufficient_quota", "rate_limit_exceeded", "AllocationQuota.FreeTierOnly",
                        "Arrearage", "AccessDenied.Unpurchased"}

    def __init__(self):
        self.enabled = os.getenv("RISKSHIELD_ENABLE_REPORT_QA") == "1"
        self.authorization_id = os.getenv("RISKSHIELD_REPORT_QA_GRANT_ID", "").strip()
        self.allowed_cases = set(filter(None, (s.strip() for s in os.getenv(
            "RISKSHIELD_REPORT_QA_ALLOWED_CASE_IDS", "").split(","))))
        self.source_version = os.getenv("RISKSHIELD_REPORT_QA_ALLOWED_SOURCE_VERSION", "").strip()
        self._key = os.getenv("DASHSCOPE_API_KEY", "").strip()
        self.base_url = os.getenv("RISKSHIELD_BAILIAN_BASE_URL",
                                  "https://dashscope.aliyuncs.com/compatible-mode/v1").rstrip("/")
        self.invalid = False
        try:
            self.budget_cny = Decimal(os.getenv("RISKSHIELD_REPORT_QA_BUDGET_CNY", "1.00"))
            self.max_calls = int(os.getenv("RISKSHIELD_REPORT_QA_MAX_CALLS", "8"))
            self.invalid = (not self.budget_cny.is_finite() or not 0 < self.budget_cny <= 30
                            or not 1 <= self.max_calls <= 100
                            or self.base_url != "https://dashscope.aliyuncs.com/compatible-mode/v1")
        except (ValueError, ArithmeticError):
            self.invalid = True
            self.budget_cny, self.max_calls = Decimal("0"), 0

    def describe(self):
        ready = bool(self.enabled and not self.invalid and self._key
                     and self.authorization_id and self.allowed_cases)
        return {"enabled": ready, "requested_model": "qwen-turbo", "provider": "aliyun_beijing",
                "allowed_case_ids": sorted(self.allowed_cases), "budget_cny": str(self.budget_cny),
                "max_calls": self.max_calls, "price_version": self.price_version,
                "execution_mode": "llm_grounded_report_qa", "default_disabled": True}

    def reservation(self, messages):
        # UTF-8 payload bytes plus chat framing is conservative; no cache discount is assumed.
        upper = len(json.dumps(messages, ensure_ascii=False).encode("utf-8")) + 2048
        if upper > self.max_input_tokens:
            raise ModelUnavailable("question_context_too_large")
        return (Decimal(self.max_input_tokens) * self.input_price +
                Decimal(self.max_output_tokens) * self.output_price) / Decimal(1_000_000)

    def complete(self, messages):
        if not self.describe()["enabled"]:
            raise ModelUnavailable("question_model_not_enabled")
        self.reservation(messages)
        payload = {"model": "qwen-turbo", "messages": messages, "enable_thinking": False,
                   "response_format": {"type": "json_object"}, "max_tokens": self.max_output_tokens,
                   "temperature": 0.2, "stream": False}
        try:
            response = httpx.post(self.base_url + "/chat/completions", json=payload,
                                  headers={"Authorization": "Bearer " + self._key},
                                  timeout=30, follow_redirects=False)
            if response.status_code != 200:
                category = f"question_provider_http_{response.status_code}"
                try:
                    code = response.json().get("error", {}).get("code")
                    if isinstance(code, str) and code in self.safe_error_codes:
                        category += "_" + code
                except (ValueError, TypeError, AttributeError):
                    pass
                raise ModelUnavailable(category)
            body = response.json()
            choice = body["choices"][0]
            usage = body["usage"]
            actual_model = body["model"]
            if (choice.get("finish_reason") != "stop"
                    or not isinstance(actual_model, str) or not actual_model.startswith("qwen-turbo")
                    or choice["message"].get("reasoning_content")
                    or not isinstance(choice["message"]["content"], str)
                    or any(type(usage.get(k)) is not int or usage[k] < 0
                           for k in ("prompt_tokens", "completion_tokens"))
                    or usage["completion_tokens"] > self.max_output_tokens
                    or usage["prompt_tokens"] > self.max_input_tokens):
                raise ModelUnavailable("question_provider_invalid_response")
            cost = (Decimal(usage["prompt_tokens"]) * self.input_price +
                    Decimal(usage["completion_tokens"]) * self.output_price) / Decimal(1_000_000)
            return {"content": choice["message"]["content"], "model": actual_model,
                    "usage": {k: usage[k] for k in ("prompt_tokens", "completion_tokens")},
                    "cost_cny": str(cost), "provider_request_id": body.get("id")}
        except httpx.HTTPError:
            raise ModelUnavailable("question_provider_network_error") from None
        except (ValueError, KeyError, IndexError, TypeError, AttributeError):
            raise ModelUnavailable("question_provider_invalid_response") from None


def model_status():
    names = ("RISKSHIELD_MODEL_BASE_URL", "RISKSHIELD_MODEL_NAME", "RISKSHIELD_MODEL_API_KEY")
    missing = [name for name in names if not os.getenv(name, "").strip()]
    return {
        "status": "not_configured" if missing else "configured_unverified",
        "missing": missing,
        "probe_enabled": os.getenv("RISKSHIELD_ENABLE_MODEL_PROBE") == "1",
        "live_probe_passed": None,
        "note": "配置存在不等于调用成功；本状态不表示动态仿真已实现。",
    }


def probe_model():
    status = model_status()
    if status["missing"]:
        raise ModelUnavailable("未配置模型服务：不能执行真实模型试验。")
    if not status["probe_enabled"]:
        raise ModelUnavailable("模型探测尚未启用；确认调用额度后设置 RISKSHIELD_ENABLE_MODEL_PROBE=1。")
    base_url = os.environ["RISKSHIELD_MODEL_BASE_URL"].rstrip("/")
    parsed = urlparse(base_url)
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ModelUnavailable("模型地址不能包含凭据、查询参数或片段。")
    if parsed.scheme != "https" and not (
        parsed.scheme == "http" and parsed.hostname in {"localhost", "127.0.0.1", "::1"}
    ):
        raise ModelUnavailable("远程模型地址必须使用 HTTPS。")
    model_name = os.environ["RISKSHIELD_MODEL_NAME"]
    payload = {
        "model": model_name,
        "messages": [
            {"role": "system", "content": "这是纯合成接入试验。仅返回 JSON，遵循此 schema："
             + json.dumps(AgentDecision.model_json_schema(), ensure_ascii=False)},
            {"role": "user", "content": "agent_id=probe_agent；证据 sample_1：某虚构产品存在服务疑问。"
             "依据此证据选择一个动作并给出简短原因，evidence_ids 只能包含 sample_1。"},
        ],
        "response_format": {"type": "json_object"}, "max_tokens": 512,
    }
    if parsed.hostname == "api.deepseek.com":
        thinking = os.getenv("RISKSHIELD_DEEPSEEK_THINKING_MODE", "disabled").strip().lower()
        if thinking not in {"enabled", "disabled"}:
            raise ModelUnavailable("DeepSeek 思考模式必须设置为 enabled 或 disabled。")
        payload["thinking"] = {"type": thinking}
        effort = os.getenv("RISKSHIELD_DEEPSEEK_REASONING_EFFORT", "").strip().lower()
        if effort:
            if effort not in {"low", "high", "max"}:
                raise ModelUnavailable("DeepSeek 思考强度必须设置为 low、high 或 max。")
            if thinking != "enabled":
                raise ModelUnavailable("只有启用 DeepSeek 思考模式时才能设置思考强度。")
            payload["reasoning_effort"] = effort
    start = time.perf_counter()
    try:
        # Do not forward Authorization across redirects; do not log provider response bodies.
        response = httpx.post(
            base_url + "/chat/completions", json=payload,
            headers={"Authorization": "Bearer " + os.environ["RISKSHIELD_MODEL_API_KEY"]},
            timeout=30, follow_redirects=False,
        )
        if response.status_code != 200:
            raise ModelUnavailable(f"模型服务返回 HTTP {response.status_code}；原始响应未回显。")
        body = response.json()
        choice = body["choices"][0]
        if choice.get("finish_reason") != "stop":
            raise ModelUnavailable("模型响应未正常结束，不能作为有效决策。")
        content = choice["message"]["content"]
        if not isinstance(content, str) or not content.strip():
            raise ModelUnavailable("模型响应内容为空，不能作为有效决策。")
        decision = AgentDecision.model_validate_json(content)
        if decision.agent_id != "probe_agent" or decision.evidence_ids != ["sample_1"]:
            raise ModelUnavailable("模型返回了未知 Agent 或证据引用。")
        actual_model = body.get("model")
        if parsed.hostname == "api.deepseek.com" and (
            not isinstance(actual_model, str) or not actual_model.strip()
        ):
            raise ModelUnavailable("模型响应未提供实际模型名称，不能记录为通过。")
        usage = body.get("usage")
        token_usage = None
        if isinstance(usage, dict):
            fields = ("prompt_tokens", "completion_tokens", "total_tokens")
            if all(type(usage.get(field)) is int and usage[field] >= 0 for field in fields):
                token_usage = {field: usage[field] for field in fields}
                details = usage.get("completion_tokens_details")
                reasoning_tokens = details.get("reasoning_tokens") if isinstance(details, dict) else None
                if type(reasoning_tokens) is int and reasoning_tokens >= 0:
                    token_usage["reasoning_tokens"] = reasoning_tokens
    except httpx.HTTPError:
        raise ModelUnavailable("模型网络请求失败；请检查地址、网络和配额。") from None
    except (ValueError, KeyError, IndexError, TypeError, AttributeError):
        raise ModelUnavailable("模型响应不符合结构化决策契约。") from None
    return {"status": "passed", "data_mode": "synthetic", "decision": decision.model_dump(),
            "elapsed_seconds": round(time.perf_counter() - start, 3),
            "requested_model": model_name, "model": actual_model, "token_usage": token_usage,
            "note": "仅通过单次接入试验，不是动态多智能体推演。"}
