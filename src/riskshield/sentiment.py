"""One bounded sentiment check against the configured model service.

The provider is the Bailian (DashScope) OpenAI-compatible endpoint recorded in
the environment, not a hardcoded host: the credential variable, base URL, model
name and price version are all configuration. Missing configuration raises
before any request is made.

The module keeps the original safety shape: a synthetic probe call runs first so
a broken contract is caught before any real material is sent, and the
conservative CNY ceiling is computed from the configured price version rather
than guessed.
"""

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
PROMPT_VERSION = "day2-sec-sentiment-v1"

# Bailian / DashScope configuration. Configuration alone does not enable a call.
API_KEY_ENV = "DASHSCOPE_API_KEY"
BASE_URL_ENV = "RISKSHIELD_BAILIAN_BASE_URL"
MODEL_ENV = "RISKSHIELD_DAY3_MODEL_MEDIA"
PRICE_VERSION_ENV = "RISKSHIELD_BAILIAN_FLASH_PRICE_VERSION"
INPUT_PRICE_ENV = "RISKSHIELD_BAILIAN_FLASH_INPUT_CNY_PER_MILLION"
OUTPUT_PRICE_ENV = "RISKSHIELD_BAILIAN_FLASH_OUTPUT_CNY_PER_MILLION"
DEFAULT_BASE_URL = "https://dashscope.aliyuncs.com/compatible-mode/v1"
DEFAULT_MODEL = "qwen-flash"

BUDGET_CNY = 2.36  # Historical one-off authorization; not the current task ceiling.
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


def model_configuration() -> dict:
    """Read the provider configuration without contacting anything.

    The credential is returned separately from everything else so callers cannot
    accidentally serialise it into a report.
    """
    api_key = os.getenv(API_KEY_ENV, "").strip()
    base_url = os.getenv(BASE_URL_ENV, "").strip() or DEFAULT_BASE_URL
    model = os.getenv(MODEL_ENV, "").strip() or DEFAULT_MODEL
    price_version = os.getenv(PRICE_VERSION_ENV, "").strip()
    try:
        input_price = float(os.getenv(INPUT_PRICE_ENV, "").strip())
        output_price = float(os.getenv(OUTPUT_PRICE_ENV, "").strip())
    except ValueError:
        raise SentimentError(
            f"未配置价格：{INPUT_PRICE_ENV} 与 {OUTPUT_PRICE_ENV} 必须是数字；未发起调用。") from None
    if input_price < 0 or output_price < 0:
        raise SentimentError("价格不能为负；未发起调用。")
    if not price_version:
        raise SentimentError(f"未配置价格版本 {PRICE_VERSION_ENV}；未发起调用。")
    return {
        "api_key": api_key,
        "base_url": base_url.rstrip("/"),
        "model": model,
        "endpoint": base_url.rstrip("/") + "/chat/completions",
        "pricing": {
            "price_version": price_version,
            "input_cny_per_million": input_price,
            "output_cny_per_million": output_price,
            "currency": "CNY",
        },
    }


def _require_key(configuration: dict, api_key: str | None) -> str:
    key = (api_key or "").strip() or configuration["api_key"]
    if not key:
        raise SentimentError(f"本机未配置 {API_KEY_ENV}；没有发起调用。")
    return key


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


def _peak_cny(pricing: dict, usage: dict) -> float:
    return round((usage["prompt_tokens"] * pricing["input_cny_per_million"]
                  + usage["completion_tokens"] * pricing["output_cny_per_million"])
                 / 1_000_000, 8)


def _call(fields: dict, api_key: str, configuration: dict) -> dict:
    payload = {
        "model": configuration["model"],
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": json.dumps(fields, ensure_ascii=False)},
        ],
        "response_format": {"type": "json_object"},
        "max_tokens": MAX_OUTPUT_TOKENS,
    }
    started = time.perf_counter()
    try:
        response = httpx.post(configuration["endpoint"], json=payload,
                              headers={"Authorization": "Bearer " + api_key},
                              timeout=30, follow_redirects=False)
        if response.status_code != 200:
            raise SentimentError(
                f"模型服务返回 HTTP {response.status_code}；未回显响应正文。")
        body = response.json()
        choice = body["choices"][0]
        if choice["finish_reason"] != "stop":
            raise SentimentError("模型输出未正常完成。")
        result = SentimentResult.model_validate_json(choice["message"]["content"])
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
        raise SentimentError("模型请求失败；未重试。") from None
    except (KeyError, IndexError, TypeError, ValueError):
        raise SentimentError("模型响应不符合三分类 JSON 契约。") from None
    return {"model": model_version, "prediction": result.model_dump(),
            "elapsed_seconds": round(time.perf_counter() - started, 3),
            "token_usage": {name: usage[name] for name in
                            ("prompt_tokens", "completion_tokens", "total_tokens")},
            "estimated_peak_cny_ceiling": _peak_cny(configuration["pricing"], usage)}


def run_once(package: dict, *, api_key: str | None = None) -> dict:
    """Run a synthetic protocol check, then one approved real snapshot input."""
    configuration = model_configuration()
    key = _require_key(configuration, api_key)
    approved = _approved_input(package)
    reserved_call_cny = _peak_cny(configuration["pricing"],
                                  {"prompt_tokens": INPUT_TOKEN_RESERVE,
                                   "completion_tokens": MAX_OUTPUT_TOKENS})
    if 2 * reserved_call_cny >= BUDGET_CNY:
        raise SentimentError("两次调用的保守费用预留触及上限，未发起调用。")
    synthetic = _call({"company": "Example Insurance", "title": "Fictional service notice",
                       "summary": "A fictional insurer reports that a test service is unavailable."},
                      key, configuration)
    if synthetic["estimated_peak_cny_ceiling"] + reserved_call_cny >= BUDGET_CNY:
        raise SentimentError("合成探测已触及费用上限，停止真实材料调用。")
    real = _call(approved, key, configuration)
    total_cny = round(synthetic["estimated_peak_cny_ceiling"]
                      + real["estimated_peak_cny_ceiling"], 8)
    return {"status": "completed_two_calls", "requested_model": configuration["model"],
            "prompt_version": PROMPT_VERSION,
            "endpoint": configuration["endpoint"],
            "pricing": configuration["pricing"],
            "outbound_real_fields": ["company", "title", "summary"],
            "local_record_id": RECORD_ID,
            "real_summary_sha256": hashlib.sha256(approved["summary"].encode()).hexdigest(),
            "synthetic": synthetic, "real": real,
            "estimated_total_peak_cny_ceiling": total_cny,
            "budget_cny": BUDGET_CNY,
            "pricing_basis": "配置价格版本对应的百万 token 单价；非供应商账单"}
