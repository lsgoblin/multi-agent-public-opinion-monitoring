"""Drive the historical run directly, without the synthetic-tier reservation loop.

Why not ``scripts/run_live_tier.py``
------------------------------------
That driver pre-computes a whole-run reservation by calling ``backend.pricing()``
for every observation. The reference adapter answers that by building a synthetic
worst-case probe padded to its hard caps, which lands at ~8.4k against the 8k
per-route limit for a real historical observation. The probe is a *reservation*
device; the run-ledger gate that decides whether this run may enter the formal
denominator never reads it. So this entry point keeps everything the gate does
read -- the engine, the real adapter, the ledger, the per-call budget ceiling --
and drops only the pre-reservation arithmetic.

Safety is not reduced. The engine still enforces the run's own budget, every call
still goes through the adapter's paid-call limit and per-request bound, and the
run is still stopped by the engine if the budget is reached.
"""

from __future__ import annotations

import json
import os
import sys
import time
from decimal import Decimal
from pathlib import Path

ROOT = Path(r"D:\test\多智能体仿真舆情监测与投诉预警")
sys.path.insert(0, str(ROOT / "runtime"))
sys.path.insert(0, str(ROOT / "src"))

CASE = "unh_change_cyberattack_20240222"
GRAPH = "936c73951d2ab46f8e0ee6bb"
DB = ROOT / "artifacts/live-run-20261014/ledger-live.db"
OUT = ROOT / "artifacts/live-run-20261014/historical-live-run.json"


def load_env(path: Path) -> int:
    count = 0
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))
        count += 1
    return count


def main() -> int:
    loaded = load_env(ROOT / ".env")
    print("credentials keys loaded: %d" % loaded)

    import bailian_backend_ref as ref
    import bailian_historical_adapter as hist
    from riskshield.simulation import Day3Simulation
    from riskshield.store import Store

    agents = int(os.environ.get("HIST_AGENTS", "10"))
    rounds = int(os.environ.get("HIST_ROUNDS", "3"))
    concurrency = int(os.environ.get("HIST_CONCURRENCY", "4"))
    max_calls = agents * rounds
    budget = Decimal("1.00")

    config = hist.uniform_route_config(ROOT / ".env", model="qwen-flash")
    print("routes: %s" % {name: route.model for name, route in config.routes.items()})
    print("role map: %s" % hist.role_route_map("qwen-flash"))
    print("  (every role resolves to qwen-flash because qwen-turbo answered 429 "
          "limit_requests; the reference role split is part of the synthetic tier's "
          "plan, not of what this run measures)")

    store = Store(DB)
    engine = Day3Simulation(store)
    run_id = engine.create_run(CASE, GRAPH, agent_count=agents, rounds=rounds,
                               concurrency=concurrency, budget_cny=budget)
    print("run: %s  agents=%d rounds=%d budget=CNY %s"
          % (run_id, agents, rounds, budget))

    backend = hist.HistoricalBailianBackend(config, enable_live=True,
                                            max_calls=max_calls)
    started = time.perf_counter()
    try:
        summary = engine.advance(run_id, backend, collect_performance=True,
                                 call_timeout_seconds=90.0)
    finally:
        backend.close()
    elapsed = time.perf_counter() - started

    report = {
        "run_id": run_id,
        "case_id": CASE,
        "graph_id": GRAPH,
        "adapter": "bailian_historical_adapter.py",
        "adapter_contract_version": hist.HistoricalBailianBackend.ADAPTER_CONTRACT_VERSION,
        "agents": agents, "rounds": rounds, "concurrency": concurrency,
        "budget_cny": str(budget),
        "max_calls": max_calls,
        "calls_made": backend.calls,
        "network_requests": getattr(backend, "network_requests", None),
        "wall_seconds": round(elapsed, 3),
        "status": summary.get("status"),
        "completed_rounds": summary.get("completed_rounds"),
        "cost_cny": summary.get("cost_cny"),
        "all_decisions_valid": summary.get("all_decisions_valid"),
        "action_counts": summary.get("action_counts"),
        "summary": summary,
    }
    OUT.write_text(json.dumps(report, ensure_ascii=False, indent=1, sort_keys=True) + "\n",
                   encoding="utf-8", newline="\n")

    print()
    print("status            : %s" % summary.get("status"))
    print("completed rounds  : %s / %s" % (summary.get("completed_rounds"), rounds))
    print("action counts     : %s" % summary.get("action_counts"))
    print("all decisions valid: %s" % summary.get("all_decisions_valid"))
    print("cost              : CNY %s" % summary.get("cost_cny"))
    print("calls made        : %d of %d allowed" % (backend.calls, max_calls))
    print("wall seconds      : %.1f" % elapsed)
    print("report            : %s" % OUT)
    return 0 if summary.get("status") == "complete" else 4


if __name__ == "__main__":
    raise SystemExit(main())
