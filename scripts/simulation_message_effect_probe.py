"""Paired runtime-message and memory effect probes (local, offline, no model calls).

This script answers two separate questions and keeps them separate, because they are
not the same experiment and must not be conflated:

* **Message control** -- evidence, persona, state, neighbours and own memory are held
  fixed; the only thing that changes is whether the decision component can read the
  runtime message the agent was sent.
* **Memory control** -- evidence, persona, state, neighbours and received messages are
  held fixed; the only thing that changes is whether the decision component can read
  the agent's own persisted memory.

In both cases the script records the *effective observation* that the decision
component actually received, normalises it, and hashes it. That is what is compared --
not the pre-intervention observation, which would be a different and weaker claim.

Two limits are stated in the output rather than hidden:

1. The decision rule here is a small deterministic function of the observation. A
   decision difference therefore proves the engine routes the runtime message and the
   isolated memory into the decision input and that the difference is attributable to
   that input. It does **not** prove that a real language model would behave the same
   way. That needs a separately authorized real-model experiment.
2. A single-round paired contrast is a *controlled same-instant* comparison. After
   several rounds the two arms diverge for many reasons at once, so this script does
   not claim "only the message differs" for multi-round divergence.

Usage (from the repository root)::

    uv run --locked --offline python scripts/simulation_message_effect_probe.py \
        --out path/to/evidence.json
"""

import argparse
import hashlib
import json
import platform
import sys
import tempfile
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
for candidate in (REPO_ROOT, REPO_ROOT / "src"):
    if str(candidate) not in sys.path:
        sys.path.insert(0, str(candidate))

from riskshield.simulation import Day3Simulation, DecisionResult  # noqa: E402
from riskshield.schemas import AgentDecision  # noqa: E402
from riskshield.store import Store  # noqa: E402

BEIJING = timezone(timedelta(hours=8))
PROBE_VERSION = "message-memory-effect-probe-v2"
RECEIVER = "agent_0001"
SENDER = "agent_0000"
# Fields the decision rule is allowed to read. Everything else is engine bookkeeping
# (run identity, round number) and is excluded from the equality comparison.
INTERVENTION_FIELDS = {"messages", "memories"}
BOOKKEEPING_FIELDS = {"run_id"}


def beijing_now() -> str:
    return datetime.now(BEIJING).isoformat(timespec="seconds")


def canonical_sha256(value) -> str:
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                     separators=(",", ":")).encode("utf-8")).hexdigest()


def normalise(observation: dict, *, drop: set) -> dict:
    """The comparable content of an observation, minus the intervened fields."""
    return {key: value for key, value in observation.items()
            if key not in drop and key not in BOOKKEEPING_FIELDS}


def sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


class ProbeBackend:
    """One decision rule for all arms; visibility of message/memory is the variable.

    ``read_messages`` and ``read_memories`` only control what this component is allowed
    to read. The engine has already frozen and hashed the observation by then, so the
    frozen-input record still covers everything that was actually visible in the round.
    """

    mode = "test_substitute"
    reservation_cny = 0
    input_cny_per_million = 0
    cached_input_cny_per_million = 0
    output_cny_per_million = 0
    price_version = "zero-cost-probe-v2"
    model = "message-memory-effect-probe"

    def __init__(self, *, arm: str, read_messages: bool, read_memories: bool,
                 delay_seconds: float = 0.0):
        self.arm = arm
        self.read_messages = read_messages
        self.read_memories = read_memories
        self.delay_seconds = delay_seconds
        self.frozen_observations: list[dict] = []
        self.effective_observations: list[dict] = []

    def decide(self, observation) -> DecisionResult:
        frozen = json.loads(json.dumps(observation))
        self.frozen_observations.append(frozen)
        messages = frozen["messages"] if self.read_messages else []
        memories = frozen["memories"] if self.read_memories else []
        effective = {**frozen, "messages": messages, "memories": memories}
        self.effective_observations.append(effective)
        if self.delay_seconds:
            time.sleep(self.delay_seconds)

        agent_id = effective["agent_id"]
        if messages:
            message = min(messages, key=lambda item: item["message_id"])
            action = "comment"
            reference = message["message_id"]
            reason = "Reacted to the runtime message delivered in the previous round."
        elif memories and effective["round"] > 1:
            memory = max(memories, key=lambda item: (item["round"], item["memory_id"]))
            action = "seek_clarification"
            reference = memory["memory_id"]
            reason = "Own persisted memory was readable; no runtime message was."
        elif agent_id == SENDER:
            action = "share"
            reference = effective["evidence"][0]["evidence_id"]
            reason = "Share the visible cutoff evidence to neighbours."
        else:
            action = "observe"
            reference = effective["evidence"][0]["evidence_id"]
            reason = "Observe first; nothing has been received yet."
        return DecisionResult(AgentDecision(agent_id=agent_id, action=action,
                                           evidence_ids=[reference], reason=reason),
                              usage={"prompt_tokens": 0, "completion_tokens": 0},
                              model=self.model)


def run_arm(root: Path, *, arm: str, agents: int, rounds: int, seed: int,
            read_messages: bool, read_memories: bool):
    from tests.fixtures.day3_synthetic_case import (  # local test fixture
        CASE_ID, GRAPH_ID, prepare_synthetic_case,
    )
    store = Store(root / f"{arm}.db")
    engine = Day3Simulation(store)
    prepare_synthetic_case(store)
    run_id = engine.create_run(CASE_ID, GRAPH_ID, agent_count=agents, rounds=rounds,
                               concurrency=agents, seed=seed)
    backend = ProbeBackend(arm=arm, read_messages=read_messages,
                           read_memories=read_memories)
    result = engine.advance(run_id, backend, collect_performance=True)
    return store, engine, run_id, backend, result


def effective_at(backend, *, round_number: int, agent_id: str) -> dict:
    return next(o for o in backend.effective_observations
                if o["round"] == round_number and o["agent_id"] == agent_id)


def frozen_at(backend, *, round_number: int, agent_id: str) -> dict:
    return next(o for o in backend.frozen_observations
                if o["round"] == round_number and o["agent_id"] == agent_id)


def decision_at(engine, run_id: str, *, round_number: int, agent_id: str) -> dict:
    rows = [row for row in engine.trajectory(run_id)
            if row["round_number"] == round_number and row["agent_id"] == agent_id]
    return rows[0]


def isolation_report(store, engine, run_id, backend, agents: int) -> dict:
    """No foreign memory, no misdelivered message, no current/future-round visibility."""
    with store.connect() as db:
        memory_rows = [dict(row) for row in db.execute(
            "SELECT * FROM simulation_memories WHERE run_id=?", (run_id,))]
        message_rows = [dict(row) for row in db.execute(
            "SELECT * FROM simulation_messages WHERE run_id=?", (run_id,))]
    stored_messages = {row["message_id"]: row for row in message_rows}
    misdelivered = not_previous_round = 0
    for observation in backend.frozen_observations:
        for message in observation["messages"]:
            stored = stored_messages.get(message["message_id"])
            if stored is None or stored["recipient_id"] != observation["agent_id"]:
                misdelivered += 1
            elif stored["round_number"] != observation["round"] - 1:
                not_previous_round += 1
    return {
        "memory_rows": len(memory_rows),
        "memory_rows_naming_another_agent": sum(
            1 for row in memory_rows if row["agent_id"] not in row["memory_id"]),
        "message_rows": len(message_rows),
        "messages_delivered_to_wrong_inbox": misdelivered,
        "messages_not_from_the_previous_round": not_previous_round,
        "foreign_memories_visible": sum(
            1 for observation in backend.frozen_observations
            for memory in observation["memories"]
            if observation["agent_id"] not in memory["memory_id"]),
        "memories_from_current_or_future_round": sum(
            1 for observation in backend.frozen_observations
            for memory in observation["memories"]
            if memory["round"] >= observation["round"]),
        "distinct_agent_states": len({
            canonical_sha256(observation["state"])
            for observation in backend.frozen_observations
            if observation["round"] == 1}),
        "agents": agents,
    }


def paired_contrast(root: Path, *, name: str, intervened: str, agents: int, rounds: int,
                    seed: int, treatment_reads, control_reads) -> dict:
    """Run one treatment/control pair and diff the effective decision input."""
    treatment = run_arm(root, arm=f"{name}-treatment", agents=agents, rounds=rounds,
                        seed=seed, **treatment_reads)
    control = run_arm(root, arm=f"{name}-control", agents=agents, rounds=rounds,
                      seed=seed, **control_reads)
    t_store, t_engine, t_run, t_backend, t_result = treatment
    c_store, c_engine, c_run, c_backend, c_result = control

    # Compare the effective decision input at the point of interest. The receiver makes
    # its first message/memory-dependent decision in round 2.
    drop = {intervened}
    t_eff = effective_at(t_backend, round_number=2, agent_id=RECEIVER)
    c_eff = effective_at(c_backend, round_number=2, agent_id=RECEIVER)
    t_norm, c_norm = normalise(t_eff, drop=drop), normalise(c_eff, drop=drop)
    differing = sorted(key for key in set(t_eff) | set(c_eff)
                       if key not in drop and key not in BOOKKEEPING_FIELDS
                       and t_eff.get(key) != c_eff.get(key))

    t_frozen = frozen_at(t_backend, round_number=2, agent_id=RECEIVER)
    c_frozen = frozen_at(c_backend, round_number=2, agent_id=RECEIVER)

    t_decision = decision_at(t_engine, t_run, round_number=2, agent_id=RECEIVER)
    c_decision = decision_at(c_engine, c_run, round_number=2, agent_id=RECEIVER)
    t_refs = t_decision["observation_refs"]
    c_refs = c_decision["observation_refs"]
    id_field = "message_id" if intervened == "messages" else "memory_id"
    intervened_ids = {item[id_field] for item in t_eff[intervened]}

    return {
        "name": name,
        "intervened_field": intervened,
        "held_fixed": sorted(set(INTERVENTION_FIELDS) - {intervened}),
        "effective_input_sha256": {
            "treatment": canonical_sha256(t_norm),
            "control": canonical_sha256(c_norm),
            "identical": t_norm == c_norm,
        },
        "effective_input_differing_keys": differing,
        "intervened_value": {
            "treatment_count": len(t_eff[intervened]),
            "control_count": len(c_eff[intervened]),
            "treatment": t_eff[intervened],
            "control": c_eff[intervened],
        },
        "frozen_input_equal_except_intervention": (
            normalise(t_frozen, drop={intervened}) ==
            normalise(c_frozen, drop={intervened})),
        "state_equal": t_eff["state"] == c_eff["state"],
        "persona_equal": t_eff["persona"] == c_eff["persona"],
        "neighbours_equal": t_eff["neighbors"] == c_eff["neighbors"],
        "evidence_equal": t_eff["evidence"] == c_eff["evidence"],
        "recipient": RECEIVER,
        "round": 2,
        "decision": {
            "treatment": t_decision["decision"],
            "control": c_decision["decision"],
            "changed": t_decision["decision"] != c_decision["decision"],
            "treatment_cited_intervention": bool(
                set(t_decision["decision"]["evidence_ids"]) & intervened_ids),
            "control_cited_intervention": bool(
                set(c_decision["decision"]["evidence_ids"]) & intervened_ids),
            "treatment_cited_own_memory": bool(
                set(t_decision["decision"]["evidence_ids"]) & set(t_refs["memories"])),
            "control_cited_own_memory": bool(
                set(c_decision["decision"]["evidence_ids"]) & set(c_refs["memories"])),
        },
        "runs": {
            "treatment": {"run_id": t_run, "status": t_result["status"],
                          "action_counts": t_result["action_counts"],
                          "all_decisions_valid": t_result["all_decisions_valid"]},
            "control": {"run_id": c_run, "status": c_result["status"],
                        "action_counts": c_result["action_counts"],
                        "all_decisions_valid": c_result["all_decisions_valid"]},
        },
        "isolation": {
            "treatment": isolation_report(t_store, t_engine, t_run, t_backend, agents),
            "control": isolation_report(c_store, c_engine, c_run, c_backend, agents),
        },
    }


def unrelated_message_and_address_check(root: Path, *, agents: int, seed: int) -> dict:
    """A message addressed elsewhere is never visible, and only the previous round is."""
    from tests.fixtures.day3_synthetic_case import CASE_ID, GRAPH_ID, prepare_synthetic_case
    store = Store(root / "isolation.db")
    engine = Day3Simulation(store)
    prepare_synthetic_case(store)
    run_id = engine.create_run(CASE_ID, GRAPH_ID, agent_count=agents, rounds=3,
                               concurrency=2, seed=seed)
    backend = ProbeBackend(arm="isolation", read_messages=True, read_memories=True)
    engine.advance(run_id, backend, collect_performance=True)

    with store.connect() as db:
        message_rows = [dict(row) for row in db.execute(
            "SELECT * FROM simulation_messages WHERE run_id=?", (run_id,))]
        memory_rows = [dict(row) for row in db.execute(
            "SELECT * FROM simulation_memories WHERE run_id=?", (run_id,))]
    stored = {row["message_id"]: row for row in message_rows}
    violations = []
    for observation in backend.frozen_observations:
        for message in observation["messages"]:
            row = stored.get(message["message_id"])
            if row is None:
                violations.append("unknown_message")
            elif row["recipient_id"] != observation["agent_id"]:
                violations.append("wrong_recipient")
            elif row["round_number"] != observation["round"] - 1:
                violations.append("not_previous_round")
        for memory in observation["memories"]:
            if observation["agent_id"] not in memory["memory_id"]:
                violations.append("foreign_memory")
            elif memory["round"] >= observation["round"]:
                violations.append("current_or_future_memory")
    # An agent's inbox must hold exactly the messages stored for it in the prior round.
    inbox_mismatches = 0
    for observation in backend.frozen_observations:
        expected = {row["message_id"] for row in message_rows
                    if row["recipient_id"] == observation["agent_id"]
                    and row["round_number"] == observation["round"] - 1}
        actual = {message["message_id"] for message in observation["messages"]}
        if expected != actual:
            inbox_mismatches += 1
    return {
        "isolated_runs_checked": 1,
        "agents": agents,
        "messages_considered": sum(len(o["messages"])
                                   for o in backend.frozen_observations),
        "memories_considered": sum(len(o["memories"])
                                   for o in backend.frozen_observations),
        "violations": violations,
        "violation_count": len(violations),
        "inbox_mismatches": inbox_mismatches,
        "stored_message_rows": len(message_rows),
        "stored_memory_rows": len(memory_rows),
        "all_stored_memories_owned": all(
            row["agent_id"] in row["memory_id"] for row in memory_rows),
        "run_id": run_id,
    }


def probe(*, agents: int = 3, rounds: int = 3, seed: int = 20261011) -> dict:
    with tempfile.TemporaryDirectory(prefix="message-effect-") as temp:
        root = Path(temp)
        message_pair = paired_contrast(
            root, name="message", intervened="messages", agents=agents, rounds=rounds,
            seed=seed,
            treatment_reads={"read_messages": True, "read_memories": True},
            control_reads={"read_messages": False, "read_memories": True})
        memory_pair = paired_contrast(
            root, name="memory", intervened="memories", agents=agents, rounds=rounds,
            seed=seed,
            treatment_reads={"read_messages": False, "read_memories": True},
            control_reads={"read_messages": False, "read_memories": False})
        isolation = unrelated_message_and_address_check(root, agents=agents, seed=seed)

        return {
            "schema": "simulation-message-memory-effect-probe-v2",
            "probe_version": PROBE_VERSION,
            "created_at": beijing_now(),
            "method": {
                "message_control": {
                    "intervened": "visibility of the runtime message",
                    "held_fixed": ["evidence", "persona", "neighbours", "own state",
                                   "own persisted memory", "round", "seed"],
                },
                "memory_control": {
                    "intervened": "visibility of the agent's own persisted memory",
                    "held_fixed": ["evidence", "persona", "neighbours", "own state",
                                   "received runtime messages", "round", "seed"],
                },
                "compared_artifact": ("the effective observation handed to the decision "
                                      "component, normalised and hashed"),
                "is_real_model": False,
                "model_calls": 0,
                "network_requests": 0,
                "supports": [
                    "the engine delivers a runtime message into the decision input",
                    "an agent's own memory is isolated per agent and enters its own input",
                    "foreign, misaddressed and future-round records are not visible",
                    "no duplicate or cross-inbox delivery under concurrency",
                ],
                "does_not_support": [
                    "that a real language model would react the same way",
                    "that message or memory causes any multi-round social outcome",
                    "any statement about real platform behaviour or model quality",
                ],
                "rule_based_limit": ("The decision component is a small deterministic "
                                     "rule. It proves input routing and attribution, not "
                                     "model cognition. A real-model experiment needs "
                                     "separate authorization."),
                "multi_round_limit": ("Only the single-round paired contrast is a "
                                      "controlled same-instant comparison. After rounds "
                                      "diverge, more than one factor differs, so no "
                                      "claim of 'only the message differs' is made for "
                                      "multi-round divergence."),
            },
            "binding": {
                "git_commit": git_commit(),
                "git_dirty": git_dirty(),
                "simulation_py_sha256": sha256_file(
                    REPO_ROOT / "src" / "riskshield" / "simulation.py"),
                "python": platform.python_version(),
                "platform": platform.platform(),
            },
            "message_control": message_pair,
            "memory_control": memory_pair,
            "isolation_checks": isolation,
            "real_model_experiment_plan": real_model_experiment_plan(),
        }


def real_model_experiment_plan() -> dict:
    """A configuration plan for a future authorized real-model contrast. Not executed."""
    return {
        "status": "not_executed_no_authorization",
        "note": ("This plan is recorded so a future authorized run is reproducible. No "
                 "real-model call was made and none is implied by this evidence file."),
        "design": "paired same-instant contrast, one arm per condition",
        "arms": ["message-visible", "message-hidden", "memory-visible", "memory-hidden"],
        "controls": ["identical frozen observation payload except the intervened field",
                     "identical model id and decoding settings",
                     "identical prompt template and canonicalisation"],
        "required_records": ["prepared request canonical JSON sha256 per arm",
                             "provider model version string",
                             "token usage and price version",
                             "raw decision plus validated decision",
                             "wall time and attempt count"],
        "must_hold": ["cutoff-bounded evidence only; no future or post-cutoff material",
                      "no evaluation labels or future outcomes in any prompt",
                      "no retry that could double-count a decision",
                      "per-call cost reservation within the confirmed budget"],
        "planned_size": {"calls_per_arm": 1, "arms": 4, "total_calls": 4},
    }


def git_commit():
    import subprocess
    try:
        return subprocess.run(["git", "-C", str(REPO_ROOT), "rev-parse", "HEAD"],
                              capture_output=True, text=True, timeout=30,
                              check=True).stdout.strip()
    except Exception:
        return None


def git_dirty():
    import subprocess
    try:
        return bool(subprocess.run(["git", "-C", str(REPO_ROOT), "status", "--porcelain"],
                                   capture_output=True, text=True, timeout=30,
                                   check=True).stdout.strip())
    except Exception:
        return None


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--agents", type=int, default=3)
    parser.add_argument("--rounds", type=int, default=3)
    parser.add_argument("--seed", type=int, default=20261011)
    parser.add_argument("--out", default=None)
    arguments = parser.parse_args(argv)
    report = probe(agents=arguments.agents, rounds=arguments.rounds, seed=arguments.seed)
    payload = json.dumps(report, ensure_ascii=False, indent=1)
    if arguments.out:
        target = Path(arguments.out)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(payload, encoding="utf-8")
        print(f"evidence written to {target}")
    else:
        print(payload)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
