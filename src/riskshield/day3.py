"""Day 3 synchronous social simulation. A decision backend is supplied by the caller.

The engine never sends data to a model on its own. A test backend proves the
engineering path, while a real model run needs a separately authorized backend.
"""

import json
import random
import re
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Protocol

from pydantic import BaseModel, ConfigDict, Field

from riskshield.day2 import Day2Pipeline
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
MAX_TASK_BUDGET_CNY = Decimal("5.00")


class SimulationError(ValueError):
    pass


class SimulationCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    case_id: str = Field(pattern=r"^[a-zA-Z0-9_-]+$", max_length=100)
    graph_id: str = Field(pattern=r"^[a-zA-Z0-9_-]+$", max_length=100)
    agent_count: int = Field(ge=1, le=500)
    rounds: int = Field(ge=1, le=30)
    seed: int = 1
    role_offset: int = Field(default=0, ge=0, lt=len(ROLES))
    concurrency: int = Field(default=8, ge=1, le=128)
    budget_cny: Decimal = Field(default=MAX_TASK_BUDGET_CNY, gt=0,
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


def _clip(value, lo, hi):
    return max(lo, min(hi, round(value, 4)))


class Day3Simulation:
    def __init__(self, store: Store):
        self.store = store
        self.day2 = Day2Pipeline(store)
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
                    PRIMARY KEY (run_id, round_number, agent_id)
                );
                CREATE TABLE IF NOT EXISTS simulation_rounds (
                    run_id TEXT NOT NULL, round_number INTEGER NOT NULL, stats TEXT NOT NULL,
                    PRIMARY KEY (run_id, round_number)
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
            ):
                if name not in action_columns:
                    db.execute(f"ALTER TABLE simulation_actions ADD COLUMN {name} {declaration}")

    def create_run(self, case_id: str, graph_id: str, *, agent_count: int,
                   rounds: int, seed: int = 1, concurrency: int = 8,
                   budget_cny: Decimal = MAX_TASK_BUDGET_CNY,
                   role_offset: int = 0) -> str:
        if not (1 <= agent_count <= 500 and 1 <= rounds <= 30 and
                1 <= concurrency <= 128 and 0 <= role_offset < len(ROLES)):
            raise SimulationError(
                "agent_count, rounds, concurrency or role_offset outside Day 3 limits")
        budget_cny = Decimal(str(budget_cny))
        if not Decimal("0") < budget_cny <= MAX_TASK_BUDGET_CNY:
            raise SimulationError("single-run budget must be within ¥5.00")
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

    def _context(self, run, agents, round_number):
        """Freeze every observation before any decision is submitted."""
        graph = self.day2.graph(run["graph_id"])
        cutoff = datetime.fromisoformat(graph["cutoff"])
        run_config = json.loads(run["config"])
        with self.store.connect() as db:
            inbox = [dict(r) for r in db.execute(
                "SELECT * FROM simulation_messages WHERE run_id=? AND round_number=?",
                (run["run_id"], round_number - 1))]
            memories = [dict(r) for r in db.execute(
                "SELECT * FROM simulation_memories WHERE run_id=? AND round_number<?",
                (run["run_id"], round_number))]
        inbox_by_agent, memories_by_agent = {}, {}
        for msg in inbox:
            inbox_by_agent.setdefault(msg["recipient_id"], []).append({
                "message_id": msg["message_id"], "sender_id": msg["sender_id"],
                "action": msg["action"], "evidence_ids": json.loads(msg["evidence_ids"]),
                "content": msg["content"]})
        for memory in memories:
            memories_by_agent.setdefault(memory["agent_id"], []).append({
                "memory_id": memory["memory_id"], "round": memory["round_number"],
                "content": memory["content"], "refs": json.loads(memory["refs"])})
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
            observations.append({"run_id": run["run_id"], "round": round_number,
                                 "agent_id": agent["agent_id"], "persona": agent["persona"],
                                 "state": dict(agent["state"]), "neighbors": agent["neighbors"],
                                 "evidence": evidence, "messages": inbox_by_agent.get(agent["agent_id"], []),
                                 "memories": own_memories[:8], "cutoff": graph["cutoff"],
                                 "data_mode": run_config["data_mode"]})
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

    def advance(self, run_id: str, backend: DecisionBackend, *, max_rounds: int | None = None):
        if backend.mode not in {"test_substitute", "real_model"}:
            raise SimulationError("backend mode must declare test_substitute or real_model")
        if max_rounds is not None and max_rounds < 1:
            raise SimulationError("max_rounds must be positive")
        run = self._run_row(run_id)
        config = json.loads(run["config"])
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
            return self.summary(run_id)
        target = min(config["rounds"], run["completed_rounds"] + max_rounds) if max_rounds else config["rounds"]
        while int(run["completed_rounds"]) < target:
            round_number = int(run["completed_rounds"]) + 1
            agents = self._agents(run_id)
            observations = self._context(run, agents, round_number)
            quotes = [self._pricing(backend, obs) for obs in observations]
            with self.store.connect() as db:
                prior_models = {row["agent_id"]: row["requested_model"] for row in db.execute(
                    "SELECT agent_id, requested_model FROM simulation_actions WHERE run_id=? "
                    "AND requested_model IS NOT NULL GROUP BY agent_id", (run_id,))}
            for obs, quote in zip(observations, quotes):
                prior = prior_models.get(obs["agent_id"])
                if prior is not None and prior != quote.requested_model:
                    raise SimulationError("an agent's requested model cannot change within a run")
            remaining = Decimal(config["budget_cny"]) - Decimal(run["cost_cny"])
            round_reservation = sum((quote.reservation_cny for quote in quotes), Decimal("0"))
            if remaining < round_reservation:
                with self.store.connect() as db:
                    db.execute("UPDATE simulation_runs SET status='partial' WHERE run_id=?", (run_id,))
                break
            started = time.perf_counter()
            # map preserves agent order; all observations were frozen before entering the pool.
            def call(item):
                obs, quote = item
                try:
                    result = backend.decide(obs)
                    decision = AgentDecision.model_validate(result.decision)
                    allowed = {e["evidence_id"] for e in obs["evidence"]}
                    allowed.update(m["message_id"] for m in obs["messages"])
                    allowed.update(m["memory_id"] for m in obs["memories"])
                    if decision.agent_id != obs["agent_id"] or not set(decision.evidence_ids) <= allowed:
                        raise SimulationError("invalid agent or inaccessible citation")
                    cost, usage = self._cost(quote, result.usage)
                    actual_model = str(result.model).strip()[:100] if result.model else None
                    if cost > quote.reservation_cny:
                        return {"status": "failed", "decision": None, "usage": usage,
                                "cost": cost, "reserved": quote.reservation_cny,
                                "requested_model": quote.requested_model,
                                "price_version": quote.price_version, "model": actual_model,
                                "error": "ReservationExceeded"}
                    return {"status": "valid", "decision": decision.model_dump(),
                            "usage": usage, "cost": cost,
                            "reserved": quote.reservation_cny,
                            "requested_model": quote.requested_model,
                            "price_version": quote.price_version,
                            "model": actual_model, "error": None}
                except Exception as exc:
                    # Record a category, never provider response bodies or private payloads.
                    return {"status": "failed", "decision": None, "usage": None,
                            "cost": quote.reservation_cny, "reserved": quote.reservation_cny,
                            "requested_model": quote.requested_model,
                            "price_version": quote.price_version,
                            "model": None, "error": type(exc).__name__}
            with ThreadPoolExecutor(max_workers=config["concurrency"]) as pool:
                outcomes = list(pool.map(call, zip(observations, quotes)))
            round_cost = sum((o["cost"] for o in outcomes), Decimal("0"))
            total_cost = Decimal(run["cost_cny"]) + round_cost
            over_budget = total_cost > Decimal(config["budget_cny"])
            usage = json.loads(run["usage"])
            usage.setdefault("cached_input_tokens", 0)
            usage.setdefault("by_model", {})
            for outcome in outcomes:
                model_usage = usage["by_model"].setdefault(outcome["requested_model"], {
                    "calls": 0, "prompt_tokens": 0, "cached_input_tokens": 0,
                    "completion_tokens": 0, "unknown_usage_calls": 0,
                    "reserved_cny": "0", "cost_cny": "0",
                    "price_version": outcome["price_version"], "actual_models": {},
                })
                model_usage["calls"] += 1
                model_usage["reserved_cny"] = str(
                    Decimal(model_usage["reserved_cny"]) + outcome["reserved"])
                model_usage["cost_cny"] = str(
                    Decimal(model_usage["cost_cny"]) + outcome["cost"])
                if outcome["model"]:
                    actual = model_usage["actual_models"]
                    actual[outcome["model"]] = actual.get(outcome["model"], 0) + 1
                if outcome["usage"] is None:
                    usage["unknown_usage_calls"] += 1
                    model_usage["unknown_usage_calls"] += 1
                else:
                    usage["prompt_tokens"] += outcome["usage"]["prompt_tokens"]
                    usage["cached_input_tokens"] += outcome["usage"]["cached_input_tokens"]
                    usage["completion_tokens"] += outcome["usage"]["completion_tokens"]
                    model_usage["prompt_tokens"] += outcome["usage"]["prompt_tokens"]
                    model_usage["cached_input_tokens"] += outcome["usage"]["cached_input_tokens"]
                    model_usage["completion_tokens"] += outcome["usage"]["completion_tokens"]
            state_by_id = {a["agent_id"]: dict(a["state"]) for a in agents}
            messages = []
            memory_rows = []
            actions = []
            for obs, outcome in zip(observations, outcomes):
                aid = obs["agent_id"]
                decision = outcome["decision"]
                refs = {"evidence": [e["evidence_id"] for e in obs["evidence"]],
                        "messages": [m["message_id"] for m in obs["messages"]],
                        "memories": [m["memory_id"] for m in obs["memories"]]}
                actions.append((run_id, round_number, aid, outcome["status"], _json(refs),
                                _json(decision) if decision else None,
                                _json(outcome["usage"]) if outcome["usage"] else None,
                                outcome["requested_model"], outcome["model"],
                                outcome["price_version"], str(outcome["reserved"]),
                                str(outcome["cost"]), outcome["error"]))
                for msg in obs["messages"]:
                    memory_rows.append((run_id, aid,
                                        f"memmsg_{round_number}_{aid}_{msg['message_id']}",
                                        round_number,
                                        f"heard {msg['sender_id']} {msg['action']}: {msg['content'][:200]}",
                                        _json([msg["message_id"], *msg["evidence_ids"]])))
                if decision is None:
                    continue
                state = state_by_id[aid]
                state["emotion"] = _clip(state["emotion"] + SIGNALS[decision["action"]]
                                         * obs["persona"]["susceptibility"], -1, 1)
                state["trust"] = _clip(state["trust"] + (0.015 if decision["action"] ==
                                        "seek_clarification" else -0.01), 0, 1)
                memory_rows.append((run_id, aid, f"mem_{round_number}_{aid}", round_number,
                                    decision["action"] + " " + decision["reason"],
                                    _json(decision["evidence_ids"])))
                if decision["action"] in {"share", "comment", "express_complaint_intent"}:
                    for neighbor in obs["neighbors"]:
                        messages.append((run_id, f"msg_{round_number}_{aid}_{neighbor}", round_number,
                                         aid, neighbor, decision["action"],
                                         _json([e for e in decision["evidence_ids"]
                                                if e in {x["evidence_id"] for x in obs["evidence"]}]),
                                         decision["reason"]))
            # Incoming messages affect next-round state simultaneously, after all decisions.
            for _, _, _, sender, recipient, action, _, _ in messages:
                state_by_id[recipient]["emotion"] = _clip(
                    state_by_id[recipient]["emotion"] + SIGNALS[action] * 0.25, -1, 1)
            valid = sum(o["status"] == "valid" for o in outcomes)
            cost_by_model = {}
            reserved_by_model = {}
            for outcome in outcomes:
                name = outcome["requested_model"]
                cost_by_model[name] = str(Decimal(cost_by_model.get(name, "0")) + outcome["cost"])
                reserved_by_model[name] = str(
                    Decimal(reserved_by_model.get(name, "0")) + outcome["reserved"])
            stats = {"configured_agents": len(agents), "decision_requests": len(outcomes),
                     "model_decision_agents": len(outcomes),
                     "local_state_update_agents": len(state_by_id),
                     "valid_decisions": valid, "active_decisions": sum(
                         o["decision"] is not None and o["decision"]["action"] != "observe"
                         for o in outcomes), "failed_decisions": len(outcomes) - valid,
                      "messages": len(messages), "reserved_cny": str(round_reservation),
                      "reserved_by_model": reserved_by_model,
                      "cost_cny": str(round_cost), "cost_by_model": cost_by_model,
                     "elapsed_seconds": round(time.perf_counter() - started, 3)}
            status = "complete" if round_number == config["rounds"] and not over_budget else "running"
            if over_budget:
                status = "partial"
            with self.store.connect() as db:
                db.execute("BEGIN IMMEDIATE")
                db.executemany(
                    "INSERT INTO simulation_actions (run_id, round_number, agent_id, status, "
                    "observation_refs, decision, usage, requested_model, model, price_version, "
                    "reserved_cny, cost_cny, error) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    actions)
                db.executemany(
                    "INSERT INTO simulation_messages "
                    "(run_id, message_id, round_number, sender_id, recipient_id, action, "
                    "evidence_ids, content) VALUES (?, ?, ?, ?, ?, ?, ?, ?)", messages)
                db.executemany("INSERT INTO simulation_memories VALUES (?, ?, ?, ?, ?, ?)", memory_rows)
                db.executemany("UPDATE simulation_agents SET state=? WHERE run_id=? AND agent_id=?",
                               [(_json(state), run_id, aid) for aid, state in state_by_id.items()])
                db.execute("INSERT INTO simulation_rounds VALUES (?, ?, ?)",
                           (run_id, round_number, _json(stats)))
                db.execute("UPDATE simulation_runs SET status=?, completed_rounds=?, cost_cny=?, usage=? "
                           "WHERE run_id=?", (status, round_number, str(total_cost), _json(usage), run_id))
            run = self._run_row(run_id)
            if over_budget:
                break
        return self.summary(run_id)

    def summary(self, run_id):
        run = self._run_row(run_id)
        with self.store.connect() as db:
            rounds = [json.loads(r["stats"]) | {"round": r["round_number"]} for r in db.execute(
                "SELECT * FROM simulation_rounds WHERE run_id=? ORDER BY round_number", (run_id,))]
            participants = db.execute("SELECT COUNT(DISTINCT agent_id) AS n FROM simulation_actions "
                                      "WHERE run_id=? AND status='valid'", (run_id,)).fetchone()["n"]
        return {"run_id": run_id, "case_id": run["case_id"], "graph_id": run["graph_id"],
                "config": json.loads(run["config"]), "status": run["status"],
                "completed_rounds": run["completed_rounds"], "actual_participants": participants,
                "cost_cny": run["cost_cny"], "usage": json.loads(run["usage"]), "rounds": rounds}

    def trajectory(self, run_id):
        self._run_row(run_id)
        with self.store.connect() as db:
            rows = db.execute("SELECT * FROM simulation_actions WHERE run_id=? "
                              "ORDER BY round_number, agent_id", (run_id,)).fetchall()
        return [{**dict(r), "decision": json.loads(r["decision"]) if r["decision"] else None,
                 "usage": json.loads(r["usage"]) if r["usage"] else None,
                 "observation_refs": json.loads(r["observation_refs"])} for r in rows]
