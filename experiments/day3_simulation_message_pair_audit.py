"""Read-only audit of a real simulated message and its paired control request.

This module reconstructs a frozen observation from persisted synthetic-run
evidence. It never sends a request and never prints message or prompt text.
"""

import argparse
import copy
import hashlib
import json
import random
import sqlite3
from contextlib import contextmanager
from decimal import Decimal, InvalidOperation
from pathlib import Path

from riskshield.day2 import Day2Pipeline
from riskshield.day3 import Day3Simulation, ROLES, ROLE_QUERIES, SIGNALS, _clip
from experiments.day3_bailian_synthetic_backend import (BEIJING_BASE_URL, BailianBackend,
                                     BailianConfig, BailianConfigurationError,
                                     BailianRequestError, BailianRoute)


class MessagePairAuditError(ValueError):
    pass


class _ReadOnlyStore:
    def __init__(self, path):
        self.path = Path(path).resolve()
        if not self.path.is_file():
            raise MessagePairAuditError("run database does not exist")

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.path.as_uri() + "?mode=ro", uri=True)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA query_only=ON")
        try:
            yield db
        finally:
            db.close()


def _engine(store):
    # Bypass constructors because they create or migrate tables.
    engine = object.__new__(Day3Simulation)
    engine.store = store
    engine.day2 = object.__new__(Day2Pipeline)
    engine.day2.store = store
    return engine


def _run(store, run_id):
    with store.connect() as db:
        if run_id is None:
            rows = db.execute("SELECT * FROM simulation_runs").fetchall()
            if len(rows) != 1:
                raise MessagePairAuditError("specify a run_id for a database with multiple runs")
            row = rows[0]
        else:
            row = db.execute("SELECT * FROM simulation_runs WHERE run_id=?",
                             (run_id,)).fetchone()
            if row is None:
                raise MessagePairAuditError("run_id is absent")
    run = dict(row)
    config = json.loads(run["config"])
    if (config.get("data_mode") != "synthetic" or
            config.get("backend_mode") != "real_model" or
            run["status"] != "complete" or
            run["completed_rounds"] != config.get("rounds") or
            run["completed_rounds"] < 2):
        raise MessagePairAuditError("a completed synthetic real-model run is required")
    return run, config


def _backend(config):
    saved = config.get("backend", {})
    if (saved.get("provider") != "alibaba-cloud-bailian" or
            saved.get("region") != "cn-beijing" or
            saved.get("base_url") != BEIJING_BASE_URL):
        raise MessagePairAuditError("recorded backend is not the Beijing Bailian adapter")
    try:
        routes = {
            model: BailianRoute(
                model=model, price_version=item["price_version"],
                input_cny_per_million=Decimal(item["input_cny_per_million"]),
                cached_input_cny_per_million=Decimal(item["cached_input_cny_per_million"]),
                output_cny_per_million=Decimal(item["output_cny_per_million"]),
                max_input_tokens=int(item["max_input_tokens"]),
                max_output_tokens=int(item["max_output_tokens"]),
            ) for model, item in saved["routes"].items()
        }
        return BailianBackend(BailianConfig("", BEIJING_BASE_URL, routes))
    except (KeyError, TypeError, ValueError, InvalidOperation,
            BailianConfigurationError):
        raise MessagePairAuditError("recorded route configuration is incomplete") from None


def _initial_agents(config, stored):
    count = config["agent_count"]
    if len(stored) != count:
        raise MessagePairAuditError("agent count does not match run configuration")
    rng = random.Random(config["seed"])
    agents = []
    for index in range(count):
        agent_id = f"agent_{index:04d}"
        row = stored.get(agent_id)
        if row is None:
            raise MessagePairAuditError("agent identity is incomplete")
        role = ROLES[(index + config["role_offset"]) % len(ROLES)]
        persona = {
            "role": role, "focus": ROLE_QUERIES[role],
            "susceptibility": round(rng.uniform(0.25, 0.9), 3),
            "influence": round(rng.uniform(0.2, 0.9), 3),
        }
        neighbors = sorted({f"agent_{(index + step) % count:04d}"
                            for step in (1, 2, 5) if (index + step) % count != index})
        state = {"emotion": round(rng.uniform(-0.2, 0.2), 3),
                 "trust": round(rng.uniform(0.35, 0.8), 3)}
        if (json.loads(row["persona"]) != persona or
                json.loads(row["neighbors"]) != neighbors):
            raise MessagePairAuditError("agent persona or neighbors differ from seeded run")
        agents.append({"agent_id": agent_id, "persona": persona,
                       "neighbors": neighbors, "state": state})
    return agents


def _hash(payload):
    canonical = json.dumps(payload, ensure_ascii=False, sort_keys=True,
                           separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _verified_candidate(db_path, *, run_id=None, message_id=None,
                        return_frozen_pair=False):
    """Verify the complete ledger before exposing an in-memory synthetic pair."""
    store = _ReadOnlyStore(db_path)
    run, config = _run(store, run_id)
    backend = _backend(config)
    engine = _engine(store)
    graph = engine.day2.graph(run["graph_id"])
    if (graph["cutoff"] != config["cutoff"] or
            graph["source_version"] != config["source_version"]):
        raise MessagePairAuditError("graph cutoff or source version differs from run")
    with store.connect() as db:
        stored = {row["agent_id"]: row for row in db.execute(
            "SELECT * FROM simulation_agents WHERE run_id=?", (run["run_id"],))}
        actions = {(row["round_number"], row["agent_id"]): row for row in db.execute(
            "SELECT * FROM simulation_actions WHERE run_id=?", (run["run_id"],))}
        messages = {row["message_id"]: row for row in db.execute(
            "SELECT * FROM simulation_messages WHERE run_id=?", (run["run_id"],))}
        memories = {row["memory_id"]: row for row in db.execute(
            "SELECT * FROM simulation_memories WHERE run_id=?", (run["run_id"],))}
    agents = _initial_agents(config, stored)
    expected_memories = {}
    candidates = []
    checked_refs = 0
    checked_messages = 0
    for round_number in range(1, run["completed_rounds"] + 1):
        observations = engine._context(run, agents, round_number)
        expected_messages = {}
        for obs in observations:
            aid = obs["agent_id"]
            row = actions.get((round_number, aid))
            if row is None:
                raise MessagePairAuditError("an action is missing from a completed round")
            refs = {"evidence": [e["evidence_id"] for e in obs["evidence"]],
                    "messages": [m["message_id"] for m in obs["messages"]],
                    "memories": [m["memory_id"] for m in obs["memories"]]}
            if refs != json.loads(row["observation_refs"]):
                raise MessagePairAuditError("replayed observation references differ from ledger")
            checked_refs += 1
            for msg in obs["messages"]:
                mid = f"memmsg_{round_number}_{aid}_{msg['message_id']}"
                expected_memories[mid] = (
                    aid, round_number,
                    f"heard {msg['sender_id']} {msg['action']}: {msg['content'][:200]}",
                    [msg["message_id"], *msg["evidence_ids"]],
                )
            decision = json.loads(row["decision"]) if row["decision"] else None
            if (decision is None) != (row["status"] == "failed"):
                raise MessagePairAuditError("action status and decision disagree")
            if decision is None:
                continue
            cited = set(refs["messages"]) & set(decision["evidence_ids"])
            for mid in cited:
                msg = messages.get(mid)
                if (msg is not None and msg["recipient_id"] == aid and
                        msg["round_number"] == round_number - 1):
                    candidates.append((len(obs["messages"]), round_number, aid,
                                       mid, obs, row, msg))
            state = next(agent["state"] for agent in agents if agent["agent_id"] == aid)
            state["emotion"] = _clip(
                state["emotion"] + SIGNALS[decision["action"]] *
                obs["persona"]["susceptibility"], -1, 1)
            state["trust"] = _clip(
                state["trust"] + (0.015 if decision["action"] ==
                                  "seek_clarification" else -0.01), 0, 1)
            expected_memories[f"mem_{round_number}_{aid}"] = (
                aid, round_number, decision["action"] + " " + decision["reason"],
                decision["evidence_ids"],
            )
            if decision["action"] in {"share", "comment", "express_complaint_intent"}:
                eligible = {item["evidence_id"] for item in obs["evidence"]}
                for recipient in obs["neighbors"]:
                    mid = f"msg_{round_number}_{aid}_{recipient}"
                    expected_messages[mid] = (
                        aid, recipient, decision["action"],
                        [ref for ref in decision["evidence_ids"] if ref in eligible],
                        decision["reason"],
                    )
        actual_messages = {mid: row for mid, row in messages.items()
                           if row["round_number"] == round_number}
        if set(actual_messages) != set(expected_messages):
            raise MessagePairAuditError("message edges differ from logged runtime actions")
        checked_messages += len(actual_messages)
        for mid, (sender, recipient, action, evidence_ids, content) in expected_messages.items():
            row = actual_messages[mid]
            if (row["sender_id"], row["recipient_id"], row["action"],
                    json.loads(row["evidence_ids"]), row["content"]) != (
                    sender, recipient, action, evidence_ids, content):
                raise MessagePairAuditError("message content or edge differs from runtime action")
        by_id = {agent["agent_id"]: agent for agent in agents}
        for row in sorted(actual_messages.values(),
                          key=lambda item: (item["sender_id"], item["recipient_id"])):
            state = by_id[row["recipient_id"]]["state"]
            state["emotion"] = _clip(state["emotion"] + SIGNALS[row["action"]] * 0.25,
                                     -1, 1)
    if len(actions) != checked_refs:
        raise MessagePairAuditError("action count differs from completed rounds")
    if len(messages) != checked_messages:
        raise MessagePairAuditError("message count differs from completed rounds")
    if set(memories) != set(expected_memories):
        raise MessagePairAuditError("persisted memory set differs from replay")
    for mid, expected in expected_memories.items():
        row = memories[mid]
        if (row["agent_id"], row["round_number"], row["content"],
                json.loads(row["refs"])) != expected:
            raise MessagePairAuditError("persisted memory content differs from replay")
    if any(agent["state"] != json.loads(stored[agent["agent_id"]]["state"])
           for agent in agents):
        raise MessagePairAuditError("replayed final state differs from persisted state")

    for _, round_number, recipient, mid, obs, action_row, msg in sorted(candidates):
        if message_id is not None and mid != message_id:
            continue
        try:
            payload, _ = backend.prepare_request(obs)
        except (BailianConfigurationError, BailianRequestError, KeyError, TypeError,
                ValueError):
            raise MessagePairAuditError(
                "reconstructed request is incompatible with current adapter") from None
        try:
            saved_audit = (json.loads(action_row["request_audit"])
                           if "request_audit" in action_row.keys() and
                           action_row["request_audit"] else None)
        except (json.JSONDecodeError, TypeError):
            raise MessagePairAuditError("recorded request audit is malformed") from None
        recorded_hash_match = None
        if saved_audit is not None:
            if saved_audit != backend._request_audit(payload):
                raise MessagePairAuditError(
                    "recorded prepared-payload audit differs from reconstruction")
            recorded_hash_match = True
        fields = json.loads(payload["messages"][1]["content"])
        outgoing = fields["received_simulated_messages"]
        allowed = fields["allowed_citation_ids"]
        if (sum(item["message_id"] == mid for item in outgoing) != 1 or
                allowed.count(mid) != 1):
            continue
        control_fields = copy.deepcopy(fields)
        control_fields["received_simulated_messages"] = [
            item for item in outgoing if item["message_id"] != mid]
        control_fields["allowed_citation_ids"] = [
            item for item in allowed if item != mid]
        differences = {key for key in fields if fields[key] != control_fields[key]}
        if (differences != {"received_simulated_messages", "allowed_citation_ids"} or
                len(outgoing) - len(control_fields["received_simulated_messages"]) != 1 or
                len(allowed) - len(control_fields["allowed_citation_ids"]) != 1):
            raise MessagePairAuditError("control differs outside the selected message")
        control_payload = copy.deepcopy(payload)
        control_payload["messages"][1]["content"] = json.dumps(
            control_fields, ensure_ascii=False, separators=(",", ":"))
        if (payload["messages"][0] != control_payload["messages"][0] or
                any(payload[key] != control_payload[key] for key in payload
                    if key != "messages")):
            raise MessagePairAuditError("generation settings differ between paired requests")
        control_obs = copy.deepcopy(obs)
        control_obs["messages"] = [item for item in obs["messages"]
                                   if item["message_id"] != mid]
        if (len(control_obs["messages"]) != len(obs["messages"]) - 1 or
                backend.prepare_request(control_obs)[0] != control_payload):
            raise MessagePairAuditError("control observation does not reproduce paired payload")
        decision = json.loads(action_row["decision"])
        report = {
            "audit_status": "paired_candidate_reconstructed",
            "run_id": run["run_id"], "recipient_id": recipient,
            "sender_id": msg["sender_id"], "message_id": mid,
            "recipient_round": round_number, "sender_round": msg["round_number"],
            "sender_action": msg["action"], "recipient_action": decision["action"],
            "recipient_cited_message": True,
            "model": payload["model"],
            "reconstructed_treatment_payload_canonical_json_sha256": _hash(payload),
            "reconstructed_control_payload_canonical_json_sha256": _hash(control_payload),
            "hash_basis": "canonical JSON of payloads reconstructed under current code",
            "recorded_prepared_payload_hash_match": recorded_hash_match,
            "field_differences": ["received_simulated_messages",
                                  "allowed_citation_ids"],
            "removed_message_count": 1, "removed_citation_count": 1,
            "other_fields_identical": True,
            "generation_settings_identical": True,
            "replay_checks": {
                "agent_personas_and_neighbors": len(agents),
                "observation_reference_rows": checked_refs,
                "message_edges": len(messages), "memory_rows": len(memories),
                "final_agent_states": len(agents),
            },
            "network_requests": 0, "model_calls": 0,
            "g3_causality_proven": False,
            "limitations": [
                "The pair is reconstructed under the current engine and adapter code; "
                "the historical run did not persist a full original prompt hash.",
                "Constructing a paired control does not show that model behavior changes.",
            ],
        }
        if return_frozen_pair:
            return report, copy.deepcopy(obs), control_obs
        return report
    raise MessagePairAuditError("no cited runtime message is present in a bounded request")


def audit_message_pair_candidate(db_path, *, run_id=None, message_id=None):
    """Return only identifiers, hashes, checks and limitations; never request a model."""
    return _verified_candidate(db_path, run_id=run_id, message_id=message_id)


def load_verified_frozen_pair(db_path, *, run_id=None, message_id=None):
    """Internal runner interface; returned observations may contain synthetic text."""
    return _verified_candidate(db_path, run_id=run_id, message_id=message_id,
                               return_frozen_pair=True)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", required=True)
    parser.add_argument("--run-id")
    parser.add_argument("--message-id")
    args = parser.parse_args(argv)
    try:
        report = audit_message_pair_candidate(args.db, run_id=args.run_id,
                                        message_id=args.message_id)
    except MessagePairAuditError as exc:
        print(json.dumps({"audit_status": "failed", "reason": str(exc),
                          "network_requests": 0, "model_calls": 0}, ensure_ascii=False))
        return 2
    except Exception:
        print(json.dumps({"audit_status": "failed",
                          "reason": "audit could not verify the persisted run",
                          "network_requests": 0, "model_calls": 0}, ensure_ascii=False))
        return 2
    print(json.dumps(report, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
