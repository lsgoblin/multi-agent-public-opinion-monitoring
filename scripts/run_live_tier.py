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
from datetime import datetime
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


class _DryRunReservation:
    """The reservation arithmetic a dry run needs, without any live client.

    A dry run totals what the whole run would reserve so the budget bound can be checked
    before anything is dispatched. That is a local computation over configured prices, so it
    must not require importing the live adapter, holding credentials, or having a client in
    the process.
    """

    mode = "test_substitute"
    model = "dry-run-reservation"
    price_version = "dry-run-reservation-v1"
    describe_routes = None

    # Defaults grounded in the measured 500x30 run rather than invented: that run cost
    # 2.88427524 CNY for 15,000 calls, i.e. 0.000192 CNY per call. The default below carries
    # roughly a 2.6x margin over the measured rate, so a reservation that passes the budget
    # check is not an optimistic fiction, and a run whose real cost would breach the cap is
    # still refused. Passing --dry-run-prices replaces these with the adapter's real prices.
    DEFAULT_RESERVATION_CNY = "0.0005"

    def __init__(self, *, prices: dict | None = None):
        self._prices = prices or {}
        self.calls = 0
        self.network_requests = 0

    def pricing(self, observation):
        from riskshield.simulation import DecisionPricing
        role = observation["persona"]["role"]
        spec = self._prices.get(role) or self._prices.get("*") or {}
        return DecisionPricing(
            requested_model=spec.get("model", f"dry-run-{role}"),
            price_version=self.price_version,
            reservation_cny=Decimal(str(spec.get("reservation_cny",
                                                self.DEFAULT_RESERVATION_CNY))),
            input_cny_per_million=Decimal(str(spec.get("input_cny_per_million", "0"))),
            cached_input_cny_per_million=Decimal(str(
                spec.get("cached_input_cny_per_million", "0"))),
            output_cny_per_million=Decimal(str(spec.get("output_cny_per_million", "0"))))

    def describe(self):
        return {"type": "dry_run_reservation", "model_calls": 0, "network_requests": 0}

    def decide(self, observation):
        raise AssertionError("a dry run must not dispatch a decision")


def parse_instant(value, *, what: str):
    """Parse an ISO time to an aware instant, returning ``(instant, problem)``.

    A Z suffix and a +00:00 offset denote the same instant but are different strings, so the
    cutoff boundary must be compared as time rather than as text. A value with no timezone is
    refused rather than compared naively.
    """
    if isinstance(value, datetime):
        instant = value
    elif isinstance(value, str) and value.strip():
        try:
            instant = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None, f"{what} is not a parsable time: {value!r}"
    else:
        return None, f"{what} is missing"
    if instant.tzinfo is None or instant.utcoffset() is None:
        return None, f"{what} carries no timezone: {value!r}"
    return instant, None


def resolve_db_path(raw: str) -> Path:
    """Resolve --db and refuse a path that would put the ledger back in a temp area.

    The 500x30 run wrote its 15,000-row ledger into a ``tempfile.mkdtemp`` directory,
    and the archiving step afterwards searched the wrong temp root and recorded that
    ledger as permanently lost. It survived only by luck. The ledger is the only
    artifact carrying the per-request audit trail, so the runner now refuses to start
    unless the database lands in a persistent directory outside every temp root.

    One exemption: a path inside this checkout. A checkout is a real directory on disk
    whether or not it happens to sit under a temp root -- someone can clone into
    ``D:\\temp`` or run CI in a temp workspace -- and its files are not swept by temp
    cleanup, which is the failure this guard exists to prevent. Without the exemption a
    cloned checkout could not run its own CLI tests.
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
    checkout = PROJECT_ROOT.resolve()
    inside_checkout = target == checkout or checkout in target.parents
    for root in temp_roots:
        if (target == root or root in target.parents) and not inside_checkout:
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
    parser.add_argument("--case-id", default=None,
                        help="an already imported real_historical case; omitted means the "
                             "synthetic fixture is used")
    parser.add_argument("--graph-id", default=None,
                        help="graph of that case; the first by id is used when omitted")
    parser.add_argument("--dry-run", action="store_true",
                        help="build the run and validate the cutoff boundary and the "
                             "reservation, dispatch nothing, then stop")
    parser.add_argument("--dry-run-prices", default=None,
                        help="JSON object of per-role reservation prices for a dry run; "
                             "without it a documented default reservation is used")
    parser.add_argument("--refuse-existing", action="store_true",
                        help="refuse to start if the ledger path already exists, so a "
                             "re-run cannot silently overwrite an archived ledger")
    args = parser.parse_args()
    db_path = resolve_db_path(args.db)
    if args.refuse_existing and db_path.exists():
        raise SystemExit(f"--db {db_path} already exists; refusing to overwrite an "
                         f"existing ledger (move it or pass a new path)")

    # The real backend, its credentials and the adapter are only needed to dispatch. A dry run
    # must be runnable on a machine with neither, so it validates the entry point and the
    # cutoff boundary without touching them.
    backend = None
    if not args.dry_run:
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
            routes = (config.describe_routes() if hasattr(config, "describe_routes")
                      else {model: route.model for model, route in config.routes.items()})
            print(f"routes: {json.dumps(routes, ensure_ascii=False)}", flush=True)
        except Exception:
            pass
    else:
        # Dry run: the same reservation arithmetic, computed locally, with no credential and
        # no adapter in the process at all.
        backend = _DryRunReservation(
            prices=json.loads(args.dry_run_prices) if args.dry_run_prices else {})

    store = Store(db_path)
    engine = Day3Simulation(store)
    # The case is a parameter, not a constant. Previously this driver always installed the
    # synthetic fixture, which made it unusable as a real historical entry point even though
    # its throughput result was real: the case it ran was not.
    if args.case_id:
        # A real historical case must already be imported, with its cutoff graph built, so
        # this driver never invents either.
        try:
            metadata = store.get_case(args.case_id)
        except KeyError:
            raise SystemExit(f"case {args.case_id!r} is not in this store; import it first")
        if metadata.get("data_mode") != "real_historical":
            raise SystemExit(
                f"case {args.case_id!r} has data_mode {metadata.get('data_mode')!r}; this "
                f"entry point runs real historical material only")
        graph_id = args.graph_id
        if not graph_id:
            with store.connect() as db:
                graphs = [row["graph_id"] for row in db.execute(
                    "SELECT graph_id FROM graphs WHERE case_id=? ORDER BY graph_id",
                    (args.case_id,))]
            if not graphs:
                raise SystemExit(f"case {args.case_id!r} has no graph; build one first")
            graph_id = graphs[0]
        snapshot = store.snapshot(args.case_id)
        with store.connect() as db:
            graph_row = db.execute("SELECT body FROM graphs WHERE graph_id=?",
                                   (graph_id,)).fetchone()
        if graph_row is None:
            raise SystemExit(f"graph {graph_id!r} is not in this store")
        graph = json.loads(graph_row["body"])
        claims = graph.get("claims") or []
        if not claims:
            raise SystemExit(f"graph {graph_id!r} has no cutoff-eligible claims")
        cutoff = snapshot["cutoff"]
        # Compare instants, not strings. The graph stores times as "...Z" while the snapshot
        # cutoff can read back as "...+00:00"; as text those differ, so a string comparison
        # would refuse material that is plainly inside the cutoff.
        cutoff_instant, problem = parse_instant(cutoff, what="case cutoff")
        if problem:
            raise SystemExit(problem)
        for claim in claims:
            available_instant, problem = parse_instant(
                claim.get("available_at"),
                what=f"graph claim {claim.get('claim_id')!r} availability")
            if problem:
                raise SystemExit(problem)
            if available_instant > cutoff_instant:
                raise SystemExit(
                    f"graph claim {claim.get('claim_id')!r} is visible after the cutoff "
                    f"{cutoff}; the run would consume post-cutoff material")
        print(f"material: real_historical case={snapshot['case_id']} "
              f"cutoff={cutoff} source_version={snapshot['version']} "
              f"records={len(snapshot['records'])}", flush=True)
        print(f"graph   : {graph_id} claims={len(claims)} "
              f"build_mode={graph.get('build_mode')}", flush=True)
        case_id, graph_id = args.case_id, graph_id
    else:
        prepare_synthetic_case(store)
        case_id, graph_id = CASE_ID, GRAPH_ID
        print(f"material: synthetic fixture case={case_id}", flush=True)

    run_id = engine.create_run(case_id, graph_id, agent_count=args.agents,
                               rounds=args.rounds, concurrency=args.concurrency,
                               seed=args.seed, budget_cny=Decimal(args.budget_cny))
    print(f"run     : {run_id}", flush=True)
    print(f"ledger  : {db_path} (outside every temp root; survives the run)", flush=True)

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

    if args.dry_run:
        # A dry run answers one question: would this configuration run, and does its input
        # respect the cutoff. It dispatches nothing, so it must report zero calls -- and it
        # says so from the objects it built rather than from an assumption.
        calls_made = getattr(backend, "calls", 0)
        print(json.dumps({
            "dry_run": True,
            "run_id": run_id,
            "case_id": case_id,
            "data_mode": store.get_case(case_id)["data_mode"],
            "observations_validated": checked,
            "whole_run_reservation_cny": str(total_reservation),
            "per_round_reservation_cny": str(per_round),
            "budget_cny": args.budget_cny,
            "model_calls": calls_made,
            "network_requests": getattr(backend, "network_requests", 0),
            "enable_live": args.enable_live,
            "note": ("no request was dispatched; this proves the entry point and the cutoff "
                     "boundary, not the run"),
        }, ensure_ascii=False, indent=1), flush=True)
        if calls_made:
            print("ABORT: a dry run dispatched a call", flush=True)
            return 1
        return 0

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
        "case_id": case_id, "graph_id": graph_id,
        "data_mode": store.get_case(case_id)["data_mode"],
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
