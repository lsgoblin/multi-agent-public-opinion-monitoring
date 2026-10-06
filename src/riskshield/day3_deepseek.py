"""Explicitly authorized, synthetic-only DeepSeek validation for Day 3.

This module is deliberately separate from the API. Importing it never sends a
request; callers must invoke ``run_synthetic_validation`` explicitly.
"""

import json
import os
import re
import threading
import time
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

import httpx

from riskshield.day3 import Day3Simulation, DecisionResult
from riskshield.schemas import AgentDecision, CaseImport
from riskshield.store import Store


MODEL = "deepseek-flash"
API_URL = "https://api.deepseek.com/chat/completions"
PROMPT_VERSION = "day3-synthetic-agent-decision-v2-message-content"
MAX_CALLS = 30
MAX_INPUT_TOKENS = 1_200
MAX_OUTPUT_TOKENS = 160
AUTHORIZED_BUDGET_CNY = Decimal("0.13248")
INPUT_CNY_PER_MILLION = Decimal("2.4")  # $0.30 * 8 CNY/USD
OUTPUT_CNY_PER_MILLION = Decimal("9.6")  # $1.20 * 8 CNY/USD
RESERVATION_CNY = ((Decimal(MAX_INPUT_TOKENS) * INPUT_CNY_PER_MILLION
                    + Decimal(MAX_OUTPUT_TOKENS) * OUTPUT_CNY_PER_MILLION)
                   / Decimal(1_000_000))
CASE_ID = "fictional_northstar_service_day3"
GRAPH_ID = "fictional_northstar_graph_v1"
RECORD_ID = "fictional-service-notice"


class DeepSeekSimulationError(RuntimeError):
    pass


SYSTEM_PROMPT = (
    "You make one runtime decision for one fictional social-simulation agent. "
    "Use only the supplied local state, fictional evidence, received messages, and own memory. "
    "A received_simulated_message was produced by another agent's action in the preceding round; "
    "treat its content as a newly observed social signal, not as verified fact. "
    "If it materially affects the decision, cite its message_id and explain the effect in reason. "
    "Return one JSON object with exactly agent_id, action, evidence_ids, reason. "
    "action must be observe, share, comment, seek_clarification, or express_complaint_intent. "
    "evidence_ids must contain 1-20 IDs from allowed_citation_ids. Keep reason under 120 characters."
)


def _short(value, limit):
    return re.sub(r"\s+", " ", str(value)).strip()[:limit]


class Day3DeepSeekBackend:
    mode = "real_model"
    model = MODEL
    reservation_cny = RESERVATION_CNY
    input_cny_per_million = INPUT_CNY_PER_MILLION
    output_cny_per_million = OUTPUT_CNY_PER_MILLION
    price_version = "deepseek-flash-peak-2026-10-04-cny8-per-usd"

    def __init__(self, api_key: str):
        if not api_key.strip():
            raise DeepSeekSimulationError("DEEPSEEK_API_KEY is not configured")
        self._api_key = api_key
        self._lock = threading.Lock()
        self.calls = 0
        self.provider_usage = {"prompt_tokens": 0, "completion_tokens": 0,
                               "total_tokens": 0}
        self.provider_estimated_cny = Decimal("0")
        self.halted = False

    def _fields(self, observation):
        if observation.get("data_mode") != "synthetic":
            raise DeepSeekSimulationError("Day 3 authorization permits synthetic input only")
        evidence = observation["evidence"][:1]
        messages = observation["messages"][-2:]
        memories = observation["memories"][:2]
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
                "content": _short(item["content"], 100),
            } for item in memories],
        }
        allowed = [item["evidence_id"] for item in evidence]
        allowed.extend(item["message_id"] for item in messages)
        allowed.extend(item["memory_id"] for item in memories)
        fields["allowed_citation_ids"] = allowed
        serialized = json.dumps(fields, ensure_ascii=False, separators=(",", ":"))
        if len(serialized) > 1_800:
            raise DeepSeekSimulationError("sanitized outbound payload exceeds its character limit")
        return fields

    def decide(self, observation):
        fields = self._fields(observation)
        with self._lock:
            if self.halted:
                raise DeepSeekSimulationError("backend halted after an authorization boundary failure")
            if self.calls >= MAX_CALLS:
                raise DeepSeekSimulationError("authorized 30-call limit reached")
            self.calls += 1
        payload = {
            "model": MODEL,
            "messages": [
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": json.dumps(
                    fields, ensure_ascii=False, separators=(",", ":"))},
            ],
            "response_format": {"type": "json_object"},
            "thinking": {"type": "disabled"},
            "max_tokens": MAX_OUTPUT_TOKENS,
        }
        started = time.perf_counter()
        try:
            response = httpx.post(
                API_URL, json=payload,
                headers={"Authorization": "Bearer " + self._api_key},
                timeout=30, follow_redirects=False,
            )
            if response.status_code != 200:
                raise DeepSeekSimulationError(
                    f"DeepSeek returned HTTP {response.status_code}; body suppressed")
            body = response.json()
            choice = body["choices"][0]
            if choice.get("finish_reason") != "stop":
                raise DeepSeekSimulationError("DeepSeek response did not finish normally")
            message = choice["message"]
            if message.get("reasoning_content"):
                raise DeepSeekSimulationError("DeepSeek returned reasoning content")
            usage = body["usage"]
            if not all(type(usage.get(name)) is int and usage[name] >= 0
                       for name in ("prompt_tokens", "completion_tokens", "total_tokens")):
                raise DeepSeekSimulationError("DeepSeek response lacks valid token usage")
            if usage["prompt_tokens"] + usage["completion_tokens"] != usage["total_tokens"]:
                raise DeepSeekSimulationError("DeepSeek token usage is inconsistent")
            actual_model = body["model"]
            if not isinstance(actual_model, str) or not actual_model.strip():
                raise DeepSeekSimulationError("DeepSeek response lacks a model identifier")
            call_cost = ((Decimal(usage["prompt_tokens"]) * INPUT_CNY_PER_MILLION
                          + Decimal(usage["completion_tokens"]) * OUTPUT_CNY_PER_MILLION)
                         / Decimal(1_000_000))
            with self._lock:
                for name in self.provider_usage:
                    self.provider_usage[name] += usage[name]
                self.provider_estimated_cny += call_cost
                if (usage["prompt_tokens"] > MAX_INPUT_TOKENS
                        or usage["completion_tokens"] > MAX_OUTPUT_TOKENS
                        or self.provider_estimated_cny > AUTHORIZED_BUDGET_CNY):
                    self.halted = True
            if self.halted:
                raise DeepSeekSimulationError("provider usage exceeded the authorized boundary")
            decision = AgentDecision.model_validate_json(message["content"])
        except httpx.HTTPError:
            raise DeepSeekSimulationError("DeepSeek request failed; no retry was attempted") from None
        except (KeyError, IndexError, TypeError, ValueError):
            raise DeepSeekSimulationError("DeepSeek response violates the AgentDecision contract") from None
        return DecisionResult(decision=decision, usage={
            "prompt_tokens": usage["prompt_tokens"],
            "completion_tokens": usage["completion_tokens"],
            "total_tokens": usage["total_tokens"],
            "elapsed_seconds": round(time.perf_counter() - started, 3),
        }, model=actual_model)


def prepare_synthetic_case(store: Store):
    case = CaseImport.model_validate({
        "case_id": CASE_ID,
        "title": "Fictional Northstar Insurance test service interruption",
        "scope": "Synthetic Day 3 model validation only",
        "cutoff": "2026-01-01T10:30:00+08:00",
        "cutoff_basis": "Synthetic notice available at 10:00 plus 30 minutes.",
        "data_mode": "synthetic",
        "version": "day3-synthetic-v1",
        "records": [{
            "record_id": RECORD_ID, "channel": "official_status",
            "source_url": "https://example.com/fictional-northstar-status",
            "publisher": "Fictional Northstar Insurance",
            "title": "Fictional test service is temporarily unavailable",
            "summary": ("In a fictional exercise, Northstar Insurance reports a temporary "
                        "test-service interruption and says technicians are investigating."),
            "content_kind": "human_summary", "data_mode": "synthetic",
            "published_at": "2026-01-01T10:00:00+08:00",
            "available_at": "2026-01-01T10:00:00+08:00",
            "collected_at": "2026-01-01T10:01:00+08:00",
            "availability_basis": "Synthetic fixture created for authorized validation.",
            "historical_integrity": "synthetic", "acquisition_method": "synthetic_fixture",
            "role": "input_candidate", "limitations": ["Fictional; not a real company or event."],
        }],
    })
    store.import_case(case)
    snapshot = store.snapshot(CASE_ID)
    source = snapshot["records"][0]
    body = {
        "case_id": CASE_ID, "cutoff": snapshot["cutoff"],
        "source_version": snapshot["version"], "nodes": [], "edges": [],
        "claims": [{
            "claim_id": "claim:" + RECORD_ID, "record_id": RECORD_ID,
            "text": source["summary"], "title": source["title"],
            "source_url": source["source_url"], "available_at": source["available_at"],
            "source_node_id": "source:" + RECORD_ID,
            "publisher_node_id": "publisher:fictional-northstar",
            "observation_ids": [], "observation_node_ids": [],
            "evidence_adapters": ["synthetic_fixture"],
        }],
        "excluded_counts": snapshot["excluded_counts"],
    }
    with store.connect() as db:
        db.execute("INSERT OR IGNORE INTO graphs VALUES (?, ?, ?, ?, ?, ?)", (
            GRAPH_ID, CASE_ID, snapshot["cutoff"], snapshot["version"],
            datetime.now(timezone.utc).isoformat(), json.dumps(body, ensure_ascii=False)))


def run_synthetic_validation(db_path: str | Path, *, api_key: str | None = None):
    key = api_key or os.getenv("DEEPSEEK_API_KEY", "")
    store = Store(db_path)
    engine = Day3Simulation(store)
    prepare_synthetic_case(store)
    backend = Day3DeepSeekBackend(key)
    run_id = engine.create_run(
        CASE_ID, GRAPH_ID, agent_count=10, rounds=3, seed=20261005,
        concurrency=3, budget_cny=AUTHORIZED_BUDGET_CNY,
    )
    result = engine.advance(run_id, backend)
    actions = engine.trajectory(run_id)
    return {
        "authorization": "synthetic_only_10_agents_3_rounds_30_calls",
        "prompt_version": PROMPT_VERSION,
        "requested_model": MODEL,
        "data_mode": "synthetic",
        "outbound_fields": ["anonymous agent role and parameters", "local state",
                            "fictional evidence", "received simulated messages",
                            "own simulated memory", "round", "action JSON constraint"],
        "calls_attempted": backend.calls,
        "provider_usage": backend.provider_usage,
        "provider_estimated_peak_cny": str(backend.provider_estimated_cny),
        "authorized_budget_cny": str(AUTHORIZED_BUDGET_CNY),
        "models": sorted({row["model"] for row in actions if row["model"]}),
        "errors": sorted({row["error"] for row in actions if row["error"]}),
        "run": result,
    }
