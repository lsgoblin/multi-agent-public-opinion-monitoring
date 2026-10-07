"""Explicit, auditable entry point for an authorized synthetic Bailian run.

This module performs no work on import. A live run requires the command-line
``--enable-live`` flag, an exact call ceiling, a task budget, and dedicated
Bailian configuration in the selected dotenv file.
"""

import argparse
import hashlib
import json
import os
from datetime import datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from unittest.mock import patch

from riskshield.day3 import (DEFAULT_TASK_BUDGET_CNY, MAX_TASK_BUDGET_CNY, ROLES,
                             Day3Simulation, SimulationError)
from experiments.day3_bailian_synthetic_backend import (BailianBackend, BailianConfig,
                                     BailianConfigurationError, ROLE_MODELS)
from tests.fixtures.day3_synthetic_case import CASE_ID, GRAPH_ID, prepare_synthetic_case
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
    if not result.is_finite() or not Decimal("0") < result <= MAX_TASK_BUDGET_CNY:
        raise SimulationError("run budget must be within ¥30.00")
    return result


def preflight_bailian(*, env_path: str | Path, agent_count: int, rounds: int,
                      role_offset: int = 0, budget_cny=None, max_calls: int | None = None):
    """Read local configuration and price all decision opportunities; never run agents."""
    if not (1 <= agent_count <= 500 and 1 <= rounds <= 30 and
            0 <= role_offset < len(ROLES)):
        raise SimulationError("agent_count, rounds or role_offset outside Day 3 limits")
    opportunities = agent_count * rounds
    if max_calls is not None and max_calls != opportunities:
        raise SimulationError("max_calls must equal agent_count * rounds")
    budget = _budget(DEFAULT_TASK_BUDGET_CNY if budget_cny is None else budget_cny)
    agents_by_model = {"qwen-turbo": 0, "qwen-flash": 0}
    for index in range(agent_count):
        model = ROLE_MODELS[ROLES[(index + role_offset) % len(ROLES)]]
        agents_by_model[model] += 1
    required_models = {model for model, count in agents_by_model.items() if count}
    # from_env loads dotenv entries into os.environ; restore the process environment
    # immediately so this read-only check cannot alter later run configuration.
    with patch.dict(os.environ):
        config = BailianConfig.from_env(
            env_path, require_api_key=True, required_models=required_models)
    # Construction validates the exact Beijing HTTPS endpoint and the fixed routes.
    # enable_live=False makes this object incapable of sending a request.
    BailianBackend(config, enable_live=False)
    for model, route in config.routes.items():
        rates = (route.input_cny_per_million, route.cached_input_cny_per_million,
                 route.output_cny_per_million)
        if (any(not rate.is_finite() for rate in rates) or
                route.max_input_tokens < 8192 or route.max_output_tokens < 160 or
                route.cached_input_cny_per_million > route.input_cny_per_million):
            raise BailianConfigurationError(
                f"{model} price or token bounds do not cover the Day 3 reservation")
    reservations = {
        model: config.routes[model].reservation_cny * agents_by_model[model] * rounds
        for model in required_models
    }
    total = sum(reservations.values(), Decimal("0"))
    within_budget = total <= budget
    return {
        "preflight_status": "configuration_passed" if within_budget else "failed",
        "budget_status": "within_limit" if within_budget else "reservation_exceeds_budget",
        "network_requests": 0,
        "model_calls": 0,
        "region": "cn-beijing",
        "dedicated_key_configured": True,
        "agent_count": agent_count,
        "rounds": rounds,
        "role_offset": role_offset,
        "decision_opportunities": opportunities,
        "agents_by_model": agents_by_model,
        "opportunities_by_model": {
            model: count * rounds for model, count in agents_by_model.items()
        },
        "reservation_cny_by_model": {
            model: str(reservations.get(model, Decimal("0"))) for model in agents_by_model
        },
        "configured_prices_cny_per_million": {
            model: {
                "input": str(route.input_cny_per_million),
                "cached_input": str(route.cached_input_cny_per_million),
                "output": str(route.output_cny_per_million),
                "max_input_tokens": route.max_input_tokens,
                "max_output_tokens": route.max_output_tokens,
                "local_price_version_label_configured": True,
            } for model, route in config.routes.items()
        },
        "total_conservative_reservation_cny": str(total),
        "task_budget_cny": str(budget),
        "remaining_budget_cny": str(budget - total),
        "price_labels_present": sorted(required_models),
        "price_verification": (
            "pending_manual_comparison_with_current_official_Beijing_prices; "
            "local price version labels are not proof of official verification"
        ),
        "live_authorized": False,
        "g3_passed": False,
    }


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
        "request_audit": row["request_audit"],
        "schema_diagnostics": row["schema_diagnostics"],
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
    try:
        result = engine.advance(run_id, backend)
    finally:
        backend.close()
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
    parser.add_argument("--preflight", action="store_true",
                        help="read-only local configuration and budget check; no model calls")
    parser.add_argument("--db")
    parser.add_argument("--output")
    parser.add_argument("--env-file", default=".env")
    parser.add_argument("--agent-count", required=True, type=int)
    parser.add_argument("--rounds", required=True, type=int)
    parser.add_argument("--concurrency", type=int)
    parser.add_argument("--role-offset", default=0, type=int,
                        help="0=netizen first; 2=media first for a qwen-flash probe")
    parser.add_argument("--max-calls", type=int)
    parser.add_argument("--budget-cny")
    parser.add_argument("--purpose")
    parser.add_argument("--authorization-label")
    parser.add_argument("--enable-live", action="store_true")
    args = parser.parse_args(argv)
    if args.preflight:
        if args.enable_live:
            parser.error("--preflight cannot be combined with --enable-live")
        if args.concurrency is not None and not 1 <= args.concurrency <= 128:
            parser.error("concurrency outside Day 3 limits")
        try:
            report = preflight_bailian(
                env_path=args.env_file, agent_count=args.agent_count,
                rounds=args.rounds, role_offset=args.role_offset,
                max_calls=args.max_calls, budget_cny=args.budget_cny)
        except (SimulationError, BailianConfigurationError, InvalidOperation) as exc:
            reason = ("invalid Bailian numeric price configuration"
                      if isinstance(exc, InvalidOperation) else str(exc))
            print(json.dumps({"preflight_status": "failed", "reason": reason,
                              "network_requests": 0, "model_calls": 0,
                              "g3_passed": False}, ensure_ascii=False))
            return 2
        print(json.dumps(report, ensure_ascii=False))
        return 0 if report["preflight_status"] == "configuration_passed" else 2
    missing = [flag for flag, value in (("--db", args.db), ("--output", args.output),
               ("--concurrency", args.concurrency), ("--max-calls", args.max_calls),
               ("--budget-cny", args.budget_cny), ("--purpose", args.purpose),
               ("--authorization-label", args.authorization_label)) if value is None]
    if missing:
        parser.error("live run requires " + ", ".join(missing))
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
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
