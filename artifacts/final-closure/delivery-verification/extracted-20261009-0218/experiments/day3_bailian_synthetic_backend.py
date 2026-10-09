"""Alibaba Cloud Bailian adapter for the Day 3 simulation engine.

Importing or configuring this module never sends a request. Live use requires
an explicit ``enable_live=True`` and a positive per-instance call limit.
"""

import hashlib
import json
import os
import re
import threading
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from pathlib import Path
from urllib.parse import urlsplit

import httpx
from pydantic import ValidationError

from riskshield.day3 import DecisionPricing, DecisionResult, SafeDecisionError
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
    "seek_clarification, or express_complaint_intent. evidence_ids must contain at least "
    "one ID from allowed_citation_ids, and every cited ID must be allowed. "
    "Keep reason at most 200 characters."
)
PROMPT_OVERHEAD_TOKENS = 128
ADAPTER_CONTRACT_VERSION = "day3-bailian-synthetic-v4"
SCHEMA_FIELDS = frozenset({"agent_id", "action", "evidence_ids", "reason"})
SCHEMA_ERROR_TYPES = frozenset({
    "missing", "literal_error", "string_type", "list_type", "too_short",
    "too_long", "string_too_short", "string_too_long",
    "string_pattern_mismatch", "value_error",
})


def _canonical_sha256(value):
    data = json.dumps(value, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(data).hexdigest()


def _schema_diagnostics(error):
    groups = {}
    errors = error.errors(include_input=False, include_url=False)
    for item in errors:
        loc = item.get("loc", ())
        field = loc[0] if loc and loc[0] in SCHEMA_FIELDS else "other"
        raw_type = item.get("type")
        kind = raw_type if raw_type in SCHEMA_ERROR_TYPES else "other"
        groups[(field, kind)] = groups.get((field, kind), 0) + 1
    return {"error_count": len(errors), "groups": [
        {"field": field, "type": kind, "count": count}
        for (field, kind), count in sorted(groups.items())]}


class BailianConfigurationError(ValueError):
    pass


class BailianRequestError(SafeDecisionError):
    pass


class BailianConnectError(BailianRequestError):
    pass


class BailianTimeoutError(BailianRequestError):
    pass


class BailianProtocolError(BailianRequestError):
    pass


class BailianTransportError(BailianRequestError):
    pass


class BailianFinishLengthError(BailianRequestError):
    pass


class BailianFinishContentFilterError(BailianRequestError):
    pass


class BailianFinishOtherError(BailianRequestError):
    pass


class BailianJSONSyntaxError(BailianRequestError):
    pass


class BailianDecisionSchemaError(BailianRequestError):
    pass


class BailianResponseShapeError(BailianRequestError):
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
        return self.reservation_for_input_upper_bound(self.max_input_tokens)

    def reservation_for_input_upper_bound(self, input_upper_bound_tokens: int):
        if (type(input_upper_bound_tokens) is not int or input_upper_bound_tokens <= 0 or
                input_upper_bound_tokens > self.max_input_tokens):
            raise BailianConfigurationError("invalid reserved input token upper bound")
        return ((Decimal(input_upper_bound_tokens) * self.input_cny_per_million +
                 Decimal(self.max_output_tokens) * self.output_cny_per_million) /
                Decimal(1_000_000))

    def pricing(self, input_upper_bound_tokens: int | None = None):
        reservation = (self.reservation_cny if input_upper_bound_tokens is None else
                       self.reservation_for_input_upper_bound(input_upper_bound_tokens))
        return DecisionPricing(
            requested_model=self.model,
            price_version=self.price_version,
            reservation_cny=reservation,
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
        self._client = None
        self._closed = False
        self._validate_config()
        if enable_live and (not config.api_key or max_calls <= 0):
            raise BailianConfigurationError(
                "live Bailian use requires an API key and a positive explicit call limit")

    def close(self):
        """Release the run's shared connection pool after all decisions finish."""
        with self._lock:
            self._closed = True
            client, self._client = self._client, None
        if client is not None:
            client.close()

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
            route = self.config.routes[ROLE_MODELS[role]]
            bound = self.max_sanitized_input_upper_bound(observation)
            return route.pricing(bound)
        except KeyError:
            raise BailianConfigurationError("unknown Day 3 agent role") from None

    def max_sanitized_input_upper_bound(self, observation):
        """Bound any v4 adapter input for the fixed Northstar simulation contract.

        Evidence comes from the fixed, versioned synthetic graph. Dynamic fields use
        the adapter's hard caps (3 messages x 160 chars and 3 memories x 120 chars).
        NUL is used to cover JSON's longest escaped character representation.
        """
        try:
            role = observation["persona"]["role"]
            evidence = observation["evidence"][:2]
            evidence_ids = [item["evidence_id"] for item in evidence]
            if not evidence_ids:
                raise BailianConfigurationError("Northstar bound requires eligible evidence")
            # The real Day 3 state is fixed to emotion/trust and clipped to these ranges.
            if set(observation["state"]) != {"emotion", "trust"}:
                raise BailianConfigurationError("unexpected Northstar state shape")
            max_agent = "agent_0499"
            messages = []
            for sender_number in (496, 497, 498):
                sender = f"agent_{sender_number:04d}"
                message_id = f"msg_29_{sender}_{max_agent}"
                messages.append({
                    "message_id": message_id,
                    "sender_id": sender,
                    "action": "express_complaint_intent",
                    "content": "\x00" * 160,
                    "evidence_ids": [max(evidence_ids, key=len)] * 2,
                })
            memories = [{
                "memory_id": f"memmsg_30_{max_agent}_{message['message_id']}",
                "round": 29,
                "content": "\x00" * 120,
            } for message in messages]
            max_observation = {
                **observation,
                "agent_id": max_agent,
                "round": 30,
                "persona": {"role": role, "susceptibility": 0.9, "influence": 0.9},
                "state": {"emotion": -0.9999, "trust": 0.9999},
                "messages": messages,
                "memories": memories,
            }
            _, bound = self.prepare_request(max_observation)
            return bound
        except BailianConfigurationError:
            raise
        except BailianRequestError:
            raise BailianConfigurationError(
                "Northstar sanitized input bound exceeds configured input limit") from None
        except (KeyError, TypeError, IndexError):
            raise BailianConfigurationError("invalid Northstar input for reservation") from None

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
            raise BailianRequestError(
                "Bailian Day 3 adapter currently permits synthetic input only",
                category="synthetic_only")
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
            raise BailianRequestError(
                "sanitized Bailian prompt exceeds its reserved input bound",
                category="prompt_bound")
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

    @staticmethod
    def _request_audit(payload, input_upper_bound_tokens: int | None = None):
        # Hash the exact prepared payload object passed to httpx's json= argument.
        # This is canonical JSON, not a hash of bytes emitted by HTTPX.
        fields = json.loads(payload["messages"][1]["content"])
        if input_upper_bound_tokens is None:
            input_upper_bound_tokens = (
                len(payload["messages"][0]["content"].encode("utf-8")) +
                len(payload["messages"][1]["content"].encode("utf-8")) +
                PROMPT_OVERHEAD_TOKENS
            )
        return {
            "stage": "prepared_for_dispatch",
            "hash_basis": "canonical_json_sorted_keys_utf8_of_prepared_payload_object",
            "adapter_version": ADAPTER_CONTRACT_VERSION,
            "input_upper_bound_tokens": input_upper_bound_tokens,
            "max_output_tokens": payload["max_tokens"],
            "prepared_payload_canonical_json_sha256": _canonical_sha256(payload),
            "prompt_sha256": hashlib.sha256(SYSTEM_PROMPT.encode("utf-8")).hexdigest(),
            "frozen_local_state_sha256": _canonical_sha256(fields["local_state"]),
            "frozen_fictional_evidence_sha256": _canonical_sha256(fields["fictional_evidence"]),
            "frozen_received_simulated_messages_sha256": _canonical_sha256(
                fields["received_simulated_messages"]),
            "frozen_own_simulated_memory_sha256": _canonical_sha256(
                fields["own_simulated_memory"]),
            "frozen_allowed_citation_ids_sha256": _canonical_sha256(
                fields["allowed_citation_ids"]),
        }

    def decide(self, observation):
        return self.decide_with_audit(observation, None)

    def decide_with_audit(self, observation, record_audit):
        if not self.enable_live:
            raise BailianRequestError(
                "live Bailian calls are disabled pending separate authorization",
                category="live_disabled")
        route = self.config.routes[ROLE_MODELS[observation["persona"]["role"]]]
        payload, input_upper_bound_tokens = self.prepare_request(observation)
        try:
            with self._lock:
                if self._closed:
                    raise BailianConfigurationError("Bailian client is closed")
                if self.calls >= self.max_calls:
                    raise BailianRequestError("explicit Bailian call limit reached",
                                              category="call_limit")
                self.calls += 1
                if self._client is None:
                    self._client = httpx.Client(
                        follow_redirects=False,
                        limits=httpx.Limits(max_connections=128,
                                            max_keepalive_connections=32),
                    )
                client = self._client
            if record_audit is not None:
                record_audit(self._request_audit(payload, input_upper_bound_tokens))
            response = client.post(
                self.config.base_url + "/chat/completions", json=payload,
                headers={"Authorization": "Bearer " + self.config.api_key},
                timeout=30, follow_redirects=False,
            )
            if response.status_code != 200:
                status = response.status_code
                raise BailianRequestError(
                    "Bailian returned a non-success HTTP status; body suppressed",
                    category="http_status",
                    http_status=status if type(status) is int and 100 <= status <= 599 else None)
            body = response.json()
            choice = body["choices"][0]
            finish_reason = choice.get("finish_reason")
            if finish_reason != "stop":
                if finish_reason == "length":
                    failure_type = BailianFinishLengthError
                elif finish_reason == "content_filter":
                    failure_type = BailianFinishContentFilterError
                else:
                    failure_type = BailianFinishOtherError
                raise failure_type("Bailian response did not finish normally",
                                   category="finish_reason")
            content = choice["message"]["content"]
            if not isinstance(content, str):
                raise BailianResponseShapeError("Bailian response content is not text",
                                                category="response_contract")
            decision_data = json.loads(content)
            try:
                decision = AgentDecision.model_validate(decision_data)
            except ValidationError as exc:
                error = BailianDecisionSchemaError(
                    "Bailian decision violates the AgentDecision schema",
                    category="response_contract")
                error.safe_schema_diagnostics = _schema_diagnostics(exc)
                raise error from None
            usage = dict(body["usage"])
            details = usage.get("prompt_tokens_details")
            cached = usage.get("cached_input_tokens", usage.get("prompt_cache_hit_tokens"))
            if cached is None and isinstance(details, dict):
                cached = details.get("cached_tokens", 0)
            usage["cached_input_tokens"] = 0 if cached is None else cached
            if any(type(usage.get(name)) is not int or usage[name] < 0
                   for name in ("prompt_tokens", "completion_tokens", "cached_input_tokens")):
                raise BailianRequestError("Bailian response lacks valid token usage",
                                          category="usage_invalid")
            if usage["cached_input_tokens"] > usage["prompt_tokens"]:
                raise BailianRequestError("Bailian cached token usage is inconsistent",
                                          category="usage_inconsistent")
            actual_model = body["model"]
            if not isinstance(actual_model, str) or not actual_model.strip():
                raise BailianRequestError("Bailian response lacks a model identifier",
                                          category="model_missing")
        except httpx.TimeoutException:
            raise BailianTimeoutError("Bailian request timed out; no retry was attempted",
                                      category="transport") from None
        except httpx.ConnectError:
            raise BailianConnectError("Bailian connection failed; no retry was attempted",
                                      category="transport") from None
        except httpx.ProtocolError:
            raise BailianProtocolError("Bailian protocol failed; no retry was attempted",
                                       category="transport") from None
        except httpx.HTTPError:
            raise BailianTransportError("Bailian transport failed; no retry was attempted",
                                        category="transport") from None
        except BailianConfigurationError:
            raise
        except json.JSONDecodeError:
            raise BailianJSONSyntaxError("Bailian response contains invalid JSON",
                                         category="response_contract") from None
        except (KeyError, IndexError, TypeError, ValueError):
            raise BailianResponseShapeError(
                "Bailian response violates the AgentDecision contract",
                category="response_contract") from None
        return DecisionResult(decision=decision, usage=usage, model=actual_model)
