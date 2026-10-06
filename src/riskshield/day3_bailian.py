"""Alibaba Cloud Bailian adapter for the Day 3 simulation engine.

Importing or configuring this module never sends a request. Live use requires
an explicit ``enable_live=True`` and a positive per-instance call limit.
"""

import json
import os
import re
import threading
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from pathlib import Path
from urllib.parse import urlsplit

import httpx

from riskshield.day3 import DecisionPricing, DecisionResult
from riskshield.schemas import AgentDecision


BEIJING_BASE_URL = "https://dashscope.aliyuncs.com/compatible-mode/v1"
ROLE_MODELS = {
    "netizen": "qwen-turbo",
    "customer": "qwen-turbo",
    "media": "qwen-flash",
    "kol": "qwen-flash",
    "regulator": "qwen-flash",
}
ENV_MODEL_KEYS = {
    role: f"RISKSHIELD_DAY3_MODEL_{role.upper()}" for role in ROLE_MODELS
}
SYSTEM_PROMPT = (
    "Make one runtime decision for one fictional social-simulation agent. "
    "Use only the supplied fictional evidence, local state, received simulated messages, "
    "and this agent's own simulated memory. Return one JSON object with exactly agent_id, "
    "action, evidence_ids, reason. action must be observe, share, comment, "
    "seek_clarification, or express_complaint_intent. Cite only allowed_citation_ids."
)
PROMPT_OVERHEAD_TOKENS = 128


class BailianConfigurationError(ValueError):
    pass


class BailianRequestError(RuntimeError):
    pass


def load_local_env(path: str | Path) -> tuple[str, ...]:
    """Load missing variables from a local dotenv file without returning values."""
    env_path = Path(path)
    if not env_path.is_file():
        return ()
    loaded = []
    for raw_line in env_path.read_text(encoding="utf-8-sig").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[7:].lstrip()
        if "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key):
            continue
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {'"', "'"}:
            value = value[1:-1]
        elif " #" in value:
            value = value.split(" #", 1)[0].rstrip()
        if key not in os.environ:
            os.environ[key] = value
            loaded.append(key)
    return tuple(loaded)


def _decimal_env(name: str) -> Decimal:
    value = os.getenv(name, "").strip()
    if not value:
        raise BailianConfigurationError(f"missing {name}")
    try:
        result = Decimal(value)
    except InvalidOperation:
        raise BailianConfigurationError(f"invalid {name}") from None
    if result <= 0:
        raise BailianConfigurationError(f"{name} must be positive")
    return result


def _int_env(name: str) -> int:
    value = os.getenv(name, "").strip()
    try:
        result = int(value)
    except ValueError:
        raise BailianConfigurationError(f"invalid {name}") from None
    if result <= 0:
        raise BailianConfigurationError(f"{name} must be positive")
    return result


@dataclass(frozen=True)
class BailianRoute:
    model: str
    price_version: str
    input_cny_per_million: Decimal
    cached_input_cny_per_million: Decimal
    output_cny_per_million: Decimal
    max_input_tokens: int
    max_output_tokens: int

    @property
    def reservation_cny(self):
        # Cached input is never assumed during reservation.
        return ((Decimal(self.max_input_tokens) * self.input_cny_per_million +
                 Decimal(self.max_output_tokens) * self.output_cny_per_million) /
                Decimal(1_000_000))

    def pricing(self):
        return DecisionPricing(
            requested_model=self.model,
            price_version=self.price_version,
            reservation_cny=self.reservation_cny,
            input_cny_per_million=self.input_cny_per_million,
            cached_input_cny_per_million=self.cached_input_cny_per_million,
            output_cny_per_million=self.output_cny_per_million,
        )


@dataclass(frozen=True)
class BailianConfig:
    api_key: str
    base_url: str
    routes: dict[str, BailianRoute]

    @classmethod
    def from_env(cls, env_path: str | Path = ".env", *, require_api_key: bool = False,
                 required_models: set[str] | None = None):
        load_local_env(env_path)
        base_url = os.getenv("RISKSHIELD_BAILIAN_BASE_URL", BEIJING_BASE_URL).rstrip("/")
        api_key = (os.getenv("DASHSCOPE_API_KEY") or
                   os.getenv("BAILIAN_API_KEY") or "").strip()
        if require_api_key and not api_key:
            raise BailianConfigurationError("DASHSCOPE_API_KEY is not configured")
        for role, expected in ROLE_MODELS.items():
            configured = os.getenv(ENV_MODEL_KEYS[role], expected).strip()
            if configured != expected:
                raise BailianConfigurationError(
                    f"{ENV_MODEL_KEYS[role]} must be {expected} for this Day 3 plan")
        selected = ({"qwen-turbo", "qwen-flash"} if required_models is None
                    else set(required_models))
        if not selected or not selected <= {"qwen-turbo", "qwen-flash"}:
            raise BailianConfigurationError("invalid required Bailian models")
        routes = {}
        for model, prefix in (("qwen-turbo", "TURBO"), ("qwen-flash", "FLASH")):
            if model not in selected:
                continue
            root = f"RISKSHIELD_BAILIAN_{prefix}_"
            routes[model] = BailianRoute(
                model=model,
                price_version=os.getenv(root + "PRICE_VERSION", "").strip(),
                input_cny_per_million=_decimal_env(root + "INPUT_CNY_PER_MILLION"),
                cached_input_cny_per_million=_decimal_env(
                    root + "CACHED_INPUT_CNY_PER_MILLION"),
                output_cny_per_million=_decimal_env(root + "OUTPUT_CNY_PER_MILLION"),
                max_input_tokens=_int_env(root + "MAX_INPUT_TOKENS"),
                max_output_tokens=_int_env(root + "MAX_OUTPUT_TOKENS"),
            )
            if not routes[model].price_version:
                raise BailianConfigurationError(f"missing {root}PRICE_VERSION")
        return cls(api_key=api_key, base_url=base_url, routes=routes)


def _short(value, limit):
    return re.sub(r"\s+", " ", str(value)).strip()[:limit]


class BailianBackend:
    mode = "real_model"

    def __init__(self, config: BailianConfig, *, enable_live: bool = False,
                 max_calls: int = 0):
        self.config = config
        self.enable_live = enable_live
        self.max_calls = max_calls
        self.calls = 0
        self._lock = threading.Lock()
        self._validate_config()
        if enable_live and (not config.api_key or max_calls <= 0):
            raise BailianConfigurationError(
                "live Bailian use requires an API key and a positive explicit call limit")

    def _validate_config(self):
        parsed = urlsplit(self.config.base_url)
        if (self.config.base_url.rstrip("/") != BEIJING_BASE_URL or
                parsed.scheme != "https" or parsed.hostname != "dashscope.aliyuncs.com" or
                parsed.query or parsed.fragment or parsed.username or parsed.password):
            raise BailianConfigurationError("Day 3 Bailian must use the Beijing HTTPS endpoint")
        if (not self.config.routes or
                not set(self.config.routes) <= {"qwen-turbo", "qwen-flash"}):
            raise BailianConfigurationError("invalid fixed Day 3 model routes")
        for model, route in self.config.routes.items():
            rates = (route.input_cny_per_million, route.cached_input_cny_per_million,
                     route.output_cny_per_million)
            if (route.model != model or not route.price_version or
                    route.max_input_tokens <= 0 or route.max_output_tokens <= 0 or
                    any(rate <= 0 for rate in rates) or route.reservation_cny <= 0):
                raise BailianConfigurationError("invalid Bailian route")

    def pricing(self, observation):
        role = observation.get("persona", {}).get("role")
        try:
            return self.config.routes[ROLE_MODELS[role]].pricing()
        except KeyError:
            raise BailianConfigurationError("unknown Day 3 agent role") from None

    def describe(self):
        return {
            "type": self.__class__.__name__,
            "provider": "alibaba-cloud-bailian",
            "region": "cn-beijing",
            "base_url": self.config.base_url,
            "thinking": "disabled",
            "role_models": dict(ROLE_MODELS),
            "routes": {model: {
                "price_version": route.price_version,
                "input_cny_per_million": str(route.input_cny_per_million),
                "cached_input_cny_per_million": str(route.cached_input_cny_per_million),
                "output_cny_per_million": str(route.output_cny_per_million),
                "max_input_tokens": route.max_input_tokens,
                "max_output_tokens": route.max_output_tokens,
                "input_bound_method": "utf8_bytes_plus_protocol_allowance",
                "reservation_cny": str(route.reservation_cny),
            } for model, route in self.config.routes.items()},
        }

    def _fields(self, observation):
        if observation.get("data_mode") != "synthetic":
            raise BailianRequestError("Bailian Day 3 adapter currently permits synthetic input only")
        evidence = observation.get("evidence", [])[:2]
        messages = observation.get("messages", [])[-3:]
        memories = observation.get("memories", [])[:3]
        fields = {
            "agent": {
                "agent_id": observation["agent_id"],
                "role": observation["persona"]["role"],
                "parameters": {
                    "susceptibility": observation["persona"]["susceptibility"],
                    "influence": observation["persona"]["influence"],
                },
            },
            "round": observation["round"],
            "local_state": observation["state"],
            "fictional_evidence": [{
                "evidence_id": item["evidence_id"],
                "title": _short(item["title"], 80),
                "summary": _short(item["text"], 180),
            } for item in evidence],
            "received_simulated_messages": [{
                "message_id": item["message_id"],
                "sender_id": item["sender_id"],
                "action": item["action"],
                "content": _short(item.get("content", ""), 160),
                "evidence_ids": item.get("evidence_ids", [])[:2],
            } for item in messages],
            "own_simulated_memory": [{
                "memory_id": item["memory_id"],
                "content": _short(item["content"], 120),
            } for item in memories],
        }
        allowed = [item["evidence_id"] for item in evidence]
        allowed.extend(item["message_id"] for item in messages)
        allowed.extend(item["memory_id"] for item in memories)
        fields["allowed_citation_ids"] = allowed
        return fields

    def prepare_request(self, observation):
        """Build and bound one request without enabling or sending a live call."""
        try:
            route = self.config.routes[ROLE_MODELS[observation["persona"]["role"]]]
        except KeyError:
            raise BailianConfigurationError("unknown Day 3 agent role") from None
        fields = self._fields(observation)
        user_content = json.dumps(fields, ensure_ascii=False, separators=(",", ":"))
        # Every tokenizer token consumes at least one UTF-8 byte. Adding a fixed
        # chat-framing allowance therefore gives a conservative tokenizer-free
        # token upper bound while retaining message and own-memory fields.
        input_upper_bound_tokens = (
            len(SYSTEM_PROMPT.encode("utf-8")) + len(user_content.encode("utf-8")) +
            PROMPT_OVERHEAD_TOKENS
        )
        if input_upper_bound_tokens > route.max_input_tokens:
            raise BailianRequestError("sanitized Bailian prompt exceeds its reserved input bound")
        payload = {
            "model": route.model,
            "messages": [
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": user_content},
            ],
            "response_format": {"type": "json_object"},
            "enable_thinking": False,
            "temperature": 0,
            "max_tokens": route.max_output_tokens,
        }
        return payload, input_upper_bound_tokens

    def decide(self, observation):
        if not self.enable_live:
            raise BailianRequestError("live Bailian calls are disabled pending separate authorization")
        route = self.config.routes[ROLE_MODELS[observation["persona"]["role"]]]
        payload, _ = self.prepare_request(observation)
        with self._lock:
            if self.calls >= self.max_calls:
                raise BailianRequestError("explicit Bailian call limit reached")
            self.calls += 1
        try:
            response = httpx.post(
                self.config.base_url + "/chat/completions", json=payload,
                headers={"Authorization": "Bearer " + self.config.api_key},
                timeout=30, follow_redirects=False,
            )
            if response.status_code != 200:
                raise BailianRequestError(
                    f"Bailian returned HTTP {response.status_code}; body suppressed")
            body = response.json()
            choice = body["choices"][0]
            if choice.get("finish_reason") != "stop":
                raise BailianRequestError("Bailian response did not finish normally")
            decision = AgentDecision.model_validate_json(choice["message"]["content"])
            usage = dict(body["usage"])
            details = usage.get("prompt_tokens_details")
            cached = usage.get("cached_input_tokens", usage.get("prompt_cache_hit_tokens"))
            if cached is None and isinstance(details, dict):
                cached = details.get("cached_tokens", 0)
            usage["cached_input_tokens"] = 0 if cached is None else cached
            if any(type(usage.get(name)) is not int or usage[name] < 0
                   for name in ("prompt_tokens", "completion_tokens", "cached_input_tokens")):
                raise BailianRequestError("Bailian response lacks valid token usage")
            if usage["cached_input_tokens"] > usage["prompt_tokens"]:
                raise BailianRequestError("Bailian cached token usage is inconsistent")
            actual_model = body["model"]
            if not isinstance(actual_model, str) or not actual_model.strip():
                raise BailianRequestError("Bailian response lacks a model identifier")
        except httpx.HTTPError:
            raise BailianRequestError("Bailian request failed; no retry was attempted") from None
        except (KeyError, IndexError, TypeError, ValueError):
            raise BailianRequestError(
                "Bailian response violates the AgentDecision contract") from None
        return DecisionResult(decision=decision, usage=usage, model=actual_model)
