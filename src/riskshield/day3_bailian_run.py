"""Explicit, auditable entry point for an authorized synthetic Bailian run.

This module performs no work on import. A live run requires the command-line
``--enable-live`` flag, an exact call ceiling, a task budget, and dedicated
Bailian configuration in the selected dotenv file.
"""

import argparse
import hashlib
import json
from datetime import datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path

from riskshield.day3 import MAX_TASK_BUDGET_CNY, ROLES, Day3Simulation, SimulationError
from riskshield.day3_bailian import BailianBackend, BailianConfig, ROLE_MODELS
from riskshield.day3_synthetic_case import CASE_ID, GRAPH_ID, prepare_synthetic_case
from riskshield.store import Store


OUTBOUND_FIELDS = [
    "agent", "round", "local_state", "fictional_evidence",
    "received_simulated_messages", "own_simulated_memory",
    "allowed_citation_ids",
]


def _budget(value) -> Decimal:
    try:
        result = Decimal(str(value))
    except InvalidOperation:
        raise SimulationError("invalid run budget") from None
    if not Decimal("0") < result <= MAX_TASK_BUDGET_CNY:
        raise SimulationError("run budget must be within ¥30.00")
    return result


def _sanitized_messages(store, run_id):
    with store.connect() as db:
        rows = db.execute(
            "SELECT message_id, round_number, sender_id, recipient_id, action, "
            "evidence_ids, content FROM simulation_messages WHERE run_id=? "
            "ORDER BY round_number, message_id", (run_id,)).fetchall()
    return [{
        "message_id": row["message_id"],
        "round": row["round_number"],
        "sender_id": row["sender_id"],
        "recipient_id": row["recipient_id"],
        "action": row["action"],
        "evidence_ids": json.loads(row["evidence_ids"]),
        "content_sha256": hashlib.sha256(row["content"].encode("utf-8")).hexdigest(),
    } for row in rows]


def _sanitized_actions(engine, run_id):
    return [{
        "round": row["round_number"],
        "agent_id": row["agent_id"],
        "status": row["status"],
        "observation_refs": row["observation_refs"],
        "decision": row["decision"],
        "usage": row["usage"],
        "requested_model": row["requested_model"],
        "actual_model": row["model"],
        "price_version": row["price_version"],
        "reserved_cny": row["reserved_cny"],
        "cost_cny": row["cost_cny"],
        "error": row["error"],
    } for row in engine.trajectory(run_id)]


def run_synthetic_bailian(*, db_path: str | Path, output_path: str | Path,
                          env_path: str | Path, agent_count: int, rounds: int,
                          concurrency: int, max_calls: int, budget_cny, role_offset: int,
                          purpose: str, authorization_label: str,
                          enable_live: bool = False):
    """Run only after a separate authorization and write a sanitized audit record."""
    if not enable_live:
        raise SimulationError("live execution requires explicit enable_live=True")
    expected_decisions = agent_count * rounds
    if max_calls != expected_decisions:
        raise SimulationError(
            "max_calls must equal agent_count * rounds for this all-agent runner")
    if not purpose.strip() or not authorization_label.strip():
        raise SimulationError("purpose and authorization_label are required")
    budget = _budget(budget_cny)
    destination = Path(output_path)
    if destination.exists():
        raise FileExistsError(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)

    required_models = {
        ROLE_MODELS[ROLES[(index + role_offset) % len(ROLES)]]
        for index in range(agent_count)
    }
    config = BailianConfig.from_env(
        env_path, require_api_key=True, required_models=required_models)
    backend = BailianBackend(config, enable_live=True, max_calls=max_calls)
    store = Store(db_path)
    engine = Day3Simulation(store)
    prepare_synthetic_case(store)
    run_id = engine.create_run(
        CASE_ID, GRAPH_ID, agent_count=agent_count, rounds=rounds,
        concurrency=concurrency, budget_cny=budget, role_offset=role_offset,
    )
    result = engine.advance(run_id, backend)
    actions = _sanitized_actions(engine, run_id)
    valid = sum(row["status"] == "valid" for row in actions)
    failed = len(actions) - valid
    completed_without_failures = (
        result["status"] == "complete" and
        result["completed_rounds"] == rounds and
        len(actions) == expected_decisions and failed == 0
    )
    report = {
        "created_at": datetime.now().astimezone().isoformat(),
        "purpose": purpose.strip(),
        "authorization_label": authorization_label.strip(),
        "provider": "alibaba-cloud-bailian",
        "region": "cn-beijing",
        "data_mode": "synthetic",
        "outbound_field_names": OUTBOUND_FIELDS,
        "configured_agents": agent_count,
        "role_offset": role_offset,
        "target_rounds": rounds,
        "expected_decisions": expected_decisions,
        "max_calls": max_calls,
        "calls_attempted": backend.calls,
        "run_budget_cny": str(budget),
        "complete_task_cap_cny": str(MAX_TASK_BUDGET_CNY),
        "backend": backend.describe(),
        "run_id": run_id,
        "run_status": result["status"],
        "completed_rounds": result["completed_rounds"],
        "actual_participants": result["actual_participants"],
        "valid_decisions": valid,
        "failed_decisions": failed,
        "completed_without_failures": completed_without_failures,
        "estimated_cost_cny": result["cost_cny"],
        "usage": result["usage"],
        "rounds": result["rounds"],
        "trajectory": actions,
        "messages": _sanitized_messages(store, run_id),
        "limitations": [
            "Estimated task cost is not the provider's final bill.",
            "A complete run status alone does not prove all decisions were valid.",
            "This synthetic run is not G3 acceptance evidence unless its separately approved "
            "scope satisfies every G3 criterion.",
        ],
    }
    destination.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    return report


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--env-file", default=".env")
    parser.add_argument("--agent-count", required=True, type=int)
    parser.add_argument("--rounds", required=True, type=int)
    parser.add_argument("--concurrency", required=True, type=int)
    parser.add_argument("--role-offset", default=0, type=int,
                        help="0=netizen first; 2=media first for a qwen-flash probe")
    parser.add_argument("--max-calls", required=True, type=int)
    parser.add_argument("--budget-cny", required=True)
    parser.add_argument("--purpose", required=True)
    parser.add_argument("--authorization-label", required=True)
    parser.add_argument("--enable-live", action="store_true")
    args = parser.parse_args(argv)
    report = run_synthetic_bailian(
        db_path=args.db, output_path=args.output, env_path=args.env_file,
        agent_count=args.agent_count, rounds=args.rounds,
        concurrency=args.concurrency, max_calls=args.max_calls,
        role_offset=args.role_offset,
        budget_cny=args.budget_cny, purpose=args.purpose,
        authorization_label=args.authorization_label, enable_live=args.enable_live,
    )
    print(json.dumps({
        "output": str(Path(args.output).resolve()),
        "run_status": report["run_status"],
        "completed_rounds": report["completed_rounds"],
        "valid_decisions": report["valid_decisions"],
        "failed_decisions": report["failed_decisions"],
        "completed_without_failures": report["completed_without_failures"],
        "estimated_cost_cny": report["estimated_cost_cny"],
    }, ensure_ascii=False))


if __name__ == "__main__":
    main()
