"""One bounded Day 2 DeepSeek sentiment check for the audited SEC input."""

import hashlib
import json
import os
import tempfile
import time
from pathlib import Path

import httpx
from pydantic import BaseModel, ConfigDict, Field

from riskshield.schemas import CaseImport
from riskshield.store import Store


CASE_ID = "unh_change_20240222_v2_archived"
RECORD_ID = "unh-sec-20240222-initial"
MODEL = "deepseek-flash"
PROMPT_VERSION = "day2-sec-sentiment-v1"
API_URL = "https://api.deepseek.com/chat/completions"
BUDGET_CNY = 2.38
# Conservative ceiling: current official peak Flash prices, all input cache misses.
INPUT_USD_PER_MILLION = 0.30
OUTPUT_USD_PER_MILLION = 1.20
CEILING_CNY_PER_USD = 8.0
MAX_OUTPUT_TOKENS = 160
INPUT_TOKEN_RESERVE = 100_000


class SentimentError(RuntimeError):
    pass


class SentimentResult(BaseModel):
    model_config = ConfigDict(extra="forbid")
    label: str = Field(pattern="^(positive|neutral|negative)$")
    reason: str = Field(min_length=1, max_length=400)


SYSTEM_PROMPT = (
    "判断给定文字对目标公司的表达态度，而非事件风险或后续舆情走势。"
    "只用所给标题和人工摘要，返回 JSON 对象，恰有 label 和 reason 两个字段。"
    "label 只能是 positive、neutral、negative。reason 简短说明文字语气；"
    '例如 {"label":"neutral","reason":"仅客观陈述事实。"}。'
)


def _approved_input(package: dict) -> dict:
    if package.get("case_import_projection", {}).get("case_id") != CASE_ID:
        raise SentimentError("不是获准的事件包。")
    if package.get("event", {}).get("primary_subject") != "UnitedHealth Group":
        raise SentimentError("事件主体不符。")
    with tempfile.TemporaryDirectory() as directory:
        store = Store(Path(directory) / "sentiment.db")
        store.import_case(CaseImport.model_validate(package["case_import_projection"]))
        snapshot = store.snapshot(CASE_ID)
    if snapshot["data_mode"] != "real_historical" or len(snapshot["records"]) != 1:
        raise SentimentError("cutoff 快照不是唯一获准输入。")
    record = snapshot["records"][0]
    if (record["record_id"] != RECORD_ID or record["channel"] != "regulatory"
            or record["content_kind"] != "human_summary"
            or record["historical_integrity"] != "archived"
            or "000073176624000045" not in record["source_url"]):
        raise SentimentError("cutoff 输入的来源或摘要类型不符。")
    if not record["title"] or not record["summary"] or len(record["title"]) > 200 or len(record["summary"]) > 1000:
        raise SentimentError("获准字段为空或超出单次调用长度限制。")
    return {"company": "UnitedHealth Group", "title": record["title"],
            "summary": record["summary"]}


def _peak_cny(usage: dict) -> float:
    return round((usage["prompt_tokens"] * INPUT_USD_PER_MILLION
                  + usage["completion_tokens"] * OUTPUT_USD_PER_MILLION)
                 * CEILING_CNY_PER_USD / 1_000_000, 8)


def _call(fields: dict, api_key: str) -> dict:
    payload = {
        "model": MODEL,
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": json.dumps(fields, ensure_ascii=False)},
        ],
        "response_format": {"type": "json_object"},
        "thinking": {"type": "disabled"},
        "max_tokens": MAX_OUTPUT_TOKENS,
    }
    started = time.perf_counter()
    try:
        response = httpx.post(API_URL, json=payload,
                              headers={"Authorization": "Bearer " + api_key},
                              timeout=30, follow_redirects=False)
        if response.status_code != 200:
            raise SentimentError(f"DeepSeek 返回 HTTP {response.status_code}；未回显响应正文。")
        body = response.json()
        choice = body["choices"][0]
        if choice["finish_reason"] != "stop":
            raise SentimentError("模型输出未正常完成。")
        message = choice["message"]
        if message.get("reasoning_content"):
            raise SentimentError("模型返回了思考内容，未按非思考模式完成。")
        result = SentimentResult.model_validate_json(message["content"])
        usage = body["usage"]
        if not all(type(usage.get(name)) is int and usage[name] >= 0
                   for name in ("prompt_tokens", "completion_tokens", "total_tokens")):
            raise SentimentError("响应缺少可核验的 token 用量。")
        if usage["prompt_tokens"] + usage["completion_tokens"] != usage["total_tokens"]:
            raise SentimentError("响应 token 用量不一致。")
        model_version = body["model"]
        if not isinstance(model_version, str) or not model_version.strip():
            raise SentimentError("响应缺少模型版本。")
    except httpx.HTTPError:
        raise SentimentError("DeepSeek 请求失败；未重试。") from None
    except (KeyError, IndexError, TypeError, ValueError):
        raise SentimentError("DeepSeek 响应不符合三分类 JSON 契约。") from None
    return {"model": model_version, "prediction": result.model_dump(),
            "elapsed_seconds": round(time.perf_counter() - started, 3),
            "token_usage": {name: usage[name] for name in
                            ("prompt_tokens", "completion_tokens", "total_tokens")},
            "estimated_peak_cny_ceiling": _peak_cny(usage)}


def run_once(package: dict, *, api_key: str | None = None) -> dict:
    """Run a synthetic protocol check, then one approved real snapshot input."""
    approved = _approved_input(package)
    key = api_key or os.getenv("DEEPSEEK_API_KEY", "")
    if not key:
        raise SentimentError("本机未配置 DEEPSEEK_API_KEY；没有发起调用。")
    reserved_call_cny = _peak_cny({"prompt_tokens": INPUT_TOKEN_RESERVE,
                                   "completion_tokens": MAX_OUTPUT_TOKENS})
    if 2 * reserved_call_cny >= BUDGET_CNY:
        raise SentimentError("两次调用的保守费用预留触及上限，未发起调用。")
    synthetic = _call({"company": "Example Insurance", "title": "Fictional service notice",
                       "summary": "A fictional insurer reports that a test service is unavailable."}, key)
    if synthetic["estimated_peak_cny_ceiling"] + reserved_call_cny >= BUDGET_CNY:
        raise SentimentError("合成探测已触及费用上限，停止真实材料调用。")
    real = _call(approved, key)
    total_cny = round(synthetic["estimated_peak_cny_ceiling"]
                      + real["estimated_peak_cny_ceiling"], 8)
    return {"status": "completed_two_calls", "requested_model": MODEL,
            "prompt_version": PROMPT_VERSION, "thinking": "disabled",
            "outbound_real_fields": ["company", "title", "summary"],
            "local_record_id": RECORD_ID,
            "real_summary_sha256": hashlib.sha256(approved["summary"].encode()).hexdigest(),
            "synthetic": synthetic, "real": real,
            "estimated_total_peak_cny_ceiling": total_cny,
            "budget_cny": BUDGET_CNY,
            "pricing_basis": "官方峰值价、输入全按缓存未命中、8 CNY/USD 保守折算；非账单"}
