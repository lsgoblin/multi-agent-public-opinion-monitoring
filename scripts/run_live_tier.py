"""Graded real-model run driver: engine + recovered Bailian live backend.

Tier 1 is the cheapest: 10 agents x 3 rounds. Nothing larger runs unless this passes.

Safety properties:
  * live calls require an explicit --enable-live flag AND a positive --max-calls;
  * the engine's own budget gate stops a round before dispatch if the reservation for
    the whole round would exceed the remaining budget;
  * no retry: the engine never re-sends a failed request;
  * credentials are read from a path given by --env (or RISKSHIELD_ENV_FILE); only the
    number of keys loaded is printed, never a value or a key;
  * the ledger path is mandatory (--db) and must be outside every temp root, so the
    per-request audit trail cannot be left behind in a scratch directory again.

Nothing here runs on its own: a real-model run additionally needs a separately authorized
call budget.
"""
import argparse
import hashlib
import json
import os
import sys
import tempfile
import time
from decimal import Decimal
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
# The live adapter is a one-off tool that is not part of the committed source tree, so its
# location is a parameter rather than a fixed path.
DEFAULT_ADAPTER = PROJECT_ROOT / "runtime" / "bailian_backend_ref.py"
sys.path.insert(0, str(PROJECT_ROOT))
sys.path.insert(0, str(PROJECT_ROOT / "src"))
sys.path.insert(0, str(PROJECT_ROOT / "runtime"))

import importlib.util  # noqa: E402

from riskshield.simulation import Day3Simulation  # noqa: E402
from riskshield.store import Store  # noqa: E402
from tests.fixtures.day3_synthetic_case import (  # noqa: E402
    CASE_ID, GRAPH_ID, prepare_synthetic_case,
)


def resolve_db_path(raw: str) -> Path:
    """Resolve --db and refuse a path that would put the ledger back in a temp area.

    The 500x30 run wrote its 15,000-row ledger into a ``tempfile.mkdtemp`` directory,
    and the archiving step afterwards searched the wrong temp root and recorded that
    ledger as permanently lost. It survived only by luck. The ledger is the only
    artifact carrying the per-request audit trail, so the runner now refuses to start
    unless the database lands in a persistent directory outside every temp root.
    """
    target = Path(raw).expanduser()
    if not target.is_absolute():
        raise SystemExit(f"--db must be an absolute path, got {raw!r}")
    target = target.resolve()
    temp_roots = [Path(tempfile.gettempdir()).resolve()]
    for name in ("TEMP", "TMP"):
        value = os.environ.get(name)
        if value:
            temp_roots.append(Path(value).resolve())
    local = os.environ.get("LOCALAPPDATA")
    if local:
        temp_roots.append((Path(local) / "Temp").resolve())
    for root in temp_roots:
        if target == root or root in target.parents:
            raise SystemExit(
                f"--db {target} is inside the temp root {root}; the ledger would be "
                "cleared with the temp area. Put it under the evidence tree, e.g. "
                ".../evidence/<date>/live/db/.")
    if target.exists() and not target.name.endswith(".db"):
        raise SystemExit(f"--db {target} exists and is not a .db file")
    if not target.parent.is_dir():
        raise SystemExit(f"--db parent directory {target.parent} does not exist; "
                         "create it first so a typo cannot silently start a paid run")
    return target


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--agents", type=int, required=True)
    parser.add_argument("--rounds", type=int, required=True)
    parser.add_argument("--concurrency", type=int, required=True)
    parser.add_argument("--max-calls", type=int, required=True)
    parser.add_argument("--budget-cny", default="30.00")
    parser.add_argument("--call-timeout-seconds", type=float, default=120.0)
    parser.add_argument("--enable-live", action="store_true")
    parser.add_argument("--out", required=True)
    parser.add_argument("--db", required=True,
                        help="absolute path of the ledger database, outside every temp "
                             "root (e.g. evidence/<date>/live/db/<tier>.db)")
    parser.add_argument("--env", default=os.environ.get("RISKSHIELD_ENV_FILE"),
                        help="credentials file to read (default $RISKSHIELD_ENV_FILE); "
                             "no value from it is ever printed")
    parser.add_argument("--adapter", default=str(DEFAULT_ADAPTER),
                        help="path of the live adapter module")
    parser.add_argument("--seed", type=int, default=20261011)
    parser.add_argument("--refuse-existing", action="store_true",
                        help="refuse to start if the ledger path already exists, so a "
                             "re-run cannot silently overwrite an archived ledger")
    args = parser.parse_args()
    db_path = resolve_db_path(args.db)
    if args.refuse_existing and db_path.exists():
        raise SystemExit(f"--db {db_path} already exists; refusing to overwrite an "
                         f"existing ledger (move it or pass a new path)")
    if not args.env:
        raise SystemExit("no credentials file given; pass --env <path> or set "
                         "RISKSHIELD_ENV_FILE")
    env_path = Path(args.env).expanduser()
    if not env_path.is_file():
        raise SystemExit(f"credentials file not found: {env_path}")

    adapter_path = Path(args.adapter).expanduser()
    if not adapter_path.is_file():
        raise SystemExit(f"live adapter not found: {adapter_path}")
    spec = importlib.util.spec_from_file_location("bailian_ref", adapter_path)
    bailian = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(bailian)

    loaded = bailian.load_local_env(env_path)
    # Only the count is reported: never a key and never a value.
    print(f"credentials keys loaded: {len(loaded)}", flush=True)

    config = bailian.BailianConfig.from_env(env_path, require_api_key=args.enable_live)
    backend = bailian.BailianBackend(config, enable_live=args.enable_live,
                                     max_calls=args.max_calls)
    try:
        print(f"routes: {json.dumps(config.describe_routes() if hasattr(config, 'describe_routes') else {m: r.model for m, r in config.routes.items()}, ensure_ascii=False)}", flush=True)
    except Exception:
        pass

    store = Store(db_path)
    engine = Day3Simulation(store)
    prepare_synthetic_case(store)
    run_id = engine.create_run(CASE_ID, GRAPH_ID, agent_count=args.agents,
                               rounds=args.rounds, concurrency=args.concurrency,
                               seed=args.seed, budget_cny=Decimal(args.budget_cny))
    print(f"ledger: {db_path} (outside every temp root; survives the run)", flush=True)

    # Preflight: validate every observation the whole run will ever build, and total the
    # reservation. A shape mismatch here costs nothing and must be found before any call.
    total_reservation = Decimal("0")
    checked = 0
    run_row = engine._run_row(run_id)
    agents = engine._agents(run_id)
    for round_number in range(1, args.rounds + 1):
        observations = engine._context(run_row, agents, round_number)
        for observation in observations:
            total_reservation += backend.pricing(observation).reservation_cny
            checked += 1
    per_round = total_reservation / Decimal(args.rounds)
    print(f"preflight: validated {checked} observations across {args.rounds} rounds; "
          f"per-round reservation {per_round} CNY; whole-run {total_reservation} CNY; "
          f"budget {args.budget_cny} CNY", flush=True)
    if total_reservation > Decimal(args.budget_cny):
        print("ABORT: whole-run reservation exceeds the budget", flush=True)
        return 2

    started = time.perf_counter()
    try:
        result = engine.advance(run_id, backend, collect_performance=True,
                                call_timeout_seconds=args.call_timeout_seconds)
    finally:
        backend.close()
    wall = time.perf_counter() - started

    payload = {
        "tier": {"agents": args.agents, "rounds": args.rounds,
                 "concurrency": args.concurrency, "max_calls": args.max_calls,
                 "budget_cny": args.budget_cny, "enable_live": args.enable_live},
        "run_id": run_id,
        "case_id": CASE_ID, "graph_id": GRAPH_ID,
        "ledger_db": str(db_path),
        "ledger_db_sha256": hashlib.sha256(db_path.read_bytes()).hexdigest(),
        "wall_seconds": round(wall, 3),
        "status": result["status"],
        "completed_rounds": result["completed_rounds"],
        "action_counts": result["action_counts"],
        "all_decisions_valid": result["all_decisions_valid"],
        "cost_cny": result["cost_cny"],
        "usage": result["usage"],
        "opportunities": result["opportunities"],
        "billing": result["billing"],
        "stop_reason": ((result.get("performance") or {}).get("invocations") or [{}])[0]
        .get("stop_reason") if result.get("performance") else None,
        "preflight_round_reservation_cny": str(per_round),
        "preflight_whole_run_reservation_cny": str(total_reservation),
        "preflight_observations_validated": checked,
        "calls_made": backend.calls,
        "per_round": [{k: row.get(k) for k in (
            "round", "decision_requests", "valid_decisions", "failed_decisions",
            "not_dispatched_requests", "deadline_exceeded_requests", "messages",
            "elapsed_seconds", "cost_cny", "failure_policy", "round_abandoned")}
            for row in result["rounds"]],
    }
    target = Path(args.out)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")
    print(json.dumps({k: payload[k] for k in (
        "status", "completed_rounds", "action_counts", "all_decisions_valid",
        "cost_cny", "usage", "calls_made", "wall_seconds", "stop_reason")},
        ensure_ascii=False), flush=True)
    print(f"written {target}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
