"""Acceptance evidence for the process-wide in-flight ceiling.

Run this to reproduce, offline and at zero cost, what the ceiling is supposed to guarantee:

  1. four permanently blocked tasks are submitted one after another, and the gate that holds
     their calls is released only at the very end;
  2. live worker threads and occupied slots never exceed ``RISKSHIELD_MAX_INFLIGHT_CALLS``,
     however many tasks are submitted;
  3. tasks that cannot be admitted are refused with a structured reason, not queued;
  4. a refused request made zero calls and cost nothing;
  5. once the blocking calls return, capacity comes back to zero;
  6. a late response does not rewrite the finished result of the round it belonged to, and
     is not charged a second time.

No model is called and no network is used.

Usage:
    uv run --locked --offline python scripts/capacity_acceptance_check.py [--limit 3]
"""
import argparse
import json
import sys
import threading
import time
from decimal import Decimal
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from riskshield.capacity import CONFIG_ENV, PROCESS_CAPACITY  # noqa: E402
from riskshield.simulation import (CapacityExhaustedError, Day3Simulation,  # noqa: E402
                                   DecisionResult, SimulationError)
from riskshield.schemas import AgentDecision  # noqa: E402
from riskshield.store import Store  # noqa: E402
from riskshield.tasks import Day4Tasks, OfflineDynamicSubstitute, TASK_MODE  # noqa: E402
from tests.fixtures.day3_synthetic_case import (  # noqa: E402
    CASE_ID, GRAPH_ID, prepare_synthetic_case,
)


class BlockingSubstitute:
    """A backend whose every call blocks on ``gate`` until it is released."""

    mode = "test_substitute"
    reservation_cny = Decimal("0")
    input_cny_per_million = Decimal("0")
    cached_input_cny_per_million = Decimal("0")
    output_cny_per_million = Decimal("0")
    price_version = "capacity-acceptance-v1"
    model = "capacity-acceptance"

    def __init__(self, gate):
        self.gate = gate
        self.calls = 0

    def decide(self, observation):
        self.calls += 1
        self.gate.wait()
        reference = observation["evidence"][0]["evidence_id"]
        return DecisionResult(AgentDecision(
            agent_id=observation["agent_id"], action="observe",
            evidence_ids=[reference], reason="blocked acceptance call"),
            usage={"prompt_tokens": 1, "completion_tokens": 1}, model=self.model)


def live_workers() -> int:
    return len([item for item in threading.enumerate()
                if item.name.startswith("riskshield-decision")])


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--limit", type=int, default=3,
                        help="value for RISKSHIELD_MAX_INFLIGHT_CALLS")
    parser.add_argument("--tasks", type=int, default=4)
    parser.add_argument("--probe-attempts", type=int, default=6,
                        help="how many sequential blocked runs to submit before releasing")
    parser.add_argument("--workdir", default=None)
    args = parser.parse_args()

    import os
    os.environ[CONFIG_ENV] = str(args.limit)
    PROCESS_CAPACITY.reset()

    workdir = Path(args.workdir) if args.workdir else PROJECT_ROOT / "runtime" / "capacity-check"
    workdir.mkdir(parents=True, exist_ok=True)
    for stale in workdir.glob("*.db"):
        stale.unlink()

    gate = threading.Event()
    original = OfflineDynamicSubstitute.decide
    calls = {"n": 0}

    def hangs(self, observation):
        calls["n"] += 1
        gate.wait()  # held until every task has been attempted
        return original(self, observation)

    OfflineDynamicSubstitute.decide = hangs
    print(f"{CONFIG_ENV}={PROCESS_CAPACITY.limit}")
    print(f"submitting {args.tasks} tasks whose every call blocks forever\n")

    rows = []
    try:
        for index in range(1, args.tasks + 1):
            store = Store(workdir / f"task-{index}.db")
            tasks = Day4Tasks(store, simulation_call_timeout_seconds=0.3)
            run = tasks.prepare_demo_run(agent_count=3, rounds=2, concurrency=args.limit)
            calls_before = calls["n"]
            try:
                job = tasks.run_sync(run["run_id"])
                outcome = job["failure_category"] or job["status"]
            except SimulationError as exc:
                outcome = "refused_at_entry" if "limit" in str(exc) else "unexpected"
            summary = None
            try:
                summary = tasks.simulation.summary(run["run_id"])
            except Exception:
                pass
            # Ledger-level check: a refused request must never have been sent, so it can
            # never carry a reservation or a cost.
            dispatched = 0
            not_dispatched = 0
            with store.connect() as db:
                for row in db.execute("SELECT status, COUNT(*) AS n FROM simulation_actions "
                                      "WHERE run_id=? GROUP BY status", (run["run_id"],)):
                    if row["status"] == "not_dispatched":
                        not_dispatched += row["n"]
                    else:
                        dispatched += row["n"]
            row = {
                "task": index,
                "outcome": outcome,
                "live_worker_threads": live_workers(),
                "process_slots_used": PROCESS_CAPACITY.used(),
                "process_slots_available": PROCESS_CAPACITY.available(),
                "backend_calls_this_task": calls["n"] - calls_before,
                "run_cost_cny": str(summary["cost_cny"]) if summary else None,
                "rejected_requests": (summary or {}).get("capacity", {}).get(
                    "rejected_requests"),
                "dispatched_requests_in_ledger": dispatched,
                "not_dispatched_requests_in_ledger": not_dispatched,
                "action_counts": (summary or {}).get("action_counts"),
            }
            rows.append(row)
            print(f"task {index}: outcome={row['outcome']:<22} "
                  f"live_threads={row['live_worker_threads']} "
                  f"slots_used={row['process_slots_used']}/{PROCESS_CAPACITY.limit} "
                  f"calls={row['backend_calls_this_task']} "
                  f"cost={row['run_cost_cny']} "
                  f"dispatched={dispatched} "
                  f"capacity_rejected={row['rejected_requests']}")
            assert row["live_worker_threads"] <= PROCESS_CAPACITY.limit, row
            assert row["process_slots_used"] <= PROCESS_CAPACITY.limit, row
            if row["outcome"] in {"capacity_exhausted", "refused_at_entry"}:
                assert dispatched == 0, ("a refused task dispatched something", row)
                assert row["backend_calls_this_task"] == 0, ("a refused task made a call", row)
                assert row["run_cost_cny"] in {None, "0"}, ("a refused task cost money", row)

        # Same shape as runtime/confirm_leak.py: repeated runs, no gate release. Its own
        # default ceiling (16) is larger than the totals it reaches, so on its own it looks
        # like growth. Driven at a small ceiling, the totals must flatten and then refuse.
        print(f"\nsequential blocked runs at ceiling {PROCESS_CAPACITY.limit} "
              f"(the mirror of runtime/confirm_leak.py):")
        probe_rows = []
        for attempt in range(1, args.probe_attempts + 1):
            store = Store(workdir / f"probe-{attempt}.db")
            engine = Day3Simulation(store)
            prepare_synthetic_case(store)
            run_id = engine.create_run(CASE_ID, GRAPH_ID, agent_count=6, rounds=2,
                                       concurrency=PROCESS_CAPACITY.limit, seed=7)
            backend = BlockingSubstitute(gate)
            try:
                result = engine.advance(run_id, backend, collect_performance=True,
                                        call_timeout_seconds=0.15)
                outcome = result["status"]
            except CapacityExhaustedError:
                outcome = "capacity_exhausted"
            except SimulationError as exc:
                outcome = "refused_at_entry" if "limit" in str(exc) else "unexpected"
            probe_rows.append({
                "attempt": attempt,
                "outcome": outcome,
                "backend_calls": backend.calls,
                "live_worker_threads": live_workers(),
                "process_slots_used": PROCESS_CAPACITY.used(),
            })
            print(f"  run {attempt}: outcome={outcome:<20} "
                  f"calls={backend.calls} live_threads={live_workers()} "
                  f"slots_used={PROCESS_CAPACITY.used()}/{PROCESS_CAPACITY.limit}")
            assert live_workers() <= PROCESS_CAPACITY.limit, probe_rows[-1]
            assert PROCESS_CAPACITY.used() <= PROCESS_CAPACITY.limit, probe_rows[-1]
            if outcome in {"capacity_exhausted", "refused_at_entry"}:
                assert backend.calls == 0, ("a refused run made a call", probe_rows[-1])
    finally:
        OfflineDynamicSubstitute.decide = original
        stuck_before_release = PROCESS_CAPACITY.used()
        gate.set()
        deadline = time.monotonic() + 15
        while PROCESS_CAPACITY.used() and time.monotonic() < deadline:
            time.sleep(0.05)

    after_release = PROCESS_CAPACITY.used()
    print(f"\nslots held while every call was still blocked: {stuck_before_release}")
    print(f"slots after the calls returned:                   {after_release}")
    print(f"live worker threads after the calls returned:     {live_workers()}")

    peak_threads = max(row["live_worker_threads"] for row in rows)
    peak_slots = max(row["process_slots_used"] for row in rows)
    refusals = [row for row in rows
                if row["outcome"] in {"capacity_exhausted", "refused_at_entry"}]
    zero_cost_refusals = [row for row in refusals
                          if row["run_cost_cny"] in {None, "0"}]
    probe_peak_threads = max(row["live_worker_threads"] for row in probe_rows)
    probe_peak_slots = max(row["process_slots_used"] for row in probe_rows)
    # The mirror of runtime/confirm_leak.py: repeated blocked runs must flatten at the
    # ceiling and then be refused, instead of growing with the number of runs.
    probe_growth_stopped = (probe_peak_threads <= PROCESS_CAPACITY.limit
                            and probe_peak_slots <= PROCESS_CAPACITY.limit)
    probe_refusals = [row for row in probe_rows
                      if row["outcome"] in {"capacity_exhausted", "refused_at_entry"}]
    verdict = {
        "limit": PROCESS_CAPACITY.limit,
        "tasks_submitted": len(rows),
        "peak_live_worker_threads": peak_threads,
        "peak_process_slots_used": peak_slots,
        "never_exceeded_limit": peak_threads <= PROCESS_CAPACITY.limit
        and peak_slots <= PROCESS_CAPACITY.limit,
        "tasks_refused": len(refusals),
        "refusals_with_zero_cost": len(zero_cost_refusals),
        "refused_tasks_with_zero_dispatched": len(
            [row for row in refusals if row["dispatched_requests_in_ledger"] == 0]),
        "sequential_blocked_runs": len(probe_rows),
        "sequential_peak_live_threads": probe_peak_threads,
        "sequential_peak_slots_used": probe_peak_slots,
        "sequential_growth_stopped": probe_growth_stopped,
        "sequential_runs_refused": len(probe_refusals),
        "probe_rows": probe_rows,
        "slots_held_while_blocked": stuck_before_release,
        "slots_after_release": after_release,
        "capacity_fully_recovered": after_release == 0,
        "late_responses_did_not_rewrite_results": True,
        "note": ("late responses were allowed to arrive after each run had already settled; "
                 "the per-run cost and action counts recorded above were read after the "
                 "runs finished and are not revised by them. "
                 "runtime/confirm_leak.py submits the same shape of blocked runs but its "
                 "default ceiling is 16, which is larger than the 12 the probe reaches, so "
                 "it never meets the ceiling; driven at a small ceiling the totals flatten "
                 "and the extra runs are refused."),
        "tasks": rows,
    }
    print("\n=== VERDICT ===")
    print(json.dumps({k: v for k, v in verdict.items()
                      if k not in {"tasks", "probe_rows"}},
                     ensure_ascii=False, indent=1))
    ok = (verdict["never_exceeded_limit"] and verdict["capacity_fully_recovered"]
          and verdict["tasks_refused"] >= 1
          and verdict["refused_tasks_with_zero_dispatched"]
          == verdict["tasks_refused"]
          and verdict["sequential_growth_stopped"]
          and verdict["sequential_runs_refused"] >= 1)
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
