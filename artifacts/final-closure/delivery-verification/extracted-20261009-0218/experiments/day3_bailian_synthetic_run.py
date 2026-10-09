"""Explicit, auditable entry point for an authorized synthetic Bailian run.

This module performs no work on import. A live run requires the command-line
``--enable-live`` flag, an exact call ceiling, a task budget, and dedicated
Bailian configuration in the selected dotenv file.
"""

import argparse
import hashlib
import json
import os
import platform
import signal
import subprocess
import tempfile
import threading
import time
from datetime import datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from unittest.mock import patch

from experiments.day3_bailian_synthetic_backend import SYSTEM_PROMPT
from riskshield.day3 import (DEFAULT_TASK_BUDGET_CNY, MAX_TASK_BUDGET_CNY, ROLES,
                             Day3Simulation, SimulationError)
from experiments.day3_bailian_synthetic_backend import (BailianBackend, BailianConfig,
                                     BailianConfigurationError, ROLE_MODELS)
from riskshield.day4_alerts import Day4Alerts
from riskshield.day4_report import Day4Reports
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
    """Price a fixed-input worst-case envelope locally; never send model requests."""
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
    backend = BailianBackend(config, enable_live=False)
    for model, route in config.routes.items():
        rates = (route.input_cny_per_million, route.cached_input_cny_per_million,
                 route.output_cny_per_million)
        expected_output = 160 if model == "qwen-turbo" else 256
        if (any(not rate.is_finite() or rate <= 0 for rate in rates) or
                route.max_input_tokens <= 0 or route.max_output_tokens != expected_output or
                route.cached_input_cny_per_million > route.input_cny_per_million):
            raise BailianConfigurationError(
                f"{model} price or token bounds do not cover the Day 3 reservation")

    # Reuse the actual versioned synthetic fixture and graph in a disposable local
    # database. This makes the future-message/memory envelope specific to Northstar,
    # without reading or changing the user's run database or sending network traffic.
    max_input_bound_by_model = {model: 0 for model in required_models}
    with tempfile.TemporaryDirectory(prefix="day3-bailian-preflight-") as temp_dir:
        store = Store(Path(temp_dir) / "preflight.sqlite3")
        engine = Day3Simulation(store)
        prepare_synthetic_case(store)
        sample_run_id = engine.create_run(
            CASE_ID, GRAPH_ID, agent_count=500, rounds=30, seed=1,
            concurrency=1, budget_cny=Decimal("30"), role_offset=0,
        )
        observations = engine._context(
            engine._run_row(sample_run_id), engine._agents(sample_run_id), 30)
        for observation in observations:
            model = ROLE_MODELS[observation["persona"]["role"]]
            if model in required_models:
                max_input_bound_by_model[model] = max(
                    max_input_bound_by_model[model],
                    backend.max_sanitized_input_upper_bound(observation),
                )
    if any(bound <= 0 for bound in max_input_bound_by_model.values()):
        raise BailianConfigurationError("could not establish a Northstar request bound")
    for model, bound in max_input_bound_by_model.items():
        if bound > config.routes[model].max_input_tokens:
            raise BailianConfigurationError(
                f"{model} configured input limit is below the Northstar upper bound")

    reservations = {
        model: config.routes[model].pricing(max_input_bound_by_model[model]).reservation_cny *
        agents_by_model[model] * rounds
        for model in required_models
    }
    route_max_reservations = {
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
        "maximum_sanitized_input_upper_bound_tokens_by_model": {
            model: max_input_bound_by_model.get(model, 0) for model in agents_by_model
        },
        "route_maximum_reservation_cny_by_model": {
            model: str(route_max_reservations.get(model, Decimal("0")))
            for model in agents_by_model
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
        "reservation_basis": (
            "Fixed Northstar v1 graph evidence; 3 messages x 160 chars, 3 memories x 120 chars; "
            "worst escaped JSON fields plus 128 protocol allowance; output max_tokens unchanged. "
            "A shared per-round budget gate reserves this envelope before dispatch."
        ),
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


def _request_performance_rows(store, run_id):
    with store.connect() as db:
        rows = db.execute(
            "SELECT run_id, round_number, agent_id, requested_model, queued_at, started_at, "
            "ended_at, queue_seconds, backend_seconds, validation_seconds, "
            "request_elapsed_seconds, status, failure_category, dispatch_evidence, "
            "usage_json, cost_cny, usage_unknown FROM simulation_request_metrics "
            "WHERE run_id=? ORDER BY round_number, agent_id", (run_id,)).fetchall()
    return [{
        **{key: row[key] for key in row.keys() if key != "usage_json"},
        "usage": json.loads(row["usage_json"]) if row["usage_json"] else None,
    } for row in rows]


def _source_manifest():
    root = Path(__file__).resolve().parents[1]
    relative_paths = (
        "experiments/day3_bailian_synthetic_run.py",
        "experiments/day3_bailian_synthetic_backend.py",
        "src/riskshield/day3.py",
        "src/riskshield/day4_alerts.py",
        "src/riskshield/day4_report.py",
        "src/riskshield/schemas.py",
        "src/riskshield/store.py",
        "tests/fixtures/day3_synthetic_case.py",
        "pyproject.toml",
        "uv.lock",
    )
    sources = {}
    for relative in relative_paths:
        path = root / relative
        sources[relative] = hashlib.sha256(path.read_bytes()).hexdigest()
    try:
        git_head = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=root, check=True,
            capture_output=True, text=True).stdout.strip()
    except (OSError, subprocess.CalledProcessError):
        git_head = None
    return {
        "git_head": git_head,
        "source_sha256": sources,
        "system_prompt_sha256": hashlib.sha256(SYSTEM_PROMPT.encode("utf-8")).hexdigest(),
        "python_version": platform.python_version(),
    }


def run_synthetic_bailian(*, db_path: str | Path, output_path: str | Path,
                          env_path: str | Path, agent_count: int, rounds: int,
                          concurrency: int, max_calls: int, budget_cny, role_offset: int,
                          purpose: str, authorization_label: str,
                          enable_live: bool = False,
                          collect_performance: bool = False,
                          stop_event: threading.Event | None = None,
                          deadline_monotonic: float | None = None,
                          stop_on_failure: bool = False,
                          generate_report: bool = False,
                          report_output_path: str | Path | None = None):
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
    budget_preflight = preflight_bailian(
        env_path=env_path, agent_count=agent_count, rounds=rounds,
        role_offset=role_offset, budget_cny=budget, max_calls=max_calls,
    )
    if budget_preflight["preflight_status"] != "configuration_passed":
        raise SimulationError("bounded Northstar request reservation exceeds run budget")
    destination = Path(output_path)
    if destination.exists():
        raise FileExistsError(destination)
    report_destination = (Path(report_output_path) if report_output_path is not None else
                          destination.with_name(destination.stem + ".five-module-report.json"))
    if generate_report and report_destination.exists():
        raise FileExistsError(report_destination)
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
    source_manifest = _source_manifest()
    event_input_started_at = datetime.now().astimezone().isoformat()
    event_input_started = time.perf_counter()
    prepare_synthetic_case(store)
    run_id = engine.create_run(
        CASE_ID, GRAPH_ID, agent_count=agent_count, rounds=rounds,
        concurrency=concurrency, budget_cny=budget, role_offset=role_offset,
    )
    with store.connect() as db:
        initial_agents = [dict(row) for row in db.execute(
            "SELECT agent_id, persona, state FROM simulation_agents "
            "WHERE run_id=? ORDER BY agent_id", (run_id,))]
        graph_body = db.execute(
            "SELECT body FROM graphs WHERE graph_id=?", (GRAPH_ID,)).fetchone()["body"]
    initial_state_sha256 = hashlib.sha256(json.dumps(
        initial_agents, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")).hexdigest()
    graph_sha256 = hashlib.sha256(graph_body.encode("utf-8")).hexdigest()
    simulation_started_at = datetime.now().astimezone().isoformat()
    simulation_started = time.perf_counter()
    advance_options = {}
    if collect_performance:
        advance_options["collect_performance"] = True
    if stop_event is not None:
        advance_options["stop_event"] = stop_event
    if deadline_monotonic is not None:
        advance_options["deadline_monotonic"] = deadline_monotonic
    if stop_on_failure:
        advance_options["stop_on_failure"] = True
    try:
        result = engine.advance(run_id, backend, **advance_options)
    finally:
        backend.close()
    simulation_ended = time.perf_counter()
    simulation_ended_at = datetime.now().astimezone().isoformat()

    alert = None
    module_report = None
    report_error = None
    report_started_at = None
    report_ended_at = None
    report_started = None
    if generate_report:
        if result["completed_rounds"] < 1:
            report_error = "NoCompletedRound"
        else:
            report_started_at = datetime.now().astimezone().isoformat()
            report_started = time.perf_counter()
            try:
                alert = Day4Alerts(store).assess(run_id)
                module_report = Day4Reports(store).build(run_id, alert)
                report_destination.parent.mkdir(parents=True, exist_ok=True)
                report_destination.write_text(
                    json.dumps(module_report, ensure_ascii=False, indent=2), encoding="utf-8")
            except Exception as exc:
                report_error = type(exc).__name__
            report_ended = time.perf_counter()
            report_ended_at = datetime.now().astimezone().isoformat()

    actions = _sanitized_actions(engine, run_id)
    valid = sum(row["status"] == "valid" for row in actions)
    failed = sum(row["status"] == "failed" for row in actions)
    not_dispatched = sum(row["status"] == "not_dispatched" for row in actions)
    completed_without_failures = (
        result["status"] == "complete" and
        result["completed_rounds"] == rounds and
        len(actions) == expected_decisions and failed == 0 and not_dispatched == 0
    )
    report_written_at = time.perf_counter()
    timing = {
        "event_input_started_at": event_input_started_at,
        "simulation_started_at": simulation_started_at,
        "simulation_ended_at": simulation_ended_at,
        "simulation_elapsed_seconds": max(0.0, simulation_ended - simulation_started),
        "report_generation_started_at": report_started_at,
        "report_generation_ended_at": report_ended_at,
        "report_generation_elapsed_seconds": (
            max(0.0, report_ended - report_started) if report_started is not None else None),
        "event_input_to_report_artifact_elapsed_seconds": (
            max(0.0, report_written_at - event_input_started)
            if module_report is not None else None),
        "event_input_to_report_artifact_ended_at": (
            datetime.now().astimezone().isoformat() if module_report is not None else None),
        "clock": "time.perf_counter monotonic durations; local ISO timestamps for correlation",
        "event_input_boundary": "immediately before loading the fixed synthetic case and graph",
    }
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
        "budget_preflight": budget_preflight,
        "run_id": run_id,
        "run_status": result["status"],
        "completed_rounds": result["completed_rounds"],
        "actual_participants": result["actual_participants"],
        "valid_decisions": valid,
        "failed_decisions": failed,
        "not_dispatched_decisions": not_dispatched,
        "completed_without_failures": completed_without_failures,
        "all_decisions_valid": result["all_decisions_valid"],
        "task_status": ("failed_report" if generate_report and module_report is None else
                        "complete" if completed_without_failures else
                        "partial" if result["status"] == "partial" or
                        result["completed_rounds"] < rounds else "complete_with_failures"),
        "stop_reason": (result.get("performance", {}).get("invocations", [{}])[-1].get(
            "stop_reason") if result.get("performance", {}).get("invocations") else None),
        "estimated_cost_cny": result["cost_cny"],
        "usage": result["usage"],
        "rounds": result["rounds"],
        "performance": result.get("performance"),
        "request_performance": (_request_performance_rows(store, run_id)
                                 if collect_performance else None),
        "timing": timing,
        "source_manifest": source_manifest,
        "input_hashes": {
            "synthetic_case_version": "day3-synthetic-v1",
            "graph_sha256": graph_sha256,
            "initial_agents_state_sha256": initial_state_sha256,
            "backend_configuration_sha256": hashlib.sha256(json.dumps(
                backend.describe(), ensure_ascii=False, sort_keys=True,
                separators=(",", ":")).encode("utf-8")).hexdigest(),
        },
        "five_module_report": ({
            "status": "generated", "report_id": module_report["report_id"],
            "module_names": sorted(module_report["modules"]),
            "data_mode": module_report["data_mode"],
            "execution_mode": module_report["execution_mode"],
            "report_sha256": hashlib.sha256(json.dumps(
                module_report, ensure_ascii=False, sort_keys=True,
                separators=(",", ":")).encode("utf-8")).hexdigest(),
            "path": str(report_destination.resolve()),
            "alert_id": alert["alert_id"] if alert else None,
            "alert_status": alert["status"] if alert else None,
        } if module_report is not None else {
            "status": "not_generated", "error_category": report_error,
            "path": str(report_destination.resolve()) if generate_report else None,
        }),
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
    parser.add_argument("--time-limit-seconds", type=float,
                        help="stop dispatching after this run-local monotonic duration")
    parser.add_argument("--collect-performance", action="store_true")
    parser.add_argument("--stop-on-failure", action="store_true")
    parser.add_argument("--generate-five-module-report", action="store_true")
    parser.add_argument("--report-output")
    parser.add_argument("--enable-live", action="store_true")
    args = parser.parse_args(argv)
    if args.time_limit_seconds is not None and args.time_limit_seconds <= 0:
        parser.error("--time-limit-seconds must be positive")
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
    stop_event = threading.Event()
    previous_sigint = signal.getsignal(signal.SIGINT)
    signal.signal(signal.SIGINT, lambda signum, frame: stop_event.set())
    try:
        report = run_synthetic_bailian(
            db_path=args.db, output_path=args.output, env_path=args.env_file,
            agent_count=args.agent_count, rounds=args.rounds,
            concurrency=args.concurrency, max_calls=args.max_calls,
            role_offset=args.role_offset,
            budget_cny=args.budget_cny, purpose=args.purpose,
            authorization_label=args.authorization_label, enable_live=args.enable_live,
            collect_performance=args.collect_performance,
            stop_event=stop_event,
            deadline_monotonic=(time.monotonic() + args.time_limit_seconds
                                if args.time_limit_seconds is not None else None),
            stop_on_failure=args.stop_on_failure,
            generate_report=args.generate_five_module_report,
            report_output_path=args.report_output,
        )
    finally:
        signal.signal(signal.SIGINT, previous_sigint)
    print(json.dumps({
        "output": str(Path(args.output).resolve()),
        "run_status": report["run_status"],
        "completed_rounds": report["completed_rounds"],
        "valid_decisions": report["valid_decisions"],
        "failed_decisions": report["failed_decisions"],
        "not_dispatched_decisions": report["not_dispatched_decisions"],
        "completed_without_failures": report["completed_without_failures"],
        "all_decisions_valid": report["all_decisions_valid"],
        "task_status": report["task_status"],
        "estimated_cost_cny": report["estimated_cost_cny"],
        "timing": report["timing"],
        "five_module_report": report["five_module_report"],
    }, ensure_ascii=False))
    return 0 if report["task_status"] == "complete" else 2


if __name__ == "__main__":
    raise SystemExit(main())
