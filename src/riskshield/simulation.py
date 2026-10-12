"""Day 3 synchronous social simulation. A decision backend is supplied by the caller.

The engine never sends data to a model on its own. A test backend proves the
engineering path, while a real model run needs a separately authorized backend.
"""

import json
import hashlib
import math
import queue
import random
import re
import threading
import time
import uuid
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Protocol

from pydantic import BaseModel, ConfigDict, Field

from riskshield.evidence import EvidencePipeline
from riskshield.schemas import AgentDecision
from riskshield.store import Store


ROLES = ("netizen", "customer", "media", "kol", "regulator")
ROLE_QUERIES = {
    "netizen": "event public service",
    "customer": "customer service access",
    "media": "event statement update",
    "kol": "public impact response",
    "regulator": "service security disclosure",
}
SIGNALS = {"observe": 0.0, "share": -0.06, "comment": -0.04,
           "seek_clarification": 0.03, "express_complaint_intent": -0.12}
# Exhaustive and exclusive billing outcomes for one request opportunity. A request
# that was dispatched without usable usage is deliberately NOT reported as free.
BILLING_STATES = ("not_sent", "released", "settled", "unsettled_reserved")
MAX_TASK_BUDGET_CNY = Decimal("30.00")
DEFAULT_TASK_BUDGET_CNY = Decimal("5.00")


class SimulationError(ValueError):
    pass


class DaemonCallPool:
    """A bounded worker pool whose threads can never hold the process open.

    ``concurrent.futures.ThreadPoolExecutor`` is unsuitable for work that may never
    return: its workers are non-daemon and an interpreter exit hook joins them, so a
    single hung request keeps the whole process alive even after the engine has stopped
    waiting for it. That is exactly the failure this pool exists to avoid.

    The pool gives the engine a bounded wait (``collect`` with a timeout) and rejects new
    work past that bound (``stop_submitting``), while a stuck worker is left behind
    harmlessly: it is a daemon thread and its late result is queued but never read,
    because the request it belonged to has already been recorded.
    """

    def __init__(self, max_workers: int):
        if type(max_workers) is not int or max_workers < 1:
            raise SimulationError("pool size must be a positive integer")
        self.max_workers = max_workers
        # Two queues on purpose: work goes one way, results come back the other. A single
        # shared queue would let the collecting thread steal a work item that no worker
        # had picked up yet.
        self._submitted: queue.Queue = queue.Queue()
        self._results: queue.Queue = queue.Queue()
        self._lock = threading.Lock()
        self._submitting = True
        self._stop = object()  # unique sentinel: never confusable with a result
        self._threads = [
            threading.Thread(target=self._worker, daemon=True,
                             name=f"riskshield-decision-{index}")
            for index in range(max_workers)
        ]
        for thread in self._threads:
            thread.start()

    def _worker(self):
        while True:
            item = self._submitted.get()
            if item is self._stop:
                return
            identifier, function, arguments = item
            try:
                self._results.put((identifier, (function(*arguments),)))
            except BaseException as exc:  # reported to the caller, never swallowed
                self._results.put((identifier, exc))

    def submit(self, identifier, function, *arguments):
        """Queue one call. Returns False once the pool has stopped accepting work."""
        with self._lock:
            if not self._submitting:
                return False
            self._submitted.put((identifier, function, arguments))
            return True

    def collect(self, timeout):
        """Return ``(identifier, result_or_exception)``, or None if nothing arrived.

        The wait is bounded: this method never blocks longer than ``timeout``.
        """
        try:
            item = (self._results.get(timeout=timeout) if timeout is not None
                    else self._results.get())
        except queue.Empty:
            return None
        identifier, payload = item
        if isinstance(payload, BaseException):
            return identifier, payload
        return identifier, payload[0]

    def stop_submitting(self):
        with self._lock:
            self._submitting = False

    def shutdown(self):
        """Signal idle workers to stop. Never joins, so a stuck worker cannot block."""
        self.stop_submitting()
        for _ in self._threads:
            self._submitted.put(self._stop)


class SafeDecisionError(RuntimeError):
    """A backend error with a bounded diagnostic category, never response text."""

    def __init__(self, message: str, *, category: str, http_status: int | None = None):
        super().__init__(message)
        self.safe_category = category
        self.safe_http_status = http_status


SAFE_DECISION_ERROR_CATEGORIES = frozenset({
    "synthetic_only", "prompt_bound", "live_disabled", "call_limit",
    "http_status", "transport", "finish_reason", "response_contract",
    "usage_invalid", "usage_inconsistent", "model_missing",
})


def _persisted_error(exc: Exception) -> str:
    """Persist only whitelisted backend diagnostics; all other messages are discarded."""
    error_type = type(exc).__name__
    if isinstance(exc, SafeDecisionError):
        category = exc.safe_category
        if type(category) is str and category in SAFE_DECISION_ERROR_CATEGORIES:
            code = f"{error_type}:{category}"
            status = exc.safe_http_status
            if category == "http_status" and type(status) is int and 100 <= status <= 599:
                code += f":{status}"
            return code
    return error_type


_AUDIT_HASH_FIELDS = (
    "prepared_payload_canonical_json_sha256", "prompt_sha256",
    "frozen_local_state_sha256", "frozen_fictional_evidence_sha256",
    "frozen_received_simulated_messages_sha256",
    "frozen_own_simulated_memory_sha256", "frozen_allowed_citation_ids_sha256",
)
_SCHEMA_FIELDS = frozenset({"agent_id", "action", "evidence_ids", "reason", "other"})
_SCHEMA_TYPES = frozenset({"missing", "literal_error", "string_type", "list_type",
                           "too_short", "too_long", "string_too_short",
                           "string_too_long", "string_pattern_mismatch",
                           "value_error", "other"})


def _safe_request_audit(value):
    """Accept only fixed metadata and digests, never backend supplied text."""
    if not isinstance(value, dict):
        return None
    if (value.get("stage") != "prepared_for_dispatch" or
            value.get("hash_basis") !=
            "canonical_json_sorted_keys_utf8_of_prepared_payload_object" or
            value.get("adapter_version") != "day3-bailian-synthetic-v4"):
        return None
    if any(type(value.get(name)) is not str or
           re.fullmatch(r"[0-9a-f]{64}", value[name]) is None
           for name in _AUDIT_HASH_FIELDS):
        return None
    if (type(value.get("input_upper_bound_tokens")) is not int or
            not 1 <= value["input_upper_bound_tokens"] <= 8192 or
            type(value.get("max_output_tokens")) is not int or
            not 1 <= value["max_output_tokens"] <= 8192):
        return None
    return {"stage": "prepared_for_dispatch",
            "hash_basis": "canonical_json_sorted_keys_utf8_of_prepared_payload_object",
            "adapter_version": "day3-bailian-synthetic-v4",
            "input_upper_bound_tokens": value["input_upper_bound_tokens"],
            "max_output_tokens": value["max_output_tokens"],
            **{name: value[name] for name in _AUDIT_HASH_FIELDS}}


def _safe_schema_diagnostics(exc):
    if (not isinstance(exc, SafeDecisionError) or
            type(exc).__name__ != "BailianDecisionSchemaError" or
            exc.safe_category != "response_contract"):
        return None
    value = getattr(exc, "safe_schema_diagnostics", None)
    if not isinstance(value, dict):
        return None
    count, groups = value.get("error_count"), value.get("groups")
    if (type(count) is not int or not 0 <= count <= 1000 or
            not isinstance(groups, list) or len(groups) > 100):
        return None
    safe_groups = []
    for item in groups:
        if not isinstance(item, dict):
            return None
        field, kind, n = item.get("field"), item.get("type"), item.get("count")
        if (type(field) is not str or field not in _SCHEMA_FIELDS or
                type(kind) is not str or kind not in _SCHEMA_TYPES or
                type(n) is not int or not 1 <= n <= 1000):
            return None
        safe_groups.append({"field": field, "type": kind, "count": n})
    if sum(item["count"] for item in safe_groups) != count:
        return None
    return {"error_count": count, "groups": safe_groups}


class SimulationCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    case_id: str = Field(pattern=r"^[a-zA-Z0-9_-]+$", max_length=100)
    graph_id: str = Field(pattern=r"^[a-zA-Z0-9_-]+$", max_length=100)
    agent_count: int = Field(ge=1, le=500)
    rounds: int = Field(ge=1, le=30)
    seed: int = 1
    role_offset: int = Field(default=0, ge=0, lt=len(ROLES))
    concurrency: int = Field(default=8, ge=1, le=128)
    budget_cny: Decimal = Field(default=DEFAULT_TASK_BUDGET_CNY, gt=0,
                                le=MAX_TASK_BUDGET_CNY)


@dataclass(frozen=True)
class DecisionResult:
    decision: AgentDecision
    usage: dict | None = None
    model: str = "unknown"


@dataclass(frozen=True)
class DecisionPricing:
    requested_model: str
    price_version: str
    reservation_cny: Decimal
    input_cny_per_million: Decimal
    cached_input_cny_per_million: Decimal
    output_cny_per_million: Decimal


class DecisionBackend(Protocol):
    mode: str  # "test_substitute" or "real_model"

    def pricing(self, observation: dict) -> DecisionPricing: ...
    def decide(self, observation: dict) -> DecisionResult: ...


def _json(value):
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def _canonical_sha256(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                     separators=(",", ":")).encode("utf-8")).hexdigest()


def _clip(value, lo, hi):
    return max(lo, min(hi, round(value, 4)))


class Day3Simulation:
    def __init__(self, store: Store):
        self.store = store
        self.day2 = EvidencePipeline(store)  # Compatibility attribute used by archived audit tooling.
        with store.connect() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS simulation_runs (
                    run_id TEXT PRIMARY KEY, case_id TEXT NOT NULL, graph_id TEXT NOT NULL,
                    config TEXT NOT NULL, status TEXT NOT NULL, completed_rounds INTEGER NOT NULL,
                    cost_cny TEXT NOT NULL, usage TEXT NOT NULL, created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS simulation_agents (
                    run_id TEXT NOT NULL, agent_id TEXT NOT NULL, persona TEXT NOT NULL,
                    neighbors TEXT NOT NULL, state TEXT NOT NULL,
                    PRIMARY KEY (run_id, agent_id)
                );
                CREATE TABLE IF NOT EXISTS simulation_memories (
                    run_id TEXT NOT NULL, agent_id TEXT NOT NULL, memory_id TEXT NOT NULL,
                    round_number INTEGER NOT NULL, content TEXT NOT NULL, refs TEXT NOT NULL,
                    PRIMARY KEY (run_id, agent_id, memory_id)
                );
                CREATE TABLE IF NOT EXISTS simulation_messages (
                    run_id TEXT NOT NULL, message_id TEXT NOT NULL, round_number INTEGER NOT NULL,
                    sender_id TEXT NOT NULL, recipient_id TEXT NOT NULL, action TEXT NOT NULL,
                    evidence_ids TEXT NOT NULL, content TEXT NOT NULL,
                    PRIMARY KEY (run_id, message_id)
                );
                CREATE INDEX IF NOT EXISTS messages_inbox ON simulation_messages
                    (run_id, recipient_id, round_number);
                CREATE TABLE IF NOT EXISTS simulation_actions (
                    run_id TEXT NOT NULL, round_number INTEGER NOT NULL, agent_id TEXT NOT NULL,
                    status TEXT NOT NULL, observation_refs TEXT NOT NULL, decision TEXT,
                    usage TEXT, requested_model TEXT, model TEXT, price_version TEXT,
                    reserved_cny TEXT NOT NULL DEFAULT '0', cost_cny TEXT NOT NULL, error TEXT,
                    request_audit TEXT, schema_diagnostics TEXT,
                    PRIMARY KEY (run_id, round_number, agent_id)
                );
                CREATE TABLE IF NOT EXISTS simulation_rounds (
                    run_id TEXT NOT NULL, round_number INTEGER NOT NULL, stats TEXT NOT NULL,
                    PRIMARY KEY (run_id, round_number)
                );
                CREATE TABLE IF NOT EXISTS simulation_request_metrics (
                    run_id TEXT NOT NULL, round_number INTEGER NOT NULL, agent_id TEXT NOT NULL,
                    requested_model TEXT NOT NULL, queued_at TEXT, started_at TEXT, ended_at TEXT,
                    queue_seconds REAL, backend_seconds REAL, validation_seconds REAL,
                    request_elapsed_seconds REAL,
                    status TEXT NOT NULL, failure_category TEXT, dispatch_evidence TEXT NOT NULL,
                    usage_json TEXT, cost_cny TEXT NOT NULL, usage_unknown INTEGER NOT NULL,
                    PRIMARY KEY (run_id, round_number, agent_id)
                );
                CREATE TABLE IF NOT EXISTS simulation_performance_calls (
                    call_id INTEGER PRIMARY KEY AUTOINCREMENT, run_id TEXT NOT NULL,
                    started_at TEXT NOT NULL, ended_at TEXT NOT NULL, wall_seconds REAL NOT NULL,
                    dispatched_requests INTEGER NOT NULL, valid_decisions INTEGER NOT NULL,
                    stop_reason TEXT
                );
            """)
            message_columns = {row["name"] for row in
                               db.execute("PRAGMA table_info(simulation_messages)")}
            if "content" not in message_columns:
                db.execute("ALTER TABLE simulation_messages ADD COLUMN content TEXT NOT NULL DEFAULT ''")
            action_columns = {row["name"] for row in
                              db.execute("PRAGMA table_info(simulation_actions)")}
            for name, declaration in (
                ("requested_model", "TEXT"), ("price_version", "TEXT"),
                ("reserved_cny", "TEXT NOT NULL DEFAULT '0'"),
                ("request_audit", "TEXT"), ("schema_diagnostics", "TEXT"),
                ("observation_sha256", "TEXT"),
            ):
                if name not in action_columns:
                    db.execute(f"ALTER TABLE simulation_actions ADD COLUMN {name} {declaration}")
            metric_columns = {row["name"] for row in
                              db.execute("PRAGMA table_info(simulation_request_metrics)")}
            if "request_elapsed_seconds" not in metric_columns:
                db.execute("ALTER TABLE simulation_request_metrics "
                           "ADD COLUMN request_elapsed_seconds REAL")

    def create_run(self, case_id: str, graph_id: str, *, agent_count: int,
                   rounds: int, seed: int = 1, concurrency: int = 8,
                   budget_cny: Decimal = DEFAULT_TASK_BUDGET_CNY,
                   role_offset: int = 0) -> str:
        if not (1 <= agent_count <= 500 and 1 <= rounds <= 30 and
                1 <= concurrency <= 128 and 0 <= role_offset < len(ROLES)):
            raise SimulationError(
                "agent_count, rounds, concurrency or role_offset outside Day 3 limits")
        budget_cny = Decimal(str(budget_cny))
        if not Decimal("0") < budget_cny <= MAX_TASK_BUDGET_CNY:
            raise SimulationError("single-run budget must be within ¥30.00")
        snapshot = self.store.snapshot(case_id)
        graph = self.day2.graph(graph_id)
        if (graph["case_id"] != case_id or graph["cutoff"] != snapshot["cutoff"]
                or graph["source_version"] != snapshot["version"]):
            raise SimulationError("graph does not match the case cutoff and version")
        eligible = {r["record_id"] for r in snapshot["records"]}
        if not graph["claims"] or any(c["record_id"] not in eligible or
            datetime.fromisoformat(c["available_at"]) > datetime.fromisoformat(snapshot["cutoff"])
            for c in graph["claims"]):
            raise SimulationError("graph contains no eligible claims or future material")
        run_id = "run_" + uuid.uuid4().hex[:20]
        rng = random.Random(seed)
        agents = []
        for i in range(agent_count):
            agent_id = f"agent_{i:04d}"
            role = ROLES[(i + role_offset) % len(ROLES)]
            persona = {"role": role, "focus": ROLE_QUERIES[role],
                       "susceptibility": round(rng.uniform(0.25, 0.9), 3),
                       "influence": round(rng.uniform(0.2, 0.9), 3)}
            neighbors = sorted({f"agent_{(i + j) % agent_count:04d}"
                                for j in (1, 2, 5) if (i + j) % agent_count != i})
            state = {"emotion": round(rng.uniform(-0.2, 0.2), 3),
                     "trust": round(rng.uniform(0.35, 0.8), 3)}
            agents.append((run_id, agent_id, _json(persona), _json(neighbors), _json(state)))
        config = {"agent_count": agent_count, "rounds": rounds, "seed": seed,
                  "role_offset": role_offset, "concurrency": concurrency,
                  "budget_cny": str(budget_cny),
                  "cutoff": snapshot["cutoff"], "source_version": snapshot["version"],
                  "data_mode": snapshot["data_mode"]}
        with self.store.connect() as db:
            db.execute("INSERT INTO simulation_runs VALUES (?, ?, ?, ?, 'created', 0, '0', ?, ?)",
                       (run_id, case_id, graph_id, _json(config), _json({"prompt_tokens": 0,
                        "cached_input_tokens": 0, "completion_tokens": 0,
                        "unknown_usage_calls": 0, "by_model": {}}),
                        datetime.now().astimezone().isoformat()))
            db.executemany("INSERT INTO simulation_agents VALUES (?, ?, ?, ?, ?)", agents)
        return run_id

    def _run_row(self, run_id):
        with self.store.connect() as db:
            row = db.execute("SELECT * FROM simulation_runs WHERE run_id=?", (run_id,)).fetchone()
        if row is None:
            raise KeyError(run_id)
        return dict(row)

    def _agents(self, run_id):
        with self.store.connect() as db:
            rows = db.execute("SELECT * FROM simulation_agents WHERE run_id=? ORDER BY agent_id",
                              (run_id,)).fetchall()
        return [{"agent_id": r["agent_id"], "persona": json.loads(r["persona"]),
                 "neighbors": json.loads(r["neighbors"]), "state": json.loads(r["state"])}
                for r in rows]

    def _context(self, run, agents, round_number, *, phase_timings=None):
        """Freeze every observation before any decision is submitted."""
        phase_started = time.perf_counter() if phase_timings is not None else None
        graph = self.day2.graph(run["graph_id"])
        cutoff = datetime.fromisoformat(graph["cutoff"])
        run_config = json.loads(run["config"])
        if phase_timings is not None:
            phase_timings["graph_read_seconds"] = time.perf_counter() - phase_started
            phase_started = time.perf_counter()
        with self.store.connect() as db:
            inbox = [dict(r) for r in db.execute(
                "SELECT * FROM simulation_messages WHERE run_id=? AND round_number=?",
                (run["run_id"], round_number - 1))]
            if phase_timings is not None:
                phase_timings["message_read_seconds"] = (
                    time.perf_counter() - phase_started)
                phase_started = time.perf_counter()
            memories = [dict(r) for r in db.execute(
                "SELECT * FROM simulation_memories WHERE run_id=? AND round_number<?",
                (run["run_id"], round_number))]
        if phase_timings is not None:
            phase_timings["memory_read_seconds"] = time.perf_counter() - phase_started
            phase_started = time.perf_counter()
        inbox_by_agent, memories_by_agent = {}, {}
        for msg in inbox:
            inbox_by_agent.setdefault(msg["recipient_id"], []).append({
                "message_id": msg["message_id"], "sender_id": msg["sender_id"],
                "action": msg["action"], "evidence_ids": json.loads(msg["evidence_ids"]),
                "content": msg["content"]})
        for memory in memories:
            memories_by_agent.setdefault(memory["agent_id"], []).append({
                "memory_id": memory["memory_id"], "round": memory["round_number"],
                "content": memory["content"], "_refs_json": memory["refs"]})
        observations = []
        for agent in agents:
            query = agent["persona"]["focus"] + " " + run["case_id"].replace("_", " ")
            tokens = [token.lower() for token in re.findall(
                r"[a-zA-Z0-9]+|[\u4e00-\u9fff]+", query) if len(token) > 2]
            hits = []
            for claim in graph["claims"]:
                score = sum(token in (claim["title"] + " " + claim["text"]).lower()
                            for token in tokens)
                if score and datetime.fromisoformat(claim["available_at"]) <= cutoff:
                    hits.append(claim | {"score": score})
            hits.sort(key=lambda item: (-item["score"], item["record_id"]))
            # The fallback is still a bounded, eligible graph claim, never a raw case record.
            if not hits:
                hits = sorted(graph["claims"], key=lambda h: h["record_id"])[:1]
            evidence = [{"evidence_id": h["record_id"], "claim_id": h["claim_id"],
                         "title": h["title"], "text": h["text"],
                         "available_at": h["available_at"]} for h in hits[:2]]
            own_memories = memories_by_agent.get(agent["agent_id"], [])
            # Recency and simple role relevance; all retrieval stays inside run+agent.
            focus = set(agent["persona"]["focus"].lower().split())
            own_memories.sort(key=lambda m: (len(focus.intersection(m["content"].lower().split())),
                                             m["round"]), reverse=True)
            selected_memories = own_memories[:8]
            for memory in selected_memories:
                memory["refs"] = json.loads(memory.pop("_refs_json"))
            observations.append({"run_id": run["run_id"], "round": round_number,
                                 "agent_id": agent["agent_id"], "persona": agent["persona"],
                                 "state": dict(agent["state"]), "neighbors": agent["neighbors"],
                                 "evidence": evidence, "messages": inbox_by_agent.get(agent["agent_id"], []),
                                 "memories": selected_memories, "cutoff": graph["cutoff"],
                                 "data_mode": run_config["data_mode"]})
        if phase_timings is not None:
            phase_timings["observation_build_seconds"] = time.perf_counter() - phase_started
        return observations

    @staticmethod
    def _pricing(backend, observation):
        """Return one validated quote while supporting the historical scalar backend."""
        provider = getattr(backend, "pricing", None)
        if callable(provider):
            raw = provider(observation)
        else:
            input_rate = Decimal(str(getattr(backend, "input_cny_per_million", 0)))
            raw = DecisionPricing(
                requested_model=str(getattr(backend, "model", backend.__class__.__name__)),
                price_version=str(getattr(backend, "price_version", "unspecified")),
                reservation_cny=Decimal(str(getattr(backend, "reservation_cny", 0))),
                input_cny_per_million=input_rate,
                cached_input_cny_per_million=Decimal(str(
                    getattr(backend, "cached_input_cny_per_million", input_rate))),
                output_cny_per_million=Decimal(str(
                    getattr(backend, "output_cny_per_million", 0))),
            )
        try:
            quote = DecisionPricing(
                requested_model=str(raw.requested_model).strip()[:100],
                price_version=str(raw.price_version).strip()[:200],
                reservation_cny=Decimal(str(raw.reservation_cny)),
                input_cny_per_million=Decimal(str(raw.input_cny_per_million)),
                cached_input_cny_per_million=Decimal(str(
                    raw.cached_input_cny_per_million)),
                output_cny_per_million=Decimal(str(raw.output_cny_per_million)),
            )
        except (AttributeError, ValueError, TypeError):
            raise SimulationError("backend returned invalid pricing") from None
        rates = (quote.input_cny_per_million, quote.cached_input_cny_per_million,
                 quote.output_cny_per_million)
        if (not quote.requested_model or not quote.price_version or
                quote.reservation_cny < 0 or any(rate < 0 for rate in rates)):
            raise SimulationError("backend returned invalid pricing")
        if backend.mode == "real_model" and (quote.reservation_cny <= 0 or
                                               any(rate <= 0 for rate in rates)):
            raise SimulationError("real model requires positive prices and reservation")
        return quote

    @staticmethod
    def _cost(quote, usage):
        if usage is None or not isinstance(usage, dict):
            return quote.reservation_cny, None
        normalized = dict(usage)
        details = normalized.get("prompt_tokens_details")
        cached = normalized.get("cached_input_tokens",
                                normalized.get("prompt_cache_hit_tokens"))
        if cached is None and isinstance(details, dict):
            cached = details.get("cached_tokens", 0)
        if cached is None:
            cached = 0
        prompt = normalized.get("prompt_tokens")
        completion = normalized.get("completion_tokens")
        if (type(prompt) is not int or type(completion) is not int or
                type(cached) is not int or min(prompt, completion, cached) < 0 or
                cached > prompt):
            return quote.reservation_cny, None
        normalized["cached_input_tokens"] = cached
        uncached = prompt - cached
        cost = ((Decimal(uncached) * quote.input_cny_per_million +
                 Decimal(cached) * quote.cached_input_cny_per_million +
                 Decimal(completion) * quote.output_cny_per_million)
                / Decimal(1_000_000))
        return max(cost, Decimal("0")), normalized

    def _round_stats(self, outcomes, *, config, round_number, failure_policy,
                     final_stop_reason, configured_agents, state_by_id,
                     message_count, abandoned):
        """One round's exclusive accounting, billing states and opportunity buckets.

        ``abandoned`` marks a round that stopped before every agent produced an outcome.
        Such a round still owns its accounting: requests that were submitted are valid or
        failed, the remainder of *this* round is ``not_dispatched_current_round``, and
        rounds strictly after it are ``not_started_future_rounds``. Those four buckets are
        exclusive and sum to the configured task total, which the persisted action count
        alone can never show.
        """
        considered = list(outcomes)
        valid_count = sum(o["status"] == "valid" for o in considered)
        failed_count = sum(o["status"] == "failed" for o in considered)
        stranded = sum(o["status"] == "not_dispatched" for o in considered)
        # A request we stopped waiting for. It was dispatched, so it is neither a failed
        # decision nor an unsent one, and its usage is unknown, so it is not free.
        timed_out = sum(o["status"] == "deadline_exceeded" for o in considered)
        # An abandoned round can also hold opportunities that produced no outcome at all.
        # They belong to this round, not to a round the run never reached.
        unresolved_current_round = max(0, configured_agents - len(considered))
        failure_categories = {}
        for outcome in considered:
            if outcome["status"] != "failed":
                continue
            category = outcome["failure_category"] or "unknown"
            failure_categories[category] = failure_categories.get(category, 0) + 1
        cost_by_model, reserved_by_model = {}, {}
        for outcome in considered:
            model = outcome["requested_model"]
            cost_by_model[model] = str(
                Decimal(cost_by_model.get(model, "0")) + outcome["cost"])
            reserved_by_model[model] = str(
                Decimal(reserved_by_model.get(model, "0")) + outcome["reserved"])
        billing_states = {state: 0 for state in BILLING_STATES}
        cost_by_billing_state = {state: "0" for state in BILLING_STATES}
        reservation_by_status = {"reserved": "0", "released": "0"}
        for outcome in considered:
            state, settled = self._billing_status(outcome)
            billing_states[state] += 1
            cost_by_billing_state[state] = str(
                Decimal(cost_by_billing_state[state]) + settled)
            if state == "released":
                reservation_by_status["released"] = str(
                    Decimal(reservation_by_status["released"]) + outcome["reserved"])
            elif state in {"settled", "unsettled_reserved"}:
                reservation_by_status["reserved"] = str(
                    Decimal(reservation_by_status["reserved"]) + outcome["reserved"])
        total_opportunities = config["agent_count"] * config["rounds"]
        not_started = max(0, config["agent_count"] * (config["rounds"] - round_number))
        current_round_stranded = stranded + unresolved_current_round
        return {
            "configured_agents": configured_agents,
            "decision_requests": configured_agents,
            "model_decision_agents": configured_agents - current_round_stranded,
            "dispatched_requests": configured_agents - current_round_stranded,
            "not_dispatched_requests": stranded,
            "deadline_exceeded_requests": timed_out,
            "not_dispatched_current_round": current_round_stranded,
            # Everything after this round that no agent will ever be offered. For a
            # completed round that is exactly the later rounds; for an abandoned round it
            # also covers the rounds the run will never record.
            "not_started_future_rounds": not_started,
            "not_started_rounds_count": max(0, config["rounds"] - round_number),
            "task_opportunities_total": total_opportunities,
            "task_opportunities_reconciled": (
                valid_count + failed_count + timed_out +
                current_round_stranded + not_started),
            "round_abandoned": abandoned,
            "billing": {
                "states": billing_states,
                "cost_by_state": cost_by_billing_state,
                "reservation": reservation_by_status,
                "note": ("unsettled_reserved covers both a dispatched request whose usage "
                         "is unknown and a request we stopped waiting for; either way the "
                         "reservation is kept as the cost rather than reported as free. "
                         "not_sent/released cost nothing."),
            },
            "local_state_update_agents": len(state_by_id),
            "valid_decisions": valid_count,
            "active_decisions": sum(
                o["decision"] is not None and
                o["decision"]["action"] != "observe" for o in considered),
            "failed_decisions": failed_count,
            "messages": message_count,
            "failure_policy": {
                "mode": failure_policy,
                "tolerated_failures": (failed_count if failure_policy == "continue" else 0),
                "stopped_by_failure": final_stop_reason == "stop_on_failure",
                "failure_categories": failure_categories,
            },
            "reserved_cny": str(sum((o["reserved"] for o in considered), Decimal("0"))),
            "reserved_by_model": reserved_by_model,
            "cost_cny": str(sum((o["cost"] for o in considered), Decimal("0"))),
            "cost_by_model": cost_by_model,
        }

    @staticmethod
    def _billing_status(outcome):
        """Classify what a request actually cost, so a ledger never implies a false number.

        - ``not_sent``: the backend rejected the request before dispatch, so nothing was
          reserved and nothing can have been billed.
        - ``released``: the opportunity was recorded without being dispatched (budget
          preflight, stop signal, deadline, or the tail of an abandoned round).
        - ``settled``: the response carried usable token usage, so the amount is billed
          usage priced at the frozen rates.
        - ``unsettled_reserved``: the request was dispatched but its usage is unknown --
          a transport failure, a malformed usage block, or a request the engine stopped
          waiting for. The reservation is kept as the cost on purpose: a possibly billed
          request must not be reported as free.
        """
        cost = outcome["cost"]
        if outcome["status"] == "not_dispatched":
            return ("not_sent" if cost == 0 else "released"), cost
        if outcome.get("usage") is not None:
            return "settled", cost
        return "unsettled_reserved", cost

    def _billing_summary(self, run_id, run_cost):
        """Aggregate billing states from the persisted action rows.

        Derived from ``simulation_actions`` rather than per-round ``stats`` on purpose: a
        round that aborted partway has decisions and cost persisted even though it may not
        have written stats, so deriving from the action ledger covers every outcome and
        can be reconciled against the charged run cost.
        """
        with self.store.connect() as db:
            rows = [dict(row) for row in db.execute(
                "SELECT status, usage, cost_cny, reserved_cny FROM simulation_actions "
                "WHERE run_id=?", (run_id,))]
        states = {state: 0 for state in BILLING_STATES}
        cost_by_state = {state: Decimal("0") for state in BILLING_STATES}
        reservation = {"reserved": Decimal("0"), "released": Decimal("0")}
        for row in rows:
            state, settled = self._billing_status({
                "status": row["status"], "cost": Decimal(row["cost_cny"]),
                "reserved": Decimal(row["reserved_cny"]),
                "usage": json.loads(row["usage"]) if row["usage"] else None})
            states[state] += 1
            cost_by_state[state] += settled
            if state == "released":
                reservation["released"] += Decimal(row["reserved_cny"])
            elif state in {"settled", "unsettled_reserved"}:
                reservation["reserved"] += Decimal(row["reserved_cny"])
        charged = cost_by_state["settled"] + cost_by_state["unsettled_reserved"]
        return {
            "recorded_requests": len(rows),
            "states": states,
            "cost_by_state": {state: str(value)
                              for state, value in cost_by_state.items()},
            "settled_cost_cny": str(cost_by_state["settled"]),
            "unsettled_reserved_cost_cny": str(cost_by_state["unsettled_reserved"]),
            "charged_cost_cny": str(charged),
            "run_cost_cny": str(run_cost),
            "cost_reconciles": charged == run_cost,
            "reservation": {key: str(value) for key, value in reservation.items()},
            "note": ("unsettled_reserved keeps the reservation as cost because the request "
                     "was dispatched and its usage is unknown; a possibly billed request is "
                     "never reported as free. not_sent and released cost nothing."),
        }

    def _opportunity_buckets(self, rounds, config):
        """Whole-task opportunity reconciliation across four exclusive buckets.

        ``valid``, ``failed`` and ``not_dispatched_current_round`` accumulate across
        rounds because each round owns its own slice. ``not_started_future_rounds`` does
        NOT accumulate: every round records it as a *point-in-time remainder* (how many
        opportunities still lay ahead of that round), so summing would count the same
        future opportunities once per round. It is taken from the final recorded round,
        which is the only view that reflects the run's actual end state, plus a round of
        opportunities for each round that never produced a row.
        """
        buckets = {"valid": 0, "failed": 0, "deadline_exceeded": 0,
                   "not_dispatched_current_round": 0, "not_started_future_rounds": 0}
        for row in rounds:
            buckets["valid"] += int(row.get("valid_decisions", 0))
            buckets["failed"] += int(row.get("failed_decisions", 0))
            buckets["deadline_exceeded"] += int(
                row.get("deadline_exceeded_requests", 0))
            buckets["not_dispatched_current_round"] += int(
                row.get("not_dispatched_current_round", 0))
        if rounds:
            # The final recorded round already owns every round after it, whether or not
            # those rounds ever produce a row. So take its remainder as the whole unseen
            # tail and add nothing else.
            buckets["not_started_future_rounds"] = int(
                rounds[-1].get("not_started_future_rounds", 0))
            last_round = int(rounds[-1].get("round", len(rounds)))
            unreached_rounds = max(0, config["rounds"] - last_round)
        else:
            # No round was started, so every opportunity is unstarted.
            unreached_rounds = config["rounds"]
            buckets["not_started_future_rounds"] = config["agent_count"] * config["rounds"]
        total = config["agent_count"] * config["rounds"]
        reconciled = sum(buckets.values())
        return {
            "total_opportunities": total,
            "buckets": buckets,
            "reconciled": reconciled,
            "fully_reconciled": reconciled == total,
            "completed_rounds": sum(1 for row in rounds
                                    if not row.get("round_abandoned")),
            "recorded_round_rows": len(rounds),
            "abandoned_rounds": sum(1 for row in rounds if row.get("round_abandoned")),
            "unreached_rounds": unreached_rounds,
            "accounting_note": ("buckets are exclusive: requests already submitted are "
                                "valid or failed; not_dispatched_current_round is the "
                                "abandoned remainder of a started round; "
                                "not_started_future_rounds were never offered to any "
                                "agent. The persisted action count alone cannot show the "
                                "last bucket."),
        }

    def advance(self, run_id: str, backend: DecisionBackend, *, max_rounds: int | None = None,
                collect_performance: bool = False,
                stop_event: threading.Event | None = None,
                deadline_monotonic: float | None = None,
                stop_on_failure: bool = False,
                failure_policy: str | None = None,
                call_timeout_seconds: float | None = None):
        """Advance whole rounds with bounded dispatch and optional timing/stop controls.

        ``deadline_monotonic`` is an absolute value from ``time.monotonic()`` in this
        process. Already submitted backend calls are drained and recorded; a stop only
        prevents additional submissions. The engine never retries a failed call.

        ``failure_policy`` decides what one failed decision means for the whole run:

        - ``"continue"`` (the default, and the same behaviour as the historical
          ``stop_on_failure=False``): a failed decision is recorded as a failed
          decision, the agent that failed keeps its current local state, and the
          remaining rounds are still attempted. The run can then reach
          ``status="complete"`` with ``all_decisions_valid=False``, which keeps "all
          rounds ran" and "every decision was valid" as two separate facts instead of
          collapsing them into ``partial``.
        - ``"stop"`` (selected by ``stop_on_failure=True``): the first failed decision
          aborts the run. The round is then incomplete, so the run is marked ``partial``
          and the remaining opportunities are recorded as ``not_dispatched``.

        The historical 500x30 attempt that stopped after 3,477/15,000 valid decisions
        reported ``stop_reason="stop_on_failure"``, so that run was driven by a caller
        passing ``stop_on_failure=True``; it was not the engine default. ``"stop"``
        remains available and is recorded in ``rounds[].failure_policy.mode``, so a
        ledger always states which policy produced it.

        Only the first failed decision of a round is reported in
        ``rounds[].failure_policy.stopped_by_failure``; the count is
        ``rounds[].failed_decisions``.
        """
        if failure_policy is not None and stop_on_failure:
            raise SimulationError(
                "pass either failure_policy or stop_on_failure, not both")
        if failure_policy is None:
            failure_policy = "stop" if stop_on_failure else "continue"
        if failure_policy not in {"stop", "continue"}:
            raise SimulationError("failure_policy must be 'stop' or 'continue'")
        if backend.mode not in {"test_substitute", "real_model"}:
            raise SimulationError("backend mode must declare test_substitute or real_model")
        if max_rounds is not None and (type(max_rounds) is not int or max_rounds < 1):
            raise SimulationError("max_rounds must be positive")
        if call_timeout_seconds is not None:
            if (isinstance(call_timeout_seconds, bool) or
                    not isinstance(call_timeout_seconds, (int, float)) or
                    not math.isfinite(call_timeout_seconds) or call_timeout_seconds <= 0):
                raise SimulationError(
                    "call_timeout_seconds must be a finite positive number of seconds")
        if stop_event is not None and not callable(getattr(stop_event, "is_set", None)):
            raise SimulationError("stop_event must provide is_set()")
        if deadline_monotonic is not None:
            if (isinstance(deadline_monotonic, bool) or
                    not isinstance(deadline_monotonic, (int, float)) or
                    not math.isfinite(deadline_monotonic)):
                raise SimulationError("deadline_monotonic must be a finite monotonic timestamp")
            deadline_monotonic = float(deadline_monotonic)

        call_started = time.perf_counter()
        call_started_at = datetime.now().astimezone().isoformat()
        # A mutable container: finish() below must observe the final counts, and a plain
        # local would be rebound in this scope without being visible to the closure.
        call_totals = {"dispatched": 0, "valid": 0, "deadline_exceeded": 0}
        final_stop_reason = None

        def effective_round_budget():
            """Seconds a round may wait for its in-flight requests before giving up.

            This is the real termination boundary. Waiting on a launched request with no
            bound is what lets one hung backend call block the whole task forever, so the
            engine must be able to stop waiting by itself and not depend on the backend
            (or the model gateway) honouring a socket timeout.

            When both limits are configured the round obeys the EARLIER one. A task
            deadline must never be extended by a larger per-round budget, or the combined
            configuration would return later than the deadline promises.
            """
            if call_timeout_seconds is None and deadline_monotonic is None:
                return None
            remaining = (None if deadline_monotonic is None
                         else max(0.0, deadline_monotonic - time.monotonic()))
            if call_timeout_seconds is None:
                return remaining
            if remaining is None:
                return call_timeout_seconds
            return min(call_timeout_seconds, remaining)

        def record_call_timeout(aid):
            """Record a request we stopped waiting for.

            It was genuinely dispatched, so it is NOT ``not_dispatched``, and its usage is
            unknown, so it is NOT free. It is ``deadline_exceeded`` and its reservation is
            kept as an unsettled cost, because the provider may have processed it.
            """
            obs, quote = observation_by_id[aid], quote_by_id[aid]
            return {
                "status": "deadline_exceeded", "decision": None, "usage": None,
                "cost": quote.reservation_cny, "reserved": quote.reservation_cny,
                "requested_model": quote.requested_model,
                "price_version": quote.price_version, "model": None,
                "error": "CallTimeout", "request_audit": None,
                "schema_diagnostics": None, "failure_category": "CallTimeout",
                # No response arrived, so there are no response timings to report. The
                # dispatch evidence records that the request was sent and then abandoned.
                "timing": {
                    "queued_at": None, "started_at": None, "ended_at": None,
                    "queue_seconds": None, "backend_seconds": None,
                    "validation_seconds": None, "elapsed_seconds": None,
                    "dispatch_evidence": "dispatched_then_not_waited_for",
                },
            }

        def finish():
            """Close the invocation and return the summary.

            Every exit path goes through here, including a round that stopped early
            (stop signal, deadline, budget or an aborting failure) and the path for a
            run that was already complete. Otherwise a partial run would lose its whole
            performance record: ``_performance_summary`` returns ``None`` when no call
            row exists, so the timing, throughput and observed concurrency of a stopped
            run would be silently unavailable exactly when they matter most.
            """
            if collect_performance:
                call_ended = time.perf_counter()
                call_ended_at = datetime.now().astimezone().isoformat()
                with self.store.connect() as db:
                    db.execute(
                        "INSERT INTO simulation_performance_calls "
                        "(run_id, started_at, ended_at, wall_seconds, dispatched_requests, "
                        "valid_decisions, stop_reason) VALUES (?, ?, ?, ?, ?, ?, ?)",
                        (run_id, call_started_at, call_ended_at,
                         max(0.0, call_ended - call_started), call_totals["dispatched"],
                         call_totals["valid"], final_stop_reason))
            return self.summary(run_id)

        def requested_stop():
            if stop_event is not None and stop_event.is_set():
                return "stop_signal"
            if deadline_monotonic is not None and time.monotonic() >= deadline_monotonic:
                return "deadline"
            return None

        run = self._run_row(run_id)
        config = json.loads(run["config"])
        current_cost = Decimal(run["cost_cny"])
        if config.get("backend_mode") not in (None, backend.mode):
            raise SimulationError("cannot mix test and real decisions in one run")
        if config.get("backend_mode") is None:
            config["backend_mode"] = backend.mode
            describe = getattr(backend, "describe", None)
            config["backend"] = describe() if callable(describe) else {
                "type": backend.__class__.__name__,
                "price_version": str(getattr(backend, "price_version", "unspecified")),
            }
            with self.store.connect() as db:
                db.execute("UPDATE simulation_runs SET config=? WHERE run_id=?",
                           (_json(config), run_id))
            run = self._run_row(run_id)
        if run["status"] == "complete":
            return finish()
        target = (min(config["rounds"], run["completed_rounds"] + max_rounds)
                  if max_rounds is not None else config["rounds"])

        while int(run["completed_rounds"]) < target:
            round_started = time.perf_counter()
            round_started_at = datetime.now().astimezone().isoformat()
            round_number = int(run["completed_rounds"]) + 1
            phases = {} if collect_performance else None

            phase_start = time.perf_counter()
            agents = self._agents(run_id)
            if phases is not None:
                phases["state_read_seconds"] = time.perf_counter() - phase_start
            observations = self._context(run, agents, round_number,
                                         phase_timings=phases)
            observation_by_id = {obs["agent_id"]: obs for obs in observations}
            observation_hashes = {aid: _canonical_sha256(obs)
                                  for aid, obs in observation_by_id.items()}

            phase_start = time.perf_counter()
            quotes = [self._pricing(backend, obs) for obs in observations]
            with self.store.connect() as db:
                prior_models = {row["agent_id"]: row["requested_model"] for row in db.execute(
                    "SELECT agent_id, requested_model FROM simulation_actions WHERE run_id=? "
                    "AND requested_model IS NOT NULL GROUP BY agent_id", (run_id,))}
                existing_rows = {row["agent_id"]: dict(row) for row in db.execute(
                    "SELECT * FROM simulation_actions WHERE run_id=? AND round_number=?",
                    (run_id, round_number))}
            quote_by_id = {obs["agent_id"]: quote for obs, quote in zip(observations, quotes)}
            for obs, quote in zip(observations, quotes):
                prior = prior_models.get(obs["agent_id"])
                if prior is not None and prior != quote.requested_model:
                    raise SimulationError("an agent's requested model cannot change within a run")
            outcomes_by_id = {}
            for aid, row in existing_rows.items():
                if row.get("observation_sha256") != observation_hashes[aid]:
                    raise SimulationError("frozen observation changed; refusing to replay this round")
                if row["status"] == "not_dispatched":
                    continue
                if row["status"] not in {"valid", "failed", "deadline_exceeded"}:
                    raise SimulationError("unknown persisted action status")
                outcomes_by_id[aid] = {
                    "status": row["status"],
                    "decision": json.loads(row["decision"]) if row["decision"] else None,
                    "usage": json.loads(row["usage"]) if row["usage"] else None,
                    "cost": Decimal(row["cost_cny"]),
                    "reserved": Decimal(row["reserved_cny"]),
                    "requested_model": row["requested_model"],
                    "price_version": row["price_version"],
                    "model": row["model"], "error": row["error"],
                    "request_audit": json.loads(row["request_audit"])
                    if row["request_audit"] else None,
                    "schema_diagnostics": json.loads(row["schema_diagnostics"])
                    if row["schema_diagnostics"] else None,
                }
            pending_ids = [obs["agent_id"] for obs in observations
                           if obs["agent_id"] not in outcomes_by_id]
            if phases is not None:
                phases["pricing_and_existing_action_read_seconds"] = (
                    time.perf_counter() - phase_start)

            budget_started = time.perf_counter()
            remaining = Decimal(config["budget_cny"]) - Decimal(run["cost_cny"])
            round_reservation = sum((quote_by_id[aid].reservation_cny
                                     for aid in pending_ids), Decimal("0"))
            if pending_ids and remaining < round_reservation:
                final_stop_reason = "budget_preflight"
            if phases is not None:
                phases["budget_preflight_seconds"] = time.perf_counter() - budget_started

            def observation_refs(obs):
                return {"evidence": [e["evidence_id"] for e in obs["evidence"]],
                        "messages": [m["message_id"] for m in obs["messages"]],
                        "memories": [m["memory_id"] for m in obs["memories"]]}

            request_ledger_persist_total = 0.0

            def insert_not_dispatched(db, aid, reason):
                obs, quote = observation_by_id[aid], quote_by_id[aid]
                values = (run_id, round_number, aid, "not_dispatched",
                          _json(observation_refs(obs)), None, None,
                          quote.requested_model, None, quote.price_version,
                          "0", "0", reason, None, None, observation_hashes[aid])
                old = db.execute(
                    "SELECT status, error FROM simulation_actions WHERE run_id=? "
                    "AND round_number=? AND agent_id=?", (run_id, round_number, aid)
                ).fetchone()
                if old is None:
                    db.execute(
                        "INSERT INTO simulation_actions "
                        "(run_id, round_number, agent_id, status, observation_refs, decision, "
                        "usage, requested_model, model, price_version, reserved_cny, cost_cny, "
                        "error, request_audit, schema_diagnostics, observation_sha256) "
                        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", values)
                elif old["status"] == "not_dispatched":
                    if old["error"] and old["error"] not in {
                            "BudgetPreflight", "StopSignal", "DeadlineExceeded",
                            "BackendCallLimit", "StoppedAfterFailure", "BudgetExceeded",
                            "NotDispatched", "FailedDecisionsTolerated"}:
                        return False
                    db.execute(
                        "UPDATE simulation_actions SET observation_refs=?, decision=NULL, "
                        "usage=NULL, requested_model=?, model=NULL, price_version=?, "
                        "reserved_cny='0', cost_cny='0', error=?, request_audit=NULL, "
                        "schema_diagnostics=NULL, observation_sha256=? WHERE run_id=? "
                        "AND round_number=? AND agent_id=? AND status='not_dispatched'",
                        (_json(observation_refs(obs)), quote.requested_model,
                         quote.price_version, reason, observation_hashes[aid],
                         run_id, round_number, aid))
                else:
                    return False
                if collect_performance:
                    db.execute(
                        "INSERT INTO simulation_request_metrics "
                        "(run_id, round_number, agent_id, requested_model, queued_at, "
                        "started_at, ended_at, queue_seconds, backend_seconds, "
                        "validation_seconds, request_elapsed_seconds, status, "
                        "failure_category, dispatch_evidence, "
                        "usage_json, cost_cny, usage_unknown) "
                        "VALUES (?, ?, ?, ?, NULL, NULL, NULL, NULL, NULL, NULL, NULL, "
                        "'not_dispatched', ?, 'not_submitted', NULL, '0', 0) "
                        "ON CONFLICT(run_id, round_number, agent_id) DO UPDATE SET "
                        "requested_model=excluded.requested_model, queued_at=NULL, "
                        "started_at=NULL, ended_at=NULL, queue_seconds=NULL, "
                        "backend_seconds=NULL, validation_seconds=NULL, "
                        "request_elapsed_seconds=NULL, "
                        "status='not_dispatched', failure_category=excluded.failure_category, "
                        "dispatch_evidence='not_submitted', usage_json=NULL, cost_cny='0', "
                        "usage_unknown=0",
                        (run_id, round_number, aid, quote.requested_model, reason))
                return True

            def insert_not_dispatched_batch(aids, reason):
                """Record every undispatched opportunity in one connection and transaction.

                Before this, each opportunity opened its own SQLite connection, so a
                round that stopped before dispatch (budget preflight, stop signal,
                deadline or an aborting failure) paid one connection per agent.
                """
                nonlocal request_ledger_persist_total
                if not aids:
                    return
                persist_started = time.perf_counter()
                with self.store.connect() as db:
                    for aid in aids:
                        insert_not_dispatched(db, aid, reason)
                request_ledger_persist_total += time.perf_counter() - persist_started

            actual_in_flight = 0
            actual_peak_in_flight = 0
            in_flight_lock = threading.Lock()
            if final_stop_reason == "budget_preflight":
                insert_not_dispatched_batch(pending_ids, "BudgetPreflight")
            elif pending_ids and requested_stop() is not None:
                final_stop_reason = requested_stop()
                reason = "StopSignal" if final_stop_reason == "stop_signal" else "DeadlineExceeded"
                insert_not_dispatched_batch(pending_ids, reason)
            else:
                def call(obs, quote, queued_at, queued_wall):
                    nonlocal actual_in_flight, actual_peak_in_flight
                    request_audit = None
                    started_mono = time.perf_counter()
                    started_wall = datetime.now().astimezone().isoformat()

                    def record_audit(value):
                        nonlocal request_audit
                        safe = _safe_request_audit(value)
                        if safe is None:
                            raise SimulationError("invalid request audit")
                        request_audit = safe

                    with in_flight_lock:
                        actual_in_flight += 1
                        actual_peak_in_flight = max(actual_peak_in_flight, actual_in_flight)
                    backend_start = time.perf_counter()
                    try:
                        decide_with_audit = getattr(backend, "decide_with_audit", None)
                        if callable(decide_with_audit):
                            request_audit = {"stage": "before_dispatch"}
                            result = decide_with_audit(obs, record_audit)
                            if request_audit["stage"] != "prepared_for_dispatch":
                                raise SimulationError("decision lacks prepared request audit")
                        else:
                            result = backend.decide(obs)
                    except Exception as exc:
                        backend_seconds = time.perf_counter() - backend_start
                        with in_flight_lock:
                            actual_in_flight -= 1
                        ended_mono = time.perf_counter()
                        ended_wall = datetime.now().astimezone().isoformat()
                        error = _persisted_error(exc)
                        rejected_before_send = (
                            isinstance(exc, SafeDecisionError) and
                            type(exc.safe_category) is str and
                            exc.safe_category in {"synthetic_only", "prompt_bound",
                                                  "live_disabled", "call_limit"})
                        return {
                            "status": "not_dispatched" if rejected_before_send else "failed",
                            "decision": None, "usage": None,
                            "cost": Decimal("0") if rejected_before_send
                            else quote.reservation_cny,
                            "reserved": Decimal("0") if rejected_before_send
                            else quote.reservation_cny,
                            "requested_model": quote.requested_model,
                            "price_version": quote.price_version, "model": None,
                            "error": error, "request_audit": request_audit,
                            "schema_diagnostics": _safe_schema_diagnostics(exc),
                            "failure_category": error,
                            "timing": {
                                "queued_at": queued_wall, "started_at": started_wall,
                                "ended_at": ended_wall,
                                "queue_seconds": max(0.0, started_mono - queued_at),
                                "backend_seconds": backend_seconds,
                                "validation_seconds": 0.0,
                                "elapsed_seconds": max(0.0, ended_mono - started_mono),
                                "dispatch_evidence": "backend_rejected_before_send"
                                if rejected_before_send else (request_audit or {}).get(
                                    "stage", "backend_invoked"),
                            },
                        }

                    backend_seconds = time.perf_counter() - backend_start
                    with in_flight_lock:
                        actual_in_flight -= 1
                    validation_start = time.perf_counter()
                    try:
                        decision = AgentDecision.model_validate(result.decision)
                        allowed = {e["evidence_id"] for e in obs["evidence"]}
                        allowed.update(m["message_id"] for m in obs["messages"])
                        allowed.update(m["memory_id"] for m in obs["memories"])
                        if (decision.agent_id != obs["agent_id"] or
                                not set(decision.evidence_ids) <= allowed):
                            raise SimulationError("invalid agent or inaccessible citation")
                        cost, usage = self._cost(quote, result.usage)
                        actual_model = str(result.model).strip()[:100] if result.model else None
                        if cost > quote.reservation_cny:
                            status, decision_data, error = "failed", None, "ReservationExceeded"
                        else:
                            status, decision_data, error = "valid", decision.model_dump(), None
                        schema_diagnostics = None
                    except Exception as exc:
                        status, decision_data = "failed", None
                        error = _persisted_error(exc)
                        schema_diagnostics = _safe_schema_diagnostics(exc)
                        cost, usage = self._cost(quote, getattr(result, "usage", None))
                        actual_model = str(getattr(result, "model", "")).strip()[:100] or None
                    validation_seconds = time.perf_counter() - validation_start
                    ended_mono = time.perf_counter()
                    ended_wall = datetime.now().astimezone().isoformat()
                    return {
                        "status": status, "decision": decision_data, "usage": usage,
                        "cost": cost, "reserved": quote.reservation_cny,
                        "requested_model": quote.requested_model,
                        "price_version": quote.price_version, "model": actual_model,
                        "error": error, "request_audit": request_audit,
                        "schema_diagnostics": schema_diagnostics,
                        "failure_category": error,
                        "timing": {
                            "queued_at": queued_wall, "started_at": started_wall,
                            "ended_at": ended_wall,
                            "queue_seconds": max(0.0, started_mono - queued_at),
                            "backend_seconds": backend_seconds,
                            "validation_seconds": validation_seconds,
                            "elapsed_seconds": max(0.0, ended_mono - started_mono),
                            "dispatch_evidence": (request_audit or {}).get(
                                "stage", "backend_invoked"),
                        },
                    }

                def persist_outcomes(batch):
                    nonlocal request_ledger_persist_total, current_cost
                    if not batch:
                        return
                    persist_started = time.perf_counter()
                    with self.store.connect() as db:
                        run_row = db.execute(
                            "SELECT cost_cny, usage FROM simulation_runs WHERE run_id=?",
                            (run_id,)).fetchone()
                        cumulative_cost = Decimal(run_row["cost_cny"])
                        cumulative_usage = json.loads(run_row["usage"])
                        cumulative_usage.setdefault("cached_input_tokens", 0)
                        cumulative_usage.setdefault("by_model", {})
                        for aid, outcome in batch:
                            obs = observation_by_id[aid]
                            values = (run_id, round_number, aid, outcome["status"],
                                      _json(observation_refs(obs)),
                                      _json(outcome["decision"]) if outcome["decision"] else None,
                                      _json(outcome["usage"]) if outcome["usage"] else None,
                                      outcome["requested_model"], outcome["model"],
                                      outcome["price_version"], str(outcome["reserved"]),
                                      str(outcome["cost"]), outcome["error"],
                                      _json(outcome["request_audit"])
                                      if outcome["request_audit"] else None,
                                      _json(outcome["schema_diagnostics"])
                                      if outcome["schema_diagnostics"] else None,
                                      observation_hashes[aid])
                            old = db.execute(
                                "SELECT status FROM simulation_actions WHERE run_id=? "
                                "AND round_number=? AND agent_id=?",
                                (run_id, round_number, aid)).fetchone()
                            if old is not None and old["status"] != "not_dispatched":
                                raise SimulationError(
                                    "refusing to dispatch a completed decision twice")
                            if old is None:
                                db.execute(
                                    "INSERT INTO simulation_actions "
                                    "(run_id, round_number, agent_id, status, observation_refs, "
                                    "decision, usage, requested_model, model, price_version, "
                                    "reserved_cny, cost_cny, error, request_audit, "
                                    "schema_diagnostics, observation_sha256) "
                                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                                    values)
                            else:
                                db.execute(
                                    "UPDATE simulation_actions SET status=?, observation_refs=?, "
                                    "decision=?, usage=?, requested_model=?, model=?, price_version=?, "
                                    "reserved_cny=?, cost_cny=?, error=?, request_audit=?, "
                                    "schema_diagnostics=?, observation_sha256=? WHERE run_id=? "
                                    "AND round_number=? AND agent_id=? AND status='not_dispatched'",
                                    (*values[3:], run_id, round_number, aid))

                            if outcome["status"] != "not_dispatched":
                                cumulative_cost += outcome["cost"]
                                model_usage = cumulative_usage["by_model"].setdefault(
                                    outcome["requested_model"], {
                                        "calls": 0, "prompt_tokens": 0,
                                        "cached_input_tokens": 0, "completion_tokens": 0,
                                        "unknown_usage_calls": 0, "reserved_cny": "0",
                                        "cost_cny": "0",
                                        "price_version": outcome["price_version"],
                                        "actual_models": {},
                                    })
                                model_usage["calls"] += 1
                                model_usage["reserved_cny"] = str(
                                    Decimal(model_usage["reserved_cny"]) + outcome["reserved"])
                                model_usage["cost_cny"] = str(
                                    Decimal(model_usage["cost_cny"]) + outcome["cost"])
                                if outcome["model"]:
                                    actual = model_usage["actual_models"]
                                    actual[outcome["model"]] = actual.get(
                                        outcome["model"], 0) + 1
                                if outcome["usage"] is None:
                                    cumulative_usage["unknown_usage_calls"] += 1
                                    model_usage["unknown_usage_calls"] += 1
                                else:
                                    for field in ("prompt_tokens", "cached_input_tokens",
                                                  "completion_tokens"):
                                        cumulative_usage[field] = (
                                            cumulative_usage.get(field, 0) +
                                            outcome["usage"].get(field, 0))
                                        model_usage[field] += outcome["usage"].get(field, 0)
                            if collect_performance:
                                timing = outcome["timing"]
                                db.execute(
                                    "INSERT INTO simulation_request_metrics "
                                    "(run_id, round_number, agent_id, requested_model, queued_at, "
                                    "started_at, ended_at, queue_seconds, backend_seconds, "
                                    "validation_seconds, request_elapsed_seconds, status, "
                                    "failure_category, dispatch_evidence, usage_json, cost_cny, "
                                    "usage_unknown) "
                                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
                                    "ON CONFLICT(run_id, round_number, agent_id) DO UPDATE SET "
                                    "requested_model=excluded.requested_model, "
                                    "queued_at=excluded.queued_at, started_at=excluded.started_at, "
                                    "ended_at=excluded.ended_at, queue_seconds=excluded.queue_seconds, "
                                    "backend_seconds=excluded.backend_seconds, "
                                    "validation_seconds=excluded.validation_seconds, "
                                    "request_elapsed_seconds=excluded.request_elapsed_seconds, "
                                    "status=excluded.status, failure_category=excluded.failure_category, "
                                    "dispatch_evidence=excluded.dispatch_evidence, "
                                    "usage_json=excluded.usage_json, cost_cny=excluded.cost_cny, "
                                    "usage_unknown=excluded.usage_unknown",
                                    (run_id, round_number, aid, outcome["requested_model"],
                                     timing["queued_at"], timing["started_at"], timing["ended_at"],
                                     timing["queue_seconds"], timing["backend_seconds"],
                                     timing["validation_seconds"], timing["elapsed_seconds"],
                                     outcome["status"], outcome["failure_category"],
                                     timing["dispatch_evidence"],
                                     _json(outcome["usage"]) if outcome["usage"] else None,
                                     str(outcome["cost"]), int(
                                         outcome["usage"] is None and
                                         outcome["status"] != "not_dispatched")))
                        db.execute(
                            "UPDATE simulation_runs SET cost_cny=?, usage=? WHERE run_id=?",
                            (str(cumulative_cost), _json(cumulative_usage), run_id))
                    current_cost = cumulative_cost
                    request_ledger_persist_total += time.perf_counter() - persist_started
                    for aid, outcome in batch:
                        outcomes_by_id[aid] = outcome

                pending = [aid for aid in pending_ids]

                scheduler_started = time.perf_counter()
                peak_in_flight = 0
                request_backend_total = 0.0
                request_validation_total = 0.0
                projected_cost = current_cost
                completed_buffer = []
                abandoned_in_flight = []
                if final_stop_reason is None and pending:
                    active = {}
                    cursor = 0
                    concurrency = config["concurrency"]
                    round_budget = effective_round_budget()
                    round_deadline = (time.monotonic() + round_budget
                                      if round_budget is not None else None)
                    pool = DaemonCallPool(concurrency)
                    try:
                        while cursor < len(pending) or active:
                            while (cursor < len(pending) and len(active) < concurrency
                                   and final_stop_reason is None):
                                stop_reason = requested_stop()
                                if stop_reason:
                                    final_stop_reason = stop_reason
                                    break
                                aid = pending[cursor]
                                cursor += 1
                                queued_at = time.perf_counter()
                                queued_wall = datetime.now().astimezone().isoformat()
                                pool.submit(aid, call, observation_by_id[aid],
                                            quote_by_id[aid], queued_at, queued_wall)
                                active[aid] = True
                                peak_in_flight = max(peak_in_flight, len(active))
                            if not active:
                                break
                            timeout = None
                            if round_deadline is not None:
                                timeout = max(0.0, round_deadline - time.monotonic())
                            arrived = pool.collect(timeout)
                            if arrived is None:
                                # The budget is spent and nothing came back. Stop waiting;
                                # the requests already dispatched are recorded below. The
                                # reason distinguishes "the run deadline passed" from "this
                                # round's call budget was exhausted".
                                final_stop_reason = (
                                    "deadline" if requested_stop() == "deadline"
                                    else "call_timeout")
                                abandoned_in_flight = sorted(active)
                                active.clear()
                                break
                            aid, outcome = arrived
                            if aid not in active:
                                continue  # late result from an already-recorded request
                            del active[aid]
                            if isinstance(outcome, BaseException):
                                raise SimulationError(
                                    "worker returned an unexpected exception"
                                ) from outcome
                            outcomes_by_id[aid] = outcome
                            completed_buffer.append((aid, outcome))
                            projected_cost += outcome["cost"]
                            call_totals["dispatched"] += outcome["status"] != "not_dispatched"
                            call_totals["valid"] += outcome["status"] == "valid"
                            request_backend_total += outcome["timing"]["backend_seconds"] or 0.0
                            request_validation_total += (
                                outcome["timing"]["validation_seconds"] or 0.0)
                            if (outcome["error"] or "").endswith(":call_limit"):
                                final_stop_reason = "backend_call_limit"
                            elif failure_policy == "stop" and outcome["status"] == "failed":
                                final_stop_reason = "stop_on_failure"
                            if projected_cost > Decimal(config["budget_cny"]):
                                final_stop_reason = "budget_exceeded"
                            if len(completed_buffer) >= concurrency:
                                persist_outcomes(completed_buffer)
                                completed_buffer.clear()
                        if final_stop_reason is None and active:
                            # A stop landed while requests were in flight. They were
                            # dispatched, so they are not "not dispatched"; their outcome
                            # is simply unknown, so they are recorded as timed out rather
                            # than released as free.
                            abandoned_in_flight = sorted(active)
                            active.clear()
                            final_stop_reason = "call_timeout"
                        persist_outcomes(completed_buffer)
                    finally:
                        pool.shutdown()
                if final_stop_reason in {"call_timeout", "deadline"} and abandoned_in_flight:
                    # Reconcile the requests we stopped waiting for, then let the normal
                    # ledger path record the rest of the round as stranded.
                    timeouts = [(aid, record_call_timeout(aid))
                                for aid in abandoned_in_flight]
                    call_totals["deadline_exceeded"] += len(timeouts)
                    persist_outcomes(timeouts)
                if pending and final_stop_reason is not None:
                    reason = {
                        "stop_signal": "StopSignal", "deadline": "DeadlineExceeded",
                        "call_timeout": "CallTimeout",
                        "budget_preflight": "BudgetPreflight",
                        "backend_call_limit": "BackendCallLimit",
                        "stop_on_failure": "StoppedAfterFailure",
                        "budget_exceeded": "BudgetExceeded",
                    }.get(final_stop_reason, "NotDispatched")
                    # One connection for the whole tail of the round instead of one per
                    # agent. Agents that already produced a persisted outcome (valid,
                    # failed or deadline_exceeded) are skipped, so a recorded outcome keeps
                    # its own status.
                    stranded_now = [aid for aid in pending if aid not in outcomes_by_id]
                    if stranded_now:
                        insert_not_dispatched_batch(stranded_now, reason)
                scheduler_wall = time.perf_counter() - scheduler_started
                if phases is not None:
                    phases["scheduler_and_drain_wall_seconds"] = scheduler_wall
                    phases["request_ledger_persist_cumulative_seconds"] = (
                        request_ledger_persist_total)
                    phases["backend_call_cumulative_seconds"] = request_backend_total
                    phases["response_validation_cumulative_seconds"] = request_validation_total
                    phases["observed_max_in_flight"] = actual_peak_in_flight
                    phases["maximum_submitted_futures"] = peak_in_flight
                    phases["dispatched_this_round"] = sum(
                        outcome["status"] != "not_dispatched"
                        for outcome in outcomes_by_id.values())

            if (len(outcomes_by_id) < len(agents) or any(
                    outcome["status"] == "not_dispatched"
                    for outcome in outcomes_by_id.values())):
                # The round did not finish, but it still owns accounting: submitted
                # requests are recorded and the remainder of this round is explicitly
                # stranded. Without a ledger row that remainder would be invisible and
                # the whole-task total could not be reconciled.
                abandoned_stats = self._round_stats(
                    [outcomes_by_id[obs["agent_id"]] for obs in observations
                     if obs["agent_id"] in outcomes_by_id],
                    config=config, round_number=round_number,
                    failure_policy=failure_policy, final_stop_reason=final_stop_reason,
                    configured_agents=len(agents), state_by_id={},
                    message_count=0, abandoned=True)
                abandoned_stats["elapsed_seconds"] = round(
                    time.perf_counter() - round_started, 3)
                if phases is not None:
                    phases["round_started_at"] = round_started_at
                    phases["round_abandoned"] = True
                    abandoned_stats["performance"] = phases
                with self.store.connect() as db:
                    db.execute("DELETE FROM simulation_rounds WHERE run_id=? "
                               "AND round_number=?", (run_id, round_number))
                    db.execute("INSERT INTO simulation_rounds VALUES (?, ?, ?)",
                               (run_id, round_number, _json(abandoned_stats)))
                    db.execute("UPDATE simulation_runs SET status='partial' WHERE run_id=?",
                               (run_id,))
                break

            outcomes = [outcomes_by_id[obs["agent_id"]] for obs in observations]
            round_cost = sum((outcome["cost"] for outcome in outcomes), Decimal("0"))
            run = self._run_row(run_id)
            total_cost = Decimal(run["cost_cny"])
            over_budget = total_cost > Decimal(config["budget_cny"])

            phase_start = time.perf_counter()
            state_by_id = {agent["agent_id"]: dict(agent["state"]) for agent in agents}
            messages = []
            memory_rows = []
            for obs, outcome in zip(observations, outcomes):
                aid = obs["agent_id"]
                decision = outcome["decision"]
                for msg in obs["messages"]:
                    memory_rows.append((run_id, aid,
                                        f"memmsg_{round_number}_{aid}_{msg['message_id']}",
                                        round_number,
                                        f"heard {msg['sender_id']} {msg['action']}: "
                                        f"{msg['content'][:200]}",
                                        _json([msg["message_id"], *msg["evidence_ids"]])))
                if decision is None:
                    continue
                state = state_by_id[aid]
                state["emotion"] = _clip(
                    state["emotion"] + SIGNALS[decision["action"]] *
                    obs["persona"]["susceptibility"], -1, 1)
                state["trust"] = _clip(
                    state["trust"] + (0.015 if decision["action"] == "seek_clarification"
                                      else -0.01), 0, 1)
                memory_rows.append((run_id, aid, f"mem_{round_number}_{aid}",
                                    round_number,
                                    decision["action"] + " " + decision["reason"],
                                    _json(decision["evidence_ids"])))
                if decision["action"] in {"share", "comment", "express_complaint_intent"}:
                    for neighbor in obs["neighbors"]:
                        evidence_set = {item["evidence_id"] for item in obs["evidence"]}
                        messages.append((
                            run_id, f"msg_{round_number}_{aid}_{neighbor}", round_number,
                            aid, neighbor, decision["action"],
                            _json([e for e in decision["evidence_ids"] if e in evidence_set]),
                            decision["reason"]))
            for _, _, _, sender, recipient, action, _, _ in messages:
                state_by_id[recipient]["emotion"] = _clip(
                    state_by_id[recipient]["emotion"] + SIGNALS[action] * 0.25, -1, 1)
            if phases is not None:
                phases["state_message_memory_calculation_seconds"] = (
                    time.perf_counter() - phase_start)

            valid = sum(outcome["status"] == "valid" for outcome in outcomes)
            failed = sum(outcome["status"] == "failed" for outcome in outcomes)
            not_dispatched = sum(outcome["status"] == "not_dispatched"
                                 for outcome in outcomes)
            round_cost = sum((outcome["cost"] for outcome in outcomes), Decimal("0"))
            stats = self._round_stats(
                outcomes, config=config, round_number=round_number,
                failure_policy=failure_policy, final_stop_reason=final_stop_reason,
                configured_agents=len(agents), state_by_id=state_by_id,
                message_count=len(messages), abandoned=False)
            if phases is not None:
                phases["round_started_at"] = round_started_at
                phases["phase_timing_note"] = (
                    "monotonic durations; per-request worker times overlap and are cumulative")
                stats["performance"] = phases
                stats["elapsed_seconds"] = round(
                    time.perf_counter() - round_started, 3)
            else:
                stats["elapsed_seconds"] = round(time.perf_counter() - round_started, 3)

            phase_start = time.perf_counter()
            with self.store.connect() as db:
                db.execute("BEGIN IMMEDIATE")
                db.executemany(
                    "INSERT INTO simulation_messages "
                    "(run_id, message_id, round_number, sender_id, recipient_id, action, "
                    "evidence_ids, content) VALUES (?, ?, ?, ?, ?, ?, ?, ?)", messages)
                db.executemany("INSERT INTO simulation_memories VALUES (?, ?, ?, ?, ?, ?)",
                               memory_rows)
                db.executemany(
                    "UPDATE simulation_agents SET state=? WHERE run_id=? AND agent_id=?",
                    [(_json(state), run_id, aid) for aid, state in state_by_id.items()])
                # A round that was previously abandoned left a ledger row behind. Resuming
                # the run retries that same round, so the stale row must be replaced
                # rather than colliding with the primary key.
                db.execute("DELETE FROM simulation_rounds WHERE run_id=? AND round_number=?",
                           (run_id, round_number))
                db.execute("INSERT INTO simulation_rounds VALUES (?, ?, ?)",
                           (run_id, round_number, _json(stats)))
                if over_budget:
                    status = "partial"
                elif round_number == config["rounds"]:
                    # Preserve the public status meaning: all rounds ran. The additive
                    # summary success flag distinguishes that from all-valid completion.
                    status = "complete"
                else:
                    status = "running"
                db.execute(
                    "UPDATE simulation_runs SET status=?, completed_rounds=? WHERE run_id=?",
                    (status, round_number, run_id))
            if phases is not None:
                phases["sqlite_persist_seconds"] = time.perf_counter() - phase_start
                phases["round_wall_seconds"] = time.perf_counter() - round_started
                phases["round_finished_at"] = datetime.now().astimezone().isoformat()
                stats["performance"] = phases
                stats["elapsed_seconds"] = round(phases["round_wall_seconds"], 3)
                with self.store.connect() as db:
                    db.execute("UPDATE simulation_rounds SET stats=? "
                               "WHERE run_id=? AND round_number=?",
                               (_json(stats), run_id, round_number))
            run = self._run_row(run_id)
            if over_budget:
                final_stop_reason = "budget_exceeded"
                break

        return finish()

    @staticmethod
    def _latency_percentiles(values):
        values = sorted(float(value) for value in values if value is not None)
        if not values:
            return {"p50": None, "p95": None, "p99": None}

        def nearest_rank(percent):
            index = max(0, math.ceil(percent * len(values)) - 1)
            return round(values[index], 6)

        return {"p50": nearest_rank(0.50), "p95": nearest_rank(0.95),
                "p99": nearest_rank(0.99)}

    def _performance_summary(self, run_id):
        with self.store.connect() as db:
            requests = [dict(row) for row in db.execute(
                "SELECT * FROM simulation_request_metrics WHERE run_id=? "
                "ORDER BY round_number, agent_id", (run_id,))]
            calls = [dict(row) for row in db.execute(
                "SELECT * FROM simulation_performance_calls WHERE run_id=? ORDER BY call_id",
                (run_id,))]
            round_rows = [dict(row) for row in db.execute(
                "SELECT stats FROM simulation_rounds WHERE run_id=? ORDER BY round_number",
                (run_id,))]
        if not requests and not calls:
            return None
        dispatched = [row for row in requests if row["status"] in {"valid", "failed"}]
        valid = [row for row in dispatched if row["status"] == "valid"]
        timed_out = [row for row in requests if row["status"] == "deadline_exceeded"]
        unknown_usage = sum(row["usage_unknown"] for row in dispatched)
        elapsed = sum(row["wall_seconds"] for row in calls)
        by_model = {}
        for row in requests:
            model = row["requested_model"]
            bucket = by_model.setdefault(model, {"requests": 0, "valid": 0, "failed": 0,
                "not_dispatched": 0, "deadline_exceeded": 0, "unknown_usage": 0,
                "cost_cny": "0", "failure_categories": {}, "backend_seconds": [],
                "queue_seconds": [], "request_elapsed_seconds": []})
            bucket["requests"] += 1
            bucket[row["status"]] += 1
            bucket["unknown_usage"] += row["usage_unknown"]
            bucket["cost_cny"] = str(Decimal(bucket["cost_cny"]) + Decimal(row["cost_cny"]))
            if row["status"] == "failed" and row["failure_category"]:
                categories = bucket["failure_categories"]
                categories[row["failure_category"]] = categories.get(
                    row["failure_category"], 0) + 1
            for field, source in (("backend_seconds", "backend_seconds"),
                                  ("queue_seconds", "queue_seconds"),
                                  ("request_elapsed_seconds", "request_elapsed_seconds")):
                if row[source] is not None:
                    bucket[field].append(row[source])
        for bucket in by_model.values():
            dispatched_count = bucket["valid"] + bucket["failed"]
            bucket["success_rate"] = (
                round(bucket["valid"] / dispatched_count, 6)
                if dispatched_count else None)
            for field in ("backend_seconds", "queue_seconds", "request_elapsed_seconds"):
                bucket[field] = self._latency_percentiles(bucket[field])
        round_performance = []
        for row in round_rows:
            stats = json.loads(row["stats"])
            if "performance" in stats:
                round_performance.append(stats["performance"])
        return {
            "capture_enabled": True,
            "timing_clock": "time.perf_counter for durations; local ISO wall time for correlation",
            "advance_invocations": len(calls),
            "invocation_wall_seconds_sum": round(elapsed, 6),
            "first_invocation_started_at": calls[0]["started_at"] if calls else None,
            "last_invocation_ended_at": calls[-1]["ended_at"] if calls else None,
            "completed_requests": len(dispatched),
            "valid_decisions": len(valid),
            "failed_dispatched_requests": sum(row["status"] == "failed" for row in dispatched),
            "deadline_exceeded_requests": len(timed_out),
            "not_dispatched": sum(row["status"] == "not_dispatched" for row in requests),
            "unknown_usage_calls": unknown_usage,
            "completed_request_throughput_per_second": (
                round(len(dispatched) / elapsed, 6) if elapsed > 0 else None),
            "effective_decision_throughput_per_second": (
                round(len(valid) / elapsed, 6) if elapsed > 0 else None),
            "request_elapsed_seconds": self._latency_percentiles(
                row["request_elapsed_seconds"] for row in dispatched),
            "backend_elapsed_seconds": self._latency_percentiles(
                row["backend_seconds"] for row in dispatched),
            "queue_seconds": self._latency_percentiles(
                row["queue_seconds"] for row in dispatched),
            "maximum_observed_in_flight": max(
                (int(item.get("observed_max_in_flight", 0)) for item in round_performance),
                default=0),
            "by_model": by_model,
            "invocations": calls,
            "note": ("backend_elapsed_seconds times the backend method, including adapter, "
                     "transport and response parsing; it is not provider inference time. "
                     "Concurrent request durations overlap; invocation wall is the throughput "
                     "denominator. Captured invocations exclude idle time between calls."),
        }

    def summary(self, run_id):
        run = self._run_row(run_id)
        with self.store.connect() as db:
            rounds = [json.loads(r["stats"]) | {"round": r["round_number"]} for r in db.execute(
                "SELECT * FROM simulation_rounds WHERE run_id=? ORDER BY round_number", (run_id,))]
            participants = db.execute("SELECT COUNT(DISTINCT agent_id) AS n FROM simulation_actions "
                                      "WHERE run_id=? AND status='valid'", (run_id,)).fetchone()["n"]
            action_counts = {row["status"]: row["n"] for row in db.execute(
                "SELECT status, COUNT(*) AS n FROM simulation_actions WHERE run_id=? "
                "GROUP BY status", (run_id,))}
        expected_decisions = (json.loads(run["config"])["agent_count"] *
                              int(run["completed_rounds"]))
        all_decisions_valid = (
            run["status"] == "complete" and
            int(run["completed_rounds"]) == json.loads(run["config"])["rounds"] and
            action_counts.get("valid", 0) == expected_decisions and
            action_counts.get("failed", 0) == 0 and
            action_counts.get("not_dispatched", 0) == 0)
        config = json.loads(run["config"])
        summary = {"run_id": run_id, "case_id": run["case_id"], "graph_id": run["graph_id"],
                   "config": config, "status": run["status"],
                   "completed_rounds": run["completed_rounds"],
                   "actual_participants": participants,
                   "action_counts": action_counts,
                   "all_decisions_valid": all_decisions_valid,
                   "cost_cny": run["cost_cny"], "usage": json.loads(run["usage"]),
                   "rounds": rounds}
        # Whole-task opportunity reconciliation and the billing ledger, both independent
        # of how many action rows happened to be persisted.
        summary["opportunities"] = self._opportunity_buckets(rounds, config)
        summary["billing"] = self._billing_summary(run_id, Decimal(run["cost_cny"]))
        performance = self._performance_summary(run_id)
        if performance is not None:
            summary["performance"] = performance
        return summary

    def trajectory(self, run_id):
        self._run_row(run_id)
        with self.store.connect() as db:
            rows = db.execute("SELECT * FROM simulation_actions WHERE run_id=? "
                              "ORDER BY round_number, agent_id", (run_id,)).fetchall()
        return [{**dict(r), "decision": json.loads(r["decision"]) if r["decision"] else None,
                 "usage": json.loads(r["usage"]) if r["usage"] else None,
                 "request_audit": json.loads(r["request_audit"])
                 if r["request_audit"] else None,
                 "schema_diagnostics": json.loads(r["schema_diagnostics"])
                 if r["schema_diagnostics"] else None,
                 "observation_refs": json.loads(r["observation_refs"])} for r in rows]
