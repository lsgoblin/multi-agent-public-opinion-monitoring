"""Paired runtime-message effect probe (local, offline, no model calls).

Purpose: show, with traceable evidence, that a simulated runtime message changes
what an agent does next -- and that each agent's state and memory stay its own.

Method: one seeded run is executed twice over the same fictional case and graph.
The two arms share personas, neighbours, evidence, per-agent state and per-agent
memory. The only difference is whether an agent sees the runtime message it was
sent. The receiving arm is therefore the treatment, and the arm that cannot see
the message is the control. Both arms use the same decision rule, so a decision
difference at the message-dependent point is attributable to the message.

The probe was deliberately built so that the *only* possible input difference at
the point of interest is the received-message list: the control arm strips the
messages after the engine froze the observation, so both arms receive byte-equal
observations apart from ``messages``. That equality is asserted and hashed here
rather than assumed.

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
PROBE_VERSION = "message-effect-probe-v1"
RECEIVER = "agent_0001"
SENDER = "agent_0000"


def beijing_now() -> str:
    return datetime.now(BEIJING).isoformat(timespec="seconds")


def canonical_sha256(value) -> str:
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                     separators=(",", ":")).encode("utf-8")).hexdigest()


def without_messages(observation: dict) -> dict:
    """Everything the receiver was told, apart from the runtime messages.

    ``run_id`` is excluded as well: it identifies the run and is never visible to a
    decision rule, so two arms can only ever differ on the content below it.
    """
    return {key: value for key, value in observation.items()
            if key not in {"messages", "run_id"}}


def sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


class ProbeBackend:
    """Same decision rule in both arms; the control arm simply cannot see messages.

    ``sees_messages=False`` removes the runtime messages from the observation the
    rule reads. The engine has already frozen and hashed the observation by then,
    so the frozen-input record still covers what was actually in the round.
    """

    mode = "test_substitute"
    reservation_cny = 0
    input_cny_per_million = 0
    output_cny_per_million = 0
    price_version = "zero-cost-probe-v1"
    model = "message-effect-probe"

    def __init__(self, *, sees_messages: bool, delay_seconds: float = 0.0):
        self.sees_messages = sees_messages
        self.delay_seconds = delay_seconds
        self.frozen_observations: list[dict] = []
        self.effective_observations: list[dict] = []

    def decide(self, observation) -> DecisionResult:
        frozen = json.loads(json.dumps(observation))
        self.frozen_observations.append(frozen)
        agent_id = observation["agent_id"]
        messages = observation["messages"] if self.sees_messages else []
        self.effective_observations.append({**frozen, "messages": messages})
        if self.delay_seconds:
            time.sleep(self.delay_seconds)
        memories = observation["memories"]
        if messages:
            message = min(messages, key=lambda item: item["message_id"])
            action = "comment"
            reference = message["message_id"]
            reason = "Reacted to the runtime message delivered in the previous round."
        elif memories and observation["round"] > 1:
            memory = max(memories, key=lambda item: (item["round"], item["memory_id"]))
            action = "seek_clarification"
            reference = memory["memory_id"]
            reason = "Own prior memory only; no runtime message was visible."
        elif agent_id == SENDER:
            action = "share"
            reference = observation["evidence"][0]["evidence_id"]
            reason = "Share the visible cutoff evidence to neighbours."
        else:
            action = "observe"
            reference = observation["evidence"][0]["evidence_id"]
            reason = "Observe first; nothing has been received yet."
        return DecisionResult(AgentDecision(agent_id=agent_id, action=action,
                                           evidence_ids=[reference], reason=reason),
                              usage={"prompt_tokens": 0, "completion_tokens": 0},
                              model=self.model)


def run_arm(root: Path, *, sees_messages: bool, agents: int, rounds: int, seed: int):
    from tests.fixtures.day3_synthetic_case import (  # local test fixture
        CASE_ID, GRAPH_ID, prepare_synthetic_case,
    )
    store = Store(root / ("treatment.db" if sees_messages else "control.db"))
    engine = Day3Simulation(store)
    prepare_synthetic_case(store)
    run_id = engine.create_run(CASE_ID, GRAPH_ID, agent_count=agents, rounds=rounds,
                               concurrency=agents, seed=seed)
    backend = ProbeBackend(sees_messages=sees_messages)
    result = engine.advance(run_id, backend, collect_performance=True)
    return store, engine, run_id, backend, result


def probe(*, agents: int = 3, rounds: int = 3, seed: int = 20261011) -> dict:
    with tempfile.TemporaryDirectory(prefix="message-effect-") as temp:
        root = Path(temp)
        treatment = run_arm(root, sees_messages=True, agents=agents, rounds=rounds,
                            seed=seed)
        control = run_arm(root, sees_messages=False, agents=agents, rounds=rounds,
                          seed=seed)
        t_store, t_engine, t_run, t_backend, t_result = treatment
        c_store, c_engine, c_run, c_backend, c_result = control

        # The receiver's round-2 view: identical except for the message list.
        t_round2_frozen = next(o for o in t_backend.frozen_observations
                               if o["round"] == 2 and o["agent_id"] == RECEIVER)
        c_round2_frozen = next(o for o in c_backend.frozen_observations
                               if o["round"] == 2 and o["agent_id"] == RECEIVER)
        t_round2 = next(o for o in t_backend.effective_observations
                        if o["round"] == 2 and o["agent_id"] == RECEIVER)
        c_round2 = next(o for o in c_backend.effective_observations
                        if o["round"] == 2 and o["agent_id"] == RECEIVER)
        frozen_equal = without_messages(t_round2_frozen) == without_messages(
            c_round2_frozen)
        stripped_equal = without_messages(t_round2) == without_messages(c_round2)
        differing_keys = sorted(
            key for key in set(t_round2_frozen) | set(c_round2_frozen)
            if key not in {"messages", "run_id"} and
            t_round2_frozen.get(key) != c_round2_frozen.get(key))

        def decision(engine, run_id, round_number, agent_id):
            rows = [row for row in engine.trajectory(run_id)
                    if row["round_number"] == round_number and row["agent_id"] == agent_id]
            return rows[0]

        t_action = decision(t_engine, t_run, 2, RECEIVER)
        c_action = decision(c_engine, c_run, 2, RECEIVER)
        t_sender_r1 = decision(t_engine, t_run, 1, SENDER)
        c_sender_r1 = decision(c_engine, c_run, 1, SENDER)

        # Isolation: every memory row belongs to the agent named in its id, and a
        # request only ever sees its own memory plus messages addressed to it.
        isolation = {}
        for name, (store, engine, run_id, backend) in {
                "treatment": (t_store, t_engine, t_run, t_backend),
                "control": (c_store, c_engine, c_run, c_backend)}.items():
            with store.connect() as db:
                memory_rows = [dict(row) for row in db.execute(
                    "SELECT * FROM simulation_memories WHERE run_id=?", (run_id,))]
                message_rows = [dict(row) for row in db.execute(
                    "SELECT * FROM simulation_messages WHERE run_id=?", (run_id,))]
            stored_messages = {row["message_id"]: row for row in message_rows}
            # The inbox carries no recipient field: an agent can only hold a message
            # whose stored recipient is that agent and whose round is the previous one.
            misdelivered = future_visible = 0
            for observation in backend.frozen_observations:
                for message in observation["messages"]:
                    stored = stored_messages.get(message["message_id"])
                    if (stored is None or
                            stored["recipient_id"] != observation["agent_id"]):
                        misdelivered += 1
                    elif stored["round_number"] != observation["round"] - 1:
                        future_visible += 1
            isolation[name] = {
                "memory_rows": len(memory_rows),
                "memory_rows_naming_another_agent": sum(
                    1 for row in memory_rows if row["agent_id"] not in row["memory_id"]),
                "message_rows": len(message_rows),
                "messages_delivered_to_wrong_inbox": misdelivered,
                "messages_not_from_the_previous_round": future_visible,
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

        return {
            "schema": "simulation-message-effect-probe-v1",
            "probe_version": PROBE_VERSION,
            "created_at": beijing_now(),
            "method": {
                "arms": ["treatment_sees_runtime_message", "control_cannot_see_message"],
                "shared": ["seed", "personas", "neighbours", "evidence", "own memory",
                           "per-agent state", "round count", "concurrency"],
                "differing": ["whether the receiver was shown the runtime message"],
                "attribution": ("Both arms use the same decision rule on the same frozen "
                                "observation apart from the received-message list, so a "
                                "decision difference at that point is attributable to the "
                                "message."),
                "is_real_model": False,
                "model_calls": 0,
                "network_requests": 0,
            },
            "binding": {
                "git_commit": git_commit(),
                "git_dirty": git_dirty(),
                "simulation_py_sha256": sha256_file(
                    REPO_ROOT / "src" / "riskshield" / "simulation.py"),
                "python": platform.python_version(),
                "platform": platform.platform(),
            },
            "frozen_input_check": {
                "receiver": RECEIVER,
                "round": 2,
                "frozen_observations_equal_apart_from_messages": frozen_equal,
                "effective_observations_equal_apart_from_messages": stripped_equal,
                "differing_observation_keys": differing_keys,
                "treatment_observation_sha256": canonical_sha256(t_round2_frozen),
                "control_observation_sha256": canonical_sha256(c_round2_frozen),
                "treatment_without_messages_sha256": canonical_sha256(
                    without_messages(t_round2_frozen)),
                "control_without_messages_sha256": canonical_sha256(
                    without_messages(c_round2_frozen)),
                "state_equal": t_round2_frozen["state"] == c_round2_frozen["state"],
                "memories_equal": t_round2_frozen["memories"] == c_round2_frozen["memories"],
                "evidence_equal": t_round2_frozen["evidence"] == c_round2_frozen["evidence"],
                "persona_equal": t_round2_frozen["persona"] == c_round2_frozen["persona"],
                "neighbours_equal": (t_round2_frozen["neighbors"] ==
                                     c_round2_frozen["neighbors"]),
                "treatment_frozen_messages": t_round2_frozen["messages"],
                "control_frozen_messages": c_round2_frozen["messages"],
                "treatment_effective_messages": t_round2["messages"],
                "control_effective_messages": c_round2["messages"],
            },
            "sender_round_1": {
                "treatment_action": t_sender_r1["decision"],
                "control_action": c_sender_r1["decision"],
                "identical": t_sender_r1["decision"] == c_sender_r1["decision"],
            },
            "receiver_round_2": {
                "treatment_decision": t_action["decision"],
                "control_decision": c_action["decision"],
                "treatment_status": t_action["status"],
                "control_status": c_action["status"],
                "decision_changed": t_action["decision"] != c_action["decision"],
                "treatment_cited_received_message": any(
                    reference == t_round2["messages"][0]["message_id"]
                    for reference in (t_action["decision"] or {}).get("evidence_ids", []))
                if t_round2["messages"] else False,
                "treatment_observation_refs": t_action["observation_refs"],
                "control_observation_refs": c_action["observation_refs"],
            },
            "isolation": isolation,
            "runs": {
                "treatment": {"run_id": t_run, "status": t_result["status"],
                              "action_counts": t_result["action_counts"],
                              "all_decisions_valid": t_result["all_decisions_valid"]},
                "control": {"run_id": c_run, "status": c_result["status"],
                            "action_counts": c_result["action_counts"],
                            "all_decisions_valid": c_result["all_decisions_valid"]},
            },
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
