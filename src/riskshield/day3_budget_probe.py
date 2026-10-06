"""One authorized synthetic DeepSeek call-capacity probe, separate from G3 runs.

Run explicitly with ``python -m riskshield.day3_budget_probe OUTPUT.json``.
No real source material is loaded or sent. This historical one-off probe keeps
its original ¥2.36 authorization; a provider balance delta is audit-only.
"""

import json
import os
import sys
import threading
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

import httpx
from pydantic import ValidationError

from riskshield.day3 import ROLES, SIGNALS
from riskshield.day3_deepseek import (API_URL, MODEL, PROMPT_VERSION,
                                      SYSTEM_PROMPT, Day3DeepSeekBackend)
from riskshield.schemas import AgentDecision


BUDGET_CNY = Decimal("2.36")  # Historical one-off authorization; do not raise retroactively.
RESERVE_CNY = Decimal("0.02")
MAX_OUTPUT_TOKENS = 160
MAX_INPUT_BYTES = 6_000
MAX_REQUESTS = 15_000
CONCURRENCY = 10
MISS_CNY_PER_MILLION = Decimal("2.4")
HIT_CNY_PER_MILLION = Decimal("0.048")
OUTPUT_CNY_PER_MILLION = Decimal("9.6")
HOLIDAY_MISS_CNY_PER_MILLION = Decimal("1.1")  # official ¥1, plus 10% buffer
HOLIDAY_HIT_CNY_PER_MILLION = Decimal("0.022")  # official ¥0.02, plus 10%
HOLIDAY_OUTPUT_CNY_PER_MILLION = Decimal("4.4")  # official ¥4, plus 10%
EVIDENCE_ID = "fictional-service-notice"


class ProbeError(RuntimeError):
    pass


def _balance(key):
    try:
        response = httpx.get(
            "https://api.deepseek.com/user/balance",
            headers={"Authorization": "Bearer " + key}, timeout=15,
            follow_redirects=False,
        )
        if response.status_code != 200:
            return None
        rows = response.json().get("balance_infos", [])
        values = [Decimal(row["total_balance"]) for row in rows
                  if row.get("currency") == "CNY"]
        return values[0] if len(values) == 1 else None
    except (httpx.HTTPError, ValueError, KeyError, TypeError):
        return None


def _write(path, result):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(temporary, path)


class BudgetProbe:
    def __init__(self, api_key, *, budget_cny=BUDGET_CNY, previous=None):
        if not api_key:
            raise ProbeError("DEEPSEEK_API_KEY is not configured")
        self.key = api_key
        self.budget = Decimal(str(budget_cny))
        if not Decimal("0") < self.budget <= BUDGET_CNY:
            raise ProbeError("probe budget exceeds ¥2.36")
        self.sanitizer = Day3DeepSeekBackend(api_key)
        self.lock = threading.Lock()
        self.inflight = Decimal("0")
        self.estimated_peak_cny = Decimal("0")
        self.protected_holiday_cny = Decimal("0")
        self.calls_sent = 0
        self.valid = 0
        self.failures = Counter()
        self.actions = Counter()
        self.models = Counter()
        self.usage = Counter()
        self.halted = False
        if previous:
            if (previous.get("purpose") != "synthetic_call_capacity_not_G3" or
                    Decimal(str(previous.get("budget_cny"))) != self.budget or
                    previous.get("prompt_version") != PROMPT_VERSION or
                    any(name not in {"ValidationError", "InvalidDecision"}
                        for name in previous.get("failures", {}))):
                raise ProbeError("resume metadata does not match this probe")
            self.estimated_peak_cny = Decimal(previous["protected_peak_cny"])
            if "protected_holiday_cny" in previous:
                self.protected_holiday_cny = Decimal(previous["protected_holiday_cny"])
            else:
                used = previous["usage"]
                missing_usage_calls = (int(previous["calls_sent"]) -
                                       int(previous["valid_decisions"]) -
                                       int(previous["failures"].get("InvalidDecision", 0)))
                self.protected_holiday_cny = self._holiday_cost(
                    used.get("prompt_cache_miss_tokens", 0),
                    used.get("prompt_cache_hit_tokens", 0),
                    used.get("completion_tokens", 0)) + RESERVE_CNY * missing_usage_calls
            self.calls_sent = int(previous["calls_sent"])
            self.valid = int(previous["valid_decisions"])
            self.failures.update(previous["failures"])
            self.actions.update(previous["actions"])
            self.models.update(previous["models"])
            self.usage.update(previous["usage"])

    @staticmethod
    def _holiday_cost(miss, hit, completion):
        return ((Decimal(miss) * HOLIDAY_MISS_CNY_PER_MILLION +
                 Decimal(hit) * HOLIDAY_HIT_CNY_PER_MILLION +
                 Decimal(completion) * HOLIDAY_OUTPUT_CNY_PER_MILLION) /
                Decimal(1_000_000))

    def _reserve(self):
        with self.lock:
            if (self.halted or self.calls_sent >= MAX_REQUESTS or
                    self.protected_holiday_cny + self.inflight + RESERVE_CNY > self.budget):
                return False
            self.inflight += RESERVE_CNY
            self.calls_sent += 1
            return True

    def _settle(self, result):
        with self.lock:
            self.inflight -= RESERVE_CNY
            cost = result.get("peak_cost_cny", RESERVE_CNY)
            self.estimated_peak_cny += cost
            self.protected_holiday_cny += result.get("holiday_cost_cny", RESERVE_CNY)
            if result.get("error"):
                self.failures[result["error"]] += 1
                if result["error"] != "InvalidDecision":
                    self.halted = True
            else:
                self.valid += 1
                self.actions[result["action"]] += 1
                self.models[result["model"]] += 1
            self.usage.update(result.get("usage", {}))
            if (cost > RESERVE_CNY or self.protected_holiday_cny > self.budget):
                self.halted = True

    def call(self, observation):
        fields = self.sanitizer._fields(observation)
        user_content = json.dumps(fields, ensure_ascii=False, separators=(",", ":"))
        if len(SYSTEM_PROMPT.encode("utf-8")) + len(user_content.encode("utf-8")) > MAX_INPUT_BYTES:
            raise ProbeError("synthetic prompt exceeds the preflight byte ceiling")
        if not self._reserve():
            return None
        payload = {
            "model": MODEL,
            "messages": [{"role": "system", "content": SYSTEM_PROMPT},
                         {"role": "user", "content": user_content}],
            "response_format": {"type": "json_object"},
            "thinking": {"type": "disabled"},
            "max_tokens": MAX_OUTPUT_TOKENS,
        }
        result = {"error": "UnknownResponse"}
        try:
            response = httpx.post(
                API_URL, json=payload,
                headers={"Authorization": "Bearer " + self.key},
                timeout=30, follow_redirects=False,
            )
            if response.status_code != 200:
                result = {"error": f"HTTP{response.status_code}"}
            else:
                body = response.json()
                usage = body["usage"]
                prompt = usage["prompt_tokens"]
                completion = usage["completion_tokens"]
                total = usage["total_tokens"]
                if (any(type(x) is not int or x < 0 for x in (prompt, completion, total))
                        or prompt + completion != total):
                    raise ProbeError("invalid usage")
                hit = usage.get("prompt_cache_hit_tokens", 0)
                miss = usage.get("prompt_cache_miss_tokens", prompt)
                if (type(hit) is not int or type(miss) is not int or hit < 0
                        or miss < 0 or hit + miss != prompt):
                    hit, miss = 0, prompt
                peak_cost = ((Decimal(miss) * MISS_CNY_PER_MILLION +
                              Decimal(hit) * HIT_CNY_PER_MILLION +
                              Decimal(completion) * OUTPUT_CNY_PER_MILLION) /
                             Decimal(1_000_000))
                result = {"peak_cost_cny": peak_cost,
                          "holiday_cost_cny": self._holiday_cost(miss, hit, completion),
                          "usage": {"prompt_tokens": prompt,
                                    "completion_tokens": completion,
                                    "total_tokens": total,
                                    "prompt_cache_hit_tokens": hit,
                                    "prompt_cache_miss_tokens": miss}}
                if (prompt > MAX_INPUT_BYTES + 512 or completion > MAX_OUTPUT_TOKENS
                        or peak_cost > RESERVE_CNY):
                    result["error"] = "ReservationExceeded"
                elif body["choices"][0].get("finish_reason") != "stop":
                    result["error"] = "IncompleteResponse"
                else:
                    try:
                        decision = AgentDecision.model_validate_json(
                            body["choices"][0]["message"]["content"])
                    except ValidationError:
                        result["error"] = "InvalidDecision"
                    else:
                        allowed = set(fields["allowed_citation_ids"])
                        if (decision.agent_id != observation["agent_id"] or
                                not set(decision.evidence_ids) <= allowed):
                            result["error"] = "InvalidDecision"
                        else:
                            result["action"] = decision.action
                            result["reason"] = decision.reason[:120]
                            result["evidence_ids"] = decision.evidence_ids
                            result["model"] = str(body["model"])[:100]
        except (httpx.HTTPError, ValueError, KeyError, IndexError, TypeError, AttributeError) as exc:
            result = {"error": type(exc).__name__}
        finally:
            self._settle(result)
        return result

    def summary(self):
        with self.lock:
            return {"calls_sent": self.calls_sent, "valid_decisions": self.valid,
                    "failures": dict(self.failures), "actions": dict(self.actions),
                    "models": dict(self.models), "usage": dict(self.usage),
                    "protected_peak_cny": str(self.estimated_peak_cny),
                    "protected_holiday_cny": str(self.protected_holiday_cny),
                    "remaining_protected_cny": str(self.budget - self.protected_holiday_cny),
                    "halted": self.halted}


def _initial_agents():
    return [{"agent_id": f"agent_{i:04d}", "role": ROLES[i % len(ROLES)],
             "emotion": -0.1, "trust": 0.5, "memories": [], "messages": []}
            for i in range(10)]


def _observation(agent, round_number):
    return {
        "run_id": "local-only-budget-probe", "round": round_number,
        "agent_id": agent["agent_id"],
        "persona": {"role": agent["role"], "susceptibility": 0.5, "influence": 0.5},
        "state": {"emotion": agent["emotion"], "trust": agent["trust"]},
        "evidence": [{"evidence_id": EVIDENCE_ID,
                      "title": "Fictional test service is temporarily unavailable",
                      "text": "A fictional insurer reports a temporary test-service interruption."}],
        "messages": agent["messages"][-2:], "memories": agent["memories"][-2:],
        "data_mode": "synthetic", "neighbors": [], "cutoff": "local-only",
    }


def run_probe(path, *, api_key=None, budget_cny=BUDGET_CNY, resume=False,
              enforce_holiday_date=True):
    if (enforce_holiday_date and
            datetime.now(timezone(timedelta(hours=8))).date().isoformat() != "2026-10-05"):
        raise ProbeError("holiday price guard only permits 2026-10-05 Asia/Shanghai")
    output = Path(path)
    previous = json.loads(output.read_text(encoding="utf-8")) if resume else None
    probe = BudgetProbe(api_key or os.getenv("DEEPSEEK_API_KEY", ""),
                        budget_cny=budget_cny, previous=previous)
    starting_balance = _balance(probe.key)
    agents = _initial_agents()
    rounds = int(previous["rounds_started"]) if previous else 0
    full_rounds = int(previous["full_10_agent_rounds"]) if previous else 0
    messages_sent = int(previous["messages_sent"]) if previous else 0
    segments = int(previous.get("segments", 1)) + 1 if previous else 1
    started = time.perf_counter()
    while probe.calls_sent < MAX_REQUESTS and not probe.halted:
        round_number = rounds % 30 + 1
        observations = [_observation(agent, round_number) for agent in agents]
        with ThreadPoolExecutor(max_workers=CONCURRENCY) as pool:
            outcomes = list(pool.map(probe.call, observations))
        sent = sum(outcome is not None for outcome in outcomes)
        if sent == 0:
            break
        full_rounds += int(sent == len(agents))
        next_messages = [[] for _ in agents]
        for index, outcome in enumerate(outcomes):
            if not outcome or outcome.get("error"):
                continue
            agent = agents[index]
            action = outcome["action"]
            agent["memories"].append({
                "memory_id": f"mem_{rounds + 1}_{agent['agent_id']}",
                "content": action + " " + outcome["reason"],
            })
            agent["emotion"] = round(max(-1, min(1, agent["emotion"] + SIGNALS[action] * .5)), 4)
            if action in {"share", "comment", "express_complaint_intent"}:
                recipient = (index + 1) % len(agents)
                next_messages[recipient].append({
                    "message_id": f"msg_{rounds + 1}_{agent['agent_id']}_{agents[recipient]['agent_id']}",
                    "sender_id": agent["agent_id"], "action": action,
                    "content": outcome["reason"], "evidence_ids": [EVIDENCE_ID],
                })
                messages_sent += 1
        for agent, received in zip(agents, next_messages):
            agent["messages"] = received
        rounds += 1
        if round_number == 30:
            agents = _initial_agents()
        if rounds % 10 == 0 or sent < len(agents) or probe.halted:
            result = {"status": "running", "purpose": "synthetic_call_capacity_not_G3",
                      "prompt_version": PROMPT_VERSION, "budget_cny": str(probe.budget),
                      "per_call_reservation_cny": str(RESERVE_CNY),
                      "max_input_bytes": MAX_INPUT_BYTES,
                      "max_output_tokens": MAX_OUTPUT_TOKENS,
                      "concurrency": CONCURRENCY, "rounds_started": rounds,
                      "full_10_agent_rounds": full_rounds,
                      "segments": segments,
                      "messages_sent": messages_sent, **probe.summary()}
            _write(output, result)
            print(f"calls={result['calls_sent']} holiday_guard_cny={result['protected_holiday_cny']}",
                  flush=True)
    final_balance = _balance(probe.key)
    result = {"status": "stopped", "purpose": "synthetic_call_capacity_not_G3",
              "stop_reason": ("provider_or_response_error" if probe.halted else
                              "max_requests_reached" if probe.calls_sent >= MAX_REQUESTS else
                              "next_request_reservation_unavailable"),
              "g3_status": "not_passed",
              "executed_at_utc": datetime.now(timezone.utc).isoformat(),
              "prompt_version": PROMPT_VERSION, "budget_cny": str(probe.budget),
              "per_call_reservation_cny": str(RESERVE_CNY),
              "price_basis": "conservative CNY peak: miss 2.4/M, hit 0.048/M, output 9.6/M",
              "budget_meter": "2026-10-05 statutory-holiday CNY off-peak rates plus 10 percent",
              "max_input_bytes": MAX_INPUT_BYTES,
              "max_output_tokens": MAX_OUTPUT_TOKENS,
              "concurrency": CONCURRENCY, "rounds_started": rounds,
              "full_10_agent_rounds": full_rounds,
              "segments": segments,
              "resume_note": "Simulation state resets between segments; this is a call-capacity probe, not a G3 trajectory." if segments > 1 else None,
              "messages_sent": messages_sent, "elapsed_seconds": round(time.perf_counter() - started, 3),
              "balance_delta_this_segment_cny": (str(starting_balance - final_balance)
                                    if starting_balance is not None and final_balance is not None
                                    else None),
              "balance_precision_note": "Provider balance has cent precision; shared-account activity may affect delta.",
              **probe.summary()}
    _write(output, result)
    return result


if __name__ == "__main__":
    if len(sys.argv) not in (2, 3) or (len(sys.argv) == 3 and sys.argv[2] != "--resume"):
        raise SystemExit("usage: python -m riskshield.day3_budget_probe OUTPUT.json [--resume]")
    summary = run_probe(sys.argv[1], resume=len(sys.argv) == 3)
    print(json.dumps({key: summary[key] for key in
                      ("status", "calls_sent", "valid_decisions", "failures", "usage",
                       "protected_peak_cny", "protected_holiday_cny",
                       "balance_delta_this_segment_cny", "elapsed_seconds")},
                     ensure_ascii=False), flush=True)
